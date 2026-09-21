"""The candidate-review plane against the database (Step 5C.3 sections 1-3, 22, 30-34).

WHY THERE IS A NEW TABLE, DEMONSTRATED
======================================
Section 3 says to reuse the existing review structures if they fit. The first four tests
attempt exactly that and read what the database says, because "it does not fit" is a
claim that should cost something to make.

Two of them are refusals with a message. One of them is **not** a refusal, and that is
the finding: `field_conflict` accepts a row naming candidate ids perfectly happily,
because it has no foreign key on `entity_id` or `competing_claim_ids`. It is still the
wrong home -- it is keyed on the canonical entity a conflict is *about*, and every
canonical table is empty -- but the honest statement is "it would accept a row that
means nothing", not "the database stops us".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.acquisition.lease import claim_next
from app.domains.acquisition.recorder import record_outcome
from app.domains.claims.review import (
    CURRENT_RULES,
    Decision,
    ReviewError,
    record_decision,
)
from app.domains.claims.runner import (
    CURRENT_DOCUMENT_VERSIONS,
    publish_rule_versions,
    run_claims,
    targets_for_claims,
)
from app.domains.extraction.runner import (
    LIVE_DOCUMENT_VERSIONS,
    ExtractionReport,
    extract_one,
    publish_document_versions,
    targets_for_extraction,
)
from tests.integration.test_acquisition_evidence import (  # noqa: F401 - fixtures
    _outcome,
    pilot,
    registered,
    store,
)
from tests.integration.test_document_extraction_plane import (  # noqa: F401 - fixtures
    _SameTransactionEngine,
    artifacts,
)
from tests.integration.test_field_claim_plane import ADMISSIONS_PAGE

# ruff: noqa: F811 -- importing a pytest fixture and then naming it as a test parameter
# is how fixture reuse across modules works.
pytestmark = pytest.mark.integration


@pytest.fixture
def reviewable(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> dict[str, Any]:
    """One extracted page with candidate claims, and an actor who can decide on them."""
    source_id = conn.execute(
        text("SELECT id FROM source WHERE url = :url"), {"url": registered["shared_url"]}
    ).scalar_one()
    cycle = uuid.uuid4().hex[:10]
    conn.execute(
        text("INSERT INTO fetch_attempt (id, source_id, cycle_key) VALUES (:i, :s, :c)"),
        {"i": uuid.uuid4(), "s": source_id, "c": cycle},
    )
    lease = claim_next(conn, cycle_key=cycle, worker="w")
    assert lease is not None
    outcome = _outcome(lease.url, content=ADMISSIONS_PAGE)
    outcome.content_type = "text/html; charset=utf-8"
    record_outcome(conn, lease=lease, outcome=outcome, store=store)

    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())
    report = run_claims(engine, artifacts=artifacts)
    assert report.claims_created > 0, report.summary()

    actor = uuid.uuid4()
    conn.execute(
        text("INSERT INTO app_user (id, email, display_name) VALUES (:i, :e, 'Reviewer')"),
        {"i": actor, "e": f"reviewer-{actor.hex[:8]}@example.test"},
    )
    candidate = conn.execute(
        text(
            "SELECT candidate_id FROM candidate_review_state "
            " WHERE NOT is_superseded ORDER BY candidate_id LIMIT 1"
        )
    ).scalar_one()
    return {"actor": actor, "candidate": candidate, "source_id": source_id}


# ===========================================================================
# 3. Why the existing review structures cannot be reused
# ===========================================================================


def test_review_task_would_need_a_forbidden_change_proposal(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """`review_task.proposal_id` is NOT NULL and anchored to `change_proposal`, which
    this step forbids creating. Passing a candidate id fails the foreign key; passing
    NULL fails the not-null."""
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="fk_review_task_proposal_id_change_proposal"):
        conn.execute(
            text(
                "INSERT INTO review_task (id, proposal_id, review_round, state) "
                "VALUES (:i, :p, 1, 'OPEN')"
            ),
            {"i": uuid.uuid4(), "p": reviewable["candidate"]},
        )
    savepoint.rollback()

    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="not-null|null value"):
        conn.execute(
            text(
                "INSERT INTO review_task (id, proposal_id, review_round, state) "
                "VALUES (:i, NULL, 1, 'OPEN')"
            ),
            {"i": uuid.uuid4()},
        )
    savepoint.rollback()


def test_review_decision_cannot_name_a_candidate(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """`item_id` is anchored to `change_proposal_item`, so a candidate id is rejected."""
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="fk_review_decision_item_id_change_proposal_item"):
        conn.execute(
            text(
                "INSERT INTO review_decision (id, task_id, item_id, reviewer_id, decision, "
                "  reason_code, reviewed_at) "
                "VALUES (:i, :t, :item, :r, 'RETURN', 'EVIDENCE_INSUFFICIENT', now())"
            ),
            {
                "i": uuid.uuid4(),
                "t": uuid.uuid4(),
                "item": reviewable["candidate"],
                "r": reviewable["actor"],
            },
        )
    savepoint.rollback()


def test_review_decision_kind_cannot_express_a_candidate_decision(
    conn: Connection,
) -> None:
    """`APPROVE | RETURN | CORRECT` is what you do to a proposed *change*. There is no
    member for `REJECTED` and none for `NEEDS_SCOPE_MAPPING`."""
    members = set(
        conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                " WHERE t.typname = 'review_decision_kind'"
            )
        ).scalars()
    )
    assert members == {"APPROVE", "RETURN", "CORRECT"}
    assert not {decision.value for decision in Decision} & members


def test_claim_resolution_needs_a_field_claim_that_c27_refuses(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Doubly blocked: the FK needs a `field_claim`, and C27 refuses to create one for a
    `NOT_ELIGIBLE` source."""
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="fk_claim_resolution_claim_id_field_claim"):
        conn.execute(
            text(
                "INSERT INTO claim_resolution (id, claim_id, entity_type, entity_id, method) "
                "VALUES (:i, :c, 'program', :e, 'FUZZY_MATCH')"
            ),
            {"i": uuid.uuid4(), "c": reviewable["candidate"], "e": uuid.uuid4()},
        )
    savepoint.rollback()


