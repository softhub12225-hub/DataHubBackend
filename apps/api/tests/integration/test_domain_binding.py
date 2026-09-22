"""A domain decision must name the institution whose packet was reviewed.

WHAT THIS CLOSES
================
`source_review.py domain` created `official_domain` rows with `target_institution_id`
NULL unless `--institution` was passed, and the pilot workflow never passed it. Dejan's
three ANU decisions would have produced three verified hosts attached to no institution.

Nothing downstream would have complained. Promotion joins
`source_mapping.official_domain_id` and reads `od.verification_status` and
`od.is_active`; it never reads the domain row's institution. So the gap was invisible to
every existing test, and `test_a_new_decision_without_an_institution_is_refused` is the
one that would have caught it.

WHY THE MANIFEST IS THE AUTHORITY
=================================
The binding is checked against the frozen review manifest rather than against whatever
the database relates, for one decisive reason: **a redirect-only host has no database
association at all.** `study.anu.edu.au` has no `pilot_collected_source` row -- it was
never submitted, only redirected to from `www.anu.edu.au`. Its link to ANU exists solely
in the manifest row. A binding resolved from the database would have to refuse such a
host or invent its institution.

HERMETIC BY CONSTRUCTION
========================
These tests build their own institutions and their own manifest file in `tmp_path`. They
do **not** read the real ANU rows: `datahub_test` has an empty target plane, and a test
that needs production data in the test database is a test that only passes on one
machine. The real ANU facts are verified against `datahub` as reported evidence for the
step -- see the note at the foot of this file for why they are not asserted here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.verification.domain_binding import (
    BindingAction,
    InstitutionBindingError,
    ManifestChangedError,
    load_domain_manifest,
    manifest_digest,
    plan_binding,
    require_binding,
    resolve_institution,
    reviewed_host,
)

pytestmark = pytest.mark.integration


def _institution(conn: Connection, label: str) -> uuid.UUID:
    """A target institution with a given match_key, plus the list row it requires."""
    list_id, target_id = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, "
            "  file_sha256, file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:id, 'Binding List', :ver, 'b.xlsx', :sha, 1, 's', 1)"
        ),
        {"id": list_id, "ver": list_id.hex[:8], "sha": list_id.hex * 2},
    )
    conn.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
            "  latest_list_id) VALUES (:id, :key, :list, :list)"
        ),
        {"id": target_id, "key": label, "list": list_id},
    )
    return target_id


def _write_manifest(directory: Path, rows: list[dict[str, Any]]) -> str:
    """A manifest file shaped like the real one, and its digest."""
    digest = manifest_digest(rows)
    (directory / "domain_decisions_proposed.json").write_text(
        json.dumps(
            {"manifest_version": "test", "content_sha256": digest, "rows": rows},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return digest


@pytest.fixture
def package(conn: Connection, tmp_path: Path) -> dict[str, Any]:
    """Two institutions and a manifest reviewing three hosts for the first of them.

    Mirrors the real ANU shape: two submitted hosts and one reached only by redirect,
    which is the case the manifest-as-authority rule exists for.
    """
    reviewed = _institution(conn, "reviewed university (ru)")
    other = _institution(conn, "some other university (sou)")
    rows = [
        {
            "host": "www.reviewed.example",
            "institution": "reviewed university (ru)",
            "reached_only_by_redirect": False,
            "proposed_decision": "NEEDS_REVIEW",
            "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
        },
        {
            "host": "courses.reviewed.example",
            "institution": "reviewed university (ru)",
            "reached_only_by_redirect": False,
            "proposed_decision": "NEEDS_REVIEW",
            "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
        },
        {
            "host": "study.reviewed.example",
            "institution": "reviewed university (ru)",
            "reached_only_by_redirect": True,
            "proposed_decision": "NEEDS_REVIEW",
            "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
        },
        {
            "host": "www.other.example",
            "institution": "some other university (sou)",
            "reached_only_by_redirect": False,
            "proposed_decision": "NEEDS_REVIEW",
            "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
        },
    ]
    return {
        "dir": tmp_path,
        "sha": _write_manifest(tmp_path, rows),
        "rows": rows,
        "reviewed": reviewed,
        "other": other,
        "submitted": "www.reviewed.example",
        "second": "courses.reviewed.example",
        "redirect_only": "study.reviewed.example",
        "other_host": "www.other.example",
    }


# ===========================================================================
# 1. Resolution, and every host mapping to one institution
# ===========================================================================


def test_all_hosts_in_one_package_resolve_to_one_institution(
    conn: Connection, package: dict[str, Any]
) -> None:
    """A shared registrable domain proves nothing; the manifest rows are what prove it."""
    resolved = {
        host: reviewed_host(conn, host=host, expect_sha256=package["sha"], directory=package["dir"])
        for host in (package["submitted"], package["second"], package["redirect_only"])
    }
    assert {r.institution_label for r in resolved.values()} == {"reviewed university (ru)"}
    assert {r.institution_id for r in resolved.values()} == {package["reviewed"]}
    assert resolved[package["redirect_only"]].reached_only_by_redirect is True
    assert resolved[package["submitted"]].reached_only_by_redirect is False


def test_the_institution_label_resolves_through_a_unique_column(
    conn: Connection, package: dict[str, Any]
) -> None:
    """`match_key` is UNIQUE, which is what makes label -> id deterministic.

    Asserted rather than trusted: without the constraint, resolution silently becomes
    "whichever row came back first".
    """
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                " WHERE conrelid = 'target_institution'::regclass AND contype = 'u' "
                "   AND conname = 'uq_target_institution_match_key'"
            )
        ).scalar_one()
        == 1
    )
    assert resolve_institution(conn, "reviewed university (ru)") == package["reviewed"]
    with pytest.raises(InstitutionBindingError, match="matches no"):
        resolve_institution(conn, "an institution that was never on the target list")


def test_a_redirect_only_host_needs_no_source_row_of_its_own(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 9, and the reason the manifest has to be the authority.

    Resolved from the database this host has nothing to resolve from.
    """
    assert (
        conn.execute(
            text("SELECT count(*) FROM pilot_collected_source WHERE host = :h"),
            {"h": package["redirect_only"]},
        ).scalar_one()
        == 0
    )
    reviewed = reviewed_host(
        conn,
        host=package["redirect_only"],
        expect_sha256=package["sha"],
        directory=package["dir"],
    )
    assert reviewed.institution_id == package["reviewed"]


