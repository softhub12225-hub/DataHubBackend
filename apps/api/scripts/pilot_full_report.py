"""Everything the Step 5B.3 report needs, queried from the persisted cycle (§28).

WHY THIS IS A COMMAND
=====================
C35: the Step 5B.1 write-up stated two counts the database contradicted, because it was
assembled from terminal scrollback after the development data had been purged. The
lesson was not "proof-read harder" -- it was that a number in a report should be a query
someone else can re-run.

So every figure below comes from SQL against the cycle that actually ran, each **grain**
is named, and nothing is summed across grains. A page that returned a challenge is
observed and not stored; a page re-fetched unchanged is an observation without a body.
Merge those and the totals stop meaning anything.

Usage::

    uv run python apps/api/scripts/pilot_full_report.py --cycle pilot-full-001
    uv run python apps/api/scripts/pilot_full_report.py --cycle pilot-full-001 --section hosts
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import FilesystemEvidenceStore, find_missing_objects

#: Ordered so a reader sees the population before the outcomes, and the outcomes before
#: the integrity checks that qualify them.
SECTIONS = (
    "population",
    "runtime",
    "status",
    "evidence",
    "integrity",
    "hosts",
    "redirects",
    "content",
    "conditional",
    "cooldown",
    "blocked",
    "dead",
    "dns",
    "internal",
    "health",
    "schedule",
    "followup",
    "trust",
)


def _rows(connection: Connection, sql: str, **params: Any) -> Sequence[Any]:
    return connection.execute(text(sql), params).all()


def _one(connection: Connection, sql: str, **params: Any) -> Any:
    return connection.execute(text(sql), params).one()


def _scalar(connection: Connection, sql: str, **params: Any) -> Any:
    return connection.execute(text(sql), params).scalar_one()


def _title(name: str) -> None:
    print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")


# `source` has no host column -- the host is derived from the URL. Defined once here so
# every section groups the same way, and matching `reporting._SQL_HOST_OF`.
HOST = "lower(split_part(regexp_replace(s.url, '^[^:]+://([^/?#]*).*$', '\\1'), ':', 1))"


def population(connection: Connection, cycle: str) -> None:
    _title("1. POPULATION (the fleet this cycle was drawn from)")
    row = _one(
        connection,
        "SELECT count(DISTINCT t.source_id) AS pages, "
        "       count(DISTINCT t.target_institution_id) AS institutions, "
        "       count(DISTINCT t.host) AS hosts "
        "  FROM acquisition_target t WHERE t.pilot_wave IS NOT NULL",
    )
    claims = _scalar(connection, "SELECT count(*) FROM pilot_collected_source")
    print(f"  institutions                      {row.institutions}")
    print(f"  responsibility claims             {claims}")
    print(f"  physical pages                    {row.pages}")
    print(f"  hosts                             {row.hosts}")
    print(f"  claims riding on another's fetch  {claims - row.pages}")


def runtime(connection: Connection, cycle: str) -> None:
    _title("2. RUNTIME AND POLITENESS")
    row = _one(
        connection,
        "SELECT min(r.started_at) AS first_start, max(r.finished_at) AS last_finish, "
        "       count(*) AS runs, "
        "       round(avg(r.duration_ms)) AS mean_ms, "
        "       max(r.duration_ms) AS slowest_ms, "
        "       round(sum(r.duration_ms) / 1000.0, 1) AS request_seconds "
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        " WHERE a.cycle_key = :c",
        c=cycle,
    )
    if row.runs == 0:
        print("  no runs recorded for this cycle")
        return
    wall = (row.last_finish - row.first_start).total_seconds()
    print(f"  first request started             {row.first_start}")
    print(f"  last request finished             {row.last_finish}")
    print(f"  elapsed (first start to last)     {wall:.0f}s ({wall / 60:.1f} min)")
    print(f"  requests sent                     {row.runs}")
    print(f"  mean request duration             {row.mean_ms} ms")
    print(f"  slowest request                   {row.slowest_ms} ms")
    print(f"  time spent in requests            {row.request_seconds}s")
    waiting = wall - float(row.request_seconds)
    print(f"  time spent waiting (host floor)   {waiting:.0f}s ({waiting / wall * 100:.0f}%)")
    slowest = _rows(
        connection,
        f"SELECT {HOST} AS host, r.duration_ms, r.status::text AS status, s.url "  # noqa: S608 - HOST is a module constant of literals, never input
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        "  JOIN source s ON s.id = r.source_id "
        " WHERE a.cycle_key = :c ORDER BY r.duration_ms DESC LIMIT 5",
        c=cycle,
    )
    print("  slowest five:")
    for item in slowest:
        print(f"    {item.duration_ms:>7} ms  {item.status:<18} {item.url[:70]}")


def status(connection: Connection, cycle: str) -> None:
    _title("3. PROCESSING TOTALS AND STATUS DISTRIBUTION")
    attempts = _one(
        connection,
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE state = 'FINALIZED') AS finalized, "
        "       count(*) FILTER (WHERE state = 'QUEUED') AS queued, "
        "       count(*) FILTER (WHERE state = 'RUNNING') AS running, "
        "       count(*) FILTER (WHERE state = 'ABANDONED') AS abandoned, "
        "       count(*) FILTER (WHERE attempt_no = 1) AS first_attempts, "
        "       count(*) FILTER (WHERE attempt_no > 1) AS retries "
        "  FROM fetch_attempt WHERE cycle_key = :c",
        c=cycle,
    )
    print(f"  fetch_attempt rows in cycle       {attempts.total}")
    print(f"    initial attempts (attempt_no=1) {attempts.first_attempts}")
    print(f"    retries queued by this cycle    {attempts.retries}")
    print(f"    FINALIZED                       {attempts.finalized}")
    print(f"    QUEUED (deferred)               {attempts.queued}")
    print(f"    RUNNING                         {attempts.running}")
    print(f"    ABANDONED                       {attempts.abandoned}")

    print("\n  terminal fetch_run by status (one per finalised attempt):")
    known = (
        "OK",
        "UNCHANGED",
        "RATE_LIMITED",
        "BLOCKED",
        "HTTP_ERROR",
        "TIMEOUT",
        "DNS_TEMPORARY",
        "NAME_NOT_RESOLVED",
        "INTERNAL_ERROR",
        "ABANDONED",
        "PARSE_FAILED",
    )
    counts = {
        row.status: row.n
        for row in _rows(
            connection,
            "SELECT r.status::text AS status, count(*) AS n FROM fetch_run r "
            "  JOIN fetch_attempt a ON a.id = r.attempt_id "
            " WHERE a.cycle_key = :c GROUP BY 1",
            c=cycle,
        )
    }
    for name in known:
        print(f"    {name:<20} {counts.get(name, 0)}")
    extra = set(counts) - set(known)
    for name in sorted(extra):
        print(f"    {name:<20} {counts[name]}   <-- unexpected status")
    print(f"    {'TOTAL':<20} {sum(counts.values())}")

    print("\n  HTTP status codes actually seen:")
    for row in _rows(
        connection,
        "SELECT r.http_status, count(*) AS n FROM fetch_run r "
        "  JOIN fetch_attempt a ON a.id = r.attempt_id "
        " WHERE a.cycle_key = :c AND r.http_status IS NOT NULL "
        " GROUP BY 1 ORDER BY 1",
        c=cycle,
    ):
        print(f"    {row.http_status:<20} {row.n}")


def evidence(connection: Connection, cycle: str) -> None:
    _title("4. EVIDENCE COUNTS (four grains, never summed)")
    sources_with = _scalar(
        connection,
        "SELECT count(DISTINCT s.source_id) FROM snapshot s",
    )
    total_sources = _scalar(connection, "SELECT count(*) FROM source")
    snapshots = _scalar(connection, "SELECT count(*) FROM snapshot")
    blobs = _scalar(connection, "SELECT count(DISTINCT content_hash) FROM snapshot")
    fleet_blobs = _scalar(connection, "SELECT count(*) FROM content_blob")
    stored_bytes = _scalar(connection, "SELECT coalesce(sum(byte_size), 0) FROM content_blob")
    print(f"  GRAIN sources with body-bearing evidence   {sources_with}")
    print(f"  GRAIN sources with no evidence at all      {total_sources - sources_with}")
    print(f"  GRAIN snapshots (occasions bytes were seen) {snapshots}")
    print(f"  GRAIN distinct bodies reachable from them   {blobs}")
    print(f"        content_blob rows in the database     {fleet_blobs}")
    print(f"        bytes stored                          {stored_bytes:,}")
    print("\n  A challenge response is observed and NOT stored; an unchanged re-fetch is")
    print("  an observation without a body. Those are why these four differ.")


def integrity(connection: Connection, cycle: str, evidence_root: Path) -> None:
    _title("5. OBJECT-STORE INTEGRITY (filesystem backend)")
    print(f"  store root                        {evidence_root}")
    store = FilesystemEvidenceStore(evidence_root)
    known = _rows(connection, "SELECT content_hash, byte_size, storage_key FROM content_blob")
    missing = find_missing_objects(store, {row.content_hash for row in known})
    hash_bad: list[str] = []
    size_bad: list[str] = []
    key_bad: list[str] = []
    for row in known:
        try:
            payload = store.get(row.content_hash)
        except (OSError, KeyError):
            continue
        if hashlib.sha256(payload).hexdigest() != row.content_hash:
            hash_bad.append(row.content_hash)
        if len(payload) != row.byte_size:
            size_bad.append(row.content_hash)
        if row.content_hash not in row.storage_key:
            key_bad.append(row.content_hash)

    on_disk = sum(1 for path in evidence_root.rglob("*") if path.is_file())
    print(f"  referenced blobs                  {len(known)}")
    print(f"  missing referenced objects        {len(missing)}")
    print(f"  hash mismatches                   {len(hash_bad)}")
    print(f"  size mismatches                   {len(size_bad)}")
    print(f"  storage keys not naming the hash  {len(key_bad)}")
    print(f"  files on disk under the root      {on_disk}")
    print(f"  orphans (on disk, unreferenced)   {on_disk - (len(known) - len(missing))}")
    for content_hash in (missing + hash_bad + size_bad + key_bad)[:10]:
        print(f"    PROBLEM {content_hash}")


def hosts(connection: Connection, cycle: str) -> None:
    _title("6. HOST REPORT")
    rows = _rows(
        connection,
        f"SELECT {HOST} AS host, "  # noqa: S608 - HOST is a module constant of literals, never input
        "       count(DISTINCT s.id) AS pages, "
        "       count(r.id) AS attempted, "
        "       count(*) FILTER (WHERE r.status = 'OK') AS ok, "
        "       count(*) FILTER (WHERE r.status = 'BLOCKED') AS blocked, "
        "       count(*) FILTER (WHERE r.status = 'RATE_LIMITED') AS rate_limited, "
        "       count(*) FILTER (WHERE r.http_status IN (404, 410)) AS gone, "
        "       count(*) FILTER (WHERE r.status IN "
        "             ('HTTP_ERROR','TIMEOUT','DNS_TEMPORARY','NAME_NOT_RESOLVED',"
        "              'INTERNAL_ERROR') AND coalesce(r.http_status, 0) NOT IN (404, 410)) "
        "             AS other_failures, "
        "       count(*) FILTER (WHERE r.error_class LIKE 'ChallengeInterstitial%') "
        "             AS challenges, "
        "       count(*) FILTER (WHERE r.redirect_chain IS NOT NULL) AS redirected "
        "  FROM source s "
        "  LEFT JOIN fetch_run r ON r.source_id = s.id "
        "  LEFT JOIN fetch_attempt a ON a.id = r.attempt_id AND a.cycle_key = :c "
        " GROUP BY 1 ORDER BY 1",
        c=cycle,
    )
    troubled = [
        row
        for row in rows
        if row.blocked or row.rate_limited or row.gone or row.other_failures or row.challenges
    ]
    print(f"  hosts in the fleet                {len(rows)}")
    print(f"  hosts with no failure at all      {len(rows) - len(troubled)}")
    print(f"  hosts with something to look at   {len(troubled)}")
    print(
        f"\n  {'host':<38} {'pg':>3} {'try':>3} {'ok':>3} {'blk':>3} "
        f"{'429':>3} {'404':>3} {'oth':>3} {'chal':>4} {'redir':>5}"
    )
    for row in troubled:
        print(
            f"  {row.host[:38]:<38} {row.pages:>3} {row.attempted:>3} {row.ok:>3} "
            f"{row.blocked:>3} {row.rate_limited:>3} {row.gone:>3} "
            f"{row.other_failures:>3} {row.challenges:>4} {row.redirected:>5}"
        )
    print("\n  busiest hosts by page count:")
    for row in sorted(rows, key=lambda r: (-r.pages, r.host))[:10]:
        print(f"    {row.pages:>3} pages  {row.ok:>3} ok  {row.host}")


def redirects(connection: Connection, cycle: str) -> None:
    _title("7. REDIRECTS (same-host separated from off-host)")
    rows = _rows(
        connection,
        f"SELECT {HOST} AS requested_host, "  # noqa: S608 - HOST is a module constant of literals, never input
        "       lower(split_part(regexp_replace(sn.effective_url, "
        "              '^[^:]+://([^/?#]*).*$', '\\1'), ':', 1)) AS effective_host, "
        "       jsonb_array_length(sn.redirect_chain) AS hops, s.url, sn.effective_url "
        "  FROM snapshot sn JOIN source s ON s.id = sn.source_id "
        " WHERE sn.redirect_chain IS NOT NULL "
        " ORDER BY 3 DESC, 1",
    )
    off_host = [
        row for row in rows if row.effective_host and row.effective_host != row.requested_host
    ]
    same_host = [row for row in rows if row not in off_host]
    print(f"  snapshots that followed a redirect   {len(rows)}")
    print(f"    same-host (canonicalisation etc.)  {len(same_host)}")
    print(f"    OFF-HOST                           {len(off_host)}")
    failed = _scalar(
        connection,
        "SELECT count(*) FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        " WHERE a.cycle_key = :c AND r.redirect_chain IS NOT NULL "
        "   AND r.status NOT IN ('OK', 'UNCHANGED')",
        c=cycle,
    )
    print(f"  failed fetches that had redirected   {failed}  (trail kept on the run, C33)")
    if off_host:
        print("\n  OFF-HOST -- recorded, publication-unverified, promoted by nothing:")
        for row in off_host:
            print(f"    {row.hops} hop(s)  {row.requested_host}  ->  {row.effective_host}")
            print(f"              {row.url[:72]}")
    print("\n  Every hop passed SSRF validation before connection, by construction:")
    print("  redirects are followed by hand and each Location is revalidated.")


def content(connection: Connection, cycle: str) -> None:
    _title("8. CONTENT TYPES (per grain)")
    print("  GRAIN 1 -- sources holding evidence, by their current media type:")
    for row in _rows(
        connection,
        "SELECT split_part(h.last_content_type, ';', 1) AS media, count(*) AS n "
        "  FROM source_health h WHERE h.last_content_hash IS NOT NULL "
        " GROUP BY 1 ORDER BY 2 DESC, 1",
    ):
        print(f"    {row.n:>4}  {row.media}")
    print("\n  GRAIN 2 -- snapshots, by the header the server sent (charset kept):")
    for row in _rows(
        connection,
        "SELECT content_type, count(*) AS n FROM snapshot GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15",
    ):
        print(f"    {row.n:>4}  {row.content_type}")
    print("\n  GRAIN 3 -- distinct bodies, by the blob's normalised media type:")
    for row in _rows(
        connection,
        "SELECT b.content_type, count(*) AS n, sum(b.byte_size) AS bytes "
        "  FROM content_blob b GROUP BY 1 ORDER BY 2 DESC, 1",
    ):
        print(f"    {row.n:>4}  {row.content_type:<28} {row.bytes:>12,} bytes")
    print("\n  A blob carries the bare media type and a snapshot keeps the header verbatim,")
    print("  so grains 2 and 3 do NOT join on content_type.")


def conditional(connection: Connection, cycle: str) -> None:
    _title("9. CONDITIONAL-REQUEST COVERAGE (what future polling can use)")
    row = _one(
        connection,
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE etag IS NOT NULL AND last_modified IS NOT NULL) AS both, "
        "       count(*) FILTER (WHERE etag IS NOT NULL AND last_modified IS NULL) AS etag_only, "
        "       count(*) FILTER (WHERE etag IS NULL AND last_modified IS NOT NULL) AS lm_only, "
        "       count(*) FILTER (WHERE etag IS NULL AND last_modified IS NULL) AS neither "
        "  FROM (SELECT DISTINCT ON (source_id) source_id, etag, last_modified "
        "          FROM snapshot ORDER BY source_id, observed_at DESC) latest",
    )
    print(f"  sources with evidence             {row.total}")
    print(f"    ETag and Last-Modified          {row.both}")
    print(f"    ETag only                       {row.etag_only}")
    print(f"    Last-Modified only              {row.lm_only}")
    print(f"    neither                         {row.neither}")
    usable = row.both + row.etag_only + row.lm_only
    if row.total:
        print(
            f"  conditional polling possible for  {usable}/{row.total} "
            f"({usable / row.total * 100:.0f}%)"
        )
    sent = _scalar(
        connection,
        "SELECT count(*) FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        " WHERE a.cycle_key = :c AND r.conditional_request_sent",
        c=cycle,
    )
    print(f"  conditional requests sent in cycle {sent}  (a first run has nothing to compare)")


def cooldown(connection: Connection, cycle: str) -> None:
    _title("10. RATE LIMITS AND COOLDOWNS")
    runs = _rows(
        connection,
        f"SELECT {HOST} AS host, s.url, r.error_class, s.cooldown_until, s.cooldown_reason, "  # noqa: S608 - HOST is a module constant of literals, never input
        "       s.rate_limit_strikes, s.fetch_eligibility::text AS fe "
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        "  JOIN source s ON s.id = r.source_id "
        " WHERE a.cycle_key = :c AND r.status = 'RATE_LIMITED' ORDER BY 1",
        c=cycle,
    )
    print(f"  RATE_LIMITED runs                 {len(runs)}")
    host_pauses = _rows(
        connection, "SELECT host, cooldown_until, reason FROM host_cooldown ORDER BY host"
    )
    print(f"  host cooldowns in force           {len(host_pauses)}")
    sources_cooling = _scalar(
        connection, "SELECT count(*) FROM source WHERE cooldown_until > now()"
    )
    print(f"  sources in cooldown now           {sources_cooling}")
    for row in runs:
        print(f"    {row.host}  strikes={row.rate_limit_strikes}  eligibility={row.fe}")
        print(f"      {row.error_class}")
        print(f"      cooldown until {row.cooldown_until}")
    for row in host_pauses:
        print(f"    HOST PAUSE {row.host} until {row.cooldown_until}: {row.reason[:60]}")
    if not runs:
        print("  No host returned 429 during this cycle.")


def blocked(connection: Connection, cycle: str) -> None:
    _title("11. BLOCKED AND CHALLENGE EVENTS")
    rows = _rows(
        connection,
        f"SELECT ti.match_key AS institution, t.physical_source_ref AS ref, "  # noqa: S608 - HOST is a module constant of literals, never input
        f"       {HOST} AS host, s.url, r.http_status, r.error_class, r.bytes_downloaded "
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        "  JOIN source s ON s.id = r.source_id "
        "  LEFT JOIN acquisition_target t ON t.source_id = s.id "
        "  LEFT JOIN target_institution ti ON ti.id = t.target_institution_id "
        " WHERE a.cycle_key = :c AND r.status = 'BLOCKED' "
        " ORDER BY r.error_class, 3",
        c=cycle,
    )
    challenges = [r for r in rows if (r.error_class or "").startswith("ChallengeInterstitial")]
    refusals = [r for r in rows if r not in challenges]
    print(f"  BLOCKED runs                      {len(rows)}")
    print(f"    challenge / interstitial        {len(challenges)}")
    print(f"    outright refusal (401/403/WAF)  {len(refusals)}")
    stored = _scalar(
        connection,
        "SELECT count(*) FROM snapshot sn JOIN fetch_run r ON r.id = sn.fetch_run_id "
        " WHERE r.status = 'BLOCKED'",
    )
    print(f"  snapshots from a BLOCKED run      {stored}   (must be 0)")
    if challenges:
        print("\n  CHALLENGES -- bytes counted and discarded, never stored as evidence:")
        for row in challenges:
            print(f"    {(row.institution or '?')[:34]:<34} {row.ref or '?':<7} {row.host}")
            print(f"      {row.bytes_downloaded} wire bytes; {(row.error_class or '')[:78]}")
    if refusals:
        print("\n  REFUSALS -- recorded and not worked around (D6):")
        for row in refusals:
            print(
                f"    {(row.institution or '?')[:34]:<34} {row.ref or '?':<7} "
                f"{row.http_status}  {row.host}"
            )


def dead(connection: Connection, cycle: str) -> None:
    _title("12. 404 / 410 -- DEAD URLS FOR SOURCE REVIEW")
    rows = _rows(
        connection,
        "SELECT ti.match_key AS institution, t.physical_source_ref AS ref, "
        "       t.categories, s.url, r.http_status "
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        "  JOIN source s ON s.id = r.source_id "
        "  LEFT JOIN acquisition_target t ON t.source_id = s.id "
        "  LEFT JOIN target_institution ti ON ti.id = t.target_institution_id "
        " WHERE a.cycle_key = :c AND r.http_status IN (404, 410) "
        " ORDER BY 1, 2",
        c=cycle,
    )
    print(f"  pages returning 404 or 410        {len(rows)}")
    retried = _scalar(
        connection,
        "SELECT count(*) FROM fetch_attempt a "
        " WHERE a.cycle_key = :c AND a.attempt_no > 1 AND a.source_id IN ("
        "   SELECT r.source_id FROM fetch_run r JOIN fetch_attempt a2 ON a2.id = r.attempt_id "
        "    WHERE a2.cycle_key = :c AND r.http_status IN (404, 410))",
        c=cycle,
    )
    print(f"  retries scheduled for them        {retried}   (must be 0)")
    print("\n  No URL was modified. These are worklist items for whoever owns the list:")
    for row in rows:
        print(f"    {row.http_status}  {(row.institution or '?')[:36]:<36} {row.ref or '?'}")
        print(f"         {', '.join(row.categories or [])}")
        print(f"         {row.url[:90]}")


def dns(connection: Connection, cycle: str) -> None:
    _title("13. DNS AND NETWORK FAILURES")
    # Mutually exclusive predicates. The previous "transport error" bucket matched
    # `Connect%`, which also catches `ConnectTimeout` -- so its count overlapped the
    # TIMEOUT bucket above it and the two were double-counted in prose (section 0,
    # defect B). A list of counts that overlap is a list that invites that mistake.
    for label, predicate in (
        ("DNS_TEMPORARY", "r.status = 'DNS_TEMPORARY'"),
        ("NAME_NOT_RESOLVED", "r.status = 'NAME_NOT_RESOLVED'"),
        ("TIMEOUT (connect or read)", "r.status = 'TIMEOUT'"),
        ("unsafe target (BLOCKED)", "r.error_class LIKE 'UnsafeTarget%'"),
        (
            "TLS certificate failure",
            "r.error_class LIKE '%CERTIFICATE_VERIFY_FAILED%' " "OR r.error_class LIKE 'SSLError%'",
        ),
        (
            "other transport error (not a timeout, not TLS)",
            "r.status <> 'TIMEOUT' "
            "AND r.error_class NOT LIKE '%CERTIFICATE_VERIFY_FAILED%' "
            "AND (r.error_class LIKE 'ConnectError%' OR r.error_class LIKE 'Transport%')",
        ),
    ):
        rows = _rows(
            connection,
            f"SELECT {HOST} AS host, s.url, r.error_class, r.status::text AS status "  # noqa: S608 - HOST is a module constant of literals, never input
            "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
            "  JOIN source s ON s.id = r.source_id "
            f" WHERE a.cycle_key = :c AND ({predicate}) ORDER BY 1",
            c=cycle,
        )
        print(f"  {label:<26} {len(rows)}")
        for row in rows[:12]:
            print(f"      {row.host:<40} {(row.error_class or '')[:52]}")
    print("\n  No resolution failure reached a connection: resolution happens before the")
    print("  socket is opened, and a raised error returns an outcome instead.")


def internal(connection: Connection, cycle: str) -> None:
    _title("14. INTERNAL ERRORS (our own defects)")
    rows = _rows(
        connection,
        f"SELECT {HOST} AS host, s.url, r.error_class, r.worker_name "  # noqa: S608 - HOST is a module constant of literals, never input
        "  FROM fetch_run r JOIN fetch_attempt a ON a.id = r.attempt_id "
        "  JOIN source s ON s.id = r.source_id "
        " WHERE a.cycle_key = :c AND r.status = 'INTERNAL_ERROR' ORDER BY 1",
        c=cycle,
    )
    print(f"  INTERNAL_ERROR runs               {len(rows)}")
    with_evidence = _scalar(
        connection,
        "SELECT count(*) FROM snapshot sn JOIN fetch_run r ON r.id = sn.fetch_run_id "
        " WHERE r.status = 'INTERNAL_ERROR'",
    )
    print(f"  snapshots from one                {with_evidence}   (must be 0)")
    stranded = _scalar(
        connection,
        "SELECT count(*) FROM fetch_attempt WHERE cycle_key = :c AND state = 'RUNNING'",
        c=cycle,
    )
    print(f"  attempts left RUNNING             {stranded}   (sweeper's work if non-zero)")
    for row in rows:
        print(f"    {row.error_class:<26} {row.host}")
        print(f"      {row.url[:88]}")


def health(connection: Connection, cycle: str) -> None:
    _title("15. SOURCE HEALTH (describes the record)")
    counts = {
        row.health: row.n
        for row in _rows(
            connection,
            "SELECT h.health, count(DISTINCT h.source_id) AS n FROM source_health h GROUP BY 1",
        )
    }
    for name in (
        "HEALTHY",
        "DEGRADED",
        "FAILING",
        "BLOCKED",
        "DISABLED",
        "NEEDS_MANUAL_REVIEW",
        "STALE",
        "NEVER_FETCHED",
    ):
        print(f"  {name:<24} {counts.get(name, 0)}")
    print(f"  {'TOTAL':<24} {sum(counts.values())}")


def schedule(connection: Connection, cycle: str) -> None:
    _title("16. SCHEDULER STATE (what a worker may do next -- a different grain)")
    counts = {
        row.schedule_state: row.n
        for row in _rows(
            connection,
            "SELECT h.schedule_state, count(DISTINCT h.source_id) AS n "
            "  FROM source_health h GROUP BY 1",
        )
    }
    for name in ("FETCHABLE_NOW", "COOLDOWN", "BLOCKED", "DISABLED", "NEEDS_MANUAL_REVIEW"):
        print(f"  {name:<24} {counts.get(name, 0)}")
    print(f"  {'TOTAL':<24} {sum(counts.values())}")
    print("\n  Health and schedulability are different questions: a page can be DEGRADED")
    print("  and FETCHABLE_NOW, or HEALTHY and in COOLDOWN.")


def followup(connection: Connection, cycle: str) -> None:
    _title("17. FOLLOW-UP QUEUE (unresolved pages, categorised)")
    rows = _rows(
        connection,
        f"SELECT s.id, s.url, {HOST} AS host, h.schedule_state, h.health, "  # noqa: S608 - HOST is a module constant of literals, never input
        "       h.last_status, h.last_http_status, h.last_error_class, "
        "       h.last_content_hash IS NOT NULL AS has_evidence "
        "  FROM source s JOIN source_health h ON h.source_id = s.id "
        " WHERE h.last_content_hash IS NULL",
    )
    # Named separately, and mutually exclusive by construction -- each page has one
    # `last_status`, so it lands in exactly one bucket. The previous version folded
    # TLS, NXDOMAIN and HTTP 202 into one `SOURCE_REVIEW_REQUIRED` count, which was
    # right but coarse enough that restating it in prose mixed a **run** count into a
    # **page** total (section 0, defect A).
    buckets: dict[str, list[Any]] = {
        "DEAD_URL": [],
        "BLOCKED_ACCESS": [],
        "RETRY_AFTER_COOLDOWN": [],
        "TLS_CHAIN_FAILURE": [],
        "NAME_NOT_RESOLVED": [],
        "UNEXPECTED_HTTP_STATUS": [],
        "TEMPORARY_NETWORK_FAILURE": [],
        "SOURCE_REVIEW_OTHER": [],
        "INTERNAL_ERROR": [],
        "NOT_YET_ATTEMPTED": [],
    }
    for row in rows:
        error = row.last_error_class or ""
        if row.last_status is None:
            buckets["NOT_YET_ATTEMPTED"].append(row)
        elif row.last_http_status in (404, 410):
            buckets["DEAD_URL"].append(row)
        elif row.last_status == "BLOCKED":
            buckets["BLOCKED_ACCESS"].append(row)
        elif row.schedule_state == "COOLDOWN" or row.last_status == "RATE_LIMITED":
            buckets["RETRY_AFTER_COOLDOWN"].append(row)
        elif "CERTIFICATE_VERIFY_FAILED" in error:
            buckets["TLS_CHAIN_FAILURE"].append(row)
        elif row.last_status == "NAME_NOT_RESOLVED":
            buckets["NAME_NOT_RESOLVED"].append(row)
        elif row.last_status == "TIMEOUT" or row.last_status == "DNS_TEMPORARY":
            buckets["TEMPORARY_NETWORK_FAILURE"].append(row)
        elif row.last_status == "INTERNAL_ERROR":
            buckets["INTERNAL_ERROR"].append(row)
        elif row.last_http_status is not None:
            buckets["UNEXPECTED_HTTP_STATUS"].append(row)
        else:
            buckets["SOURCE_REVIEW_OTHER"].append(row)

    print(f"  pages with no body-bearing evidence {len(rows)}")
    for name, items in buckets.items():
        print(f"    {name:<28} {len(items)}")
    assigned = sum(len(items) for items in buckets.values())
    print(f"    {'(sum, must equal the total)':<28} {assigned}")

    # "Needs a person to look at the URL" is a narrower question than "has no
    # evidence": an access refusal is a question for whoever owns the relationship,
    # not for whoever owns the list.
    review = (
        len(buckets["DEAD_URL"])
        + len(buckets["TLS_CHAIN_FAILURE"])
        + len(buckets["NAME_NOT_RESOLVED"])
        + len(buckets["UNEXPECTED_HTTP_STATUS"])
        + len(buckets["SOURCE_REVIEW_OTHER"])
    )
    print(f"\n  pages needing SOURCE review (URL is wrong or unreachable): {review}")
    print(
        f"  pages needing ACCESS review (the site refused us):          "
        f"{len(buckets['BLOCKED_ACCESS'])}"
    )
    print("\n  No URL was modified and no block was worked around.")


def trust(connection: Connection, cycle: str) -> None:
    _title("18. TRUST AND CANONICAL SAFETY")
    for label, sql in (
        (
            "publication-eligible sources",
            "SELECT count(*) FROM source " "WHERE publication_eligibility <> 'NOT_ELIGIBLE'",
        ),
        (
            "official_domain VERIFIED_OFFICIAL",
            "SELECT count(*) FROM official_domain "
            "WHERE verification_status = 'VERIFIED_OFFICIAL'",
        ),
        (
            "promoted source_mappings",
            "SELECT count(*) FROM source_mapping " "WHERE promoted_source_id IS NOT NULL",
        ),
        ("field_claim", "SELECT count(*) FROM field_claim"),
        ("extraction", "SELECT count(*) FROM extraction"),
        ("change_proposal", "SELECT count(*) FROM change_proposal"),
        ("field_provenance", "SELECT count(*) FROM field_provenance"),
        ("university", "SELECT count(*) FROM university"),
        ("program", "SELECT count(*) FROM program"),
        ("tuition", "SELECT count(*) FROM tuition"),
        ("application_deadline", "SELECT count(*) FROM application_deadline"),
        ("admission_requirement", "SELECT count(*) FROM admission_requirement"),
        ("language_requirement", "SELECT count(*) FROM language_requirement"),
        ("entity_version", "SELECT count(*) FROM entity_version"),
        ("change_event", "SELECT count(*) FROM change_event"),
    ):
        print(f"  {label:<36} {_scalar(connection, sql)}")
    pending = _scalar(
        connection,
        "SELECT count(*) FROM pilot_collected_source " " WHERE verification_state = :state",
        state="PENDING",
    )
    print(f"  {'claims still PENDING':<36} {pending}")
    print("\n  Fetching a page is technical evidence capture. It earns no trust (C27).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--evidence-root", type=Path, default=Path(".evidence-full"))
    parser.add_argument("--section", action="append", choices=SECTIONS, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    engine: Engine = create_engine(settings.database.sync_dsn(DatabaseRole.API), future=True)
    wanted = args.section or list(SECTIONS)
    try:
        with engine.connect() as connection:
            print(f"STEP 5B.3 REPORT -- cycle {args.cycle!r}")
            print("Every number below is a query against the persisted cycle (C35).")
            for name in SECTIONS:
                if name not in wanted:
                    continue
                if name == "integrity":
                    integrity(connection, args.cycle, args.evidence_root)
                else:
                    globals()[name](connection, args.cycle)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