def test_field_conflict_accepts_the_row_and_means_something_else(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """The one that is NOT refused, which is why the claim has to be stated carefully.

    `field_conflict` has no foreign key on `entity_id` or `competing_claim_ids`, so a
    row naming candidate ids inserts happily. It is still the wrong home: it is keyed on
    the canonical entity a conflict is *about*, and every canonical table is empty, so
    the only `entity_id` available is a fiction. "The database would accept a row that
    means nothing" is a weaker guarantee than "the database refuses it", and only the
    first is true.
    """
    candidates = list(
        conn.execute(
            text(
                "SELECT candidate_id FROM candidate_review_state "
                " WHERE NOT is_superseded ORDER BY candidate_id LIMIT 2"
            )
        ).scalars()
    )
    assert len(candidates) == 2

    savepoint = conn.begin_nested()
    inserted = conn.execute(
        text(
            "INSERT INTO field_conflict (id, entity_type, entity_id, field_path, "
            "  competing_claim_ids, detected_at) "
            "VALUES (:i, 'program', :e, 'language.overall', :ids, now()) RETURNING id"
        ),
        {"i": uuid.uuid4(), "e": uuid.uuid4(), "ids": candidates},
    ).scalar_one()
    assert inserted is not None, "the insert was refused after all"
    # And the entity it names does not exist, which is the actual objection.
    assert (
        conn.execute(text("SELECT count(*) FROM program")).scalar_one() == 0
    ), "a canonical program appeared"
    savepoint.rollback()


# ===========================================================================
# 3. What the new table does
# ===========================================================================


def test_a_decision_is_recorded_with_its_actor_and_reason(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    outcome = record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )
    assert outcome.previous == "UNREVIEWED"
    assert outcome.review_count == 1

    row = conn.execute(
        text(
            "SELECT decision_state, reason_code, actor_id, review_count, "
            "       source_not_verified, is_superseded "
            "  FROM candidate_review_state WHERE candidate_id = :c"
        ),
        {"c": reviewable["candidate"]},
    ).one()
    assert row.decision_state == "ACCEPTED"
    assert row.reason_code == "CORRECT_AS_EXTRACTED"
    assert row.actor_id == reviewable["actor"]
    assert row.review_count == 1
    # Section 30: accepting says nothing about whether the source may be published from.
    assert row.source_not_verified is True
    assert row.is_superseded is False


def test_a_later_decision_supersedes_without_erasing_the_earlier_one(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """A reviewer may revisit a decision. The earlier one is history, not an error."""
    earlier = datetime.now(UTC) - timedelta(hours=2)
    record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.NEEDS_CONTEXT,
        reason_code="EVIDENCE_INSUFFICIENT",
        decided_at=earlier,
    )
    outcome = record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.REJECTED,
        reason_code="SITE_CHROME",
    )
    assert outcome.previous == "NEEDS_CONTEXT"
    assert outcome.review_count == 2

    state = conn.execute(
        text(
            "SELECT decision_state, review_count FROM candidate_review_state "
            " WHERE candidate_id = :c"
        ),
        {"c": reviewable["candidate"]},
    ).one()
    assert state.decision_state == "REJECTED"
    assert state.review_count == 2

    history = (
        conn.execute(
            text(
                "SELECT decision FROM field_claim_candidate_review "
                " WHERE candidate_id = :c ORDER BY decided_at"
            ),
            {"c": reviewable["candidate"]},
        )
        .scalars()
        .all()
    )
    assert history == ["NEEDS_CONTEXT", "REJECTED"], "the earlier decision was lost"