# ===========================================================================
# 2. The binding is required, and must be the right one
# ===========================================================================


def test_a_new_decision_without_an_institution_is_refused(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 2, and the test that would have caught the original defect.

    `--institution` defaulted to None and the pilot commands omitted it, so this call is
    exactly what the workflow did.
    """
    with pytest.raises(InstitutionBindingError, match="must name the institution"):
        require_binding(
            conn,
            host=package["submitted"],
            institution_id=None,
            expect_sha256=package["sha"],
            directory=package["dir"],
        )


def test_the_correct_host_institution_and_digest_are_accepted(
    conn: Connection, package: dict[str, Any]
) -> None:
    """The positive case, or the guard is indistinguishable from a wall."""
    for host in (package["submitted"], package["second"], package["redirect_only"]):
        reviewed = require_binding(
            conn,
            host=host,
            institution_id=package["reviewed"],
            expect_sha256=package["sha"],
            directory=package["dir"],
        )
        assert reviewed.host == host
        assert reviewed.institution_id == package["reviewed"]
        assert reviewed.manifest_sha256 == package["sha"]
        assert reviewed.proposed_status_if_accepted == "VERIFIED_OFFICIAL"


def test_a_correct_host_with_the_wrong_institution_is_refused(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 3. This is the case a digest check alone cannot catch.

    The manifest is authentic, the host is in it, and only the institution argument is
    wrong -- so an approval genuinely granted for one institution must not be spendable
    on another. The wrong institution here is a real one that also appears in the same
    manifest, which is the most plausible mistake available.
    """
    for wrong in (package["other"], uuid.uuid4()):
        with pytest.raises(InstitutionBindingError, match="institution mismatch"):
            require_binding(
                conn,
                host=package["submitted"],
                institution_id=wrong,
                expect_sha256=package["sha"],
                directory=package["dir"],
            )

    # And the reverse: the other institution's host may not be claimed for the reviewed one.
    with pytest.raises(InstitutionBindingError, match="institution mismatch"):
        require_binding(
            conn,
            host=package["other_host"],
            institution_id=package["reviewed"],
            expect_sha256=package["sha"],
            directory=package["dir"],
        )


def test_a_host_absent_from_the_manifest_is_refused(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Only hosts the reviewer was actually shown may be decided from a package.

    The bare registrable domain is the interesting case: a plausible-looking near-miss
    of three reviewed hosts, and not itself reviewed.
    """
    for absent in ("reviewed.example", "alumni.reviewed.example", "nowhere.example"):
        with pytest.raises(InstitutionBindingError, match="does not appear in the approved"):
            require_binding(
                conn,
                host=absent,
                institution_id=package["reviewed"],
                expect_sha256=package["sha"],
                directory=package["dir"],
            )


# ===========================================================================
# 3. Existing-row semantics (section 7)
# ===========================================================================


def _actor(conn: Connection) -> uuid.UUID:
    """Someone for `verified_by` to name.

    `ck_official_domain_verified_domain_records_its_basis` requires a VERIFIED_OFFICIAL
    row to carry a method, a timestamp AND a verifier. A verified domain that records no
    basis is not a decision, so the schema refuses it.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    "INSERT INTO app_user (id, email, display_name) "
                    "VALUES (gen_random_uuid(), :email, 'Binding fixture') RETURNING id"
                ),
                {"email": f"binding-{uuid.uuid4().hex[:8]}@example.test"},
            ).scalar_one()
        )
    )


