"""The whole human review workflow, end to end, on fixture data (Step 5C.7B §21, §24, §26).

WHY THIS EXISTS AS ONE TEST
===========================
Every piece of this chain is tested somewhere: authentication, the domain decision, the
pilot decision, registration, the responsibility decision, promotion, C27's field
scoping, readiness. What nothing tested was that they *compose* -- that the output of
each step is the input the next one expects, and that a person can get from "I have
read this page" to "a candidate from it is promotable" without a gap.

That is also where a real reviewer discovers a missing command, which is exactly what
Step 5C.7A found: two of the seven steps had no operator entry point at all.

THE MULTI-RESPONSIBILITY CASE IS THE POINT
==========================================
`test_three_responsibilities_on_one_page_get_three_different_answers` mirrors the real
CUHK pattern: one physical URL carrying three responsibility claims. One is verified,
one rejected, one left needing review, and only the verified one obtains publication
authority. A source-level model cannot express that, and the pilot has 49 pages shaped
this way.

Everything here uses `[TEST ONLY]` identities and invented hosts. No real pilot row is
touched, and `refuse_test_identity_on_real_data` stops these identities reaching the
operator commands that would.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.identity.passwords import hash_password
from app.domains.pilot import verification as pilot
from app.domains.verification.authentication import authenticate
from app.domains.verification.identity import provision_reviewer
from app.domains.verification.policy import Blocker
from app.domains.verification.promotion import Actor, promote
from app.domains.verification.readiness import blockers_for
from tests.integration.conftest import fixture_binding
from tests.integration.test_publication_eligibility import sha

pytestmark = pytest.mark.integration

PASSWORD = "workflow-fixture-password"
HOST = "rivermouth.ac.uk"
SHARED_URL = f"https://{HOST}/admissions/english-fees-and-programmes"


@pytest.fixture
def reviewer(conn: Connection) -> dict[str, Any]:
    """An authenticated `[TEST ONLY]` reviewer, as a real one would arrive."""
    unique = uuid.uuid4().hex[:8]
    email = f"workflow-{unique}@example.test"
    created = provision_reviewer(
        conn, email=email, display_name=f"Workflow {unique}", test_only=True
    )
    conn.execute(
        text("UPDATE app_user SET password_hash = :h WHERE id = :i"),
        {"h": hash_password(PASSWORD), "i": created.id},
    )
    session = authenticate(conn, email=email, password=PASSWORD)
    return {"session": session, "email": email}


@pytest.fixture
def workbook(conn: Connection) -> dict[str, Any]:
    """One institution, one URL, three responsibility claims. The CUHK shape."""
    ids = {name: uuid.uuid4() for name in ("list", "target", "submission", "source")}
    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, "
            "file_sha256, file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:i, 'Workflow list', :v, 'w.xlsx', :s, 1024, 's', 1)"
        ),
        {"i": ids["list"], "v": ids["list"].hex[:8], "s": sha(str(ids["list"]))},
    )
    conn.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
            "latest_list_id, destination_code) VALUES (:i, :k, :l, :l, 'GB')"
        ),
        {"i": ids["target"], "k": f"rivermouth-{ids['target'].hex[:8]}", "l": ids["list"]},
    )
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "file_byte_size, template_version, selected_university_count) "
            "VALUES (:i, :h, 'w.xlsx', 1024, 'v1', 1)"
        ),
        {"i": ids["submission"], "h": sha(str(ids["submission"]))},
    )
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:i, :u, :h, 'admissions_page', 'MONTHLY', 'STATIC')"
        ),
        {"i": ids["source"], "u": SHARED_URL, "h": sha(SHARED_URL)},
    )

    claims: dict[str, uuid.UUID] = {}
    for index, responsibility in enumerate(
        ("LANGUAGE_REQUIREMENTS", "TUITION_FEES", "PROGRAM_CATALOG"), start=1
    ):
        claim_id = uuid.uuid4()
        # `ix_pilot_collected_source_physical` is UNIQUE on
        # (submission_id, target_institution_id, url_sha256) WHERE duplicate_of_source_ref
        # IS NULL. That index IS the 385-over-319 model: one PHYSICAL row per URL, and
        # every further responsibility on that URL is a duplicate row pointing at it.
        # The real CUHK page is exactly this -- S0355 physical, S0356 and S0363 duplicates.
        conn.execute(
            text(
                "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
                "target_institution_id, sheet_row_no, source_type, official_url, "
                "normalized_url, url_sha256, host, acquisition_source_id, "
                "duplicate_of_source_ref) "
                "VALUES (:i, :sub, :ref, :t, :row, :cat, :u, :u, :h, :host, :src, :dup)"
            ),
            {
                "i": claim_id,
                "sub": ids["submission"],
                "ref": f"S{index:04d}",
                "t": ids["target"],
                "row": index + 1,
                "cat": responsibility,
                "u": SHARED_URL,
                # The same URL hash for all three, because that is what the real workbook
                # has: all 385 rows match their source's url_hash, and the claims
                # sharing a URL share it. `promote` compares the two, so a fixture
                # that made them unique tested a shape that cannot occur.
                "h": sha(SHARED_URL),
                "host": HOST,
                "src": ids["source"],
                "dup": None if index == 1 else "S0001",
            },
        )
        claims[responsibility] = claim_id
    return {"ids": ids, "claims": claims}


def _verify_domain(conn: Connection, workbook: dict[str, Any], session: Any) -> uuid.UUID:
    domain_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, "
            "verification_status, verification_method, verification_evidence, "
            "verified_at, verified_by) VALUES (:i, :t, :host, 'VERIFIED_OFFICIAL', "
            "'MANUAL_STAFF_REVIEW', 'listed in the invented register', now(), :by)"
        ),
        {"i": domain_id, "t": workbook["ids"]["target"], "host": HOST, "by": session.id},
    )
    return domain_id


# ===========================================================================
# 21. The whole path
# ===========================================================================


def test_the_full_human_workflow_makes_one_candidate_promotable(
    conn: Connection, reviewer: dict[str, Any], workbook: dict[str, Any]
) -> None:
    """authenticate → domain → pilot-source → register → responsibility → promote.

    Seven steps, each an explicit act. If this fails, a reviewer cannot get from reading
    a page to relying on it, whatever the individual units say.
    """
    session = reviewer["session"]
    actor = pilot.Actor(user_id=session.id)
    language_claim = workbook["claims"]["LANGUAGE_REQUIREMENTS"]

    # 1. Domain identity, before anything semantic.
    _verify_domain(conn, workbook, session)

    # 2. The page itself: "I have read this and it is the right URL."
    pilot.verify_candidate(
        conn,
        candidate_id=language_claim,
        actor=actor,
        reason="the page is the institution's English requirements page",
    )
    assert (
        conn.execute(
            text("SELECT verification_state::text FROM pilot_collected_source WHERE id = :i"),
            {"i": language_claim},
        ).scalar()
        == "VERIFIED"
    )

    # 3. Registration. Creates the mapping and grants nothing.
    mapping_id = pilot.register_verified_candidate(
        conn,
        candidate_id=language_claim,
        source_category="LANGUAGE_REQUIREMENTS",
        actor=actor,
        reason="register the reviewed source",
    )
    mapping = conn.execute(
        text(
            "SELECT verification_status::text AS status, publication_eligibility, "
            "       promoted_source_id FROM source_mapping WHERE id = :i"
        ),
        {"i": mapping_id},
    ).one()
    assert mapping.status == "CANDIDATE", "registration must not verify"
    assert mapping.publication_eligibility == "NOT_ELIGIBLE"
    assert mapping.promoted_source_id is None, "registration must not promote"

    # 4. The responsibility decision: "and it is authoritative for THIS."
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_at = now(), verified_by = :by WHERE id = :i"
        ),
        {"by": session.id, "i": mapping_id},
    )
    assert (
        conn.execute(
            text("SELECT publication_eligibility FROM source_mapping WHERE id = :i"),
            {"i": mapping_id},
        ).scalar()
        == "OFFICIAL_VERIFIED"
    )

    # 5. Promotion: "and we now rely on it."
    result = promote(
        conn,
        mapping_id=mapping_id,
        binding=fixture_binding(conn, mapping_id),
        source_id=workbook["ids"]["source"],
        actor=Actor(id=session.id, display=session.display_name),
        reason="relying on the reviewed English requirements page",
    )
    assert result.already_promoted is False
    assert result.eligibility == "OFFICIAL_VERIFIED"
    assert result.bindings_written > 0

    assert (
        conn.execute(
            text("SELECT publication_eligibility::text FROM source WHERE id = :i"),
            {"i": workbook["ids"]["source"]},
        ).scalar()
        == "OFFICIAL_VERIFIED"
    )

    # 6. And no fact was published along the way.
    assert conn.execute(text("SELECT count(*) FROM field_claim")).scalar() == 0


# ===========================================================================
# 24. Three responsibilities, three answers
# ===========================================================================


def test_three_responsibilities_on_one_page_get_three_different_answers(
    conn: Connection, reviewer: dict[str, Any], workbook: dict[str, Any]
) -> None:
    """The real CUHK pattern. Only the verified responsibility gains authority.

    One URL, one source, one snapshot's worth of evidence, three claims. Verify the
    language one, reject the tuition one, leave the programme one needing review.
    """
    session = reviewer["session"]
    actor = pilot.Actor(user_id=session.id)
    _verify_domain(conn, workbook, session)
    claims = workbook["claims"]

    pilot.verify_candidate(
        conn,
        candidate_id=claims["LANGUAGE_REQUIREMENTS"],
        actor=actor,
        reason="states the English requirement in its own words",
    )
    pilot.reject_candidate(
        conn,
        candidate_id=claims["TUITION_FEES"],
        actor=actor,
        reason="links to the fee schedule, does not state fees",
    )
    pilot.flag_candidate_for_review(
        conn,
        candidate_id=claims["PROGRAM_CATALOG"],
        actor=actor,
        reason="unclear whether this lists programmes or links to a picker",
    )

    states: dict[str, str] = {
        row.source_type: row.verification_state
        for row in conn.execute(
            text(
                "SELECT source_type, verification_state::text FROM pilot_collected_source "
                " WHERE acquisition_source_id = :s ORDER BY source_type"
            ),
            {"s": workbook["ids"]["source"]},
        )
    }
    assert states == {
        "LANGUAGE_REQUIREMENTS": "VERIFIED",
        "TUITION_FEES": "REJECTED",
        "PROGRAM_CATALOG": "NEEDS_REVIEW",
    }

    # Only the verified one may even be registered.
    mapping_id = pilot.register_verified_candidate(
        conn,
        candidate_id=claims["LANGUAGE_REQUIREMENTS"],
        source_category="LANGUAGE_REQUIREMENTS",
        actor=actor,
        reason="register the reviewed source",
    )
    for rejected in ("TUITION_FEES", "PROGRAM_CATALOG"):
        with pytest.raises(pilot.CandidateDecisionRefusedError, match="verify it before"):
            pilot.register_verified_candidate(
                conn,
                candidate_id=claims[rejected],
                source_category=rejected,
                actor=actor,
                reason="attempting to register an undecided responsibility",
            )

    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_at = now(), verified_by = :by WHERE id = :i"
        ),
        {"by": session.id, "i": mapping_id},
    )
    promote(
        conn,
        mapping_id=mapping_id,
        binding=fixture_binding(conn, mapping_id),
        source_id=workbook["ids"]["source"],
        actor=Actor(id=session.id, display=session.display_name),
        reason="relying on the reviewed English requirements page",
    )

    bound = {
        (row.entity_type, row.field_path)
        for row in conn.execute(
            text("SELECT entity_type, field_path FROM source_field_binding WHERE source_id = :s"),
            {"s": workbook["ids"]["source"]},
        )
    }
    assert ("language_requirement", "overall_score") in bound
    assert ("tuition", "amount") not in bound, "a rejected responsibility granted authority"
    assert ("program", "name_en") not in bound, "an undecided responsibility granted authority"


def test_a_candidate_from_the_rejected_responsibility_stays_blocked() -> None:
    """Readiness must say WHY, and say it per responsibility rather than per source."""

    class _Row:
        is_superseded = False
        decision_state = "ACCEPTED"
        domain_status = "VERIFIED_OFFICIAL"
        domain_active = True
        mapping_status = "REJECTED"
        mapping_active = False
        promoted_source_id = None
        source_id = "s"
        responsibility = "TUITION_FEES"
        field_kind = "TUITION"
        eligibility = "OFFICIAL_VERIFIED"
        superseded_by_source_id = None
        scope_unresolved = False

    assert blockers_for(_Row()) == [Blocker.RESPONSIBILITY_NOT_VERIFIED]


# ===========================================================================
# 26. The audit chain
# ===========================================================================


def test_every_step_of_the_workflow_appends_to_one_audit_chain(
    conn: Connection, reviewer: dict[str, Any], workbook: dict[str, Any]
) -> None:
    """Section 26. Same chain, same actor, head intact afterwards."""
    session = reviewer["session"]
    actor = pilot.Actor(user_id=session.id)
    before = conn.execute(text("SELECT coalesce(max(seq), 0) FROM audit_log")).scalar()

    _verify_domain(conn, workbook, session)
    claim = workbook["claims"]["LANGUAGE_REQUIREMENTS"]
    pilot.verify_candidate(
        conn, candidate_id=claim, actor=actor, reason="reviewed against the packet"
    )
    mapping_id = pilot.register_verified_candidate(
        conn,
        candidate_id=claim,
        source_category="LANGUAGE_REQUIREMENTS",
        actor=actor,
        reason="register the reviewed source",
    )
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_at = now(), verified_by = :by WHERE id = :i"
        ),
        {"by": session.id, "i": mapping_id},
    )
    promote(
        conn,
        mapping_id=mapping_id,
        binding=fixture_binding(conn, mapping_id),
        source_id=workbook["ids"]["source"],
        actor=Actor(id=session.id, display=session.display_name),
        reason="relying on the reviewed page",
    )

    entries = conn.execute(
        text(
            "SELECT action, actor_type::text AS actor_type, actor_id, seq, row_hash "
            "  FROM audit_log WHERE seq > :s ORDER BY seq"
        ),
        {"s": before},
    ).all()
    assert entries, "the workflow appended nothing to the audit chain"
    assert all(
        entry.actor_id == session.id for entry in entries
    ), "an audit row named somebody other than the authenticated reviewer"
    assert all(entry.actor_type == "USER" for entry in entries)
    assert "SOURCE_MAPPING_PROMOTED" in {entry.action for entry in entries}

    head = conn.execute(text("SELECT last_seq, last_row_hash FROM audit_chain_head")).one()
    assert head.last_seq == entries[-1].seq
    assert head.last_row_hash == entries[-1].row_hash
