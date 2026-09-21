"""The candidate-claim plane against the database (Step 5C.2 sections 1-5, 27, 35, 38).

The rules have their own fixture tests. These are about what a claim *is* in the
record: that C27 still refuses to let any of this near publication, that a claim can be
walked back to the bytes it came from, that a second pass inserts nothing, and that
nothing canonical moved.

THE TEST THAT MATTERS MOST IS THE FIRST ONE
===========================================
`test_field_claim_still_refuses_a_pilot_source` is why `field_claim_candidate` exists.
All 319 pilot sources are `NOT_ELIGIBLE`, and `field_claim` is gated on eligibility
somebody earned. The two honest options were to weaken C27 or to add a weaker plane;
this asserts the gate is still shut, so the second option cannot quietly become the
first.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.acquisition.lease import claim_next
from app.domains.acquisition.recorder import record_outcome
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.claims.locator import resolve
from app.domains.claims.model import FieldKind
from app.domains.claims.runner import (
    MIN_USABLE_TEXT,
    ClaimReport,
    claims_for_document,
    load_document,
    run_claims,
    targets_for_claims,
)
from app.domains.extraction.runner import (
    DERIVED_PREFIX,
    ExtractionReport,
    extract_one,
    targets_for_extraction,
)
from tests.integration.test_acquisition_evidence import (  # noqa: F401 - fixtures
    _enqueue,
    _outcome,
    pilot,
    registered,
    store,
)
from tests.integration.test_document_extraction_plane import (  # noqa: F401 - fixtures
    _SameTransactionEngine,
    artifacts,
)

# ruff: noqa: F811 -- importing a pytest fixture and then naming it as a test
# parameter is how fixture reuse across modules works.
pytestmark = pytest.mark.integration


#: An admissions page with every shape the extractors are meant to find, written so
#: the visible text clears `MIN_USABLE_TEXT` -- a thin page is a coverage finding, and
#: this fixture is not testing that path.
ADMISSIONS_PAGE = (
    b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    b"<title>Postgraduate admissions</title></head><body><main>"
    b"<h1>Entry requirements</h1>"
    b"<p>Applicants from mainland China must hold a bachelor's degree from a "
    b"recognised institution, with a grade average equivalent to a UK 2:1. We assess "
    b"every application individually and consider the institution attended as well as "
    b"the grades achieved, so the figures below are a guide to what we expect rather "
    b"than a threshold applied mechanically.</p>"
    b"<h2>English language requirements</h2>"
    b"<p>IELTS 7.0 overall with no component below 6.5 is required for entry to all "
    b"taught programmes in this faculty. We also accept TOEFL iBT with a minimum of "
    b"100 overall. Tests must have been taken within two years of the start date.</p>"
    b"<h2>Application deadlines</h2>"
    b"<p>The application deadline is 15 January. Applications received after that "
    b"date are considered only if places remain available in the programme.</p>"
    b"</main></body></html>"
)

#: A page with almost no visible text: the `INSUFFICIENT_STATIC_CONTENT` path.
THIN_PAGE = (
    b"<!DOCTYPE html><html><head><title>Loading</title></head>"
    b'<body><div id="root"></div><script>render()</script></body></html>'
)


def _capture(
    conn: Connection, registered_pilot: dict[str, Any], store_: Any, url: str, payload: bytes
) -> uuid.UUID:
    """Put one real body into the evidence plane for the source serving `url`."""
    source_id: uuid.UUID = conn.execute(
        text("SELECT id FROM source WHERE url = :url"), {"url": url}
    ).scalar_one()
    cycle = uuid.uuid4().hex[:10]
    conn.execute(
        text("INSERT INTO fetch_attempt (id, source_id, cycle_key) VALUES (:id, :source, :cycle)"),
        {"id": uuid.uuid4(), "source": source_id, "cycle": cycle},
    )
    lease = claim_next(conn, cycle_key=cycle, worker="w")
    assert lease is not None and lease.source_id == source_id
    outcome = _outcome(lease.url, content=payload)
    outcome.content_type = "text/html; charset=utf-8"
    record_outcome(conn, lease=lease, outcome=outcome, store=store_)
    return source_id


def _extract(conn: Connection, store_: Any, artifacts_: Any, source_id: uuid.UUID) -> uuid.UUID:
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == source_id)
    extraction_id = extract_one(
        engine, target, evidence=store_, artifacts=artifacts_, report=ExtractionReport()
    )
    assert extraction_id is not None
    return extraction_id


@pytest.fixture
def prepared(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> dict[str, Any]:
    """One extracted admissions page, claimed by three responsibilities."""
    source_id = _capture(conn, registered, store, registered["shared_url"], ADMISSIONS_PAGE)
    extraction_id = _extract(conn, store, artifacts, source_id)
    return {
        "source_id": source_id,
        "extraction_id": extraction_id,
        "store": store,
        "artifacts": artifacts,
        "pilot": registered,
    }


# ===========================================================================
# 1-2. The trust boundary this plane exists because of
# ===========================================================================


def test_field_claim_still_refuses_a_pilot_source(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """C27 is intact, and that is why there is a second table.

    Not an assumption: the insert is attempted and the refusal is read. If C27 were
    ever relaxed, this test fails and the design note in the migration becomes a lie
    that somebody has to come back and fix.
    """
    eligibility = conn.execute(
        text("SELECT publication_eligibility::text FROM source WHERE id = :id"),
        {"id": prepared["source_id"]},
    ).scalar_one()
    assert eligibility == "NOT_ELIGIBLE"

    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="NOT_ELIGIBLE"):
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
                "  proposed_field_status, value_normalized, value_raw_text, observed_at) "
                "VALUES (:id, :e, 'program_offering', 'tuition.amount_min', 'PUBLISHED', "
                "  '{\"amount\": 38000}'::jsonb, '38,000', now())"
            ),
            {"id": uuid.uuid4(), "e": prepared["extraction_id"]},
        )
    savepoint.rollback()


def test_a_candidate_claim_is_accepted_for_the_same_evidence(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """The weaker statement is permitted, because it is weaker.

    "This extractor found this in this exact region" is true of a page nobody has
    verified. "This value may be published" is not.
    """
    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    assert report.claims_created > 0, report.summary()
    stored = conn.execute(
        text("SELECT count(*) FROM field_claim_candidate WHERE extraction_id = :e"),
        {"e": prepared["extraction_id"]},
    ).scalar_one()
    assert stored == report.claims_created


def test_a_candidate_claim_creates_no_publication_row(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Section 35. Nothing downstream moved.

    `field_claim`, `field_provenance` and `change_proposal` are the three tables a
    value passes through on its way to being published, and this step touches none of
    them. Scoped to this test's own evidence, because the development database carries
    committed rows from earlier steps.
    """
    downstream = ("field_claim", "field_provenance", "change_proposal")

    def totals() -> dict[str, int]:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in downstream
        }

    before = totals()
    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    assert report.claims_created > 0, "nothing ran, so nothing was proved"
    assert totals() == before, "a publication-plane table gained a row"


