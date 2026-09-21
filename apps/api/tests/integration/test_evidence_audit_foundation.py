"""Step 3.5: the evidence and audit foundation Step 4 will build on.

Three properties are under test, each one a defect that was found in review:

1. **Audit chain determinism under concurrency.** The chain must have exactly one
   predecessor per entry and must not fork when two writers append at once.
2. **Fetch lifecycle.** Progress state must never mutate immutable history, a
   crashed worker must become observable, and retries must stay separate attempts.
3. **Content vs observation.** Deduplicating identical bytes must not erase the
   record of having looked.

The concurrency tests use real concurrent connections rather than a simulation: the
guarantee is about PostgreSQL's locking, so a mocked version would prove nothing.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Connection, Engine, text

from tests.integration.conftest import EligibleEvidence, expect_violation

pytestmark = pytest.mark.integration


SHA_A = "a" * 64
SHA_B = "b" * 64


def _audit(conn: Connection, action: str) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (id, actor_type, action, object_type) "
            "VALUES (:id, 'SYSTEM', :action, 'program')"
        ),
        {"id": uuid.uuid4(), "action": action},
    )


# ===========================================================================
# 1. Audit chain
# ===========================================================================


def test_seq_is_assigned_by_the_database_not_the_caller(conn: Connection) -> None:
    """A caller cannot choose its own chain position."""
    conn.execute(
        text(
            "INSERT INTO audit_log (id, seq, actor_type, action, object_type, prev_hash, "
            "row_hash) VALUES (:id, 999999, 'SYSTEM', 'forge.attempt', 'program', "
            "'forged-prev', 'forged-row')"
        ),
        {"id": uuid.uuid4()},
    )
    row = conn.execute(
        text("SELECT seq, prev_hash, row_hash FROM audit_log WHERE action = 'forge.attempt'")
    ).one()
    assert row[0] != 999999, "the trigger must overwrite a caller-supplied seq"
    assert row[1] != "forged-prev"
    assert row[2] != "forged-row"
    assert len(row[2]) == 64


def test_predecessor_assignment_is_deterministic(conn: Connection) -> None:
    """Each entry's prev_hash is exactly its seq-predecessor's row_hash."""
    for i in range(6):
        _audit(conn, f"determinism.{i}")

    rows = conn.execute(text("SELECT seq, prev_hash, row_hash FROM audit_log ORDER BY seq")).all()
    assert len(rows) >= 6

    # Contiguous and strictly increasing.
    seqs = [r[0] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    # Every link points at its immediate predecessor, and only at that.
    for previous, current in itertools.pairwise(rows):
        assert current[1] == previous[2], f"seq {current[0]} must link to seq {previous[0]}"


def test_the_chain_verifies_against_its_own_contents(conn: Connection) -> None:
    """The in-database verifier finds nothing wrong with a chain it did not build."""
    for i in range(4):
        _audit(conn, f"verify.{i}")
    problems = conn.execute(text("SELECT bad_seq, reason FROM app_audit_log_verify_chain()")).all()
    assert problems == [], f"chain verification reported: {problems}"


def test_chain_order_does_not_depend_on_occurred_at(conn: Connection) -> None:
    """Rows written in one transaction share occurred_at exactly.

    That equality is what broke the old `ORDER BY occurred_at DESC, id DESC` scheme:
    the tie fell through to a random UUID. `seq` must order them regardless.
    """
    for i in range(3):
        _audit(conn, f"same-instant.{i}")

    rows = conn.execute(
        text(
            "SELECT seq, occurred_at FROM audit_log "
            "WHERE action LIKE 'same-instant.%' ORDER BY seq"
        )
    ).all()
    assert len(rows) == 3
    assert len({r[1] for r in rows}) == 1, "expected identical occurred_at in one transaction"
    assert [r[0] for r in rows] == sorted(r[0] for r in rows), "seq must still order them"


def test_concurrent_appends_cannot_fork_the_chain(owner_engine: Engine, postgres_dsn: str) -> None:
    """The core concurrency guarantee, exercised with real parallel connections.

    Eight threads append simultaneously. Every entry must get a distinct seq and a
    distinct prev_hash, and the links must form one unbroken chain -- no fork, no
    duplicate predecessor, and no writer failing merely because another was appending.
    """
    marker = f"concurrent-{uuid.uuid4().hex[:8]}"
    writers = 8
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def append(index: int) -> None:
        try:
            with owner_engine.connect() as connection:
                transaction = connection.begin()
                # Line every thread up so they contend for the head lock together.
                barrier.wait(timeout=30)
                connection.execute(
                    text(
                        "INSERT INTO audit_log (id, actor_type, action, object_type) "
                        "VALUES (:id, 'SYSTEM', :action, 'program')"
                    ),
                    {"id": uuid.uuid4(), "action": f"{marker}.{index}"},
                )
                transaction.commit()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    try:
        assert errors == [], f"concurrent appends must block, not fail: {errors}"

        with owner_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT seq, prev_hash, row_hash FROM audit_log "
                    "WHERE action LIKE :pattern ORDER BY seq"
                ),
                {"pattern": f"{marker}.%"},
            ).all()

            assert len(rows) == writers
            assert len({r[0] for r in rows}) == writers, "seq collision: the chain forked"
            assert len({r[1] for r in rows}) == writers, "duplicate prev_hash: the chain forked"
            assert len({r[2] for r in rows}) == writers

            # Contiguous, and each links to the previous one.
            seqs = [r[0] for r in rows]
            assert seqs == list(range(seqs[0], seqs[0] + writers))
            for previous, current in itertools.pairwise(rows):
                assert current[1] == previous[2]

            problems = connection.execute(
                text("SELECT bad_seq, reason FROM app_audit_log_verify_chain()")
            ).all()
            assert problems == [], f"chain verification reported: {problems}"
    finally:
        # Committed on purpose -- the point was real concurrency -- so clean up under
        # the maintenance escape hatch rather than leaving the chain polluted.
        with owner_engine.connect() as connection:
            cleanup = connection.begin()
            connection.execute(text("SET LOCAL app.allow_history_maintenance = 'on'"))
            connection.execute(
                text("DELETE FROM audit_log WHERE action LIKE :pattern"),
                {"pattern": f"{marker}.%"},
            )
            connection.execute(
                text(
                    "UPDATE audit_chain_head SET last_seq = coalesce("
                    "(SELECT max(seq) FROM audit_log), 0), last_row_hash = ("
                    "SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1) WHERE singleton"
                )
            )
            cleanup.commit()


def test_a_retried_append_produces_a_new_link_not_a_conflict(conn: Connection) -> None:
    """Application retries must stay safe.

    A retry re-enters the head lock and gets a fresh predecessor, so an accidentally
    repeated append becomes an additional entry rather than a constraint violation
    that masks the original write.
    """
    _audit(conn, "retry.probe")
    _audit(conn, "retry.probe")
    rows = conn.execute(
        text("SELECT seq, prev_hash FROM audit_log WHERE action = 'retry.probe' ORDER BY seq")
    ).all()
    assert len(rows) == 2
    assert rows[0][1] != rows[1][1], "each attempt must take its own predecessor"


def test_audit_entries_remain_non_updatable_and_non_deletable(conn: Connection) -> None:
    _audit(conn, "immutable.probe")
    with expect_violation(conn, "append-only history"):
        conn.execute(text("UPDATE audit_log SET action = 'tampered' WHERE action LIKE '%probe'"))
    with expect_violation(conn, "append-only history"):
        conn.execute(text("DELETE FROM audit_log WHERE action LIKE '%probe'"))


def test_runtime_roles_cannot_touch_the_chain_head(conn: Connection) -> None:
    """The head counter is reachable only by appending, because the trigger is
    SECURITY DEFINER and no role holds privileges on the table."""
    granted = conn.execute(
        text(
            "SELECT grantee, privilege_type FROM information_schema.table_privileges "
            "WHERE table_name = 'audit_chain_head' AND grantee LIKE 'app%'"
        )
    ).all()
    assert granted == [], f"no runtime role may touch audit_chain_head, but: {granted}"


def test_there_can_only_ever_be_one_chain_head(conn: Connection) -> None:
    with expect_violation(conn, "duplicate key|exactly_one_row"):
        conn.execute(text("INSERT INTO audit_chain_head (singleton, last_seq) VALUES (true, 0)"))


# ===========================================================================
# 2. Fetch lifecycle
# ===========================================================================


@pytest.fixture
def source_id(conn: Connection) -> uuid.UUID:
    """A registered fictional source."""
    identifier = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:id, :url, :hash, 'admissions_page', 'DAILY', 'STATIC')"
        ),
        {
            "id": identifier,
            "url": f"https://example.test/{identifier.hex[:8]}",
            "hash": identifier.hex + "0" * (64 - len(identifier.hex)),
        },
    )
    return identifier


def _attempt(
    conn: Connection, source: uuid.UUID, *, cycle: str, attempt_no: int, state: str = "QUEUED"
) -> uuid.UUID:
    identifier = uuid.uuid4()
    columns: dict[str, object] = {
        "id": identifier,
        "source_id": source,
        "cycle_key": cycle,
        "attempt_no": attempt_no,
        "state": state,
    }
    if state == "RUNNING":
        columns |= {
            "claimed_at": datetime.now(UTC),
            "claimed_by": "worker-1",
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
            # Step 5B: a RUNNING attempt carries the token that authorises its
            # writes, and the CHECK now insists. A fixture is a producer like any
            # other, so it supplies one rather than being exempted.
            "lease_token": uuid.uuid4(),
            "lease_generation": 1,
        }
    if state in {"FINALIZED", "ABANDONED"}:
        columns["finalized_at"] = datetime.now(UTC)
    names = ", ".join(columns)
    values = ", ".join(f":{name}" for name in columns)
    conn.execute(text(f"INSERT INTO fetch_attempt ({names}) VALUES ({values})"), columns)
    return identifier


def _run(
    conn: Connection, source: uuid.UUID, attempt: uuid.UUID, *, status: str, attempt_no: int
) -> uuid.UUID:
    identifier = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
            "finished_at, status, fetcher, error_class) "
            "VALUES (:id, :attempt, :source, :no, now(), now(), :status, 'STATIC', :err)"
        ),
        {
            "id": identifier,
            "attempt": attempt,
            "source": source,
            "no": attempt_no,
            "status": status,
            "err": None if status in {"OK", "UNCHANGED"} else f"{status}Error",
        },
    )
    return identifier


def test_a_running_attempt_must_be_leased(conn: Connection, source_id: uuid.UUID) -> None:
    """Without a lease and an owner, a crash would be undetectable."""
    with expect_violation(conn, "running_attempt_is_leased"):
        conn.execute(
            text(
                "INSERT INTO fetch_attempt (id, source_id, cycle_key, state) "
                "VALUES (:id, :source, '2027-01-15T06', 'RUNNING')"
            ),
            {"id": uuid.uuid4(), "source": source_id},
        )


def test_stale_running_work_is_detectable(conn: Connection, source_id: uuid.UUID) -> None:
    """A crashed worker stops renewing its lease; the sweeper query finds it."""
    stale = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO fetch_attempt (id, source_id, cycle_key, attempt_no, state, "
            "claimed_at, claimed_by, heartbeat_at, lease_expires_at, lease_token, "
            "lease_generation) "
            "VALUES (:id, :source, '2027-01-15T06', 1, 'RUNNING', now() - interval '20 min', "
            "'worker-crashed', now() - interval '18 min', now() - interval '10 min', "
            ":token, 1)"
        ),
        {"id": stale, "source": source_id, "token": uuid.uuid4()},
    )
    _attempt(conn, source_id, cycle="2027-01-15T12", attempt_no=1, state="RUNNING")

    stuck = (
        conn.execute(
            text(
                "SELECT id FROM fetch_attempt "
                "WHERE state = 'RUNNING' AND lease_expires_at < now()"
            )
        )
        .scalars()
        .all()
    )
    assert list(stuck) == [stale], "only the expired lease should be reported as stuck"


def test_an_abandoned_attempt_becomes_permanent_history(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """The crash is recorded in immutable history, not just cleared from the queue."""
    attempt = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=1, state="RUNNING")
    _run(conn, source_id, attempt, status="ABANDONED", attempt_no=1)
    conn.execute(
        text("UPDATE fetch_attempt SET state = 'ABANDONED', finalized_at = now() WHERE id = :id"),
        {"id": attempt},
    )

    status = conn.execute(
        text("SELECT status FROM fetch_run WHERE attempt_id = :a"), {"a": attempt}
    ).scalar_one()
    assert status == "ABANDONED"

    # And that record cannot then be quietly removed.
    with expect_violation(conn, "append-only history"):
        conn.execute(text("DELETE FROM fetch_run WHERE attempt_id = :a"), {"a": attempt})


def test_retries_are_separate_attempts_with_separate_history(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """A failure followed by a successful retry must leave both outcomes visible."""
    first = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=1, state="FINALIZED")
    _run(conn, source_id, first, status="TIMEOUT", attempt_no=1)
    second = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=2, state="FINALIZED")
    _run(conn, source_id, second, status="OK", attempt_no=2)

    outcomes = conn.execute(
        text("SELECT attempt_no, status FROM fetch_run WHERE source_id = :s ORDER BY attempt_no"),
        {"s": source_id},
    ).all()
    assert [(r[0], r[1]) for r in outcomes] == [
        (1, "TIMEOUT"),
        (2, "OK"),
    ], "the failed attempt must not be replaced by the successful retry"


def test_one_completed_run_per_attempt(conn: Connection, source_id: uuid.UUID) -> None:
    """Two runs for one attempt would mean an attempt with two outcomes."""
    attempt = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=1, state="FINALIZED")
    _run(conn, source_id, attempt, status="OK", attempt_no=1)
    with expect_violation(conn, "uq_fetch_run_attempt_id|duplicate key"):
        _run(conn, source_id, attempt, status="HTTP_ERROR", attempt_no=1)


def test_failed_attempts_remain_queryable(conn: Connection, source_id: uuid.UUID) -> None:
    """Source health depends on historical failures never disappearing."""
    for index, status in enumerate(("HTTP_ERROR", "TIMEOUT", "BLOCKED", "OK"), start=1):
        attempt = _attempt(
            conn, source_id, cycle=f"2027-01-1{index}T06", attempt_no=1, state="FINALIZED"
        )
        _run(conn, source_id, attempt, status=status, attempt_no=1)

    failures = (
        conn.execute(
            text("SELECT status FROM fetch_run WHERE source_id = :s AND status <> 'OK'"),
            {"s": source_id},
        )
        .scalars()
        .all()
    )
    assert sorted(failures) == ["BLOCKED", "HTTP_ERROR", "TIMEOUT"]


def test_a_failed_run_must_name_its_error(conn: Connection, source_id: uuid.UUID) -> None:
    attempt = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=1, state="FINALIZED")
    with expect_violation(conn, "a_failure_names_its_error"):
        conn.execute(
            text(
                "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
                "status, fetcher) VALUES (:id, :a, :s, 1, now(), 'HTTP_ERROR', 'STATIC')"
            ),
            {"id": uuid.uuid4(), "a": attempt, "s": source_id},
        )


def test_progress_state_is_mutable_without_touching_history(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """The reason for the split: Celery can heartbeat, history cannot change."""
    attempt = _attempt(conn, source_id, cycle="2027-01-15T06", attempt_no=1, state="QUEUED")
    conn.execute(
        text(
            "UPDATE fetch_attempt SET state = 'RUNNING', claimed_at = now(), "
            "claimed_by = 'worker-7', heartbeat_at = now(), "
            "lease_expires_at = now() + interval '5 min', lease_token = :token, "
            "lease_generation = 1 WHERE id = :id"
        ),
        {"id": attempt, "token": uuid.uuid4()},
    )
    conn.execute(
        text("UPDATE fetch_attempt SET heartbeat_at = now() WHERE id = :id"), {"id": attempt}
    )
    _run(conn, source_id, attempt, status="OK", attempt_no=1)
    with expect_violation(conn, "append-only history"):
        conn.execute(
            text("UPDATE fetch_run SET status = 'UNCHANGED' WHERE attempt_id = :a"),
            {"a": attempt},
        )


# ===========================================================================
# 3. Content identity vs observation
# ===========================================================================


def _blob(conn: Connection, content_hash: str) -> None:
    conn.execute(
        text(
            "INSERT INTO content_blob (content_hash, storage_key, content_type, byte_size, "
            "first_observed_at) VALUES (:h, :key, 'text/html', 2048, now()) "
            "ON CONFLICT (content_hash) DO NOTHING"
        ),
        {"h": content_hash, "key": f"sources/blob/{content_hash}"},
    )


def _observe(
    conn: Connection,
    source: uuid.UUID,
    run: uuid.UUID,
    content_hash: str,
    *,
    requested: str,
    effective: str | None = None,
    observed_at: str = "now()",
) -> uuid.UUID:
    identifier = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, requested_url, "
            f"effective_url, http_status, fetcher, observed_at) VALUES (:id, :run, :source, "
            f":h, :req, :eff, 200, 'STATIC', {observed_at})"
        ),
        {
            "id": identifier,
            "run": run,
            "source": source,
            "h": content_hash,
            "req": requested,
            "eff": effective,
        },
    )
    return identifier


def test_identical_bytes_observed_twice_keep_both_observations(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """THE defect this revision fixes.

    A unique constraint on (source_id, content_hash) made the second observation
    unstorable, silently destroying the "we checked again on this date" record.
    """
    _blob(conn, SHA_A)
    first_attempt = _attempt(
        conn, source_id, cycle="2027-01-10T06", attempt_no=1, state="FINALIZED"
    )
    first_run = _run(conn, source_id, first_attempt, status="OK", attempt_no=1)
    second_attempt = _attempt(
        conn, source_id, cycle="2027-01-17T06", attempt_no=1, state="FINALIZED"
    )
    second_run = _run(conn, source_id, second_attempt, status="UNCHANGED", attempt_no=1)

    _observe(
        conn,
        source_id,
        first_run,
        SHA_A,
        requested="https://example.test/fees",
        observed_at="'2027-01-10T06:00:00+00'",
    )
    _observe(
        conn,
        source_id,
        second_run,
        SHA_A,
        requested="https://example.test/fees",
        observed_at="'2027-01-17T06:00:00+00'",
    )

    observations = conn.execute(
        text(
            "SELECT observed_at, fetch_run_id FROM snapshot "
            "WHERE source_id = :s AND content_hash = :h ORDER BY observed_at"
        ),
        {"s": source_id, "h": SHA_A},
    ).all()
    assert len(observations) == 2, "both observations of identical bytes must survive"
    assert observations[0][1] != observations[1][1], "each observation names its own fetch run"

    blobs = conn.execute(
        text("SELECT count(*) FROM content_blob WHERE content_hash = :h"), {"h": SHA_A}
    ).scalar_one()
    assert blobs == 1, "identical bytes must deduplicate to one blob"


def test_identical_bytes_from_two_sources_preserve_both_source_relationships(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """Two official URLs can serve the same document; both provenance paths matter."""
    other = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:id, :url, :hash, 'fee_page', 'DAILY', 'STATIC')"
        ),
        {"id": other, "url": "https://other.example.test/fees", "hash": "c" * 64},
    )
    _blob(conn, SHA_A)

    for source in (source_id, other):
        attempt = _attempt(conn, source, cycle="2027-01-10T06", attempt_no=1, state="FINALIZED")
        run = _run(conn, source, attempt, status="OK", attempt_no=1)
        _observe(conn, source, run, SHA_A, requested=f"https://example.test/{source.hex[:6]}")

    sources = (
        conn.execute(
            text("SELECT DISTINCT source_id FROM snapshot WHERE content_hash = :h"), {"h": SHA_A}
        )
        .scalars()
        .all()
    )
    assert set(sources) == {source_id, other}
    assert (
        conn.execute(
            text("SELECT count(*) FROM content_blob WHERE content_hash = :h"), {"h": SHA_A}
        ).scalar_one()
        == 1
    )


def test_a_redirect_records_both_urls(conn: Connection, source_id: uuid.UUID) -> None:
    """After a redirect, the requested and effective URLs both matter for provenance."""
    _blob(conn, SHA_B)
    attempt = _attempt(conn, source_id, cycle="2027-01-10T06", attempt_no=1, state="FINALIZED")
    run = _run(conn, source_id, attempt, status="OK", attempt_no=1)
    _observe(
        conn,
        source_id,
        run,
        SHA_B,
        requested="https://example.test/old-fees",
        effective="https://example.test/study/fees",
    )
    requested, effective = conn.execute(
        text("SELECT requested_url, effective_url FROM snapshot WHERE content_hash = :h"),
        {"h": SHA_B},
    ).one()
    assert requested == "https://example.test/old-fees"
    assert effective == "https://example.test/study/fees"


def test_content_identity_is_immutable(conn: Connection) -> None:
    """A blob's bytes never change, so neither may its row."""
    _blob(conn, SHA_A)
    with expect_violation(conn, "append-only history"):
        conn.execute(
            text("UPDATE content_blob SET storage_key = 'moved' WHERE content_hash = :h"),
            {"h": SHA_A},
        )
    with expect_violation(conn, "append-only history"):
        conn.execute(text("DELETE FROM content_blob WHERE content_hash = :h"), {"h": SHA_A})


