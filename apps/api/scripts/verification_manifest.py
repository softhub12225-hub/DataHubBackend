"""The persisted review manifest for Step 5C.5 (sections 5, 7-9, 13-15, 19).

MODE B, AND WHY
===============
Section 19 gives two modes. MODE A applies decisions and needs a real reviewer actor.
MODE B produces a manifest and stops. This runs in **MODE B**, and the reason is not
that the database cannot hold a decision -- it can, and the machinery to record one has
existed since revision `b0c1d2e3f4a5`.

The reason is section 10: *do not use a machine actor to disguise automatic
verification, and do not invent an actor*. There is exactly one `app_user` row. It was
created during Step 5C.3 to attribute a single review decision, it carries no
`password_hash` and no `external_subject`, so nobody can authenticate as it. Writing 120
domain decisions and 385 responsibility decisions under it would record that a person
inspected 505 things they have not seen. The identity would be real and the judgement
would not, which is precisely the disguise section 10 prohibits.

So every row this produces is a **proposal**: a reviewer-ready packet with the evidence
gathered and the question stated, and `proposed_decision` set to `NEEDS_REVIEW` for
everything. Nothing here writes to `official_domain`, `source_mapping`,
`pilot_collected_source.verification_state`, or `source.publication_eligibility`.

WHAT THE EVIDENCE IS, AND WHAT IT IS NOT
========================================
Section 12 asks for explicit reasons rather than an opaque score, so each row carries a
list of named evidence items -- `SUBMITTED_BY_CLIENT`, `HOST_MATCHES_INSTITUTION_TOKEN`,
`PAGE_TITLE_NAMES_INSTITUTION`, `SIBLING_OF_SUBMITTED_HOST`, `REACHED_ONLY_BY_REDIRECT`
-- and no number anywhere. A reviewer can disagree with any single item.

Three things are deliberately absent from the evidence, because section 20 and sections
15-16 forbid them:

* **HTTP success.** 175 pages fetched. A server answered; that is all it means.
* **The TLD.** `.edu` is a registrar's product, not a verification.
* **Candidate counts.** A page that produced 199 candidates is not thereby official, and
  a page that produced none is not thereby fake. Counts appear in the packet as
  *workload*, never as evidence, and they are in a separate block for that reason.

THE THREE UNITS ARE SEPARATE (section 4)
========================================
`domains.json`  -- one row per host. Is this host the institution's?
`sources.json`  -- one row per physical URL. Is this page an appropriate official source?
`responsibilities.json` -- one row per **workbook responsibility claim**, all 385. Is
this page authoritative *for this particular thing*?

A host decision never implies a page decision and a page decision never implies a
responsibility decision. That is why there are three files and not one: an official
admissions page is not thereby a tuition source, and collapsing the 385 responsibility
claims into 319 page decisions would lose exactly that distinction (section 14).
"""

# ruff: noqa: S608 -- the only interpolation in any query here is a table alias or a
# predicate written as a literal at the call site. Every value is a bound parameter.

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.claims.review import CURRENT_PARAMS, current_only

#: Where the manifest lands. A directory of files rather than a table: section 18
#: forbids a duplicate authority system, and a proposal that lived in the database
#: beside the real decisions would be exactly that.
DEFAULT_OUT = Path(".reports/step-5c5")

#: Public suffixes where the registrable domain is the last three labels. Only the ones
#: this fleet contains: a general public-suffix implementation is a dependency, and a
#: guess is worse than a short explicit list. Used to DESCRIBE a relationship between
#: hosts, never to decide one (section 11).
_THREE_LABEL = (
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
    ".edu.tw",
    ".ac.th",
    ".edu.pk",
)

#: Every proposal starts here. There is no code path in this file that sets anything
#: else, and that is the point.
NEEDS_REVIEW = "NEEDS_REVIEW"


def registrable(host: str | None) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    for suffix in _THREE_LABEL:
        if host.endswith(suffix):
            return ".".join(host.split(".")[-3:])
    return ".".join(host.split(".")[-2:])


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    match = re.match(r"^https?://([^/:]+)", url.strip(), re.I)
    return match.group(1).lower() if match else None