def test_eligibility_is_not_changed_by_a_claim_pass(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Section 35 and the explicit prohibition on touching promotion.

    Finding values on a page is not evidence that the page is official.
    """
    before = conn.execute(
        text(
            "SELECT id, publication_eligibility::text AS e, eligibility_set_by "
            "  FROM source ORDER BY id"
        )
    ).all()
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    after = conn.execute(
        text(
            "SELECT id, publication_eligibility::text AS e, eligibility_set_by "
            "  FROM source ORDER BY id"
        )
    ).all()
    assert before == after


# ===========================================================================
# 4. Routing
# ===========================================================================


def test_an_unclassified_page_authorises_nothing(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 4. Beta's leaflet is `UNCLASSIFIED` (D32) and runs no extractor.

    A thorough page nobody classified is still a page nobody said was about fees.
    """
    source_id = _capture(
        conn, registered, store, "https://beta.example.ac.uk/leaflet", ADMISSIONS_PAGE
    )
    _extract(conn, store, artifacts, source_id)

    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=artifacts,
    )
    assert report.documents_not_authorised == 1
    assert report.claims_created == 0, "an unclassified page produced claims"


def test_the_responsibility_that_authorised_each_claim_is_recorded(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Section 4. "Why did a language claim come off this page" is answerable from the
    row, not from reading the routing table in the code."""
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    rows = conn.execute(
        text(
            "SELECT DISTINCT field_kind, source_responsibility FROM field_claim_candidate "
            " WHERE extraction_id = :e ORDER BY field_kind, source_responsibility"
        ),
        {"e": prepared["extraction_id"]},
    ).all()
    by_kind = {row.field_kind: row.source_responsibility for row in rows}
    assert by_kind[FieldKind.LANGUAGE_OVERALL_SCORE.value] in (
        "ENTRY_REQUIREMENTS",
        "POSTGRADUATE_ADMISSIONS",
    )
    assert by_kind[FieldKind.APPLICATION_DEADLINE.value] in (
        "APPLICATION_DEADLINES",
        "POSTGRADUATE_ADMISSIONS",
    )
    # Every recorded responsibility is one this page was actually claimed for.
    claimed = set(
        conn.execute(
            text(
                "SELECT pcs.source_type FROM pilot_collected_source pcs "
                " WHERE pcs.acquisition_source_id = :s"
            ),
            {"s": prepared["source_id"]},
        ).scalars()
    )
    assert set(by_kind.values()) <= claimed


def test_an_extractor_authorised_twice_still_runs_once(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """The shared page is claimed by three categories, two of which authorise
    `language`. Running it twice would create two identical claims for one sentence."""
    targets = [
        target
        for target in targets_for_claims(conn)
        if target.extraction_id == prepared["extraction_id"]
    ]
    assert len(targets) == 1
    target = targets[0]
    assert len(target.responsibilities) == 3

    document = load_document(prepared["artifacts"], target.document_hash)
    produced = claims_for_document(document, target)

    # Each authorised extractor appears once in the run, attributed to one
    # responsibility -- not once per responsibility that authorised it.
    attribution: dict[str, set[str]] = {name: set() for _, _, _, name, _ in produced}
    for _, _, responsibility, name, _ in produced:
        attribution[name].add(responsibility)
    assert all(len(values) == 1 for values in attribution.values()), attribution

    # And the consequence that matters: every candidate has its own fingerprint, so
    # none is silently discarded by `ON CONFLICT DO NOTHING` on insert.
    fingerprints = {
        candidate.fingerprint(
            extraction_id=str(target.extraction_id), extractor=name, version=version
        )
        for candidate, _, _, name, version in produced
    }
    assert len(fingerprints) == len(produced), (
        f"{len(produced) - len(fingerprints)} of {len(produced)} candidates "
        "collide on their fingerprint and would be dropped on insert"
    )


# ===========================================================================
# 5. Idempotency
# ===========================================================================


def test_a_second_claim_pass_creates_nothing(conn: Connection, prepared: dict[str, Any]) -> None:
    """Section 5. The fingerprint is the identity, so a repeat pass is a no-op."""
    engine: Any = _SameTransactionEngine(conn)
    first = run_claims(engine, artifacts=prepared["artifacts"])
    assert first.claims_created > 0

    second = run_claims(engine, artifacts=prepared["artifacts"])
    assert second.claims_created == 0
    assert second.claims_already_present == first.claims_created

    total = conn.execute(
        text("SELECT count(*) FROM field_claim_candidate WHERE extraction_id = :e"),
        {"e": prepared["extraction_id"]},
    ).scalar_one()
    assert total == first.claims_created


def test_the_unique_fingerprint_is_what_enforces_idempotency(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Non-vacuity: the `ON CONFLICT` above is a convenience, the index is the
    guarantee."""
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    row = conn.execute(
        text("SELECT * FROM field_claim_candidate WHERE extraction_id = :e LIMIT 1"),
        {"e": prepared["extraction_id"]},
    ).one()

    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="uq_field_claim_candidate_claim_fingerprint"):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate (id, extraction_id, "
                "  pilot_collected_source_id, source_responsibility, field_kind, "
                "  value_normalized, value_raw_text, evidence_text, locator, "
                "  extractor_name, extractor_version, confidence_band, confidence_reason, "
                "  claim_fingerprint) "
                "VALUES (:id, :e, :p, :r, :k, '{\"a\": 1}'::jsonb, 'x', 'y', "
                "  '{\"kind\": \"block\"}'::jsonb, 'n', '1', 'LOW', 'why', :f)"
            ),
            {
                "id": uuid.uuid4(),
                "e": row.extraction_id,
                "p": row.pilot_collected_source_id,
                "r": row.source_responsibility,
                "k": row.field_kind,
                "f": row.claim_fingerprint,
            },
        )
    savepoint.rollback()