def test_a_decision_cannot_be_updated_or_deleted(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Append-only. Revising a decision in place would erase what the reviewer said,
    which is the only thing the row is for."""
    record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )
    for statement in (
        "UPDATE field_claim_candidate_review SET decision = 'REJECTED' WHERE candidate_id = :c",
        "DELETE FROM field_claim_candidate_review WHERE candidate_id = :c",
    ):
        savepoint = conn.begin_nested()
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text(statement), {"c": reviewable["candidate"]})
        savepoint.rollback()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"reason_code": "NOT_A_REAL_CODE"}, "unknown reason code"),
        ({"reason_code": "OTHER"}, "explains nothing"),
    ],
)
def test_the_service_refuses_what_it_cannot_verify(
    conn: Connection, reviewable: dict[str, Any], kwargs: dict[str, Any], match: str
) -> None:
    """A decision that names an unknown reason is not auditable, which is the only
    reason the row exists."""
    with pytest.raises(ReviewError, match=match):
        record_decision(
            conn,
            candidate_id=reviewable["candidate"],
            actor_id=reviewable["actor"],
            decision=Decision.REJECTED,
            **{"reason_code": "SITE_CHROME", **kwargs},
        )


def test_a_decision_must_name_a_real_candidate_and_a_real_actor(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    with pytest.raises(ReviewError, match="no candidate"):
        record_decision(
            conn,
            candidate_id=uuid.uuid4(),
            actor_id=reviewable["actor"],
            decision=Decision.ACCEPTED,
            reason_code="CORRECT_AS_EXTRACTED",
        )
    with pytest.raises(ReviewError, match="someone answerable"):
        record_decision(
            conn,
            candidate_id=reviewable["candidate"],
            actor_id=uuid.uuid4(),
            decision=Decision.ACCEPTED,
            reason_code="CORRECT_AS_EXTRACTED",
        )


def test_the_schema_refuses_a_decision_made_after_it_was_written(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="decided_before_recorded"):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate_review "
                "  (id, candidate_id, actor_id, decision, reason_code, decided_at) "
                "VALUES (:i, :c, :a, 'ACCEPTED', 'CORRECT_AS_EXTRACTED', now() + interval '1 day')"
            ),
            {"i": uuid.uuid4(), "c": reviewable["candidate"], "a": reviewable["actor"]},
        )
    savepoint.rollback()


# ===========================================================================
# 22. Current versions
# ===========================================================================


def test_the_view_and_the_registry_agree_on_current_versions(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Section 22. The whole point of `claim_rule_version`.

    The first attempt froze the pairs into the view, and four rule corrections later it
    reported every current candidate as superseded -- silently, with a total that looked
    plausible. This asserts the registry and the database say the same thing.
    """
    stored = tuple(
        sorted(
            (row.extractor_name, row.version)
            for row in conn.execute(text("SELECT extractor_name, version FROM claim_rule_version"))
        )
    )
    assert stored == CURRENT_RULES


def test_a_superseded_candidate_is_excluded_from_active_review(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """A rule whose version is no longer live produced history. Non-vacuous: the same
    row is current before the version moves and superseded after."""
    candidate = reviewable["candidate"]
    assert (
        conn.execute(
            text("SELECT is_superseded FROM candidate_review_state WHERE candidate_id = :c"),
            {"c": candidate},
        ).scalar_one()
        is False
    )

    name = conn.execute(
        text("SELECT extractor_name FROM field_claim_candidate WHERE id = :c"),
        {"c": candidate},
    ).scalar_one()
    conn.execute(
        text("UPDATE claim_rule_version SET version = '999' WHERE extractor_name = :n"),
        {"n": name},
    )
    assert (
        conn.execute(
            text("SELECT is_superseded FROM candidate_review_state WHERE candidate_id = :c"),
            {"c": candidate},
        ).scalar_one()
        is True
    )


def test_publishing_the_registry_is_idempotent(conn: Connection) -> None:
    """Called on every claim pass, so it must not churn `updated_at` for no reason."""
    publish_rule_versions(conn)
    before = conn.execute(text("SELECT max(updated_at) FROM claim_rule_version")).scalar_one()
    publish_rule_versions(conn)
    after = conn.execute(text("SELECT max(updated_at) FROM claim_rule_version")).scalar_one()
    assert before == after


# ===========================================================================
# 30, 34. Nothing published
# ===========================================================================


def test_a_review_decision_creates_nothing_publishable(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Section 34. An accepted candidate is not a `field_claim`, and accepting one does
    not make it publishable: promotion additionally needs the source to be eligible."""
    before = {
        table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in (
            "field_claim",
            "field_provenance",
            "change_proposal",
            "change_proposal_item",
            "program",
            "university",
            "tuition",
            "admission_requirement",
        )
    }
    eligibility_before = conn.execute(
        text("SELECT id, publication_eligibility::text AS e FROM source ORDER BY id")
    ).all()

    record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )

    after = {
        table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in before
    }
    assert after == before, "accepting a candidate moved something downstream"
    assert (
        conn.execute(
            text("SELECT id, publication_eligibility::text AS e FROM source ORDER BY id")
        ).all()
        == eligibility_before
    )
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM official_domain "
                " WHERE verification_status = 'VERIFIED_OFFICIAL'"
            )
        ).scalar_one()
        == 0
    )


def test_an_accepted_candidate_is_still_refused_by_c27(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Section 30, demonstrated. The reviewer's judgement is real and recorded; it is
    not permission, and C27 is what makes that true rather than a convention."""
    record_decision(
        conn,
        candidate_id=reviewable["candidate"],
        actor_id=reviewable["actor"],
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )
    extraction_id = conn.execute(
        text("SELECT extraction_id FROM field_claim_candidate WHERE id = :c"),
        {"c": reviewable["candidate"]},
    ).scalar_one()

    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="NOT_ELIGIBLE"):
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
                "  proposed_field_status, value_normalized, observed_at) "
                "VALUES (:i, :e, 'program', 'admission.requirement', 'PUBLISHED', "
                "  '{\"a\": 1}'::jsonb, now())"
            ),
            {"i": uuid.uuid4(), "e": extraction_id},
        )
    savepoint.rollback()