def _tokens(name: str) -> set[str]:
    """Words from an institution name worth looking for in a hostname."""
    stop = {
        "university",
        "the",
        "of",
        "college",
        "institute",
        "school",
        "national",
        "state",
        "at",
        "and",
        "for",
        "technology",
        "science",
        "sciences",
        "hong",
    }
    words = re.findall(r"[a-z]+", (name or "").lower())
    return {word for word in words if len(word) >= 4 and word not in stop}


# ===========================================================================
# Access classification (sections 8, 9)
# ===========================================================================

#: How a page's acquisition ended, as classes that must be treated differently.
#: Section 8: a blocked fetch is not evidence that a domain is unofficial. Section 9:
#: an official URL can be dead, and a dead URL's RESPONSIBILITY cannot be verified from
#: its path.
ACCESS_CLASSES = (
    ("BODY_EVIDENCE", "a snapshot with a stored body exists; page content can be read"),
    ("BLOCKED", "the site refused automated access. Says nothing about officialness"),
    ("DEAD_NOT_FOUND", "the server answered 404/410. The URL needs replacing"),
    ("DEAD_HOST", "the hostname does not resolve. The URL needs replacing"),
    ("TLS_FAILURE", "the certificate or handshake failed. A transport problem, not identity"),
    ("OTHER_HTTP", "another HTTP status; neither usable evidence nor proof of anything"),
    ("TIMEOUT", "no answer in time. Transient until shown otherwise"),
    ("NO_ATTEMPT", "never fetched"),
)


def classify_access(row: Any) -> str:
    """Which access class this page is in. Evidence availability, not trust."""
    if row.has_evidence:
        return "BODY_EVIDENCE"
    status = (row.fetch_status or "").upper()
    http = row.http_status
    error = (row.error_class or "").upper()
    if status == "BLOCKED":
        return "BLOCKED"
    if status == "NAME_NOT_RESOLVED":
        return "DEAD_HOST"
    if status == "TIMEOUT":
        return "TIMEOUT"
    if not status:
        return "NO_ATTEMPT"
    if http in (404, 410):
        return "DEAD_NOT_FOUND"
    if "TLS" in error or "SSL" in error or "CERT" in error:
        return "TLS_FAILURE"
    return "OTHER_HTTP"


# ===========================================================================
# The page-level facts everything else is built from
# ===========================================================================

PAGES_SQL = """
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (sn.source_id)
               sn.source_id, sn.effective_url, sn.http_status, sn.content_type,
               sn.redirect_chain, sn.id AS snapshot_id
          FROM snapshot sn ORDER BY sn.source_id, sn.observed_at DESC
    ),
    latest_run AS (
        SELECT DISTINCT ON (fr.source_id)
               fr.source_id, fr.status, fr.http_status, fr.error_class,
               fr.effective_url, fr.redirect_chain
          FROM fetch_run fr ORDER BY fr.source_id, fr.started_at DESC
    ),
    page_extraction AS (
        SELECT DISTINCT ON (ls.source_id)
               ls.source_id, e.output->>'title' AS title,
               e.output->>'media_type' AS media_type
          FROM latest_snapshot ls JOIN extraction e ON e.snapshot_id = ls.snapshot_id
         ORDER BY ls.source_id, e.recorded_at DESC
    )
    SELECT pcs.id AS claim_id, pcs.source_ref, pcs.duplicate_of_source_ref,
           pcs.host AS requested_host, pcs.official_url, pcs.normalized_url,
           pcs.source_type, pcs.degree_scope, pcs.verification_state, pcs.is_third_party,
           pcs.target_institution_id, coalesce(ti.match_key, '?') AS institution,
           s.id AS source_id, s.publication_eligibility, s.fetch_eligibility,
           s.source_type AS source_classification,
           coalesce(ls.effective_url, lr.effective_url) AS effective_url,
           coalesce(ls.redirect_chain, lr.redirect_chain) AS redirect_chain,
           lr.status AS fetch_status, lr.error_class,
           coalesce(ls.http_status, lr.http_status) AS http_status,
           pe.title, pe.media_type,
           (ls.snapshot_id IS NOT NULL) AS has_evidence
      FROM pilot_collected_source pcs
      JOIN source s ON s.id = pcs.acquisition_source_id
      LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
      LEFT JOIN latest_snapshot ls ON ls.source_id = s.id
      LEFT JOIN latest_run lr ON lr.source_id = s.id
      LEFT JOIN page_extraction pe ON pe.source_id = s.id
     ORDER BY institution, pcs.host, pcs.official_url, pcs.source_ref
"""


