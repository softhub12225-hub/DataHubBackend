"""Claiming, heartbeating and finalising fetch work (Step 5B sections 5-6).

THE RACE THIS EXISTS TO LOSE SAFELY
===================================
Worker A claims an attempt and starts fetching. Its process stalls -- a long GC pause,
a hypervisor freeze, a network partition -- past `lease_expires_at`. The sweeper does
its job: the attempt becomes ABANDONED and a terminal `fetch_run` records the
abandonment. A new attempt is queued and worker B claims it. Then A wakes up, finishes
its fetch, and finalises.

Without fencing, A wins: it writes an authoritative `fetch_run` and a `snapshot` for
work it no longer owns, possibly over B's. No comparison of timestamps prevents this,
because the clock disagreement is what caused the stall in the first place.

`lease_token` is the fence. Every successful claim mints a fresh UUID. Every heartbeat
and every finalisation is a **conditional write**::

    UPDATE fetch_attempt
       SET ...
     WHERE id = :id AND state = 'RUNNING' AND lease_token = :token

A worker holding a stale token updates zero rows, and this module turns that into
`LeaseLostError` rather than letting it pass silently. The fenced update is the *first*
statement of the finalisation transaction, so a lost lease means nothing downstream is
written at all -- no run, no snapshot, no blob row.

EXACTLY ONE TERMINAL RUN PER ATTEMPT
====================================
Two independent guarantees, because one would be a single point of failure:

1. The fenced update requires `state = 'RUNNING'`, and the state transition and the
   run insert share a transaction. Whoever loses the update never reaches the insert.
2. `uq_fetch_run_attempt_id` refuses a second run for the same attempt regardless.

The sweeper and a late finaliser therefore cannot both produce a terminal record, and
if the first guarantee were ever weakened the second would still hold.

RETRIES ARE NEW ATTEMPTS
========================
Never a state reset. A retry inserts a fresh `fetch_attempt` with the next
`attempt_no`, so the failed try keeps its own `fetch_run` and the history shows what
actually happened rather than only how it ended.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.enums import FetchStatus

logger = get_logger(__name__)

#: How long a claim is good for without a heartbeat. Comfortably longer than the
#: fetcher's total budget, so a slow-but-alive fetch is never reaped mid-request.
DEFAULT_LEASE_SECONDS = 180

#: How many times one physical page is retried inside a cycle before it is left alone.
#: Low on purpose: a page that fails three times is telling us something, and the
#: right response is a worklist entry, not more requests.
MAX_ATTEMPTS_PER_CYCLE = 3


class LeaseLostError(RuntimeError):
    """This worker no longer owns the attempt. Nothing it produced may be written.

    Raised rather than returned, because every caller's correct response is to stop,
    and a boolean would eventually be ignored at one call site.
    """


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof of ownership. The token is the only thing that authorises a write."""

    attempt_id: uuid.UUID
    source_id: uuid.UUID
    url: str
    cycle_key: str
    attempt_no: int
    lease_token: uuid.UUID
    lease_generation: int
    expires_at: datetime
    worker: str


@dataclass(slots=True)
class EnqueueReport:
    """What an enqueue did, in the terms the operator asked in."""

    cycle_key: str
    physical_pages_selected: int = 0
    hosts: int = 0
    duplicate_responsibilities_skipped: int = 0
    already_queued: int = 0
    blocked_or_disabled: int = 0
    #: Fetchable, but not yet: a source or its host is owed a pause (Step 5B.2 §2).
    #: Counted apart from `blocked_or_disabled` because the two need opposite
    #: responses -- one waits on the clock, the other on a person.
    in_cooldown: int = 0
    attempts_created: int = 0
    max_attempts_reached: int = 0

    def summary(self) -> str:
        return (
            f"cycle {self.cycle_key}: {self.attempts_created} attempt(s) created over "
            f"{self.physical_pages_selected} physical page(s) across {self.hosts} host(s); "
            f"{self.duplicate_responsibilities_skipped} duplicate responsibilit"
            f"{'y' if self.duplicate_responsibilities_skipped == 1 else 'ies'} needed no "
            f"second fetch; {self.already_queued} already queued, "
            f"{self.blocked_or_disabled} not fetchable, "
            f"{self.in_cooldown} in cooldown"
        )


