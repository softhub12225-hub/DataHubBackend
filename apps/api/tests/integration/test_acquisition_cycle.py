"""One acquisition cycle, end to end against the fixture server (Step 5B).

The tests above this one check the pieces. This one checks that they compose: a queued
attempt becomes a real HTTP request against a local server, a stored object, a
`content_blob`, a `fetch_run` and a `snapshot`, with the lease fenced throughout.

It also carries the one test that needs **two database connections**: two workers
racing for the same attempt cannot be tested inside a single rolled-back transaction,
because neither connection can see the other's uncommitted rows. That test commits and
cleans up after itself.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from app.db.enums import FetchStatus
from app.domains.acquisition.lease import claim_next, enqueue_cycle
from app.domains.acquisition.registration import register_acquisition_targets
from app.domains.acquisition.runner import HostGate, run_cycle
from app.domains.acquisition.storage import FilesystemEvidenceStore
from tests.integration.fixtures_http import HTML_ONE, TEST_MAX_BYTES, FixtureServer
from tests.integration.test_acquisition_fetch import make_fetcher

pytestmark = pytest.mark.integration


@pytest.fixture
def server() -> Any:
    with FixtureServer() as fixture:
        yield fixture


@pytest.fixture
def engine(postgres_dsn: str) -> Any:
    created = create_engine(postgres_dsn, future=True)
    yield created
    created.dispose()


def _seed(connection: Connection, server: FixtureServer, paths: list[str]) -> dict[str, Any]:
    """A pilot whose pages are the fixture server's routes."""
    import hashlib

    list_id, submission_id, target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, file_sha256, "
            "file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:id, 'Cycle', :v, 'c.xlsx', :sha, 1, 's', 1)"
        ),
        {"id": list_id, "v": list_id.hex[:8], "sha": hashlib.sha256(list_id.bytes).hexdigest()},
    )
    connection.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id, latest_list_id, "
            "destination_code, pilot_wave) VALUES (:id, :key, :l, :l, 'GB', 1)"
        ),
        {"id": target, "key": f"cycle-{target.hex[:8]}", "l": list_id},
    )
    connection.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, file_byte_size, "
            "template_version, submission_kind, defines_pilot_scope, selected_university_count) "
            "VALUES (:id, :sha, 's.xlsx', 1, 'v1', 'OFFICIAL_SOURCE_LIST', true, 1)"
        ),
        {"id": submission_id, "sha": hashlib.sha256(submission_id.bytes).hexdigest()},
    )
    connection.execute(
        text(
            "INSERT INTO pilot_selected_university (submission_id, target_institution_id, "
            "sheet_row_no, is_selected, official_homepage) "
            "VALUES (:s, :t, 2, true, :home)"
        ),
        {"s": submission_id, "t": target, "home": server.url("/ok")},
    )
    for index, path in enumerate(paths, start=1):
        url = server.url(path)
        connection.execute(
            text(
                "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
                "target_institution_id, sheet_row_no, source_type, official_url, "
                "normalized_url, url_sha256, host, workbook_column) "
                "VALUES (:id, :sub, :ref, :t, 2, 'PROGRAM_CATALOG', :url, :url, :hash, "
                ":host, 'Program/Course Catalogue URL')"
            ),
            {
                "id": uuid.uuid4(),
                "sub": submission_id,
                "ref": f"S{index:04d}",
                "t": target,
                "url": url,
                "hash": hashlib.sha256(url.encode()).hexdigest(),
                "host": url.split("/")[2],
            },
        )
    # `register_acquisition_targets` computes fetch eligibility from URL shape, and an
    # IP literal is correctly refused -- so the fixture's own sources are marked
    # FETCHABLE here explicitly. Everything else about them goes through the real path.
    register_acquisition_targets(connection, submission_id=submission_id)
    connection.execute(
        text(
            "UPDATE source SET fetch_eligibility = 'FETCHABLE', "
            "fetch_eligibility_reason = 'loopback fixture server (test only)' "
            " WHERE id IN (SELECT acquisition_source_id FROM pilot_collected_source "
            "               WHERE submission_id = :s)"
        ),
        {"s": submission_id},
    )
    source_ids = list(
        connection.execute(
            text(
                "SELECT DISTINCT acquisition_source_id FROM pilot_collected_source "
                " WHERE submission_id = :s"
            ),
            {"s": submission_id},
        ).scalars()
    )
    return {
        "submission_id": submission_id,
        "target": target,
        "list_id": list_id,
        "source_ids": source_ids,
    }


