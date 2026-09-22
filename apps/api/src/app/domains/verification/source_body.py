"""Showing a reviewer the stored body of a page, so a decision rests on evidence.

WHY THIS EXISTS
===============
By this point a reviewer has been told a page's host is official, its access class, its
title and how many candidates it produced. None of that answers the question they are
actually being asked: *does this page carry this responsibility?* A page can be live,
titled, on a verified host and still be a news article, a redirect stub or a course
listing for a different degree level.

So this resolves the evidence chain and prints the normalised document. Nothing else.

WHAT IT RESOLVES, AND WHY EACH HOP IS NEEDED
============================================
``pilot_collected_source`` -> ``source`` -> latest ``snapshot`` -> current
``extraction`` -> normalised document artifact.

* **Latest snapshot**, by ``observed_at``: a page may have been fetched more than once,
  and the reviewer must see what is stored now rather than the first thing ever fetched.
* **Current extraction.** Two artifacts can exist for one snapshot because the document
  normaliser is versioned independently of the claim rules (D55). Showing a superseded
  artifact would show the reviewer text the current claim pass never saw, so the
  extraction is filtered to the published ``document_artifact_version`` and, among those,
  the most recent.
* **The artifact, not the raw HTML.** The raw bytes contain scripts, trackers and markup;
  the artifact is the structure Step 5C.1 derived, which is also exactly what the claim
  rules read. A reviewer judging a page should be looking at the same text the extractors
  did, or they are reviewing a different document.

STRICTLY READ-ONLY
==================
No INSERT, no UPDATE, no audit append, and no fetch. Looking at evidence is not an event.
A viewer that recorded "who looked at what" would write to an append-only chain on every
glance, and the chain exists for decisions.

DUPLICATE RESPONSIBILITIES
==========================
Two rows can claim different responsibilities of the same physical page. They share one
body, and the header states which responsibility is under review, because the body is
identical and the question is not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.domains.acquisition.storage import EvidenceStore
from app.domains.extraction.document import Block, BlockKind, NormalizedDocument
from app.domains.extraction.html_document import CHROME_CONTAINERS

#: Printed when there is no stored body. Greppable, and the same string in every case so
#: a caller can branch on it without parsing prose.
NOT_AVAILABLE = "BODY_EVIDENCE_NOT_AVAILABLE"


def looks_like_chrome(block: Block) -> bool:
    """Best-effort: is this block navigation, header or footer furniture?

    **Approximate, and it has to be.** `Block.in_chrome` is the reliable answer -- the
    normaliser computes it by tracking every ancestor -- but it is **not serialised into
    the stored artifact**: a stored block carries only `container`, `kind`, `level` and
    `text`. So a consumer reading an artifact back has only the *innermost* container,
    and `Block`'s own docstring warns why that is not enough: a `<section>` inside a
    `<nav>` reports `section`, and a guard reading only `container` waves it through.

    This therefore **under-filters** -- it can leave chrome in, and never removes real
    content. That asymmetry is deliberate: hiding a page's actual text from a reviewer
    deciding whether the page carries a responsibility would be a far worse failure than
    leaving a menu on screen. It is a reading convenience, never a claim about structure,
    and the caller must default to showing everything.
    """
    return (block.container or "") in CHROME_CONTAINERS


class SourceBodyError(RuntimeError):
    """The pilot source, or its evidence chain, cannot be resolved."""


@dataclass(frozen=True, slots=True)
class BodyEvidence:
    """Everything the header needs, and whether there is a body to print."""

    pilot_source_id: uuid.UUID
    source_ref: str
    institution: str
    responsibility: str
    degree_scope: str | None
    duplicate_of: str | None
    requested_url: str
    effective_url: str | None
    host: str
    host_status: str | None
    verification_state: str
    snapshot_id: uuid.UUID | None = None
    extraction_id: uuid.UUID | None = None
    document_artifact_version: str | None = None
    document_hash: str | None = None
    extraction_status: str | None = None
    media_type: str | None = None
    http_status: int | None = None
    fetch_status: str | None = None
    error_class: str | None = None
    access_class: str = NOT_AVAILABLE
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.document_hash is not None


def _access_class(
    *, has_evidence: bool, fetch_status: str | None, http_status: int | None, error: str | None
) -> str:
    """Which access class this page is in. Evidence availability, never trust.

    Deliberately the same ladder as `verification_manifest.classify_access`. Duplicated
    rather than imported because `scripts/` is operator tooling and `src/` must not
    depend on it; the two are asserted to agree in the tests.
    """
    if has_evidence:
        return "BODY_EVIDENCE"
    status = (fetch_status or "").upper()
    err = (error or "").upper()
    if status == "BLOCKED":
        return "BLOCKED"
    if status == "NAME_NOT_RESOLVED":
        return "DEAD_HOST"
    if status == "TIMEOUT":
        return "TIMEOUT"
    if not status:
        return "NO_ATTEMPT"
    if http_status in (404, 410):
        return "DEAD_NOT_FOUND"
    if "TLS" in err or "SSL" in err or "CERT" in err:
        return "TLS_FAILURE"
    return "OTHER_HTTP"


_RESOLVE_SQL = """
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (sn.source_id)
               sn.source_id, sn.id AS snapshot_id, sn.effective_url, sn.http_status,
               sn.content_type, sn.observed_at
          FROM snapshot sn ORDER BY sn.source_id, sn.observed_at DESC
    ),
    latest_run AS (
        SELECT DISTINCT ON (fr.source_id)
               fr.source_id, fr.status AS fetch_status, fr.error_class, fr.http_status,
               fr.effective_url
          FROM fetch_run fr ORDER BY fr.source_id, fr.started_at DESC
    ),
    current_extraction AS (
        -- The current artifact for that snapshot: a published document version, and
        -- among those the newest. Both halves matter -- see the module docstring.
        SELECT DISTINCT ON (e.snapshot_id)
               e.snapshot_id, e.id AS extraction_id, e.status::text AS extraction_status,
               e.extractor_version AS document_artifact_version, e.document_hash,
               e.output->>'media_type' AS media_type, e.warnings
          FROM extraction e
          JOIN document_artifact_version dav
            ON dav.extractor_name = e.extractor_name AND dav.version = e.extractor_version
         WHERE e.document_hash IS NOT NULL
         ORDER BY e.snapshot_id, e.recorded_at DESC
    )
    SELECT pcs.id, pcs.source_ref, pcs.source_type AS responsibility,
           pcs.degree_scope::text AS degree_scope, pcs.duplicate_of_source_ref,
           pcs.official_url, pcs.host, pcs.verification_state::text AS verification_state,
           coalesce(ti.match_key, '?') AS institution,
           od.verification_status::text AS host_status,
           ls.snapshot_id, coalesce(ls.effective_url, lr.effective_url) AS effective_url,
           coalesce(ls.http_status, lr.http_status) AS http_status,
           lr.fetch_status, lr.error_class,
           ce.extraction_id, ce.extraction_status, ce.document_artifact_version,
           ce.document_hash, ce.media_type, ce.warnings,
           (ls.snapshot_id IS NOT NULL AND ce.document_hash IS NOT NULL) AS has_evidence
      FROM pilot_collected_source pcs
      JOIN source s ON s.id = pcs.acquisition_source_id
      LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
      LEFT JOIN official_domain od ON od.host = pcs.host
      LEFT JOIN latest_snapshot ls ON ls.source_id = s.id
      LEFT JOIN latest_run lr ON lr.source_id = s.id
      LEFT JOIN current_extraction ce ON ce.snapshot_id = ls.snapshot_id
     WHERE pcs.id = :pilot_source