def enqueue_cycle(
    connection: Connection,
    *,
    cycle_key: str,
    source_ids: list[uuid.UUID] | None = None,
    target_institution_id: uuid.UUID | None = None,
    pilot_only: bool = True,
    limit: int | None = None,
) -> EnqueueReport:
    """Queue one attempt per **physical page**, once per cycle.

    Scheduling is over `acquisition_target`, which is one row per distinct URL. A page
    claimed by three responsibilities is one row there and therefore one fetch: the
    duplicate claims ride on the same observation rather than causing the university
    to be asked the same question three times.

    Idempotent within a cycle. `uq_fetch_attempt_source_cycle_attempt` makes a second
    enqueue a no-op rather than a second fetch, which matters because "run the 06:00
    cycle" is the kind of command an operator runs twice.
    """
    report = EnqueueReport(cycle_key=cycle_key)

    # One statement with NULL-guarded predicates rather than an assembled WHERE
    # clause: the filters are optional, and a query built by string concatenation is
    # the shape a later "just add one more filter" turns into an injection.
    rows = connection.execute(
        text(
            """
            SELECT t.source_id, t.host, t.fetch_eligibility, t.claim_count,
                   t.url, t.access_state, h.schedule_state
              FROM acquisition_target t
              LEFT JOIN source_health h ON h.source_id = t.source_id
             WHERE t.is_active
               AND (cast(:source_ids AS uuid[]) IS NULL OR t.source_id = ANY(:source_ids))
               AND (cast(:institution AS uuid) IS NULL OR t.target_institution_id = :institution)
               AND (NOT :pilot_only OR t.pilot_wave IS NOT NULL)
             ORDER BY t.host, t.source_id
            """
        ),
        {
            "source_ids": list(source_ids) if source_ids is not None else None,
            "institution": target_institution_id,
            "pilot_only": pilot_only,
        },
    ).all()

    report.physical_pages_selected = len(rows)
    report.hosts = len({row.host for row in rows})
    report.duplicate_responsibilities_skipped = sum(max(0, row.claim_count - 1) for row in rows)

    created = 0
    for row in rows:
        if row.fetch_eligibility != "FETCHABLE" or row.access_state != "OK":
            report.blocked_or_disabled += 1
            continue
        if row.schedule_state == "COOLDOWN":
            # Queueing it now would make the cooldown a formality: the attempt would
            # sit QUEUED and `claim_next` would refuse it, but the report would claim
            # the page was scheduled. Counted honestly instead.
            report.in_cooldown += 1
            continue
        if limit is not None and created >= limit:
            break

        existing = connection.execute(
            text("SELECT count(*) FROM fetch_attempt WHERE source_id = :s AND cycle_key = :c"),
            {"s": row.source_id, "c": cycle_key},
        ).scalar_one()
        if existing:
            report.already_queued += 1
            continue

        connection.execute(
            text(
                "INSERT INTO fetch_attempt (id, source_id, attempt_no, cycle_key, state, "
                "scheduled_for) VALUES (:id, :s, 1, :c, 'QUEUED', now())"
            ),
            {"id": uuid.uuid4(), "s": row.source_id, "c": cycle_key},
        )
        created += 1

    report.attempts_created = created
    logger.info(
        "acquisition_cycle_enqueued",
        cycle_key=cycle_key,
        pages=report.physical_pages_selected,
        created=created,
        hosts=report.hosts,
    )
    return report