def _cleanup(engine: Engine, seeded: dict[str, Any]) -> None:
    """Remove committed fixture rows, newest dependency first.

    Uses `app.allow_history_maintenance`, which the append-only trigger itself names
    as the sanctioned escape -- rather than disabling the trigger, which would need
    table ownership and would be a habit worth not forming.
    """
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL app.allow_history_maintenance = 'on'"))
        for statement, params in (
            ("DELETE FROM snapshot WHERE source_id = ANY(:ids)", {"ids": seeded["source_ids"]}),
            ("DELETE FROM fetch_run WHERE source_id = ANY(:ids)", {"ids": seeded["source_ids"]}),
            (
                "DELETE FROM fetch_attempt WHERE source_id = ANY(:ids)",
                {"ids": seeded["source_ids"]},
            ),
            (
                "DELETE FROM pilot_collected_source WHERE submission_id = :s",
                {"s": seeded["submission_id"]},
            ),
            (
                "DELETE FROM pilot_selected_university WHERE submission_id = :s",
                {"s": seeded["submission_id"]},
            ),
            ("DELETE FROM pilot_submission WHERE id = :s", {"s": seeded["submission_id"]}),
            ("DELETE FROM source WHERE id = ANY(:ids)", {"ids": seeded["source_ids"]}),
            ("DELETE FROM target_institution WHERE id = :t", {"t": seeded["target"]}),
            # Last, and by its own id: the institution row that referenced it is
            # already gone by this point.
            ("DELETE FROM target_list WHERE id = :l", {"l": seeded["list_id"]}),
        ):
            connection.execute(text(statement), params)


def test_a_cycle_fetches_stores_and_records(
    conn: Connection, server: FixtureServer, tmp_path: Path, postgres_dsn: str
) -> None:
    """End to end: queue, fetch, store, record. Nothing extracted.

    Uses one connection throughout by running the cycle against an engine bound to the
    same transaction, so the whole thing rolls back.
    """
    seeded = _seed(conn, server, ["/ok", "/pdf"])
    enqueue_cycle(conn, cycle_key="e2e", source_ids=seeded["source_ids"])

    store = FilesystemEvidenceStore(tmp_path / "evidence")
    report = asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="e2e",
            store=store,
            worker="test-worker",
            fetcher=make_fetcher(server, max_bytes=TEST_MAX_BYTES),
            gate=HostGate(min_interval_seconds=0.0),
        )
    )

    assert report.attempted == 2
    assert report.ok == 2
    assert report.snapshots == 2
    assert report.blobs_created == 2
    assert len(report.urls_contacted) == 2

    rows = conn.execute(
        text(
            "SELECT s.content_hash, s.http_status, s.content_type, s.technical_metadata, "
            "       b.storage_key, b.byte_size, r.status, r.bytes_downloaded "
            "  FROM snapshot s "
            "  JOIN content_blob b ON b.content_hash = s.content_hash "
            "  JOIN fetch_run r ON r.id = s.fetch_run_id "
            " WHERE s.source_id = ANY(:ids) ORDER BY s.content_type"
        ),
        {"ids": seeded["source_ids"]},
    ).all()
    assert len(rows) == 2
    kinds = {row.content_type.split(";")[0] for row in rows}
    assert kinds == {"text/html", "application/pdf"}
    for row in rows:
        assert row.status == FetchStatus.OK.value
        assert store.exists(row.content_hash), "a blob row references a missing object"
        assert row.storage_key.endswith(row.content_hash)
        assert row.bytes_downloaded == row.byte_size

    # The PDF was stored and not read.
    pdf = next(r for r in rows if r.content_type.startswith("application/pdf"))
    assert pdf.technical_metadata == {"content_type": "application/pdf"}

    html = next(r for r in rows if r.content_type.startswith("text/html"))
    assert html.technical_metadata["title"] == "Fees 2027"
    assert store.get(html.content_hash) == HTML_ONE

    for table in ("field_claim", "extraction", "change_proposal", "field_provenance"):
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_a_blocked_response_stops_the_source_being_scheduled(
    conn: Connection, server: FixtureServer, tmp_path: Path
) -> None:
    """Requirement 21. The site said no; recording it and stopping is the response."""
    seeded = _seed(conn, server, ["/403"])
    enqueue_cycle(conn, cycle_key="blocked", source_ids=seeded["source_ids"])

    report = asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="blocked",
            store=FilesystemEvidenceStore(tmp_path / "evidence"),
            worker="test-worker",
            fetcher=make_fetcher(server),
            gate=HostGate(min_interval_seconds=0.0),
        )
    )
    assert report.blocked == 1

    source = conn.execute(
        text(
            "SELECT fetch_eligibility, access_state, fetch_eligibility_reason FROM source "
            " WHERE id = ANY(:ids)"
        ),
        {"ids": seeded["source_ids"]},
    ).one()
    assert source.fetch_eligibility == "BLOCKED"
    assert source.access_state == "BLOCKED"
    assert "HTTP403" in source.fetch_eligibility_reason

    # And it is no longer schedulable, which is what "stop retrying" means here.
    again = enqueue_cycle(conn, cycle_key="blocked-2", source_ids=seeded["source_ids"])
    assert again.attempts_created == 0
    assert again.blocked_or_disabled == 1


