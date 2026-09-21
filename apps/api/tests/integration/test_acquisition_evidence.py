"""Lease fencing, evidence capture, and the publication boundary (Step 5B).

The two claims this module has to earn:

1. **A pending candidate can be safely acquired.** Fetching is a technical question,
   and coupling it to verification made the review pass impossible to do well.
2. **Nothing acquired from a pending candidate can be published.** The boundary is C27,
   unchanged, and it is asserted here from the acquisition side rather than argued
   from the code.

The concurrency tests are the other half. They exist because the failure they describe
-- a stalled worker finalising after it lost its lease -- is silent, produces a
plausible-looking record, and cannot be found afterwards.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.core.clock import utcnow
from app.db.enums import FetchStatus, PublicationEligibility
from app.domains.acquisition.fetcher import FetchOutcome
from app.domains.acquisition.lease import (
    Lease,
    LeaseLostError,
    backoff_seconds,
    claim_next,
    enqueue_cycle,
    heartbeat,
    schedule_retry,
    sweep_expired,
)
from app.domains.acquisition.recorder import conditional_headers_for, record_outcome
from app.domains.acquisition.registration import (
    assert_nothing_became_publishable,
    register_acquisition_targets,
)
from app.domains.acquisition.reporting import acquisition_summary, verification_assistance
from app.domains.acquisition.storage import (
    FilesystemEvidenceStore,
    find_missing_objects,
    find_orphans,
    storage_key_for,
)
from tests.integration.conftest import expect_violation

pytestmark = pytest.mark.integration

HTML = b"<html><head><title>Fees</title></head><body>one</body></html>"
HTML_CHANGED = b"<html><head><title>Fees</title></head><body>two</body></html>"


# ---------------------------------------------------------------------------
# A small pilot, built by hand so these tests do not need the client's files
# ---------------------------------------------------------------------------


@pytest.fixture
def pilot(conn: Connection) -> dict[str, Any]:
    """Two institutions; one page claimed three times, one page claimed once.

    The three-claim page is the shape that matters: 4 claims over 2 URLs, so "fetched
    once per page, not once per claim" is testable.
    """
    ids: dict[str, Any] = {}
    list_id, submission_id = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, file_sha256, "
            "file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:id, 'Acq', 'v1', 'a.xlsx', :sha, 1, 's', 2)"
        ),
        {"id": list_id, "sha": "c" * 64},
    )
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, file_byte_size, "
            "template_version, submission_kind, defines_pilot_scope, selected_university_count) "
            "VALUES (:id, :sha, 'sources.xlsx', 1, 'v1', 'OFFICIAL_SOURCE_LIST', true, 2)"
        ),
        {"id": submission_id, "sha": "d" * 64},
    )
    ids["submission_id"] = submission_id

    for index, (key, host) in enumerate(
        [("alpha", "alpha.example.ac.uk"), ("beta", "beta.example.ac.uk")]
    ):
        target = uuid.uuid4()
        ids[f"target_{key}"] = target
        conn.execute(
            text(
                "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
                "latest_list_id, destination_code, pilot_wave) "
                "VALUES (:id, :key, :list, :list, 'GB', 1)"
            ),
            {"id": target, "key": f"acq-{key}", "list": list_id},
        )
        conn.execute(
            text(
                "INSERT INTO pilot_selected_university (submission_id, target_institution_id, "
                "sheet_row_no, is_selected, official_homepage) "
                "VALUES (:s, :t, :row, true, :home)"
            ),
            {"s": submission_id, "t": target, "row": index + 2, "home": f"https://{host}/"},
        )

    # Institution alpha: one URL claimed by three categories, plus a second URL.
    shared = "https://alpha.example.ac.uk/apply"
    ids["shared_url"] = shared
    for ref, category, duplicate in [
        ("S0001", "POSTGRADUATE_ADMISSIONS", None),
        ("S0002", "APPLICATION_DEADLINES", "S0001"),
        ("S0003", "ENTRY_REQUIREMENTS", "S0001"),
    ]:
        _claim(conn, submission_id, ids["target_alpha"], ref, category, shared, duplicate)
    _claim(
        conn,
        submission_id,
        ids["target_alpha"],
        "S0004",
        "TUITION_FEES",
        "https://alpha.example.ac.uk/fees",
        None,
    )
    # Institution beta: one unclassified page, which must still be fetchable.
    _claim(
        conn,
        submission_id,
        ids["target_beta"],
        "S0005",
        "UNCLASSIFIED",
        "https://beta.example.ac.uk/leaflet",
        None,
    )
    return ids


def _claim(
    conn: Connection,
    submission_id: uuid.UUID,
    target: uuid.UUID,
    ref: str,
    category: str,
    url: str,
    duplicate_of: str | None,
) -> None:
    import hashlib

    conn.execute(
        text(
            "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
            "target_institution_id, sheet_row_no, source_type, official_url, normalized_url, "
            "url_sha256, host, duplicate_of_source_ref, workbook_column) "
            "VALUES (:id, :sub, :ref, :t, 2, :cat, :url, :url, :hash, :host, :dup, :col)"
        ),
        {
            "id": uuid.uuid4(),
            "sub": submission_id,
            "ref": ref,
            "t": target,
            "cat": category,
            "url": url,
            "hash": hashlib.sha256(url.encode()).hexdigest(),
            "host": url.split("/")[2],
            "dup": duplicate_of,
            "col": f"{category} column",
        },
    )


@pytest.fixture
def registered(conn: Connection, pilot: dict[str, Any]) -> dict[str, Any]:
    register_acquisition_targets(conn, submission_id=pilot["submission_id"])
    pilot["source_ids"] = _source_ids(conn, pilot)
    return pilot


def _source_ids(conn: Connection, pilot: dict[str, Any]) -> list[uuid.UUID]:
    """This fixture's own acquisition targets.

    Every query below is scoped to these. The development database carries the
    committed Step 5A import, and a test that assumed an empty `source` table would
    pass only until someone ran the importer.
    """
    return list(
        conn.execute(
            text(
                "SELECT DISTINCT acquisition_source_id FROM pilot_collected_source "
                " WHERE submission_id = :s AND acquisition_source_id IS NOT NULL"
            ),
            {"s": pilot["submission_id"]},
        ).scalars()
    )


def _enqueue(conn: Connection, pilot: dict[str, Any], cycle_key: str) -> Any:
    return enqueue_cycle(conn, cycle_key=cycle_key, source_ids=pilot["source_ids"])


def _enqueue_one(conn: Connection, pilot: dict[str, Any], cycle_key: str) -> Any:
    """Queue exactly one page.

    `claim_next` orders by `scheduled_for`, so with three pages queued a retry lands
    behind the other two and the next claim is a different source. Tests about one
    page's *sequence* therefore queue one page; that is the behaviour under test, not
    a limitation of it.
    """
    return enqueue_cycle(conn, cycle_key=cycle_key, source_ids=[pilot["source_ids"][0]])


def _claim_ours(conn: Connection, pilot: dict[str, Any], worker: str, cycle_key: str = "c1") -> Any:
    """Claim from this fixture's sources, within one cycle.

    `cycle_key` became a required, filtered argument in Step 5B.2: a worker asked for
    cycle K now claims only K's attempts. `"c1"` is the default because almost every
    test here queues exactly that; the ones that do not pass their own.
    """
    lease = claim_next(conn, cycle_key=cycle_key, worker=worker)
    if lease is not None:
        assert lease.source_id in pilot["source_ids"], "claimed an attempt we did not queue"
    return lease


@pytest.fixture
def store(tmp_path: Path) -> FilesystemEvidenceStore:
    return FilesystemEvidenceStore(tmp_path / "evidence")


def _outcome(
    url: str, *, content: bytes | None = HTML, status: FetchStatus = FetchStatus.OK, **kwargs: Any
) -> FetchOutcome:
    import hashlib

    # `started_at` is set explicitly: the dataclass default_factory runs *after* the
    # `finished_at` argument is evaluated, so the default would be a microsecond later
    # than the finish and violate `finish_after_start`.
    started = kwargs.pop("started_at", utcnow())
    outcome = FetchOutcome(
        started_at=started,
        status=status,
        requested_url=url,
        effective_url=kwargs.pop("effective_url", url),
        http_status=kwargs.pop("http_status", 200 if status is FetchStatus.OK else None),
        content=content,
        content_hash=hashlib.sha256(content).hexdigest() if content else None,
        byte_size=len(content) if content else None,
        content_type=kwargs.pop("content_type", "text/html"),
        finished_at=kwargs.pop("finished_at", started),
        duration_ms=12,
        **kwargs,
    )
    return outcome


# ===========================================================================
# 1-4. The trust boundary, and the physical-page split
# ===========================================================================


def test_a_pending_candidate_is_registered_fetchable_and_not_publishable(
    conn: Connection, pilot: dict[str, Any]
) -> None:
    """Requirements 1 and 2. The correction this step exists to make.

    Nothing has been verified: zero official domains, zero source mappings, every
    candidate `PENDING`. The pages are still fetchable, and nothing they produce may
    be published.
    """
    assert conn.execute(text("SELECT count(*) FROM official_domain")).scalar_one() == 0
    report = register_acquisition_targets(conn, submission_id=pilot["submission_id"])

    assert report.physical_pages == 3
    assert report.sources_created == 3
    assert report.claims_linked == 5
    assert report.fetchable == 3

    rows = conn.execute(text("SELECT fetch_eligibility, publication_eligibility FROM source")).all()
    assert {row.fetch_eligibility for row in rows} == {"FETCHABLE"}
    assert {row.publication_eligibility for row in rows} == {
        PublicationEligibility.NOT_ELIGIBLE.value
    }
    # And no mapping was invented to make that possible.
    assert conn.execute(text("SELECT count(*) FROM source_mapping")).scalar_one() == 0
    assert_nothing_became_publishable(conn)


def test_an_unclassified_page_is_fetchable(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirement 25. Fetching does not require knowing what a page is."""
    row = conn.execute(
        text(
            "SELECT s.fetch_eligibility, s.source_type, s.publication_eligibility "
            "  FROM source s JOIN pilot_collected_source p ON p.acquisition_source_id = s.id "
            " WHERE p.source_ref = 'S0005'"
        )
    ).one()
    assert row.fetch_eligibility == "FETCHABLE"
    assert row.source_type == "unclassified"
    assert row.publication_eligibility == PublicationEligibility.NOT_ELIGIBLE.value


