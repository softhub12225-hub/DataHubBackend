"""Run document extraction over stored pilot evidence (Step 5C.1 section 22).

Usage::

    uv run python apps/api/scripts/extract_documents.py run
    uv run python apps/api/scripts/extract_documents.py report
    uv run python apps/api/scripts/extract_documents.py jsonld
    uv run python apps/api/scripts/extract_documents.py thin

**Offline.** This makes no network request: it reads bytes already in the evidence
store and writes derived documents to the artifact store. Nothing is fetched, no
JavaScript is executed, no LLM is involved, and no `field_claim` is created.

A manual command rather than a CI job, deliberately: the automated suite is
fixture-based, and turning 174 universities' markup into a test dependency would make
it fail when they redesign their sites (section 31).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.extraction.document import EXTRACTOR_VERSION, PDF_EXTRACTOR
from app.domains.extraction.runner import DERIVED_PREFIX, run_extraction


def _engine(role: DatabaseRole) -> Any:
    settings = get_settings()
    return create_engine(settings.database.sync_dsn(role), future=True)


def command_run(args: argparse.Namespace) -> int:
    engine = _engine(DatabaseRole.WORKER)
    evidence = FilesystemEvidenceStore(args.evidence_root)
    artifacts = FilesystemEvidenceStore(args.artifact_root, prefix=DERIVED_PREFIX)
    print(f"evidence   {args.evidence_root}")
    print(f"artifacts  {args.artifact_root} (prefix {DERIVED_PREFIX}/)")
    print(f"version    {EXTRACTOR_VERSION}")
    print("offline: no network request is made and no field_claim is created\n")

    report = run_extraction(engine, evidence=evidence, artifacts=artifacts, limit=args.limit)
    print(report.summary())
    if report.by_media:
        print("\n  by media type:")
        for media, count in sorted(report.by_media.items()):
            print(f"    {count:>4}  {media}")
    if report.failures:
        print(f"\n  failures ({len(report.failures)}):")
        for line in report.failures[:20]:
            print(f"    {line}")
    engine.dispose()
    return 0


def command_report(args: argparse.Namespace) -> int:
    """Section 22-23: totals and structural coverage, queried from the database."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        _totals(connection)
        _coverage(connection)
        _text_lengths(connection)
        _encoding(connection)
        _pdf(connection, args.artifact_root)
    engine.dispose()
    return 0


def _totals(connection: Connection) -> None:
    print("=" * 78)
    print("EXTRACTION TOTALS (section 22)")
    print("=" * 78)
    rows = connection.execute(
        text(
            "SELECT e.extractor_name, e.status::text AS status, count(*) AS n "
            "  FROM extraction e GROUP BY 1, 2 ORDER BY 1, 2"
        )
    ).all()
    by_extractor: dict[str, Counter[str]] = {}
    for row in rows:
        by_extractor.setdefault(row.extractor_name, Counter())[row.status] = row.n
    for name, counts in sorted(by_extractor.items()):
        total = sum(counts.values())
        print(f"\n  {name}  ({total} document(s))")
        for status in ("OK", "PARTIAL", "FAILED"):
            print(f"    {status:<10} {counts.get(status, 0)}")

    eligible = connection.execute(text("SELECT count(*) FROM snapshot")).scalar_one()
    extracted = connection.execute(
        text("SELECT count(DISTINCT snapshot_id) FROM extraction")
    ).scalar_one()
    sources = connection.execute(
        text(
            "SELECT count(DISTINCT sn.source_id) FROM extraction e "
            "  JOIN snapshot sn ON sn.id = e.snapshot_id"
        )
    ).scalar_one()
    print(f"\n  snapshots in the evidence plane   {eligible}")
    print(f"  snapshots extracted               {extracted}")
    print(f"  distinct sources extracted        {sources}")

    print("\n  warnings, by kind (what makes a PARTIAL partial):")
    kinds: Counter[str] = Counter()
    for row in connection.execute(
        text("SELECT warnings FROM extraction WHERE warnings IS NOT NULL")
    ):
        for warning in row.warnings.get("warnings", []):
            kinds[str(warning).split(":")[0].split("(")[0].strip()[:60]] += 1
    for kind, count in kinds.most_common(12):
        print(f"    {count:>4}  {kind}")