def test_a_429_pauses_the_host_for_the_stated_time(
    conn: Connection, server: FixtureServer, tmp_path: Path
) -> None:
    """The server's number, obeyed. No retry sooner than it asked."""
    seeded = _seed(conn, server, ["/429"])
    enqueue_cycle(conn, cycle_key="throttle", source_ids=seeded["source_ids"])
    gate = HostGate(min_interval_seconds=0.0)

    report = asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="throttle",
            store=FilesystemEvidenceStore(tmp_path / "evidence"),
            worker="test-worker",
            fetcher=make_fetcher(server),
            gate=gate,
        )
    )
    # Step 5B.2 §1. A 429 is a throttle, not a refusal: it used to arrive here as
    # `BLOCKED`, which set `fetch_eligibility = 'BLOCKED'` permanently and skipped the
    # retry branch, so the first rate limit any host sent removed that page from the
    # schedule for good.
    assert report.blocked == 0, "a 429 was counted as a refusal"
    assert report.rate_limited == 1
    run = conn.execute(
        text(
            "SELECT http_status, error_class, status::text AS status FROM fetch_run "
            " WHERE source_id = ANY(:ids)"
        ),
        {"ids": seeded["source_ids"]},
    ).one()
    assert run.http_status == 429
    assert run.status == "RATE_LIMITED"
    # `fetch_run.error_class` carries "class: detail" by long-standing convention --
    # the table has no separate detail column, so the alternative is losing the
    # detail. The class is what identifies the condition, and it is no longer
    # `HTTP429` lumped in with 401 and 403.
    assert run.error_class.startswith("RateLimited")

    # The source is still fetchable -- just not yet.
    state = conn.execute(
        text(
            "SELECT fetch_eligibility::text AS fe, access_state::text AS access, "
            "       cooldown_until, cooldown_reason, rate_limit_strikes "
            "  FROM source WHERE id = ANY(:ids)"
        ),
        {"ids": seeded["source_ids"]},
    ).one()
    assert state.fe == "FETCHABLE", "a single 429 blocked the source permanently"
    assert state.access == "OK"
    assert state.cooldown_until is not None
    assert "429" in state.cooldown_reason
    assert state.rate_limit_strikes == 1

    # And the whole host is quiet, not just the page that happened to be asked (§13).
    host_pause = conn.execute(text("SELECT host, cooldown_until, reason FROM host_cooldown")).all()
    assert len(host_pause) == 1
    assert report.hosts_paused == 1


def test_two_workers_on_two_connections_cannot_share_an_attempt(
    engine: Engine, server: FixtureServer
) -> None:
    """Requirement 5, with real concurrency.

    Two connections, two transactions, one queued attempt. `SKIP LOCKED` means the
    loser gets nothing rather than blocking, and the tokens differ so neither can
    write on the other's behalf. This test commits, so it cleans up after itself.
    """
    with engine.begin() as connection:
        seeded = _seed(connection, server, ["/ok"])
        enqueue_cycle(connection, cycle_key="race", source_ids=seeded["source_ids"])

    try:
        first_connection = engine.connect()
        second_connection = engine.connect()
        try:
            first_transaction = first_connection.begin()
            second_transaction = second_connection.begin()
            try:
                first = claim_next(first_connection, cycle_key="race", worker="racer-a")
                second = claim_next(second_connection, cycle_key="race", worker="racer-b")
                assert first is not None, "the first worker got nothing"
                assert second is None, "two workers claimed the same attempt"
                first_transaction.commit()
                second_transaction.rollback()
            except BaseException:
                first_transaction.rollback()
                second_transaction.rollback()
                raise
        finally:
            first_connection.close()
            second_connection.close()

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT state, claimed_by, lease_token, lease_generation "
                    "  FROM fetch_attempt WHERE source_id = ANY(:ids)"
                ),
                {"ids": seeded["source_ids"]},
            ).one()
            assert row.state == "RUNNING"
            assert row.claimed_by == "racer-a"
            assert row.lease_token is not None
            assert row.lease_generation == 1
    finally:
        _cleanup(engine, seeded)


class _SameTransactionEngine:
    """Hands `run_cycle` the test's own connection instead of opening new ones.

    `run_cycle` takes an engine because a worker owns its own transactions. A test
    that let it open real ones could not roll back, and would leave rows behind in a
    database the rest of the suite shares. Every `begin()` here yields the enclosing
    transaction via a savepoint, so the whole cycle unwinds with the test.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def begin(self) -> Any:
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
