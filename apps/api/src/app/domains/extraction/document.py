"""The normalised document: what a page becomes before anyone reads it for meaning.

WHAT THIS IS
============
A structural representation of one fetched document, in **document order**, from which
Step 5C.2 can later derive field-level claims. It is not a fact, not a claim, and not
flattened into one string -- because "the fee table's third row" and "the paragraph
after the Entry Requirements heading" are both things a later extractor needs to be
able to say, and a single blob of text can say neither.

DETERMINISM IS THE WHOLE POINT
==============================
`canonical_bytes()` serialises a document to exactly one byte sequence for a given
input: sorted keys, no insignificant whitespace, UTF-8. Its sha256 is the artifact hash.

The document therefore contains **no ids and no timestamps**. Not because they are
uninteresting, but because including them would make the hash a function of *when* and
*where* extraction ran rather than of *what was parsed*, and the determinism test would
be untestable. Lineage lives on the `extraction` row -- `snapshot_id` and
`input_content_hash` -- which is where a join can follow it.

A consequence worth knowing: two sources serving byte-identical pages produce one
artifact, stored once. That is correct. The two extractions differ, and they differ in
the row, which is the thing that carries identity.

TEXT IS PRESERVED, NEVER REWRITTEN
==================================
Whitespace is collapsed and Unicode is NFC-normalised. Nothing is translated,
summarised, re-worded or inferred, and no number is turned into a business value. The
official source wording survives to the character, modulo whitespace -- because a
reviewer comparing our claim against the page has to be able to find the sentence.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import StrEnum

#: Bumped whenever the *output* of extraction changes for unchanged input. A new value
#: means a new `extraction` row may exist beside the old one, and the old one is kept:
#: comparing two versions over the same bytes is how extractor drift becomes visible
#: rather than being mistaken for a source change (D4).
#:
#: **2.0.0** (Step 5C.4). Three structural corrections, none of them cosmetic:
#:
#: 1. The pass that removes `<script>`, `<style>` and `<svg>` mutated the tree while
#:    iterating it, so `tree.iter()` skipped siblings and **22 of 29 scripts and 48 of
#:    49 SVGs survived** on one real page. Their text then reached paragraph blocks as
#:    ordinary prose -- the 30 JavaScript "admission requirements" Step 5C.3 found.
#: 2. `Link` records its structural container, its block, its ancestry and whether its
#:    block is nothing but itself, so a catalogue's programme link can be told from a
#:    site-wide course picker (section 2).
#: 3. `Block` records a `LinkProfile`, so a field-specific extractor can treat a
#:    link-only paragraph differently on a catalogue and on an entry-requirements page
#:    (section 3).
#:
#: A major bump rather than a minor one: every document hash changes, so every
#: extraction and every candidate derived from one is superseded.
EXTRACTOR_VERSION = "2.0.0"

HTML_EXTRACTOR = "html-document-normaliser"
PDF_EXTRACTOR = "pdf-document-normaliser"

#: The schema of the artifact itself, so a reader in two years knows what it is looking
#: at without guessing from the shape. Bumped to `/2` with the Step 5C.4 structural
#: fields, because a consumer written against `/1` will not find `link_profile` and
#: should be able to tell that from the artifact rather than from a stack trace.
DOCUMENT_SCHEMA = "normalized-document/2"

#: Above this share of anchor text, a block is a list of links rather than prose that
#: happens to contain one. Chosen from the corpus: a real requirement sentence that
#: links a term or two sits well below it, and a navigation run sits at or near 1.0.
#: It is a stated convention, not a discovered boundary -- and it is only ever a
#: *signal* to a field-specific rule, never a deletion.
LINK_DOMINATED_FRACTION = 0.8


class BlockKind(StrEnum):
    """What a block *is* structurally. Never what it means."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    PREFORMATTED = "preformatted"
    QUOTE = "quote"
    #: A PDF page's text, kept per page so page numbers survive.
    PDF_PAGE = "pdf_page"


def normalise_text(value: str) -> str:
    """Collapse whitespace and NFC-normalise. Nothing else.

    NFC because the same character can be spelled several ways in Unicode and two
    spellings of one university name should hash alike. Whitespace because HTML
    indentation is not content, while paragraph and list boundaries -- which *are*
    content -- are carried by the block structure rather than by newlines.
    """
    return unicodedata.normalize("NFC", " ".join(value.split()))


