"""Controlled real-web acquisition smoke test (Step 5B.1).

Usage::

    uv run python apps/api/scripts/acquisition_smoke.py preflight
    uv run python apps/api/scripts/acquisition_smoke.py run --i-understand
    uv run python apps/api/scripts/acquisition_smoke.py run --i-understand --cycle smoke-2
    uv run python apps/api/scripts/acquisition_smoke.py evidence

**`run` contacts twelve real university websites.** It refuses to start without
`--i-understand`, prints every URL before and after, keeps one request in flight per
host with a several-second floor, and obeys `Retry-After`. There is no flag that makes
it faster, and none that relaxes TLS verification or the SSRF guard — if a page cannot
be fetched safely it is recorded as a failure, because discovering that is the point.

`preflight` performs **DNS only**: it resolves each host, classifies every answer, and
reports what would be requested. No HTTP request is sent.

The set itself lives in `smoke/acquisition_smoke_set.toml`, checked in so the run is
reproducible. It is not business configuration and nothing else reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.evidence import latest_effective_evidence
from app.domains.acquisition.fetcher import USER_AGENT, FetchPolicy, StaticHttpFetcher
from app.domains.acquisition.lease import enqueue_cycle
from app.domains.acquisition.netsafety import (
    UnsafeTargetError,
    classify_address,
    resolve_and_validate,
)
from app.domains.acquisition.recorder import conditional_headers_for
from app.domains.acquisition.runner import HostGate, run_cycle
from app.domains.acquisition.storage import (
    build_evidence_store,
    find_missing_objects,
)

SMOKE_SET = Path(__file__).resolve().parents[1] / "smoke" / "acquisition_smoke_set.toml"

#: Deliberately slow. Twelve pages is not a throughput problem, and the cost of being
#: impolite to a university on first contact is being blocked for the whole pilot.
SMOKE_POLICY = FetchPolicy(
    connect_timeout_seconds=15.0,
    read_timeout_seconds=25.0,
    total_timeout_seconds=90.0,  # a four-hop chain needs room for four connects
    max_redirects=5,
    max_bytes=32 * 1024 * 1024,
    verify_tls=True,  # never relaxed; a TLS failure is a finding, not an obstacle
    pin_addresses=True,
)
SMOKE_HOST_INTERVAL_SECONDS = 5.0
SMOKE_GLOBAL_CONCURRENCY = 2


@dataclass(frozen=True, slots=True)
class SmokePage:
    ref: str
    institution: str
    destination: str
    category: str
    url: str
    why: str


def load_smoke_set(path: Path = SMOKE_SET) -> list[SmokePage]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pages = [SmokePage(**page) for page in data["page"]]
    expected = data.get("expected_pages")
    if expected is not None and len(pages) != expected:
        raise SystemExit(f"{path.name} declares {expected} pages and contains {len(pages)}")
    urls = [page.url for page in pages]
    if len(set(urls)) != len(urls):
        raise SystemExit("the smoke set repeats a URL; one physical page is fetched once")
    return pages


def resolve_sources(engine: Engine, pages: list[SmokePage]) -> list[dict[str, Any]]:
    """Match the checked-in set against the imported acquisition targets, by URL."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT t.source_id, t.url, t.normalized_url, t.workbook_url, t.host,
                       t.match_key, t.destination_code, t.fetch_strategy,
                       t.fetch_eligibility, t.publication_eligibility, t.categories,
                       t.claim_count, t.physical_source_ref, t.source_type,
                       t.min_interval_seconds
                  FROM acquisition_target t
                 WHERE t.url = ANY(:urls)
                """
            ),
            {"urls": [page.url for page in pages]},
        ).all()
    by_url = {row.url: row for row in rows}
    missing = [page.url for page in pages if page.url not in by_url]
    if missing:
        raise SystemExit(
            "the smoke set names URLs that are not registered acquisition targets; "
            "re-run the import and `acquisition register` first:\n  " + "\n  ".join(missing)
        )
    return [{"page": page, "row": by_url[page.url]} for page in pages]


async def preflight(engine: Engine, pages: list[SmokePage]) -> int:
    """Resolve and classify every target. DNS only; no HTTP request is sent."""
    entries = resolve_sources(engine, pages)
    print(f"Smoke set: {len(entries)} physical page(s) from {SMOKE_SET.name}")
    print(f"User-Agent: {USER_AGENT}")
    print(
        f"Policy: connect {SMOKE_POLICY.connect_timeout_seconds}s, read "
        f"{SMOKE_POLICY.read_timeout_seconds}s, total {SMOKE_POLICY.total_timeout_seconds}s, "
        f"max {SMOKE_POLICY.max_bytes // (1024 * 1024)}MB, TLS verify "
        f"{SMOKE_POLICY.verify_tls}, pin {SMOKE_POLICY.pin_addresses}"
    )
    print(
        f"Politeness: global concurrency {SMOKE_GLOBAL_CONCURRENCY}, per-host 1, "
        f"{SMOKE_HOST_INTERVAL_SECONDS}s floor between requests to a host"
    )
    print("Evidence store: FILESYSTEM (S3/MinIO is unverified; Docker is unavailable)\n")

    failures = 0
    with engine.connect() as connection:
        for index, entry in enumerate(entries, start=1):
            page, row = entry["page"], entry["row"]
            label = f"{page.institution}  [{row.destination_code}]"
            print(f"{index:2}. {label}  {row.physical_source_ref}")
            print(f"    raw        {row.workbook_url}")
            print(f"    normalized {row.normalized_url}")
            print(f"    claims     {', '.join(row.categories)}  (claim_count={row.claim_count})")
            print(f"    host       {row.host}")
            print(f"    strategy   {row.fetch_strategy}   fetch: {row.fetch_eligibility}")

            try:
                target = await resolve_and_validate(row.url, timeout_seconds=8.0)
                classified = {address: classify_address(address) for address in target.addresses}
                unsafe = {a: r for a, r in classified.items() if r is not None}
                print(f"    DNS        {', '.join(target.addresses)}")
                verdict = "ALL ANSWERS PASSED" if not unsafe else f"REFUSED: {unsafe}"
                print(f"    SSRF       {verdict}   pinned -> {target.primary}")
            except UnsafeTargetError as exc:
                failures += 1
                print(f"    SSRF       REFUSED: {exc}")

            conditional = conditional_headers_for(connection, row.source_id)
            have = conditional.as_headers()
            print(f"    condition  {have or 'none stored yet'}")
            print(f"    publication {row.publication_eligibility}")
            if row.publication_eligibility != "NOT_ELIGIBLE":
                failures += 1
                print("    *** publication eligibility is not NOT_ELIGIBLE ***")
            print()

    print(
        f"Pre-flight complete. {len(entries) - failures} page(s) would be requested; "
        f"{failures} refused."
    )
    print("No HTTP request was sent. Publication eligibility unchanged.")
    return 0


def run(engine: Engine, pages: list[SmokePage], *, cycle: str, evidence_root: Path) -> int:
    entries = resolve_sources(engine, pages)
    source_ids = [entry["row"].source_id for entry in entries]

    with engine.begin() as connection:
        report = enqueue_cycle(connection, cycle_key=cycle, source_ids=source_ids)
    print(report.summary())
    print()

    store = build_evidence_store(get_settings(), local_root=evidence_root)
    fetcher = StaticHttpFetcher(SMOKE_POLICY)
    gate = HostGate(min_interval_seconds=SMOKE_HOST_INTERVAL_SECONDS)
    result = asyncio.run(
        run_cycle(
            engine,
            cycle_key=cycle,
            store=store,
            worker=f"smoke:{uuid.uuid4().hex[:8]}",
            fetcher=fetcher,
            gate=gate,
            global_concurrency=SMOKE_GLOBAL_CONCURRENCY,
        )
    )
    print(result.summary())
    print(f"\nURLs contacted ({len(result.urls_contacted)}):")
    for url in result.urls_contacted:
        print(f"  {url}")
    return 0


def evidence(engine: Engine, pages: list[SmokePage]) -> int:
    """What the run produced, per page, including 304 resolution."""
    entries = resolve_sources(engine, pages)
    with engine.connect() as connection:
        for entry in entries:
            page, row = entry["page"], entry["row"]
            runs = connection.execute(
                text(
                    "SELECT attempt_no, status::text AS status, http_status, error_class, "
                    "       bytes_downloaded, conditional_request_sent, unchanged_content_hash "
                    "  FROM fetch_run WHERE source_id = :s ORDER BY started_at, attempt_no"
                ),
                {"s": row.source_id},
            ).all()
            current = latest_effective_evidence(connection, row.source_id)
            print(f"{page.institution}  {row.physical_source_ref}  {row.url}")
            for entry_run in runs:
                print(
                    f"    run #{entry_run.attempt_no} {entry_run.status:9} "
                    f"http={entry_run.http_status} bytes={entry_run.bytes_downloaded} "
                    f"conditional={entry_run.conditional_request_sent} "
                    f"{entry_run.error_class or ''}"
                )
            if current.has_evidence:
                print(
                    f"    effective: snapshot {str(current.snapshot_id)[:8]} "
                    f"{current.content_type} observed {current.observed_at} "
                    f"confirmed {current.confirmed_at} "
                    f"(via 304: {current.resolved_through_unchanged}, "
                    f"streak {current.unchanged_runs_since})"
                )
            else:
                print("    effective: no body-bearing evidence")
            print()
    return 0


def audit(engine: Engine, pages: list[SmokePage], *, evidence_root: Path) -> int:
    """Every count the Step 5B.1 report should have been quoting, straight from SQL.

    WHY THIS IS A COMMAND AND NOT A PARAGRAPH
    =========================================
    The first Step 5B.1 report got two numbers wrong -- the health fleet counts and the
    content-type totals -- because it was written from scrollback after the development
    database had been purged, and scrollback held an *intermediate* cycle's output. Both
    errors were of the same kind: a grain confusion. A page that returned a challenge was
    counted once as a successful HTML page and again as a discarded challenge; a source
    blocked after the run was reported with the health it had before.

    So the grains are printed separately and never summed:

      * **sources** -- physical pages, the thing the client's workbook names;
      * **snapshots** -- occasions on which a body was seen (a re-fetch adds one);
      * **blobs** -- distinct bodies (a re-fetch of unchanged bytes adds none);
      * **observed and discarded** -- responses that reached us and were deliberately
        not stored, which have a status and a byte count but no snapshot and no blob.

    Anything that wants one total has to say which grain it means.
    """
    entries = resolve_sources(engine, pages)
    source_ids = [entry["row"].source_id for entry in entries]
    print(f"AUDIT of {len(entries)} smoke page(s) -- every count is per named grain\n")

    with engine.connect() as connection:
        print("=== SOURCE HEALTH, per physical page ===")
        rows = connection.execute(
            text(
                "SELECT t.physical_source_ref AS ref, t.match_key, h.source_id, h.health, "
                "       h.last_status, h.last_http_status, h.consecutive_failures, "
                "       h.total_runs, h.last_error_class, h.last_content_type "
                "  FROM source_health h "
                "  JOIN acquisition_target t ON t.source_id = h.source_id "
                " WHERE h.source_id = ANY(:ids) "
                " ORDER BY h.health, t.physical_source_ref"
            ),
            {"ids": source_ids},
        ).all()
        for row in rows:
            print(
                f"  {row.health:13} {row.ref}  {row.source_id}  "
                f"{row.match_key[:34]:34} last={row.last_status}/{row.last_http_status} "
                f"runs={row.total_runs} fails={row.consecutive_failures}"
            )
            if row.last_error_class:
                print(f"                {row.last_error_class[:110]}")
        # Tallied over distinct sources, not over `rows`: the join to
        # `acquisition_target` is per page **per institution**, so a page two
        # institutions cite appears twice and would be counted twice. And the
        # remainder is queried rather than subtracted from a literal 319 -- a
        # hardcoded fleet size is a number that goes stale silently, which is the
        # whole failure this command exists to prevent.
        fleet: dict[str, int] = {}
        for source_id in {row.source_id for row in rows}:
            health = next(row.health for row in rows if row.source_id == source_id)
            fleet[health] = fleet.get(health, 0) + 1
        print(f"  fleet over the smoke set: {fleet}")
        registered = connection.execute(
            text("SELECT count(DISTINCT source_id) FROM acquisition_target")
        ).scalar_one()
        print(f"  (the other {registered - len(fleet)} registered pages are NEVER_FETCHED)\n")

        print("=== GRAIN 1: physical sources holding body-bearing evidence, by content type ===")
        for row in connection.execute(
            text(
                "SELECT h.last_content_type AS ct, count(*) AS n FROM source_health h "
                " WHERE h.source_id = ANY(:ids) AND h.last_content_hash IS NOT NULL "
                " GROUP BY 1 ORDER BY 1"
            ),
            {"ids": source_ids},
        ):
            print(f"  {row.n:4}  {row.ct}")

        print("\n=== GRAIN 2: snapshots (occasions a body was seen), by content type ===")
        for row in connection.execute(
            text(
                "SELECT s.content_type AS ct, count(*) AS n FROM snapshot s "
                " WHERE s.source_id = ANY(:ids) GROUP BY 1 ORDER BY 1"
            ),
            {"ids": source_ids},
        ):
            print(f"  {row.n:4}  {row.ct}")

        print("\n=== GRAIN 3: distinct content blobs (distinct bodies), by content type ===")
        print("  note: a blob carries the bare media type; a snapshot keeps the header")
        print("  verbatim, charset included. The two grains do NOT join on content_type.")
        # Scoped through `snapshot`, because `content_blob` has no source column: it is
        # the global dedup boundary, and two sources serving identical bytes share one
        # row. An unscoped count here would mix this set with every other fetch in the
        # database -- which is how the counts this command exists to fix went wrong.
        for row in connection.execute(
            text(
                "SELECT b.content_type AS ct, count(*) AS n, sum(b.byte_size) AS bytes "
                "  FROM content_blob b "
                " WHERE EXISTS (SELECT 1 FROM snapshot s "
                "                WHERE s.content_hash = b.content_hash "
                "                  AND s.source_id = ANY(:ids)) "
                " GROUP BY 1 ORDER BY 1"
            ),
            {"ids": source_ids},
        ):
            print(f"  {row.n:4}  {row.ct:20} {row.bytes} bytes")

        print("\n=== GRAIN 4: responses observed on the wire and deliberately NOT stored ===")
        for row in connection.execute(
            text(
                "SELECT r.status::text AS status, r.http_status, r.error_class, "
                "       r.bytes_downloaded, count(*) AS n FROM fetch_run r "
                " WHERE r.source_id = ANY(:ids) "
                "   AND r.status IN ('BLOCKED', 'HTTP_ERROR', 'TIMEOUT') "
                " GROUP BY 1, 2, 3, 4 ORDER BY 2, 3"
            ),
            {"ids": source_ids},
        ):
            counted = row.bytes_downloaded
            wire = "no body" if counted is None else f"{counted} wire bytes"
            print(f"  {row.n:4}  {row.status:11} http={row.http_status}  {wire}")
            print(f"        {(row.error_class or '')[:110]}")

        print("\n=== RUN TOTALS ===")
        totals = connection.execute(
            text(
                "SELECT count(*) AS runs, "
                "  count(*) FILTER (WHERE status='OK') AS ok, "
                "  count(*) FILTER (WHERE status='UNCHANGED') AS unchanged, "
                "  count(*) FILTER (WHERE status='HTTP_ERROR') AS http_error, "
                "  count(*) FILTER (WHERE status='BLOCKED') AS blocked, "
                "  count(*) FILTER (WHERE status='TIMEOUT') AS timeout, "
                "  count(*) FILTER (WHERE redirect_chain IS NOT NULL) AS redirected, "
                "  count(*) FILTER (WHERE conditional_request_sent) AS conditional, "
                "  count(DISTINCT source_id) AS pages "
                "  FROM fetch_run WHERE source_id = ANY(:ids)"
            ),
            {"ids": source_ids},
        ).one()
        attempts = connection.execute(
            text("SELECT count(*) FROM fetch_attempt WHERE source_id = ANY(:ids)"),
            {"ids": source_ids},
        ).scalar_one()
        snaps = connection.execute(
            text("SELECT count(*) FROM snapshot WHERE source_id = ANY(:ids)"),
            {"ids": source_ids},
        ).scalar_one()
        blobs = connection.execute(
            text("SELECT count(DISTINCT content_hash) FROM snapshot WHERE source_id = ANY(:ids)"),
            {"ids": source_ids},
        ).scalar_one()
        fleet_blobs = connection.execute(text("SELECT count(*) FROM content_blob")).scalar_one()
        print(f"  pages touched {totals.pages} · attempts {attempts} · runs {totals.runs}")
        print(
            f"  OK {totals.ok} · UNCHANGED {totals.unchanged} · HTTP_ERROR "
            f"{totals.http_error} · BLOCKED {totals.blocked} · TIMEOUT {totals.timeout}"
        )
        print(
            f"  redirected {totals.redirected} · conditional requests sent " f"{totals.conditional}"
        )
        print(f"  snapshots {snaps} · distinct blobs {blobs}   (this set)")
        if fleet_blobs != blobs:
            print(f"  NOTE: the database holds {fleet_blobs} blob(s) in total, so some")
            print("        bodies above came from pages outside this smoke set.")

        print("\n=== OBJECT-STORE INTEGRITY (filesystem backend) ===")
        store = build_evidence_store(get_settings(), local_root=evidence_root)
        known = connection.execute(
            text(
                "SELECT b.content_hash, b.byte_size FROM content_blob b "
                " WHERE EXISTS (SELECT 1 FROM snapshot s "
                "                WHERE s.content_hash = b.content_hash "
                "                  AND s.source_id = ANY(:ids))"
            ),
            {"ids": source_ids},
        ).all()
        missing = find_missing_objects(store, {row.content_hash for row in known})
        mismatch = []
        for row in known:
            try:
                payload = store.get(row.content_hash)
            except (OSError, KeyError):
                continue
            if hashlib.sha256(payload).hexdigest() != row.content_hash:
                mismatch.append((row.content_hash, "hash"))
            elif len(payload) != row.byte_size:
                mismatch.append((row.content_hash, "size"))
        print(f"  blobs {len(known)} · missing objects {len(missing)} · mismatches {len(mismatch)}")
        for content_hash, kind in mismatch:
            print(f"    {kind} mismatch: {content_hash}")

        print("\n=== NOTHING WAS EXTRACTED, NOTHING WAS TRUSTED ===")
        for label, sql in (
            ("field_claim", "SELECT count(*) FROM field_claim"),
            ("extraction", "SELECT count(*) FROM extraction"),
            ("change_proposal", "SELECT count(*) FROM change_proposal"),
            ("field_provenance", "SELECT count(*) FROM field_provenance"),
            ("university", "SELECT count(*) FROM university"),
            ("program", "SELECT count(*) FROM program"),
            ("tuition", "SELECT count(*) FROM tuition"),
            (
                "source_mapping promoted",
                "SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL",
            ),
            (
                "official_domain VERIFIED_OFFICIAL",
                "SELECT count(*) FROM official_domain "
                "WHERE verification_status = 'VERIFIED_OFFICIAL'",
            ),
            (
                "source publication-eligible",
                "SELECT count(*) FROM source WHERE publication_eligibility <> 'NOT_ELIGIBLE'",
            ),
        ):
            print(f"  {label:36} {connection.execute(text(sql)).scalar_one()}")
        pending = connection.execute(
            text("SELECT count(*) FROM pilot_collected_source WHERE verification_state='PENDING'")
        ).scalar_one()
        print(f"  {'claims still PENDING':36} {pending}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print the smoke set and exit")
    sub.add_parser("preflight", help="resolve and classify; sends no HTTP request")
    runner = sub.add_parser("run", help="FETCH the smoke set from real websites")
    runner.add_argument("--i-understand", action="store_true", required=False)
    runner.add_argument("--cycle", default="smoke-1")
    runner.add_argument("--evidence-root", type=Path, default=Path(".evidence"))
    sub.add_parser("evidence", help="what the run produced, per page")
    auditor = sub.add_parser("audit", help="every count, per grain, straight from SQL")
    auditor.add_argument("--evidence-root", type=Path, default=Path(".evidence"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pages = load_smoke_set()

    if args.command == "list":
        for index, page in enumerate(pages, start=1):
            print(f"{index:2}. [{page.destination}] {page.institution}")
            print(f"    {page.category:26} {page.ref}")
            print(f"    {page.url}")
            print(f"    why: {page.why}")
        return 0

    role = DatabaseRole.WORKER if args.command == "run" else DatabaseRole.API
    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(role), future=True)
    try:
        if args.command == "preflight":
            return asyncio.run(preflight(engine, pages))
        if args.command == "evidence":
            return evidence(engine, pages)
        if args.command == "audit":
            return audit(engine, pages, evidence_root=args.evidence_root)
        if args.command == "run":
            if not args.i_understand:
                print(
                    f"refusing: this contacts {len(pages)} real university websites. "
                    "Re-run with --i-understand.",
                    file=sys.stderr,
                )
                return 2
            return run(engine, pages, cycle=args.cycle, evidence_root=args.evidence_root)
    finally:
        engine.dispose()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
