"""Recovery, cycle isolation and exception containment (Step 5B.2 sections 19-23).

Four operational blockers stood between the smoke test and a 319-page run, and three
of them shared a shape: **a temporary condition was recorded as a permanent one, and
nothing could undo it.** A single `429` set `fetch_eligibility = 'BLOCKED'`; a resolver
timeout took the same path as "this hostname resolves to loopback"; and no code
anywhere set that column back, because registration is `ON CONFLICT DO NOTHING` by
design. One rate limit anywhere in a 319-page run silently dropped a legitimate page
for good.

Every test here is a claim about what happens next -- which is the thing the old code
got wrong. Classifying a 429 correctly is worth nothing if the consequence is still a
permanent block, so these assert the consequence: the column, the cooldown, the
schedulability, and whether another attempt appears.

**No test contacts a university.** The rate-limit cases use the loopback fixture
server; the DNS cases inject a resolver that raises, because the alternative is a test
that depends on a real name failing to resolve, which is a test that depends on
someone else's DNS.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from app.core.clock import utcnow
from app.db.enums import FetchStatus
from app.domains.acquisition import recovery
from app.domains.acquisition.fetcher import FetchPolicy, StaticHttpFetcher
from app.domains.acquisition.lease import claim_next, enqueue_cycle, schedule_retry
from app.domains.acquisition.netsafety import (
    DnsTemporaryError,
    NameNotResolvedError,
    UnsafeTargetError,
    resolve_and_validate,
)
from app.domains.acquisition.recorder import record_outcome
from app.domains.acquisition.registration import register_acquisition_targets
from app.domains.acquisition.runner import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_STRIKE_LIMIT,
    CycleReport,
    HostGate,
    run_cycle,
)
from app.domains.acquisition.storage import FilesystemEvidenceStore
from tests.integration.fixtures_http import FixtureServer
from tests.integration.test_acquisition_cycle import (  # noqa: F401 - fixtures/helpers
    _cleanup,
    _SameTransactionEngine,
    _seed,
    engine,
    server,
)
from tests.integration.test_acquisition_evidence import (  # noqa: F401 - fixtures
    _enqueue_one,
    _outcome,
    pilot,
    registered,
    store,
)
from tests.integration.test_acquisition_fetch import fetch

# ruff: noqa: F811 -- importing a pytest fixture and naming it as a test parameter is
# how fixture reuse across modules works; the "redefinition" is the mechanism.
pytestmark = pytest.mark.integration


# ===========================================================================
# Helpers
# ===========================================================================


def _react_to(
    conn: Connection,
    registered_pilot: dict[str, Any],
    store_: Any,
    outcome: Any,
    *,
    cycle: str = "c1",
) -> Any:
    """Record one outcome against one queued page and apply its consequences."""
    from app.domains.acquisition.runner import _react

    _enqueue_one(conn, registered_pilot, cycle)
    lease = claim_next(conn, cycle_key=cycle, worker="w")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=outcome, store=store_)
    _react(conn, lease, outcome, CycleReport(cycle_key=cycle))
    return lease


def _source_row(conn: Connection, source_id: uuid.UUID) -> Any:
    return conn.execute(
        text(
            "SELECT fetch_eligibility::text AS fe, access_state::text AS access, "
            "       cooldown_until, cooldown_reason, rate_limit_strikes, "
            "       fetch_eligibility_reason "
            "  FROM source WHERE id = :id"
        ),
        {"id": source_id},
    ).one()


def _rate_limited(url: str, *, retry_after: float | None = None) -> Any:
    return _outcome(
        url,
        content=None,
        status=FetchStatus.RATE_LIMITED,
        http_status=429,
        error_class="RateLimited",
        retry_after_seconds=retry_after,
    )


def _actor(conn: Connection) -> recovery.Actor:
    """A real `app_user`, because the audit row's FK is not decorative."""
    user_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO app_user (id, email, display_name) VALUES (:id, :email, 'Operator')"),
        {"id": user_id, "email": f"op-{user_id.hex[:8]}@example.test"},
    )
    return recovery.Actor(user_id=user_id)


# ===========================================================================
# 19. Rate limiting
# ===========================================================================


def test_one_429_does_not_permanently_block_the_source(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 1. The defect this whole step exists to remove.

    A single rate limit used to set `fetch_eligibility = 'BLOCKED'` and skip the retry
    branch, so the page left the schedule for good and nothing could put it back.
    """
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=60))
    row = _source_row(conn, lease.source_id)

    assert row.fe == "FETCHABLE", "a single 429 permanently blocked the source"
    assert row.access == "OK", "a throttle was recorded as an access refusal"
    assert row.cooldown_until is not None, "no cooldown was set, so nothing changed"
    assert row.rate_limit_strikes == 1

    # Health says DEGRADED, not BLOCKED: the distinction an operator acts on.
    health = conn.execute(
        text("SELECT health, schedule_state FROM source_health WHERE source_id = :s"),
        {"s": lease.source_id},
    ).one()
    assert health.health == "DEGRADED"
    assert health.schedule_state == "COOLDOWN"


def test_retry_after_in_seconds_sets_the_cooldown(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 2. The server's number wins over ours, always."""
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=600))
    row = _source_row(conn, lease.source_id)
    remaining = (row.cooldown_until - utcnow()).total_seconds()
    # Within a wide band: the point is that it followed 600 rather than a default.
    assert 540 < remaining <= 620, f"cooldown was {remaining}s, not the stated 600s"
    assert "600" in row.cooldown_reason