def _university(conn: Connection) -> uuid.UUID:
    """A canonical university, the only other thing a domain row may be bound to.

    Needed to construct the one legal state in which `target_institution_id` is NULL.
    `ck_official_domain_domain_belongs_to_a_target_or_a_university` requires
    `num_nonnulls(target_institution_id, university_id) >= 1`, so a row bound to neither
    cannot exist -- see `test_the_database_already_refuses_a_domain_bound_to_nothing`.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    "INSERT INTO university (id, canonical_id, destination_code, name_en) "
                    "VALUES (gen_random_uuid(), :cid, 'AU', 'Binding Fixture University') "
                    "RETURNING id"
                ),
                {"cid": f"binding-{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
        )
    )


def _existing_row(
    conn: Connection,
    host: str,
    institution: uuid.UUID | None,
    *,
    university: uuid.UUID | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, university_id, host, "
            "  verification_status, is_active) "
            "VALUES (gen_random_uuid(), :inst, :uni, :host, 'CANDIDATE', true)"
        ),
        {"inst": institution, "uni": university, "host": host},
    )


def test_an_existing_row_bound_to_another_institution_is_refused(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 7C. A domain is never reassigned from one institution to another.

    The audit trail would otherwise hold a verification somebody made about institution
    A's property, presented as a decision about institution B's.
    """
    _existing_row(conn, package["submitted"], package["other"])
    with pytest.raises(InstitutionBindingError, match="INSTITUTION_BINDING_CONFLICT"):
        plan_binding(conn, host=package["submitted"], institution_id=package["reviewed"])

    still = conn.execute(
        text("SELECT target_institution_id FROM official_domain WHERE host = :h"),
        {"h": package["submitted"]},
    ).scalar_one()
    assert uuid.UUID(str(still)) == package["other"], "the binding was reassigned"


