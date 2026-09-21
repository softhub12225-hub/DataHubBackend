"""Step 5C.7L/M: the manifest binding, redirect authority, sessions and the decision path.

WHAT THESE TESTS ARE FOR
========================
Every one of them corresponds to a way a decision could be recorded about something the
reviewer did not look at, or by someone who was not entitled to make it. They are not
coverage for its own sake: the console's entire claim is that what a reviewer sees on
screen is what the write is checked against, and each test below is one way that claim
could be false.

EVERYTHING HERE RUNS IN A ROLLED-BACK TRANSACTION AGAINST `datahub_test`.
The `conn` fixture opens a transaction and always rolls it back, and `app.db.safety`
refuses a test mutation against the real `datahub` outright (Step 5C.7F). Section S
requires the real ANU rows to be untouched by this suite, and both of those together are
what makes that true rather than hoped for.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.identity import sessions
from app.domains.identity.passwords import hash_password
from app.domains.verification import console as console_reads
from app.domains.verification import decisions as decision_service
from app.domains.verification import redirect_authority
from app.domains.verification import responsibility_binding as binding_module
from app.domains.verification.identity import provision_reviewer

PASSWORD = "console-fixture-password-1"
SECRET = "fixture-signing-key-not-the-real-one"

PRIMARY_HOST = "www.fixture-university.test"
REDIRECT_HOST = "study.fixture-university.test"
FOREIGN_HOST = "portal.some-vendor.test"
URL = f"https://{PRIMARY_HOST}/apply/english"
EFFECTIVE_URL = f"https://{REDIRECT_HOST}/apply/english"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ===========================================================================
# fixtures
# ===========================================================================


@pytest.fixture
def reviewer(conn: Connection) -> dict[str, Any]:
    """A `[TEST ONLY]` reviewer holding `source:verify`, with a password set."""
    unique = uuid.uuid4().hex[:8]
    email = f"console-{unique}@example.test"
    created = provision_reviewer(
        conn, email=email, display_name=f"Console {unique}", test_only=True
    )
    conn.execute(
        text("UPDATE app_user SET password_hash = :h WHERE id = :i"),
        {"h": hash_password(PASSWORD), "i": created.id},
    )
    return {"id": created.id, "email": email}


@pytest.fixture
def world(conn: Connection, reviewer: dict[str, Any]) -> dict[str, Any]:
    """One institution, one verified host, one registered VERIFIED mapping.

    Deliberately built at the SQL level rather than through the pilot workflow: what is
    under test is the binding and the decision service, and routing through five other
    modules to arrive here would make a failure in any of them look like a failure in
    these.
    """
    ids = {
        name: uuid.uuid4()
        for name in (
            "list",
            "target",
            "other_target",
            "submission",
            "source",
            "domain",
            "redirect_domain",
            "mapping",
            "pilot",
            "fetch_run",
            "snapshot",
        )
    }
    match_key = f"fixture university {ids['target'].hex[:8]}"
    other_key = f"other university {ids['other_target'].hex[:8]}"

    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, "
            "file_sha256, file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:i, 'Console list', :v, 'c.xlsx', :s, 1024, 's', 1)"
        ),
        {"i": ids["list"], "v": ids["list"].hex[:8], "s": sha(str(ids["list"]))},
    )
    for key, label in ((ids["target"], match_key), (ids["other_target"], other_key)):
        conn.execute(
            text(
                "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
                "latest_list_id, destination_code) VALUES (:i, :k, :l, :l, 'GB')"
            ),
            {"i": key, "k": label, "l": ids["list"]},
        )
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "file_byte_size, template_version, selected_university_count) "
            "VALUES (:i, :h, 'c.xlsx', 1024, 'v1', 1)"
        ),
        {"i": ids["submission"], "h": sha(str(ids["submission"]))},
    )
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:i, :u, :h, 'admissions_page', 'MONTHLY', 'STATIC')"
        ),
        {"i": ids["source"], "u": URL, "h": sha(URL)},
    )
    for domain_id, host in ((ids["domain"], PRIMARY_HOST), (ids["redirect_domain"], REDIRECT_HOST)):
        conn.execute(
            text(
                "INSERT INTO official_domain (id, target_institution_id, host, "
                "verification_status, verification_method, verification_evidence, "
                "verified_at, verified_by) VALUES (:i, :t, :host, 'VERIFIED_OFFICIAL', "
                "'MANUAL_STAFF_REVIEW', 'fixture register', now(), :by)"
            ),
            {
                "i": domain_id,
                "t": ids["target"],
                "host": host,
                "by": reviewer["id"],
            },
        )
    conn.execute(
        text(
            "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
            "target_institution_id, sheet_row_no, source_type, official_url, "
            "normalized_url, url_sha256, host, acquisition_source_id, verification_state, "
            "verified_at, verified_by, verification_reason) "
            "VALUES (:i, :sub, 'S0001', :t, 2, 'LANGUAGE_REQUIREMENTS', :u, :u, :h, "
            ":host, :src, 'VERIFIED', now(), :by, 'fixture')"
        ),
        {
            "i": ids["pilot"],
            "sub": ids["submission"],
            "t": ids["target"],
            "u": URL,
            "h": sha(URL),
            "host": PRIMARY_HOST,
            "src": ids["source"],
            "by": reviewer["id"],
        },
    )
    conn.execute(
        text(
            "INSERT INTO source_mapping (id, target_institution_id, source_category, url, "
            "normalized_url, url_sha256, host, official_domain_id, verification_status, "
            "collection_priority, discovered_by) "
            "VALUES (:i, :t, 'LANGUAGE_REQUIREMENTS', :u, :u, :h, :host, :d, 'CANDIDATE', "
            "3, :by)"
        ),
        {
            "i": ids["mapping"],
            "t": ids["target"],
            "u": URL,
            "h": sha(URL),
            "host": PRIMARY_HOST,
            "d": ids["domain"],
            "by": reviewer["id"],
        },
    )
    conn.execute(
        text("UPDATE pilot_collected_source SET promoted_source_mapping_id = :m WHERE id = :i"),
        {"m": ids["mapping"], "i": ids["pilot"]},
    )
    return {"ids": ids, "match_key": match_key, "other_key": other_key}


def write_manifest(directory: Path, rows: list[dict[str, Any]]) -> str:
    """Write a responsibility manifest and return its digest.

    Uses the module's own `manifest_digest` rather than recomputing the serialisation --
    a second implementation is how a check starts passing against a hash nobody else
    would produce, which happened for real while verifying the domain manifest.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{binding_module.RESPONSIBILITY_MANIFEST}.json"
    path.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    return binding_module.manifest_digest(rows)


