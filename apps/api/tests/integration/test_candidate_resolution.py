"""Step 5C.9: the human scope and conflict resolution workflow.

Runs in a rolled-back transaction against `datahub_test`. The real ANU candidate-review
state must be untouched by this suite, and `app.db.safety` plus the `conn` fixture are
what make that structural rather than hoped for.

Each test corresponds to a way a scope or conflict decision could be recorded about
something the reviewer did not look at, or could let an unresolved candidate look ready.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.claims import resolution as candidate_resolution
from app.domains.claims.precedence import (
    DIM_APPLICANT_COUNTRY,
    DIM_QUALIFICATION_TYPE,
    ScopeState,
)
from app.domains.identity.passwords import hash_password
from app.domains.verification import decisions as decision_service
from app.domains.verification.identity import provision_reviewer

pytestmark = pytest.mark.integration

SECRET = "scope-fixture-signing-key"
HOST = "www.scope-fixture.test"
URL = f"https://{HOST}/entry-requirements"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def reviewer(conn: Connection) -> dict[str, Any]:
    unique = uuid.uuid4().hex[:8]
    email = f"scope-{unique}@example.test"
    created = provision_reviewer(conn, email=email, display_name=f"Scope {unique}", test_only=True)
    conn.execute(
        text("UPDATE app_user SET password_hash = :h WHERE id = :i"),
        {"h": hash_password("scope-fixture-password-1"), "i": created.id},
    )
    return {"id": created.id, "email": email}


@pytest.fixture
def world(conn: Connection, reviewer: dict[str, Any]) -> dict[str, Any]:
    """One institution, one source, and two candidates that share a context.

    Two, because a single candidate can never exercise the conflict path, and the whole
    point of Step 5C.9 is what happens when two claims compete.
    """
    ids = {
        name: uuid.uuid4()
        for name in (
            "list",
            "target",
            "submission",
            "source",
            "domain",
            "mapping",
            "pilot",
            "attempt",
            "run",
            "snapshot",
            "extraction",
            "c1",
            "c2",
            "stale",
        )
    }
    match_key = f"scope university {ids['target'].hex[:8]}"

    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, file_sha256,"
            " file_byte_size, sheet_name, imported_row_count)"
            " VALUES (:i, 'Scope list', :v, 's.xlsx', :s, 1024, 's', 1)"
        ),
        {"i": ids["list"], "v": ids["list"].hex[:8], "s": sha(str(ids["list"]))},
    )
    conn.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id,"
            " latest_list_id, destination_code) VALUES (:i, :k, :l, :l, 'GB')"
        ),
        {"i": ids["target"], "k": match_key, "l": ids["list"]},
    )
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename,"
            " file_byte_size, template_version, selected_university_count)"
            " VALUES (:i, :h, 's.xlsx', 1024, 'v1', 1)"
        ),
        {"i": ids["submission"], "h": sha(str(ids["submission"]))},
    )
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency,"
            " fetch_strategy) VALUES (:i, :u, :h, 'admissions_page', 'MONTHLY', 'STATIC')"
        ),
        {"i": ids["source"], "u": URL, "h": sha(URL)},
    )
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host,"
            " verification_status, verification_method, verification_evidence,"
            " verified_at, verified_by) VALUES (:i, :t, :host, 'VERIFIED_OFFICIAL',"
            " 'MANUAL_STAFF_REVIEW', 'fixture', now(), :by)"
        ),
        {"i": ids["domain"], "t": ids["target"], "host": HOST, "by": reviewer["id"]},
    )
    conn.execute(
        text(
            "INSERT INTO pilot_collected_source (id, submission_id, source_ref,"
            " target_institution_id, sheet_row_no, source_type, official_url,"
            " normalized_url, url_sha256, host, acquisition_source_id, verification_state,"
            " verified_at, verified_by, verification_reason)"
            " VALUES (:i, :sub, 'S0001', :t, 2, 'UNDERGRADUATE_ADMISSIONS', :u, :u, :h,"
            " :host, :src, 'VERIFIED', now(), :by, 'fixture')"
        ),
        {
            "i": ids["pilot"],
            "sub": ids["submission"],
            "t": ids["target"],
            "u": URL,
            "h": sha(URL),
            "host": HOST,
            "src": ids["source"],
            "by": reviewer["id"],
        },
    )
    conn.execute(
        text(
            # `ck_source_mapping_verified_mapping_records_actor_and_time`: a verified
            # mapping must name who verified it and when. A status without an actor is
            # exactly the unattributed decision the schema refuses to store.
            "INSERT INTO source_mapping (id, target_institution_id, source_category, url,"
            " normalized_url, url_sha256, host, official_domain_id, verification_status,"
            " verified_by, verified_at, collection_priority, discovered_by)"
            " VALUES (:i, :t, 'UNDERGRADUATE_ADMISSIONS', :u, :u, :h, :host, :d,"
            " 'VERIFIED_OFFICIAL', :by, now(), 3, :by)"
        ),
        {
            "i": ids["mapping"],
            "t": ids["target"],
            "u": URL,
            "h": sha(URL),
            "host": HOST,
            "d": ids["domain"],
            "by": reviewer["id"],
        },
    )
    conn.execute(
        text("UPDATE pilot_collected_source SET promoted_source_mapping_id = :m WHERE id = :i"),
        {"m": ids["mapping"], "i": ids["pilot"]},
    )

    # Evidence chain: attempt -> run -> blob -> snapshot -> extraction.
    conn.execute(
        text(
            "INSERT INTO fetch_attempt (id, source_id, attempt_no, cycle_key, state,"
            " scheduled_for, finalized_at)"
            " VALUES (:i, :s, 1, :c, 'FINALIZED', now(), now())"
        ),
        {"i": ids["attempt"], "s": ids["source"], "c": uuid.uuid4().hex[:16]},
    )
    conn.execute(
        text(
            "INSERT INTO fetch_run (id, source_id, attempt_id, attempt_no, status, fetcher,"
            " started_at, finished_at) VALUES (:i, :s, :a, 1, 'OK', 'STATIC', now(), now())"
        ),
        {"i": ids["run"], "s": ids["source"], "a": ids["attempt"]},
    )
    content_hash = sha(f"body-{ids['snapshot']}")
    conn.execute(
        text(
            "INSERT INTO content_blob (content_hash, storage_key, content_type, byte_size,"
            " first_observed_at) VALUES (:c, :k, 'text/html', 2048, now())"
        ),
        {"c": content_hash, "k": f"evidence/{content_hash[:2]}/{content_hash}"},
    )
    conn.execute(
        text(
            "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, observed_at,"
            " requested_url, http_status, fetcher, content_type)"
            " VALUES (:i, :r, :s, :c, now(), :u, 200, 'STATIC', 'text/html')"
        ),
        {
            "i": ids["snapshot"],
            "r": ids["run"],
            "s": ids["source"],
            "c": content_hash,
            "u": URL,
        },
    )

    current = _current_rule(conn)
    conn.execute(
        text(
            # `extraction` has no started/finished columns, and its extractor_version is
            # the DOCUMENT artifact version -- the second of the two D55 axes that
            # `current_only` filters on.
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version,"
            " status) VALUES (:i, :s, 'html-document-normaliser', :dv, 'OK')"
        ),
        {"i": ids["extraction"], "s": ids["snapshot"], "dv": current["document_version"]},
    )
    # c1 and c2 share a context (test=IELTS) and disagree on the score, which is what
    # makes them a real group. `stale` carries a superseded rule version so the
    # current-only filter has something to exclude -- and it is a separate ROW, because
    # `field_claim_candidate` is append-only and a trigger refuses UPDATE.
    for key, raw, value, rule_version in (
        (
            "c1",
            "IELTS 7.0 overall for international applicants",
            {"test": "IELTS", "score": 7.0, "operator": "GTE"},
            current["rule_version"],
        ),
        (
            "c2",
            "IELTS 6.5 overall for international applicants",
            {"test": "IELTS", "score": 6.5, "operator": "GTE"},
            current["rule_version"],
        ),
        (
            "stale",
            "IELTS 7.0 overall (old rule)",
            {"test": "IELTS", "score": 7.0, "operator": "GTE"},
            "1",
        ),
    ):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate (id, extraction_id,"
                " pilot_collected_source_id, source_responsibility, field_kind,"
                " value_normalized, value_raw_text, evidence_text, locator, extractor_name,"
                " extractor_version, confidence_band, confidence_reason, claim_fingerprint)"
                " VALUES (:i, :e, :p, 'UNDERGRADUATE_ADMISSIONS', 'LANGUAGE_OVERALL_SCORE',"
                " CAST(:v AS jsonb), :raw, :raw, CAST(:loc AS jsonb), :en, :ev, 'MEDIUM',"
                " 'fixture band', :fp)"
            ),
            {
                "i": ids[key],
                "e": ids["extraction"],
                "p": ids["pilot"],
                "v": _json(value),
                "raw": raw,
                "loc": '{"heading_path": ["Entry requirements", "International students"]}',
                "en": current["rule_name"],
                "ev": rule_version,
                "fp": sha(raw),
            },
        )
    return {"ids": ids, "match_key": match_key, "current": current}


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)


def _current_rule(conn: Connection) -> dict[str, str]:
    """The rule and document versions the queue treats as current.

    Read from `review.CURRENT_*` rather than hard-coded: the queue filters on both axes,
    and a fixture pinned to a stale version would silently produce an empty queue and a
    passing test that proved nothing.
    """
    from app.domains.claims import review as review_rules

    # The language extractor specifically: `current_only` matches on name@version, and a
    # candidate labelled with another rule's name would never look current however new
    # its version is.
    name, version = next((n, v) for n, v in review_rules.CURRENT_RULES if n.startswith("language"))
    return {
        "rule_name": name,
        "rule_version": version,
        "document_version": review_rules.CURRENT_DOCUMENTS[0],
    }


def actor(reviewer: dict[str, Any], *, is_test: bool = False) -> decision_service.Reviewer:
    return decision_service.Reviewer(
        id=reviewer["id"],
        email=reviewer["email"],
        display_name="Scope Fixture",
        is_test=is_test,
    )


# ===========================================================================
# 1. the queue
# ===========================================================================


def test_the_queue_returns_current_candidates_with_everything_needed(
    conn: Connection, world: dict[str, Any]
) -> None:
    items = candidate_resolution.queue(conn, institution=world["match_key"])
    assert len(items) == 2, "the third candidate is superseded and must not appear"
    item = items[0]
    assert item.source_ref == "S0001"
    assert item.field_kind == "LANGUAGE_OVERALL_SCORE"
    assert item.responsibility == "UNDERGRADUATE_ADMISSIONS"
    assert item.requested_host == HOST
    assert item.heading_path == ("Entry requirements", "International students")
    assert item.evidence_text
    assert item.human_scope_state == ScopeState.UNSCOPED.value
    assert item.scope_resolved is False


def test_a_superseded_candidate_is_never_reviewable(
    conn: Connection, world: dict[str, Any]
) -> None:
    """Not filtered out -- never selected. A filter could be inverted; this cannot.

    The fixture's `stale` row carries an older rule version. It is a separate row rather
    than a mutated one because `field_claim_candidate` is append-only and a trigger
    refuses UPDATE -- which is also how supersession happens in production.
    """
    items = candidate_resolution.queue(conn, institution=world["match_key"])
    assert {item.candidate_id for item in items} == {world["ids"]["c1"], world["ids"]["c2"]}
    assert world["ids"]["stale"] not in {item.candidate_id for item in items}


def test_machine_hints_and_a_suggested_construction_are_offered(
    conn: Connection, world: dict[str, Any]
) -> None:
    """The evidence says "international", which is a fee status, not a country."""
    item = candidate_resolution.queue(conn, institution=world["match_key"])[0]
    assert "INTERNATIONAL" in item.machine_category_hints
    assert item.machine_country_hints == ()
    suggested = {c["dimension_code"] for c in item.suggested_criteria}
    assert suggested == {"residency_status"}


# ===========================================================================
# 2. scope resolution
# ===========================================================================


def _preview(
    conn: Connection, world: dict[str, Any], who: dict[str, Any], **overrides: Any
) -> candidate_resolution.ScopePreview:
    kwargs: dict[str, Any] = {
        "institution": world["match_key"],
        "candidate_id": world["ids"]["c1"],
        "state": ScopeState.APPLICANT_JURISDICTION.value,
        "criteria": (
            {"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},
        ),
        "reason": "the section heading names international applicants",
        "reviewer": actor(who),
        "secret": SECRET,
    }
    kwargs.update(overrides)
    return candidate_resolution.preview_scope(conn, **kwargs)


def test_a_valid_scope_preview_writes_nothing_and_issues_a_token(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    before = conn.execute(text("SELECT count(*) FROM candidate_scope_resolution")).scalar_one()
    result = _preview(conn, world, reviewer)

    assert result.valid is True
    assert result.token
    assert result.before_state == ScopeState.UNSCOPED.value
    assert result.creates_field_claim is False
    assert result.modifies_canonical is False
    assert "SCOPE_UNRESOLVED" in result.blockers_before
    assert "SCOPE_UNRESOLVED" not in result.blockers_after
    assert (
        conn.execute(text("SELECT count(*) FROM candidate_scope_resolution")).scalar_one() == before
    )


def test_the_state_must_match_the_criteria(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """A record claiming a dimension it does not carry would misstate the decision."""
    result = _preview(
        conn,
        world,
        reviewer,
        state=ScopeState.QUALIFICATION_SYSTEM.value,
        criteria=({"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},),
    )
    assert result.valid is False
    assert candidate_resolution.BlockerCode.STATE_CRITERIA_MISMATCH in {
        b.code for b in result.blockers
    }


def test_universal_explicit_may_not_carry_criteria(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(
        conn,
        world,
        reviewer,
        state=ScopeState.UNIVERSAL_EXPLICIT.value,
        criteria=({"dimension_code": DIM_QUALIFICATION_TYPE, "operator": "EQUALS", "value": "IB"},),
    )
    assert result.valid is False


def test_an_unknown_dimension_is_refused(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(
        conn,
        world,
        reviewer,
        criteria=({"dimension_code": "china_specific", "operator": "EQUALS", "value": "x"},),
    )
    assert result.valid is False
    assert candidate_resolution.BlockerCode.UNKNOWN_DIMENSION in {b.code for b in result.blockers}


def test_a_scope_decision_requires_a_reason(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(conn, world, reviewer, reason="   ")
    assert result.valid is False
    assert candidate_resolution.BlockerCode.REASON_REQUIRED in {b.code for b in result.blockers}


def test_a_fixture_identity_may_not_resolve_scope(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    result = _preview(conn, world, reviewer, reviewer=actor(reviewer, is_test=True))
    assert result.valid is False
    assert candidate_resolution.BlockerCode.FIXTURE_IDENTITY in {b.code for b in result.blockers}


def test_a_superseded_candidate_cannot_be_scoped(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """A candidate nobody may review is a candidate nobody may scope."""
    result = _preview(conn, world, reviewer, candidate_id=world["ids"]["stale"])
    assert result.valid is False
    assert candidate_resolution.BlockerCode.UNKNOWN_CANDIDATE in {b.code for b in result.blockers}


def test_applying_a_scope_records_it_and_clears_the_blocker(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, reviewer)
    result = candidate_resolution.apply_scope(
        conn,
        token=preview.token or "",
        institution=world["match_key"],
        candidate_id=world["ids"]["c1"],
        state=ScopeState.APPLICANT_JURISDICTION.value,
        criteria=({"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},),
        reason="the section heading names international applicants",
        reviewer=actor(reviewer),
        secret=SECRET,
        ttl_seconds=900,
    )
    assert result.after_state == ScopeState.APPLICANT_JURISDICTION.value
    assert result.audit_action == candidate_resolution.AUDIT_SCOPE
    assert result.audit_chain_ok is True
    assert result.field_claim == 0
    assert result.canonical_unchanged is True
    assert "SCOPE_UNRESOLVED" not in result.blockers_now

    stored = conn.execute(
        text(
            "SELECT scope_state, reason, actor_id FROM candidate_scope_resolution"
            " WHERE candidate_id = :i"
        ),
        {"i": world["ids"]["c1"]},
    ).one()
    assert stored.scope_state == ScopeState.APPLICANT_JURISDICTION.value
    assert stored.actor_id == reviewer["id"]


def test_the_database_refuses_an_unscoped_row_that_names_a_scope(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """The CHECK, not just the service: UNSCOPED must never look resolved."""
    from sqlalchemy.exc import IntegrityError

    universal = conn.execute(
        text("SELECT id FROM applicant_scope WHERE code = 'UNIVERSAL'")
    ).scalar_one()
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO candidate_scope_resolution (candidate_id, actor_id,"
                " scope_state, applicant_scope_id, reason, decided_at)"
                " VALUES (:c, :a, 'UNSCOPED', :s, 'should be refused', now())"
            ),
            {"c": world["ids"]["c1"], "a": reviewer["id"], "s": universal},
        )


# ===========================================================================
# 3. stale-preview protection
# ===========================================================================


def test_a_scope_apply_without_a_token_is_refused(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    for bogus in ("", "not-a-token", "a.b"):
        with pytest.raises(decision_service.PreviewForgedError):
            candidate_resolution.apply_scope(
                conn,
                token=bogus,
                institution=world["match_key"],
                candidate_id=world["ids"]["c1"],
                state=ScopeState.APPLICANT_JURISDICTION.value,
                criteria=(),
                reason="r",
                reviewer=actor(reviewer),
                secret=SECRET,
                ttl_seconds=900,
            )


def test_changing_the_reason_after_preview_is_stale(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, reviewer)
    with pytest.raises(decision_service.PreviewStaleError):
        candidate_resolution.apply_scope(
            conn,
            token=preview.token or "",
            institution=world["match_key"],
            candidate_id=world["ids"]["c1"],
            state=ScopeState.APPLICANT_JURISDICTION.value,
            criteria=(
                {"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},
            ),
            reason="a different reason entirely",
            reviewer=actor(reviewer),
            secret=SECRET,
            ttl_seconds=900,
        )


def test_an_expired_scope_preview_is_refused(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, reviewer)
    with pytest.raises(decision_service.PreviewStaleError, match="older than"):
        candidate_resolution.apply_scope(
            conn,
            token=preview.token or "",
            institution=world["match_key"],
            candidate_id=world["ids"]["c1"],
            state=ScopeState.APPLICANT_JURISDICTION.value,
            criteria=(
                {"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},
            ),
            reason="the section heading names international applicants",
            reviewer=actor(reviewer),
            secret=SECRET,
            ttl_seconds=900,
            now=datetime.now(UTC) + timedelta(seconds=1000),
        )


def test_a_token_signed_with_another_key_is_refused(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, reviewer)
    with pytest.raises(decision_service.PreviewForgedError):
        candidate_resolution.apply_scope(
            conn,
            token=preview.token or "",
            institution=world["match_key"],
            candidate_id=world["ids"]["c1"],
            state=ScopeState.APPLICANT_JURISDICTION.value,
            criteria=(
                {"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},
            ),
            reason="the section heading names international applicants",
            reviewer=actor(reviewer),
            secret="a-different-key",
            ttl_seconds=900,
        )


# ===========================================================================
# 4. conflict groups
# ===========================================================================


def test_two_values_in_one_context_are_exposed_as_a_group(
    conn: Connection, world: dict[str, Any]
) -> None:
    groups = candidate_resolution.conflict_groups(conn, institution=world["match_key"])
    assert groups, "the two candidates share a context and must form a group"
    group = groups[0]
    # INSUFFICIENT_CONTEXT, not CONFLICTS -- and that is the correct answer.
    #
    # The two candidates share test=IELTS and disagree on the score, but neither states
    # an applicant scope, a programme or a period. `grouping` treats a contradiction
    # under an unknown context as unconfirmed rather than established: asserting that two
    # sources contradict each other requires that they were answering the same question,
    # and a key of unknowns has not shown that. An IELTS 7.0 for one audience and 6.5 for
    # another are not rival answers.
    #
    # Either way a human must look, which is what `needs_resolution` records.
    assert group["verdict"] == "INSUFFICIENT_CONTEXT"
    assert len(group["members"]) == 2
    assert group["needs_resolution"] is True


def test_no_winner_is_chosen_for_the_reviewer(conn: Connection, world: dict[str, Any]) -> None:
    """Every member is reported; nothing marks one as preferred."""
    groups = candidate_resolution.conflict_groups(conn, institution=world["match_key"])
    for group in groups:
        assert "winner" not in group
        assert "preferred" not in group
        assert group["action"] is None


def test_a_conflict_resolution_must_name_a_candidate_only_when_selecting(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    groups = candidate_resolution.conflict_groups(conn, institution=world["match_key"])
    fingerprint = groups[0]["context_fingerprint"]

    stray = candidate_resolution.preview_conflict(
        conn,
        institution=world["match_key"],
        context_fingerprint=fingerprint,
        action="LEFT_UNRESOLVED",
        selected_candidate_id=world["ids"]["c1"],
        reason="r",
        reviewer=actor(reviewer),
        secret=SECRET,
    )
    assert stray.valid is False

    outside = candidate_resolution.preview_conflict(
        conn,
        institution=world["match_key"],
        context_fingerprint=fingerprint,
        action="SELECTED_SUPPORTED_CLAIM",
        selected_candidate_id=uuid.uuid4(),
        reason="r",
        reviewer=actor(reviewer),
        secret=SECRET,
    )
    assert outside.valid is False
    assert candidate_resolution.BlockerCode.SELECTION_NOT_IN_GROUP in {
        b.code for b in outside.blockers
    }


def test_applying_a_conflict_resolution_records_it(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    groups = candidate_resolution.conflict_groups(conn, institution=world["match_key"])
    fingerprint = groups[0]["context_fingerprint"]
    preview = candidate_resolution.preview_conflict(
        conn,
        institution=world["match_key"],
        context_fingerprint=fingerprint,
        action="LEFT_UNRESOLVED",
        selected_candidate_id=None,
        reason="both values are plausible and the page does not say which applies",
        reviewer=actor(reviewer),
        secret=SECRET,
    )
    assert preview.valid is True

    result = candidate_resolution.apply_conflict(
        conn,
        token=preview.token or "",
        institution=world["match_key"],
        context_fingerprint=fingerprint,
        action="LEFT_UNRESOLVED",
        selected_candidate_id=None,
        reason="both values are plausible and the page does not say which applies",
        reviewer=actor(reviewer),
        secret=SECRET,
        ttl_seconds=900,
    )
    assert result.audit_action == candidate_resolution.AUDIT_CONFLICT
    assert result.audit_chain_ok is True
    assert result.field_claim == 0
    assert result.canonical_unchanged is True

    stored = conn.execute(
        text(
            "SELECT action, verdict_at_decision FROM candidate_conflict_resolution"
            " WHERE context_fingerprint = :f"
        ),
        {"f": fingerprint},
    ).one()
    assert stored.action == "LEFT_UNRESOLVED"


def test_left_unresolved_does_not_clear_the_conflict_blocker(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    """ "Somebody looked and could not tell" is recorded, and is still not resolved."""
    groups = candidate_resolution.conflict_groups(conn, institution=world["match_key"])
    group = next((g for g in groups if g["needs_resolution"]), None)
    if group is None:
        pytest.skip("this fixture produced no group needing resolution")
    preview = candidate_resolution.preview_conflict(
        conn,
        institution=world["match_key"],
        context_fingerprint=group["context_fingerprint"],
        action="LEFT_UNRESOLVED",
        selected_candidate_id=None,
        reason="cannot tell",
        reviewer=actor(reviewer),
        secret=SECRET,
    )
    candidate_resolution.apply_conflict(
        conn,
        token=preview.token or "",
        institution=world["match_key"],
        context_fingerprint=group["context_fingerprint"],
        action="LEFT_UNRESOLVED",
        selected_candidate_id=None,
        reason="cannot tell",
        reviewer=actor(reviewer),
        secret=SECRET,
        ttl_seconds=900,
    )
    items = candidate_resolution.queue(conn, institution=world["match_key"])
    affected = [i for i in items if i.context_fingerprint == group["context_fingerprint"]]
    assert affected and all(not i.conflict_resolved for i in affected)


# ===========================================================================
# 5. the blocker matrix, and the promise that nothing was published
# ===========================================================================


def test_no_candidate_is_ready_while_scope_is_unresolved(
    conn: Connection, world: dict[str, Any]
) -> None:
    matrix = candidate_resolution.blocker_matrix(conn, institution=world["match_key"])
    assert matrix["current_candidates"] == 2
    assert matrix["ready"] == 0
    assert matrix["scope_unresolved"] == 2
    assert "SCOPE_UNRESOLVED" in matrix["blocker_counts"]


def test_the_whole_workflow_creates_no_claim_and_no_canonical_row(
    conn: Connection, world: dict[str, Any], reviewer: dict[str, Any]
) -> None:
    preview = _preview(conn, world, reviewer)
    candidate_resolution.apply_scope(
        conn,
        token=preview.token or "",
        institution=world["match_key"],
        candidate_id=world["ids"]["c1"],
        state=ScopeState.APPLICANT_JURISDICTION.value,
        criteria=({"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": "CN"},),
        reason="the section heading names international applicants",
        reviewer=actor(reviewer),
        secret=SECRET,
        ttl_seconds=900,
    )
    for table in ("field_claim", "field_provenance", "change_proposal", "change_event"):
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    canonical = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM university) + (SELECT count(*) FROM program)"
            " + (SELECT count(*) FROM tuition)"
        )
    ).scalar_one()
    assert canonical == 0