def test_retry_after_as_an_http_date_is_parsed(server: FixtureServer) -> None:
    """Requirement 3. The other legal form, from a real socket."""
    outcome = fetch(server, "/429-date")
    assert outcome.status is FetchStatus.RATE_LIMITED
    assert outcome.retry_after_seconds is not None
    # The fixture names a moment ~240s out; allow for the request's own latency.
    assert 180 < outcome.retry_after_seconds <= 245


def test_an_unparseable_retry_after_falls_back_conservatively(
    conn: Connection, registered: dict[str, Any], store: Any, server: FixtureServer
) -> None:
    """Requirement 4. `Retry-After: soon` is not a number and not a date.

    The dangerous reading is "no usable value, so retry immediately", which is how a
    throttle becomes a block. The fallback is deliberately long.
    """
    outcome = fetch(server, "/429-garbage")
    assert outcome.status is FetchStatus.RATE_LIMITED
    assert outcome.retry_after_seconds is None, "garbage was parsed as a number"

    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=None))
    row = _source_row(conn, lease.source_id)
    remaining = (row.cooldown_until - utcnow()).total_seconds()
    assert remaining > DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS - 60
    assert "absent" in row.cooldown_reason


def test_nothing_is_claimable_before_the_cooldown_expires(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 5. A cooldown that the scheduler ignores is not a cooldown."""
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=600))

    # The retry exists and is queued...
    queued = conn.execute(
        text("SELECT count(*) FROM fetch_attempt " " WHERE source_id = :s AND state = 'QUEUED'"),
        {"s": lease.source_id},
    ).scalar_one()
    assert queued == 1, "no retry was queued at all"

    # ...and is still not claimable, because the source is in cooldown. Both the
    # `scheduled_for` delay and the cooldown gate it; this asserts the cooldown does,
    # by making the attempt due immediately and leaving the cooldown in place.
    conn.execute(
        text(
            "UPDATE fetch_attempt SET scheduled_for = now() - interval '1 hour' "
            " WHERE source_id = :s AND state = 'QUEUED'"
        ),
        {"s": lease.source_id},
    )
    assert (
        claim_next(conn, cycle_key="c1", worker="w2") is None
    ), "a source in cooldown was handed to a worker"


def test_the_source_becomes_claimable_once_the_cooldown_expires(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirements 6 and 20. Recovery from a throttle needs no human at all."""
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=600))
    conn.execute(
        text(
            "UPDATE fetch_attempt SET scheduled_for = now() - interval '1 hour' "
            " WHERE source_id = :s AND state = 'QUEUED'"
        ),
        {"s": lease.source_id},
    )
    assert claim_next(conn, cycle_key="c1", worker="w2") is None

    # The clock moves on. Nothing else happens -- no operator, no audit row.
    #
    # Both cooldowns have to expire, and that is the host-level pause working rather
    # than an inconvenience: a 429 quiets the whole host (§13), so a source whose own
    # cooldown has passed is still not asked for while its host is owed time.
    conn.execute(
        text("UPDATE source SET cooldown_until = now() - interval '1 second' WHERE id = :s"),
        {"s": lease.source_id},
    )
    assert (
        claim_next(conn, cycle_key="c1", worker="w2") is None
    ), "the host pause was ignored once the source's own cooldown expired"
    conn.execute(text("UPDATE host_cooldown SET cooldown_until = now() - interval '1 second'"))
    again = claim_next(conn, cycle_key="c1", worker="w2")
    assert again is not None, "an expired cooldown still blocked the source"
    assert again.source_id == lease.source_id


