"""What the first real-web run found, turned into fixtures (Step 5B.1).

Every test here exists because a real university server did something the deterministic
suite had not covered. The servers are still fixtures -- **no test contacts a
university** -- but the behaviours are ones observed rather than imagined:

* NUS answered `200` with a 212-byte Imperva challenge, and the pipeline stored it as
  the academic calendar. A plausible-looking snapshot whose content is not what the
  university published is the worst failure mode this system has, because nothing
  downstream can tell.
* PolyU's fee URL is a permanent `404`, and it was retried three times per cycle to
  learn the same thing each time.
* UBC's calendar redirects four times and the connect timed out on a later hop; the
  record kept the final status and lost every hop that led there.
* Caltech, ANU and UBC all answered `304` to a real conditional request, and ANU later
  chose to re-send a `200` with identical bytes instead.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import FetchStatus
from app.domains.acquisition.evidence import latest_effective_evidence
from app.domains.acquisition.lease import claim_next, enqueue_cycle, schedule_retry
from app.domains.acquisition.recorder import REDACTED, record_outcome, redact_url
from app.domains.acquisition.runner import PERMANENT_STATUSES
from tests.integration.fixtures_http import FixtureServer
from tests.integration.test_acquisition_evidence import (  # noqa: F401 - fixtures
    HTML,
    HTML_CHANGED,
    _claim,
    _enqueue_one,
    _outcome,
    pilot,
    registered,
    store,
)
from tests.integration.test_acquisition_fetch import fetch, make_fetcher, server  # noqa: F401

# ruff: noqa: F811 -- importing a pytest fixture and then naming it as a test
# parameter is how fixture reuse across modules works; the "redefinition" is the
# mechanism, not a mistake.
pytestmark = pytest.mark.integration


# ===========================================================================
# A 200 that is not the page
# ===========================================================================


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/challenge-incapsula", "_Incapsula_Resource"),
        ("/challenge-cloudflare", "Just a moment..."),
    ],
)
def test_a_challenge_served_as_200_is_blocked_not_stored(
    server: FixtureServer, path: str, marker: str
) -> None:
    """The NUS case. A refusal wearing a success code is a refusal.

    Storing those bytes would put a WAF interstitial in the evidence record as though
    it were the university's page -- and a later extractor finding nothing in it would
    report the university as publishing nothing, which is a false
    `OFFICIALLY_NOT_PUBLISHED` waiting to happen.
    """
    outcome = fetch(server, path)
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_class == "ChallengeInterstitial"
    assert marker in outcome.error_detail
    # The bytes are counted and discarded: nothing is stored as evidence.
    assert outcome.content is None
    assert outcome.content_hash is None
    assert outcome.byte_size is not None and outcome.byte_size > 0


def test_a_real_page_mentioning_a_marker_is_not_mistaken_for_a_challenge(
    server: FixtureServer,
) -> None:
    """Non-vacuity, and the reason the detector requires a small body too.

    An IT-security page that mentions Imperva by name is a real page. A detector that
    refused it would be inventing failures, which is as bad as hiding them.
    """
    outcome = fetch(server, "/mentions-challenge")
    assert outcome.status is FetchStatus.OK
    assert outcome.content is not None
    assert b"_Incapsula_Resource" in outcome.content
    assert outcome.technical_metadata["title"] == "IT Security"


def test_a_challenge_blocks_the_source_from_further_scheduling(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """A blocked source stops being scheduled; it is not retried harder."""
    from app.domains.acquisition.runner import CycleReport, _react

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    outcome = _outcome(
        lease.url,
        content=None,
        status=FetchStatus.BLOCKED,
        http_status=200,
        error_class="ChallengeInterstitial",
    )
    record_outcome(conn, lease=lease, outcome=outcome, store=store)
    _react(conn, lease, outcome, CycleReport(cycle_key="c1"))

    state = conn.execute(
        text(
            "SELECT fetch_eligibility::text AS fe, fetch_eligibility_reason AS why "
            "  FROM source WHERE id = :s"
        ),
        {"s": lease.source_id},
    ).one()
    assert state.fe == "BLOCKED"
    assert "ChallengeInterstitial" in state.why


# ===========================================================================
# A permanent 404 is not retried
# ===========================================================================


def test_a_permanent_status_schedules_no_retry(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """The PolyU case. Asking a fourth time cannot change a 404.

    Contrast with a 500, which is transient and does earn another try -- asserted
    below, so this test proves a distinction rather than the absence of retries.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    assert 404 in PERMANENT_STATUSES and 410 in PERMANENT_STATUSES

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    report = CycleReport(cycle_key="c1")

    gone = _outcome(
        lease.url,
        content=None,
        status=FetchStatus.HTTP_ERROR,
        http_status=404,
        error_class="HTTP404",
    )
    record_outcome(conn, lease=lease, outcome=gone, store=store)
    _react(conn, lease, gone, report)
    assert report.retries_scheduled == 0
    queued = conn.execute(
        text("SELECT count(*) FROM fetch_attempt WHERE source_id = :s AND state = 'QUEUED'"),
        {"s": lease.source_id},
    ).scalar_one()
    assert queued == 0, "a 404 queued a retry"


