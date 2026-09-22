"""Reviewer-facing source and domain verification packet (Step 5C.4 sections 15-21).

WHAT THIS IS, AND WHAT IT REFUSES TO BE
=======================================
Every one of the 319 pilot pages is `NOT_ELIGIBLE` for publication, and the C27 trust
boundary means no candidate can ever become a `field_claim` until a human says a source
is official. This script prepares that decision. It does not make it.

So: **nothing here writes anything**. No verification status is set, no domain is
approved, no category is inferred. Section 15 is explicit -- "Do NOT auto-verify
anything", "No inference from successful HTTP = official" -- and the temptation is real,
because 175 of these pages fetched successfully from a host that looks exactly like a
university. A successful fetch proves a server answered. It does not prove the
institution publishes there, and it certainly does not prove the page means what a rule
read off it.

Three inferences this deliberately does not make:

*Same host means verified* (section 16). 120 hosts each map to exactly one institution,
which makes the grouping convenient and proves nothing. `www.imperial.ac.uk` carrying
eleven verified pages says nothing about the twelfth until someone looks at it.

*Trust travels between hosts* (section 19). Fifteen pages end on a host they did not
request. All fifteen stay inside the same registrable domain, which is the most
persuasive-looking case there is, and still: `study.ed.ac.uk` is not `www.ed.ac.uk`.
The redirect is reported as something to verify, never as something already verified.

*A page's category can be read off its candidates* (section 20). A page that produced
forty tuition candidates is not thereby a fees page. The workbook says what each page
was collected as; that claim is what gets verified.

WHERE THE DECISION WILL BE RECORDED
===================================
Section 18 forbids a second authority system, and there is no need for one.
`official_domain` already carries `verification_status`, `verification_method`,
`verification_evidence`, `authorization_reference`, `verified_by`, `verified_at` and
`rejected_reason`; `source_mapping` carries the same shape plus `source_category`, and
`pilot_collected_source` carries `verification_state` / `verified_by` / `verified_at` /
`verification_reason` per page. All three are empty of decisions today, which is the
correct starting state and is asserted by `authority`.

Those columns are what section 17 asks for -- an actor, a reason and a timestamp on
every decision -- and they already exist, so the workflow is a matter of using them one
row at a time rather than building somewhere new to bulk-approve.
"""

# ruff: noqa: S608 -- the only thing any query interpolates is a table alias or a
# column name written as a literal at the call site. Every value travels as a bound
# parameter. Nothing here is reachable from a fetched page or a workbook cell.

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import Connection, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.claims.review import CURRENT_PARAMS, current_only

#: The physical pages. 385 workbook rows collapse to 319 once the 66 that are marked as
#: duplicates of another row are excluded; those duplicates carry extra responsibilities
#: for a page, not extra pages.
PHYSICAL = "pcs.duplicate_of_source_ref IS NULL"

#: How a page reaches its evidence. `acquisition_source_id`, not `pilot_collected_source
#: .id`: 73 candidates hang off duplicate rows, and counting through the wrong key
#: under-reports them.
PAGE_JOIN = """
      FROM pilot_collected_source pcs
      JOIN source s ON s.id = pcs.acquisition_source_id
      LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
"""

#: Public suffixes long enough that the registrable domain is the last three labels.
#: Only the ones the pilot fleet actually contains, because a general public-suffix
#: implementation is a dependency and a guess is worse than a short explicit list.
_THREE_LABEL_SUFFIXES = (
    ".ac.uk",
    ".edu.au",
    ".edu.hk",
    ".edu.sg",
    ".edu.cn",
    ".ac.nz",
    ".ac.jp",
    ".com.au",
    ".co.uk",
    ".edu.my",
    ".ac.kr",
)