def test_two_sources_stating_the_same_value_stay_two_claims(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 26. Two official pages agreeing is two pieces of evidence.

    A fingerprint over the value would collapse them, and the corroboration -- the
    most useful thing a second source gives you -- would be lost.
    """
    first = _capture(conn, registered, store, registered["shared_url"], ADMISSIONS_PAGE)
    second = _capture(
        conn, registered, store, "https://beta.example.ac.uk/leaflet", ADMISSIONS_PAGE
    )
    # Beta's page is UNCLASSIFIED, so reclassify this one claim to make it a
    # requirements page. Done in SQL because the point is the claim plane, not the
    # workbook import.
    conn.execute(
        text(
            "UPDATE pilot_collected_source SET source_type = 'ENTRY_REQUIREMENTS' "
            " WHERE acquisition_source_id = :s"
        ),
        {"s": second},
    )
    extraction_one = _extract(conn, store, artifacts, first)
    extraction_two = _extract(conn, store, artifacts, second)
    assert extraction_one != extraction_two

    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=artifacts,
    )
    rows = conn.execute(
        text(
            "SELECT extraction_id, claim_fingerprint, value_normalized "
            "  FROM field_claim_candidate "
            " WHERE field_kind = 'LANGUAGE_OVERALL_SCORE' "
            "   AND extraction_id IN (:a, :b)"
        ),
        {"a": extraction_one, "b": extraction_two},
    ).all()
    assert len({row.extraction_id for row in rows}) == 2, "one source lost its claim"
    assert len({row.claim_fingerprint for row in rows}) == len(rows)
    # Byte-identical pages, so the two sources agree exactly -- and agreement is
    # precisely what must not collapse them. Corroboration is the most useful thing a
    # second official source gives you, and it only exists as two rows.
    by_source: dict[uuid.UUID, set[float]] = {}
    for claim in rows:
        scores = by_source.setdefault(claim.extraction_id, set())
        scores.add(float(claim.value_normalized["score"]))
    assert len(by_source) == 2
    left, right = by_source.values()
    assert left == right, "the two sources disagree, so this proves nothing about merging"


# ===========================================================================
# 3, 34. Lineage
# ===========================================================================


def test_a_candidate_walks_back_to_its_institution_and_its_wording(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Sections 3 and 34. The chain, and the locator landing on the quoted wording.

    `extraction_id` alone would say "somewhere in this page", which is the work the
    extractor was supposed to have done.
    """
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    walked = conn.execute(
        text(
            """
            SELECT c.id, c.field_kind, c.locator, c.evidence_text, c.value_raw_text,
                   c.confidence_band, c.confidence_reason,
                   e.document_hash, sn.content_hash, r.status::text AS run_status,
                   s.url, s.publication_eligibility::text AS eligibility,
                   pcs.source_ref, pcs.source_type, ti.match_key
              FROM field_claim_candidate c
              JOIN extraction e ON e.id = c.extraction_id
              JOIN snapshot sn ON sn.id = e.snapshot_id
              JOIN fetch_run r ON r.id = sn.fetch_run_id
              JOIN source s ON s.id = sn.source_id
              JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
              JOIN target_institution ti ON ti.id = pcs.target_institution_id
             WHERE c.extraction_id = :e
            """
        ),
        {"e": prepared["extraction_id"]},
    ).all()
    assert walked, "no candidate claim to walk"

    document = load_document(
        prepared["artifacts"],
        conn.execute(
            text("SELECT document_hash FROM extraction WHERE id = :e"),
            {"e": prepared["extraction_id"]},
        ).scalar_one(),
    )

    for row in walked:
        assert row.match_key.startswith("acq-")
        assert row.run_status == "OK"
        assert row.eligibility == "NOT_ELIGIBLE", "lineage reached a publishable source"
        assert row.locator and row.locator != {}
        assert row.confidence_band in ("HIGH", "MEDIUM", "LOW")
        assert row.confidence_reason.strip()
        resolved = resolve(document, row.locator)
        assert resolved is not None, f"{row.field_kind} locator does not resolve"
        assert (
            row.value_raw_text in resolved or resolved == row.evidence_text
        ), f"{row.field_kind} locator points somewhere else than its evidence"


# ===========================================================================
# Append-only, and what the schema refuses
# ===========================================================================


def test_a_candidate_claim_cannot_be_updated_or_deleted(
    conn: Connection, prepared: dict[str, Any]
) -> None:
    """Append-only, like the rest of the evidence plane.

    A claim is a record that an extractor said something. Revising it in place would
    erase what it said, which is the one thing the row is for.
    """
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    for statement in (
        "UPDATE field_claim_candidate SET confidence_band = 'HIGH' WHERE extraction_id = :e",
        "DELETE FROM field_claim_candidate WHERE extraction_id = :e",
    ):
        savepoint = conn.begin_nested()
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text(statement), {"e": prepared["extraction_id"]})
        savepoint.rollback()


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("evidence_text", "   ", "evidence_is_not_blank"),
        ("value_raw_text", "", "raw_text_is_not_blank"),
        ("confidence_reason", " ", "confidence_is_explained"),
        ("field_kind", "TUITION_MAYBE", "field_kind_known"),
        ("confidence_band", "0.873421", "confidence_band_known"),
        ("claim_fingerprint", "not-a-hash", "fingerprint_is_sha256_hex"),
    ],
)
def test_the_schema_refuses_an_unreviewable_claim(
    conn: Connection, prepared: dict[str, Any], column: str, value: str, constraint: str
) -> None:
    """Section 33 flags these as suspicious; the schema simply refuses them.

    A claim with blank evidence cannot be reviewed, so it is not a claim. A confidence
    of `0.873421` is the fake precision section 25 forbids, and the CHECK is why it
    cannot be smuggled in as a string.
    """
    params = {
        "id": uuid.uuid4(),
        "e": prepared["extraction_id"],
        "p": conn.execute(
            text("SELECT id FROM pilot_collected_source WHERE acquisition_source_id = :s LIMIT 1"),
            {"s": prepared["source_id"]},
        ).scalar_one(),
        "field_kind": "TUITION",
        "value_raw_text": "£38,000",
        "evidence_text": "The annual fee is £38,000.",
        "confidence_band": "LOW",
        "confidence_reason": "a fixture",
        "claim_fingerprint": "a" * 64,
    }
    params[column] = value

    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match=constraint):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate (id, extraction_id, "
                "  pilot_collected_source_id, source_responsibility, field_kind, "
                "  value_normalized, value_raw_text, evidence_text, locator, "
                "  extractor_name, extractor_version, confidence_band, confidence_reason, "
                "  claim_fingerprint) "
                "VALUES (:id, :e, :p, 'ENTRY_REQUIREMENTS', :field_kind, "
                "  '{\"a\": 1}'::jsonb, :value_raw_text, :evidence_text, "
                "  '{\"kind\": \"block\"}'::jsonb, 'n', '1', :confidence_band, "
                "  :confidence_reason, :claim_fingerprint)"
            ),
            params,
        )
    savepoint.rollback()