def load_pages(connection: Connection) -> list[Any]:
    return list(connection.execute(text(PAGES_SQL)).all())


def candidate_counts(connection: Connection) -> dict[Any, int]:
    """Current candidates per source. WORKLOAD, never evidence (section 20)."""
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


def chain_of(value: Any) -> list[dict[str, Any]]:
    chain = json.loads(value) if isinstance(value, str) else value
    return list(chain or [])


# ===========================================================================
# Domain proposals (sections 5, 6, 7, 11, 12)
# ===========================================================================


def build_domains(pages: list[Any], counts: dict[Any, int]) -> list[dict[str, Any]]:
    """One proposal per host, grouped so an institution's hosts are reviewed together."""
    physical = [page for page in pages if page.duplicate_of_source_ref is None]

    by_institution: dict[str, list[Any]] = defaultdict(list)
    for page in physical:
        by_institution[page.institution].append(page)

    # Hosts reached only by following a redirect are not submitted hosts. They need
    # their own decision (section 7) and they are the easiest thing to wave through.
    effective_only: dict[str, dict[str, Any]] = {}
    for page in pages:
        effective = host_of(page.effective_url)
        if effective and effective != page.requested_host:
            entry = effective_only.setdefault(
                effective,
                {"institution": page.institution, "from_hosts": set(), "examples": []},
            )
            entry["from_hosts"].add(page.requested_host)
            if len(entry["examples"]) < 3:
                entry["examples"].append(
                    {
                        "requested_url": page.official_url,
                        "effective_url": page.effective_url,
                        "responsibility": page.source_type,
                        "redirect_chain": chain_of(page.redirect_chain),
                    }
                )

    out: list[dict[str, Any]] = []
    for institution, institution_pages in sorted(by_institution.items()):
        submitted_hosts = sorted({page.requested_host for page in institution_pages})
        name_tokens = _tokens(institution)
        domains = {registrable(host) for host in submitted_hosts}

        for host in submitted_hosts:
            host_pages = [page for page in institution_pages if page.requested_host == host]
            evidence: list[dict[str, str]] = [
                {
                    "kind": "SUBMITTED_BY_CLIENT",
                    "detail": f"{len(host_pages)} page(s) submitted on this host by the "
                    f"client workbook for {institution}",
                }
            ]
            if any(token in host for token in name_tokens):
                matched = sorted(token for token in name_tokens if token in host)
                evidence.append(
                    {
                        "kind": "HOST_MATCHES_INSTITUTION_TOKEN",
                        "detail": f"hostname contains {', '.join(matched)}",
                    }
                )
            titles = sorted({page.title for page in host_pages if page.title})
            naming = [
                title for title in titles if any(token in title.lower() for token in name_tokens)
            ]
            if naming:
                evidence.append(
                    {
                        "kind": "PAGE_TITLE_NAMES_INSTITUTION",
                        "detail": f"{len(naming)} of {len(titles)} stored title(s) name the "
                        f"institution, e.g. {naming[0][:90]!r}",
                    }
                )
            siblings = [
                other
                for other in submitted_hosts
                if other != host and registrable(other) == registrable(host)
            ]
            if siblings:
                evidence.append(
                    {
                        "kind": "SIBLING_OF_SUBMITTED_HOST",
                        "detail": "shares the registrable domain "
                        f"{registrable(host)} with {', '.join(siblings)}. "
                        "A relationship to check, not a reason to inherit trust",
                    }
                )
            access: defaultdict[str, int] = defaultdict(int)
            for page in host_pages:
                access[classify_access(page)] += 1
            if access.get("BODY_EVIDENCE"):
                evidence.append(
                    {
                        "kind": "PAGE_EVIDENCE_AVAILABLE",
                        "detail": f"{access['BODY_EVIDENCE']} page(s) have a stored body a "
                        "reviewer can read. Evidence availability, not officialness",
                    }
                )
            if access.get("BLOCKED"):
                evidence.append(
                    {
                        "kind": "ACCESS_BLOCKED",
                        "detail": f"{access['BLOCKED']} page(s) refused automated access. "
                        "Section 8: this is not evidence the domain is unofficial",
                    }
                )

            out.append(
                {
                    "institution": institution,
                    "host": host,
                    "registrable_domain": registrable(host),
                    "institution_has_multiple_registrable_domains": len(domains) > 1,
                    "sibling_hosts_same_institution": siblings,
                    "proposed_decision": NEEDS_REVIEW,
                    "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
                    "current_decision": "NONE (official_domain has no row for this host)",
                    "question": f"Is {host} controlled by, or officially authorised for, "
                    f"{institution}?",
                    "evidence": evidence,
                    "not_evidence": [
                        "HTTP 200 on any page",
                        "the top-level domain",
                        f"{sum(counts.get(page.source_id, 0) for page in host_pages)} "
                        "extracted candidates",
                    ],
                    "pages": len(host_pages),
                    "access_breakdown": dict(sorted(access.items())),
                    "responsibilities_claimed": sorted(
                        {
                            page.source_type
                            for page in institution_pages
                            if page.requested_host == host
                        }
                    ),
                    "workload_candidates": sum(
                        counts.get(page.source_id, 0) for page in host_pages
                    ),
                    "reached_only_by_redirect": False,
                }
            )

    known = {row["host"] for row in out}
    for host, entry in sorted(effective_only.items()):
        if host in known:
            continue
        sources = sorted(entry["from_hosts"])
        out.append(
            {
                "institution": entry["institution"],
                "host": host,
                "registrable_domain": registrable(host),
                "institution_has_multiple_registrable_domains": False,
                "sibling_hosts_same_institution": sources,
                "proposed_decision": NEEDS_REVIEW,
                "proposed_status_if_accepted": "VERIFIED_OFFICIAL",
                "current_decision": "NONE (official_domain has no row for this host)",
                "question": f"Is {host} controlled by, or officially authorised for, "
                f"{entry['institution']}? It was never submitted: it was reached by "
                f"redirect from {', '.join(sources)}.",
                "evidence": [
                    {
                        "kind": "REACHED_ONLY_BY_REDIRECT",
                        "detail": f"redirected to from {', '.join(sources)}. Section 19: a "
                        "redirect is evidence to inspect, never authorisation. Trust does "
                        "not travel between hosts, nor between subdomains of one domain",
                    },
                    {
                        "kind": "SAME_REGISTRABLE_DOMAIN"
                        if all(registrable(s) == registrable(host) for s in sources)
                        else "CROSSES_REGISTRABLE_DOMAIN",
                        "detail": f"{registrable(sources[0])} -> {registrable(host)}",
                    },
                ],
                "not_evidence": ["the redirect itself", "HTTP 200 after redirecting"],
                "pages": 0,
                "access_breakdown": {},
                "responsibilities_claimed": sorted(
                    {example["responsibility"] for example in entry["examples"]}
                ),
                "workload_candidates": 0,
                "reached_only_by_redirect": True,
                "redirect_examples": entry["examples"],
            }
        )
    return out


