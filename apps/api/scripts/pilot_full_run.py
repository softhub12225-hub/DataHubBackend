"""The one watched full-pilot acquisition cycle (Step 5B.3).

Usage::

    uv run python apps/api/scripts/pilot_full_run.py --cycle pilot-full-001 --i-understand

**This contacts 120 real university websites for 319 pages.** It refuses to start
without `--i-understand`, and there is no flag that makes it faster, relaxes TLS or
softens the SSRF guard.

WHY THIS IS A SEPARATE SCRIPT
=============================
`acquisition.py worker` is the general command and carries general defaults -- a 2s
host floor and a 60s total budget. The configuration approved for the *first complete
run over the real fleet* is stricter than that in the direction of politeness, and
baking it into a named script means the approved settings are a thing you can read
rather than a set of flags someone has to remember to pass.

It is also deliberately slow. Effective concurrency is 1 (`run_cycle` awaits each page
before claiming the next), one request in flight per host, five seconds between
requests to the same host. For 319 pages that is a floor of roughly half an hour, and
the point of the exercise is correctness and politeness rather than throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.fetcher import USER_AGENT, FetchPolicy, StaticHttpFetcher
from app.domains.acquisition.runner import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_STRIKE_LIMIT,
    HostGate,
    run_cycle,
)
from app.domains.acquisition.storage import build_evidence_store

#: The approved configuration for the first full run. Every value is more conservative
#: than the general default, and `total_timeout_seconds` is 90 rather than 60 because
#: the Step 5B.1 smoke run met a four-hop redirect chain whose later connects needed
#: room -- a distance and hop-count budget, not a defect in pinning.
FULL_RUN_POLICY = FetchPolicy(
    connect_timeout_seconds=15.0,
    read_timeout_seconds=30.0,
    total_timeout_seconds=90.0,
    verify_tls=True,
    pin_addresses=True,
)

#: Five seconds between requests to one host. One pilot host serves eleven pages, and
#: the cost of being impolite on first contact is being blocked for the whole pilot.
HOST_INTERVAL_SECONDS = 5.0

#: Effectively 1 regardless: `run_cycle` awaits each page before claiming the next.
#: Passed explicitly so the report can state what was asked for as well as what the
#: loop actually does.
GLOBAL_CONCURRENCY = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cycle", required=True, help="the cycle key to drain")
    parser.add_argument("--evidence-root", type=Path, default=Path(".evidence-full"))
    parser.add_argument("--i-understand", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="stop after this many pages (for a rehearsal); default: drain the cycle",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole.WORKER), future=True)

    with engine.connect() as connection:
        queued = connection.execute(
            text(
                "SELECT count(*) FROM fetch_attempt " " WHERE cycle_key = :c AND state = 'QUEUED'"
            ),
            {"c": args.cycle},
        ).scalar_one()
        hosts = connection.execute(
            text(
                "SELECT count(DISTINCT lower(split_part(split_part(s.url, '://', 2), '/', 1))) "
                "  FROM fetch_attempt a JOIN source s ON s.id = a.source_id "
                " WHERE a.cycle_key = :c AND a.state = 'QUEUED'"
            ),
            {"c": args.cycle},
        ).scalar_one()

    if not args.i_understand:
        print(
            f"refusing: this would contact {hosts} real university websites for "
            f"{queued} page(s). Re-run with --i-understand.",
            file=sys.stderr,
        )
        return 2
    if queued == 0:
        print(f"nothing queued in cycle {args.cycle!r}.", file=sys.stderr)
        return 2

    worker_name = f"full:{uuid.uuid4().hex[:8]}"
    print(f"CYCLE            {args.cycle}")
    print(f"queued pages     {queued} over {hosts} host(s)")
    print(f"worker           {worker_name}")
    print(f"User-Agent       {USER_AGENT}")
    print(
        f"policy           connect {FULL_RUN_POLICY.connect_timeout_seconds}s, "
        f"read {FULL_RUN_POLICY.read_timeout_seconds}s, "
        f"total {FULL_RUN_POLICY.total_timeout_seconds}s, "
        f"max {FULL_RUN_POLICY.max_bytes // (1024 * 1024)}MB"
    )
    print(
        f"safety           TLS verify {FULL_RUN_POLICY.verify_tls}, "
        f"pin {FULL_RUN_POLICY.pin_addresses}, redirects revalidated per hop"
    )
    print(
        f"politeness       global {GLOBAL_CONCURRENCY} (effectively 1), per-host 1, "
        f"{HOST_INTERVAL_SECONDS}s same-host floor"
    )
    print(
        f"rate limits      Retry-After obeyed; {DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS:.0f}s "
        f"fallback; review after {RATE_LIMIT_STRIKE_LIMIT} strikes"
    )
    print("evidence store   FILESYSTEM (Docker unavailable; S3/MinIO NOT exercised)")
    print(f"started          {datetime.now(UTC).isoformat()}")
    print(flush=True)

    started_wall = time.monotonic()
    started_at = datetime.now(UTC)
    report = asyncio.run(
        run_cycle(
            engine,
            cycle_key=args.cycle,
            store=build_evidence_store(get_settings(), local_root=args.evidence_root),
            worker=worker_name,
            fetcher=StaticHttpFetcher(FULL_RUN_POLICY),
            gate=HostGate(min_interval_seconds=HOST_INTERVAL_SECONDS),
            max_pages=args.max_pages,
            global_concurrency=GLOBAL_CONCURRENCY,
        )
    )
    elapsed = time.monotonic() - started_wall
    finished_at = datetime.now(UTC)

    print()
    print(report.summary())
    print()
    print(f"started          {started_at.isoformat()}")
    print(f"finished         {finished_at.isoformat()}")
    print(f"elapsed          {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"requests sent    {len(report.urls_contacted)}")
    if report.urls_contacted:
        print(f"mean per page    {elapsed / len(report.urls_contacted):.2f}s (wall, incl. waits)")
    print(
        "\nEvidence captured. No field_claim, extraction or change proposal was created, "
        "and nothing became publication eligible."
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
