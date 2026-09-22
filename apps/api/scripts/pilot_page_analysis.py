"""Technical structure of the stored bodies, for Step 5C parser design (§18-21).

WHAT THIS IS NOT
================
It is **not** extraction. Nothing here reads a page for what it *says*: no fee, no
deadline, no requirement, no `field_claim`. It counts elements and measures text, which
is the difference between "this page has four tables" and "this page says tuition is
£38,000". The first is parser design; the second is Step 5C and has not been approved.

WHAT IT ANSWERS
===============
One question: **what machinery will Step 5C actually need?** Guessing costs either a
400 MB browser dependency nobody needed or a parser that silently returns nothing for a
third of the fleet. Both are avoidable by measuring first.

The classification is deliberately technical and carries no trust or category meaning:
`STATIC_HTML` says a parser can read the bytes we already hold, and says nothing about
whether the page is the tuition page or whether anyone has verified it.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import FilesystemEvidenceStore

# --- crude, on purpose -------------------------------------------------------------
# These are counts for sizing a parser, not a parse. A real extractor will use a proper
# HTML tree; regexes here keep this script free of a parsing dependency it would then
# be tempting to reuse for extraction.
SCRIPT_BLOCK = re.compile(rb"<script\b[^>]*>.*?</script\s*>", re.S | re.I)
STYLE_BLOCK = re.compile(rb"<style\b[^>]*>.*?</style\s*>", re.S | re.I)
COMMENT = re.compile(rb"<!--.*?-->", re.S)
TAG = re.compile(rb"<[^>]+>")
TITLE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.S | re.I)
HEADING = re.compile(rb"<h[1-6]\b", re.I)
LIST = re.compile(rb"<[uo]l\b", re.I)
LIST_ITEM = re.compile(rb"<li\b", re.I)
TABLE = re.compile(rb"<table\b", re.I)
IFRAME = re.compile(rb"<(iframe|embed|object)\b", re.I)
SCRIPT_TAG = re.compile(rb"<script\b", re.I)
JSON_LD = re.compile(rb'type\s*=\s*[\'"]application/ld\+json[\'"]', re.I)
NEXT_DATA = re.compile(rb'id\s*=\s*[\'"]__NEXT_DATA__[\'"]', re.I)
PDF_LINK = re.compile(rb'href\s*=\s*[\'"]([^\'"]*?\.pdf(?:\?[^\'"]*)?)[\'"]', re.I)
#: Root elements a single-page app mounts into. Presence alone proves nothing -- plenty
#: of server-rendered pages have a `<div id="root">` -- so it only counts alongside a
#: near-empty body.
SPA_ROOT = re.compile(
    rb'<(?:div|main)\b[^>]*\bid\s*=\s*[\'"](?:root|app|__next|application)[\'"]', re.I
)

#: Below this much visible text an HTML page is not usable by a static parser, whatever
#: else is true of it. Chosen from the Step 5B.1 evidence, where the thinnest real page
#: carried ~4,000 characters and the only page under this was a WAF interstitial.
MIN_USABLE_TEXT = 400


@dataclass
class PageShape:
    source_id: str
    url: str
    institution: str
    ref: str
    categories: list[str]
    media: str
    byte_size: int
    classification: str = "UNAVAILABLE"
    reasons: list[str] = field(default_factory=list)
    title: str | None = None
    visible_text: int = 0
    headings: int = 0
    lists: int = 0
    list_items: int = 0
    tables: int = 0
    scripts: int = 0
    iframes: int = 0
    json_ld: int = 0
    embedded_json: bool = False
    pdf_links: int = 0
    pdf_link_samples: list[str] = field(default_factory=list)
    spa_shell: bool = False


def analyse_html(payload: bytes, shape: PageShape) -> None:
    """Count structure. Never interpret meaning."""
    shape.scripts = len(SCRIPT_TAG.findall(payload))
    shape.iframes = len(IFRAME.findall(payload))
    shape.json_ld = len(JSON_LD.findall(payload))
    shape.embedded_json = bool(NEXT_DATA.search(payload)) or shape.json_ld > 0
    shape.headings = len(HEADING.findall(payload))
    shape.lists = len(LIST.findall(payload))
    shape.list_items = len(LIST_ITEM.findall(payload))
    shape.tables = len(TABLE.findall(payload))

    found = TITLE.search(payload)
    if found:
        raw = TAG.sub(b" ", found.group(1))
        shape.title = " ".join(raw.decode("utf-8", "replace").split())[:120]

    links = PDF_LINK.findall(payload)
    shape.pdf_links = len(links)
    shape.pdf_link_samples = [
        link.decode("utf-8", "replace")[:100] for link in list(dict.fromkeys(links))[:3]
    ]

    # Visible text: what a static parser would have to work with. Scripts, styles and
    # comments removed first, because a 400 KB page of JavaScript is not 400 KB of
    # readable content and counting it as such is how a browser gets bought for nothing.
    body = COMMENT.sub(b" ", STYLE_BLOCK.sub(b" ", SCRIPT_BLOCK.sub(b" ", payload)))
    visible = re.sub(rb"\s+", b" ", TAG.sub(b" ", body)).strip()
    shape.visible_text = len(visible)
    shape.spa_shell = bool(SPA_ROOT.search(payload)) and shape.visible_text < MIN_USABLE_TEXT


def classify(shape: PageShape) -> None:
    """Technical classification only (§19). Not a publication category."""
    media = shape.media
    if media.startswith("application/pdf"):
        shape.classification = "PDF_DOCUMENT"
        shape.reasons.append("served as application/pdf")
        return
    if media.startswith(("application/json", "application/ld+json")):
        shape.classification = "STRUCTURED_JSON"
        shape.reasons.append("served as JSON")
        return

    if shape.visible_text >= MIN_USABLE_TEXT:
        shape.classification = "STATIC_HTML"
        shape.reasons.append(
            f"{shape.visible_text} characters of visible text in the first response"
        )
        if shape.json_ld:
            shape.reasons.append(f"{shape.json_ld} JSON-LD block(s) available as a bonus")
        return

    # Thin body. Distinguish "needs a browser" from "we were given nothing".
    shape.classification = "POSSIBLE_BROWSER_REQUIRED"
    shape.reasons.append(f"only {shape.visible_text} characters of visible text")
    if shape.spa_shell:
        shape.reasons.append("an app-root element with an effectively empty body")
    if shape.scripts:
        shape.reasons.append(f"{shape.scripts} script tag(s)")
    if shape.embedded_json:
        shape.reasons.append("embedded JSON present, which may make a browser unnecessary")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path(".evidence-full"))
    parser.add_argument("--list-all", action="store_true", help="one line per page")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole.API), future=True)
    store = FilesystemEvidenceStore(args.evidence_root)

    shapes: list[PageShape] = []
    unavailable: list[dict[str, Any]] = []

    with engine.connect() as connection:
        shapes = _analyse_stored(connection, store)
        unavailable = _unavailable(connection)
    engine.dispose()

    _report(shapes, unavailable, list_all=args.list_all)
    return 0


def _analyse_stored(connection: Connection, store: FilesystemEvidenceStore) -> list[PageShape]:
    rows = connection.execute(
        text(
            "SELECT DISTINCT ON (sn.source_id) sn.source_id, s.url, sn.content_type, "
            "       sn.content_hash, b.byte_size, "
            "       coalesce(ti.match_key, '?') AS institution, "
            "       coalesce(t.physical_source_ref, '?') AS ref, "
            "       coalesce(t.categories, ARRAY[]::text[]) AS categories "
            "  FROM snapshot sn "
            "  JOIN source s ON s.id = sn.source_id "
            "  JOIN content_blob b ON b.content_hash = sn.content_hash "
            "  LEFT JOIN acquisition_target t ON t.source_id = sn.source_id "
            "  LEFT JOIN target_institution ti ON ti.id = t.target_institution_id "
            " ORDER BY sn.source_id, sn.observed_at DESC"
        )
    ).all()

    shapes: list[PageShape] = []
    for row in rows:
        media = (row.content_type or "").split(";")[0].strip().lower()
        shape = PageShape(
            source_id=str(row.source_id),
            url=row.url,
            institution=row.institution,
            ref=row.ref,
            categories=list(row.categories),
            media=media,
            byte_size=row.byte_size,
        )
        try:
            payload = store.get(row.content_hash)
        except (OSError, KeyError):
            shape.classification = "UNAVAILABLE"
            shape.reasons.append("the stored object could not be read")
            shapes.append(shape)
            continue

        if payload[:5] == b"%PDF-":
            shape.media = "application/pdf"
            shape.title = payload[:8].decode("ascii", "replace").strip()
        elif media.startswith("application/json"):
            try:
                json.loads(payload.decode("utf-8", "replace"))
                shape.reasons.append("parsed as JSON")
            except ValueError:
                shape.reasons.append("declared JSON but did not parse")
        else:
            analyse_html(payload, shape)
        classify(shape)
        shapes.append(shape)
    return shapes


def _unavailable(connection: Connection) -> list[dict[str, Any]]:
    """Pages with no body, classified by *why* -- a different question from shape."""
    rows = connection.execute(
        text(
            "SELECT s.url, h.last_status, h.last_http_status, h.last_error_class, "
            "       h.schedule_state, coalesce(ti.match_key, '?') AS institution, "
            "       coalesce(t.physical_source_ref, '?') AS ref "
            "  FROM source s JOIN source_health h ON h.source_id = s.id "
            "  LEFT JOIN acquisition_target t ON t.source_id = s.id "
            "  LEFT JOIN target_institution ti ON ti.id = t.target_institution_id "
            " WHERE h.last_content_hash IS NULL"
        )
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.last_status == "BLOCKED":
            kind = "BLOCKED"
        elif row.last_http_status in (404, 410):
            kind = "SOURCE_ERROR"
        elif row.last_status is None:
            kind = "NOT_ATTEMPTED"
        else:
            kind = "UNAVAILABLE"
        out.append(
            {
                "classification": kind,
                "url": row.url,
                "institution": row.institution,
                "ref": row.ref,
                "status": row.last_status,
                "http": row.last_http_status,
                "error": row.last_error_class,
                "schedule_state": row.schedule_state,
            }
        )
    return out


def _report(shapes: list[PageShape], unavailable: list[dict[str, Any]], *, list_all: bool) -> None:
    print("STEP 5B.3 -- PAGE-TYPE TECHNICAL ANALYSIS")
    print("Structure only. Nothing here reads a page for what it says (no extraction).\n")

    print("=" * 78)
    print("PARSER-NEED CLASSIFICATION (technical; not a publication category)")
    print("=" * 78)
    counts = Counter(shape.classification for shape in shapes)
    counts.update(item["classification"] for item in unavailable)
    for name in (
        "STATIC_HTML",
        "PDF_DOCUMENT",
        "STRUCTURED_JSON",
        "POSSIBLE_BROWSER_REQUIRED",
        "BLOCKED",
        "SOURCE_ERROR",
        "UNAVAILABLE",
        "NOT_ATTEMPTED",
    ):
        print(f"  {name:<28} {counts.get(name, 0)}")
    print(f"  {'TOTAL':<28} {sum(counts.values())}")

    html = [s for s in shapes if s.classification == "STATIC_HTML"]
    if html:
        texts = sorted(s.visible_text for s in html)
        print("\n" + "=" * 78)
        print("STATIC HTML -- what a parser would be working with")
        print("=" * 78)
        print(f"  pages                        {len(html)}")
        print(f"  visible text: min            {texts[0]:,} characters")
        print(f"                median         {texts[len(texts) // 2]:,}")
        print(f"                max            {texts[-1]:,}")
        print(f"  pages with any <table>       {sum(1 for s in html if s.tables)}")
        print(f"  total <table> elements       {sum(s.tables for s in html)}")
        print(f"  pages with any <ul>/<ol>     {sum(1 for s in html if s.lists)}")
        print(f"  median lists per page        {sorted(s.lists for s in html)[len(html) // 2]}")
        print(f"  median headings per page     {sorted(s.headings for s in html)[len(html) // 2]}")
        print(f"  pages with JSON-LD           {sum(1 for s in html if s.json_ld)}")
        print(f"  pages with embedded JSON     {sum(1 for s in html if s.embedded_json)}")
        print(f"  pages with iframe/embed      {sum(1 for s in html if s.iframes)}")
        print(f"  pages linking to a PDF       {sum(1 for s in html if s.pdf_links)}")

    browser = [s for s in shapes if s.classification == "POSSIBLE_BROWSER_REQUIRED"]
    print("\n" + "=" * 78)
    print("POSSIBLE_BROWSER_REQUIRED -- evidence, not a decision (§20)")
    print("=" * 78)
    print(f"  pages                        {len(browser)}")
    if not browser:
        print("  None. Every stored HTML body carried usable text in the first response,")
        print("  so the fleet gives no argument for a browser fetcher.")
    for shape in browser:
        print(f"\n  {shape.institution[:40]}  {shape.ref}")
        print(f"    {shape.url[:88]}")
        print(f"    title: {shape.title or '(none)'}")
        for reason in shape.reasons:
            print(f"    - {reason}")

    print("\n" + "=" * 78)
    print("PDF INVENTORY (§21)")
    print("=" * 78)
    direct = [s for s in shapes if s.classification == "PDF_DOCUMENT"]
    linking = [s for s in shapes if s.classification == "STATIC_HTML" and s.pdf_links]
    print(f"  registered sources that ARE a PDF        {len(direct)}")
    for shape in direct:
        print(f"    {shape.byte_size:>10,} bytes  {shape.institution[:34]:<34} {shape.url[:60]}")
    print(f"\n  HTML pages that LINK to PDFs             {len(linking)}")
    print(f"  distinct PDF links seen on them           {sum(s.pdf_links for s in linking)}")
    print("  Not downloaded: a linked PDF is not a registered acquisition target, and")
    print("  following links would be crawling rather than fetching a named source.")
    for shape in sorted(linking, key=lambda s: -s.pdf_links)[:10]:
        print(f"    {shape.pdf_links:>3} link(s)  {shape.institution[:34]:<34} {shape.ref}")
        for sample in shape.pdf_link_samples:
            print(f"        {sample}")

    print("\n" + "=" * 78)
    print("PAGES WITH NO BODY -- why (a different question from shape)")
    print("=" * 78)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in unavailable:
        by_kind.setdefault(item["classification"], []).append(item)
    for kind, items in sorted(by_kind.items()):
        print(f"\n  {kind}  ({len(items)})")
        for item in items:
            print(
                f"    {item['institution'][:36]:<36} {item['ref']:<7} "
                f"{item['status']}/{item['http']}"
            )
            print(f"      {item['url'][:86]}")
            if item["error"]:
                print(f"      {item['error'][:86]}")

    if list_all:
        print("\n" + "=" * 78)
        print("EVERY STORED PAGE")
        print("=" * 78)
        header = (
            f"  {'ref':<7} {'class':<26} {'text':>8} {'tbl':>4} {'lst':>4} "
            f"{'hd':>4} {'scr':>4} {'jld':>4} {'pdf':>4}  institution"
        )
        print(header)
        for shape in sorted(shapes, key=lambda s: (s.classification, s.ref)):
            print(
                f"  {shape.ref:<7} {shape.classification:<26} {shape.visible_text:>8} "
                f"{shape.tables:>4} {shape.lists:>4} {shape.headings:>4} "
                f"{shape.scripts:>4} {shape.json_ld:>4} {shape.pdf_links:>4}  "
                f"{shape.institution[:34]}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