def test_an_observation_cannot_reference_unregistered_content(
    conn: Connection, source_id: uuid.UUID
) -> None:
    """Evidence must exist before a fact can cite it."""
    attempt = _attempt(conn, source_id, cycle="2027-01-10T06", attempt_no=1, state="FINALIZED")
    run = _run(conn, source_id, attempt, status="OK", attempt_no=1)
    with expect_violation(conn, "violates foreign key"):
        _observe(conn, source_id, run, "f" * 64, requested="https://example.test/x")


def test_provenance_navigates_from_claim_to_source(
    conn: Connection, eligible_evidence: EligibleEvidence
) -> None:
    """The full chain Step 4 depends on:

    field_claim -> extraction -> snapshot (observation) -> fetch_run -> source
                                        \\-> content_blob

    Uses the `eligible_evidence` fixture rather than the plain `source_id` one,
    because since C27 a `field_claim` must resolve to a publication-eligible source.
    The ordinary fixture leaves its source at the closed default, which is right for
    every other test in this module -- none of the others create a claim.
    """
    source_id = eligible_evidence.source
    _blob(conn, SHA_A)
    attempt = _attempt(conn, source_id, cycle="2027-01-10T06", attempt_no=1, state="FINALIZED")
    run = _run(conn, source_id, attempt, status="OK", attempt_no=1)
    snapshot = _observe(conn, source_id, run, SHA_A, requested="https://example.test/fees")

    extraction = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
            "status) VALUES (:id, :snap, 'fee_table', '1.2.0', 'OK')"
        ),
        {"id": extraction, "snap": snapshot},
    )
    claim = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
            "proposed_field_status, value_normalized, value_raw_text, observed_at) "
            "VALUES (:id, :ext, 'tuition', 'amount', 'PUBLISHED', '32000'::jsonb, "
            "'GBP 32,000 per year', now())"
        ),
        {"id": claim, "ext": extraction},
    )

    row = conn.execute(
        text(
            """
            SELECT s.url            AS source_url,
                   fr.status        AS run_status,
                   fa.attempt_no    AS attempt_no,
                   sn.observed_at   AS observed_at,
                   sn.requested_url AS requested_url,
                   cb.storage_key   AS storage_key,
                   e.extractor_version,
                   fc.value_raw_text
              FROM field_claim fc
              JOIN extraction   e  ON e.id  = fc.extraction_id
              JOIN snapshot     sn ON sn.id = e.snapshot_id
              JOIN content_blob cb ON cb.content_hash = sn.content_hash
              JOIN fetch_run    fr ON fr.id = sn.fetch_run_id
              JOIN fetch_attempt fa ON fa.id = fr.attempt_id
              JOIN source       s  ON s.id  = fr.source_id
             WHERE fc.id = :claim
            """
        ),
        {"claim": claim},
    ).one()

    # Assert against the source's own URL rather than a host prefix: what this test
    # is about is that the chain navigates to the *right* source.
    expected_url = conn.execute(
        text("SELECT url FROM source WHERE id = :id"), {"id": source_id}
    ).scalar_one()
    assert row.source_url == expected_url
    assert row.run_status == "OK"
    assert row.attempt_no == 1
    assert row.requested_url == "https://example.test/fees"
    assert row.storage_key.endswith(SHA_A)
    assert row.extractor_version == "1.2.0"
    assert row.value_raw_text == "GBP 32,000 per year"