def _coverage(connection: Connection) -> None:
    print("\n" + "=" * 78)
    print("STRUCTURAL COVERAGE (section 23)")
    print("=" * 78)
    checks = (
        ("documents with headings", "(output->'statistics'->>'headings')::int > 0"),
        ("documents with lists", "(output->'statistics'->>'lists')::int > 0"),
        ("documents with tables", "(output->'statistics'->>'tables')::int > 0"),
        ("documents with JSON-LD", "(output->'statistics'->>'json_ld_blocks')::int > 0"),
        (
            "documents with embedded JSON",
            "(output->'statistics'->>'embedded_json_blocks')::int > 0",
        ),
        ("documents linking PDFs", "(output->'statistics'->>'pdf_links')::int > 0"),
        ("documents with iframes/embeds", "(output->'statistics'->>'embeds')::int > 0"),
        ("documents with a canonical link", "output->>'canonical_url' IS NOT NULL"),
        ("documents with a title", "output->>'title' IS NOT NULL"),
        ("documents with a language", "output->>'language' IS NOT NULL"),
        (
            "documents with usable visible text (>=400 chars)",
            "(output->'statistics'->>'text_characters')::int >= 400",
        ),
    )
    total = connection.execute(
        text("SELECT count(*) FROM extraction WHERE output IS NOT NULL")
    ).scalar_one()
    print(f"  documents with a stored representation  {total}\n")
    for label, predicate in checks:
        count = connection.execute(
            text(f"SELECT count(*) FROM extraction WHERE output IS NOT NULL AND {predicate}")  # noqa: S608
        ).scalar_one()
        share = f"{count / total * 100:.0f}%" if total else "-"
        print(f"  {label:<48} {count:>4}  ({share})")


def _text_lengths(connection: Connection) -> None:
    row = connection.execute(
        text(
            "SELECT min(chars) AS min, max(chars) AS max, "
            "       percentile_disc(0.5) WITHIN GROUP (ORDER BY chars) AS median, "
            "       round(avg(chars)) AS mean, sum(chars) AS total "
            "  FROM (SELECT (output->'statistics'->>'text_characters')::int AS chars "
            "          FROM extraction WHERE output IS NOT NULL) t"
        )
    ).one()
    print("\n  normalised text length, characters:")
    print(f"    min     {row.min:,}" if row.min is not None else "    min     -")
    print(f"    median  {row.median:,}" if row.median is not None else "    median  -")
    print(f"    mean    {int(row.mean):,}" if row.mean is not None else "    mean    -")
    print(f"    max     {row.max:,}" if row.max is not None else "    max     -")
    print(f"    total   {row.total:,}" if row.total is not None else "    total   -")

    artifact = connection.execute(
        text(
            "SELECT count(*) AS n, sum(document_byte_size) AS bytes, "
            "       count(DISTINCT document_hash) AS distinct_docs "
            "  FROM extraction WHERE document_hash IS NOT NULL"
        )
    ).one()
    print("\n  derived artifacts:")
    print(f"    rows pointing at one        {artifact.n}")
    print(f"    distinct artifacts          {artifact.distinct_docs}")
    print(f"    bytes                       {(artifact.bytes or 0):,}")


def _encoding(connection: Connection) -> None:
    print("\n  charset decisions (section 8):")
    for row in connection.execute(
        text(
            "SELECT output->'encoding'->>'used' AS used, "
            "       (output->'encoding'->>'fallback_used')::bool AS fallback, "
            "       count(*) AS n "
            "  FROM extraction WHERE output IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC"
        )
    ):
        flag = " (fallback)" if row.fallback else ""
        print(f"    {row.n:>4}  {row.used}{flag}")
    lossy = connection.execute(
        text(
            "SELECT count(*) FROM extraction WHERE output IS NOT NULL "
            "  AND (output->'encoding'->>'replacement_characters')::int > 0"
        )
    ).scalar_one()
    print(f"    documents with undecodable characters: {lossy}")