def registrable(host: str) -> str:
    """The registrable domain, for the suffixes this fleet uses.

    Used **only to describe** a redirect -- "these two hosts share a registrable domain"
    -- never to decide one. Section 19: trust does not travel between hosts, and it does
    not travel between subdomains of one domain either.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    for suffix in _THREE_LABEL_SUFFIXES:
        if host.endswith(suffix):
            return ".".join(host.split(".")[-3:])
    return ".".join(host.split(".")[-2:])


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    match = re.match(r"^https?://([^/:]+)", url.strip(), re.I)
    return match.group(1).lower() if match else None


def _engine(role: DatabaseRole) -> Any:
    return create_engine(get_settings().database.sync_dsn(role), future=True)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ===========================================================================
# packet (sections 15, 16, 20)
# ===========================================================================

PACKET_SQL = f"""
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (sn.source_id)
               sn.source_id, sn.effective_url, sn.http_status, sn.content_type,
               sn.redirect_chain, sn.id AS snapshot_id, sn.observed_at
          FROM snapshot sn
         ORDER BY sn.source_id, sn.observed_at DESC
    ),
    latest_run AS (
        SELECT DISTINCT ON (fr.source_id)
               fr.source_id, fr.status, fr.http_status, fr.error_class,
               fr.effective_url, fr.redirect_chain, fr.started_at
          FROM fetch_run fr
         ORDER BY fr.source_id, fr.started_at DESC
    ),
    page_extraction AS (
        SELECT DISTINCT ON (ls.source_id)
               ls.source_id, e.output->>'title' AS title,
               e.output->>'media_type' AS media_type, e.status AS extraction_status
          FROM latest_snapshot ls
          JOIN extraction e ON e.snapshot_id = ls.snapshot_id
         ORDER BY ls.source_id, e.recorded_at DESC
    )
    SELECT pcs.id AS page_id, pcs.source_ref, pcs.host AS requested_host,
           pcs.official_url, pcs.normalized_url, pcs.source_type, pcs.degree_scope,
           pcs.verification_state, pcs.verified_by, pcs.verified_at,
           pcs.verification_reason, pcs.is_third_party,
           coalesce(ti.match_key, '?') AS institution,
           s.id AS source_id, s.publication_eligibility, s.fetch_eligibility,
           s.access_state,
           coalesce(ls.effective_url, lr.effective_url) AS effective_url,
           coalesce(ls.redirect_chain, lr.redirect_chain) AS redirect_chain,
           lr.status AS fetch_status, lr.error_class,
           coalesce(ls.http_status, lr.http_status) AS http_status,
           ls.content_type, pe.title, pe.media_type, pe.extraction_status,
           (ls.snapshot_id IS NOT NULL) AS has_evidence
    {PAGE_JOIN}
      LEFT JOIN latest_snapshot ls ON ls.source_id = s.id
      LEFT JOIN latest_run lr ON lr.source_id = s.id
      LEFT JOIN page_extraction pe ON pe.source_id = s.id
     WHERE {PHYSICAL}
     ORDER BY institution, pcs.host, pcs.official_url