@pytest.fixture
def manifest(tmp_path: Path, world: dict[str, Any]) -> dict[str, Any]:
    """A frozen manifest that correctly describes the fixture mapping."""
    rows = [
        {
            "source_ref": "S0001",
            "institution": world["match_key"],
            "claimed_responsibility": "LANGUAGE_REQUIREMENTS",
            "url": URL,
            "host": PRIMARY_HOST,
            "access_class": "BODY_EVIDENCE",
            "page_evidence_available": True,
            "is_duplicate_row": False,
            "duplicate_of": None,
            "declared_degree_scope": None,
        }
    ]
    return {"dir": tmp_path, "sha256": write_manifest(tmp_path, rows), "rows": rows}


def reviewer_actor(reviewer: dict[str, Any], *, is_test: bool = False) -> decision_service.Reviewer:
    return decision_service.Reviewer(
        id=reviewer["id"],
        email=reviewer["email"],
        display_name="Console Fixture",
        is_test=is_test,
    )


def snapshot_with(conn: Connection, world: dict[str, Any], *, effective_url: str | None) -> None:
    """Give the fixture source one stored snapshot, optionally a redirecting one."""
    run_id = uuid.uuid4()
    conn.execute(
        text(
            # FINALIZED, not SUCCEEDED: `ck_fetch_attempt_state_known` allows only
            # QUEUED / RUNNING / FINALIZED / ABANDONED, and a terminal state must carry
            # `finalized_at` (`ck_fetch_attempt_terminal_state_has_a_timestamp`).
            "INSERT INTO fetch_attempt (id, source_id, attempt_no, cycle_key, state, "
            "scheduled_for, finalized_at) VALUES (:i, :s, 1, :c, 'FINALIZED', now(), now())"
        ),
        {"i": world["ids"]["fetch_run"], "s": world["ids"]["source"], "c": uuid.uuid4().hex[:16]},
    )
    conn.execute(
        text(
            # OK, not SUCCESS: `fetch_status` is an enum and
            # `ck_fetch_run_a_failure_names_its_error` requires anything else to name an
            # error class.
            "INSERT INTO fetch_run (id, source_id, attempt_id, attempt_no, status, "
            "fetcher, started_at, finished_at, effective_url) "
            "VALUES (:i, :s, :a, 1, 'OK', 'STATIC', now(), now(), :e)"
        ),
        {
            "i": run_id,
            "s": world["ids"]["source"],
            "a": world["ids"]["fetch_run"],
            "e": effective_url,
        },
    )
    # `fk_snapshot_content_hash_content_blob`: a snapshot names stored bytes, so the blob
    # has to exist first. The evidence model does not allow a snapshot of nothing.
    content_hash = sha(f"body-{uuid.uuid4()}")
    conn.execute(
        text(
            "INSERT INTO content_blob (content_hash, storage_key, content_type, "
            "byte_size, first_observed_at) "
            "VALUES (:c, :k, 'text/html', 1024, now())"
        ),
        {"c": content_hash, "k": f"evidence/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"},
    )
    conn.execute(
        text(
            "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, observed_at, "
            "requested_url, effective_url, http_status, fetcher, content_type) "
            "VALUES (:i, :r, :s, :c, now(), :u, :e, 200, 'STATIC', 'text/html')"
        ),
        {
            "i": world["ids"]["snapshot"],
            "r": run_id,
            "s": world["ids"]["source"],
            "c": content_hash,
            "u": URL,
            "e": effective_url,
        },
    )


# ===========================================================================
# 1. Sessions (section C)
# ===========================================================================


def test_login_issues_a_session_and_stores_only_its_hash(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    """The database must never hold anything that could be replayed as a cookie."""
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)

    stored = conn.execute(
        text("SELECT token_hash FROM user_session WHERE id = :i"), {"i": issued.session_id}
    ).scalar_one()
    assert stored == sessions.hash_token(issued.token)
    assert issued.token not in stored
    assert len(issued.token) > 20
    assert issued.csrf_token != issued.token