def _pdf(connection: Connection, artifact_root: Path) -> None:
    print("\n" + "=" * 78)
    print("PDF RESULT (section 25)")
    print("=" * 78)
    rows = connection.execute(
        text(
            "SELECT e.document_hash, e.status::text AS status, e.output, e.warnings, "
            "       s.url, e.document_byte_size "
            "  FROM extraction e JOIN snapshot sn ON sn.id = e.snapshot_id "
            "  JOIN source s ON s.id = sn.source_id "
            " WHERE e.extractor_name = :name"
        ),
        {"name": PDF_EXTRACTOR},
    ).all()
    if not rows:
        print("  No PDF was extracted.")
        return
    artifacts = FilesystemEvidenceStore(artifact_root, prefix=DERIVED_PREFIX)
    for row in rows:
        stats = row.output.get("statistics", {})
        print(f"  {row.url}")
        print(f"    status            {row.status}")
        print(f"    pages             {stats.get('pdf_pages', 0)}")
        print(f"    characters        {stats.get('text_characters', 0):,}")
        print(f"    blocks            {stats.get('blocks', 0)}")
        print(f"    links             {stats.get('links', 0)}")
        print(f"    title             {row.output.get('title')}")
        print(f"    artifact bytes    {row.document_byte_size:,}")
        metadata = row.output.get("metadata") or {}
        if metadata:
            print(f"    metadata          {json.dumps(metadata, ensure_ascii=False)[:200]}")
        if row.warnings:
            for warning in row.warnings.get("warnings", []):
                print(f"    warning           {warning}")
        else:
            print("    text layer        usable (no OCR needed, and none performed)")
        try:
            payload = json.loads(artifacts.get(row.document_hash))
        except (KeyError, OSError, ValueError):
            continue
        pages = [b for b in payload.get("blocks", []) if b.get("kind") == "pdf_page"]
        for page in pages[:2]:
            print(f"    page {page.get('level')}: {page.get('text', '')[:150]}")