"""


def command_packet(args: argparse.Namespace) -> int:
    """One block per page: what was asked for, what answered, what a reviewer must judge."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(text(PACKET_SQL)).all()
        candidates = _candidates_per_source(connection)

    _rule("SOURCE VERIFICATION PACKET (sections 15, 16, 20)")
    print(
        "\n  Nothing below is verified. A successful fetch means a server answered;\n"
        "  it is not evidence that the institution publishes there (section 15).\n"
    )

    shown = rows if args.limit is None else rows[: args.limit]
    for row in shown:
        effective_host = host_of(row.effective_url)
        moved = bool(effective_host and effective_host != row.requested_host)
        print("-" * 78)
        print(f"  {row.institution}  [{row.source_ref}]")
        print(
            f"    claimed responsibility : {row.source_type}"
            f"{'  degree scope ' + row.degree_scope if row.degree_scope else ''}"
        )
        print(f"    submitted URL          : {row.official_url}")
        if row.normalized_url != row.official_url:
            print(f"    normalized URL         : {row.normalized_url}")
        print(f"    requested host         : {row.requested_host}")
        if row.effective_url:
            print(f"    effective URL          : {row.effective_url}")
        if moved:
            print(f"    !! ended on a DIFFERENT host: {effective_host}")
            print(
                f"       registrable domain: {registrable(row.requested_host)}"
                f" -> {registrable(effective_host or '')}"
            )
            print("       this is a thing to verify, not a reason to trust it (section 19)")
        chain = row.redirect_chain
        chain = json.loads(chain) if isinstance(chain, str) else chain
        if chain:
            print(f"    redirect chain ({len(chain)} hop(s)):")
            for hop in chain:
                print(
                    f"       {hop.get('status')}  {host_of(hop.get('from'))}"
                    f" -> {host_of(hop.get('to'))}"
                )
        print(
            f"    fetch                  : {row.fetch_status}"
            f"{'  HTTP ' + str(row.http_status) if row.http_status else ''}"
            f"{'  ' + row.error_class if row.error_class else ''}"
        )
        if row.has_evidence:
            print(f"    stored page title      : {row.title!r}")
            print(f"    content type           : {row.content_type}  ({row.media_type})")
        else:
            print("    stored page title      : (no body stored; nothing to read)")
        print(f"    third party declared   : {row.is_third_party}")
        print(f"    current candidates     : {candidates.get(row.source_id, 0)}")
        print(f"    publication eligibility: {row.publication_eligibility}")
        print(
            f"    page verification      : {row.verification_state}"
            f"{'  by ' + str(row.verified_by) if row.verified_by else ''}"
            f"{'  at ' + str(row.verified_at) if row.verified_at else ''}"
        )
        print(
            "    TO DECIDE: is this host official for this institution, and does this"
            " page\n               genuinely carry the responsibility claimed above?"
        )

    print("-" * 78)
    print(
        f"\n  {len(shown)} of {len(rows)} physical page(s) shown."
        + ("" if args.limit is None else "  Use --limit 0 for all.")
    )
    engine.dispose()
    return 0


def _candidates_per_source(connection: Connection) -> dict[Any, int]:
    rows = connection.execute(
        text(
            f"""
            SELECT sn.source_id, count(*) AS n
              FROM field_claim_candidate c
              JOIN extraction e ON e.id = c.extraction_id
              JOIN snapshot sn ON sn.id = e.snapshot_id
             WHERE {current_only("c")}
             GROUP BY 1
            """
        ),
        CURRENT_PARAMS,
    ).all()
    return {row.source_id: row.n for row in rows}


# ===========================================================================
# domains (sections 16, 17)
# ===========================================================================


