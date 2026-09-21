"""Group the approved review packages by institution, for a human to work through.

WHY THIS READS THE MANIFESTS AND NOT THE DATABASE
=================================================
Step 5C.6 section 3: a reviewer must know which exact immutable package they are
reviewing, and applying manifest B with approval given to manifest A is the failure the
content hash exists to prevent. If this view re-derived its rows from the database it
could disagree with the package under review the moment anything changed, and the
disagreement would be invisible.

So it is a **view of the three manifests**, reads nothing else, and prints their SHAs at
the top and the bottom. What a reviewer sees here is what they would be approving.

GROUPING IS ERGONOMICS, NOT SEMANTICS
=====================================
Section 4 asks for this because 129 unordered hosts is not a thing a person can review
carefully. The decisions stay independent: one institution's hosts are shown together
and are still decided one at a time, and a page verified for one responsibility says
nothing about the next.

NOTHING HERE DECIDES ANYTHING
=============================
No default, no recommendation, no ranking by likelihood. The evidence each manifest row
carries is printed as the manifest recorded it, including the `not_evidence` block --
HTTP success, the TLD, and the candidate count are listed as things that prove nothing,
because they are the three a hurried reviewer would otherwise lean on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".reports/step-5c5")

MANIFESTS = (
    "domain_decisions_proposed",
    "source_decisions_proposed",
    "responsibility_decisions_proposed",
)


def load(directory: Path, name: str) -> tuple[list[dict[str, Any]], str]:
    """Rows plus the digest, re-checked against the content rather than trusted."""
    path = directory / f"{name}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    rows = list(envelope["rows"])
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != envelope.get("content_sha256"):
        raise SystemExit(
            f"{path} has been edited since it was generated. Re-generate it and "
            "re-review; its rows no longer hash to what it claims."
        )
    return rows, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--institution", default=None, help="show only this institution")
    parser.add_argument("--limit", type=int, default=0, help="0 for all institutions")
    args = parser.parse_args()

    directory = Path(args.dir)
    domains, domain_sha = load(directory, "domain_decisions_proposed")
    sources, source_sha = load(directory, "source_decisions_proposed")
    responsibilities, responsibility_sha = load(directory, "responsibility_decisions_proposed")

    print("=" * 78)
    print("REVIEW PACKAGE UNDER REVIEW")
    print("=" * 78)
    for name, rows, digest in (
        ("domains", domains, domain_sha),
        ("sources", sources, source_sha),
        ("responsibilities", responsibilities, responsibility_sha),
    ):
        print(f"  {name:18} {len(rows):5} rows   sha256 {digest}")
    print(
        "\n  Approve by these digests. `source_review.py apply-manifest --expect-sha256`\n"
        "  refuses any other file, so approval given here cannot be spent elsewhere."
    )

    by_institution: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"domains": [], "sources": [], "responsibilities": []}
    )
    for row in domains:
        by_institution[str(row["institution"])]["domains"].append(row)
    for row in sources:
        by_institution[str(row["institution"])]["sources"].append(row)
    for row in responsibilities:
        by_institution[str(row["institution"])]["responsibilities"].append(row)

    names = sorted(by_institution)
    if args.institution:
        names = [n for n in names if args.institution.lower() in n.lower()]
    shown = names if not args.limit else names[: args.limit]

    for institution in shown:
        entry = by_institution[institution]
        print("\n" + "=" * 78)
        print(f"{institution.upper()}")
        print("=" * 78)

        redirect_only = [d for d in entry["domains"] if d.get("reached_only_by_redirect")]
        submitted = [d for d in entry["domains"] if not d.get("reached_only_by_redirect")]
        print(
            f"\n  {len(submitted)} submitted host(s), {len(redirect_only)} reached only by "
            f"redirect, {len(entry['sources'])} page(s), "
            f"{len(entry['responsibilities'])} responsibility claim(s)"
        )

        print("\n  --- DOMAIN DECISIONS (decide these first) ---")
        for row in sorted(entry["domains"], key=lambda r: str(r["host"])):
            marker = "  [REDIRECT-ONLY]" if row.get("reached_only_by_redirect") else ""
            print(f"\n    {row['host']}{marker}")
            print(f"      registrable domain : {row['registrable_domain']}")
            siblings = row.get("sibling_hosts_same_institution") or []
            if siblings:
                print(f"      siblings           : {', '.join(siblings)}")
            print(
                f"      pages              : {row['pages']}   "
                f"access {row.get('access_breakdown') or {}}"
            )
            print(f"      current decision   : {row['current_decision']}")
            print(f"      QUESTION           : {row['question']}")
            for item in row.get("evidence") or []:
                print(f"        + {item['kind']}: {item['detail']}")
            for item in row.get("not_evidence") or []:
                print(f"        - NOT evidence: {item}")
            for example in row.get("redirect_examples") or []:
                hops = " -> ".join(
                    f"{hop.get('status')} {hop.get('to')}"
                    for hop in example.get("redirect_chain", [])
                )
                print(f"        chain: {example['requested_url']}")
                print(f"               {hops or '(endpoint only)'}")
            print("      DECISION: ______________  REASON: ______________")

        print("\n  --- PAGES ---")
        for row in sorted(entry["sources"], key=lambda r: str(r["url"]))[:12]:
            moved = f"  -> {row['effective_host']}" if row.get("ended_on_a_different_host") else ""
            print(f"    [{row['access_class']:15}] {row['url']}{moved}")
            if row.get("stored_title"):
                print(f"        title: {str(row['stored_title'])[:88]!r}")
            for note in ("blocked_note", "dead_note"):
                if row.get(note):
                    print(f"        ! {row[note]}")
        if len(entry["sources"]) > 12:
            print(f"    ... and {len(entry['sources']) - 12} more")

        print("\n  --- RESPONSIBILITY DECISIONS (one per claim, decided separately) ---")
        per_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in entry["responsibilities"]:
            per_url[str(row["url"])].append(row)
        for url, claims in sorted(per_url.items()):
            if len(claims) > 1:
                print(f"\n    {url}   ** {len(claims)} responsibilities on ONE page **")
            else:
                print(f"\n    {url}")
            for claim in sorted(claims, key=lambda c: str(c["claimed_responsibility"])):
                evidence = (
                    "body evidence"
                    if claim["page_evidence_available"]
                    else (f"NO BODY EVIDENCE ({claim['access_class']})")
                )
                print(f"      {claim['claimed_responsibility']:26} [{evidence}]")
                if claim.get("declared_degree_scope"):
                    print(f"        degree scope   : {claim['declared_degree_scope']}")
                produced = claim.get("field_kinds_produced") or {}
                if produced:
                    print(f"        rules produced : {produced}  (context, NOT authority)")
                print("        DECISION: ____________  REASON: ____________")

    print("\n" + "=" * 78)
    print(f"{len(shown)} of {len(names)} institution(s) shown.")
    print(f"  domains          sha256 {domain_sha}")
    print(f"  sources          sha256 {source_sha}")
    print(f"  responsibilities sha256 {responsibility_sha}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
