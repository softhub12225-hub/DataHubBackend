"""Celery tasks for acquisition (Step 5B section 7).

SCHEDULES ARE CONFIGURABLE AND NOT YET THE PRD'S
================================================
The PRD's 06:00 / 12:00 / 18:00 sweep is a business rule about high-risk fields, and
imposing it globally now would put 319 pages in front of 120 universities three times a
day before anybody has looked at a single result. `beat_schedule` stays empty; cycles
are enqueued by hand for the pilot, and the schedule is switched on per source class
once the first real runs have been reviewed.

`cycle_key` groups retries of one logical check and makes enqueueing idempotent:
`uq_fetch_attempt_source_cycle_attempt` turns a second `enqueue` for the same cycle
into a no-op, which matters because "run the 06:00 cycle" is exactly the command an
operator runs twice when the first appears to have done nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.clock import now_in
from app.core.config import DatabaseRole, get_settings
from app.core.logging import get_logger
from app.domains.acquisition.lease import enqueue_cycle
from app.domains.acquisition.runner import run_cycle, sweep_and_report
from app.domains.acquisition.storage import EvidenceStore, build_evidence_store
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


def cycle_key_for(moment: Any = None) -> str:
    """A stable key for the cycle a moment belongs to: `2027-01-15T06`.

    Hour granularity in the scheduler's timezone, because the PRD's windows are local
    business rules and a key that drifted with UTC would split one 06:00 sweep across
    two keys twice a year.
    """
    from app.core.clock import SCHEDULER_TIMEZONE

    when = moment or now_in(SCHEDULER_TIMEZONE)
    return when.strftime("%Y-%m-%dT%H")


def _engine() -> Any:
    from sqlalchemy import create_engine

    settings = get_settings()
    return create_engine(settings.database.sync_dsn(DatabaseRole.WORKER), future=True)


def _store() -> EvidenceStore:
    """The evidence store this worker writes to.

    Chosen by `EVIDENCE_BACKEND`, defaulting to the filesystem so the worker still
    starts on a machine with neither Docker nor a bucket. This used to construct the
    filesystem store unconditionally, which meant a deployed worker wrote evidence to
    its own container and lost it on the next deploy however carefully S3 was
    configured.
    """
    return build_evidence_store(get_settings(), local_root=".evidence")


@celery_app.task(name="app.workers.tasks.crawl.enqueue_pilot_cycle", bind=True)
def enqueue_pilot_cycle(
    self: Any,
    *,
    cycle_key: str | None = None,
    target_institution_id: str | None = None,
    source_ids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Queue one attempt per physical page. Idempotent within a cycle."""
    del self
    key = cycle_key or cycle_key_for()
    engine = _engine()
    try:
        with engine.begin() as connection:
            report = enqueue_cycle(
                connection,
                cycle_key=key,
                source_ids=[uuid.UUID(s) for s in source_ids] if source_ids else None,
                target_institution_id=(
                    uuid.UUID(target_institution_id) if target_institution_id else None
                ),
                limit=limit,
            )
    finally:
        engine.dispose()
    return {
        "cycle_key": report.cycle_key,
        "physical_pages_selected": report.physical_pages_selected,
        "hosts": report.hosts,
        "duplicate_responsibilities_skipped": report.duplicate_responsibilities_skipped,
        "already_queued": report.already_queued,
        "blocked_or_disabled": report.blocked_or_disabled,
        "attempts_created": report.attempts_created,
    }


@celery_app.task(name="app.workers.tasks.crawl.run_acquisition_cycle", bind=True)
def run_acquisition_cycle(
    self: Any, *, cycle_key: str | None = None, max_pages: int | None = None
) -> dict[str, Any]:
    """Drain the queue for one cycle. Politeness is enforced inside `run_cycle`."""
    del self
    key = cycle_key or cycle_key_for()
    engine = _engine()
    worker = f"celery:{uuid.uuid4().hex[:8]}"
    try:
        report = asyncio.run(
            run_cycle(engine, cycle_key=key, store=_store(), worker=worker, max_pages=max_pages)
        )
    finally:
        engine.dispose()
    return {
        "cycle_key": report.cycle_key,
        "attempted": report.attempted,
        "ok": report.ok,
        "unchanged": report.unchanged,
        "http_error": report.http_error,
        "blocked": report.blocked,
        "timeout": report.timeout,
        "lease_lost": report.lease_lost,
        "snapshots": report.snapshots,
        "blobs_created": report.blobs_created,
        "hosts": len(report.hosts_touched),
    }


@celery_app.task(name="app.workers.tasks.crawl.sweep_expired_leases", bind=True)
def sweep_expired_leases(self: Any) -> dict[str, Any]:
    """Reap attempts whose worker stopped heartbeating.

    A crash becomes a permanent, queryable `fetch_run` with status ABANDONED, rather
    than a row sitting in RUNNING forever that a future maintainer resets by hand.
    """
    del self
    engine = _engine()
    try:
        abandoned = sweep_and_report(engine)
    finally:
        engine.dispose()
    return {"abandoned": len(abandoned)}


__all__ = [
    "cycle_key_for",
    "enqueue_pilot_cycle",
    "run_acquisition_cycle",
    "sweep_expired_leases",
]