def claim_next(
    connection: Connection,
    *,
    cycle_key: str,
    worker: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    host: str | None = None,
) -> Lease | None:
    """Take ownership of one queued attempt, or return None.

    `FOR UPDATE SKIP LOCKED` inside a single statement is what makes two workers
    unable to claim the same row: the second skips it rather than blocking, so
    concurrency costs nothing and correctness costs nothing either.

    The returned token is minted here and stored on the row in the same statement, so
    there is no window in which a row is RUNNING without a fence.

    `cycle_key` is **required and filtered**, not a label. The UPDATE never writes
    `cycle_key`, so a claimed attempt keeps the cycle it was queued under and a retry
    scheduled from it stays in the same cycle.
    """
    token = uuid.uuid4()
    expires = utcnow() + timedelta(seconds=lease_seconds)

    row = connection.execute(
        text(
            """
            WITH candidate AS (
                SELECT a.id
                  FROM fetch_attempt a
                  JOIN source s ON s.id = a.source_id
                 WHERE a.state = 'QUEUED'
                   -- Step 5B.2 §6. `cycle_key` used to be a label the caller passed
                   -- and the query ignored, so `run_cycle(K)` drained every due
                   -- attempt in the table and reported them all as K's. Mixing a
                   -- smoke cycle into the full pilot run that way is the kind of
                   -- thing you discover afterwards, from counts that do not add up.
                   AND a.cycle_key = :cycle_key
                   AND (a.scheduled_for IS NULL OR a.scheduled_for <= now())
                   AND s.is_active
                   AND s.fetch_eligibility = 'FETCHABLE'
                   -- `enqueue_cycle` and `source_health` both honour `access_state`,
                   -- and this did not, so a worker could claim a page the scheduler
                   -- and the operator's report agreed was BLOCKED.
                   AND s.access_state = 'OK'
                   -- Operational timing, separate from eligibility: a source in
                   -- cooldown is perfectly fetchable, just not yet (§2).
                   AND (s.cooldown_until IS NULL OR s.cooldown_until <= now())
                   -- And a host we owe a pause to is not asked for anything, however
                   -- many other pages of its we have queued (§13).
                   AND NOT EXISTS (
                         SELECT 1 FROM host_cooldown hc
                          WHERE hc.cooldown_until > now()
                            AND hc.host = lower(split_part(
                                  split_part(s.url, '://', 2), '/', 1)))
                   AND (cast(:host AS text) IS NULL OR split_part(
                         split_part(s.url, '://', 2), '/', 1) = :host)
                 ORDER BY a.scheduled_for NULLS FIRST, a.created_at
                 FOR UPDATE OF a SKIP LOCKED
                 LIMIT 1
            )
            UPDATE fetch_attempt a
               SET state = 'RUNNING',
                   claimed_at = now(),
                   claimed_by = :worker,
                   heartbeat_at = now(),
                   lease_expires_at = :expires,
                   lease_token = :token,
                   lease_generation = a.lease_generation + 1
              FROM candidate c, source s
             WHERE a.id = c.id AND s.id = a.source_id
            RETURNING a.id, a.source_id, s.url, a.cycle_key, a.attempt_no,
                      a.lease_token, a.lease_generation, a.lease_expires_at
            """
        ),
        {
            "worker": worker,
            "expires": expires,
            "token": token,
            "host": host,
            "cycle_key": cycle_key,
        },
    ).one_or_none()

    if row is None:
        return None
    return Lease(
        attempt_id=row.id,
        source_id=row.source_id,
        url=row.url,
        cycle_key=row.cycle_key,
        attempt_no=row.attempt_no,
        lease_token=row.lease_token,
        lease_generation=row.lease_generation,
        expires_at=row.lease_expires_at,
        worker=worker,
    )