@dataclass(frozen=True, slots=True)
class LinkProfile:
    """How much of a block's text is anchor text (Step 5C.4 section 3).

    Recorded so a *field-specific* extractor can decide, which is the point: a
    programme catalogue legitimately lists its programmes as links, and an entry-
    requirements page's link labels are navigation. Deleting link-only content
    wholesale would lose the first to fix the second.

    Absent when the block contains no anchor, so "no profile" means "no links".
    """

    #: Anchors whose text contributes to this block.
    count: int
    #: Characters of anchor text over characters of block text, after normalisation.
    #: 0.0 when the block has text but no anchor text; 1.0 when it is entirely a link.
    #: Rounded to three places so the artifact hash cannot depend on float noise.
    text_fraction: float
    #: The block's whole text is exactly one anchor's text. The 146-row case.
    is_link_only: bool
    #: Most of the block is anchor text -- see `LINK_DOMINATED_FRACTION`.
    is_link_dominated: bool
    #: For a LIST block, how many of its items are exactly one anchor. A course
    #: catalogue's item is link-only while the list it belongs to is not, and the
    #: item is the unit a reader sees.
    link_only_items: int = 0


@dataclass(frozen=True, slots=True)
class Block:
    """One structural unit, in document order."""

    kind: BlockKind
    text: str = ""
    #: Heading depth 1-6, or a PDF page number. None for everything else.
    level: int | None = None
    #: List items, in order, for `LIST`.
    items: list[str] = field(default_factory=list)
    #: True when a `LIST` was `<ol>`.
    ordered: bool | None = None
    #: Index into `NormalizedDocument.tables` for `TABLE`.
    table_index: int | None = None
    #: Where this block sat in the tree, for a later extractor that wants to know
    #: whether text came from a nav or from the main column without that decision
    #: having been made for it here (section 6).
    container: str | None = None
    #: True when any ancestor was a `nav`, `header` or `footer`, whatever containers
    #: nest inside it. `container` records the innermost container and is therefore not
    #: enough on its own: a `<section>` inside a `<nav>` reports `section`, and a chrome
    #: guard reading only `container` would wave it through.
    in_chrome: bool = False
    #: How much of this block's text is anchor text. Absent when it contains no anchor.
    link_profile: LinkProfile | None = None


@dataclass(frozen=True, slots=True)
class TableCell:
    text: str
    header: bool = False
    rowspan: int = 1
    colspan: int = 1


@dataclass(frozen=True, slots=True)
class Table:
    """A table as rows of cells. Not interpreted as anything (section 11)."""

    caption: str | None
    #: Rows from `<thead>`, kept apart because a header row is the thing a later
    #: extractor keys on, and `<th>` is used inconsistently in the wild.
    header_rows: list[list[TableCell]] = field(default_factory=list)
    rows: list[list[TableCell]] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        candidates = [sum(cell.colspan for cell in row) for row in (*self.header_rows, *self.rows)]
        return max(candidates) if candidates else 0


@dataclass(frozen=True, slots=True)
class Link:
    """An anchor. Recorded, never followed (section 12)."""

    text: str
    href: str
    #: Resolved against the document's effective URL when that is safe. None when the
    #: href is a fragment, a `mailto:`, a `javascript:` or otherwise not a location.
    resolved: str | None = None
    rel: str | None = None
    #: True when the resolved target ends in `.pdf`. Inventory only -- nothing is
    #: enqueued and nothing is downloaded (section 14).
    is_pdf: bool = False
    #: Host of the resolved target, so off-site links are countable without parsing
    #: URLs again downstream.
    host: str | None = None

    # --- structural provenance (Step 5C.4 section 2) --------------------------------
    #: The semantic container this anchor sat in: `main`, `article`, `section`, `nav`,
    #: `header`, `footer`, `aside`, `form`, or None for a `<div>` soup with no semantic
    #: wrapper. **The reason this field exists**: without it a catalogue's programme
    #: link and a site-wide course picker are the same row, and 180 of 226 programme
    #: groups could not be confirmed as body content.
    container: str | None = None
    #: Index into `NormalizedDocument.blocks` of the block this anchor's text landed
    #: in, or None when the anchor sat outside every emitted block. The heading path is
    #: **derived** from this rather than duplicated here: two copies of a derived value
    #: are two things that can disagree.
    block_index: int | None = None
    #: Ancestor tag names, outermost first, up to the container, capped at
    #: `_MAX_ANCESTRY`. Tag names only -- no classes, no ids, no indices -- so it is
    #: stable across parses and across a restyle that does not restructure. Section 2
    #: forbids storing an unstable full DOM path, and section 5 forbids relying on CSS
    #: class names.
    ancestry: str | None = None
    #: True when any ancestor was a `nav`, `header` or `footer`. Sticky, for the same
    #: reason as `Block.in_chrome`: the innermost container alone is not enough.
    in_chrome: bool = False
    #: The block this anchor sits in is exactly this anchor's text. True for a nav
    #: entry, and for a link label rendered as a paragraph.
    is_link_only_block: bool = False
    #: The enclosing list item is exactly this anchor's text. True for a catalogue,
    #: where the block is the whole list and so the block-level flag is False. Both
    #: are recorded because they answer different questions, and section 3 wants a
    #: field-specific rule to tell them apart rather than deleting link-only content
    #: wholesale -- which would lose the catalogue to fix the navigation.
    is_link_only_item: bool = False