def test_one_url_with_three_claims_is_one_acquisition_target(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 3. Fetched once; claimed three times; both directions queryable."""
    row = conn.execute(
        text(
            "SELECT claim_count, categories, source_refs FROM acquisition_target "
            " WHERE url = :url"
        ),
        {"url": registered["shared_url"]},
    ).one()
    assert row.claim_count == 3
    assert set(row.categories) == {
        "APPLICATION_DEADLINES",
        "ENTRY_REQUIREMENTS",
        "POSTGRADUATE_ADMISSIONS",
    }
    assert set(row.source_refs) == {"S0001", "S0002", "S0003"}

    enqueued = _enqueue(conn, registered, "2027-01-15T06")
    assert enqueued.physical_pages_selected == 3
    assert enqueued.attempts_created == 3
    assert enqueued.duplicate_responsibilities_skipped == 2
    # Three pages, three attempts -- not five.
    assert _count(conn, registered, "fetch_attempt") == 3


def test_all_responsibility_lineage_survives_registration(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 4. From a source back to the workbook, and to the institution."""
    rows = conn.execute(
        text(
            "SELECT p.source_ref, p.source_type, p.workbook_column, p.submission_id, "
            "       p.target_institution_id, p.official_url, p.normalized_url "
            "  FROM pilot_collected_source p "
            "  JOIN source s ON s.id = p.acquisition_source_id "
            " WHERE s.url = :url ORDER BY p.source_ref"
        ),
        {"url": registered["shared_url"]},
    ).all()
    assert [row.source_ref for row in rows] == ["S0001", "S0002", "S0003"]
    assert all(row.submission_id == registered["submission_id"] for row in rows)
    assert all(row.target_institution_id == registered["target_alpha"] for row in rows)
    assert all(row.workbook_column for row in rows)


def test_enqueueing_twice_in_one_cycle_queues_nothing_new(
    conn: Connection, registered: dict[str, Any]
) -> None:
    _enqueue(conn, registered, "2027-01-15T06")
    second = _enqueue(conn, registered, "2027-01-15T06")
    assert second.attempts_created == 0
    assert second.already_queued == 3


# ===========================================================================
# 5-9. Lease fencing and the lifecycle
# ===========================================================================


def test_two_workers_cannot_hold_the_same_attempt(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 5. `SKIP LOCKED` in one statement, so the second worker moves on."""
    _enqueue(conn, registered, "c1")
    first = _claim_ours(conn, registered, "worker-a")
    second = _claim_ours(conn, registered, "worker-b")
    assert first is not None and second is not None
    assert first.attempt_id != second.attempt_id
    assert first.lease_token != second.lease_token

    claimed = _count(conn, registered, "fetch_attempt", "state = 'RUNNING'")
    assert claimed == 2


def test_every_claim_mints_a_new_token(conn: Connection, registered: dict[str, Any]) -> None:
    _enqueue_one(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None
    assert lease.lease_generation == 1

    # Expire it and let the sweeper take it, then requeue and reclaim.
    conn.execute(text("UPDATE fetch_attempt SET lease_expires_at = now() - interval '1 hour'"))
    sweep_expired(conn)
    schedule_retry(conn, source_id=lease.source_id, cycle_key="c1", delay_seconds=0)
    again = _claim_ours(conn, registered, "worker-b")
    assert again is not None
    assert again.lease_token != lease.lease_token


def test_a_stale_worker_cannot_heartbeat(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirement 6. The fence, at its cheapest point."""
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None
    heartbeat(conn, lease)  # still ours: fine

    stale = Lease(
        attempt_id=lease.attempt_id,
        source_id=lease.source_id,
        url=lease.url,
        cycle_key=lease.cycle_key,
        attempt_no=lease.attempt_no,
        lease_token=uuid.uuid4(),  # a token we were never given
        lease_generation=lease.lease_generation,
        expires_at=lease.expires_at,
        worker="worker-a-zombie",
    )
    with pytest.raises(LeaseLostError):
        heartbeat(conn, stale)


def test_a_stale_worker_cannot_finalise_or_write_anything(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 7, and the reason the fence is the transaction's first write.

    The stalled worker has real bytes and a real outcome. It must produce no run, no
    snapshot and no blob row -- and the assertion covers all three, because writing the
    run first and checking the fence afterwards would pass a test that only looked at
    the snapshot.
    """
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None

    # The sweeper takes it while worker-a is stalled.
    conn.execute(text("UPDATE fetch_attempt SET lease_expires_at = now() - interval '1 hour'"))
    sweep_expired(conn)

    with pytest.raises(LeaseLostError):
        record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    runs = (
        conn.execute(
            text("SELECT status FROM fetch_run WHERE attempt_id = :a"), {"a": lease.attempt_id}
        )
        .scalars()
        .all()
    )
    assert runs == [FetchStatus.ABANDONED.value], "the stale worker wrote a run"
    assert _count(conn, registered, "snapshot") == 0
    assert conn.execute(text("SELECT count(*) FROM content_blob")).scalar_one() == 0


def test_the_sweeper_and_a_finaliser_produce_exactly_one_terminal_run(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 8. Whoever loses the fenced update never reaches the insert."""
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None

    record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)
    # The sweeper arrives late. The attempt is FINALIZED, so it wins nothing.
    conn.execute(text("UPDATE fetch_attempt SET lease_expires_at = now() - interval '1 hour'"))
    assert sweep_expired(conn) == []

    runs = conn.execute(
        text("SELECT count(*) FROM fetch_run WHERE attempt_id = :a"), {"a": lease.attempt_id}
    ).scalar_one()
    assert runs == 1


def test_a_second_run_for_one_attempt_is_refused_by_the_database(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """The independent guarantee, tested independently of the fence."""
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    with expect_violation(conn, "uq_fetch_run_attempt_id|duplicate key"):
        conn.execute(
            text(
                "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
                "status, fetcher) VALUES (:id, :a, :s, 1, now(), 'OK', 'STATIC')"
            ),
            {"id": uuid.uuid4(), "a": lease.attempt_id, "s": lease.source_id},
        )


def test_a_failure_and_a_later_success_stay_separate_runs(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 9. A retry is a new attempt; the failed try keeps its own record."""
    _enqueue_one(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "worker-a")
    assert lease is not None
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            content=None,
            status=FetchStatus.HTTP_ERROR,
            http_status=500,
            error_class="HTTP500",
        ),
        store=store,
    )
    retry_id = schedule_retry(conn, source_id=lease.source_id, cycle_key="c1", delay_seconds=0)
    assert retry_id is not None

    second = _claim_ours(conn, registered, "worker-a")
    assert second is not None and second.attempt_no == 2
    record_outcome(conn, lease=second, outcome=_outcome(second.url), store=store)

    runs = conn.execute(
        text(
            "SELECT attempt_no, status FROM fetch_run WHERE source_id = :s " " ORDER BY attempt_no"
        ),
        {"s": lease.source_id},
    ).all()
    assert [(r.attempt_no, r.status) for r in runs] == [(1, "HTTP_ERROR"), (2, "OK")]


def test_a_blocked_source_is_not_retried_indefinitely(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 21. Three tries per cycle, then it is a worklist item."""
    _enqueue(conn, registered, "c1")
    source_id = registered["source_ids"][0]
    assert schedule_retry(conn, source_id=source_id, cycle_key="c1", delay_seconds=0)
    assert schedule_retry(conn, source_id=source_id, cycle_key="c1", delay_seconds=0)
    assert schedule_retry(conn, source_id=source_id, cycle_key="c1", delay_seconds=0) is None


def test_retry_after_overrides_our_backoff() -> None:
    """The server's number wins. Ours is only used when it did not say."""
    assert backoff_seconds(1, retry_after=120.0) == 120.0
    assert backoff_seconds(1) == 15.0
    assert backoff_seconds(4) == 120.0
    assert backoff_seconds(9) == 300.0  # capped
    assert backoff_seconds(1, retry_after=99999.0) == 3600.0  # also capped


# ===========================================================================
# 10-12, 22-23. Content, blobs and storage ordering
# ===========================================================================


def test_the_same_bytes_twice_make_two_snapshots_and_one_blob(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 10. Dedup saves storage; it must not erase the record of looking."""
    _enqueue_one(conn, registered, "c1")
    first = _claim_ours(conn, registered, "w")
    assert first is not None
    one = record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)
    assert one.blob_created is True

    schedule_retry(conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0)
    second = _claim_ours(conn, registered, "w")
    assert second is not None and second.source_id == first.source_id
    two = record_outcome(conn, lease=second, outcome=_outcome(second.url), store=store)
    assert two.blob_created is False
    assert two.content_hash == one.content_hash

    counts = conn.execute(
        text(
            "SELECT count(*) AS snaps, count(DISTINCT content_hash) AS blobs "
            "  FROM snapshot WHERE source_id = :s"
        ),
        {"s": first.source_id},
    ).one()
    assert (counts.snaps, counts.blobs) == (2, 1)


def test_different_bytes_make_a_second_blob(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 11."""
    _enqueue_one(conn, registered, "c1")
    first = _claim_ours(conn, registered, "w")
    assert first is not None
    record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)
    schedule_retry(conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0)
    second = _claim_ours(conn, registered, "w")
    assert second is not None
    result = record_outcome(
        conn, lease=second, outcome=_outcome(second.url, content=HTML_CHANGED), store=store
    )
    assert result.blob_created is True
    assert (
        conn.execute(
            text("SELECT count(DISTINCT content_hash) FROM snapshot WHERE source_id = :s"),
            {"s": first.source_id},
        ).scalar_one()
        == 2
    )


def test_a_304_records_an_unchanged_run_with_no_snapshot(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 12. Immutable history, and no fabricated body."""
    _enqueue_one(conn, registered, "c1")
    first = _claim_ours(conn, registered, "w")
    assert first is not None
    initial = record_outcome(
        conn,
        lease=first,
        outcome=_outcome(first.url, etag='"v1"'),
        store=store,
    )

    schedule_retry(conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0)
    second = _claim_ours(conn, registered, "w")
    assert second is not None
    conditional = conditional_headers_for(conn, second.source_id)
    assert conditional.etag == '"v1"'

    recorded = record_outcome(
        conn,
        lease=second,
        outcome=_outcome(
            second.url,
            content=None,
            status=FetchStatus.UNCHANGED,
            http_status=304,
            conditional_request_sent=True,
        ),
        store=store,
        previous_content_hash=conditional.content_hash,
    )
    assert recorded.snapshot_id is None
    assert recorded.content_hash is None

    run = conn.execute(
        text(
            "SELECT status, http_status, unchanged_content_hash, conditional_request_sent, "
            "       bytes_downloaded FROM fetch_run WHERE attempt_id = :a"
        ),
        {"a": second.attempt_id},
    ).one()
    assert run.status == "UNCHANGED"
    assert run.http_status == 304
    assert run.unchanged_content_hash == initial.content_hash
    assert run.conditional_request_sent is True
    assert run.bytes_downloaded is None
    # Still one snapshot: the 304 did not add one, and did not remove one.
    assert (
        conn.execute(
            text("SELECT count(*) FROM snapshot WHERE source_id = :s"), {"s": first.source_id}
        ).scalar_one()
        == 1
    )


def test_only_a_304_may_claim_unchanged_content(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """A failed fetch asserting "nothing changed" would extend evidence nobody checked."""
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "w")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)
    content_hash = conn.execute(
        text("SELECT content_hash FROM snapshot WHERE source_id = :s"),
        {"s": lease.source_id},
    ).scalar_one()

    with expect_violation(conn, "only_a_304_confirms_unchanged_content"):
        conn.execute(
            text(
                "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
                "status, http_status, fetcher, error_class, unchanged_content_hash) "
                "VALUES (:id, :a, :s, 9, now(), 'HTTP_ERROR', 500, 'STATIC', 'HTTP500', :h)"
            ),
            {
                "id": uuid.uuid4(),
                "a": _fresh_attempt(conn, lease.source_id),
                "s": lease.source_id,
                "h": content_hash,
            },
        )


def test_the_storage_key_is_deterministic_and_content_derived(
    store: FilesystemEvidenceStore,
) -> None:
    """Requirement 22."""
    import hashlib

    digest = hashlib.sha256(HTML).hexdigest()
    assert storage_key_for(digest) == f"evidence/{digest[:2]}/{digest[2:4]}/{digest}"
    first = store.put(HTML)
    second = store.put(HTML)
    assert first.storage_key == second.storage_key == storage_key_for(digest)
    assert first.newly_written is True
    assert second.newly_written is False
    assert store.get(digest) == HTML


def test_a_failed_transaction_leaves_an_orphan_not_a_dangling_reference(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 23, and the reason the ordering is object-first.

    The stale worker stored bytes and then lost the fence. The object is an orphan --
    recoverable, reconcilable, harmless. What must never exist is the reverse: a row
    referencing an object that was never written.
    """
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "w")
    assert lease is not None
    conn.execute(text("UPDATE fetch_attempt SET lease_expires_at = now() - interval '1 hour'"))
    sweep_expired(conn)

    with pytest.raises(LeaseLostError):
        record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    import hashlib

    digest = hashlib.sha256(HTML).hexdigest()
    assert store.exists(digest), "the object was stored before the transaction"
    known = set(conn.execute(text("SELECT content_hash FROM content_blob")).scalars().all())
    assert digest not in known, "a dangling database reference was created"

    # Young orphans are left alone; the object was written seconds ago.
    assert find_orphans(store, known) == []
    assert find_orphans(store, known, min_age=timedelta(seconds=0)) == [storage_key_for(digest)]
    # And the direction that matters is clean.
    assert find_missing_objects(store, known) == []


# ===========================================================================
# 26-29. The publication boundary
# ===========================================================================


def test_evidence_from_a_pending_source_cannot_support_publication(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirements 2 and 26. C27, asserted from the acquisition side.

    The snapshot is real and stored. Turning it into a claim is refused by the
    database, not by this code choosing not to try.
    """
    _enqueue(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "w")
    assert lease is not None
    recorded = record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)
    assert recorded.snapshot_id is not None

    eligibility = conn.execute(
        text("SELECT publication_eligibility FROM source WHERE id = :s"), {"s": lease.source_id}
    ).scalar_one()
    assert eligibility == PublicationEligibility.NOT_ELIGIBLE.value

    with expect_violation(conn, "eligib|NOT_ELIGIBLE|not permitted|restrict"):
        conn.execute(
            text(
                "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
                "status) VALUES (:id, :snap, 'test', '1', 'OK')"
            ),
            {"id": uuid.uuid4(), "snap": recorded.snapshot_id},
        )
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, entity_id, "
                "field_path, proposed_field_status, value_normalized, observed_at) "
                "SELECT :id, e.id, 'tuition', :entity, 'amount_min', 'PUBLISHED', "
                "       '1'::jsonb, now() "
                "  FROM extraction e WHERE e.snapshot_id = :snap"
            ),
            {"id": uuid.uuid4(), "entity": uuid.uuid4(), "snap": recorded.snapshot_id},
        )


def test_a_source_cannot_be_made_eligible_by_registering_it(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """C27's `source_eligibility_is_earned`, unweakened by Step 5B."""
    source_id = registered["source_ids"][0]
    actor = _actor(conn)
    with expect_violation(conn, "source_eligibility_is_earned|eligib"):
        conn.execute(
            text(
                "UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED', "
                "eligibility_set_by = :a, eligibility_set_at = now() WHERE id = :id"
            ),
            {"id": source_id, "a": actor},
        )


def test_acquisition_creates_no_claim_proposal_or_canonical_row(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirements 27, 28 and 29 in one sweep."""
    _enqueue(conn, registered, "c1")
    while (lease := _claim_ours(conn, registered, "w")) is not None:
        record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    assert _count(conn, registered, "snapshot") == 3
    for table in (
        "field_claim",
        "extraction",
        "change_proposal",
        "field_provenance",
        "university",
        "program",
        "tuition",
        "admission_requirement",
        "entity_version",
    ):
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"acquisition wrote {count} row(s) to {table}"


def test_an_inactive_source_cannot_be_fetchable(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Two switches that could disagree is one switch too many."""
    source_id = registered["source_ids"][0]
    with expect_violation(conn, "an_inactive_source_is_not_fetchable"):
        conn.execute(
            text("UPDATE source SET is_active = false, deactivated_reason = 'test' WHERE id = :id"),
            {"id": source_id},
        )


def test_a_blocked_source_must_say_what_happened(
    conn: Connection, registered: dict[str, Any]
) -> None:
    source_id = registered["source_ids"][0]
    with expect_violation(conn, "blocked_names_what_happened"):
        conn.execute(
            text(
                "UPDATE source SET fetch_eligibility = 'BLOCKED', "
                "fetch_eligibility_reason = NULL WHERE id = :id"
            ),
            {"id": source_id},
        )


# ===========================================================================
# 24. Source health, and verification assistance
# ===========================================================================


def test_source_health_derives_from_history(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 24. A view over the runs, so it cannot drift from them."""
    health = {
        row[0]: str(row[1])
        for row in conn.execute(
            text("SELECT source_id, health FROM source_health WHERE source_id = ANY(:ids)"),
            {"ids": registered["source_ids"]},
        )
    }
    assert set(health.values()) == {"NEVER_FETCHED"}

    _enqueue_one(conn, registered, "c1")
    lease = _claim_ours(conn, registered, "w")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    row = conn.execute(
        text(
            "SELECT health, total_runs, consecutive_failures, last_status, "
            "       last_content_hash, last_success_at FROM source_health WHERE source_id = :s"
        ),
        {"s": lease.source_id},
    ).one()
    assert row.health == "HEALTHY"
    assert row.total_runs == 1
    assert row.consecutive_failures == 0
    assert row.last_status == "OK"
    assert row.last_content_hash is not None

    # Two failures in a row -> DEGRADED then FAILING, counted from the history.
    #
    # Each run is given a distinct `started_at`. Real fetches are seconds apart, but
    # four constructed in one test share an instant, and the view's last tiebreak is
    # a random uuid -- so without this the "most recent" run is whichever id sorted
    # highest, and the assertion below would flake rather than fail.
    for index in range(3):
        # `max_attempts` raised past its default: the three-per-cycle cap is the
        # subject of its own test, and here it would stop the run before the third
        # failure that distinguishes FAILING from DEGRADED.
        schedule_retry(
            conn,
            source_id=lease.source_id,
            cycle_key="c1",
            delay_seconds=0,
            max_attempts=10,
        )
        nxt = _claim_ours(conn, registered, "w")
        assert nxt is not None
        record_outcome(
            conn,
            lease=nxt,
            outcome=_outcome(
                nxt.url,
                content=None,
                status=FetchStatus.HTTP_ERROR,
                http_status=500,
                error_class=f"HTTP500-{index}",
                started_at=utcnow() + timedelta(seconds=index + 1),
                finished_at=utcnow() + timedelta(seconds=index + 1),
            ),
            store=store,
        )
        state = conn.execute(
            text("SELECT health, consecutive_failures FROM source_health WHERE source_id = :s"),
            {"s": lease.source_id},
        ).one()
        assert state.consecutive_failures == index + 1
        assert state.health == ("DEGRADED" if index < 2 else "FAILING")


def test_verification_assistance_reports_without_deciding(
    conn: Connection, registered: dict[str, Any], store: FilesystemEvidenceStore
) -> None:
    """Requirement 23 of the spec. Surfaced, and acted on by nothing.

    The page redirected to another host and returned a title. Both are shown; the
    source is still `PENDING`, still `NOT_ELIGIBLE`, still unmapped.
    """
    # Alpha's page specifically, not "whichever page was claimed first". Enqueueing
    # all three and claiming one left the choice to a tie-break between identical
    # `scheduled_for` values, so the assertion below held only when the arbitrary
    # winner happened to belong to alpha -- and a query-plan change in Step 5B.2
    # (extra predicates on `claim_next`) was enough to flip it. The subject of the
    # test is the assist output, not claim ordering, so the page is chosen here.
    alpha_source = conn.execute(
        text(
            "SELECT acquisition_source_id FROM pilot_collected_source "
            " WHERE target_institution_id = :t AND acquisition_source_id IS NOT NULL "
            " ORDER BY source_ref LIMIT 1"
        ),
        {"t": registered["target_alpha"]},
    ).scalar_one()
    enqueue_cycle(conn, cycle_key="c1", source_ids=[alpha_source])
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    assert lease.source_id == alpha_source
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            effective_url="https://portal.supplier.example.com/apply",
            etag='"v1"',
            redirect_chain=[
                {
                    "status": 302,
                    "from": lease.url,
                    "to": "https://portal.supplier.example.com/apply",
                }
            ],
            technical_metadata={"title": "Apply | Supplier Portal"},
        ),
        store=store,
    )

    rows = verification_assistance(conn, target_institution_id=registered["target_alpha"])
    moved = [row for row in rows if row.host_differs]
    assert len(moved) == 1
    assert moved[0].effective_host == "portal.supplier.example.com"
    assert moved[0].page_title == "Apply | Supplier Portal"
    assert moved[0].redirect_chain is not None
    assert moved[0].publication_eligibility == PublicationEligibility.NOT_ELIGIBLE.value
    # Whichever page the worker claimed, its own responsibilities are the ones
    # surfaced -- asserted against the database rather than a fixed category, since
    # claim order across three queued pages is not the thing under test.
    expected = set(
        conn.execute(
            text(
                "SELECT source_type FROM pilot_collected_source "
                " WHERE acquisition_source_id = :s"
            ),
            {"s": lease.source_id},
        ).scalars()
    )
    assert set(moved[0].claimed_categories) == expected
    assert expected

    # Nothing about that host was promoted by observing it.
    assert conn.execute(text("SELECT count(*) FROM official_domain")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM source_mapping")).scalar_one() == 0
    states = (
        conn.execute(text("SELECT DISTINCT verification_state FROM pilot_collected_source"))
        .scalars()
        .all()
    )
    assert set(states) == {"PENDING"}


def test_the_acquisition_summary_counts_pages_and_claims_separately(
    conn: Connection, registered: dict[str, Any]
) -> None:
    summary = acquisition_summary(conn)
    # Scoped by asserting the fixture's own contribution rather than a global total:
    # the development database carries the committed Step 5A import.
    assert summary.physical_pages >= 3
    assert summary.responsibility_claims >= 5
    assert summary.institutions >= 2
    assert summary.hosts >= 2
    assert summary.fetchable >= 3
    # The number that must be exact, because it is the boundary.
    assert summary.publication_eligible == 0
    assert set(summary.by_health) == {"NEVER_FETCHED"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _count(conn: Connection, pilot: dict[str, Any], table: str, extra: str = "TRUE") -> int:
    """Rows of `table` belonging to this fixture's sources.

    A literal table name and predicate, both from this module's own call sites --
    never from anything a caller supplies.
    """
    allowed = {"fetch_attempt", "fetch_run", "snapshot"}
    assert table in allowed, table
    return int(
        conn.execute(
            text(f"SELECT count(*) FROM {table} " f" WHERE source_id = ANY(:ids) AND {extra}"),
            {"ids": pilot["source_ids"]},
        ).scalar_one()
    )


def _actor(conn: Connection) -> uuid.UUID:
    existing: uuid.UUID | None = conn.execute(
        text("SELECT id FROM app_user WHERE email = 'acq@example.test'")
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    actor_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO app_user (id, email, display_name) "
            "VALUES (:id, 'acq@example.test', 'Acquisition')"
        ),
        {"id": actor_id},
    )
    return actor_id


def _fresh_attempt(conn: Connection, source_id: uuid.UUID) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO fetch_attempt (id, source_id, attempt_no, cycle_key, state) "
            "VALUES (:id, :s, 99, 'manual', 'QUEUED')"
        ),
        {"id": attempt_id, "s": source_id},
    )
    return attempt_id