def test_an_empty_locator_is_not_provenance(conn: Connection, prepared: dict[str, Any]) -> None:
    """Section 3. `extraction_id` alone is not provenance, and `{}` is that with extra
    steps."""
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="locator_is_present"):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate (id, extraction_id, "
                "  pilot_collected_source_id, source_responsibility, field_kind, "
                "  value_normalized, value_raw_text, evidence_text, locator, "
                "  extractor_name, extractor_version, confidence_band, confidence_reason, "
                "  claim_fingerprint) "
                "VALUES (:id, :e, :p, 'ENTRY_REQUIREMENTS', 'TUITION', "
                "  '{\"a\": 1}'::jsonb, 'x', 'y', '{}'::jsonb, 'n', '1', 'LOW', 'why', :f)"
            ),
            {
                "id": uuid.uuid4(),
                "e": prepared["extraction_id"],
                "p": conn.execute(
                    text(
                        "SELECT id FROM pilot_collected_source "
                        " WHERE acquisition_source_id = :s LIMIT 1"
                    ),
                    {"s": prepared["source_id"]},
                ).scalar_one(),
                "f": "b" * 64,
            },
        )
    savepoint.rollback()


def test_a_null_value_must_say_why(conn: Connection, prepared: dict[str, Any]) -> None:
    """A null value with no reason is a claim that says nothing at all."""
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="unresolved_says_why"):
        conn.execute(
            text(
                "INSERT INTO field_claim_candidate (id, extraction_id, "
                "  pilot_collected_source_id, source_responsibility, field_kind, "
                "  value_normalized, value_raw_text, evidence_text, locator, "
                "  extractor_name, extractor_version, confidence_band, confidence_reason, "
                "  claim_fingerprint) "
                "VALUES (:id, :e, :p, 'ENTRY_REQUIREMENTS', 'DEGREE_LEVEL', "
                "  NULL, 'x', 'y', '{\"kind\": \"block\"}'::jsonb, 'n', '1', "
                "  'LOW', 'why', :f)"
            ),
            {
                "id": uuid.uuid4(),
                "e": prepared["extraction_id"],
                "p": conn.execute(
                    text(
                        "SELECT id FROM pilot_collected_source "
                        " WHERE acquisition_source_id = :s LIMIT 1"
                    ),
                    {"s": prepared["source_id"]},
                ).scalar_one(),
                "f": "c" * 64,
            },
        )
    savepoint.rollback()