def heartbeat(
    connection: Connection, lease: Lease, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> datetime:
    """Extend a lease, or raise if it is no longer ours.

    Conditional on the token, so a worker whose lease was swept cannot quietly keep
    the row alive and then finalise -- which is the failure this whole module is for.
    """
    expires = utcnow() + timedelta(seconds=lease_seconds)
    updated = connection.execute(
        text(
            "UPDATE fetch_attempt "
            "   SET heartbeat_at = now(), lease_expires_at = :expires "
            " WHERE id = :id AND state = 'RUNNING' AND lease_token = :token "
            "RETURNING lease_expires_at"
        ),
        {"id": lease.attempt_id, "token": lease.lease_token, "expires": expires},
    ).one_or_none()
    if updated is None:
        raise LeaseLostError(
            f"attempt {lease.attempt_id} is no longer held by {lease.worker}; "
            "it was swept, finalised or re-leased. Nothing this worker produced "
            "may be written."
        )
    return expires


def release_lease(connection: Connection, lease: Lease, *, reason: str) -> None:
    """Give an attempt back without finalising it, e.g. on a deliberate shutdown.

    Returns it to QUEUED and clears the token, so the next claim mints a new one and
    this worker's token can never be replayed.
    """
    connection.execute(
        text(
            "UPDATE fetch_attempt "
            "   SET state = 'QUEUED', claimed_at = NULL, claimed_by = NULL, "
            "       lease_expires_at = NULL, lease_token = NULL, "
            "       scheduled_for = now() "
            " WHERE id = :id AND state = 'RUNNING' AND lease_token = :token"
        ),
        {"id": lease.attempt_id, "token": lease.lease_token},
    )
    logger.info("acquisition_lease_released", attempt_id=str(lease.attempt_id), reason=reason)


def take_ownership_for_finalisation(connection: Connection, lease: Lease) -> None:
    """Fenced transition to FINALIZED. **Must be the first write of the transaction.**

    Ordering matters: doing this first means a lost lease aborts before anything
    downstream exists. Writing the run first and checking the fence afterwards would
    leave the check as the only thing standing between a stale worker and an
    authoritative record, and a later refactor would eventually move it.
    """
    updated = connection.execute(
        text(
            "UPDATE fetch_attempt "
            "   SET state = 'FINALIZED', finalized_at = now() "
            " WHERE id = :id AND state = 'RUNNING' AND lease_token = :token "
            "RETURNING id"
        ),
        {"id": lease.attempt_id, "token": lease.lease_token},
    ).one_or_none()
    if updated is None:
        raise LeaseLostError(
            f"attempt {lease.attempt_id} cannot be finalised by {lease.worker}: "
            "the lease was lost. No fetch_run, snapshot or blob will be written."
        )


def oldest_open_cycle(connection: Connection) -> str | None:
    """The earliest cycle that still has queued work, or ``None`` if there is none.

    WHY A BOUNDED WORKER CANNOT JUST ASK FOR "THIS HOUR"
    ====================================================
    `claim` filters on `cycle_key` and never rewrites it, so an attempt queued under
    one cycle can only ever be claimed by a worker asking for that same cycle. That is
    correct -- it is what keeps a retry inside the check it belongs to.

    It also means a worker that defaults to the current hour can only do work that was
    enqueued in the current hour. A scheduled crawl is not shaped like that: enqueueing
    319 pages takes a moment and draining them takes hours, because `--max-pages`
    bounds each run so it exits inside its cron window. The second run lands in the
    next hour, asks for a cycle nothing was queued under, and reports "0 attempted"
    while the queue sits full.

    That is not hypothetical -- it stranded 304 of 324 attempts on the first deployed
    run. Twenty were fetched in the half hour the enqueue and the worker happened to
    share, and every hourly run afterwards found nothing, hour after hour, reporting
    success each time.

    So the worker asks the queue what to work on rather than assuming. Oldest first,
    so a cycle is finished before a newer one is started and no cycle is abandoned
    half-done.
    """
    row = connection.execute(
        text(
            "SELECT cycle_key FROM fetch_attempt "
            " WHERE state = 'QUEUED' "
            " ORDER BY scheduled_for, cycle_key "
            " LIMIT 1"
        )
    ).first()
    return str(row.cycle_key) if row is not None else None


def sweep_expired(
    connection: Connection, *, now: datetime | None = None, limit: int = 100
) -> list[uuid.UUID]:
    """Mark timed-out attempts ABANDONED and record each as a terminal run.

    A crash becomes a permanent, queryable fact rather than a row stuck in RUNNING.
    The sweep takes the same fenced transition -- `state = 'RUNNING'` -- so a worker
    finalising at the same moment is a race exactly one of them wins.
    """
    moment = now or utcnow()
    rows = connection.execute(
        text(
            "SELECT id, source_id, attempt_no, scheduled_for, claimed_at, claimed_by "
            "  FROM fetch_attempt "
            " WHERE state = 'RUNNING' AND lease_expires_at < :now "
            " ORDER BY lease_expires_at LIMIT :limit "
            "   FOR UPDATE SKIP LOCKED"
        ),
        {"now": moment, "limit": limit},
    ).all()

    abandoned: list[uuid.UUID] = []
    for row in rows:
        won = connection.execute(
            text(
                "UPDATE fetch_attempt SET state = 'ABANDONED', finalized_at = now() "
                " WHERE id = :id AND state = 'RUNNING' RETURNING id"
            ),
            {"id": row.id},
        ).one_or_none()
        if won is None:
            continue
        connection.execute(
            text(
                "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, "
                "scheduled_for, started_at, finished_at, status, fetcher, error_class, "
                "worker_name) VALUES (:id, :attempt, :source, :no, :sched, :started, "
                "now(), :status, 'STATIC', 'LeaseExpired', :worker)"
            ),
            {
                "id": uuid.uuid4(),
                "attempt": row.id,
                "source": row.source_id,
                "no": row.attempt_no,
                "sched": row.scheduled_for,
                "started": row.claimed_at or moment,
                "status": FetchStatus.ABANDONED.value,
                "worker": row.claimed_by,
            },
        )
        abandoned.append(row.id)

    if abandoned:
        logger.warning("acquisition_leases_swept", count=len(abandoned))
    return abandoned


def schedule_retry(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    cycle_key: str,
    delay_seconds: float,
    max_attempts: int = MAX_ATTEMPTS_PER_CYCLE,
) -> uuid.UUID | None:
    """Queue a fresh attempt for a failed page, or decline to.

    A new row with the next `attempt_no`, never a reset of the old one -- the failed
    try keeps its own terminal run, so the history shows three tries rather than one
    that eventually worked.

    Returns None at the cap. A page failing repeatedly is a worklist item; more
    requests are the one response that makes it worse, particularly if what it is
    saying is 429.
    """
    highest = connection.execute(
        text(
            "SELECT coalesce(max(attempt_no), 0) FROM fetch_attempt "
            " WHERE source_id = :s AND cycle_key = :c"
        ),
        {"s": source_id, "c": cycle_key},
    ).scalar_one()
    if highest >= max_attempts:
        logger.info(
            "acquisition_retry_declined",
            source_id=str(source_id),
            cycle_key=cycle_key,
            attempts=highest,
        )
        return None

    attempt_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO fetch_attempt (id, source_id, attempt_no, cycle_key, state, "
            "scheduled_for) VALUES (:id, :s, :no, :c, 'QUEUED', "
            "now() + make_interval(secs => :delay))"
        ),
        {
            "id": attempt_id,
            "s": source_id,
            "no": highest + 1,
            "c": cycle_key,
            "delay": float(delay_seconds),
        },
    )
    return attempt_id


def backoff_seconds(attempt_no: int, *, retry_after: float | None = None) -> float:
    """How long before trying again.

    `Retry-After` wins outright when the server sent one: it is the site telling us
    what it wants, and a shorter locally-computed delay is how a temporary throttle
    becomes a block. Otherwise exponential with a ceiling.
    """
    if retry_after is not None:
        return max(1.0, min(retry_after, 3600.0))
    return float(min(300, 15 * (2 ** max(0, attempt_no - 1))))


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MAX_ATTEMPTS_PER_CYCLE",
    "EnqueueReport",
    "Lease",
    "LeaseLostError",
    "backoff_seconds",
    "claim_next",
    "enqueue_cycle",
    "heartbeat",
    "oldest_open_cycle",
    "release_lease",
    "schedule_retry",
    "sweep_expired",
    "take_ownership_for_finalisation",
]