def test_an_existing_row_bound_to_the_same_institution_is_kept(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 7A. Idempotent: the normal update rules continue."""
    _existing_row(conn, package["submitted"], package["reviewed"])
    assert (
        plan_binding(conn, host=package["submitted"], institution_id=package["reviewed"])
        is BindingAction.KEEP
    )


def test_an_existing_row_with_a_null_binding_is_adopted(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 7B. Allowed only because the caller arrives with a validated institution
    and the audit entry records the change.

    The row is bound to a *university* instead, which is the only legal way
    `target_institution_id` can be NULL: the schema requires at least one of the two.
    """
    _existing_row(conn, package["submitted"], None, university=_university(conn))
    assert (
        plan_binding(conn, host=package["submitted"], institution_id=package["reviewed"])
        is BindingAction.ADOPT
    )


def test_no_existing_row_plans_a_create(conn: Connection, package: dict[str, Any]) -> None:
    assert (
        plan_binding(conn, host=package["submitted"], institution_id=package["reviewed"])
        is BindingAction.CREATE
    )


def test_the_database_already_refuses_a_domain_bound_to_nothing(
    conn: Connection, package: dict[str, Any]
) -> None:
    """The defect was worse than "the row would lack an institution".

    `ck_official_domain_domain_belongs_to_a_target_or_a_university` requires
    `num_nonnulls(target_institution_id, university_id) >= 1`. The old command passed
    NULL for the institution and never set a university, so the INSERT it would have
    issued is refused outright by the schema.

    The consequence is sharper than a completeness gap: `--dry-run` returned "would
    change NONE -> VERIFIED_OFFICIAL" **before** reaching the INSERT, so the preview was
    clean and the real application would have failed. A dry-run that does not agree with
    the apply is worse than no dry-run, because it is trusted.

    This test pins the constraint so the binding requirement cannot be quietly relaxed
    back to optional on the grounds that "the row would still be created".
    """
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(Exception, match="belongs_to_a_target_or_a_university"):
            conn.execute(
                text(
                    "INSERT INTO official_domain (id, target_institution_id, "
                    "  university_id, host, verification_status, verification_method, "
                    "  verification_evidence, verified_at, verified_by, is_active) "
                    "VALUES (gen_random_uuid(), NULL, NULL, :host, 'VERIFIED_OFFICIAL', "
                    "  'MANUAL_STAFF_REVIEW', 'reason', now(), :actor, true)"
                ),
                {"host": package["submitted"], "actor": _actor(conn)},
            )
    finally:
        savepoint.rollback()


# ===========================================================================
# 4. Manifest validation (section 8)
# ===========================================================================


def test_a_missing_or_wrong_digest_is_refused(package: dict[str, Any]) -> None:
    """An optional digest is a digest that gets omitted on the day it matters."""
    for bad in ("", "   ", "0" * 64):
        with pytest.raises(ManifestChangedError, match="REVIEW_MANIFEST_CHANGED"):
            load_domain_manifest(expect_sha256=bad, directory=package["dir"])


def test_an_edited_manifest_no_longer_matches_its_digest(
    conn: Connection, package: dict[str, Any], tmp_path: Path
) -> None:
    """Section 8. The digest is verified, not displayed.

    The forged copy changes only the institution on one row -- the smallest edit that
    would matter, and exactly the one this whole invariant exists to catch.
    """
    forged = tmp_path / "forged"
    forged.mkdir()
    rows = json.loads(json.dumps(package["rows"]))
    rows[0]["institution"] = "some other university (sou)"
    _write_manifest(forged, rows)

    with pytest.raises(ManifestChangedError, match="REVIEW_MANIFEST_CHANGED"):
        load_domain_manifest(expect_sha256=package["sha"], directory=forged)
    with pytest.raises(ManifestChangedError, match="REVIEW_MANIFEST_CHANGED"):
        require_binding(
            conn,
            host=package["submitted"],
            institution_id=package["reviewed"],
            expect_sha256=package["sha"],
            directory=forged,
        )


def test_an_absent_manifest_is_refused_rather_than_regenerated(tmp_path: Path) -> None:
    """Section 11. A manifest rebuilt by the applying code is not one anybody reviewed."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ManifestChangedError, match="does not exist"):
        load_domain_manifest(expect_sha256="a" * 64, directory=empty)


# ===========================================================================
# 5. The CLI shape, and that validation writes nothing
# ===========================================================================


def test_the_domain_command_requires_the_approved_digest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Section 4/8. `--expect-sha256` is required by the parser, not merely honoured.

    Driven through `main(argv)`: what matters is that the command refuses, not how the
    refusal is configured. It never reaches a database -- argparse rejects it first.
    """
    from scripts.source_review import main

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "domain",
                "--host",
                "www.reviewed.example",
                "--decision",
                "VERIFIED_OFFICIAL",
                "--reviewer-email",
                "nobody@example.test",
                "--reason",
                "should never run",
                "--dry-run",
            ]
        )
    assert exit_info.value.code == 2
    assert "expect-sha256" in capsys.readouterr().err