def test_a_rate_limit_retry_is_a_new_attempt(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 7. Never a reset of the failed one, and never in-loop.

    Three tries must read as three attempts with three terminal runs, not one row that
    eventually worked -- and a retry inside the worker loop would be asking again
    immediately, which is the opposite of what the 429 requested.
    """
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=60))
    attempts = conn.execute(
        text(
            "SELECT attempt_no, state, cycle_key, scheduled_for FROM fetch_attempt "
            " WHERE source_id = :s ORDER BY attempt_no"
        ),
        {"s": lease.source_id},
    ).all()
    assert [row.attempt_no for row in attempts] == [1, 2]
    assert attempts[0].state == "FINALIZED"
    assert attempts[1].state == "QUEUED"
    assert attempts[1].scheduled_for > utcnow(), "the retry was due immediately"
    # Requirement 30, proved here too: a retry keeps the logical cycle.
    assert {row.cycle_key for row in attempts} == {"c1"}


def test_the_429_run_stays_immutable(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 8. History records what happened, including the throttle."""
    lease = _react_to(conn, registered, store, _rate_limited("https://x", retry_after=60))
    run = conn.execute(
        text(
            "SELECT status::text AS status, http_status, error_class "
            "  FROM fetch_run WHERE source_id = :s"
        ),
        {"s": lease.source_id},
    ).one()
    assert run.status == "RATE_LIMITED"
    assert run.http_status == 429
    assert run.error_class.startswith("RateLimited")

    with pytest.raises(Exception, match="append-only"):
        conn.execute(
            text("UPDATE fetch_run SET status = 'OK' WHERE source_id = :s"),
            {"s": lease.source_id},
        )


def test_repeated_rate_limits_escalate_to_manual_review(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 9. A pattern is a question about our cadence, for a person.

    Still not `BLOCKED`: the site has not refused us, it has asked us to slow down
    more times than a schedule should keep ignoring.
    """
    from app.domains.acquisition.runner import _react

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    report = CycleReport(cycle_key="c1")

    for strike in range(1, RATE_LIMIT_STRIKE_LIMIT + 1):
        outcome = _rate_limited(lease.url, retry_after=5)
        _react(conn, lease, outcome, report)
        row = _source_row(conn, lease.source_id)
        assert row.rate_limit_strikes == strike
        if strike < RATE_LIMIT_STRIKE_LIMIT:
            assert row.fe == "FETCHABLE", f"escalated early, at strike {strike}"

    row = _source_row(conn, lease.source_id)
    assert row.fe == "NEEDS_MANUAL_REVIEW"
    assert row.access == "OK", "a throttle was recorded as an access refusal"
    assert "rate limited" in row.fetch_eligibility_reason
    assert report.escalated_to_review == 1

    health = conn.execute(
        text("SELECT health, schedule_state FROM source_health WHERE source_id = :s"),
        {"s": lease.source_id},
    ).one()
    assert health.health == "NEEDS_MANUAL_REVIEW"
    assert health.schedule_state == "NEEDS_MANUAL_REVIEW"


def test_a_success_clears_the_cooldown_and_the_strikes(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """The automatic half of recovery: a throttled page that answers is fine again.

    Counting strikes for a lifetime rather than a streak would eventually escalate
    every source in the pilot, which is a slow way of turning a working system off.
    """
    from app.domains.acquisition.runner import _react

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    _react(conn, lease, _rate_limited(lease.url, retry_after=60), CycleReport(cycle_key="c1"))
    assert _source_row(conn, lease.source_id).rate_limit_strikes == 1

    _react(conn, lease, _outcome(lease.url), CycleReport(cycle_key="c1"))
    row = _source_row(conn, lease.source_id)
    assert row.cooldown_until is None
    assert row.rate_limit_strikes == 0
    assert row.fe == "FETCHABLE"


def test_a_rate_limit_quiets_the_whole_host(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 13 of the spec's host-cooldown section.

    One pilot host serves eleven pages. Honouring the throttle on the page that
    received it and then immediately asking for the other ten is not honouring it.
    """
    from app.domains.acquisition.runner import _react

    ours = registered["source_ids"]
    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    _react(conn, lease, _rate_limited(lease.url, retry_after=300), CycleReport(cycle_key="c1"))

    pauses = conn.execute(text("SELECT host, cooldown_until, reason FROM host_cooldown")).all()
    assert len(pauses) == 1
    host = pauses[0].host
    assert host == host.lower()

    # Every other page on that host is now COOLDOWN too, without any of them having
    # been fetched.
    siblings = conn.execute(
        text(
            "SELECT h.source_id, h.schedule_state FROM source_health h "
            " WHERE h.source_id = ANY(:ids) "
            "   AND lower(split_part(split_part(h.url, '://', 2), '/', 1)) = :host"
        ),
        {"ids": ours, "host": host},
    ).all()
    assert len(siblings) >= 1
    assert {row.schedule_state for row in siblings} == {"COOLDOWN"}


def test_no_proxy_or_user_agent_rotation_exists(server: FixtureServer) -> None:
    """Requirement 10. The absence is the design, so it is asserted (D6).

    Checked as a property of what the fetcher *sends* rather than by scanning source
    for the word "proxy": the User-Agent is one fixed honest string across requests,
    it names us, and it does not claim to be a browser.
    """
    from app.domains.acquisition.fetcher import USER_AGENT

    agents = []
    for path in ("/ok", "/429", "/ok"):
        fetch(server, path)
        agents.append(server.log.last_headers().get("user-agent"))

    assert len(set(agents)) == 1, "the User-Agent changed between requests"
    assert agents[0] == USER_AGENT
    for lie in ("Mozilla", "Chrome", "Safari", "Edge", "Firefox"):
        assert lie not in USER_AGENT, f"the User-Agent impersonates {lie}"
    assert "OverseasUniDataHub" in USER_AGENT

    policy = FetchPolicy()
    assert not hasattr(policy, "proxy")
    assert not hasattr(policy, "proxies")
    assert policy.verify_tls is True
    assert policy.pin_addresses is True


# ===========================================================================
# 20. DNS and security
# ===========================================================================


def _fetcher_whose_resolver_raises(exc: BaseException) -> StaticHttpFetcher:
    """A fetcher that cannot resolve anything, for the reason given.

    The resolver is injected rather than monkeypatched, and the failure is raised
    rather than simulated over a real name: a test that depends on `nxdomain.invalid`
    actually failing to resolve is a test that depends on someone else's DNS.
    """

    async def resolver(url: str, **_: Any) -> Any:
        raise exc

    return StaticHttpFetcher(FetchPolicy(), resolver=resolver)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_class"),
    [
        (DnsTemporaryError("timed out"), FetchStatus.DNS_TEMPORARY, "DnsTemporaryFailure"),
        (
            DnsTemporaryError("the resolver did not answer: SERVFAIL"),
            FetchStatus.DNS_TEMPORARY,
            "DnsTemporaryFailure",
        ),
        (
            NameNotResolvedError("the name does not resolve"),
            FetchStatus.NAME_NOT_RESOLVED,
            "NameNotResolved",
        ),
        (UnsafeTargetError("resolves to a loopback address"), FetchStatus.BLOCKED, "UnsafeTarget"),
    ],
)
def test_resolution_failures_are_classified_apart(
    error: BaseException, expected_status: FetchStatus, expected_class: str
) -> None:
    """Requirements 11, 12, 13. Three outcomes that used to be one.

    The fourth case is the control: a genuine security refusal must keep taking the
    permanent path, or separating the others would have weakened the guard.
    """
    fetcher = _fetcher_whose_resolver_raises(error)
    outcome = asyncio.run(fetcher.fetch("https://whatever.example/"))
    assert outcome.status is expected_status
    assert outcome.error_class == expected_class
    assert outcome.content is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-2, NameNotResolvedError),  # EAI_NONAME  (glibc)
        (-5, NameNotResolvedError),  # EAI_NODATA  (glibc)
        (11001, NameNotResolvedError),  # WSAHOST_NOT_FOUND
        (-3, DnsTemporaryError),  # EAI_AGAIN   (glibc)
        (-4, DnsTemporaryError),  # EAI_FAIL    (glibc) -- SERVFAIL
        (11002, DnsTemporaryError),  # WSATRY_AGAIN
        (99999, DnsTemporaryError),  # unknown: temporary is the safe guess
    ],
)
def test_resolver_error_codes_map_to_the_right_class(code: int, expected: type) -> None:
    """Requirements 11-13 at the boundary, for both platforms' code families.

    The constants differ by platform and each is missing the other's, so both are
    pinned numerically -- otherwise this suite would assert one mapping on Windows and
    a different one in CI.
    """
    from app.domains.acquisition.netsafety import classify_resolver_failure

    assert isinstance(classify_resolver_failure(socket.gaierror(code, "x")), expected)


