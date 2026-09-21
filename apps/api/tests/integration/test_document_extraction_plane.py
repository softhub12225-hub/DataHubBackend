"""Extraction against the database: lineage, idempotency, trust (sections 1, 26, 27).

The parser has its own fixture tests. These are about what extraction does to the
*record*: that a document can be walked back to the institution it came from, that
re-running produces no second row, and that a perfectly parsed page is still a page
nobody may publish.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import ExtractionStatus, FetchStatus
from app.domains.acquisition.lease import claim_next
from app.domains.acquisition.recorder import record_outcome
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.extraction.document import EXTRACTOR_VERSION, HTML_EXTRACTOR
from app.domains.extraction.runner import (
    DERIVED_PREFIX,
    ExtractionReport,
    derived_key_for,
    extract_one,
    extractor_for,
    targets_for_extraction,
)
from tests.integration.test_acquisition_evidence import (  # noqa: F401 - fixtures
    _enqueue_one,
    _outcome,
    pilot,
    registered,
    store,
)

# ruff: noqa: F811 -- importing a pytest fixture and then naming it as a test
# parameter is how fixture reuse across modules works; the "redefinition" is the
# mechanism, not a mistake.
pytestmark = pytest.mark.integration

PAGE = (
    b'<html lang="en"><head><title>Entry requirements</title>'
    b'<script type="application/ld+json">{"@type": "CollegeOrUniversity"}</script>'
    b"</head><body><main><h1>Entry requirements</h1>"
    b"<p>Applicants need a recognised bachelor degree.</p>"
    b"<ul><li>IELTS 7.0</li><li>TOEFL 100</li></ul>"
    b"</main></body></html>"
)


class _SameTransactionEngine:
    """Hands the runner the test's own connection, so everything rolls back.

    Typed as `Any` at the call sites: it implements the two methods the runner
    uses (`begin`, `connect`) and nothing else of `Engine`, which is the point --
    a real engine would open its own transactions and leave rows behind in a
    database the rest of the suite shares.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def begin(self) -> Any:
        return _Savepoint(self._connection)

    def connect(self) -> Any:
        return _Savepoint(self._connection)


class _Savepoint:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._nested: Any = None

    def __enter__(self) -> Connection:
        self._nested = self._connection.begin_nested()
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._nested is None:  # pragma: no cover - defensive
            return
        if exc_type is None:
            self._nested.commit()
        else:
            self._nested.rollback()


@pytest.fixture
def artifacts(tmp_path: Path) -> FilesystemEvidenceStore:
    """A derived-artifact store, namespaced away from raw evidence."""
    return FilesystemEvidenceStore(tmp_path / "artifacts", prefix=DERIVED_PREFIX)


