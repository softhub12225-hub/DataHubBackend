"""Running one acquisition cycle politely (Step 5B sections 13-14, 22-23).

POLITENESS IS THE DESIGN, NOT A KNOB
====================================
120 hosts serve the pilot's 319 pages, and one of them serves eleven. A naive worker
pool would open eleven simultaneous connections to a single university and look exactly
like something to block -- and blocking us would be the correct decision on their part.

`HostGate` allows **one** in-flight request per host and enforces a floor between
consecutive requests to the same host. Global concurrency is bounded separately, so
throughput comes from working many hosts at once rather than any host harder.

429 and `Retry-After` are obeyed as stated: the server's number wins over ours, always.
A source that keeps refusing becomes `BLOCKED` and stops being scheduled, which is the
only correct response -- there is no proxy rotation, no user-agent cycling and no
CAPTCHA handling anywhere in this package, and their absence is the point.

WHAT A CYCLE PRODUCES
=====================
Immutable evidence and nothing else: a `fetch_run` per attempt, a `snapshot` per
observation that carried bytes, a `content_blob` per distinct sequence of bytes. No
`field_claim`, no `extraction`, no `change_proposal`, no canonical row. The pages are
preserved; nothing has been read out of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, text

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.enums import FetchEligibility, FetchStatus
from app.domains.acquisition.fetcher import FetchOutcome, StaticHttpFetcher
from app.domains.acquisition.lease import (
    DEFAULT_LEASE_SECONDS,
    Lease,
    LeaseLostError,
    backoff_seconds,
    claim_next,
    schedule_retry,
)
from app.domains.acquisition.recorder import (
    conditional_headers_for,
    record_internal_error,
    record_outcome,
)
from app.domains.acquisition.storage import EvidenceStore

logger = get_logger(__name__)

#: Statuses that will not change by asking again. Retrying them costs the university
#: requests and tells us nothing we do not already know.
PERMANENT_STATUSES: frozenset[int] = frozenset({404, 410})

#: How long a `429` with no usable `Retry-After` quiets a source. Deliberately long:
#: the site said "too many" and gave us no number, so guessing small is guessing in
#: the direction that gets us blocked.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 900.0

#: A temporary resolver failure earns a shorter pause -- nothing was learned about the
#: target and nobody was inconvenienced by the attempt.
DEFAULT_DNS_COOLDOWN_SECONDS = 300.0

#: Consecutive `429`s before the source stops being retried automatically and waits
#: for a person. Four, not one: a single throttle is normal traffic shaping, and four
#: in a row is the site telling us something about our whole approach to it. Reset by
#: any success, so this counts a pattern rather than a lifetime total.
RATE_LIMIT_STRIKE_LIMIT = 4

#: A `429` is a statement about the server, so the pause applies to the host. Without
#: this, honouring a throttle on the page that received it and then immediately asking
#: for the other ten pages of that university is not honouring it at all (§13).
HOST_COOLDOWN_ON_RATE_LIMIT = True


@dataclass(slots=True)
class CycleReport:
    """What one worker run did. The counts an operator asks for."""

    cycle_key: str
    attempted: int = 0
    ok: int = 0
    unchanged: int = 0
    http_error: int = 0
    blocked: int = 0
    timeout: int = 0
    rate_limited: int = 0
    dns_temporary: int = 0
    name_not_resolved: int = 0
    internal_error: int = 0
    lease_lost: int = 0
    #: An internal error we could not even record, because the lease was gone or the
    #: database was unavailable. The attempt is left RUNNING for the sweeper (§10).
    unrecorded: int = 0
    cooldowns_set: int = 0
    hosts_paused: int = 0
    escalated_to_review: int = 0
    snapshots: int = 0
    blobs_created: int = 0
    retries_scheduled: int = 0
    hosts_touched: set[str] = field(default_factory=set)
    urls_contacted: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"cycle {self.cycle_key}: {self.attempted} attempted over "
            f"{len(self.hosts_touched)} host(s) -- {self.ok} ok, {self.unchanged} unchanged, "
            f"{self.http_error} http error, {self.blocked} blocked, {self.timeout} timeout, "
            f"{self.rate_limited} rate limited, {self.dns_temporary} dns temporary, "
            f"{self.name_not_resolved} name not resolved, "
            f"{self.internal_error} internal error, {self.lease_lost} lease lost, "
            f"{self.unrecorded} unrecorded; {self.snapshots} snapshot(s), "
            f"{self.blobs_created} new blob(s); {self.cooldowns_set} cooldown(s), "
            f"{self.hosts_paused} host pause(s), "
            f"{self.escalated_to_review} escalated to review"
        )


class HostGate:
    """One request at a time per host, with a floor between them.

    Per-host rather than global because the constraint being respected is a
    university's, not ours: twelve requests spread over twelve hosts is polite,
    and twelve to one host is not, regardless of the global rate.
    """

    def __init__(self, *, min_interval_seconds: float = 2.0) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}
        self._paused_until: dict[str, float] = {}

    @contextlib.asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        async with self._locks[host]:
            loop = asyncio.get_running_loop()
            now = loop.time()
            # A `Retry-After` pause outlives the interval floor: the site named a
            # time, and shortening it is how a throttle becomes a block.
            wait = max(
                self._paused_until.get(host, 0.0) - now,
                self._last_request.get(host, 0.0) + self.min_interval_seconds - now,
                0.0,
            )
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._last_request[host] = asyncio.get_running_loop().time()

    def pause(self, host: str, seconds: float) -> None:
        """Hold off this host, because it asked us to."""
        loop = asyncio.get_running_loop()
        self._paused_until[host] = max(self._paused_until.get(host, 0.0), loop.time() + seconds)
        logger.info("acquisition_host_paused", host=host, seconds=round(seconds, 1))


async def run_cycle(
    engine: Engine,
    *,
    cycle_key: str,
    store: EvidenceStore,
    worker: str,
    fetcher: StaticHttpFetcher | None = None,
    gate: HostGate | None = None,
    max_pages: int | None = None,
    global_concurrency: int = 4,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> CycleReport:
    """Claim and process queued attempts until there are none left.

    `global_concurrency` is deliberately small. The bound that matters is per-host,
    and a large pool mostly produces workers queueing behind each other's host locks.
    """
    http = fetcher or StaticHttpFetcher()
    host_gate = gate or HostGate()
    report = CycleReport(cycle_key=cycle_key)
    semaphore = asyncio.Semaphore(global_concurrency)
    processed = 0

    while max_pages is None or processed < max_pages:
        with engine.begin() as connection:
            lease = claim_next(
                connection,
                cycle_key=cycle_key,
                worker=worker,
                lease_seconds=lease_seconds,
            )
        if lease is None:
            break
        processed += 1
        async with semaphore:
            await _process_one(engine, lease, http, host_gate, store, report, worker)

    logger.info(
        "acquisition_cycle_complete",
        cycle_key=cycle_key,
        attempted=report.attempted,
        ok=report.ok,
        unchanged=report.unchanged,
        blocked=report.blocked,
    )
    return report


async def _process_one(
    engine: Engine,
    lease: Lease,
    http: StaticHttpFetcher,
    gate: HostGate,
    store: EvidenceStore,
    report: CycleReport,
    worker: str,
) -> None:
    """Process one attempt, and let nothing it does end the cycle (§8, §11).

    WHY THE BOUNDARY IS HERE
    =======================
    It used to catch only `LeaseLostError`, so any other exception -- a disk error in
    `store.put`, a bug in a parser, anything -- propagated out of `run_cycle` and the
    cycle stopped. With 319 pages queued, a defect on page 40 meant 279 universities
    went unvisited and the operator got a traceback instead of a report.

    `except Exception` and not `BaseException`: `KeyboardInterrupt`, `SystemExit` and
    `asyncio.CancelledError` all derive from `BaseException`, so an operator pressing
    Ctrl-C still stops the run and cancellation still propagates. Swallowing those
    would make the worker unkillable, which is a worse failure than the one being
    fixed.
    """
    try:
        await _attempt_one(engine, lease, http, gate, store, report, worker)
    except LeaseLostError as exc:
        # The expected outcome for a worker that stalled. Nothing was written, which
        # is the whole point of the fence, so this is logged and not re-raised.
        report.lease_lost += 1
        logger.warning(
            "acquisition_lease_lost",
            attempt_id=str(lease.attempt_id),
            worker=worker,
            detail=str(exc),
        )
    except Exception as exc:
        _contain_internal_error(engine, lease, exc, report, worker, fetcher=http.fetcher_name)


def _contain_internal_error(
    engine: Engine,
    lease: Lease,
    exc: Exception,
    report: CycleReport,
    worker: str,
    *,
    fetcher: str = "STATIC",
) -> None:
    """Record our own failure as terminal history, or leave it to the sweeper (§9-10).

    If the lease is still ours, the attempt gets an immutable `INTERNAL_ERROR` run so
    the page is visibly *not fetched* rather than silently skipped. If it is not --
    the lease expired, or the database is the thing that broke -- then **nothing is
    manufactured**: the attempt stays `RUNNING` and the existing sweeper closes it out
    as `ABANDONED`, which is exactly the history that describes what happened.
    """
    report.internal_error += 1
    try:
        with engine.begin() as connection:
            record_internal_error(connection, lease=lease, error=exc, fetcher=fetcher)
    except LeaseLostError:
        report.internal_error -= 1
        report.lease_lost += 1
        logger.warning(
            "acquisition_internal_error_not_recorded",
            reason="lease lost before finalisation",
            attempt_id=str(lease.attempt_id),
            worker=worker,
        )
    except Exception:
        # The database is unavailable or the transaction failed. Inventing a
        # `fetch_run` here would be asserting a terminal outcome we could not write.
        report.internal_error -= 1
        report.unrecorded += 1
        logger.exception(
            "acquisition_internal_error_not_recorded",
            reason="could not finalise; left RUNNING for the lease sweeper",
            attempt_id=str(lease.attempt_id),
            worker=worker,
        )


async def _attempt_one(
    engine: Engine,
    lease: Lease,
    http: StaticHttpFetcher,
    gate: HostGate,
    store: EvidenceStore,
    report: CycleReport,
    worker: str,
) -> None:
    host = _host_of(lease.url)
    report.hosts_touched.add(host)
    report.attempted += 1

    with engine.begin() as connection:
        conditional = conditional_headers_for(connection, lease.source_id)

    async with gate.acquire(host):
        report.urls_contacted.append(lease.url)
        outcome = await http.fetch(lease.url, conditional=conditional)

    # In-process pause as well as the persisted one: this cycle should stop asking
    # immediately, and `host_cooldown` is what the *next* cycle reads.
    if outcome.status is FetchStatus.RATE_LIMITED:
        gate.pause(host, outcome.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)

    with engine.begin() as connection:
        recorded = record_outcome(
            connection,
            lease=lease,
            outcome=outcome,
            store=store,
            fetcher=http.fetcher_name,
            previous_content_hash=conditional.content_hash,
        )
        if recorded.snapshot_id is not None:
            report.snapshots += 1
        if recorded.blob_created:
            report.blobs_created += 1
        _tally(report, outcome.status)
        _react(connection, lease, outcome, report, host=host)


def _tally(report: CycleReport, status: FetchStatus) -> None:
    match status:
        case FetchStatus.OK:
            report.ok += 1
        case FetchStatus.UNCHANGED:
            report.unchanged += 1
        case FetchStatus.BLOCKED:
            report.blocked += 1
        case FetchStatus.TIMEOUT:
            report.timeout += 1
        case FetchStatus.RATE_LIMITED:
            report.rate_limited += 1
        case FetchStatus.DNS_TEMPORARY:
            report.dns_temporary += 1
        case FetchStatus.NAME_NOT_RESOLVED:
            report.name_not_resolved += 1
        case FetchStatus.INTERNAL_ERROR:
            report.internal_error += 1
        case _:
            report.http_error += 1


def _set_cooldown(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    seconds: float,
    reason: str,
) -> None:
    """Hold a source back for a while. Touches timing only, never eligibility."""
    connection.execute(
        text(
            "UPDATE source SET cooldown_until = now() + make_interval(secs => :secs), "
            "cooldown_reason = :reason, cooldown_set_at = now() WHERE id = :id"
        ),
        {"secs": float(seconds), "reason": reason[:400], "id": source_id},
    )


def _clear_cooldown(connection: Connection, *, source_id: uuid.UUID) -> None:
    """A success means the pause and the strike count have served their purpose.

    This is the automatic half of recovery (§4): nothing has to be done by hand for a
    source that was merely throttled and is now answering again. It deliberately does
    **not** touch `fetch_eligibility` -- a human decision is not undone by a fetch
    happening to succeed.
    """
    connection.execute(
        text(
            "UPDATE source SET cooldown_until = NULL, cooldown_reason = NULL, "
            "cooldown_set_at = NULL, rate_limit_strikes = 0 "
            " WHERE id = :id AND (cooldown_until IS NOT NULL OR rate_limit_strikes <> 0)"
        ),
        {"id": source_id},
    )


def _set_eligibility_if_unjudged(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    state: FetchEligibility,
    reason: str,
    access_state: str | None = None,
) -> bool:
    """Record a machine's judgement, but never overwrite a person's (§4).

    The `fetch_eligibility = 'FETCHABLE'` predicate is the whole point: a source a
    person has DISABLED, or already put in NEEDS_MANUAL_REVIEW, or that is already
    BLOCKED, keeps that state and its original reason. A worker that could silently
    reset it would make the operator's decision advisory.
    """
    # `access_state` moves only when asked: a source refused by the site is
    # `BLOCKED` there too, but one parked for human review has had nothing said about
    # its access at all, and conflating those would report a review queue as a set of
    # refusals.
    updated = connection.execute(
        text(
            "UPDATE source SET fetch_eligibility = :state, "
            "fetch_eligibility_reason = :reason, fetch_eligibility_set_at = now(), "
            "access_state = coalesce(cast(:access AS source_access_state), access_state) "
            " WHERE id = :id AND fetch_eligibility = 'FETCHABLE'"
        ),
        {
            "state": state.value,
            "reason": reason[:400],
            "access": access_state,
            "id": source_id,
        },
    ).rowcount
    return bool(updated)


def _pause_host(
    connection: Connection,
    *,
    host: str,
    seconds: float,
    reason: str,
    source_id: uuid.UUID,
) -> None:
    """Quiet a whole host, because a 429 is about the server (§13).

    `GREATEST` on conflict: two pages of one host throttled in the same cycle must not
    let the second, shorter pause shorten the first. A cooldown only ever extends.
    """
    connection.execute(
        text(
            "INSERT INTO host_cooldown (host, cooldown_until, reason, "
            "                           triggered_by_source_id) "
            "VALUES (:host, now() + make_interval(secs => :secs), :reason, :source) "
            "ON CONFLICT (host) DO UPDATE SET "
            "  cooldown_until = GREATEST(host_cooldown.cooldown_until, EXCLUDED.cooldown_until), "
            "  reason = EXCLUDED.reason, set_at = now(), "
            "  triggered_by_source_id = EXCLUDED.triggered_by_source_id"
        ),
        {"host": host.lower(), "secs": float(seconds), "reason": reason[:400], "source": source_id},
    )


def _react(
    connection: Connection,
    lease: Lease,
    outcome: FetchOutcome,
    report: CycleReport,
    *,
    host: str | None = None,
) -> None:
    """Operational consequences of one outcome. Never a trust consequence.

    Nothing here touches `publication_eligibility`, `source_mapping` or any canonical
    row: a page answering 200 is not evidence that it is the right page, and a
    redirect to another host is a finding for a reviewer rather than a promotion.

    The branches are ordered by what the outcome *means*, and the distinction Step
    5B.2 exists to draw runs through all of them: a site asking us to wait, a resolver
    that did not answer, and a site refusing us are three different things, and only
    the last is permanent.
    """
    host = host or _host_of(lease.url)

    if outcome.status in (FetchStatus.OK, FetchStatus.UNCHANGED):
        _clear_cooldown(connection, source_id=lease.source_id)
        return

    if outcome.status is FetchStatus.RATE_LIMITED:
        # The site's number wins outright. Ours is only a fallback for a 429 that
        # named none, and it is deliberately generous.
        delay = outcome.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        strikes = connection.execute(
            text(
                "UPDATE source SET rate_limit_strikes = rate_limit_strikes + 1 "
                " WHERE id = :id RETURNING rate_limit_strikes"
            ),
            {"id": lease.source_id},
        ).scalar_one()
        _set_cooldown(
            connection,
            source_id=lease.source_id,
            seconds=delay,
            reason=f"HTTP 429; Retry-After {outcome.retry_after_seconds or 'absent'}",
        )
        report.cooldowns_set += 1
        if HOST_COOLDOWN_ON_RATE_LIMIT:
            _pause_host(
                connection,
                host=host,
                seconds=delay,
                reason=f"HTTP 429 on {lease.url}",
                source_id=lease.source_id,
            )
            report.hosts_paused += 1

        if strikes >= RATE_LIMIT_STRIKE_LIMIT:
            # Still not BLOCKED: the site has not refused us, it has asked us to slow
            # down more times than a schedule should ignore. That is a question about
            # our cadence, and a person should answer it.
            if _set_eligibility_if_unjudged(
                connection,
                source_id=lease.source_id,
                state=FetchEligibility.NEEDS_MANUAL_REVIEW,
                reason=f"rate limited {strikes} times in a row; review the crawl cadence",
            ):
                report.escalated_to_review += 1
            logger.warning(
                "acquisition_rate_limit_escalated",
                source_id=str(lease.source_id),
                strikes=strikes,
            )
            return

        # A retry is a NEW attempt, scheduled no earlier than the cooldown. Never a
        # retry inside this worker loop: that would be asking again immediately,
        # which is the opposite of what a 429 requested.
        if schedule_retry(
            connection,
            source_id=lease.source_id,
            cycle_key=lease.cycle_key,
            delay_seconds=delay,
        ):
            report.retries_scheduled += 1
        return

    if outcome.status is FetchStatus.DNS_TEMPORARY:
        delay = backoff_seconds(lease.attempt_no)
        _set_cooldown(
            connection,
            source_id=lease.source_id,
            seconds=max(delay, DEFAULT_DNS_COOLDOWN_SECONDS),
            reason=f"temporary DNS failure: {outcome.error_class}",
        )
        report.cooldowns_set += 1
        if schedule_retry(
            connection,
            source_id=lease.source_id,
            cycle_key=lease.cycle_key,
            delay_seconds=max(delay, DEFAULT_DNS_COOLDOWN_SECONDS),
        ):
            report.retries_scheduled += 1
        return

    if outcome.status is FetchStatus.NAME_NOT_RESOLVED:
        # No number of retries invents a hostname. The URL is wrong, or the
        # institution has retired the name, and either way a person decides.
        if _set_eligibility_if_unjudged(
            connection,
            source_id=lease.source_id,
            state=FetchEligibility.NEEDS_MANUAL_REVIEW,
            reason=f"hostname does not resolve: {outcome.error_detail or 'NXDOMAIN'}",
        ):
            report.escalated_to_review += 1
        return

    if outcome.status is FetchStatus.BLOCKED:
        # The site said no, or we refused the target. Recording that and stopping is
        # the entire response -- there is no proxy rotation, no user-agent cycling and
        # no CAPTCHA handling anywhere in this package (D6).
        _set_eligibility_if_unjudged(
            connection,
            source_id=lease.source_id,
            state=FetchEligibility.BLOCKED,
            reason=(f"{outcome.error_class}: {outcome.error_detail or ''}".strip()[:400])
            or "refused by the site",
            access_state="BLOCKED",
        )
        logger.warning(
            "acquisition_source_blocked",
            source_id=str(lease.source_id),
            error=outcome.error_class,
        )
        return

    if outcome.status is FetchStatus.INTERNAL_ERROR:
        # Our defect. Recorded, not retried: the cost of a bug in this repository
        # should not be paid in extra requests to a university (§12).
        return

    if outcome.status in (FetchStatus.HTTP_ERROR, FetchStatus.TIMEOUT):
        if outcome.http_status in PERMANENT_STATUSES:
            # 404/410 are not transient. The smoke run retried a dead PolyU fee URL
            # three times per cycle to learn the same thing each time, which is load
            # on a university for no information. A missing page is a worklist item
            # for whoever owns the source list.
            logger.info(
                "acquisition_permanent_status",
                source_id=str(lease.source_id),
                http_status=outcome.http_status,
            )
            return
        delay = backoff_seconds(lease.attempt_no, retry_after=outcome.retry_after_seconds)
        if schedule_retry(
            connection,
            source_id=lease.source_id,
            cycle_key=lease.cycle_key,
            delay_seconds=delay,
        ):
            report.retries_scheduled += 1

    if outcome.effective_host_differs:
        # Recorded for the source reviewer and acted on by nothing. A university page
        # redirecting to an external application portal is normal and observable; it
        # does not make that host official (Step 5B section 12).
        logger.info(
            "acquisition_effective_host_differs",
            source_id=str(lease.source_id),
            requested=outcome.requested_url,
            effective=outcome.effective_url,
        )


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def sweep_and_report(engine: Engine) -> list[uuid.UUID]:
    """Reap attempts whose worker stopped heartbeating."""
    from app.domains.acquisition.lease import sweep_expired

    with engine.begin() as connection:
        return sweep_expired(connection, now=utcnow())


__all__ = [
    "DEFAULT_DNS_COOLDOWN_SECONDS",
    "DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS",
    "HOST_COOLDOWN_ON_RATE_LIMIT",
    "PERMANENT_STATUSES",
    "RATE_LIMIT_STRIKE_LIMIT",
    "CycleReport",
    "HostGate",
    "run_cycle",
    "sweep_and_report",
]