def test_the_domain_command_accepts_the_stable_institution_identifier(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 4. `--institution-id` is preferred and the older alias still parses.

    Both are driven to the authentication refusal, which proves the flag is accepted
    without needing anybody's credential.
    """
    from scripts.source_review import main

    # Supplied through the environment so `read_password` never reaches getpass, which
    # cannot control echo under pytest. Deliberately not a real credential: the point is
    # that the flag parses and the command then refuses.
    monkeypatch.setenv("DATAHUB_REVIEWER_PASSWORD", "not-a-real-password")

    for flag in ("--institution-id", "--institution"):
        code = main(
            [
                "domain",
                "--host",
                "www.reviewed.example",
                "--decision",
                "VERIFIED_OFFICIAL",
                flag,
                str(uuid.uuid4()),
                "--expect-sha256",
                "a" * 64,
                "--reviewer-email",
                "nobody-at-all@example.test",
                "--reason",
                "argument-parsing check; not a decision",
                "--dry-run",
            ]
        )
        assert code == 2, f"{flag} should have reached a refusal, not succeeded"
        assert "refused" in capsys.readouterr().out.lower()


def test_binding_validation_writes_nothing(conn: Connection, package: dict[str, Any]) -> None:
    """Section 12. Validation is a read; only applying writes.

    `audit_log` is included deliberately: a guard that recorded its own refusals would
    be appending to an append-only chain every time somebody mistyped a host.
    """
    tables = ("official_domain", "audit_log", "source_mapping", "field_claim")
    before = {
        table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in tables
    }

    require_binding(
        conn,
        host=package["submitted"],
        institution_id=package["reviewed"],
        expect_sha256=package["sha"],
        directory=package["dir"],
    )
    plan_binding(conn, host=package["submitted"], institution_id=package["reviewed"])
    attempts: tuple[Callable[[], object], ...] = (
        lambda: require_binding(
            conn,
            host=package["submitted"],
            institution_id=None,
            expect_sha256=package["sha"],
            directory=package["dir"],
        ),
        lambda: require_binding(
            conn,
            host=package["submitted"],
            institution_id=package["other"],
            expect_sha256=package["sha"],
            directory=package["dir"],
        ),
        lambda: require_binding(
            conn,
            host="reviewed.example",
            institution_id=package["reviewed"],
            expect_sha256=package["sha"],
            directory=package["dir"],
        ),
    )
    for attempt in attempts:
        with pytest.raises(InstitutionBindingError):
            attempt()

    after = {
        table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in tables
    }
    assert before == after, f"binding validation wrote something: {before} -> {after}"


def test_a_real_creation_would_carry_a_non_null_institution(
    conn: Connection, package: dict[str, Any]
) -> None:
    """Section 6/10.10. What the row would actually look like.

    Exercised through the same INSERT the command issues, inside the rolled-back fixture
    transaction, so the assertion is about the write and not about the intention.
    """
    reviewed = require_binding(
        conn,
        host=package["redirect_only"],
        institution_id=package["reviewed"],
        expect_sha256=package["sha"],
        directory=package["dir"],
    )
    assert (
        plan_binding(conn, host=reviewed.host, institution_id=reviewed.institution_id)
        is BindingAction.CREATE
    )
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, "
            "  verification_status, verification_method, verification_evidence, "
            "  verified_at, verified_by, is_active) "
            "VALUES (gen_random_uuid(), :inst, :host, 'VERIFIED_OFFICIAL', "
            "  'MANUAL_STAFF_REVIEW', :why, now(), :actor, true)"
        ),
        {
            "inst": reviewed.institution_id,
            "host": reviewed.host,
            "why": "binding shape check",
            "actor": _actor(conn),
        },
    )
    stored = conn.execute(
        text(
            "SELECT target_institution_id, verification_status::text AS st "
            "  FROM official_domain WHERE host = :h"
        ),
        {"h": reviewed.host},
    ).one()
    assert stored.target_institution_id is not None, "the created row has no institution"
    assert uuid.UUID(str(stored.target_institution_id)) == reviewed.institution_id
    assert stored.st == "VERIFIED_OFFICIAL"


# ===========================================================================
# 6. Why the real ANU package is NOT asserted here
# ===========================================================================
#
# An earlier draft of this file verified the real ANU rows directly and skipped when
# they were absent. On this machine it always skipped: the 181 institutions live in
# `datahub`, and the suite runs against `datahub_test`, whose target plane is empty.
#
# A test that always skips verifies nothing while looking like coverage, which is the
# same failure this project already corrected for the privilege tests. The ANU facts --
# that all three reviewed hosts resolve to one institution, and which id that is -- are
# verified against the real database as reported evidence for the step, not pretended at
# here. What this file owns is the invariant, exercised on data it builds itself.