# ===========================================================================
# 23. Insufficient static content
# ===========================================================================


def test_a_thin_page_is_a_coverage_finding_not_a_failure(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 23. A page whose content is behind JavaScript produced no claim, and
    the pass says so rather than reporting it as a page where the rules found nothing.

    The distinction is what keeps the five thin pages in the real fleet visible.
    """
    source_id = _capture(conn, registered, store, registered["shared_url"], THIN_PAGE)
    _extract(conn, store, artifacts, source_id)

    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=artifacts,
    )
    assert report.documents_insufficient_text == 1
    assert report.documents_without_claims == 0
    assert report.failures == []
    assert report.claims_created == 0
    # Non-vacuity: the page really was parsed, and the finding is about its *text*.
    extracted = conn.execute(
        text(
            "SELECT e.status::text AS status, e.output FROM extraction e "
            "  JOIN snapshot sn ON sn.id = e.snapshot_id WHERE sn.source_id = :s"
        ),
        {"s": source_id},
    ).one()
    assert extracted.status in ("OK", "PARTIAL"), "the fixture failed to parse"
    assert extracted.output["statistics"]["text_characters"] < MIN_USABLE_TEXT


# ===========================================================================
# Privileges
# ===========================================================================


def test_the_api_role_may_read_but_not_write_candidates(role_engines: dict[str, Any]) -> None:
    """The claim plane is written by workers and read by the API, like the rest of the
    evidence plane."""
    with role_engines["app_api"].connect() as connection:
        connection.execute(text("SELECT count(*) FROM field_claim_candidate"))
        with pytest.raises(Exception, match="permission denied"):
            connection.execute(
                text(
                    "INSERT INTO field_claim_candidate (id, extraction_id, "
                    " pilot_collected_source_id, source_responsibility, field_kind, "
                    " value_raw_text, evidence_text, locator, extractor_name, "
                    " extractor_version, confidence_band, confidence_reason, "
                    " claim_fingerprint) VALUES (gen_random_uuid(), "
                    " gen_random_uuid(), gen_random_uuid(), 'X', 'TUITION', 'x', 'y', "
                    " '{\"kind\": \"block\"}'::jsonb, 'n', '1', 'LOW', 'w', :f)"
                ),
                {"f": "d" * 64},
            )


# ===========================================================================
# 30, 38. Offline, and acquisition untouched
# ===========================================================================


def test_the_claim_pass_makes_no_network_request(
    conn: Connection, prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 30. Stored artifacts only.

    `socket.socket` is replaced rather than mocked at a higher level, so an HTTP
    client smuggled in by any library would fail here too.
    """
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the claim pass opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    assert report.claims_created > 0


def test_the_claim_pass_enqueues_no_fetch(conn: Connection, prepared: dict[str, Any]) -> None:
    """Section 38. No new crawling, and no acquisition state changed."""
    before = conn.execute(
        text("SELECT count(*) AS n, coalesce(max(attempt_no), 0) AS a FROM fetch_attempt")
    ).one()
    run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=prepared["artifacts"],
    )
    after = conn.execute(
        text("SELECT count(*) AS n, coalesce(max(attempt_no), 0) AS a FROM fetch_attempt")
    ).one()
    assert (before.n, before.a) == (after.n, after.a)


def test_a_claim_pass_over_no_documents_is_not_an_error(conn: Connection, artifacts: Any) -> None:
    """An empty pass reports zero rather than raising: running the command twice, or
    before any extraction, is a legitimate thing to do."""
    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=artifacts,
        limit=0,
    )
    assert isinstance(report, ClaimReport)
    assert report.claims_created == 0
    assert report.documents_considered == 0


def test_an_unreadable_artifact_is_a_reported_failure_not_a_crash(
    conn: Connection, prepared: dict[str, Any], tmp_path: Path
) -> None:
    """One missing artifact must not cost the rest of the pass -- the same containment
    lesson as 5B.2's per-attempt handling."""
    empty = FilesystemEvidenceStore(tmp_path / "nothing", prefix=DERIVED_PREFIX)
    report = run_claims(
        _SameTransactionEngine(conn),  # type: ignore[arg-type]
        artifacts=empty,
    )
    assert report.claims_created == 0
    assert report.failures, "a missing artifact was silently ignored"
    assert "artifact unreadable" in report.failures[0]