# ===========================================================================
# Source and responsibility proposals (sections 13, 14, 15)
# ===========================================================================


def build_sources(pages: list[Any], counts: dict[Any, int]) -> list[dict[str, Any]]:
    """One proposal per PHYSICAL url. 319 of them."""
    out: list[dict[str, Any]] = []
    for page in pages:
        if page.duplicate_of_source_ref is not None:
            continue
        access = classify_access(page)
        effective = host_of(page.effective_url)
        out.append(
            {
                "source_ref": page.source_ref,
                "institution": page.institution,
                "host": page.requested_host,
                "url": page.official_url,
                "effective_url": page.effective_url,
                "effective_host": effective,
                "ended_on_a_different_host": bool(effective and effective != page.requested_host),
                "redirect_chain": chain_of(page.redirect_chain),
                "access_class": access,
                "fetch_status": page.fetch_status,
                "http_status": page.http_status,
                "error_class": page.error_class,
                "stored_title": page.title,
                "media_type": page.media_type,
                "page_evidence_available": bool(page.has_evidence),
                "source_classification": page.source_classification,
                "current_publication_eligibility": str(page.publication_eligibility),
                "proposed_decision": NEEDS_REVIEW,
                "question": f"Is this URL an appropriate official source for "
                f"{page.institution}?",
                "blocked_note": (
                    "Section 8: the domain may still be verifiable from the institution "
                    "relationship and other pages. Do not fake page-content verification "
                    "for a body that was never captured."
                    if access == "BLOCKED"
                    else None
                ),
                "dead_note": (
                    "Section 9: an official URL can be dead. This needs source "
                    "replacement, and its responsibility must NOT be verified from the "
                    "URL path. No replacement is searched for at this stage."
                    if access in ("DEAD_NOT_FOUND", "DEAD_HOST", "TLS_FAILURE")
                    else None
                ),
                "workload_candidates": counts.get(page.source_id, 0),
            }
        )
    return out


