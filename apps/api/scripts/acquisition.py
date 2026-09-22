"""Acquisition operations for the pilot (Step 5B section 25).

Usage::

    uv run python apps/api/scripts/acquisition.py register --dry-run
    uv run python apps/api/scripts/acquisition.py enqueue --pilot --dry-run
    uv run python apps/api/scripts/acquisition.py enqueue --university <target_id>
    uv run python apps/api/scripts/acquisition.py enqueue --source <source_id>
    uv run python apps/api/scripts/acquisition.py worker --max-pages 5
    uv run python apps/api/scripts/acquisition.py report
    uv run python apps/api/scripts/acquisition.py assist --moved

`register` creates one `source` per distinct URL. That is an acquisition target and
**not** a trust signal: every source is created `NOT_ELIGIBLE` for publication, and
only a verified domain plus a promoted mapping can change that.

`worker` contacts real websites. It prints every URL it contacted, keeps one request
in flight per host, obeys `Retry-After`, and refuses to start without `--i-understand`
unless `--max-pages` is small. There is no flag that makes it faster per host.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition import recovery
from app.domains.acquisition.lease import enqueue_cycle
from app.domains.acquisition.registration import (
    assert_nothing_became_publishable,
    register_acquisition_targets,
)
from app.domains.acquisition.reporting import (
    acquisition_summary,
    health_by_institution,
    verification_assistance,
)
from app.domains.acquisition.runner import run_cycle
from app.domains.acquisition.storage import build_evidence_store
from app.workers.tasks.crawl import cycle_key_for


class _RollbackError(Exception):
    """Internal: unwinds the transaction after a dry run."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--role",
        default=None,
        choices=[role.value for role in DatabaseRole],
        help="database identity to act as. Defaults per command: the fetch plane "
        "(fetch_attempt, fetch_run, snapshot, content_blob) is writable only by "
        "app_worker, and reading is app_api's job.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="create a source per distinct pilot URL")
    register.add_argument("--dry-run", action="store_true")
    register.add_argument("--all-institutions", action="store_true")
    register.add_argument("--by", type=uuid.UUID, default=None, metavar="USER_ID")

    enqueue = sub.add_parser("enqueue", help="queue one attempt per physical page")
    enqueue.add_argument("--pilot", action="store_true", help="every pilot institution")
    enqueue.add_argument("--university", type=uuid.UUID, default=None, metavar="TARGET_ID")
    enqueue.add_argument("--source", type=uuid.UUID, default=None, metavar="SOURCE_ID")
    enqueue.add_argument("--cycle", default=None, help="cycle key (default: this hour)")
    enqueue.add_argument("--limit", type=int, default=None)
    enqueue.add_argument("--dry-run", action="store_true")

    worker = sub.add_parser("worker", help="fetch queued pages (CONTACTS REAL SITES)")
    worker.add_argument("--cycle", default=None)
    worker.add_argument("--max-pages", type=int, default=5)
    worker.add_argument("--concurrency", type=int, default=2)
    worker.add_argument("--evidence-root", type=Path, default=Path(".evidence"))
    worker.add_argument(
        "--i-understand",
        action="store_true",
        help="required for more than 20 pages: this contacts real university websites",
    )

    report = sub.add_parser("report", help="acquisition state")
    report.add_argument("--by-institution", action="store_true")

    assist = sub.add_parser("assist", help="what acquisition learned, for a source reviewer")
    assist.add_argument("--university", type=uuid.UUID, default=None)
    assist.add_argument(
        "--moved",
        action="store_true",
        help="only pages whose final host differs from the one requested",
    )
    assist.add_argument("--limit", type=int, default=30)

    plan = sub.add_parser(
        "plan", help="full-fleet dry run: what a cycle would do, sending no requests"
    )
    plan.add_argument("--cycle", default=None, help="the cycle key you intend to use")
    plan.add_argument("--hosts", action="store_true", help="also list the busiest hosts")

    status = sub.add_parser("source-status", help="one source's operational state")
    status.add_argument("--source", type=uuid.UUID, required=True, metavar="SOURCE_ID")

    # Each of these is a person overriding what the system concluded, so each needs an
    # actor and a reason and each appends to the audit chain. `--reason` is not a
    # formality: the audit row is what a later reviewer reads.
    for name, helptext in (
        ("source-reenable", "allow technical fetch attempts again (NOT a trust decision)"),
        ("source-disable", "stop fetching this page until a person says otherwise"),
        ("source-review", "park this page for a human decision"),
        ("clear-cooldown", "cut short a cooldown whose cause is known to be resolved"),
    ):
        command = sub.add_parser(name, help=helptext)
        command.add_argument("--source", type=uuid.UUID, required=True, metavar="SOURCE_ID")
        command.add_argument("--actor", type=uuid.UUID, required=True, metavar="USER_ID")
        command.add_argument("--reason", required=True)
        if name == "clear-cooldown":
            command.add_argument(
                "--include-host",
                action="store_true",
                help="also un-quiet the whole host, not just this page",
            )

    return parser