def test_an_unknown_code_is_temporary_not_permanent() -> None:
    """Non-vacuity for the default above, stated as the judgement it is.

    Wrong in the temporary direction costs one retry. Wrong the other way parks a
    working page in `NEEDS_MANUAL_REVIEW` and waits for a human with no reason to look.
    """
    from app.domains.acquisition.netsafety import classify_resolver_failure

    assert isinstance(classify_resolver_failure(OSError("no errno at all")), DnsTemporaryError)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://2130706433/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://metadata.google.internal/",
    ],
)
def test_unsafe_targets_stay_permanently_blocked(url: str) -> None:
    """Requirements 14-17. Separating DNS failures must not have loosened these."""
    with pytest.raises(UnsafeTargetError):
        asyncio.run(resolve_and_validate(url, timeout_seconds=5.0))


def test_a_failed_resolution_sends_no_http_request(server: FixtureServer) -> None:
    """Requirement 18. The guard is worthless if a failure falls through to a connect."""
    before = len(server.log.paths)
    for error in (
        DnsTemporaryError("timed out"),
        NameNotResolvedError("no such name"),
        UnsafeTargetError("resolves to a loopback address"),
    ):
        fetcher = _fetcher_whose_resolver_raises(error)
        outcome = asyncio.run(fetcher.fetch(server.url("/ok")))
        assert outcome.status is not FetchStatus.OK
    assert len(server.log.paths) == before, "a request was sent despite resolution failing"


def test_a_redirect_into_private_space_is_blocked_not_a_dns_failure(
    server: FixtureServer,
) -> None:
    """Requirement 19. Every hop is revalidated, and a hostile hop is still a refusal."""
    outcome = fetch(server, "/redirect-private")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_class == "UnsafeTarget"
    assert outcome.content is None