def test_a_wrong_password_and_an_unknown_account_are_indistinguishable(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    """Anything else is an account-enumeration oracle."""
    with pytest.raises(sessions.SessionRefusedError) as wrong:
        sessions.login(conn, email=reviewer["email"], password="not-the-password", ttl_minutes=60)
    with pytest.raises(sessions.SessionRefusedError) as unknown:
        sessions.login(conn, email="nobody@example.test", password=PASSWORD, ttl_minutes=60)
    assert str(wrong.value) == str(unknown.value)


def test_resolve_returns_the_identity_the_server_looked_up(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)
    resolved = sessions.resolve(conn, token=issued.token)
    assert resolved.user_id == reviewer["id"]
    assert resolved.may_verify is True
    assert resolved.is_test is True


def test_logout_revokes_the_session_immediately(conn: Connection, reviewer: dict[str, Any]) -> None:
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)
    assert sessions.logout(conn, token=issued.token) is True
    with pytest.raises(sessions.SessionExpiredError):
        sessions.resolve(conn, token=issued.token)
    # Idempotent: logging out twice is not an error.
    assert sessions.logout(conn, token=issued.token) is False


def test_a_deactivated_reviewer_cannot_keep_using_a_live_session(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    """The reason the session is a row and not a self-contained signed blob."""
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)
    sessions.resolve(conn, token=issued.token)

    conn.execute(
        # `ck_app_user_deactivation_has_a_timestamp` requires the timestamp alongside
        # the flag, so a row can never claim to be deactivated without saying when.
        text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
        {"i": reviewer["id"]},
    )
    with pytest.raises(sessions.SessionExpiredError, match="deactivated"):
        sessions.resolve(conn, token=issued.token)


def test_an_expired_session_is_refused(conn: Connection, reviewer: dict[str, Any]) -> None:
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)
    later = datetime.now(UTC) + timedelta(minutes=61)
    with pytest.raises(sessions.SessionExpiredError, match="expired"):
        sessions.resolve(conn, token=issued.token, now=later)


def test_a_revoked_role_stops_working_without_waiting_for_expiry(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    """Permissions are recomputed on every resolve, never remembered from login."""
    issued = sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)
    assert sessions.resolve(conn, token=issued.token).may_verify is True

    conn.execute(text("DELETE FROM user_role WHERE user_id = :i"), {"i": reviewer["id"]})
    assert sessions.resolve(conn, token=issued.token).may_verify is False
    with pytest.raises(PermissionError):
        sessions.resolve(conn, token=issued.token).require("source:verify")


def test_an_account_without_verify_permission_cannot_open_a_session(
    conn: Connection, reviewer: dict[str, Any]
) -> None:
    conn.execute(text("DELETE FROM user_role WHERE user_id = :i"), {"i": reviewer["id"]})
    with pytest.raises(sessions.SessionRefusedError, match="source:verify"):
        sessions.login(conn, email=reviewer["email"], password=PASSWORD, ttl_minutes=60)


# ===========================================================================
# 2. Manifest binding (section A.2, A.3)
# ===========================================================================


def test_a_correct_manifest_binds_the_mapping(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any]
) -> None:
    bound = binding_module.require_binding(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    assert bound.source_ref == "S0001"
    assert bound.claimed_responsibility == "LANGUAGE_REQUIREMENTS"
    assert bound.institution_id == world["ids"]["target"]
    assert bound.url == URL


def test_a_missing_digest_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """The gap Step 5C.7L closed: the digest used to be optional and skipped when absent."""
    with pytest.raises(binding_module.ManifestChangedError, match="no approved manifest digest"):
        binding_module.require_binding(
            conn,
            mapping_id=world["ids"]["mapping"],
            expect_sha256="",
            directory=manifest["dir"],
        )


def test_a_wrong_digest_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any]
) -> None:
    with pytest.raises(binding_module.ManifestChangedError, match="REVIEW_MANIFEST_CHANGED"):
        binding_module.require_binding(
            conn,
            mapping_id=world["ids"]["mapping"],
            expect_sha256="0" * 64,
            directory=manifest["dir"],
        )


def test_a_correct_digest_does_not_authorise_a_mapping_the_package_omits(
    conn: Connection, world: dict[str, Any], tmp_path: Path
) -> None:
    """The core of the fix. A genuinely approved package, and a mapping that is not in it."""
    digest = write_manifest(
        tmp_path,
        [
            {
                "source_ref": "S9999",
                "institution": world["match_key"],
                "claimed_responsibility": "LANGUAGE_REQUIREMENTS",
                "url": URL,
                "host": PRIMARY_HOST,
            }
        ],
    )
    with pytest.raises(binding_module.ResponsibilityBindingError, match="does not appear"):
        binding_module.require_binding(
            conn,
            mapping_id=world["ids"]["mapping"],
            expect_sha256=digest,
            directory=tmp_path,
        )