def build_responsibilities(
    pages: list[Any], counts: dict[Any, int], produced: dict[Any, dict[str, int]]
) -> list[dict[str, Any]]:
    """One proposal per RESPONSIBILITY CLAIM. All 385, duplicates included.

    Section 14: the 66 duplicate rows are how one URL carries several responsibilities.
    Collapsing them into 319 decisions would lose exactly the distinction this step
    exists to make.
    """
    out: list[dict[str, Any]] = []
    for page in pages:
        access = classify_access(page)
        fields = produced.get(page.claim_id, {})
        out.append(
            {
                "source_ref": page.source_ref,
                "is_duplicate_row": page.duplicate_of_source_ref is not None,
                "duplicate_of": page.duplicate_of_source_ref,
                "institution": page.institution,
                "host": page.requested_host,
                "url": page.official_url,
                "claimed_responsibility": page.source_type,
                "declared_degree_scope": page.degree_scope,
                "current_state": str(page.verification_state),
                "proposed_decision": NEEDS_REVIEW,
                "question": f"Is this page authoritative for {page.source_type}?",
                "page_evidence_available": bool(page.has_evidence),
                "access_class": access,
                "field_kinds_produced": dict(sorted(fields.items())),
                "verifiable_from_page_content": bool(page.has_evidence),
                "note": (
                    "Section 20: what the rules extracted is NOT evidence that the page "
                    "is authoritative for this responsibility. It is shown so a reviewer "
                    "can compare the claim against the page, and a mismatch is a "
                    "question rather than a reclassification."
                ),
            }
        )
    return out


def produced_by_claim(connection: Connection) -> dict[Any, dict[str, int]]:
    rows = connection.execute(
        text(
            f"""
            SELECT c.pilot_collected_source_id AS claim_id, c.field_kind, count(*) AS n
              FROM field_claim_candidate c
             WHERE {current_only("c")}
             GROUP BY 1, 2
            """
        ),
        CURRENT_PARAMS,
    ).all()
    out: dict[Any, dict[str, int]] = defaultdict(dict)
    for row in rows:
        out[row.claim_id][row.field_kind] = row.n
    return out


# ===========================================================================
# Writing it out
# ===========================================================================


#: The manifest format a reviewer approves and an apply command consumes. Bumped when
#: the row shape changes, so an old file cannot be applied by new code that would read
#: its columns differently.
MANIFEST_VERSION = "5c5.1"