#: Which identity each command needs. Not a convenience -- the privilege separation is
#: the point: `app_api` may read the fetch plane and never write it, and a scheduler
#: or worker running as the API role would be a hole in that, not a shortcut.
DEFAULT_ROLES: dict[str, DatabaseRole] = {
    "register": DatabaseRole.API,  # writes `source`, which is onboarding-side work
    "enqueue": DatabaseRole.WORKER,  # writes `fetch_attempt`
    "worker": DatabaseRole.WORKER,  # writes runs, snapshots and blobs
    "report": DatabaseRole.API,  # read-only
    "assist": DatabaseRole.API,  # read-only
    "plan": DatabaseRole.API,  # read-only, and sends no HTTP request
    "source-status": DatabaseRole.API,  # read-only
    # The recovery commands write `source` and `audit_log`. They run as the API role
    # rather than the worker: restoring a page is an operator action taken through the
    # application, not something the fetch plane does to itself.
    "source-reenable": DatabaseRole.API,
    "source-disable": DatabaseRole.API,
    "source-review": DatabaseRole.API,
    "clear-cooldown": DatabaseRole.API,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    role = DatabaseRole(args.role) if args.role else DEFAULT_ROLES[args.command]
    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(role), future=True)
    try:
        return _dispatch(args, engine)
    finally:
        engine.dispose()


RECOVERY_COMMANDS = {
    "source-reenable": "reenable",
    "source-disable": "disable",
    "source-review": "mark_needs_review",
    "clear-cooldown": "clear_cooldown",
}


def _print_state(state: recovery.SourceState) -> None:
    """One source's operational state, in the terms an operator asks in."""
    print(f"  {state.url}")
    print(f"    source id            {state.source_id}")
    print(f"    schedule state       {state.schedule_state}")
    print(f"    health               {state.health}")
    print(f"    fetch eligibility    {state.fetch_eligibility}")
    if state.fetch_eligibility_reason:
        print(f"      reason             {state.fetch_eligibility_reason}")
    print(f"    access state         {state.access_state}")
    print(f"    cooldown until       {state.cooldown_until or 'none'}")
    if state.cooldown_reason:
        print(f"      reason             {state.cooldown_reason}")
    print(f"    rate-limit strikes   {state.rate_limit_strikes}")
    print(f"    host                 {state.host}")
    print(f"    host cooldown until  {state.host_cooldown_until or 'none'}")
    print(f"    runs so far          {state.total_runs}")
    print(f"    last run             {state.last_status or 'never'} {state.last_http_status or ''}")
    if state.last_error_class:
        print(f"      error              {state.last_error_class}")
    # Printed every time, deliberately. "Re-enabled" invites the reading "trusted",
    # and this is the line that refuses it.
    print(f"    publication          {state.publication_eligibility}  (unchanged by recovery)")