def command_jsonld(args: argparse.Namespace) -> int:
    """Section 24: the @type inventory the real pages actually contain."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        print("=" * 78)
        print("JSON-LD @type INVENTORY (section 24)")
        print("=" * 78)
        pages = connection.execute(
            text(
                "SELECT count(*) FROM extraction WHERE output IS NOT NULL "
                "  AND jsonb_array_length(coalesce(output->'json_ld_types', '[]'::jsonb)) > 0"
            )
        ).scalar_one()
        print(f"  documents carrying at least one JSON-LD @type   {pages}\n")
        rows = connection.execute(
            text(
                "SELECT value AS type_name, count(*) AS documents "
                "  FROM extraction e, "
                "       jsonb_array_elements_text(coalesce(e.output->'json_ld_types', "
                "                                          '[]'::jsonb)) AS value "
                " WHERE e.output IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"
            )
        ).all()
        print(f"  {'@type':<34} documents")
        for row in rows:
            print(f"  {row.type_name[:34]:<34} {row.documents}")
        if not rows:
            print("  (none)")
    engine.dispose()
    return 0


def command_thin(args: argparse.Namespace) -> int:
    """Section 21: what the possible-browser pages actually yielded, offline."""
    engine = _engine(DatabaseRole.API)
    artifacts = FilesystemEvidenceStore(args.artifact_root, prefix=DERIVED_PREFIX)
    with engine.connect() as connection:
        print("=" * 78)
        print("LOW-TEXT PAGES: is a browser genuinely needed? (section 21)")
        print("=" * 78)
        rows = connection.execute(
            text(
                "SELECT s.url, coalesce(ti.match_key, '?') AS institution, "
                "       coalesce(t.physical_source_ref, '?') AS ref, "
                "       e.output, e.document_hash "
                "  FROM extraction e "
                "  JOIN snapshot sn ON sn.id = e.snapshot_id "
                "  JOIN source s ON s.id = sn.source_id "
                "  LEFT JOIN acquisition_target t ON t.source_id = s.id "
                "  LEFT JOIN target_institution ti ON ti.id = t.target_institution_id "
                " WHERE e.output IS NOT NULL "
                "   AND (e.output->'statistics'->>'text_characters')::int < :threshold "
                " ORDER BY (e.output->'statistics'->>'text_characters')::int"
            ),
            {"threshold": args.threshold},
        ).all()
        print(f"  documents under {args.threshold} characters of visible text: {len(rows)}\n")
        for row in rows:
            stats = row.output.get("statistics", {})
            print(f"  {row.institution[:44]}  {row.ref}")
            print(f"    {row.url[:92]}")
            print(f"    title            {row.output.get('title')}")
            print(f"    visible text     {stats.get('text_characters', 0)} characters")
            print(f"    scripts          {stats.get('script_elements', 0)}")
            print(
                f"    structured       {stats.get('json_ld_blocks', 0)} JSON-LD, "
                f"{stats.get('embedded_json_blocks', 0)} embedded JSON"
            )
            verdict, detail = _browser_verdict(artifacts, row.document_hash)
            print(f"    VERDICT          {verdict}")
            for line in detail:
                print(f"      {line}")
    engine.dispose()
    return 0


def _browser_verdict(
    artifacts: FilesystemEvidenceStore, document_hash: str | None
) -> tuple[str, list[str]]:
    """Whether the embedded JSON holds recoverable prose, or only a shell.

    Deliberately crude: it measures how much *text* is reachable inside the payload
    without interpreting any of it. The question is "could a parser get the words
    without a browser", not "what do the words say".
    """
    if not document_hash:
        return "UNKNOWN", ["no stored artifact"]
    try:
        payload = json.loads(artifacts.get(document_hash))
    except (KeyError, OSError, ValueError) as exc:
        return "UNKNOWN", [f"artifact unreadable: {exc}"]

    structured = payload.get("structured_data") or {}
    blobs = [*(structured.get("embedded_json") or []), *(structured.get("json_ld") or [])]
    if not blobs:
        return "BROWSER_LIKELY_REQUIRED", [
            "no embedded JSON and no JSON-LD: the initial response carries neither",
            "prose nor data, so the content arrives by script execution",
        ]

    strings: list[str] = []
    _collect_strings(blobs, strings)
    prose = [value for value in strings if len(value) >= 40 and " " in value]
    total = sum(len(value) for value in prose)
    detail = [
        f"{len(strings)} string(s) inside the payload, "
        f"{len(prose)} of them sentence-like ({total:,} characters)",
    ]
    if prose[:2]:
        for sample in prose[:2]:
            detail.append(f'sample: "{sample[:110]}"')
    if total >= 1000:
        return "RECOVERABLE_WITHOUT_A_BROWSER", detail
    if total > 0:
        return "PARTIALLY_RECOVERABLE", detail
    return "SHELL_ONLY_DATA", [*detail, "the payload is configuration, not content"]


def _collect_strings(value: object, out: list[str], depth: int = 0) -> None:
    if depth > 12 or len(out) > 20000:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, out, depth + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--evidence-root", type=Path, default=Path(".evidence-full"))
    parser.add_argument("--artifact-root", type=Path, default=Path(".artifacts-full"))
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="extract every body-bearing source")
    runner.add_argument("--limit", type=int, default=None)
    sub.add_parser("report", help="totals, coverage, charsets, PDF")
    sub.add_parser("jsonld", help="the @type inventory")
    thin = sub.add_parser("thin", help="low-text pages: is a browser needed?")
    thin.add_argument("--threshold", type=int, default=400)

    args = parser.parse_args(argv)
    commands = {
        "run": command_run,
        "report": command_report,
        "jsonld": command_jsonld,
        "thin": command_thin,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