def _envelope(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap the rows with an identity a later apply can check (Step 5C.6 section 17).

    The failure this prevents is specific: a reviewer reads manifest A, somebody
    regenerates it, and the apply command silently applies manifest B. The hash is over
    the serialised rows only -- not over the envelope -- so it does not depend on when
    the file was written or what it was called.
    """
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    return {
        "manifest": name,
        "manifest_version": MANIFEST_VERSION,
        "row_count": len(rows),
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "rows": rows,
    }


def _write(out_dir: Path, name: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    envelope = _envelope(name, rows)
    (out_dir / f"{name}.json").write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    with (out_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {
                key: json.dumps(value, ensure_ascii=False, default=str)
                if isinstance(value, list | dict)
                else value
                for key, value in row.items()
            }
            writer.writerow(flat)
    digest = _envelope(name, rows)["content_sha256"]
    print(f"  {out_dir / name}.json  and  .csv   ({len(rows)} row(s))")
    print(f"      sha256 {digest}")


def command_manifest(args: argparse.Namespace) -> int:
    engine = create_engine(get_settings().database.sync_dsn(DatabaseRole.API), future=True)
    with engine.connect() as connection:
        pages = load_pages(connection)
        counts = candidate_counts(connection)
        produced = produced_by_claim(connection)
    engine.dispose()

    domains = build_domains(pages, counts)
    sources = build_sources(pages, counts)
    responsibilities = build_responsibilities(pages, counts, produced)

    out_dir = Path(args.out)
    print("=" * 78)
    print("PERSISTED REVIEW MANIFEST -- MODE B (sections 5, 13, 14, 19)")
    print("=" * 78)
    print(
        "\n  Every proposed_decision is NEEDS_REVIEW. Nothing is written to\n"
        "  official_domain, source_mapping, pilot_collected_source.verification_state\n"
        "  or source.publication_eligibility. See the module docstring for why.\n"
    )
    _write(
        out_dir,
        "domain_decisions_proposed",
        domains,
        [
            "institution",
            "host",
            "registrable_domain",
            "proposed_decision",
            "proposed_status_if_accepted",
            "current_decision",
            "pages",
            "reached_only_by_redirect",
            "sibling_hosts_same_institution",
            "responsibilities_claimed",
            "access_breakdown",
            "workload_candidates",
            "question",
            "evidence",
            "not_evidence",
        ],
    )
    _write(
        out_dir,
        "source_decisions_proposed",
        sources,
        [
            "source_ref",
            "institution",
            "host",
            "url",
            "effective_host",
            "ended_on_a_different_host",
            "access_class",
            "fetch_status",
            "http_status",
            "error_class",
            "stored_title",
            "page_evidence_available",
            "current_publication_eligibility",
            "proposed_decision",
            "question",
            "blocked_note",
            "dead_note",
            "workload_candidates",
        ],
    )
    _write(
        out_dir,
        "responsibility_decisions_proposed",
        responsibilities,
        [
            "source_ref",
            "is_duplicate_row",
            "duplicate_of",
            "institution",
            "host",
            "url",
            "claimed_responsibility",
            "declared_degree_scope",
            "current_state",
            "proposed_decision",
            "page_evidence_available",
            "access_class",
            "field_kinds_produced",
            "verifiable_from_page_content",
            "question",
        ],
    )

    print(f"\n  hosts proposed for review      : {len(domains)}")
    print(
        f"    of which reached only by redirect: "
        f"{sum(1 for row in domains if row['reached_only_by_redirect'])}"
    )
    print(f"  physical sources               : {len(sources)}")
    print(f"  responsibility claims          : {len(responsibilities)}")
    applied = {row["proposed_decision"] for row in domains + sources + responsibilities}
    print(f"\n  distinct proposed decisions    : {sorted(applied)}")
    assert applied == {NEEDS_REVIEW}, "a proposal other than NEEDS_REVIEW was produced"
    print("  STOPPED before modifying any verification state (section 19, MODE B).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return command_manifest(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
