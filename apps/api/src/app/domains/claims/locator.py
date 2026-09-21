"""Where a candidate came from, precisely (Step 5C.2 section 3).

`extraction_id` alone is not provenance. It says "somewhere in this 40,000-character
page", and a reviewer asked to confirm a £38,000 fee against that is being asked to
re-read the page — which is the work the extractor was supposed to have done.

A locator names the exact region. The shape differs by what produced the candidate:

* an HTML block — its index in document order, plus the heading path above it;
* a table cell — table index, row, column, and whether the row was a header;
* a list item — the block index and the item's position;
* a PDF page — the page number and the block;
* a JSON-LD value — the path through the parsed object.

All of them carry `heading_path` where one exists, because "under the heading *English
language requirements*" is the context a human uses to judge whether a number means
what the extractor thinks it means (section 21).

Locators are **deterministic**: the same document produces the same locator for the
same region, so a claim fingerprint built from one is stable across runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Locator:
    """A resolvable pointer into a normalised document."""

    #: What kind of region this is: `block`, `list_item`, `table_cell`, `pdf_page`,
    #: `json_ld`. Present so a consumer can branch without guessing from which keys
    #: happen to be set.
    kind: str
    #: Index into `NormalizedDocument.blocks`, in document order. The spine of every
    #: HTML and PDF locator.
    block_index: int | None = None
    #: Headings above this block, outermost first. Contextual evidence, not decoration.
    heading_path: list[str] = field(default_factory=list)
    #: Position within a list block.
    list_item_index: int | None = None
    #: Index into `NormalizedDocument.tables`, plus the cell.
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    is_header_row: bool | None = None
    #: 1-based PDF page, mirroring `Block.level` for a `pdf_page` block.
    pdf_page: int | None = None
    #: Dotted path into a parsed JSON-LD object, e.g. `0.offers.price`.
    json_ld_path: str | None = None
    #: Index into `NormalizedDocument.links`, for a claim taken from anchor text.
    #:
    #: Its own field rather than a `json_ld_path` of `"links.192"`. That is what the
    #: first version did, and it was wrong twice: a link is not JSON-LD, and `resolve`
    #: had no branch for it, so 37 of every 200 locators sampled did not resolve at
    #: all -- a pointer nobody can follow, which is what section 3 exists to prevent.
    link_index: int | None = None
    #: Character range within the block's text, when the candidate came from part of a
    #: sentence rather than the whole block.
    char_start: int | None = None
    char_end: int | None = None

    def as_json(self) -> dict[str, Any]:
        """Only the keys that are set, so the stored locator is readable.

        Sorted, because the fingerprint is computed over this and dict ordering must
        not make an identical region hash two ways.
        """
        payload: dict[str, Any] = {"kind": self.kind}
        for name in (
            "block_index",
            "list_item_index",
            "table_index",
            "row_index",
            "column_index",
            "is_header_row",
            "pdf_page",
            "json_ld_path",
            "link_index",
            "char_start",
            "char_end",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.heading_path:
            payload["heading_path"] = list(self.heading_path)
        return dict(sorted(payload.items()))


def heading_path_for(blocks: list[Any], index: int) -> list[str]:
    """The headings above `blocks[index]`, outermost first.

    Walks backwards keeping a heading only when it is *shallower* than the shallowest
    kept so far, which is what reconstructs a path rather than a list of every heading
    that happened to precede the block. `h1 Fees` → `h2 International` → `h3 2027` is
    a path; the `h3 2026` before it is not part of it.

    **When the subject is itself a heading, its own level seeds the walk.** Otherwise
    the first heading met going backwards is kept unconditionally, and for a heading
    that is its SIBLING rather than its ancestor: Cambridge's programme list gave
    "Archaeology, BA (Hons)" the path `[..., 'A', 'Anglo-Saxon, Norse, and Celtic, BA
    (Hons)']`, naming the programme listed above it as a parent section. For a content
    block the old behaviour is right, because the nearest preceding heading really is
    the section it sits in.
    """
    path: list[tuple[int, str]] = []
    shallowest: int | None = None

    subject = blocks[index] if 0 <= index < len(blocks) else None
    if subject is not None and getattr(getattr(subject, "kind", None), "value", "") == "heading":
        shallowest = subject.level or 6
    for candidate in range(index - 1, -1, -1):
        block = blocks[candidate]
        if getattr(block, "kind", None) is None or block.kind.value != "heading":
            continue
        level = block.level or 6
        if shallowest is None or level < shallowest:
            path.append((level, block.text))
            shallowest = level
        if level == 1:
            break
    return [text for _, text in reversed(path)]


def resolve(document: Any, locator: dict[str, Any]) -> str | None:
    """The text a locator points at, or None if it does not resolve.

    Used by the lineage proof (section 34) to show the pointer really lands on the
    wording the claim quotes -- a locator nobody can follow is decoration.
    """
    kind = locator.get("kind")
    blocks = document.blocks
    tables = document.tables

    if kind == "table_cell":
        table_index = locator.get("table_index")
        if table_index is None or table_index >= len(tables):
            return None
        table = tables[table_index]
        rows = table.header_rows if locator.get("is_header_row") else table.rows
        row_index = locator.get("row_index")
        column_index = locator.get("column_index")
        if row_index is None or row_index >= len(rows):
            return None
        row = rows[row_index]
        if column_index is None or column_index >= len(row):
            return None
        return str(row[column_index].text)

    if kind == "link":
        link_index = locator.get("link_index")
        links = document.links
        if link_index is None or link_index >= len(links):
            return None
        return str(links[link_index].text)

    block_index = locator.get("block_index")
    if block_index is None or block_index >= len(blocks):
        return None
    block = blocks[block_index]

    if kind == "list_item":
        item_index = locator.get("list_item_index")
        if item_index is None or item_index >= len(block.items):
            return None
        return str(block.items[item_index])

    text = str(block.text)
    start, end = locator.get("char_start"), locator.get("char_end")
    if start is not None and end is not None:
        return text[start:end]
    return text


__all__ = ["Locator", "heading_path_for", "resolve"]