def _plan(engine: Engine, *, cycle_key: str, show_hosts: bool) -> int:
    """What a full cycle would do. **Sends no HTTP request** and writes nothing.

    The five scheduler states are printed separately and never summed, because they
    need different responses: `COOLDOWN` waits on the clock, `NEEDS_MANUAL_REVIEW` and
    `DISABLED` wait on a person, and `BLOCKED` waits on the URL being corrected.
    Collapsing them into "not fetchable" is how an operator ends up waiting for a
    cooldown that was actually a refusal.
    """
    from app.domains.acquisition.runner import (
        DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        RATE_LIMIT_STRIKE_LIMIT,
    )

    with engine.connect() as connection:
        population = connection.execute(
            text(
                "SELECT count(DISTINCT t.source_id) AS pages, "
                "       count(DISTINCT t.target_institution_id) AS institutions, "
                "       count(DISTINCT t.host) AS hosts "
                "  FROM acquisition_target t WHERE t.pilot_wave IS NOT NULL"
            )
        ).one()
        claims = connection.execute(
            text(
                "SELECT count(*) FROM pilot_collected_source c "
                "  JOIN acquisition_target t ON t.source_id = c.acquisition_source_id "
                " WHERE t.pilot_wave IS NOT NULL"
            )
        ).scalar_one()
        states = connection.execute(
            text(
                "SELECT h.schedule_state AS state, count(DISTINCT h.source_id) AS n "
                "  FROM source_health h "
                "  JOIN acquisition_target t ON t.source_id = h.source_id "
                " WHERE t.pilot_wave IS NOT NULL "
                " GROUP BY 1 ORDER BY 1"
            )
        ).all()
        per_host = connection.execute(
            text(
                "SELECT t.host, count(DISTINCT t.source_id) AS pages "
                "  FROM acquisition_target t WHERE t.pilot_wave IS NOT NULL "
                " GROUP BY 1 ORDER BY 2 DESC, 1"
            )
        ).all()
        already = connection.execute(
            text("SELECT count(*) FROM fetch_attempt WHERE cycle_key = :c"),
            {"c": cycle_key},
        ).scalar_one()

    print(f"FULL-FLEET DRY RUN for cycle {cycle_key!r} -- no HTTP request is sent\n")
    print("=== population ===")
    print(f"  institutions                 {population.institutions}")
    print(f"  responsibility claims        {claims}")
    print(f"  physical pages               {population.pages}")
    print(f"  hosts                        {population.hosts}")
    print(
        f"  claims that need no second fetch  {claims - population.pages}"
        "   (one page answers for several)"
    )

    print("\n=== scheduler state, per physical page (never summed) ===")
    by_state = {row.state: row.n for row in states}
    for state in (
        "FETCHABLE_NOW",
        "COOLDOWN",
        "BLOCKED",
        "DISABLED",
        "NEEDS_MANUAL_REVIEW",
    ):
        print(f"  {state:22} {by_state.get(state, 0)}")
    unexpected = set(by_state) - {
        "FETCHABLE_NOW",
        "COOLDOWN",
        "BLOCKED",
        "DISABLED",
        "NEEDS_MANUAL_REVIEW",
    }
    for state in sorted(unexpected):
        print(f"  {state:22} {by_state[state]}  <-- unexpected state")

    counts = [row.pages for row in per_host]
    if counts:
        ordered = sorted(counts)
        middle = len(ordered) // 2
        median = (
            ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        )
        print("\n=== host workload ===")
        print(f"  hosts                        {len(counts)}")
        print(f"  busiest host                 {per_host[0].host} ({per_host[0].pages} pages)")
        print(f"  median pages per host        {median}")
        print(f"  hosts serving one page       {sum(1 for n in counts if n == 1)}")
        if show_hosts:
            print("  busiest:")
            for row in per_host[:10]:
                print(f"    {row.pages:3}  {row.host}")

    print("\n=== politeness as configured ===")
    print("  global concurrency           2 requested, and effectively 1:")
    print("                               run_cycle awaits each page before claiming")
    print("                               the next, so the semaphore never binds.")
    print("  per-host concurrency         1 (HostGate holds a lock per host)")
    print("  minimum host delay           5.0s between requests to one host")
    print(
        f"  429 fallback cooldown        {DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS:.0f}s "
        "when Retry-After is absent"
    )
    print(f"  429 strikes before review    {RATE_LIMIT_STRIKE_LIMIT}")
    print("  Retry-After                  obeyed as stated, for source and host")

    fetchable = by_state.get("FETCHABLE_NOW", 0)
    print("\n=== what the cycle would do ===")
    print(f"  attempts it would create     {fetchable}")
    print(f"  attempts already in {cycle_key!r}: {already}")
    estimate = fetchable * 5.0
    print(
        f"  wall clock floor             ~{estimate / 60:.0f} min "
        f"({fetchable} pages x 5s host delay, serial), plus fetch time"
    )
    print("\nNothing was written and no request was sent.")
    return 0