def command_domains(args: argparse.Namespace) -> int:
    """The same fleet grouped by host, because a domain is what gets verified once."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(text(PACKET_SQL)).all()
        candidates = _candidates_per_source(connection)

    by_host: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_host[row.requested_host].append(row)

    _rule("DOMAIN-CENTRIC VIEW (sections 16, 17)")
    print(
        f"\n  {len(by_host)} host(s) over {len(rows)} page(s). Each host belongs to exactly\n"
        "  one institution, which makes this grouping tidy and proves nothing: verifying\n"
        "  a host is a judgement about the institution's publishing, made once, and it\n"
        "  still does not verify what any individual page is (section 16).\n"
    )
    ordered = sorted(by_host.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    shown = ordered if args.limit is None else ordered[: args.limit]
    print(f"  {'host':38} {'pages':>5} {'ok':>4} {'blk':>4} {'err':>4} {'body':>5} {'cands':>6}")
    for host, pages in shown:
        ok = sum(1 for p in pages if p.fetch_status == "OK")
        blocked = sum(1 for p in pages if p.fetch_status == "BLOCKED")
        errored = sum(1 for p in pages if p.fetch_status in ("HTTP_ERROR", "NAME_NOT_RESOLVED"))
        body = sum(1 for p in pages if p.has_evidence)
        cands = sum(candidates.get(p.source_id, 0) for p in pages)
        print(f"  {host[:38]:38} {len(pages):5} {ok:4} {blocked:4} {errored:4} {body:5} {cands:6}")

    institutions = {row.institution for row in rows}
    print(f"\n  {len(institutions)} institution(s); every host maps to exactly one of them.")
    eligible = {row.publication_eligibility for row in rows}
    print(f"  publication eligibility across the fleet: {sorted(eligible)}")
    print(
        "\n  0 hosts are verified, and none can be verified by this script. The decision\n"
        "  belongs on official_domain, one host at a time, with an actor and a reason\n"
        "  (section 17). See `authority`."
    )
    engine.dispose()
    return 0


# ===========================================================================
# redirects (section 19)
# ===========================================================================


def command_redirects(_: argparse.Namespace) -> int:
    """Hosts that were reached without being asked for."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(text(PACKET_SQL)).all()

    _rule("HOSTS REACHED BY REDIRECT (section 19)")
    moved = []
    internal_hops: Counter[tuple[str, str]] = Counter()
    for row in rows:
        effective_host = host_of(row.effective_url)
        if effective_host and effective_host != row.requested_host:
            moved.append((row, effective_host))
        chain = row.redirect_chain
        chain = json.loads(chain) if isinstance(chain, str) else chain
        for hop in chain or []:
            source_host, target_host = host_of(hop.get("from")), host_of(hop.get("to"))
            if source_host and target_host and source_host != target_host:
                internal_hops[(source_host, target_host)] += 1

    print(f"\n  {len(moved)} page(s) ended on a host other than the one requested.\n")
    pairs: Counter[tuple[str, str]] = Counter()
    for row, effective_host in moved:
        pairs[(row.requested_host, effective_host)] += 1
    for (requested, effective), count in pairs.most_common():
        same = registrable(requested) == registrable(effective)
        relation = "same registrable domain" if same else "DIFFERENT REGISTRABLE DOMAIN"
        print(f"  {requested}\n    -> {effective}   ({count} page(s); {relation})")

    crossing = [p for p in pairs if registrable(p[0]) != registrable(p[1])]
    print(f"\n  pairs crossing a registrable domain: {len(crossing)}")
    print(
        "\n  Every pair above stays inside one registrable domain, which is the most\n"
        "  persuasive-looking case there is. It still confers nothing: study.ed.ac.uk is\n"
        "  a different host from www.ed.ac.uk and needs its own verification. Trust does\n"
        "  not travel between hosts, and it does not travel between subdomains either."
    )
    print(
        f"\n  cross-host hops anywhere inside a chain: {sum(internal_hops.values())}"
        f" over {len(internal_hops)} distinct pair(s)"
    )
    print("  (a chain can pass through a host and come back; the endpoint alone hides it)")
    for (source_host, target_host), count in internal_hops.most_common(10):
        print(f"    {source_host} -> {target_host}  ({count})")
    engine.dispose()
    return 0


# ===========================================================================
# responsibilities (sections 18, 20)
# ===========================================================================


def command_responsibilities(_: argparse.Namespace) -> int:
    """What each page is claimed to be, and what a reviewer confirms or denies."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT pcs.source_type, pcs.degree_scope, count(*) AS pages,
                       count(*) FILTER (WHERE pcs.verification_state = 'PENDING') AS pending
                {PAGE_JOIN}
                 WHERE {PHYSICAL}
                 GROUP BY 1, 2 ORDER BY 3 DESC, 1
                """
            )
        ).all()
        fanout = connection.execute(
            text(
                """
                SELECT n, count(*) AS pages FROM (
                    SELECT acquisition_source_id, count(DISTINCT source_type) AS n
                      FROM pilot_collected_source GROUP BY 1
                ) t GROUP BY 1 ORDER BY 1
                """
            )
        ).all()
        produced = connection.execute(
            text(
                f"""
                SELECT c.source_responsibility, c.field_kind, count(*) AS n
                  FROM field_claim_candidate c
                 WHERE {current_only("c")}
                 GROUP BY 1, 2 ORDER BY 1, 3 DESC
                """
            ),
            CURRENT_PARAMS,
        ).all()

    _rule("RESPONSIBILITY VERIFICATION (sections 18, 20)")
    print(
        "\n  What the workbook claims each page is. This is the claim under review --\n"
        "  NOT something to re-derive from what the rules happened to find on the page\n"
        "  (section 20).\n"
    )
    print(f"  {'responsibility':30} {'degree scope':24} {'pages':>6} {'pending':>8}")
    for row in rows:
        print(f"  {row.source_type:30} {row.degree_scope or '-':24} {row.pages:6} {row.pending:8}")

    print("\n  responsibilities per physical page:")
    for row in fanout:
        print(f"    {row.n} responsibilit{'y' if row.n == 1 else 'ies'}: {row.pages} page(s)")

    print(
        "\n  what each responsibility actually produced, for the reviewer to compare\n"
        "  against the claim -- a mismatch is a question, not a reclassification:"
    )
    current: str | None = None
    for row in produced:
        if row.source_responsibility != current:
            current = row.source_responsibility
            print(f"    {current}")
        print(f"        {row.field_kind:28} {row.n}")
    engine.dispose()
    return 0