def test_a_cross_institution_manifest_row_is_refused(
    conn: Connection, world: dict[str, Any], tmp_path: Path
) -> None:
    """One institution's approval must not be spendable on another's source."""
    digest = write_manifest(
        tmp_path,
        [
            {
                "source_ref": "S0001",
                "institution": world["other_key"],
                "claimed_responsibility": "LANGUAGE_REQUIREMENTS",
                "url": URL,
                "host": PRIMARY_HOST,
            }
        ],
    )
    with pytest.raises(binding_module.ResponsibilityBindingError, match="institution mismatch"):
        binding_module.require_binding(
            conn, mapping_id=world["ids"]["mapping"], expect_sha256=digest, directory=tmp_path
        )


def test_a_mismatched_responsibility_is_refused(
    conn: Connection, world: dict[str, Any], tmp_path: Path
) -> None:
    digest = write_manifest(
        tmp_path,
        [
            {
                "source_ref": "S0001",
                "institution": world["match_key"],
                "claimed_responsibility": "TUITION_FEES",
                "url": URL,
                "host": PRIMARY_HOST,
            }
        ],
    )
    with pytest.raises(binding_module.ResponsibilityBindingError, match="responsibility mismatch"):
        binding_module.require_binding(
            conn, mapping_id=world["ids"]["mapping"], expect_sha256=digest, directory=tmp_path
        )


def test_a_mismatched_url_is_refused(
    conn: Connection, world: dict[str, Any], tmp_path: Path
) -> None:
    digest = write_manifest(
        tmp_path,
        [
            {
                "source_ref": "S0001",
                "institution": world["match_key"],
                "claimed_responsibility": "LANGUAGE_REQUIREMENTS",
                "url": "https://www.fixture-university.test/somewhere-else",
                "host": PRIMARY_HOST,
            }
        ],
    )
    with pytest.raises(binding_module.ResponsibilityBindingError, match="URL mismatch"):
        binding_module.require_binding(
            conn, mapping_id=world["ids"]["mapping"], expect_sha256=digest, directory=tmp_path
        )


# ===========================================================================
# 3. Redirect authority (section A.6)
# ===========================================================================


def test_no_snapshot_reports_no_evidence_rather_than_ok(
    conn: Connection, world: dict[str, Any]
) -> None:
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.verdict is redirect_authority.AuthorityVerdict.NO_EVIDENCE
    assert result.ok is False


def test_a_null_effective_url_is_a_non_redirect_not_missing_evidence(
    conn: Connection, world: dict[str, Any]
) -> None:
    """Regression: the first draft called this NO_EVIDENCE and blocked two real sources.

    S0232 and S0236 each hold a 200 snapshot with a NULL `effective_url`, because neither
    page redirected. Treating that as missing evidence would have stranded them.
    """
    snapshot_with(conn, world, effective_url=None)
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.verdict is redirect_authority.AuthorityVerdict.OK
    assert result.redirected is False


def test_a_redirect_to_another_verified_host_of_the_same_institution_is_ok(
    conn: Connection, world: dict[str, Any]
) -> None:
    """The live ANU shape: www.anu.edu.au -> study.anu.edu.au, both verified."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.redirected is True
    assert result.effective.host == REDIRECT_HOST
    assert result.verdict is redirect_authority.AuthorityVerdict.OK


def test_a_redirect_to_an_unverified_host_is_refused(
    conn: Connection, world: dict[str, Any]
) -> None:
    """The substitution this module exists to stop."""
    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.verdict is redirect_authority.AuthorityVerdict.EFFECTIVE_UNTRUSTED
    assert FOREIGN_HOST in (result.blocker or "")


def test_a_redirect_to_another_institutions_verified_host_is_refused(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Authority does not cross institutions, even between two verified hosts."""
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, "
            "verification_status, verification_method, verification_evidence, "
            "verified_at, verified_by) VALUES (:i, :t, :host, 'VERIFIED_OFFICIAL', "
            "'MANUAL_STAFF_REVIEW', 'fixture', now(), :by)"
        ),
        {
            "i": uuid.uuid4(),
            "t": world["ids"]["other_target"],
            "host": FOREIGN_HOST,
            "by": reviewer["id"],
        },
    )
    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.verdict is redirect_authority.AuthorityVerdict.INSTITUTION_MISMATCH


def test_a_deactivated_redirect_target_is_refused(conn: Connection, world: dict[str, Any]) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    conn.execute(
        text("UPDATE official_domain SET is_active = false WHERE host = :h"),
        {"h": REDIRECT_HOST},
    )
    result = redirect_authority.for_mapping(conn, world["ids"]["mapping"])
    assert result.verdict is redirect_authority.AuthorityVerdict.EFFECTIVE_UNTRUSTED


# ===========================================================================
# 4. Preview (sections J, K)
# ===========================================================================


def _preview(
    conn: Connection,
    world: dict[str, Any],
    manifest: dict[str, Any],
    who: dict[str, Any],
    *,
    decision: str = "VERIFIED",
    reason: str = "the page states the requirement in its own words",
    **overrides: Any,
) -> decision_service.DecisionPreview:
    """`who` rather than `reviewer` so a test can override the actor via **overrides."""
    kwargs: dict[str, Any] = {
        "mapping_id": world["ids"]["mapping"],
        "decision": decision,
        "reason": reason,
        "reviewer": reviewer_actor(who),
        "expect_sha256": manifest["sha256"],
        "secret": SECRET,
        "directory": manifest["dir"],
    }
    kwargs.update(overrides)
    return decision_service.preview(conn, **kwargs)