@dataclass(frozen=True, slots=True)
class Embed:
    """An `<iframe>`, `<embed>` or `<object>`. Its URL is recorded, never fetched."""

    tag: str
    src: str | None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredData:
    """Machine-readable blocks the page volunteered about itself (sections 9-10).

    Present is not the same as true. JSON-LD is a claim the page makes in a convenient
    format, not a more authoritative version of the page, and nothing here becomes a
    `field_claim` in this step.
    """

    #: Successfully parsed `application/ld+json` payloads, flattened out of `@graph`
    #: and arrays so a reader sees one list of objects.
    json_ld: list[object] = field(default_factory=list)
    #: `@type` values observed, sorted, for the inventory in section 24.
    json_ld_types: list[str] = field(default_factory=list)
    #: Other safely-identifiable JSON payloads: `application/json` script blocks,
    #: `__NEXT_DATA__`. No JavaScript is executed to obtain these.
    embedded_json: list[object] = field(default_factory=list)
    #: Script payloads seen and deliberately *not* parsed, by type, for future work.
    unparsed_payloads: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DocumentStatistics:
    """Counts a coverage report can use without re-walking the document."""

    text_characters: int = 0
    blocks: int = 0
    headings: int = 0
    paragraphs: int = 0
    lists: int = 0
    list_items: int = 0
    tables: int = 0
    links: int = 0
    pdf_links: int = 0
    embeds: int = 0
    json_ld_blocks: int = 0
    embedded_json_blocks: int = 0
    script_elements: int = 0
    pdf_pages: int = 0


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """One page, structurally. Deterministic for a given input and version."""

    schema: str
    extractor_name: str
    extractor_version: str
    media_type: str
    title: str | None = None
    language: str | None = None
    canonical_url: str | None = None
    meta_description: str | None = None
    #: The document's own metadata: `<meta name=...>` for HTML, the info dictionary
    #: for PDF. Verbatim, so a later extractor can use a field we did not anticipate.
    metadata: dict[str, str] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    embeds: list[Embed] = field(default_factory=list)
    structured_data: StructuredData = field(default_factory=StructuredData)
    encoding: dict[str, object] = field(default_factory=dict)
    statistics: DocumentStatistics = field(default_factory=DocumentStatistics)
    #: Non-fatal problems: a malformed JSON-LD block, a lossy decode, a PDF page with
    #: no text layer. Their presence is what makes an extraction PARTIAL rather than
    #: SUCCEEDED (section 17).
    warnings: list[str] = field(default_factory=list)

    def visible_text(self) -> str:
        """Every block's text, in order, joined by blank lines.

        A convenience for humans and for coverage statistics. It is derived *from* the
        structure rather than being the representation, which is the distinction
        section 5 asks for -- the blocks remain addressable.
        """
        parts: list[str] = []
        for block in self.blocks:
            if block.kind is BlockKind.LIST:
                parts.extend(block.items)
            elif block.text:
                parts.append(block.text)
        return "\n\n".join(parts)

    def to_json(self) -> dict[str, object]:
        """A plain dict, with enums as their values and no `None`-only noise."""
        cleaned = _clean(asdict(self))
        assert isinstance(cleaned, dict)  # _clean preserves the top-level mapping
        return cleaned

    def canonical_bytes(self) -> bytes:
        """The one byte sequence this document serialises to.

        Sorted keys and tight separators so the hash depends on content and not on
        dict ordering; `ensure_ascii=False` so the stored artifact is readable in the
        languages the sources are actually written in.
        """
        return json.dumps(
            self.to_json(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def document_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _clean(value: object) -> object:
    """Drop keys whose value is None or an empty container.

    Not cosmetic: it keeps the artifact stable when a later version adds an optional
    field that most documents do not have, so adding one does not rewrite every hash
    in the fleet for pages where it is absent.
    """
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            done = _clean(item)
            if done is None or done == [] or done == {}:
                continue
            cleaned[key] = done
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


__all__ = [
    "DOCUMENT_SCHEMA",
    "EXTRACTOR_VERSION",
    "HTML_EXTRACTOR",
    "LINK_DOMINATED_FRACTION",
    "PDF_EXTRACTOR",
    "Block",
    "BlockKind",
    "DocumentStatistics",
    "Embed",
    "Link",
    "LinkProfile",
    "NormalizedDocument",
    "StructuredData",
    "Table",
    "TableCell",
    "normalise_text",
]