def _dispatch(args: argparse.Namespace, engine: Engine) -> int:
    if args.command in RECOVERY_COMMANDS:
        action = getattr(recovery, RECOVERY_COMMANDS[args.command])
        actor = recovery.Actor(user_id=args.actor)
        extra = {"include_host": args.include_host} if args.command == "clear-cooldown" else {}
        try:
            with engine.begin() as connection:
                before = recovery.source_state(connection, args.source)
                print("before:")
                _print_state(before)
                after = action(
                    connection,
                    source_id=args.source,
                    actor=actor,
                    reason=args.reason,
                    **extra,
                )
                print("\nafter:")
                _print_state(after)
        except recovery.RecoveryRefusedError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(
            "\nRecorded in the audit chain. This changes only whether a worker may "
            "request the URL;\nit confers no publication trust, and the next fetch "
            "still resolves DNS, validates every\naddress and revalidates every "
            "redirect exactly as before."
        )
        return 0

    if args.command == "source-status":
        with engine.connect() as connection:
            try:
                _print_state(recovery.source_state(connection, args.source))
            except recovery.RecoveryRefusedError as exc:
                print(f"{exc}", file=sys.stderr)
                return 2
        return 0

    if args.command == "plan":
        return _plan(engine, cycle_key=args.cycle or cycle_key_for(), show_hosts=args.hosts)

    if args.command == "register":
        try:
            with engine.begin() as connection:
                registered = register_acquisition_targets(
                    connection,
                    pilot_only=not args.all_institutions,
                    registered_by=args.by,
                )
                assert_nothing_became_publishable(connection)
                print(registered.summary())
                print(f"\n  physical pages      {registered.physical_pages}")
                print(f"  sources created     {registered.sources_created}")
                print(f"  already present     {registered.sources_existing}")
                print(f"  claims linked       {registered.claims_linked}")
                print(f"  hosts               {registered.hosts}")
                print(f"  FETCHABLE           {registered.fetchable}")
                print(f"  needs manual review {registered.needs_manual_review}")
                print(f"  unclassified pages  {registered.unclassified}")
                print(f"  DOCUMENT strategy   {registered.document_strategy}")
                for line in registered.refused[:20]:
                    print(f"    refused: {line}")
                if args.dry_run:
                    raise _RollbackError
        except _RollbackError:
            print("\n--dry-run: rolled back. Nothing was written.")
        else:
            print(
                "\nAcquisition targets only. Every source is NOT_ELIGIBLE for "
                "publication and nothing has been fetched."
            )
        return 0

    if args.command == "enqueue":
        cycle = args.cycle or cycle_key_for()
        try:
            with engine.begin() as connection:
                queued = enqueue_cycle(
                    connection,
                    cycle_key=cycle,
                    source_ids=[args.source] if args.source else None,
                    target_institution_id=args.university,
                    pilot_only=args.pilot or not (args.source or args.university),
                    limit=args.limit,
                )
                print(queued.summary())
                print(f"\n  physical pages selected      {queued.physical_pages_selected}")
                print(f"  hosts                        {queued.hosts}")
                print(
                    f"  duplicate responsibilities   {queued.duplicate_responsibilities_skipped}"
                    "  <- no second fetch needed"
                )
                print(f"  already queued               {queued.already_queued}")
                print(f"  blocked / disabled           {queued.blocked_or_disabled}")
                print(f"  new attempts                 {queued.attempts_created}")
                if args.dry_run:
                    raise _RollbackError
        except _RollbackError:
            print("\n--dry-run: rolled back. Nothing was queued.")
        return 0

    if args.command == "worker":
        cycle = args.cycle or cycle_key_for()
        if (args.max_pages is None or args.max_pages > 20) and not args.i_understand:
            print(
                "refusing: more than 20 pages contacts a lot of real university "
                "websites. Re-run with --i-understand if that is intended.",
                file=sys.stderr,
            )
            return 2
        store = build_evidence_store(get_settings(), local_root=args.evidence_root)
        worker_name = f"cli:{uuid.uuid4().hex[:8]}"
        result = asyncio.run(
            run_cycle(
                engine,
                cycle_key=cycle,
                store=store,
                worker=worker_name,
                max_pages=args.max_pages,
                global_concurrency=args.concurrency,
            )
        )
        print(result.summary())
        if result.urls_contacted:
            print(f"\n  URLs contacted ({len(result.urls_contacted)}):")
            for url in result.urls_contacted:
                print(f"    {url}")
        print(
            "\nEvidence captured. No field_claim, extraction or change proposal was "
            "created, and nothing became publication eligible."
        )
        return 0

    if args.command == "report":
        with engine.connect() as connection:
            summary = acquisition_summary(connection)
            print(summary.summary())
            print(f"\n  physical pages           {summary.physical_pages}")
            print(f"  responsibility claims    {summary.responsibility_claims}")
            print(f"  institutions / hosts     {summary.institutions} / {summary.hosts}")
            print(f"  FETCHABLE                {summary.fetchable}")
            print(f"  BLOCKED                  {summary.blocked}")
            print(f"  DISABLED                 {summary.disabled}")
            print(f"  needs manual review      {summary.needs_manual_review}")
            print(f"  fetch runs               {summary.total_runs}")
            print(f"  snapshots / blobs        {summary.snapshots} / {summary.blobs}")
            print(f"  304 UNCHANGED runs       {summary.unchanged_runs}")
            print(f"  page(s) now on another host {summary.pages_redirecting_off_host}")
            print(f"  publication eligible     {summary.publication_eligible}")
            if summary.by_health:
                print("\n  health:")
                for health, count in sorted(summary.by_health.items()):
                    print(f"    {health:16} {count}")
            if args.by_institution:
                print("\n  by institution:")
                for name, counts in sorted(health_by_institution(connection).items()):
                    rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                    print(f"    {name[:44]:44} {rendered}")
        return 0

    if args.command == "assist":
        with engine.connect() as connection:
            rows = verification_assistance(
                connection,
                target_institution_id=args.university,
                only_host_differs=args.moved,
                limit=args.limit,
            )
            if not rows:
                print("Nothing fetched yet in this scope. Run `worker` first.")
                return 0
            print(f"{len(rows)} page(s). This is assistance for a reviewer, not a decision:")
            print("nothing here verifies, classifies or promotes anything.\n")
            for row in rows:
                moved = " -> " + (row.effective_host or "") if row.host_differs else ""
                print(f"  {row.physical_source_ref}  {row.match_key[:36]:36} {row.health}")
                print(f"      {row.url}")
                print(
                    f"      status {row.http_status}  {row.content_type or '-'}  "
                    f"host {row.requested_host}{moved}"
                )
                if row.page_title:
                    print(f"      title: {row.page_title[:96]}")
                if row.redirect_chain:
                    print(f"      redirects: {len(row.redirect_chain)} hop(s)")
                print(
                    f"      claimed for: {', '.join(row.claimed_categories)}  "
                    f"[publication: {row.publication_eligibility}]"
                )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