def _capture(
    conn: Connection, registered_pilot: dict[str, Any], store_: Any, *, payload: bytes = PAGE
) -> Any:
    """Put one real body into the evidence plane and return its lease."""
    _enqueue_one(conn, registered_pilot, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    outcome = _outcome(lease.url, content=payload)
    outcome.content_type = "text/html; charset=utf-8"
    record_outcome(conn, lease=lease, outcome=outcome, store=store_)
    return lease


# ===========================================================================
# 4. Only body-bearing evidence
# ===========================================================================


def test_only_sources_with_a_body_are_offered_for_extraction(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Section 4. A blocked page has no bytes, so there is nothing to parse.

    An extraction row for it would assert we read a document that does not exist.
    """
    ours = set(registered["source_ids"])
    blocked = _outcome(
        "https://x",
        content=None,
        status=FetchStatus.BLOCKED,
        http_status=403,
        error_class="HTTP403",
    )
    _enqueue_one(conn, registered, "c0")
    lease = claim_next(conn, cycle_key="c0", worker="w")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=blocked, store=store)

    targets = [t for t in targets_for_extraction(conn) if t.source_id in ours]
    assert targets == [], "a source with no stored body was offered for extraction"

    # Now give a different page a body, and only that one appears.
    with_body = _capture(conn, registered, store)
    targets = [t for t in targets_for_extraction(conn) if t.source_id in ours]
    assert [t.source_id for t in targets] == [with_body.source_id]
    assert targets[0].content_hash


def test_a_304_extracts_the_body_it_confirmed(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Section 4. An unchanged page resolves to the snapshot that carried bytes.

    The alternative -- manufacturing an extraction from a bodiless 304 -- would be an
    extraction of nothing, recorded as though it had read the page.
    """
    lease = _capture(conn, registered, store)
    original = targets_for_extraction(conn)
    body_hash = next(t.content_hash for t in original if t.source_id == lease.source_id)

    from app.domains.acquisition.lease import schedule_retry

    schedule_retry(conn, source_id=lease.source_id, cycle_key="c1", delay_seconds=0, max_attempts=9)
    again = claim_next(conn, cycle_key="c1", worker="w")
    assert again is not None
    unchanged = _outcome(
        again.url, content=None, status=FetchStatus.UNCHANGED, http_status=304, error_class=None
    )
    record_outcome(
        conn, lease=again, outcome=unchanged, store=store, previous_content_hash=body_hash
    )

    targets = [t for t in targets_for_extraction(conn) if t.source_id == lease.source_id]
    assert len(targets) == 1
    assert targets[0].content_hash == body_hash, "the 304 lost the body it confirmed"


# ===========================================================================
# 27. Idempotency
# ===========================================================================


def test_re_running_the_same_version_creates_no_second_row(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 27. The result is a pure function of its inputs.

    So a second row could only be a duplicate. There is no separate run record: an
    extraction *is* the result, and re-deriving it is not an event worth storing.
    """
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)

    first_report = ExtractionReport()
    first = extract_one(
        engine,
        target,
        evidence=store,
        artifacts=artifacts,
        report=first_report,
    )
    assert first is not None
    assert first_report.attempted == 1

    second_report = ExtractionReport()
    second = extract_one(
        engine,
        target,
        evidence=store,
        artifacts=artifacts,
        report=second_report,
    )
    assert second is None, "a second extraction row was created"
    assert second_report.skipped_existing == 1
    assert second_report.attempted == 0

    rows = conn.execute(
        text(
            "SELECT count(*) FROM extraction WHERE snapshot_id = :s "
            " AND extractor_name = :n AND extractor_version = :v"
        ),
        {"s": target.snapshot_id, "n": HTML_EXTRACTOR, "v": EXTRACTOR_VERSION},
    ).scalar_one()
    assert rows == 1


def test_the_unique_index_is_what_enforces_it(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Non-vacuity: the skip above is a convenience, the index is the guarantee.

    Step 5C.2 section 0 made the uniqueness partial, so the name changed with it. The
    guarantee for a *result* did not weaken -- that is what this asserts.
    """
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    with pytest.raises(Exception, match="uq_extraction_result_per_version"):
        conn.execute(
            text(
                "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
                "status) VALUES (:id, :s, :n, :v, 'OK')"
            ),
            {
                "id": uuid.uuid4(),
                "s": target.snapshot_id,
                "n": HTML_EXTRACTOR,
                "v": EXTRACTOR_VERSION,
            },
        )


# ===========================================================================
# Step 5C.2 section 0. Retrying a transient failure
# ===========================================================================


def test_a_failed_extraction_can_be_retried_at_the_same_version(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 0. A transient failure must not need a version bump to retry.

    Before this change, `extractor_version` meant two things at once -- "the logic
    changed" and "the filesystem was briefly unavailable" -- so the only way to retry
    was to lie about the first. The partial index separates them.
    """
    lease = _capture(conn, registered, store)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)

    # A failure exactly as the runner records one.
    failed_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
            "status, error_detail, input_content_hash) "
            "VALUES (:id, :s, :n, :v, 'FAILED', 'OSError: store unavailable', :h)"
        ),
        {
            "id": failed_id,
            "s": target.snapshot_id,
            "n": HTML_EXTRACTOR,
            "v": EXTRACTOR_VERSION,
            "h": target.content_hash,
        },
    )

    # The runner does not consider the page done, and the retry succeeds.
    engine: Any = _SameTransactionEngine(conn)
    report = ExtractionReport()
    retried = extract_one(engine, target, evidence=store, artifacts=artifacts, report=report)
    assert retried is not None, "a FAILED row blocked a retry at the same version"
    assert report.skipped_existing == 0
    assert retried != failed_id


def test_the_failure_stays_as_history_beside_the_success(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 0. Failures accumulate; they are not overwritten.

    The table is append-only, so "retry" cannot mean "revise". Two rows for one
    snapshot at one version is the correct outcome, and the reason a page that needed
    three attempts is distinguishable afterwards from one that needed none.
    """
    lease = _capture(conn, registered, store)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    for attempt in range(2):
        conn.execute(
            text(
                "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
                "status, error_detail) VALUES (:id, :s, :n, :v, 'FAILED', :detail)"
            ),
            {
                "id": uuid.uuid4(),
                "s": target.snapshot_id,
                "n": HTML_EXTRACTOR,
                "v": EXTRACTOR_VERSION,
                "detail": f"OSError: attempt {attempt}",
            },
        )
    engine: Any = _SameTransactionEngine(conn)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    rows = conn.execute(
        text(
            "SELECT status::text AS status, error_detail FROM extraction "
            " WHERE snapshot_id = :s AND extractor_version = :v "
            " ORDER BY status, error_detail"
        ),
        {"s": target.snapshot_id, "v": EXTRACTOR_VERSION},
    ).all()
    assert [row.status for row in rows] == ["FAILED", "FAILED", "OK"]
    assert [row.error_detail for row in rows[:2]] == [
        "OSError: attempt 0",
        "OSError: attempt 1",
    ], "a retry overwrote the failure history"


def test_two_failures_are_allowed_but_two_results_are_not(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 0. The partial index draws the line in exactly one place.

    Without the `WHERE status <> 'FAILED'` clause the first insert here would be
    refused; without the index at all, the second would be permitted. Both halves
    are asserted, so the test cannot pass on a constraint that is simply absent.
    """
    lease = _capture(conn, registered, store)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)

    def insert_extraction(status: str) -> None:
        conn.execute(
            text(
                "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
                "status) VALUES (:id, :s, :n, :v, :status)"
            ),
            {
                "id": uuid.uuid4(),
                "s": target.snapshot_id,
                "n": HTML_EXTRACTOR,
                "v": EXTRACTOR_VERSION,
                "status": status,
            },
        )

    insert_extraction("FAILED")
    insert_extraction("FAILED")  # permitted: failures are history

    insert_extraction("OK")
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="uq_extraction_result_per_version"):
        insert_extraction("PARTIAL")  # refused: that slot is taken by a result
    savepoint.rollback()


def test_a_new_extractor_version_may_extract_the_same_snapshot_again(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 3. Changed logic gets a new row; the old one is retained.

    Comparing two versions over one snapshot is how extractor drift becomes visible
    instead of being mistaken for a source change (D4).

    The second version is **derived** from the live one rather than written out. It was
    the literal `"2.0.0"`, which stopped being a second version the day the normaliser
    itself reached 2.0.0: the re-extraction became a no-op, `extract_one` correctly
    returned None, and the test failed for a reason that had nothing to do with what it
    was checking.
    """
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    successor = f"{EXTRACTOR_VERSION}-successor"

    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())
    newer = extract_one(
        engine,
        target,
        evidence=store,
        artifacts=artifacts,
        report=ExtractionReport(),
        version=successor,
    )
    assert newer is not None

    versions = (
        conn.execute(
            text(
                "SELECT extractor_version FROM extraction WHERE snapshot_id = :s "
                " ORDER BY extractor_version"
            ),
            {"s": target.snapshot_id},
        )
        .scalars()
        .all()
    )
    assert versions == sorted([EXTRACTOR_VERSION, successor])


def test_an_old_extraction_cannot_be_mutated(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 3. Never revised in place -- the table is append-only."""
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    with pytest.raises(Exception, match="append-only"):
        conn.execute(
            text("UPDATE extraction SET status = 'FAILED' WHERE snapshot_id = :s"),
            {"s": target.snapshot_id},
        )


# ===========================================================================
# 15. Artifact storage boundary
# ===========================================================================


def test_the_payload_lives_in_the_store_and_the_row_points_at_it(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 15. Metadata in PostgreSQL, payload in the object store."""
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    row = conn.execute(
        text(
            "SELECT status::text AS status, document_hash, document_storage_key, "
            "       document_byte_size, input_content_hash, output "
            "  FROM extraction WHERE snapshot_id = :s"
        ),
        {"s": target.snapshot_id},
    ).one()

    assert row.status == ExtractionStatus.OK.value
    assert row.input_content_hash == target.content_hash
    assert row.document_hash and len(row.document_hash) == 64
    assert row.document_byte_size and row.document_byte_size > 0
    # The payload really is retrievable under that hash, and integrity is checkable.
    payload = artifacts.get(row.document_hash)
    assert len(payload) == row.document_byte_size
    import hashlib

    assert hashlib.sha256(payload).hexdigest() == row.document_hash

    # `output` holds a summary, never the document.
    assert row.output is not None
    assert "statistics" in row.output
    assert "blocks" not in row.output, "the payload leaked into PostgreSQL"


def test_derived_artifacts_cannot_collide_with_raw_evidence(
    conn: Connection, registered: dict[str, Any], store: Any, tmp_path: Path
) -> None:
    """Section 15. Raw evidence is never overwritten, structurally.

    Both stores share a root here -- the worst case -- and the prefixes still keep
    them apart. Without the prefix this test would have one file where it needs two.
    """
    shared = tmp_path / "shared"
    raw = FilesystemEvidenceStore(shared)
    derived = FilesystemEvidenceStore(shared, prefix=DERIVED_PREFIX)

    payload = b"identical bytes in both planes"
    raw_object = raw.put(payload)
    derived_object = derived.put(payload)

    assert raw_object.content_hash == derived_object.content_hash
    assert raw_object.storage_key != derived_object.storage_key
    assert raw_object.storage_key.startswith("evidence/")
    assert derived_object.storage_key.startswith(f"{DERIVED_PREFIX}/")
    assert derived_key_for(raw_object.content_hash) == derived_object.storage_key
    assert len([path for path in shared.rglob("*") if path.is_file()]) == 2


# ===========================================================================
# 26. Lineage
# ===========================================================================


def test_an_extraction_walks_back_to_its_institution(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 26. extraction -> snapshot -> fetch_run -> source -> claims -> institution.

    The invariant Step 5C.2 depends on: no extraction may lose evidence lineage, and
    `field_claim.extraction_id` is the hook that makes a published fact traceable to
    the bytes it came from.
    """
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extraction_id = extract_one(
        engine,
        target,
        evidence=store,
        artifacts=artifacts,
        report=ExtractionReport(),
    )
    assert extraction_id is not None

    walked = conn.execute(
        text(
            """
            SELECT e.id            AS extraction_id,
                   e.document_hash,
                   sn.id           AS snapshot_id,
                   sn.content_hash,
                   r.id            AS fetch_run_id,
                   r.status::text  AS run_status,
                   s.id            AS source_id,
                   s.url,
                   s.publication_eligibility::text AS publication,
                   pcs.source_ref,
                   pcs.source_type AS claimed_category,
                   ti.id           AS institution_id,
                   ti.match_key
              FROM extraction e
              JOIN snapshot sn        ON sn.id = e.snapshot_id
              JOIN fetch_run r        ON r.id = sn.fetch_run_id
              JOIN source s           ON s.id = sn.source_id
              JOIN content_blob b     ON b.content_hash = sn.content_hash
              JOIN pilot_collected_source pcs ON pcs.acquisition_source_id = s.id
              JOIN target_institution ti ON ti.id = pcs.target_institution_id
             WHERE e.id = :id
            """
        ),
        {"id": extraction_id},
    ).all()

    assert walked, "the lineage chain is broken somewhere"
    first = walked[0]
    assert first.snapshot_id == target.snapshot_id
    assert first.content_hash == target.content_hash
    assert first.source_id == lease.source_id
    assert first.run_status == "OK"
    assert first.match_key
    assert first.source_ref
    # Every claim riding on that page is reachable from the one extraction.
    assert {row.claimed_category for row in walked}


# ===========================================================================
# 1. Extraction confers no trust
# ===========================================================================


def test_a_perfect_extraction_changes_no_trust_state(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 1. Successful parsing earns nothing.

    A document extracted from a `NOT_ELIGIBLE` source is a well-parsed document from a
    source nobody may publish (C27).
    """
    before = conn.execute(
        text(
            "SELECT count(*) FILTER (WHERE publication_eligibility <> 'NOT_ELIGIBLE') AS eligible, "
            "       count(*) FILTER (WHERE fetch_eligibility <> 'FETCHABLE') AS not_fetchable "
            "  FROM source"
        )
    ).one()

    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    after = conn.execute(
        text(
            "SELECT count(*) FILTER (WHERE publication_eligibility <> 'NOT_ELIGIBLE') AS eligible, "
            "       count(*) FILTER (WHERE fetch_eligibility <> 'FETCHABLE') AS not_fetchable "
            "  FROM source"
        )
    ).one()
    assert (after.eligible, after.not_fetchable) == (before.eligible, before.not_fetchable)

    source = conn.execute(
        text("SELECT publication_eligibility::text AS pub FROM source WHERE id = :s"),
        {"s": lease.source_id},
    ).scalar_one()
    assert source == "NOT_ELIGIBLE"


def test_extraction_creates_no_business_fact(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 19. The hard boundary of this step.

    Asserted over the tables rather than by reading the code, because the code is what
    would change.
    """
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    for table in (
        "field_claim",
        "change_proposal",
        "field_provenance",
        "program",
        "program_offering",
        "admission_requirement",
        "language_requirement",
        "tuition",
        "application_deadline",
        "entity_version",
        "change_event",
    ):
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"extraction created a row in {table}"

    # The extraction itself exists, so the test is not vacuous.
    assert conn.execute(text("SELECT count(*) FROM extraction")).scalar_one() >= 1


def test_official_domain_and_mapping_promotion_are_untouched(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 1. Named explicitly because they are the trust levers."""
    lease = _capture(conn, registered, store)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=ExtractionReport())

    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM official_domain "
                " WHERE verification_status = 'VERIFIED_OFFICIAL'"
            )
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL")
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM pilot_collected_source "
                " WHERE verification_state <> 'PENDING'"
            )
        ).scalar_one()
        == 0
    )


# ===========================================================================
# 17. Status
# ===========================================================================


def test_a_malformed_structured_block_records_partial_not_failed(
    conn: Connection, registered: dict[str, Any], store: Any, artifacts: Any
) -> None:
    """Section 17's worked example, end to end through the database."""
    page = (
        b"<html><head><title>Fees</title>"
        b'<script type="application/ld+json">{"@type": "Course",}</script>'
        b"</head><body><p>Real content that a parser can use.</p></body></html>"
    )
    lease = _capture(conn, registered, store, payload=page)
    engine: Any = _SameTransactionEngine(conn)
    target = next(t for t in targets_for_extraction(conn) if t.source_id == lease.source_id)
    report = ExtractionReport()
    extract_one(engine, target, evidence=store, artifacts=artifacts, report=report)

    row = conn.execute(
        text(
            "SELECT status::text AS status, warnings, error_detail, document_hash "
            "  FROM extraction WHERE snapshot_id = :s"
        ),
        {"s": target.snapshot_id},
    ).one()
    assert row.status == ExtractionStatus.PARTIAL.value
    assert row.error_detail is None, "a PARTIAL must not claim to be an error"
    assert any("malformed" in warning for warning in row.warnings["warnings"])
    # And the document was still stored: the text survived the bad block.
    assert row.document_hash
    assert report.partial == 1


def test_a_pdf_is_routed_by_its_magic_bytes_not_its_label() -> None:
    """A server that labels a PDF `text/html` has mislabelled it; bytes are evidence."""
    from app.domains.extraction.document import PDF_EXTRACTOR

    assert extractor_for("text/html", b"%PDF-1.7\n...") == PDF_EXTRACTOR
    assert extractor_for("application/pdf", b"<html>") == PDF_EXTRACTOR
    assert extractor_for("text/html", b"<html>") == HTML_EXTRACTOR