def test_a_transient_status_does_schedule_a_retry(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    from app.domains.acquisition.runner import CycleReport, _react

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    report = CycleReport(cycle_key="c1")
    broken = _outcome(
        lease.url,
        content=None,
        status=FetchStatus.HTTP_ERROR,
        http_status=500,
        error_class="HTTP500",
    )
    record_outcome(conn, lease=lease, outcome=broken, store=store)
    _react(conn, lease, broken, report)
    assert report.retries_scheduled == 1


# ===========================================================================
# The redirect trail survives a failure
# ===========================================================================


def test_a_failed_fetch_keeps_its_redirect_trail(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """The UBC case. `TIMEOUT, http 301` and nothing else was undiagnosable.

    No snapshot is written for a failed fetch, so the chain has to live on the run --
    it is a fact about the attempt, not about bytes that never arrived.
    """
    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    chain = [
        {"status": 301, "from": lease.url, "to": "https://calendar.example.ac.uk/a"},
        {
            "status": 302,
            "from": "https://calendar.example.ac.uk/a",
            "to": "https://vancouver.calendar.example.ac.uk/admissions",
        },
    ]
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            content=None,
            status=FetchStatus.TIMEOUT,
            http_status=301,
            error_class="ConnectTimeout",
            effective_url="https://vancouver.calendar.example.ac.uk/admissions",
            redirect_chain=chain,
        ),
        store=store,
    )

    run = conn.execute(
        text(
            "SELECT status::text AS status, http_status, effective_url, redirect_chain "
            "  FROM fetch_run WHERE source_id = :s"
        ),
        {"s": lease.source_id},
    ).one()
    assert run.status == "TIMEOUT"
    assert (
        conn.execute(
            text("SELECT count(*) FROM snapshot WHERE source_id = :s"), {"s": lease.source_id}
        ).scalar_one()
        == 0
    )
    assert run.effective_url == "https://vancouver.calendar.example.ac.uk/admissions"
    assert [hop["status"] for hop in run.redirect_chain] == [301, 302]


# ===========================================================================
# Nothing credential-bearing is persisted
# ===========================================================================