def test_a_temporary_dns_failure_earns_a_cooldown_and_a_retry(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 11's consequence. Backoff, not a security block."""
    outcome = _outcome(
        "https://x",
        content=None,
        status=FetchStatus.DNS_TEMPORARY,
        error_class="DnsTemporaryFailure",
    )
    lease = _react_to(conn, registered, store, outcome)
    row = _source_row(conn, lease.source_id)
    assert row.fe == "FETCHABLE", "a resolver hiccup blocked the source"
    assert row.access == "OK"
    assert row.cooldown_until is not None
    assert "DNS" in row.cooldown_reason
    queued = conn.execute(
        text("SELECT count(*) FROM fetch_attempt WHERE source_id = :s AND state = 'QUEUED'"),
        {"s": lease.source_id},
    ).scalar_one()
    assert queued == 1, "no retry was scheduled for a temporary failure"


def test_an_unresolvable_name_goes_to_manual_review_without_retrying(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 13's consequence. No number of lookups invents a hostname."""
    outcome = _outcome(
        "https://x",
        content=None,
        status=FetchStatus.NAME_NOT_RESOLVED,
        error_class="NameNotResolved",
        error_detail="the name does not resolve",
    )
    lease = _react_to(conn, registered, store, outcome)
    row = _source_row(conn, lease.source_id)
    assert row.fe == "NEEDS_MANUAL_REVIEW"
    assert row.access == "OK", "a missing name was recorded as an access refusal"
    queued = conn.execute(
        text("SELECT count(*) FROM fetch_attempt WHERE source_id = :s AND state = 'QUEUED'"),
        {"s": lease.source_id},
    ).scalar_one()
    assert queued == 0, "a nonexistent hostname was queued for another look"


# ===========================================================================
# 21. Recovery
# ===========================================================================


def test_reenable_requires_an_actor_and_a_reason(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 21. A transition with no reason is not a decision.

    The reason is not paperwork: the audit row is what a later reviewer reads to
    decide whether the override still holds, and "ok" tells them nothing.
    """
    source_id = registered["source_ids"][0]
    actor = _actor(conn)
    with pytest.raises(recovery.RecoveryRefusedError, match="reason"):
        recovery.reenable(conn, source_id=source_id, actor=actor, reason="ok")
    with pytest.raises(TypeError):
        recovery.reenable(conn, source_id=source_id, reason="a proper reason here")  # type: ignore[call-arg]


def test_reenable_restores_fetchability_and_is_audited(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirements 22 and 26. The way back, recorded, conferring no trust."""
    lease = _react_to(
        conn,
        registered,
        store,
        _outcome(
            "https://x",
            content=None,
            status=FetchStatus.BLOCKED,
            http_status=403,
            error_class="HTTP403",
        ),
    )
    blocked = _source_row(conn, lease.source_id)
    assert blocked.fe == "BLOCKED"

    actor = _actor(conn)
    before_audit = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    after = recovery.reenable(
        conn,
        source_id=lease.source_id,
        actor=actor,
        reason="the university restored the page; verified by hand",
    )

    assert after.fetch_eligibility == "FETCHABLE"
    assert after.schedule_state == "FETCHABLE_NOW"
    # Requirement 26: technical permission only.
    assert after.publication_eligibility == "NOT_ELIGIBLE"

    rows = conn.execute(
        text(
            "SELECT action, actor_id, object_type, object_id, reason, "
            "       before_state, after_state FROM audit_log "
            " ORDER BY seq DESC LIMIT 1"
        )
    ).one()
    assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == before_audit + 1
    assert rows.action == "ACQUISITION_SOURCE_REENABLE"
    assert rows.actor_id == actor.user_id
    assert rows.object_type == "source"
    assert rows.object_id == lease.source_id
    assert "restored the page" in rows.reason
    assert rows.before_state["fetch_eligibility"] == "BLOCKED"
    assert rows.after_state["fetch_eligibility"] == "FETCHABLE"
    # The trail shows publication trust did not move, which is the point of
    # including it in the snapshot at all.
    assert rows.before_state["publication_eligibility"] == "NOT_ELIGIBLE"
    assert rows.after_state["publication_eligibility"] == "NOT_ELIGIBLE"


def test_reregistration_cannot_revive_a_disabled_source(
    conn: Connection, registered: dict[str, Any], pilot: dict[str, Any]
) -> None:
    """Requirement 23. Re-importing the workbook must not undo an operator.

    `register_acquisition_targets` is `ON CONFLICT DO NOTHING`, so this asserts a
    property the code already had -- and asserts it because the property is invisible
    at the call site and one `DO UPDATE` would quietly remove it.
    """
    source_id = registered["source_ids"][0]
    actor = _actor(conn)
    recovery.disable(
        conn, source_id=source_id, actor=actor, reason="the URL is wrong; awaiting the client"
    )
    assert _source_row(conn, source_id).fe == "DISABLED"

    register_acquisition_targets(conn, submission_id=pilot["submission_id"])

    row = _source_row(conn, source_id)
    assert row.fe == "DISABLED", "re-registration revived a source an operator disabled"
    assert "disabled by operator" in row.fetch_eligibility_reason


@pytest.mark.parametrize("human_state", ["DISABLED", "NEEDS_MANUAL_REVIEW", "BLOCKED"])
def test_a_worker_cannot_override_a_human_decision(
    conn: Connection, registered: dict[str, Any], store: Any, human_state: str
) -> None:
    """Requirements 24 and 25. The operator's decision is not advisory.

    A worker records what it observed; it does not get to conclude that a source a
    person parked is fine now, nor that one they disabled should be tried again.
    """
    from app.domains.acquisition.runner import _react

    source_id = registered["source_ids"][0]
    actor = _actor(conn)
    if human_state == "DISABLED":
        recovery.disable(conn, source_id=source_id, actor=actor, reason="operator decision here")
    elif human_state == "NEEDS_MANUAL_REVIEW":
        recovery.mark_needs_review(
            conn, source_id=source_id, actor=actor, reason="operator decision here"
        )
    else:
        conn.execute(
            text(
                "UPDATE source SET fetch_eligibility = 'BLOCKED', "
                "fetch_eligibility_reason = 'refused earlier' WHERE id = :s"
            ),
            {"s": source_id},
        )

    before = _source_row(conn, source_id)

    # A successful fetch is the strongest case: if anything would wrongly reopen a
    # source, it is a 200.
    lease_stub = type(
        "Stub",
        (),
        {
            "source_id": source_id,
            "url": "https://alpha.example.ac.uk/apply",
            "cycle_key": "c1",
            "attempt_no": 1,
        },
    )()
    _react(conn, lease_stub, _outcome("https://alpha.example.ac.uk/apply"), CycleReport("c1"))

    after = _source_row(conn, source_id)
    assert after.fe == before.fe == human_state
    assert after.fetch_eligibility_reason == before.fetch_eligibility_reason


def test_clear_cooldown_refuses_when_there_is_no_cooldown(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """A command that silently does nothing is worse than one that says so."""
    actor = _actor(conn)
    with pytest.raises(recovery.RecoveryRefusedError, match="not in cooldown"):
        recovery.clear_cooldown(
            conn,
            source_id=registered["source_ids"][0],
            actor=actor,
            reason="nothing to clear here",
        )


def test_clear_cooldown_is_audited_and_can_include_the_host(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Shortening a site's pause is a choice, so it is recorded as one."""
    from app.domains.acquisition.runner import _react

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    _react(conn, lease, _rate_limited(lease.url, retry_after=600), CycleReport("c1"))
    assert conn.execute(text("SELECT count(*) FROM host_cooldown")).scalar_one() == 1

    actor = _actor(conn)
    after = recovery.clear_cooldown(
        conn,
        source_id=lease.source_id,
        actor=actor,
        reason="the throttle was our own bug, since fixed",
        include_host=True,
    )
    assert after.schedule_state == "FETCHABLE_NOW"
    assert conn.execute(text("SELECT count(*) FROM host_cooldown")).scalar_one() == 0
    action = conn.execute(
        text("SELECT action, reason FROM audit_log ORDER BY seq DESC LIMIT 1")
    ).one()
    assert action.action == "ACQUISITION_SOURCE_CLEAR_COOLDOWN"
    assert "host pause cleared too" in action.reason


def test_an_automatic_cooldown_is_not_audited(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Section 18. The chain records decisions, and a clock tick is not one.

    Auditing every cooldown would bury the handful of rows that are decisions under
    thousands that are not, which makes the trail less useful, not more.
    """
    before = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    _react_to(conn, registered, store, _rate_limited("https://x", retry_after=60))
    assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == before


# ===========================================================================
# 22. Cycle isolation
# ===========================================================================


def _queue_in(conn: Connection, source_ids: list[uuid.UUID], cycle: str) -> None:
    enqueue_cycle(conn, cycle_key=cycle, source_ids=source_ids)


def test_a_cycle_claims_only_its_own_attempts(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirements 27, 28. `cycle_key` was a label the query ignored."""
    ours = registered["source_ids"]
    _queue_in(conn, ours, "cycle-a")
    _queue_in(conn, ours, "cycle-b")

    claimed_a, claimed_b = [], []
    while (lease := claim_next(conn, cycle_key="cycle-a", worker="worker-a")) is not None:
        claimed_a.append(lease)
    while (lease := claim_next(conn, cycle_key="cycle-b", worker="worker-b")) is not None:
        claimed_b.append(lease)

    assert claimed_a, "cycle A claimed nothing at all"
    assert claimed_b, "cycle B claimed nothing at all"
    assert {lease.cycle_key for lease in claimed_a} == {"cycle-a"}
    assert {lease.cycle_key for lease in claimed_b} == {"cycle-b"}
    assert len(claimed_a) == len(claimed_b) == len(ours)


def test_a_worker_cannot_claim_a_dummy_cycle(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirement 29's serial half, and §16's proof for the planned full run."""
    ours = registered["source_ids"]
    _queue_in(conn, ours, "pilot-full-001")
    _queue_in(conn, ours, "dummy-unrelated")

    claimed = []
    while (lease := claim_next(conn, cycle_key="pilot-full-001", worker="w")) is not None:
        claimed.append(lease)
    assert len(claimed) == len(ours)
    assert {lease.cycle_key for lease in claimed} == {"pilot-full-001"}

    still_queued = conn.execute(
        text(
            "SELECT count(*) FROM fetch_attempt "
            " WHERE cycle_key = 'dummy-unrelated' AND state = 'QUEUED'"
        )
    ).scalar_one()
    assert still_queued == len(ours), "the dummy cycle's work was drained by the full run"


def test_a_claim_never_rewrites_the_cycle_key(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirement 31. Claiming is about ownership, not about re-labelling work."""
    ours = registered["source_ids"]
    _queue_in(conn, ours, "cycle-a")

    def cycles_by_attempt() -> dict[uuid.UUID, str]:
        return {
            row.id: row.cycle_key
            for row in conn.execute(
                text("SELECT id, cycle_key FROM fetch_attempt WHERE source_id = ANY(:ids)"),
                {"ids": ours},
            )
        }

    before = cycles_by_attempt()
    lease = claim_next(conn, cycle_key="cycle-a", worker="w")
    assert lease is not None
    assert cycles_by_attempt() == before


def test_a_retry_stays_in_its_original_cycle(conn: Connection, registered: dict[str, Any]) -> None:
    """Requirement 30. A retry belongs to the logical check that spawned it."""
    ours = [registered["source_ids"][0]]
    _queue_in(conn, ours, "cycle-a")
    lease = claim_next(conn, cycle_key="cycle-a", worker="w")
    assert lease is not None
    schedule_retry(conn, source_id=lease.source_id, cycle_key=lease.cycle_key, delay_seconds=0)
    cycles = (
        conn.execute(
            text("SELECT DISTINCT cycle_key FROM fetch_attempt WHERE source_id = :s"),
            {"s": lease.source_id},
        )
        .scalars()
        .all()
    )
    assert cycles == ["cycle-a"]
    # And another cycle still cannot take it.
    assert claim_next(conn, cycle_key="cycle-b", worker="w2") is None


def test_duplicate_queueing_is_still_prevented(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 32. Enqueue stays idempotent within a cycle."""
    ours = registered["source_ids"]
    first = enqueue_cycle(conn, cycle_key="cycle-a", source_ids=ours)
    second = enqueue_cycle(conn, cycle_key="cycle-a", source_ids=ours)
    assert first.attempts_created == len(ours)
    assert second.attempts_created == 0
    assert second.already_queued == len(ours)


def test_two_concurrent_cycles_never_cross_claim(engine: Engine, server: FixtureServer) -> None:
    """Requirement 29, with real concurrency on two connections.

    Cycle A has three attempts and cycle B has three. Two workers drain their own
    cycle at the same time on separate transactions; neither may see the other's work,
    and `SKIP LOCKED` must not let a worker fall through to a different cycle's row
    because its own are locked.
    """
    with engine.begin() as connection:
        seeded = _seed(connection, server, ["/ok", "/etag", "/modified"])
        enqueue_cycle(connection, cycle_key="conc-a", source_ids=seeded["source_ids"])
        enqueue_cycle(connection, cycle_key="conc-b", source_ids=seeded["source_ids"])

    try:
        first, second = engine.connect(), engine.connect()
        try:
            t1, t2 = first.begin(), second.begin()
            try:
                a_claims, b_claims = [], []
                for _ in range(3):
                    a = claim_next(first, cycle_key="conc-a", worker="conc-worker-a")
                    b = claim_next(second, cycle_key="conc-b", worker="conc-worker-b")
                    if a is not None:
                        a_claims.append(a)
                    if b is not None:
                        b_claims.append(b)

                assert len(a_claims) == 3, f"cycle A got {len(a_claims)} of its 3"
                assert len(b_claims) == 3, f"cycle B got {len(b_claims)} of its 3"
                assert {lease.cycle_key for lease in a_claims} == {"conc-a"}
                assert {lease.cycle_key for lease in b_claims} == {"conc-b"}
                # Same pages, different attempts: no attempt was handed to both.
                assert not {lease.attempt_id for lease in a_claims} & {
                    lease.attempt_id for lease in b_claims
                }
                t1.rollback()
                t2.rollback()
            except BaseException:
                t1.rollback()
                t2.rollback()
                raise
        finally:
            first.close()
            second.close()
    finally:
        # `_seed` committed, so this test owns its rows. `_cleanup` already knows the
        # order and uses `app.allow_history_maintenance`, which the append-only
        # trigger itself names as the sanctioned escape.
        _cleanup(engine, seeded)


# ===========================================================================
# 23. Exception containment
# ===========================================================================


class _BoomError(RuntimeError):
    """A defect in our own code, as a test can produce one."""


def _exploding_fetcher(fail_on: str) -> Any:
    """A fetcher that raises on one URL and behaves on the others."""

    class Exploding:
        fetcher_name = "STATIC"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch(self, url: str, **_: Any) -> Any:
            self.calls.append(url)
            if fail_on in url:
                raise _BoomError("a bug in our own code")
            return _outcome(url)

    return Exploding()


def test_an_exception_on_one_page_does_not_stop_the_others(
    conn: Connection, server: FixtureServer, tmp_path: Path
) -> None:
    """Requirements 33 and 11 of the spec's continue-after-failure section.

    The cycle used to end at the first unexpected exception. With 319 pages queued,
    that meant a defect on page 40 left 279 universities unvisited and produced a
    traceback instead of a report.
    """
    seeded = _seed(conn, server, ["/ok", "/etag", "/modified"])
    enqueue_cycle(conn, cycle_key="boom", source_ids=seeded["source_ids"])
    fetcher = _exploding_fetcher("/etag")

    report = asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="boom",
            store=FilesystemEvidenceStore(tmp_path / "evidence"),
            worker="test-worker",
            fetcher=fetcher,
            gate=HostGate(min_interval_seconds=0.0),
        )
    )

    assert report.attempted == 3, "the cycle stopped early"
    assert report.internal_error == 1
    assert report.ok == 2, "the pages after the failure were not fetched"
    assert len(fetcher.calls) == 3

    runs = conn.execute(
        text(
            "SELECT status::text AS status, count(*) AS n FROM fetch_run "
            " WHERE source_id = ANY(:ids) GROUP BY 1 ORDER BY 1"
        ),
        {"ids": seeded["source_ids"]},
    ).all()
    assert {row.status: row.n for row in runs} == {"INTERNAL_ERROR": 1, "OK": 2}


def test_an_internal_error_is_terminal_history_with_no_evidence(
    conn: Connection, server: FixtureServer, tmp_path: Path
) -> None:
    """Requirements 34, 35, 36. Recorded, and carrying nothing it did not observe.

    A snapshot would assert we saw bytes we cannot produce, which is exactly what the
    evidence chain exists to prevent.
    """
    seeded = _seed(conn, server, ["/ok"])
    enqueue_cycle(conn, cycle_key="boom", source_ids=seeded["source_ids"])
    blobs_before = conn.execute(text("SELECT count(*) FROM content_blob")).scalar_one()

    asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="boom",
            store=FilesystemEvidenceStore(tmp_path / "evidence"),
            worker="test-worker",
            fetcher=_exploding_fetcher("/ok"),
            gate=HostGate(min_interval_seconds=0.0),
        )
    )

    run = conn.execute(
        text(
            "SELECT status::text AS status, error_class, http_status, bytes_downloaded, "
            "       worker_name, finished_at FROM fetch_run WHERE source_id = ANY(:ids)"
        ),
        {"ids": seeded["source_ids"]},
    ).one()
    assert run.status == "INTERNAL_ERROR"
    # The exception *type*, not a traceback: the database is not a log sink.
    assert run.error_class == "_BoomError"
    assert "Traceback" not in (run.error_class or "")
    assert run.http_status is None, "an internal error invented an HTTP status"
    assert run.finished_at is not None

    assert (
        conn.execute(
            text("SELECT count(*) FROM snapshot WHERE source_id = ANY(:ids)"),
            {"ids": seeded["source_ids"]},
        ).scalar_one()
        == 0
    )
    assert conn.execute(text("SELECT count(*) FROM content_blob")).scalar_one() == blobs_before

    # And the attempt is closed out, not left RUNNING for the sweeper.
    state = conn.execute(
        text("SELECT state FROM fetch_attempt WHERE source_id = ANY(:ids)"),
        {"ids": seeded["source_ids"]},
    ).scalar_one()
    assert state == "FINALIZED"


def test_an_internal_error_schedules_no_retry(
    conn: Connection, server: FixtureServer, tmp_path: Path
) -> None:
    """Section 12. Our defect must not be paid for in requests to a university."""
    seeded = _seed(conn, server, ["/ok"])
    enqueue_cycle(conn, cycle_key="boom", source_ids=seeded["source_ids"])
    asyncio.run(
        run_cycle(
            _SameTransactionEngine(conn),  # type: ignore[arg-type]
            cycle_key="boom",
            store=FilesystemEvidenceStore(tmp_path / "evidence"),
            worker="test-worker",
            fetcher=_exploding_fetcher("/ok"),
            gate=HostGate(min_interval_seconds=0.0),
        )
    )
    queued = conn.execute(
        text(
            "SELECT count(*) FROM fetch_attempt "
            " WHERE source_id = ANY(:ids) AND state = 'QUEUED'"
        ),
        {"ids": seeded["source_ids"]},
    ).scalar_one()
    assert queued == 0


def test_losing_the_lease_prevents_a_conflicting_internal_error_record(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 37. The fence holds for our own failures too.

    A stalled worker whose lease was swept must not write an authoritative terminal
    run for work it no longer owns -- and `record_internal_error` takes the fenced
    transition as its first write, so it writes nothing at all.
    """
    from app.domains.acquisition.lease import LeaseLostError
    from app.domains.acquisition.recorder import record_internal_error

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None

    # Somebody else takes the attempt: a new generation and a new token.
    conn.execute(
        text(
            "UPDATE fetch_attempt SET lease_token = :token, "
            "lease_generation = lease_generation + 1 WHERE id = :id"
        ),
        {"token": uuid.uuid4(), "id": lease.attempt_id},
    )

    with pytest.raises(LeaseLostError):
        record_internal_error(conn, lease=lease, error=_BoomError("boom"))

    assert (
        conn.execute(
            text("SELECT count(*) FROM fetch_run WHERE source_id = :s"),
            {"s": lease.source_id},
        ).scalar_one()
        == 0
    )


def test_a_finalisation_failure_leaves_the_attempt_for_the_sweeper(
    conn: Connection, registered: dict[str, Any]
) -> None:
    """Requirement 38. When we cannot record, we do not invent.

    An engine whose transactions fail stands in for "the database is the thing that
    broke". No `fetch_run` is manufactured, the attempt stays `RUNNING`, and the
    existing sweeper closes it out as `ABANDONED` -- which is the history that
    describes what actually happened.
    """
    from app.domains.acquisition.lease import sweep_expired
    from app.domains.acquisition.runner import _contain_internal_error

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None

    class _BrokenEngine:
        def begin(self) -> Any:
            raise OSError("the database is unavailable")

    report = CycleReport(cycle_key="c1")
    _contain_internal_error(
        _BrokenEngine(),  # type: ignore[arg-type]
        lease,
        _BoomError("boom"),
        report,
        "w",
    )

    assert report.unrecorded == 1
    assert report.internal_error == 0, "an unrecordable failure was counted as recorded"
    assert (
        conn.execute(
            text("SELECT count(*) FROM fetch_run WHERE source_id = :s"),
            {"s": lease.source_id},
        ).scalar_one()
        == 0
    ), "a fetch_run was manufactured for a failure we could not finalise"

    state = conn.execute(
        text("SELECT state FROM fetch_attempt WHERE id = :id"), {"id": lease.attempt_id}
    ).scalar_one()
    assert state == "RUNNING"

    # The sweeper does what it was designed to do.
    conn.execute(
        text(
            "UPDATE fetch_attempt SET lease_expires_at = now() - interval '1 hour' "
            " WHERE id = :id"
        ),
        {"id": lease.attempt_id},
    )
    swept = sweep_expired(conn, now=utcnow() + timedelta(seconds=1))
    assert lease.attempt_id in swept
    after = conn.execute(
        text(
            "SELECT a.state, r.status::text AS status FROM fetch_attempt a "
            "  LEFT JOIN fetch_run r ON r.attempt_id = a.id WHERE a.id = :id"
        ),
        {"id": lease.attempt_id},
    ).one()
    assert after.state == "ABANDONED"
    assert after.status == "ABANDONED"


@pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit])
def test_the_containment_boundary_does_not_swallow_base_exceptions(
    conn: Connection, server: FixtureServer, tmp_path: Path, escape: type[BaseException]
) -> None:
    """Requirements 39 and 40. An unkillable worker is a worse bug than the one fixed.

    `except Exception` rather than `except BaseException` is what makes this hold:
    `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` all derive from
    `BaseException`, so Ctrl-C still stops a 319-page run and cancellation still
    propagates.
    """
    seeded = _seed(conn, server, ["/ok", "/etag"])
    enqueue_cycle(conn, cycle_key="escape", source_ids=seeded["source_ids"])

    class Escaping:
        fetcher_name = "STATIC"

        async def fetch(self, url: str, **_: Any) -> Any:
            raise escape()

    with pytest.raises(escape):
        asyncio.run(
            run_cycle(
                _SameTransactionEngine(conn),  # type: ignore[arg-type]
                cycle_key="escape",
                store=FilesystemEvidenceStore(tmp_path / "evidence"),
                worker="test-worker",
                fetcher=Escaping(),  # type: ignore[arg-type]
                gate=HostGate(min_interval_seconds=0.0),
            )
        )