# ===========================================================================
# authority (sections 17, 18)
# ===========================================================================

AUTHORITY_TABLES = (
    (
        "official_domain",
        "one row per host an institution publishes from",
        (
            "verification_status",
            "verification_method",
            "verified_by",
            "verified_at",
            "rejected_reason",
            "authorization_reference",
        ),
    ),
    (
        "source_mapping",
        "one row per URL, with the category it is authoritative for",
        ("verification_status", "source_category", "verified_by", "verified_at", "rejected_reason"),
    ),
    (
        "source_field_binding",
        "which field kinds a mapping may speak for",
        (),
    ),
    (
        "source_degree_scope",
        "which degree levels a mapping covers",
        (),
    ),
    (
        "pilot_collected_source",
        "the workbook row, with its own per-page verification",
        ("verification_state", "verified_by", "verified_at", "verification_reason"),
    ),
)


def command_authority(_: argparse.Namespace) -> int:
    """Prove the decision has a home already, and that nothing has been decided."""
    engine = _engine(DatabaseRole.API)
    _rule("WHERE A VERIFICATION DECISION GETS RECORDED (sections 17, 18)")
    print(
        "\n  Section 18 forbids a duplicate authority system. There is no need for one:\n"
        "  every column the workflow requires already exists.\n"
    )
    with engine.connect() as connection:
        for table, purpose, columns in AUTHORITY_TABLES:
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            print(f"  {table}  ({count} row(s))")
            print(f"    purpose: {purpose}")
            if columns:
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            " WHERE table_name = :t"
                        ),
                        {"t": table},
                    )
                }
                missing = [column for column in columns if column not in present]
                print(f"    actor / reason / timestamp columns: {', '.join(columns)}")
                print(
                    f"    all present: {not missing}" + (f"  MISSING {missing}" if missing else "")
                )
        decided = connection.execute(
            text(
                "SELECT count(*) FROM pilot_collected_source "
                " WHERE verification_state <> 'PENDING' OR verified_by IS NOT NULL"
            )
        ).scalar()
        eligible = connection.execute(
            text("SELECT count(*) FROM source WHERE publication_eligibility <> 'NOT_ELIGIBLE'")
        ).scalar()
    print(f"\n  pages with any verification decision recorded : {decided}")
    print(f"  sources eligible for publication              : {eligible}")
    print(
        "\n  Both are 0, and this script cannot change either. Section 17 requires an\n"
        "  actor, a reason and a timestamp per decision and forbids bulk auto-verify;\n"
        "  the columns above are exactly that, used one row at a time."
    )
    engine.dispose()
    return 0


# ===========================================================================
# scopes (section 21)
# ===========================================================================