def test_only_allowlisted_headers_are_persisted(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 15. An allowlist, so a header nobody anticipated is excluded.

    The server here sends `Set-Cookie` and `Authorization`, which is what a real one
    does; neither may reach the database.
    """
    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            response_headers={
                "content-type": "text/html",
                "etag": '"v1"',
                "set-cookie": "SESSIONID=secret-value; Path=/; HttpOnly",
                "authorization": "Bearer secret-token",
                "x-amz-request-id": "tracing-noise",
                "www-authenticate": "Basic realm=x",
            },
        ),
        store=store,
    )
    headers = conn.execute(
        text("SELECT response_headers FROM snapshot WHERE source_id = :s"),
        {"s": lease.source_id},
    ).scalar_one()
    assert set(headers) == {"content-type", "etag"}
    serialised = str(headers)
    for secret in ("secret-value", "secret-token", "SESSIONID", "Bearer"):
        assert secret not in serialised


def test_a_session_token_in_a_redirect_url_is_redacted(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """A CMS bouncing through `?JSESSIONID=...` must not leave one in the record."""
    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    hostile = "https://portal.example.ac.uk/apply?JSESSIONID=abc123secret&page=2"
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            effective_url=hostile,
            redirect_chain=[{"status": 302, "from": lease.url, "to": hostile}],
        ),
        store=store,
    )
    row = conn.execute(
        text("SELECT effective_url, redirect_chain FROM snapshot WHERE source_id = :s"),
        {"s": lease.source_id},
    ).one()
    assert "abc123secret" not in str(row.effective_url)
    assert "abc123secret" not in str(row.redirect_chain)
    # The rest of the address is untouched: it is still the page we fetched.
    assert "page=2" in row.effective_url
    assert "JSESSIONID" in row.effective_url


def test_a_meaningful_query_is_never_touched() -> None:
    """The UBC case in the other direction: `?tree=...` must survive byte-exact."""
    ubc = "https://www.calendar.ubc.ca/vancouver/index.cfm?tree=2%2C0%2C0%2C0"
    assert redact_url(ubc) == ubc
    assert redact_url("https://x.ac.uk/a") == "https://x.ac.uk/a"
    masked = redact_url("https://x.ac.uk/a?token=abc&tree=2")
    assert "abc" not in str(masked)
    assert "tree=2" in str(masked)
    assert REDACTED in str(masked).replace("%5B", "[").replace("%5D", "]")


# ===========================================================================
# Which evidence is current, through 304s
# ===========================================================================


def test_a_304_resolves_to_the_last_body_bearing_snapshot(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Requirement 23. Step 5C cannot start without this.

    Caltech's PDF is the real case: fetched once, then confirmed unchanged. The
    current evidence is the first observation's bytes, and the confirmation time is
    the later run's -- reporting the older time would say nobody had looked since.
    """
    from datetime import timedelta

    from app.core.clock import utcnow

    _enqueue_one(conn, registered, "c1")
    first = claim_next(conn, cycle_key="c1", worker="w")
    assert first is not None
    original = record_outcome(
        conn, lease=first, outcome=_outcome(first.url, etag='"v1"'), store=store
    )

    # Three consecutive 304s, spaced so "most recent" is unambiguous.
    for index in range(3):
        schedule_retry(
            conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
        )
        nxt = claim_next(conn, cycle_key="c1", worker="w")
        assert nxt is not None
        when = utcnow() + timedelta(seconds=index + 1)
        record_outcome(
            conn,
            lease=nxt,
            outcome=_outcome(
                nxt.url,
                content=None,
                status=FetchStatus.UNCHANGED,
                http_status=304,
                conditional_request_sent=True,
                started_at=when,
                finished_at=when,
            ),
            store=store,
            previous_content_hash=original.content_hash,
        )

    current = latest_effective_evidence(conn, first.source_id)
    assert current.has_evidence
    assert current.snapshot_id == original.snapshot_id
    assert current.content_hash == original.content_hash
    assert current.resolved_through_unchanged is True
    assert current.unchanged_runs_since == 3
    # Seen once, confirmed three times later. Both facts survive.
    assert current.confirmed_at is not None and current.observed_at is not None
    assert current.confirmed_at > current.observed_at
    # And no snapshot was invented for any of the 304s.
    assert (
        conn.execute(
            text("SELECT count(*) FROM snapshot WHERE source_id = :s"), {"s": first.source_id}
        ).scalar_one()
        == 1
    )


def test_a_later_200_supersedes_the_resolved_snapshot(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Non-vacuity: resolution follows the history rather than pinning the first."""
    from datetime import timedelta

    from app.core.clock import utcnow

    _enqueue_one(conn, registered, "c1")
    first = claim_next(conn, cycle_key="c1", worker="w")
    assert first is not None
    record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)

    schedule_retry(conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0)
    second = claim_next(conn, cycle_key="c1", worker="w")
    assert second is not None
    later = utcnow() + timedelta(seconds=5)
    fresh = record_outcome(
        conn,
        lease=second,
        outcome=_outcome(second.url, content=HTML_CHANGED, started_at=later, finished_at=later),
        store=store,
    )

    current = latest_effective_evidence(conn, first.source_id)
    assert current.snapshot_id == fresh.snapshot_id
    assert current.resolved_through_unchanged is False
    assert current.unchanged_runs_since == 0


def test_a_source_with_only_failures_has_no_evidence(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Oxford and Monash, after the smoke run. Better than something plausible."""
    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    record_outcome(
        conn,
        lease=lease,
        outcome=_outcome(
            lease.url,
            content=None,
            status=FetchStatus.BLOCKED,
            http_status=403,
            error_class="HTTP403",
        ),
        store=store,
    )
    current = latest_effective_evidence(conn, lease.source_id)
    assert current.has_evidence is False
    assert current.snapshot_id is None
    assert current.content_hash is None


def test_a_never_fetched_source_has_no_evidence(
    conn: Connection, registered: dict[str, Any]
) -> None:
    current = latest_effective_evidence(conn, registered["source_ids"][0])
    assert current.has_evidence is False
    assert current.unchanged_runs_since == 0


# ===========================================================================
# The smoke set itself
# ===========================================================================


def test_the_smoke_set_is_well_formed() -> None:
    """It is checked in so the run is reproducible; this keeps it loadable.

    Deliberately does *not* contact anything, and does not require the URLs to be
    imported: a test that depended on a database import or on a university being up
    would be exactly the flaky network test section 25 forbids.
    """
    import sys
    from pathlib import Path

    api_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(api_root))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_smoke_cli", api_root / "scripts" / "acquisition_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its annotations through `sys.modules[cls.__module__]`,
    # which is absent for a module loaded by path -- so register it first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    pages = module.load_smoke_set()
    assert len(pages) == 12
    assert len({page.url for page in pages}) == 12, "a URL appears twice"
    assert len({page.institution for page in pages}) == 12, "an institution appears twice"
    # All six destinations, so the run exercises every region the pilot covers.
    assert {page.destination for page in pages} == {"AU", "CA", "GB", "HK", "SG", "US"}
    # The two structurally unusual URLs are present by construction.
    assert any(url.endswith(".pdf") for url in (page.url for page in pages))
    assert any("?" in page.url for page in pages)
    assert any(page.category == "UNCLASSIFIED" for page in pages)
    # Never relaxed, whatever a real site does.
    assert module.SMOKE_POLICY.verify_tls is True
    assert module.SMOKE_POLICY.pin_addresses is True
    assert module.SMOKE_GLOBAL_CONCURRENCY <= 2
    assert module.SMOKE_HOST_INTERVAL_SECONDS >= 3.0


def test_no_automated_test_can_reach_a_real_host() -> None:
    """Requirement 25/26. Automated tests use fixtures, and only fixtures.

    Asserted as the property that actually holds rather than by scanning for
    hostnames: several tests legitimately pass a real URL to `check_url_shape`, which
    performs no I/O, and a text scan would flag those while missing a fetch of
    `some-other.example`.

    What makes the suite safe is that `make_fetcher` -- the only fetcher constructor
    under `tests/` -- always injects the fixture resolver, and that resolver permits
    exactly one address: the loopback fixture server's. Anything else is handed to the
    production guard, which refuses loopback and private space. So a test cannot reach
    a real host even if someone writes one into a URL.
    """
    import asyncio

    from app.domains.acquisition.netsafety import (
        UnsafeTargetError,
        VettedTarget,
        resolve_and_validate,
    )

    with FixtureServer() as fixture:
        fetcher = make_fetcher(fixture)
        # The resolver is injected, not the default.
        assert fetcher._resolve is not None
        assert fetcher._resolve is not resolve_and_validate

        async def resolve(url: str) -> VettedTarget:
            return await fetcher._resolve(url)

        # It permits the fixture's own address...
        target = asyncio.run(resolve(fixture.url("/ok")))
        assert target.addresses == ("127.0.0.1",)

        # ...and delegates everything else to the real guard, which refuses it.
        for hostile in ("http://127.0.0.1:6379/", "http://169.254.169.254/", "http://10.0.0.1/"):
            with pytest.raises(UnsafeTargetError):
                asyncio.run(resolve(hostile))


def test_the_smoke_set_is_the_only_place_real_hosts_are_named() -> None:
    """And it is an input, not a test: nothing under `tests/` loads it to fetch from."""
    from pathlib import Path as _Path

    api_root = _Path(__file__).resolve().parents[2]
    smoke = api_root / "smoke" / "acquisition_smoke_set.toml"
    assert smoke.is_file()
    # The only test that reads it asserts its shape; none fetches from it.
    readers = [
        path.name
        for path in (api_root / "tests").rglob("*.py")
        if "acquisition_smoke_set" in path.read_text(encoding="utf-8")
        or "load_smoke_set" in path.read_text(encoding="utf-8")
    ]
    assert readers == [_Path(__file__).name]


# ===========================================================================
# Health, and the three grains a report must not merge
#
# These exist because the Step 5B.1 report got two counts wrong, in the same way
# twice: it read a number off an earlier run instead of asking the database. The
# defence against that is not proof-reading -- it is having the projection assert
# what the prose claimed, so each of these is a count that report stated.
# ===========================================================================


def _health(conn: Connection, source_id: Any) -> Any:
    return conn.execute(
        text(
            "SELECT health, last_status, last_http_status, consecutive_failures, "
            "       last_content_hash, last_content_type, total_runs "
            "  FROM source_health WHERE source_id = :s"
        ),
        {"s": source_id},
    ).one()


def test_a_challenged_source_reports_blocked_health_not_healthy(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """The counting error, as an assertion.

    The report said `HEALTHY 9 / BLOCKED 2` while also saying NUS was blocked, which
    cannot both be true. `test_a_challenge_blocks_the_source_from_further_scheduling`
    proved `source.fetch_eligibility` moved; nothing proved the **health projection**
    followed, and the projection is what the report was quoting.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    # A real body first, so this is the hard case rather than the easy one: the source
    # has succeeded before, and the view must still say BLOCKED afterwards.
    _enqueue_one(conn, registered, "c1")
    first = claim_next(conn, cycle_key="c1", worker="w")
    assert first is not None
    record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)
    assert _health(conn, first.source_id).health == "HEALTHY"

    schedule_retry(
        conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
    )
    second = claim_next(conn, cycle_key="c1", worker="w")
    assert second is not None
    challenge = _outcome(
        second.url,
        content=None,
        status=FetchStatus.BLOCKED,
        http_status=200,
        error_class="ChallengeInterstitial",
    )
    record_outcome(conn, lease=second, outcome=challenge, store=store)
    _react(conn, second, challenge, CycleReport(cycle_key="c1"))

    state = _health(conn, second.source_id)
    assert state.health == "BLOCKED"
    # BLOCKED outranks the streak: one failure alone would read DEGRADED.
    assert state.consecutive_failures == 1
    assert state.last_status == "BLOCKED"
    assert state.last_http_status == 200


def test_a_blocked_source_still_resolves_to_its_last_real_body(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """A hazard for Step 5C, characterised rather than papered over.

    Evidence is append-only, so blocking a source does not retract a snapshot taken
    before it was blocked. `latest_effective_evidence` therefore still returns a body
    for a source that is now refusing us. That is correct -- the bytes really were
    observed -- and it is dangerous, because if that stored body were a challenge
    captured before the detector existed, an extractor would attribute a WAF page to
    the university.

    So the guarantee is narrower than "no challenge is ever readable": it is that no
    challenge is stored **from now on**, and that health says BLOCKED so a reader has
    to notice. Step 5C must consult health, not merely ask for the latest evidence.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    _enqueue_one(conn, registered, "c1")
    first = claim_next(conn, cycle_key="c1", worker="w")
    assert first is not None
    record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)
    body_hash = _health(conn, first.source_id).last_content_hash
    assert body_hash is not None

    schedule_retry(
        conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
    )
    second = claim_next(conn, cycle_key="c1", worker="w")
    assert second is not None
    challenge = _outcome(
        second.url,
        content=None,
        status=FetchStatus.BLOCKED,
        http_status=200,
        error_class="ChallengeInterstitial",
    )
    record_outcome(conn, lease=second, outcome=challenge, store=store)
    _react(conn, second, challenge, CycleReport(cycle_key="c1"))

    current = latest_effective_evidence(conn, second.source_id)
    assert current.has_evidence
    assert current.content_hash == body_hash
    # The blocked attempt contributed nothing to it.
    assert current.resolved_through_unchanged is False
    assert _health(conn, second.source_id).health == "BLOCKED"


def test_a_challenge_adds_no_snapshot_and_no_blob(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """The content-type grain error, as an assertion.

    The report counted NUS once as a successful `text/html` page and again as a
    discarded challenge, which is how eight pages became nine. A challenge response is
    genuinely observed -- it has a status, a byte count and a content type on the wire
    -- and it contributes **no** snapshot and **no** blob. Different grains; the only
    way to keep them straight is to count them separately.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    source_ids = registered["source_ids"]
    counts = (
        "SELECT (SELECT count(*) FROM snapshot WHERE source_id = ANY(:ids)) AS snaps, "
        "       (SELECT count(*) FROM content_blob) AS blobs"
    )
    before = conn.execute(text(counts), {"ids": source_ids}).one()

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    challenge = _outcome(
        lease.url,
        content=None,
        status=FetchStatus.BLOCKED,
        http_status=200,
        error_class="ChallengeInterstitial",
    )
    record_outcome(conn, lease=lease, outcome=challenge, store=store)
    _react(conn, lease, challenge, CycleReport(cycle_key="c1"))

    after = conn.execute(text(counts), {"ids": source_ids}).one()
    assert after.snaps == before.snaps
    assert after.blobs == before.blobs
    # The run itself is on the record, with the status the server actually sent.
    run = conn.execute(
        text(
            "SELECT status::text AS status, http_status, error_class, bytes_downloaded "
            "  FROM fetch_run WHERE source_id = :s"
        ),
        {"s": lease.source_id},
    ).one()
    assert run.status == "BLOCKED"
    assert run.http_status == 200
    assert run.error_class == "ChallengeInterstitial"


def test_snapshots_sources_and_blobs_are_three_different_counts(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """One source, two observations of identical bytes: 1 source, 2 snapshots, 1 blob.

    Stated as a test because the report merged these grains. A blob is a distinct
    body, a snapshot is an occasion on which a body was seen, and a source is a page --
    so re-fetching an unchanged page increases exactly one of the three.
    """
    _enqueue_one(conn, registered, "c1")
    first = claim_next(conn, cycle_key="c1", worker="w")
    assert first is not None
    record_outcome(conn, lease=first, outcome=_outcome(first.url), store=store)

    schedule_retry(
        conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
    )
    second = claim_next(conn, cycle_key="c1", worker="w")
    assert second is not None
    # Identical bytes on a genuine second 200: a new observation of the same body.
    record_outcome(conn, lease=second, outcome=_outcome(second.url), store=store)

    counted = conn.execute(
        text(
            "SELECT count(*) AS snapshots, count(DISTINCT content_hash) AS blobs, "
            "       count(DISTINCT source_id) AS sources "
            "  FROM snapshot WHERE source_id = :s"
        ),
        {"s": first.source_id},
    ).one()
    assert (counted.sources, counted.snapshots, counted.blobs) == (1, 2, 1)


# ===========================================================================
# "Redirected off host" meant "the URL changed at all" (C36)
#
# Found by auditing the counts in the Step 5B.1 report rather than by a failing
# test: `assist --moved` returned four pages when only three had changed host, and
# the summary counted snapshots where it said pages. Both came from using
# `effective_url IS NOT NULL` as a stand-in for "the host differs" -- and
# `effective_url` is written whenever the final URL differs as a *string*, which a
# trailing slash or an http->https upgrade is enough to do.
# ===========================================================================


def _observe(
    conn: Connection,
    registered: dict[str, Any],
    store: Any,
    *,
    effective: str | None = None,
    content: bytes | None = None,
    cycle: str = "c1",
) -> Any:
    """Record one observation, optionally one that ended on `effective`.

    The redirect is part of the outcome rather than an edit afterwards, because
    `snapshot` is append-only and a trigger enforces that -- correcting history is not
    something this system permits, so a test must not need to.
    """
    _enqueue_one(conn, registered, cycle)
    lease = claim_next(conn, cycle_key=cycle, worker="w")
    assert lease is not None
    outcome = _outcome(lease.url) if content is None else _outcome(lease.url, content=content)
    if effective is not None:
        outcome.effective_url = effective
    record_outcome(conn, lease=lease, outcome=outcome, store=store)
    return lease


def _url_of(conn: Connection, source_id: Any) -> str:
    return str(
        conn.execute(text("SELECT url FROM source WHERE id = :s"), {"s": source_id}).scalar_one()
    )


def test_a_same_host_redirect_is_not_counted_as_moving_off_host(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Toronto's page redirects within its own host. It has not gone anywhere.

    This is the count an operator reads as "a university sent us somewhere else", so a
    trailing-slash redirect inflating it is not cosmetic -- it manufactures a source
    review finding out of nothing.
    """
    from app.domains.acquisition.reporting import acquisition_summary

    # Claim the page first so its own URL is known, then re-observe it with a
    # same-host redirect recorded on the outcome.
    first = _observe(conn, registered, store)
    landing = _url_of(conn, first.source_id).rstrip("/") + "/index.html"
    schedule_retry(
        conn, source_id=first.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
    )
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    outcome = _outcome(lease.url, content=HTML_CHANGED)
    outcome.effective_url = landing
    record_outcome(conn, lease=lease, outcome=outcome, store=store)

    stored = conn.execute(
        text(
            "SELECT effective_url FROM snapshot WHERE source_id = :s "
            " ORDER BY observed_at DESC LIMIT 1"
        ),
        {"s": first.source_id},
    ).scalar_one()
    assert stored == landing, "the same-host redirect was not recorded at all"

    moved = acquisition_summary(conn, pilot_only=False).pages_redirecting_off_host
    assert moved == 0, "a same-host redirect was counted as moving off host"


def test_a_genuine_off_host_redirect_is_counted_once_per_page(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Non-vacuity for the test above, and the grain: pages, not observations.

    HKU's page really does land on another host, and it did so on all three cycles of
    the smoke run. That is one page that moved, not three.
    """
    from app.domains.acquisition.reporting import acquisition_summary

    elsewhere = "https://portal.elsewhere.example/landing"
    lease = _observe(conn, registered, store, effective=elsewhere)
    assert acquisition_summary(conn, pilot_only=False).pages_redirecting_off_host == 1

    # A second observation of the same page moving to the same place.
    schedule_retry(
        conn, source_id=lease.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10
    )
    again = claim_next(conn, cycle_key="c1", worker="w")
    assert again is not None
    second = _outcome(again.url, content=HTML_CHANGED)
    second.effective_url = elsewhere
    record_outcome(conn, lease=again, outcome=second, store=store)

    snapshots = conn.execute(
        text("SELECT count(*) FROM snapshot WHERE source_id = :s"), {"s": lease.source_id}
    ).scalar_one()
    assert snapshots == 2, "the second observation was not recorded"
    # Two observations, still one page that moved.
    assert acquisition_summary(conn, pilot_only=False).pages_redirecting_off_host == 1


def test_the_moved_filter_and_the_moved_count_agree(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """`report` and `assist --moved` disagreed: four pages listed, three had moved.

    Two outputs answering one question have to answer it the same way, or an operator
    reconciling them concludes the data is wrong when it is the code.
    """
    from app.domains.acquisition.reporting import acquisition_summary, verification_assistance

    same = _observe(conn, registered, store)
    landing = _url_of(conn, same.source_id).rstrip("/") + "/"
    schedule_retry(conn, source_id=same.source_id, cycle_key="c1", delay_seconds=0, max_attempts=10)
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    outcome = _outcome(lease.url, content=HTML_CHANGED)
    outcome.effective_url = landing
    record_outcome(conn, lease=lease, outcome=outcome, store=store)

    assert len(verification_assistance(conn, only_host_differs=True)) == 0
    assert acquisition_summary(conn, pilot_only=False).pages_redirecting_off_host == 0

    # And when one genuinely moves, both see exactly it.
    moved = _observe(conn, registered, store, effective="https://other.example/x", cycle="c2")
    listed = verification_assistance(conn, only_host_differs=True)
    assert acquisition_summary(conn, pilot_only=False).pages_redirecting_off_host == 1
    assert [row.source_id for row in listed] == [moved.source_id]
    assert listed[0].host_differs is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.ac.uk/",
        "https://www.example.ac.uk/path/to/page",
        "https://WWW.Example.AC.UK/MixedCase",
        "https://www.example.ac.uk:8443/with-port",
        "http://sub.domain.example.com/a?b=c#d",
        "https://www.example.ac.uk?query=first",
        "https://www.example.ac.uk#fragment-first",
        "https://calendar.ubc.ca/vancouver/index.cfm?tree=2%2C0%2C0%2C0",
    ],
)
def test_sql_and_python_agree_on_the_host_of_a_url(conn: Connection, url: str) -> None:
    """One definition, two implementations, kept honest by a test.

    The off-host predicate has to run in SQL (it is a count over every snapshot) and the
    assist rows compute the same thing in Python. Two implementations of one definition
    drift silently, so the agreement is asserted rather than assumed.
    """
    from app.domains.acquisition.reporting import _SQL_HOST_OF, _host_of

    in_sql = conn.execute(
        text(f"SELECT {_SQL_HOST_OF.format(':u')} AS host"), {"u": url}
    ).scalar_one()
    assert in_sql == _host_of(url), f"SQL said {in_sql!r}, Python said {_host_of(url)!r}"


# ===========================================================================
# One page cited by two institutions is still one page (C37)
# ===========================================================================


def test_a_page_two_institutions_cite_is_counted_once(
    conn: Connection, pilot: dict[str, Any]
) -> None:
    """`acquisition_target` is one row per page **per institution**, not per page.

    A URL's uniqueness is enforced per institution (D31) while `source` is unique on
    `url_hash` globally, so a page both institutions claim gets two view rows and one
    source. Counting the view's rows therefore reported one page as two, and summing
    its per-source `claim_count` counted every one of that page's claims twice.

    The client's current workbook happens to be 1:1 -- 319 sources, 319 non-duplicate
    physical rows -- so this was latent. It is tested because "latent" means "wrong as
    soon as two universities cite one government fee table", which is a normal thing
    for two universities to do.
    """
    from app.domains.acquisition.registration import register_acquisition_targets
    from app.domains.acquisition.reporting import acquisition_summary

    # Beta claims alpha's page: a second non-duplicate physical row for one URL.
    _claim(
        conn,
        pilot["submission_id"],
        pilot["target_beta"],
        "S0006",
        "PROGRAM_CATALOG",
        pilot["shared_url"],
        None,
    )
    register_acquisition_targets(conn, submission_id=pilot["submission_id"])

    fanned = conn.execute(
        text("SELECT count(*) FROM acquisition_target WHERE url = :u"),
        {"u": pilot["shared_url"]},
    ).scalar_one()
    assert fanned == 2, "the fan-out this test is about did not happen"
    sources = conn.execute(
        text("SELECT count(*) FROM source WHERE url = :u"), {"u": pilot["shared_url"]}
    ).scalar_one()
    assert sources == 1, "one URL should be one source"

    summary = acquisition_summary(conn, pilot_only=True)
    # Three distinct URLs: alpha's /apply, alpha's /fees, beta's /leaflet.
    assert summary.physical_pages == 3, "a shared page was counted twice"
    # Six claims: four on the shared page, one each on the other two.
    assert summary.responsibility_claims == 6, "the shared page's claims were counted twice"
    assert summary.institutions == 2
    assert summary.hosts == 2
    # And the eligibility counts partition the pages, not the view's rows.
    assert summary.fetchable == 3


def test_the_summary_totals_are_scoped_like_the_rest_of_the_summary(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """`pilot_only=True` has to mean the same thing for every field in one sentence.

    The evidence totals were global while the page counts were pilot-filtered, so
    `summary()` rendered a filtered numerator against whole-database totals -- the
    same mixed-population error as C35, in shipped code.
    """
    from app.domains.acquisition.reporting import acquisition_summary

    _enqueue_one(conn, registered, "c1")
    lease = claim_next(conn, cycle_key="c1", worker="w")
    assert lease is not None
    record_outcome(conn, lease=lease, outcome=_outcome(lease.url), store=store)

    # A source outside the pilot, with its own run, snapshot and body.
    outside = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy, publication_eligibility, fetch_eligibility) "
            "VALUES (:id, :url, :hash, 'university_site', 'WEEKLY', 'STATIC', "
            "        'NOT_ELIGIBLE', 'FETCHABLE')"
        ),
        {
            "id": outside,
            "url": "https://outside.example.org/page",
            "hash": hashlib.sha256(b"https://outside.example.org/page").hexdigest(),
        },
    )
    pilot_scoped = acquisition_summary(conn, pilot_only=True)
    everything = acquisition_summary(conn, pilot_only=False)

    # The pilot view does not see the outside source at all...
    assert pilot_scoped.physical_pages == 3
    assert pilot_scoped.snapshots == 1
    assert pilot_scoped.blobs == 1
    assert pilot_scoped.total_runs == 1
    # ...and the unfiltered view counts pages by the same rule it counts evidence.
    assert everything.snapshots == 1
    assert everything.total_runs == 1


# ===========================================================================
# Reporting buckets must partition, not overlap (Step 5C.1 section 0)
#
# Two defects found by reconciling the Step 5B.3 report against the database:
#
#   A. the follow-up buckets folded TLS, NXDOMAIN and HTTP 202 into one coarse
#      `SOURCE_REVIEW_REQUIRED`, and restating that in prose mixed a *run* count
#      (5 × HTTP 202) into a *page* total (2 pages);
#   B. the DNS section's "transport error" predicate matched `Connect%`, catching
#      `ConnectTimeout` as well, so its count overlapped TIMEOUT.
#
# Both are the same failure: a list of counts that do not partition their population
# invites double-counting by whoever reads it. So the property asserted is the
# partition itself.
# ===========================================================================


def _no_evidence_buckets(conn: Connection) -> dict[str, int]:
    """The follow-up bucketing, as the report computes it."""
    rows = conn.execute(
        text(
            "SELECT h.last_status, h.last_http_status, h.last_error_class, h.schedule_state "
            "  FROM source_health h WHERE h.last_content_hash IS NULL"
        )
    ).all()
    buckets: dict[str, int] = {}
    for row in rows:
        error = row.last_error_class or ""
        if row.last_status is None:
            key = "NOT_YET_ATTEMPTED"
        elif row.last_http_status in (404, 410):
            key = "DEAD_URL"
        elif row.last_status == "BLOCKED":
            key = "BLOCKED_ACCESS"
        elif row.schedule_state == "COOLDOWN" or row.last_status == "RATE_LIMITED":
            key = "RETRY_AFTER_COOLDOWN"
        elif "CERTIFICATE_VERIFY_FAILED" in error:
            key = "TLS_CHAIN_FAILURE"
        elif row.last_status == "NAME_NOT_RESOLVED":
            key = "NAME_NOT_RESOLVED"
        elif row.last_status in ("TIMEOUT", "DNS_TEMPORARY"):
            key = "TEMPORARY_NETWORK_FAILURE"
        elif row.last_status == "INTERNAL_ERROR":
            key = "INTERNAL_ERROR"
        elif row.last_http_status is not None:
            key = "UNEXPECTED_HTTP_STATUS"
        else:
            key = "SOURCE_REVIEW_OTHER"
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def test_the_no_evidence_buckets_partition_their_population(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Defect A. Every page with no body lands in exactly one bucket.

    Built from three pages that fail three different ways -- shapes the real fleet
    produced -- one page each, because `source_health` reports a page's *latest*
    status and reusing a source would simply overwrite the earlier failure.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "c-dead",
            {"http_status": 404, "error_class": "HTTP404", "status": FetchStatus.HTTP_ERROR},
        ),
        (
            "c-blocked",
            {"http_status": 403, "error_class": "HTTP403", "status": FetchStatus.BLOCKED},
        ),
        (
            "c-tls",
            {
                "error_class": "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate",
                "status": FetchStatus.HTTP_ERROR,
            },
        ),
    )
    assert len(registered["source_ids"]) >= len(cases), "one distinct page per failure shape"
    for index, (cycle, kwargs) in enumerate(cases):
        source_id = registered["source_ids"][index]
        enqueue_cycle(conn, cycle_key=cycle, source_ids=[source_id])
        lease = claim_next(conn, cycle_key=cycle, worker="w")
        assert lease is not None
        outcome = _outcome(lease.url, content=None, **kwargs)
        record_outcome(conn, lease=lease, outcome=outcome, store=store)
        _react(conn, lease, outcome, CycleReport(cycle_key=cycle))

    total = conn.execute(
        text("SELECT count(*) FROM source_health WHERE last_content_hash IS NULL")
    ).scalar_one()
    buckets = _no_evidence_buckets(conn)

    assert (
        sum(buckets.values()) == total
    ), f"the buckets sum to {sum(buckets.values())} but {total} pages have no evidence"
    # And each distinct failure is distinguishable, which is the point of splitting:
    # under the old coarse bucketing these three were one number.
    assert buckets.get("DEAD_URL", 0) == 1
    assert buckets.get("BLOCKED_ACCESS", 0) == 1
    assert buckets.get("TLS_CHAIN_FAILURE", 0) == 1
    assert "SOURCE_REVIEW_REQUIRED" not in buckets, "the coarse bucket is back"


def test_a_tls_failure_is_not_counted_as_an_unexpected_status(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Non-vacuity for the partition: a TLS failure has no HTTP status at all.

    It would fall into `UNEXPECTED_HTTP_STATUS` only if the TLS branch were removed,
    and into neither if the ordering were wrong -- so this pins the ordering.
    """
    from app.domains.acquisition.runner import CycleReport, _react

    enqueue_cycle(conn, cycle_key="tls", source_ids=[registered["source_ids"][0]])
    lease = claim_next(conn, cycle_key="tls", worker="w")
    assert lease is not None
    outcome = _outcome(
        lease.url,
        content=None,
        status=FetchStatus.HTTP_ERROR,
        error_class="ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get issuer",
    )
    record_outcome(conn, lease=lease, outcome=outcome, store=store)
    _react(conn, lease, outcome, CycleReport(cycle_key="tls"))

    buckets = _no_evidence_buckets(conn)
    assert buckets.get("TLS_CHAIN_FAILURE", 0) == 1
    assert buckets.get("UNEXPECTED_HTTP_STATUS", 0) == 0


def test_the_timeout_and_transport_error_buckets_do_not_overlap(
    conn: Connection, registered: dict[str, Any], store: Any
) -> None:
    """Defect B. `ConnectTimeout` is a timeout, and must be counted once.

    The old predicate was `error_class LIKE 'Connect%'`, which matched both
    `ConnectError` and `ConnectTimeout`, so a connect timeout appeared under TIMEOUT
    *and* under "transport error".
    """
    from app.domains.acquisition.runner import CycleReport, _react

    shapes = (
        ("to", FetchStatus.TIMEOUT, "ConnectTimeout: timed out"),
        ("te", FetchStatus.HTTP_ERROR, "ConnectError: transport error"),
    )
    for index, (cycle, status, error_class) in enumerate(shapes):
        source_id = registered["source_ids"][index]
        enqueue_cycle(conn, cycle_key=cycle, source_ids=[source_id])
        lease = claim_next(conn, cycle_key=cycle, worker="w")
        assert lease is not None
        outcome = _outcome(lease.url, content=None, status=status, error_class=error_class)
        record_outcome(conn, lease=lease, outcome=outcome, store=store)
        _react(conn, lease, outcome, CycleReport(cycle_key=cycle))

    timeouts = conn.execute(
        text("SELECT count(*) FROM fetch_run WHERE status = 'TIMEOUT'")
    ).scalar_one()
    # The report's corrected predicate: transport errors that are NOT timeouts and NOT
    # certificate failures.
    transport = conn.execute(
        text(
            "SELECT count(*) FROM fetch_run r WHERE r.status <> 'TIMEOUT' "
            "  AND r.error_class NOT LIKE '%CERTIFICATE_VERIFY_FAILED%' "
            "  AND (r.error_class LIKE 'ConnectError%' OR r.error_class LIKE 'Transport%')"
        )
    ).scalar_one()
    # The old, overlapping predicate, for contrast.
    overlapping = conn.execute(
        text(
            "SELECT count(*) FROM fetch_run r "
            "  WHERE r.error_class LIKE 'Connect%' OR r.error_class LIKE 'Transport%'"
        )
    ).scalar_one()

    assert timeouts == 1
    assert transport == 1
    assert overlapping == 2, "the old predicate no longer demonstrates the overlap"
    assert (
        timeouts + transport == overlapping
    ), "the corrected buckets must together cover what the old one did, once each"