# ===========================================================================
# Privileges
# ===========================================================================


def test_the_worker_may_read_review_state_but_not_decide(
    role_engines: dict[str, Any],
) -> None:
    """A review decision is a human action arriving through the API, not a worker one.
    The worker writes `claim_rule_version` because it is the thing that knows."""
    with role_engines["app_worker"].connect() as connection:
        connection.execute(text("SELECT count(*) FROM candidate_review_state"))
        with pytest.raises(Exception, match="permission denied"):
            connection.execute(
                text(
                    "INSERT INTO field_claim_candidate_review "
                    "  (candidate_id, actor_id, decision, reason_code, decided_at) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'ACCEPTED', "
                    "  'CORRECT_AS_EXTRACTED', now())"
                )
            )


def test_the_api_may_decide(role_engines: dict[str, Any]) -> None:
    with role_engines["app_api"].connect() as connection:
        connection.execute(text("SELECT count(*) FROM field_claim_candidate_review"))
        connection.execute(text("SELECT count(*) FROM claim_rule_version"))


def test_the_artifact_store_is_not_consulted_over_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 35. The review pass reads persisted evidence and nothing else.

    `socket.socket` is replaced for the duration, so an HTTP client smuggled in by any
    library would fail here too.
    """
    import socket

    from app.domains.acquisition.storage import FilesystemEvidenceStore
    from app.domains.extraction.runner import DERIVED_PREFIX

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the review pass opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    store = FilesystemEvidenceStore(tmp_path / "artifacts", prefix=DERIVED_PREFIX)
    with pytest.raises(KeyError):
        store.get("0" * 64)


# ===========================================================================
# Step 5C.4: the document artifact is a second axis of "current" (sections 8, 11, 12)
# ===========================================================================


def test_a_re_parsed_page_supersedes_its_candidates_without_touching_any_rule_version(
    conn: Connection, reviewable: dict[str, Any], artifacts: Any
) -> None:
    """Section 8. Bumping every rule to express a parser change would be a lie.

    Four of the six rules were measured to produce byte-identical statements across the
    document 1.0.0 -> 2.0.0 change. Marking their output superseded is still correct --
    it was read from a parse that no longer exists -- but the reason is the document,
    not the rule, and the two must not be conflated (D39).
    """
    candidate = reviewable["candidate"]
    before = conn.execute(
        text(
            "SELECT rule_superseded, document_superseded, is_superseded "
            "  FROM candidate_review_state WHERE candidate_id = :c"
        ),
        {"c": candidate},
    ).one()
    assert (before.rule_superseded, before.document_superseded, before.is_superseded) == (
        False,
        False,
        False,
    )

    # A newer parse of the same page becomes the live artifact.
    successor = conn.execute(
        text("SELECT version FROM document_artifact_version WHERE extractor_name = :n"),
        {"n": "html-document-normaliser"},
    ).scalar_one()
    conn.execute(
        text("UPDATE document_artifact_version SET version = :v WHERE extractor_name = :n"),
        {"v": f"{successor}-successor", "n": "html-document-normaliser"},
    )

    after = conn.execute(
        text(
            "SELECT rule_superseded, document_superseded, is_superseded, extractor_version "
            "  FROM candidate_review_state WHERE candidate_id = :c"
        ),
        {"c": candidate},
    ).one()
    assert after.rule_superseded is False, "no rule changed"
    assert after.document_superseded is True, "the page was re-parsed"
    assert after.is_superseded is True, "and so the candidate is no longer live"


def test_a_decision_on_a_superseded_candidate_is_surfaced_and_never_transferred(
    conn: Connection, reviewable: dict[str, Any]
) -> None:
    """Section 11. Equal normalised values do not make two candidates one observation.

    A reviewer who accepted a claim read from one parse of a page has not looked at the
    claim read from the next one. The decision stays where it was made, and the queue is
    told it needs redoing.
    """
    candidate, actor = reviewable["candidate"], reviewable["actor"]
    record_decision(
        conn,
        candidate_id=candidate,
        actor_id=actor,
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )
    live = conn.execute(
        text(
            "SELECT decision_state, review_superseded FROM candidate_review_state "
            " WHERE candidate_id = :c"
        ),
        {"c": candidate},
    ).one()
    assert live.decision_state == "ACCEPTED"
    assert live.review_superseded is False

    conn.execute(
        text("UPDATE claim_rule_version SET version = version || '-next'"),
    )

    stranded = conn.execute(
        text(
            "SELECT decision_state, is_superseded, review_superseded "
            "  FROM candidate_review_state WHERE candidate_id = :c"
        ),
        {"c": candidate},
    ).one()
    assert stranded.is_superseded is True
    assert stranded.review_superseded is True, "the decision must be shown as needing redoing"
    assert stranded.decision_state == "ACCEPTED", "and it must still say what was decided"

    # Nothing acquired that decision by resembling it.
    elsewhere = conn.execute(
        text(
            "SELECT count(*) FROM candidate_review_state "
            " WHERE decision_state <> 'UNREVIEWED' AND candidate_id <> :c"
        ),
        {"c": candidate},
    ).scalar_one()
    assert elsewhere == 0


def test_the_registry_and_the_code_agree_about_the_live_document_version(
    conn: Connection,
) -> None:
    """Section 12. A frozen copy of something that changes is wrong by construction.

    This is the same assertion `claim_rule_version` carries, for the same reason: the
    previous attempt froze the versions into the view and reported every current
    candidate as superseded, silently, with a total that happened to look right.
    """
    publish_document_versions(conn)
    stored: dict[str, str] = {
        row.extractor_name: row.version
        for row in conn.execute(
            text("SELECT extractor_name, version FROM document_artifact_version")
        )
    }
    assert stored == dict(LIVE_DOCUMENT_VERSIONS)


def test_a_claim_pass_reads_only_the_live_document_artifact(
    conn: Connection, reviewable: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 8. Re-extraction keeps the old artifact, so the pass must choose.

    Without this the rules run over both parses of every page and create two candidates
    for one fact -- and the fingerprint cannot collapse them, because `extraction_id` is
    part of it.
    """
    engine: Any = _SameTransactionEngine(conn)
    source_id = reviewable["source_id"]
    target = next(t for t in targets_for_extraction(conn) if t.source_id == source_id)
    live = conn.execute(
        text("SELECT version FROM document_artifact_version WHERE extractor_name = :n"),
        {"n": "html-document-normaliser"},
    ).scalar_one()

    # A second artifact for the same snapshot, exactly as re-extraction produces.
    extract_one(
        engine,
        target,
        evidence=store,
        artifacts=artifacts,
        report=ExtractionReport(),
        version=f"{live}-successor",
    )
    artifact_count = conn.execute(
        text("SELECT count(*) FROM extraction WHERE snapshot_id = :s"),
        {"s": target.snapshot_id},
    ).scalar_one()
    assert artifact_count == 2, "the previous artifact must be retained"

    targets = targets_for_claims(conn)
    for entry in targets:
        version = conn.execute(
            text("SELECT extractor_version FROM extraction WHERE id = :i"),
            {"i": entry.extraction_id},
        ).scalar_one()
        assert (
            version in CURRENT_DOCUMENT_VERSIONS
        ), f"the claim pass targeted a superseded artifact at version {version}"
