"""Reviewer identity, authorisation and manifest integrity (Step 5C.6 sections 11-18).

WHAT THESE PROTECT
==================
The one thing standing between a proposal and a trust decision is a person. Every test
here is a way that could stop being true without anybody noticing: an invented actor, a
missing actor, an actor with the wrong role, a fixture identity mistaken for a real one,
or an approved manifest quietly replaced by a regenerated one between the reading and
the applying.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Connection, text

from app.domains.verification.identity import (
    TEST_MARKER,
    VERIFY_PERMISSION,
    IdentityRefusedError,
    NotAuthorizedError,
    authorize,
    permissions_of,
    provision_reviewer,
)

pytestmark = pytest.mark.integration


def _reviewer(conn: Connection, *, role: str = "reviewer", suffix: str = "") -> uuid.UUID:
    unique = uuid.uuid4().hex[:8]
    return provision_reviewer(
        conn,
        email=f"reviewer-{unique}{suffix}@example.test",
        display_name=f"Fixture reviewer {unique}",
        role=role,
        test_only=True,
    ).id


# ===========================================================================
# 11-12. Provisioning, and the role it uses
# ===========================================================================


def test_the_existing_reviewer_role_carries_the_verify_permission(conn: Connection) -> None:
    """Section 12. One reviewer role, not two.

    `role.reviewer` has existed since the RBAC migration with `is_reviewer_role = true`.
    Step 5C.6 adds a permission to it; adding a second role would have left two things
    to keep in step.
    """
    row = conn.execute(text("SELECT code, is_reviewer_role FROM role WHERE is_reviewer_role")).all()
    assert [entry.code for entry in row] == ["reviewer"], "there must be exactly one reviewer role"
    held = {
        entry.permission_code
        for entry in conn.execute(
            text("SELECT permission_code FROM role_permission WHERE role_code = 'reviewer'")
        )
    }
    assert VERIFY_PERMISSION in held


def test_verifying_is_not_the_same_permission_as_registering_a_source(
    conn: Connection,
) -> None:
    """A data editor may add a URL and may not declare it official."""
    editor = {
        entry.permission_code
        for entry in conn.execute(
            text("SELECT permission_code FROM role_permission WHERE role_code = 'data_editor'")
        )
    }
    assert "source:manage" in editor
    assert VERIFY_PERMISSION not in editor


def test_provisioning_refuses_to_invent_an_identity(conn: Connection) -> None:
    """Section 11. No default email, no default name, and no guessing."""
    for email, name, message in (
        ("", "Someone", "email"),
        ("not-an-email", "Someone", "email"),
        ("real@example.test", "   ", "display name"),
    ):
        with pytest.raises(IdentityRefusedError, match=message):
            provision_reviewer(conn, email=email, display_name=name, test_only=True)


def test_a_test_identity_is_marked_and_cannot_pass_for_a_real_one(conn: Connection) -> None:
    """Section 13. A fixture decision must be recognisable in the audit trail."""
    reviewer = provision_reviewer(
        conn,
        email=f"fixture-{uuid.uuid4().hex[:8]}@example.test",
        display_name="Pilot reviewer",
        test_only=True,
    )
    assert reviewer.display_name.startswith(TEST_MARKER)
    assert reviewer.is_test

    # A reserved domain without the flag is refused...
    with pytest.raises(IdentityRefusedError, match="reserved test domain"):
        provision_reviewer(
            conn, email="someone@example.test", display_name="Real person", test_only=False
        )
    # ...and the flag with a real domain is refused too, so `test_only` cannot be used
    # to mark a real identity as fake or the reverse.
    with pytest.raises(IdentityRefusedError, match="reserved domain"):
        provision_reviewer(
            conn, email="someone@a-real-university.ac.uk", display_name="X", test_only=True
        )


def test_provisioning_sets_no_password(conn: Connection) -> None:
    """An identity that cannot sign in is the correct state until there is a front end."""
    actor = _reviewer(conn)
    assert (
        conn.execute(
            text("SELECT password_hash FROM app_user WHERE id = :i"), {"i": actor}
        ).scalar()
        is None
    )


def test_provisioning_is_idempotent_for_the_same_person(conn: Connection) -> None:
    email = f"repeat-{uuid.uuid4().hex[:8]}@example.test"
    first = provision_reviewer(conn, email=email, display_name="A", test_only=True)
    second = provision_reviewer(conn, email=email, display_name="A", test_only=True)
    assert first.id == second.id
    assert (
        conn.execute(
            text("SELECT count(*) FROM user_role WHERE user_id = :i"), {"i": first.id}
        ).scalar()
        == 1
    )


# ===========================================================================
# 13. Authorisation
# ===========================================================================


def test_no_actor_is_refused_before_anything_else(conn: Connection) -> None:
    with pytest.raises(NotAuthorizedError, match="requires an actor"):
        authorize(conn, None)


def test_an_unknown_actor_is_refused(conn: Connection) -> None:
    with pytest.raises(IdentityRefusedError, match="no app_user"):
        authorize(conn, uuid.uuid4())


def test_an_actor_without_the_permission_is_refused_by_name(conn: Connection) -> None:
    """The refusal says which roles they hold, because that is what they must change."""
    editor = _reviewer(conn, role="data_editor", suffix="-editor")
    with pytest.raises(NotAuthorizedError, match="data_editor"):
        authorize(conn, editor)


def test_a_deactivated_reviewer_is_refused(conn: Connection) -> None:
    actor = _reviewer(conn)
    authorize(conn, actor)
    conn.execute(
        text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
        {"i": actor},
    )
    with pytest.raises(NotAuthorizedError, match="deactivated"):
        authorize(conn, actor)


def test_an_authorized_reviewer_resolves_with_its_permissions(conn: Connection) -> None:
    actor = _reviewer(conn)
    reviewer = authorize(conn, actor)
    assert reviewer.roles == ("reviewer",)
    assert VERIFY_PERMISSION in permissions_of(conn, actor)


# ===========================================================================
# 16-17. Manifest integrity
# ===========================================================================


def _manifest(tmp_path: Path, rows: list[dict[str, object]], *, name: str = "manifest") -> Path:
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    envelope = {
        "manifest": "domain_decisions_proposed",
        "manifest_version": "5c5.1",
        "row_count": len(rows),
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "rows": rows,
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return path


def test_a_manifest_edited_after_generation_is_refused(tmp_path: Path) -> None:
    """Section 17, the inner check: the file must match its own hash."""
    from scripts.source_review import _load_manifest

    path = _manifest(tmp_path, [{"host": "a.ac.uk", "decision": "VERIFIED_OFFICIAL"}])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["rows"][0]["decision"] = "REJECTED"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(SystemExit, match="edited since it was generated"):
        _load_manifest(path, None)


def test_applying_a_different_manifest_than_the_one_reviewed_is_refused(
    tmp_path: Path,
) -> None:
    """Section 17, the outer check, and the failure mode it names.

    A reviewer reads manifest A. Somebody regenerates it into manifest B. Without the
    expected digest, the apply command would silently apply B.
    """
    from scripts.source_review import _load_manifest

    # Distinct filenames, deliberately: writing both to one path would overwrite the
    # reviewed manifest and the test would prove nothing about which file was applied.
    reviewed = _manifest(
        tmp_path, [{"host": "a.ac.uk", "decision": "VERIFIED_OFFICIAL"}], name="reviewed"
    )
    reviewed_digest = json.loads(reviewed.read_text(encoding="utf-8"))["content_sha256"]

    regenerated = _manifest(
        tmp_path,
        [
            {"host": "a.ac.uk", "decision": "VERIFIED_OFFICIAL"},
            {"host": "b.ac.uk", "decision": "VERIFIED_OFFICIAL"},
        ],
        name="regenerated",
    )
    with pytest.raises(SystemExit, match="not the manifest that was reviewed"):
        _load_manifest(regenerated, reviewed_digest)

    # The one that was reviewed still applies.
    assert len(_load_manifest(reviewed, reviewed_digest)) == 1


def test_the_real_proposal_manifests_carry_only_needs_review() -> None:
    """Section 16. A missing decision is never read as VERIFIED.

    The generated proposals are all `NEEDS_REVIEW`, so applying one is a no-op by
    construction. That is the property worth asserting: not that the apply command
    happens to refuse, but that there is nothing in the file to approve.
    """
    directory = Path(__file__).resolve().parents[2] / ".reports" / "step-5c5"
    if not directory.exists():
        pytest.skip("no generated manifest in this working copy; run verify-manifest")
    for name in (
        "domain_decisions_proposed",
        "source_decisions_proposed",
        "responsibility_decisions_proposed",
    ):
        envelope = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        decisions = {row["proposed_decision"] for row in envelope["rows"]}
        assert decisions == {"NEEDS_REVIEW"}, f"{name} carries {decisions}"