def test_a_valid_preview_writes_nothing_and_issues_a_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    before = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    result = _preview(conn, world, manifest, reviewer)

    assert result.valid is True
    assert result.token
    assert result.before.verification_status == "CANDIDATE"
    assert result.after is not None
    assert result.after.verification_status == "VERIFIED_OFFICIAL"
    assert result.would_append == "RESPONSIBILITY_VERIFIED"
    assert result.creates_field_claim is False
    assert result.modifies_canonical is False
    assert result.promotes is False
    assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == before
    assert (
        conn.execute(
            text("SELECT verification_status::text FROM source_mapping WHERE id = :i"),
            {"i": world["ids"]["mapping"]},
        ).scalar_one()
        == "CANDIDATE"
    )


def test_a_preview_reports_the_manifest_mismatch_rather_than_raising(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """A blocked preview is information for the reviewer, not an exception for the client."""
    result = _preview(conn, world, manifest, reviewer, expect_sha256="0" * 64)
    assert result.valid is False
    assert result.token is None
    codes = {blocker.code for blocker in result.blockers}
    assert decision_service.BlockerCode.MANIFEST_MISMATCH in codes


def test_a_preview_requires_a_reason(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(conn, world, manifest, reviewer, reason="   ")
    assert result.valid is False
    assert decision_service.BlockerCode.REASON_REQUIRED in {b.code for b in result.blockers}


def test_a_fixture_identity_may_not_decide(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(
        conn,
        world,
        manifest,
        reviewer,
        reviewer=reviewer_actor(reviewer, is_test=True),
    )
    assert result.valid is False
    assert decision_service.BlockerCode.FIXTURE_IDENTITY in {b.code for b in result.blockers}


def test_an_untrusted_redirect_blocks_the_preview(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    result = _preview(conn, world, manifest, reviewer)
    assert result.valid is False
    assert decision_service.BlockerCode.EFFECTIVE_HOST_UNTRUSTED in {
        b.code for b in result.blockers
    }


def test_missing_evidence_does_not_block_a_responsibility_decision(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """A reviewer must be able to REJECT a dead page.

    Requiring stored evidence to decide would trap exactly the rows that most need a
    decision. Promotion is where missing evidence matters, because that is the act that
    relies on the content.
    """
    result = _preview(conn, world, manifest, reviewer, decision="REJECTED", reason="page is dead")
    assert result.valid is True


# ===========================================================================
# 5. Apply, and the stale-preview guard (sections L, M, N)
# ===========================================================================


def _apply(
    conn: Connection,
    world: dict[str, Any],
    manifest: dict[str, Any],
    who: dict[str, Any],
    token: str,
    *,
    decision: str = "VERIFIED",
    reason: str = "the page states the requirement in its own words",
    **overrides: Any,
) -> decision_service.DecisionResult:
    """`who` rather than `reviewer`; see `_preview`."""
    kwargs: dict[str, Any] = {
        "token": token,
        "mapping_id": world["ids"]["mapping"],
        "decision": decision,
        "reason": reason,
        "reviewer": reviewer_actor(who),
        "expect_sha256": manifest["sha256"],
        "secret": SECRET,
        "ttl_seconds": 900,
        "directory": manifest["dir"],
    }
    kwargs.update(overrides)
    return decision_service.apply_decision(conn, **kwargs)


def test_apply_records_the_decision_and_reads_it_back(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, manifest, reviewer)
    result = _apply(conn, world, manifest, reviewer, preview.token or "")

    assert result.before.verification_status == "CANDIDATE"
    assert result.after.verification_status == "VERIFIED_OFFICIAL"
    assert result.after.verified_by == reviewer["id"]
    assert result.action == "RESPONSIBILITY_VERIFIED"
    assert result.audit_chain_ok is True
    assert result.field_claim_count == 0
    assert result.canonical_unchanged is True
    # Promotion is a separate act and this is not it.
    assert result.after.promoted_source_id is None

    audit = conn.execute(
        text(
            "SELECT action, actor_id FROM audit_log WHERE object_id = :i "
            " ORDER BY seq DESC LIMIT 1"
        ),
        {"i": world["ids"]["mapping"]},
    ).one()
    assert audit.action == "RESPONSIBILITY_VERIFIED"
    assert audit.actor_id == reviewer["id"]


def test_apply_is_impossible_without_a_preview(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    for bogus in ("", "not-a-token", "a.b"):
        with pytest.raises(decision_service.PreviewForgedError):
            _apply(conn, world, manifest, reviewer, bogus)


def test_a_token_signed_with_another_key_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The client cannot mint its own approval."""
    preview = _preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewForgedError):
        _apply(conn, world, manifest, reviewer, preview.token or "", secret="a-different-key")


def test_a_tampered_token_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, manifest, reviewer)
    token = preview.token or ""
    with pytest.raises(decision_service.PreviewForgedError):
        _apply(conn, world, manifest, reviewer, token[:-6] + "abcdef")


def test_changing_the_reason_after_preview_is_stale(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The reason is part of the decision, so altering it invalidates the preview."""
    preview = _preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewStaleError):
        _apply(conn, world, manifest, reviewer, preview.token or "", reason="something else")


def test_changing_the_decision_after_preview_is_stale(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewStaleError):
        _apply(conn, world, manifest, reviewer, preview.token or "", decision="REJECTED")


def test_a_state_change_after_preview_is_stale(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Somebody else decided the same mapping in between. Section M's central case."""
    preview = _preview(conn, world, manifest, reviewer)
    conn.execute(
        text(
            # `ck_source_mapping_deactivation_has_a_reason`: a row switched off must say
            # why, so a deactivated mapping can never be silent about it.
            "UPDATE source_mapping SET verification_status = 'REJECTED', "
            "is_active = false, deactivated_reason = 'someone else decided first', "
            "rejected_reason = 'someone else decided first', "
            "verified_by = :by, verified_at = now() WHERE id = :i"
        ),
        {"by": reviewer["id"], "i": world["ids"]["mapping"]},
    )
    with pytest.raises(decision_service.PreviewStaleError):
        _apply(conn, world, manifest, reviewer, preview.token or "")


def test_a_trust_change_after_preview_is_stale(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The host was verified when the reviewer looked, and is not now."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    preview = _preview(conn, world, manifest, reviewer)
    assert preview.valid is True

    conn.execute(
        text("UPDATE official_domain SET is_active = false WHERE host = :h"),
        {"h": REDIRECT_HOST},
    )
    with pytest.raises(decision_service.DecisionRefusedError):
        _apply(conn, world, manifest, reviewer, preview.token or "")


def test_an_expired_preview_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Time passing is what expires a preview, so the test advances the clock.

    An earlier version passed `ttl_seconds=0` instead. That failed intermittently: the
    preview and the apply can read the same wall-clock value on Windows, making the
    elapsed time exactly 0.0, and `0.0 > 0` is false. The bug was in the test -- a zero
    TTL is not a scenario -- but it is worth stating, because a flaky staleness check is
    the kind of thing that gets "fixed" by loosening the guard.
    """
    preview = _preview(conn, world, manifest, reviewer)
    well_past = datetime.now(UTC) + timedelta(seconds=1000)
    with pytest.raises(decision_service.PreviewStaleError, match="older than"):
        _apply(
            conn,
            world,
            manifest,
            reviewer,
            preview.token or "",
            ttl_seconds=900,
            now=well_past,
        )


def test_a_preview_for_one_mapping_cannot_apply_to_another(
    conn: Connection,
    world: dict[str, Any],
    manifest: dict[str, Any],
    reviewer: dict[str, Any],
) -> None:
    preview = _preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewStaleError, match="different mapping"):
        _apply(conn, world, manifest, reviewer, preview.token or "", mapping_id=uuid.uuid4())


def test_applying_a_decision_creates_no_claim_and_no_canonical_row(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Section S, as an assertion rather than a promise."""
    preview = _preview(conn, world, manifest, reviewer)
    _apply(conn, world, manifest, reviewer, preview.token or "")

    for table in ("field_claim", "field_provenance", "change_proposal", "change_event"):
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    assert (
        conn.execute(
            text("SELECT count(*) FROM source WHERE publication_eligibility <> 'NOT_ELIGIBLE'")
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL")
        ).scalar_one()
        == 0
    )


# ===========================================================================
# 6. Promotion preview (section O)
# ===========================================================================


def test_promotion_preview_reports_what_it_would_do_without_doing_it(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    preview = _preview(conn, world, manifest, reviewer)
    _apply(conn, world, manifest, reviewer, preview.token or "")

    promotion = decision_service.preview_promotion(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    assert promotion.valid is True
    assert promotion.manifest_bound is True
    assert promotion.field_bindings, "a promotable responsibility authorises fields"
    assert promotion.source_eligibility_before == "NOT_ELIGIBLE"
    assert promotion.source_eligibility_after == "OFFICIAL_VERIFIED"
    assert promotion.promoted_source_id_before is None
    assert promotion.field_claim_remains_zero is True

    # And nothing happened.
    assert (
        conn.execute(
            text("SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL")
        ).scalar_one()
        == 0
    )


def test_promotion_preview_blocks_an_untrusted_redirect(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_by = :by, verified_at = now() WHERE id = :i"
        ),
        {"by": reviewer["id"], "i": world["ids"]["mapping"]},
    )
    promotion = decision_service.preview_promotion(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    assert promotion.valid is False
    assert decision_service.BlockerCode.EFFECTIVE_HOST_UNTRUSTED in {
        b.code for b in promotion.blockers
    }


def test_promotion_itself_refuses_an_untrusted_redirect(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The guard is in `promote`, not only in its preview."""
    from app.domains.verification.promotion import Actor, NotYetTrustedError, promote

    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_by = :by, verified_at = now() WHERE id = :i"
        ),
        {"by": reviewer["id"], "i": world["ids"]["mapping"]},
    )
    bound = binding_module.require_binding(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    with pytest.raises(NotYetTrustedError, match="redirect authority"):
        promote(
            conn,
            mapping_id=world["ids"]["mapping"],
            source_id=world["ids"]["source"],
            actor=Actor(id=reviewer["id"], display="Console Fixture"),
            reason="relying on the reviewed page",
            binding=bound,
        )


def test_promotion_refuses_a_binding_for_a_different_mapping(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    from dataclasses import replace

    from app.domains.verification.promotion import Actor, PromotionRefusedError, promote

    bound = binding_module.require_binding(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    elsewhere = replace(bound, mapping_id=uuid.uuid4())
    with pytest.raises(PromotionRefusedError, match="not transferable"):
        promote(
            conn,
            mapping_id=world["ids"]["mapping"],
            source_id=world["ids"]["source"],
            actor=Actor(id=reviewer["id"], display="Console Fixture"),
            reason="relying on the reviewed page",
            binding=elsewhere,
        )


# ===========================================================================
# 7. Console read model (sections D, E, F, H, P)
# ===========================================================================


def test_the_dashboard_counts_are_live(conn: Connection, world: dict[str, Any]) -> None:
    data = console_reads.dashboard(conn)
    assert data.trust.pilot_institutions >= 1
    assert data.safety.field_claim == 0
    assert data.safety.all_zero is True
    assert data.audit_chain_ok is True


def test_the_institution_list_reports_progress(conn: Connection, world: dict[str, Any]) -> None:
    rows = console_reads.institutions(conn)
    mine = next(row for row in rows if row.institution_id == world["ids"]["target"])
    assert mine.verified_domains == 2
    assert mine.pilot_rows == 1
    assert mine.registered_mappings == 1
    assert mine.responsibility_decided == 0
    assert mine.promoted == 0


def test_responsibility_cards_report_their_binding(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any]
) -> None:
    cards = console_reads.responsibility_cards(
        conn,
        world["ids"]["target"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    assert len(cards) == 1
    assert cards[0].manifest_bound is True
    assert cards[0].verification_status == "CANDIDATE"
    assert cards[0].publication_eligibility == "NOT_ELIGIBLE"


def test_a_card_says_so_when_the_binding_fails(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any]
) -> None:
    cards = console_reads.responsibility_cards(
        conn, world["ids"]["target"], expect_sha256="0" * 64, directory=manifest["dir"]
    )
    assert cards[0].manifest_bound is False
    assert cards[0].manifest_blocker


def test_registration_now_appends_its_own_action_name(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Section A.7. Registration is not a decision and no longer shares its name."""
    from app.domains.pilot import verification as pilot

    second = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
            "target_institution_id, sheet_row_no, source_type, official_url, "
            "normalized_url, url_sha256, host, acquisition_source_id, verification_state, "
            "verified_at, verified_by, verification_reason, duplicate_of_source_ref) "
            "VALUES (:i, :sub, 'S0002', :t, 3, 'TUITION_FEES', :u, :u, :h, :host, :src, "
            "'VERIFIED', now(), :by, 'fixture', 'S0001')"
        ),
        {
            "i": second,
            "sub": world["ids"]["submission"],
            "t": world["ids"]["target"],
            "u": URL,
            "h": sha(URL),
            "host": PRIMARY_HOST,
            "src": world["ids"]["source"],
            "by": reviewer["id"],
        },
    )
    pilot.register_verified_candidate(
        conn,
        candidate_id=second,
        source_category="TUITION_FEES",
        actor=pilot.Actor(user_id=reviewer["id"]),
        reason="register the reviewed source",
    )
    action = conn.execute(
        text("SELECT action FROM audit_log WHERE object_id = :i ORDER BY seq DESC LIMIT 1"),
        {"i": second},
    ).scalar_one()
    assert action == "PILOT_SOURCE_REGISTERED"


def test_the_audit_tab_flags_the_historical_registrations(conn: Connection) -> None:
    """A registration filed under the decision action is labelled, never rewritten."""
    rows = console_reads.audit_trail(conn, None, limit=5)
    # Shape assertion: the detector keys off the after-state, exactly as documented.
    for row in rows:
        if row.action == "PILOT_SOURCE_VERIFY" and isinstance(row.after_state, dict):
            expected = "promoted_source_mapping_id" in row.after_state
            assert row.is_historical_registration is expected


# ===========================================================================
# 8. Promotion: preview -> confirm -> apply (the promotion UI fix)
# ===========================================================================


def _promotion_preview(
    conn: Connection,
    world: dict[str, Any],
    manifest: dict[str, Any],
    who: dict[str, Any],
    **overrides: Any,
) -> decision_service.PromotionPreview:
    kwargs: dict[str, Any] = {
        "mapping_id": world["ids"]["mapping"],
        "expect_sha256": manifest["sha256"],
        "reviewer": reviewer_actor(who),
        "secret": SECRET,
        "directory": manifest["dir"],
    }
    kwargs.update(overrides)
    return decision_service.preview_promotion(conn, **kwargs)


def _promote(
    conn: Connection,
    world: dict[str, Any],
    manifest: dict[str, Any],
    who: dict[str, Any],
    token: str,
    **overrides: Any,
) -> decision_service.PromotionResultView:
    kwargs: dict[str, Any] = {
        "token": token,
        "mapping_id": world["ids"]["mapping"],
        "reviewer": reviewer_actor(who),
        "reason": "relying on the reviewed page",
        "expect_sha256": manifest["sha256"],
        "secret": SECRET,
        "ttl_seconds": 900,
        "directory": manifest["dir"],
    }
    kwargs.update(overrides)
    return decision_service.apply_promotion(conn, **kwargs)


def _make_verified(conn: Connection, world: dict[str, Any], who: dict[str, Any]) -> None:
    """Put the mapping in the one state promotion is allowed from."""
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_by = :by, verified_at = now() WHERE id = :i"
        ),
        {"by": who["id"], "i": world["ids"]["mapping"]},
    )


def test_a_valid_promotion_preview_issues_a_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Requirement 1: the confirm control needs something server-generated to confirm."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)

    preview = _promotion_preview(conn, world, manifest, reviewer)
    assert preview.valid is True
    assert preview.token, "a valid promotion preview must issue a token"
    assert preview.issued_at is not None


def test_a_blocked_promotion_preview_issues_no_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Requirements 3 and 6, enforced by the server rather than by hiding a button.

    The console renders the confirm control only when `preview_token` is present, so a
    blocked preview cannot produce one to click.
    """
    snapshot_with(conn, world, effective_url=f"https://{FOREIGN_HOST}/apply")
    _make_verified(conn, world, reviewer)

    preview = _promotion_preview(conn, world, manifest, reviewer)
    assert preview.valid is False
    assert preview.token is None


def test_an_unverified_responsibility_yields_no_promotion_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The mapping is still CANDIDATE, so there is nothing to promote."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    assert preview.valid is False
    assert preview.token is None


def test_a_preview_without_a_reviewer_issues_no_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """A read-only preview cannot become an authorisation by accident."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = decision_service.preview_promotion(
        conn,
        mapping_id=world["ids"]["mapping"],
        expect_sha256=manifest["sha256"],
        directory=manifest["dir"],
    )
    assert preview.valid is True
    assert preview.token is None


def test_promotion_applies_and_reads_the_result_back(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Requirements 4 and 5, against a fixture -- never the real ANU rows."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)

    result = _promote(conn, world, manifest, reviewer, preview.token or "")

    assert result.promoted is True
    assert result.promoted_source_id == world["ids"]["source"]
    assert result.source_eligibility == "OFFICIAL_VERIFIED"
    assert result.audit_action == "SOURCE_MAPPING_PROMOTED"
    assert result.audit_chain_ok is True
    assert result.bindings_written > 0
    # The safety counts the result panel prints.
    assert result.field_claim == 0
    assert result.change_proposal == 0
    assert result.change_event == 0
    assert result.canonical_unchanged is True

    # And the database agrees, independently of what the result object said.
    assert (
        conn.execute(
            text("SELECT promoted_source_id FROM source_mapping WHERE id = :i"),
            {"i": world["ids"]["mapping"]},
        ).scalar_one()
        == world["ids"]["source"]
    )
    action = conn.execute(
        text("SELECT action FROM audit_log WHERE seq = :s"), {"s": result.audit_seq}
    ).scalar_one()
    assert action == "SOURCE_MAPPING_PROMOTED"


def test_promotion_apply_refuses_without_a_token(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    for bogus in ("", "not-a-token", "a.b"):
        with pytest.raises(decision_service.PreviewForgedError):
            _promote(conn, world, manifest, reviewer, bogus)


def test_a_promotion_token_signed_with_another_key_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewForgedError):
        _promote(conn, world, manifest, reviewer, preview.token or "", secret="another-key")


def test_a_responsibility_token_cannot_be_spent_on_a_promotion(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The two fingerprints are namespaced, so the tokens are not interchangeable.

    Without the namespace this would rest on the two payloads happening to differ, which
    stops being true the moment somebody adds a field to one of them.
    """
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    decision = _preview(conn, world, manifest, reviewer)
    _apply(conn, world, manifest, reviewer, decision.token or "")

    with pytest.raises(decision_service.PreviewStaleError):
        _promote(conn, world, manifest, reviewer, decision.token or "")


def test_a_trust_change_after_the_promotion_preview_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The redirect target was verified when the reviewer looked, and is not now."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    assert preview.token

    conn.execute(
        text("UPDATE official_domain SET is_active = false WHERE host = :h"),
        {"h": REDIRECT_HOST},
    )
    with pytest.raises(decision_service.DecisionRefusedError):
        _promote(conn, world, manifest, reviewer, preview.token)

    assert (
        conn.execute(
            text("SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL")
        ).scalar_one()
        == 0
    )


def test_an_expired_promotion_preview_is_refused(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.PreviewStaleError, match="older than"):
        _promote(
            conn,
            world,
            manifest,
            reviewer,
            preview.token or "",
            now=datetime.now(UTC) + timedelta(seconds=1000),
        )


def test_promotion_requires_a_reason(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    with pytest.raises(decision_service.DecisionRefusedError, match="record why"):
        _promote(conn, world, manifest, reviewer, preview.token or "", reason="   ")


def test_promotion_creates_no_claim_and_no_canonical_row(
    conn: Connection, world: dict[str, Any], manifest: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """Requirement 8, as an assertion. Promotion earns eligibility; it publishes nothing."""
    snapshot_with(conn, world, effective_url=EFFECTIVE_URL)
    _make_verified(conn, world, reviewer)
    preview = _promotion_preview(conn, world, manifest, reviewer)
    _promote(conn, world, manifest, reviewer, preview.token or "")

    for table in ("field_claim", "field_provenance", "change_proposal", "change_event"):
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    canonical = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM university) + (SELECT count(*) FROM program)"
            "     + (SELECT count(*) FROM tuition)"
        )
    ).scalar_one()
    assert canonical == 0