"""


def resolve(connection: Connection, pilot_source_id: uuid.UUID) -> BodyEvidence:
    """Walk the evidence chain for one pilot-source responsibility row. Reads only."""
    row = connection.execute(text(_RESOLVE_SQL), {"pilot_source": pilot_source_id}).one_or_none()
    if row is None:
        raise SourceBodyError(
            f"no pilot_collected_source {pilot_source_id}. The id comes from the review "
            "packet; it is not a source_ref."
        )
    warnings = list(row.warnings or []) if row.warnings else []
    return BodyEvidence(
        pilot_source_id=row.id,
        source_ref=row.source_ref,
        institution=row.institution,
        responsibility=row.responsibility,
        degree_scope=row.degree_scope,
        duplicate_of=row.duplicate_of_source_ref,
        requested_url=row.official_url,
        effective_url=row.effective_url,
        host=row.host,
        host_status=row.host_status,
        verification_state=row.verification_state,
        snapshot_id=row.snapshot_id,
        extraction_id=row.extraction_id,
        document_artifact_version=row.document_artifact_version,
        document_hash=row.document_hash,
        extraction_status=row.extraction_status,
        media_type=row.media_type,
        http_status=row.http_status,
        fetch_status=row.fetch_status,
        error_class=row.error_class,
        access_class=_access_class(
            has_evidence=bool(row.has_evidence),
            fetch_status=row.fetch_status,
            http_status=row.http_status,
            error=row.error_class,
        ),
        warnings=warnings,
    )


def _table_lines(document: NormalizedDocument, index: int) -> list[str]:
    """One table as aligned text. Structure preserved; nothing interpreted."""
    if index >= len(document.tables):
        return ["    [table index out of range]"]
    table = document.tables[index]
    lines: list[str] = []
    if table.caption:
        lines.append(f"    caption: {table.caption}")
    rendered = [[cell.text for cell in row] for row in (*table.header_rows, *table.rows)]
    if not rendered:
        return [*lines, "    [empty table]"]
    widths: dict[int, int] = {}
    for row in rendered:
        for position, value in enumerate(row):
            widths[position] = max(widths.get(position, 0), min(len(value), 40))
    header_count = len(table.header_rows)
    for number, row in enumerate(rendered):
        cells = [value[:40].ljust(widths.get(position, 0)) for position, value in enumerate(row)]
        lines.append("    | " + " | ".join(cells).rstrip() + " |")
        if header_count and number == header_count - 1:
            lines.append("    |" + "-" * (sum(widths.values()) + 3 * len(widths)) + "|")
    return lines


def render(document: NormalizedDocument, *, max_blocks: int = 0, skip_chrome: bool = False) -> str:
    """The document as readable text, keeping the structure a reviewer needs.

    Headings, paragraphs, lists and tables survive as themselves, because "is this a
    fees table or a news item?" is answered by shape as much as by words. Scripts,
    tracking payloads and raw markup are simply not in the artifact -- Step 5C.1 dropped
    them -- so there is nothing here to strip.

    `max_blocks` truncates long pages and says so. It never silently stops.

    `skip_chrome` hides blocks whose innermost container is a nav, header or footer. It
    defaults to **off**: see `looks_like_chrome` for why the signal is approximate, and
    note that a reviewer judging trust should see the whole document unless they ask
    otherwise. When on, the count hidden is printed, so nothing disappears in silence.
    """
    lines: list[str] = []
    if document.title:
        lines += [f"TITLE: {document.title}", ""]
    if document.meta_description:
        lines += [f"DESCRIPTION: {document.meta_description}", ""]

    blocks = document.blocks
    hidden = 0
    if skip_chrome:
        kept = [block for block in blocks if not looks_like_chrome(block)]
        hidden = len(blocks) - len(kept)
        blocks = kept
        lines += [
            f"[--skip-chrome: {hidden} nav/header/footer block(s) hidden, "
            f"{len(blocks)} shown. The signal is approximate and under-filters; it "
            "never hides real content. Omit the flag for the whole document.]",
            "",
        ]
    shown = blocks if max_blocks <= 0 else blocks[:max_blocks]
    for block in shown:
        if block.kind is BlockKind.HEADING:
            level = block.level or 1
            lines += ["", f"{'#' * min(level, 6)} {block.text}", ""]
        elif block.kind is BlockKind.PARAGRAPH:
            if block.text:
                lines.append(block.text)
                lines.append("")
        elif block.kind is BlockKind.LIST:
            for position, item in enumerate(block.items, 1):
                marker = f"{position}." if block.ordered else "-"
                lines.append(f"  {marker} {item}")
            lines.append("")
        elif block.kind is BlockKind.TABLE:
            lines.append("  [TABLE]")
            lines += _table_lines(document, block.table_index or 0)
            lines.append("")
        elif block.kind is BlockKind.PDF_PAGE:
            lines += ["", f"--- PDF page {block.level} ---", block.text, ""]
        elif block.kind in (BlockKind.PREFORMATTED, BlockKind.QUOTE):
            prefix = "    " if block.kind is BlockKind.PREFORMATTED else "  > "
            lines += [prefix + piece for piece in block.text.splitlines()] or [prefix]
            lines.append("")

    if max_blocks > 0 and len(blocks) > max_blocks:
        lines += [
            "",
            f"[truncated: showing {max_blocks} of {len(blocks)} blocks. "
            "Re-run with --max-blocks 0 for the whole document.]",
        ]
    return "\n".join(lines).strip() + "\n"


def load(store: EvidenceStore, evidence: BodyEvidence) -> NormalizedDocument:
    """Rehydrate the artifact this row's current extraction produced."""
    if evidence.document_hash is None:
        raise SourceBodyError(f"{NOT_AVAILABLE}: {evidence.source_ref} has no stored body")
    from app.domains.claims.runner import load_document

    return load_document(store, evidence.document_hash)


__all__ = [
    "NOT_AVAILABLE",
    "BodyEvidence",
    "SourceBodyError",
    "load",
    "looks_like_chrome",
    "render",
    "resolve",
]