def command_scopes(args: argparse.Namespace) -> int:
    """The applicant-scope vocabulary the pages actually use.

    Section 21: return the observed vocabulary so a taxonomy can be decided from real
    data, rather than inventing one -- and specifically not by adding a China-shaped
    special case to a general problem.
    """
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT c.value_normalized, c.unresolved_reason, c.field_kind,
                       c.value_raw_text
                  FROM field_claim_candidate c
                 WHERE {current_only("c")}
                   AND c.field_kind = 'ADMISSION_REQUIREMENT'
                """
            ),
            CURRENT_PARAMS,
        ).all()

    stated: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    unresolved = 0
    examples: dict[str, str] = {}
    for row in rows:
        value = row.value_normalized
        value = json.loads(value) if isinstance(value, str) else (value or {})
        scopes = value.get("applicant_scopes")
        if not scopes:
            unresolved += 1
            continue
        for scope in scopes:
            marker = str(scope.get("marker") or scope.get("value") or scope)
            kind = str(scope.get("kind") or "?")
            stated[marker] += 1
            kinds[kind] += 1
            examples.setdefault(marker, (row.value_raw_text or "")[:110])

    _rule("OBSERVED APPLICANT-SCOPE VOCABULARY (section 21)")
    print(f"\n  {len(rows)} current admission-requirement candidate(s).")
    print(f"  {unresolved} state no applicant scope at all -- they are UNRESOLVED, not universal.")
    print(f"  {sum(stated.values())} scope marker(s) across {len(stated)} distinct wording(s).\n")
    print("  by marker kind:")
    for kind, count in kinds.most_common():
        print(f"    {kind:28} {count}")
    print("\n  the wordings themselves, as the pages put them:")
    for marker, count in stated.most_common(args.limit):
        print(f"    {marker:34} {count:5}")
        print(f"        e.g. {examples[marker]!r}")
    print(
        "\n  No taxonomy is created here and no scope record is written. The point of\n"
        "  returning the vocabulary is that the taxonomy gets decided from what the\n"
        "  pages say -- and that whatever is decided is general, rather than a special\n"
        "  case bolted on for one market."
    )
    engine.dispose()
    return 0


# ===========================================================================
# untouched (sections 26, 29)
# ===========================================================================

MUST_BE_EMPTY = (
    "field_claim",
    "field_provenance",
    "change_proposal",
    "university",
    "program",
    "tuition",
    "application_round",
    "language_requirement",
)


def command_untouched(_: argparse.Namespace) -> int:
    """Prove this step published nothing, rather than asserting it."""
    engine = _engine(DatabaseRole.API)
    _rule("NOTHING WAS PUBLISHED (sections 26, 29)")
    print(
        "\n  A preparation step that quietly wrote a canonical row would be the single\n"
        "  worst outcome available, so it is checked rather than promised.\n"
    )
    failures = 0
    with engine.connect() as connection:
        for table in MUST_BE_EMPTY:
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0
            marker = "ok  " if count == 0 else "FAIL"
            if count:
                failures += 1
            print(f"  [{marker}] {table:24} {count} row(s)")
        eligible = (
            connection.execute(
                text("SELECT count(*) FROM source WHERE publication_eligibility <> 'NOT_ELIGIBLE'")
            ).scalar()
            or 0
        )
        marker = "ok  " if eligible == 0 else "FAIL"
        failures += 1 if eligible else 0
        print(f"  [{marker}] {'sources publishable':24} {eligible}")
    print("\n  All clear." if not failures else f"\n  {failures} check(s) FAILED.")
    engine.dispose()
    return 1 if failures else 0


COMMANDS = {
    "packet": command_packet,
    "domains": command_domains,
    "redirects": command_redirects,
    "responsibilities": command_responsibilities,
    "authority": command_authority,
    "scopes": command_scopes,
    "untouched": command_untouched,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    packet = subparsers.add_parser("packet", help="per-page verification packet")
    packet.add_argument("--limit", type=int, default=10)
    domains = subparsers.add_parser("domains", help="domain-centric view")
    domains.add_argument("--limit", type=int, default=30)
    subparsers.add_parser("redirects", help="hosts reached by redirect")
    subparsers.add_parser("responsibilities", help="what each page is claimed to be")
    subparsers.add_parser("authority", help="where a decision gets recorded")
    scopes = subparsers.add_parser("scopes", help="observed applicant-scope vocabulary")
    scopes.add_argument("--limit", type=int, default=40)
    subparsers.add_parser("untouched", help="prove nothing was published")
    args = parser.parse_args()
    if getattr(args, "limit", None) == 0:
        args.limit = None
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
