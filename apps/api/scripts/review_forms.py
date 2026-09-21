"""Write the reviewer's decision forms: one Markdown packet and two CSVs.

WHAT THIS IS FOR
================
`review_packet.py` prints the packet to a terminal. A person working through 129 hosts
and 385 responsibility claims needs something they can keep, annotate and hand back, so
this writes the same content to files with an empty decision field on every row.

It reads **only the three manifests**, re-checks each digest, and stamps every output
with the manifest version, the SHAs, the reviewer's name and email, and the time the
form was generated. A form that did not say which package it belongs to could be filled
in against one manifest and applied against another, which is the failure the hashes
exist to prevent.

NOTHING IS PRESELECTED
======================
Every `DOMAIN DECISION` and `DECISION` field is blank. There is no recommended option,
no ordering by likelihood and no "probably official" hint. The evidence each row carries
is printed as the manifest recorded it, including the `not_evidence` list -- HTTP 200,
the TLD, the institution name appearing in the hostname, the candidate count -- because
those are the four a hurried reviewer would otherwise treat as an answer.

The CSVs have a `decision` column that is empty in every row. `apply-manifest` refuses a
row with no decision rather than defaulting it, so an unfinished form cannot be applied
by accident.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".reports/step-5c5")
DEFAULT_OUT = Path(".reports/step-5c7")

#: Access classes that mean no page body was ever captured, so no reviewer can have
#: read the content. Each needs saying out loud on the form (sections 10 and 11).
NO_BODY = {
    "BLOCKED": "ACCESS BLOCKED -- body content NOT available. Domain identity may still "
    "be reviewable; page content was never captured.",
    "DEAD_NOT_FOUND": "SOURCE_REVIEW_REQUIRED -- HTTP 404. May still be an official "
    "domain. Do not verify the responsibility from the URL path.",
    "DEAD_HOST": "SOURCE_REVIEW_REQUIRED -- hostname does not resolve.",
    "TLS_FAILURE": "SOURCE_REVIEW_REQUIRED -- TLS/certificate failure. A transport "
    "problem, not an identity finding.",
    "OTHER_HTTP": "SOURCE_REVIEW_REQUIRED -- unexpected HTTP status; no usable body.",
    "TIMEOUT": "SOURCE_REVIEW_REQUIRED -- no answer in time.",
    "NO_ATTEMPT": "SOURCE_REVIEW_REQUIRED -- never fetched.",
}

DOMAIN_OPTIONS = ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL", "REJECTED", "NEEDS_REVIEW")
RESPONSIBILITY_OPTIONS = ("VERIFIED", "REJECTED", "NEEDS_REVIEW")


def load(directory: Path, name: str) -> tuple[list[dict[str, Any]], str, str]:
    path = directory / f"{name}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    rows = list(envelope["rows"])
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != envelope.get("content_sha256"):
        raise SystemExit(f"{path} no longer hashes to what it claims; re-generate and re-review")
    return rows, digest, str(envelope.get("manifest_version"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--reviewer-name", required=True)
    parser.add_argument("--reviewer-email", required=True)
    parser.add_argument("--reviewer-id", required=True)
    args = parser.parse_args()

    directory, out_dir = Path(args.dir), Path(args.out)
    domains, domain_sha, version = load(directory, "domain_decisions_proposed")
    sources, source_sha, _ = load(directory, "source_decisions_proposed")
    responsibilities, responsibility_sha, _ = load(directory, "responsibility_decisions_proposed")
    generated = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    header = [
        "# Step 5C.7 source verification review packet",
        "",
        f"- **Reviewer**: {args.reviewer_name} <{args.reviewer_email}>",
        f"- **Reviewer id**: `{args.reviewer_id}`",
        f"- **Manifest version**: `{version}`",
        f"- **Domains manifest sha256**: `{domain_sha}` ({len(domains)} rows)",
        f"- **Sources manifest sha256**: `{source_sha}` ({len(sources)} rows)",
        f"- **Responsibilities manifest sha256**: `{responsibility_sha}` "
        f"({len(responsibilities)} rows)",
        f"- **Form generated (UTC)**: {generated}",
        "",
        "Nothing in this packet is preselected. Decide the **domain** for each host "
        "before the responsibilities on it.",
        "",
        "HTTP 200, the top-level domain, the institution's name appearing in a hostname, "
        "a shared registrable domain and the number of extracted candidates are "
        "**evidence for you**, not answers. None of them establishes that a host is "
        "officially the institution's.",
        "",
    ]

    by_institution: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"domains": [], "sources": [], "responsibilities": []}
    )
    for row in domains:
        by_institution[str(row["institution"])]["domains"].append(row)
    for row in sources:
        by_institution[str(row["institution"])]["sources"].append(row)
    for row in responsibilities:
        by_institution[str(row["institution"])]["responsibilities"].append(row)

    source_by_url = {str(row["url"]): row for row in sources}
    multi_pages = {
        url
        for url, claims in _group(responsibilities, "url").items()
        if len({str(c["claimed_responsibility"]) for c in claims}) > 1
    }

    body: list[str] = []
    body.append("## Contents\n")
    for institution in sorted(by_institution):
        entry = by_institution[institution]
        body.append(
            f"- {institution} — {len(entry['domains'])} host(s), "
            f"{len(entry['sources'])} page(s), {len(entry['responsibilities'])} claim(s)"
        )
    body.append("")

    for institution in sorted(by_institution):
        entry = by_institution[institution]
        redirect_only = [d for d in entry["domains"] if d.get("reached_only_by_redirect")]
        submitted = [d for d in entry["domains"] if not d.get("reached_only_by_redirect")]
        body.append("\n---\n")
        body.append(f"## {institution}\n")
        body.append(
            f"{len(submitted)} submitted host(s) · {len(redirect_only)} redirect-only "
            f"host(s) · {len(entry['sources'])} page(s) · "
            f"{len(entry['responsibilities'])} responsibility claim(s)\n"
        )

        body.append("### 1. Domain decisions — decide these first\n")
        for row in sorted(entry["domains"], key=lambda r: str(r["host"])):
            flag = (
                " — **REDIRECT-ONLY, never submitted**"
                if row.get("reached_only_by_redirect")
                else ""
            )
            body.append(f"#### `{row['host']}`{flag}\n")
            body.append(f"- registrable domain: `{row['registrable_domain']}`")
            siblings = row.get("sibling_hosts_same_institution") or []
            if siblings:
                body.append(f"- sibling host(s): {', '.join(f'`{s}`' for s in siblings)}")
            body.append(f"- pages: {row['pages']} — access {row.get('access_breakdown') or {}}")
            body.append(
                f"- responsibilities on this host: "
                f"{', '.join(row.get('responsibilities_claimed') or []) or '(none)'}"
            )
            body.append(f"- current state: {row['current_decision']}")
            body.append(f"\n**Question**: {row['question']}\n")
            body.append("Evidence:\n")
            for item in row.get("evidence") or []:
                body.append(f"- `{item['kind']}` — {item['detail']}")
            body.append("\nExplicitly **not** evidence:\n")
            for item in row.get("not_evidence") or []:
                body.append(f"- {item}")
            for example in row.get("redirect_examples") or []:
                hops = " → ".join(
                    f"{hop.get('status')} {hop.get('to')}"
                    for hop in example.get("redirect_chain", [])
                )
                body.append(f"\n- redirect: `{example['requested_url']}`")
                body.append(f"  - chain: {hops or '(endpoint only)'}")
                body.append(f"  - affects: {example.get('responsibility')}")
            body.append("\n```")
            body.append("DOMAIN DECISION:")
            for option in DOMAIN_OPTIONS:
                body.append(f"  [ ] {option}")
            body.append("")
            body.append("REASON:")
            body.append("________________________________________________________")
            body.append("```\n")

        body.append("### 2. Pages on these hosts — context for the decisions below\n")
        for row in sorted(entry["sources"], key=lambda r: str(r["url"])):
            moved = f" → `{row['effective_host']}`" if row.get("ended_on_a_different_host") else ""
            body.append(f"- `{row['url']}`{moved}")
            body.append(
                f"  - access: **{row['access_class']}**"
                f"{'' if row['page_evidence_available'] else ' — no stored body'}"
            )
            if row.get("stored_title"):
                body.append(f"  - title: {str(row['stored_title'])[:110]!r}")
            note = NO_BODY.get(str(row["access_class"]))
            if note:
                body.append(f"  - **{note}**")
        body.append("")

        body.append("### 3. Responsibility decisions — one per claim\n")
        for url, claims in sorted(_group(entry["responsibilities"], "url").items()):
            page = source_by_url.get(url, {})
            multi = url in multi_pages
            body.append(f"#### `{url}`")
            if multi:
                body.append(
                    f"\n> **{len(claims)} responsibilities on ONE page.** Decide each "
                    "separately. One may be VERIFIED while another is REJECTED.\n"
                )
            access = str(page.get("access_class", "?"))
            body.append(f"- access: **{access}**")
            if page.get("stored_title"):
                body.append(f"- title: {str(page['stored_title'])[:110]!r}")
            note = NO_BODY.get(access)
            if note:
                body.append(f"- **{note}**")
            for claim in sorted(claims, key=lambda c: str(c["claimed_responsibility"])):
                body.append(f"\n**RESPONSIBILITY: {claim['claimed_responsibility']}**\n")
                if claim.get("declared_degree_scope"):
                    body.append(f"- declared degree scope: {claim['declared_degree_scope']}")
                produced = claim.get("field_kinds_produced") or {}
                body.append(
                    f"- candidates extracted from this page: {produced or 'none'} "
                    "— *context only, not proof of authority*"
                )
                body.append(f"- body evidence available: {claim['page_evidence_available']}")
                body.append(f"- current state: {claim['current_state']}")
                body.append("\n```")
                body.append("DECISION:")
                for option in RESPONSIBILITY_OPTIONS:
                    body.append(f"  [ ] {option}")
                body.append("")
                body.append("REASON:")
                body.append("________________________________________________________")
                body.append("```")
            body.append("")

    footer = [
        "\n---\n",
        "## Applying these decisions\n",
        "Fill the CSVs beside this file, then apply them one manifest at a time with "
        "`--dry-run` first. A row left blank is refused, never treated as VERIFIED.\n",
        f"- domains sha256: `{domain_sha}`",
        f"- responsibilities sha256: `{responsibility_sha}`",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / "step-5c7-review-packet.md"
    markdown.write_text("\n".join(header + body + footer), encoding="utf-8")

    stamp = {
        "manifest_version": version,
        "reviewer_name": args.reviewer_name,
        "reviewer_email": args.reviewer_email,
        "reviewer_id": args.reviewer_id,
        "generated_utc": generated,
    }
    domain_csv = out_dir / "step-5c7-domain-review.csv"
    with domain_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"# {k}={v}" for k, v in stamp.items()])
        writer.writerow([f"# manifest_sha256={domain_sha}"])
        writer.writerow(
            [
                "institution",
                "host",
                "redirect_only",
                "pages",
                "access",
                "question",
                "decision",
                "reason",
            ]
        )
        for row in sorted(domains, key=lambda r: (str(r["institution"]), str(r["host"]))):
            writer.writerow(
                [
                    row["institution"],
                    row["host"],
                    row.get("reached_only_by_redirect"),
                    row["pages"],
                    json.dumps(row.get("access_breakdown") or {}),
                    row["question"],
                    "",
                    "",
                ]
            )

    responsibility_csv = out_dir / "step-5c7-responsibility-review.csv"
    with responsibility_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"# {k}={v}" for k, v in stamp.items()])
        writer.writerow([f"# manifest_sha256={responsibility_sha}"])
        writer.writerow(
            [
                "institution",
                "source_ref",
                "url",
                "responsibility",
                "multi_responsibility_page",
                "access_class",
                "body_evidence",
                "degree_scope",
                "decision",
                "reason",
            ]
        )
        for row in sorted(responsibilities, key=lambda r: (str(r["institution"]), str(r["url"]))):
            page = source_by_url.get(str(row["url"]), {})
            writer.writerow(
                [
                    row["institution"],
                    row["source_ref"],
                    row["url"],
                    row["claimed_responsibility"],
                    str(row["url"]) in multi_pages,
                    row.get("access_class") or page.get("access_class"),
                    row["page_evidence_available"],
                    row.get("declared_degree_scope") or "",
                    "",
                    "",
                ]
            )

    print(f"  {markdown}")
    print(f"  {domain_csv}")
    print(f"  {responsibility_csv}")
    print(f"\n  institutions           : {len(by_institution)}")
    print(
        f"  domain rows to decide  : {len(domains)}"
        f"  ({sum(1 for r in domains if r.get('reached_only_by_redirect'))} redirect-only)"
    )
    print(f"  responsibility rows    : {len(responsibilities)}")
    print(f"  multi-responsibility pages: {len(multi_pages)}")
    blank = sum(1 for r in domains if True)
    print(f"  decisions preselected  : 0 of {blank + len(responsibilities)}")
    return 0


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[str(row[key])].append(row)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
