"""Deterministic static HTML normalisation (Step 5C.1 sections 5-12).

NO JAVASCRIPT, NO NETWORK
=========================
`no_network=True` is passed explicitly. It is lxml's default, and stating it is the
point: we parse 174 pages of untrusted bytes, and a parser that resolved an external
reference would be *making an HTTP request*, which section 18 forbids and which would
also be a way out of the offline sandbox.

`resolve_entities` is deliberately **not** passed -- it is an `XMLParser` option, and
`HTMLParser` has no XML entity mechanism to exploit in the first place. Claiming it
here would be describing a protection that is not the one actually doing the work.

Nothing is executed, no `src` is fetched, no iframe is followed, and
`test_the_parser_makes_no_network_call` holds a socket-level guard over the whole
parse to prove it rather than asserting it.

CONSERVATIVE ABOUT NAVIGATION
=============================
Scripts and styles are dropped, because executable code is not content. `nav`,
`header`, `footer` and `aside` are **kept**, because universities put fee tables in
asides and application deadlines in footers, and a parser that deleted them because a
CSS class looked like navigation would silently lose the thing we came for.

Instead of deleting, each block records the `container` it came from. A later extractor
that wants to prefer the main column can; the decision is left to the step that knows
what it is looking for, and the raw bytes remain regardless (section 6).

WHAT THE DROP PASS USED TO DO
=============================
It removed `<script>`, `<style>`, `<svg>`, `<template>` and `<math>` **while iterating
the tree**:

    for element in tree.iter():
        if element.tag in DROPPED_TAGS:
            _drop(element)

`tree.iter()` is a lazy document-order generator, and removing the element it is
standing on invalidates its position, so the next siblings are skipped. On
`grad.uchicago.edu/admissions/` that left **22 of 29 `<script>` elements and 48 of 49
`<svg>` elements in the tree**, and their text then reached paragraph blocks as ordinary
prose. That is where Step 5C.3's 30 JavaScript "admission requirements" came from --
`window.dataLayer = ...`, `$(document).ready(...)`, a Drupal settings blob -- and the
fix is to collect the doomed elements first and remove them afterwards.

Section 4 is right that removing the nodes is not sufficient on its own: the *order*
matters too. Structured data is read **before** the drop, so a JSON-LD or
`application/json` payload is still preserved in `structured_data`; what changes is that
its source text can no longer also appear as prose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from app.domains.extraction.charset import DecodedText, decode_document
from app.domains.extraction.document import (
    DOCUMENT_SCHEMA,
    EXTRACTOR_VERSION,
    HTML_EXTRACTOR,
    LINK_DOMINATED_FRACTION,
    Block,
    BlockKind,
    DocumentStatistics,
    Embed,
    Link,
    LinkProfile,
    NormalizedDocument,
    StructuredData,
    Table,
    TableCell,
    normalise_text,
)

#: Executable or presentational. Removed entirely -- their text is not document text.
DROPPED_TAGS = frozenset({"script", "style", "template", "svg", "math"})

#: Kept, but recorded, because their content is sometimes load-bearing (section 6).
#: `section` joins them in v2 (Step 5C.4 section 2): a `<section>` is the page's own
#: statement that a run of content belongs together, and it is the difference between
#: "a link in the body somewhere" and "a link in a catalogue section".
CONTAINER_TAGS = (
    "main",
    "article",
    "section",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
)

#: Containers the page itself labels as its own content, as opposed to furniture. Used
#: by Step 5C.4's extractors to decide whether a link may become a candidate; kept here
#: because it is a statement about the document model, not about any one field.
BODY_CONTAINERS = frozenset({"main", "article", "section"})

#: Furniture. Never deleted -- universities put fee tables in asides and deadlines in
#: footers (D45) -- but a business rule should not read a fact out of one.
CHROME_CONTAINERS = frozenset({"nav", "header", "footer"})

#: How many ancestor tag names a link's `ancestry` records. Enough to see
#: `main/div/ul/li` and distinguish a list item from a bare paragraph; short enough that
#: it stays readable and cannot encode a whole document's shape.
_MAX_ANCESTRY = 6

BLOCK_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "dl",
        "table",
        "pre",
        "blockquote",
    }
)

#: `<noscript>` usually duplicates content for non-JS clients, which is us. Its text is
#: kept but marked, so a later extractor can deduplicate if it sees the same sentence
#: twice rather than us guessing now which copy is canonical.
NOSCRIPT = "noscript"

#: Metadata, not body content. `<head>` is read by `_title`, `_language`, `_canonical`
#: and `_metadata`; walking into it as well emitted the page title a second time as a
#: paragraph, which duplicated it into the visible text and inflated every character
#: count. Caught by `test_the_document_keeps_its_structure_in_order`.
METADATA_TAGS = frozenset({"head", "title", "meta", "link", "base"})

_JSON_SCRIPT_TYPES = frozenset({"application/json", "application/ld+json"})
_JSON_LD_TYPE = "application/ld+json"
#: Script ids that reliably hold a serialised data payload rather than code. Matching
#: on an exact id, not a pattern: a heuristic here would start executing judgement on
#: arbitrary script bodies, which is how a parser becomes a JavaScript interpreter.
_JSON_SCRIPT_IDS = frozenset({"__NEXT_DATA__", "__NUXT_DATA__"})

_MAX_JSON_BYTES = 4 * 1024 * 1024
#: Anchor text longer than this is a paragraph that happens to be a link; the text is
#: kept in the block structure, and the link inventory stores a truncated label.
_MAX_LINK_TEXT = 300


def _parser() -> html.HTMLParser:
    """A recovery parser that cannot reach the network.

    `recover=True` because real HTML is malformed and the alternative is losing the
    page. `huge_tree=False` keeps lxml's own limits on absurd nesting, which is the
    parser-level equivalent of the fetcher's byte cap: stored evidence is untrusted
    input, and a hostile document should exhaust a limit rather than the machine.
    """
    return html.HTMLParser(
        recover=True,
        no_network=True,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse_html_document(
    payload: bytes,
    *,
    content_type: str | None = None,
    effective_url: str | None = None,
) -> NormalizedDocument:
    """Normalise one HTML document. Never raises for malformed markup.

    Malformed *markup* is recovered from, because real pages are malformed and refusing
    them would lose the fleet. Malformed *structured data* is a warning, because it is
    optional (section 9). Only an undecodable document or an unparseable tree is a
    failure, and the caller turns that into a `FAILED` extraction.
    """
    decoded = decode_document(payload, content_type=content_type)
    warnings: list[str] = []
    if decoded.fallback_used:
        warnings.append(
            f"charset fallback to {decoded.used}"
            f" (http={decoded.declared_http}, document={decoded.declared_document})"
        )
    if decoded.lossy:
        warnings.append(f"{decoded.replacement_characters} character(s) could not be decoded")

    tree = _build_tree(decoded, payload)

    # Counted before the drop, so "this page had 19 scripts" survives the fact that we
    # then removed them.
    script_count = len(tree.findall(".//script"))
    structured = _structured_data(tree, warnings)

    # Collected first, removed afterwards. Removing an element while `tree.iter()` is
    # standing on it invalidates the iterator's position and skips the next siblings:
    # on one real page that left 22 of 29 scripts and 48 of 49 SVGs in the tree, and
    # their source text reached paragraph blocks as prose (Step 5C.4 section 4).
    doomed = [
        element
        for element in tree.iter()
        if isinstance(element.tag, str) and element.tag.lower() in DROPPED_TAGS
    ]
    for element in doomed:
        _drop(element)

    blocks: list[Block] = []
    tables: list[Table] = []
    anchors: list[_Anchor] = []
    _walk(tree, blocks, tables, anchors, container=None)

    links = _links(tree, anchors, effective_url)
    embeds = _embeds(tree)
    metadata = _metadata(tree)

    document = NormalizedDocument(
        schema=DOCUMENT_SCHEMA,
        extractor_name=HTML_EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
        media_type="text/html",
        title=_title(tree),
        language=_language(tree),
        canonical_url=_canonical(tree, effective_url),
        meta_description=metadata.get("description"),
        metadata=metadata,
        blocks=blocks,
        tables=tables,
        links=links,
        embeds=embeds,
        structured_data=structured,
        encoding=decoded.as_metadata(),
        warnings=warnings,
    )
    return _with_statistics(document, script_count=script_count)


def _build_tree(decoded: DecodedText, payload: bytes) -> html.HtmlElement:
    """Parse to a tree, from text when possible and from bytes as a fallback.

    Parsing the decoded *text* is preferred: we have already decided what the bytes
    mean, and letting lxml re-decide could disagree with the recorded `encoding`
    metadata. Some documents carry an XML declaration that forbids parsing a `str`, so
    those fall back to the bytes.
    """
    try:
        tree = html.document_fromstring(decoded.text, parser=_parser())
    except (ValueError, etree.ParserError):
        tree = html.document_fromstring(payload, parser=_parser())
    return tree


def _drop(element: html.HtmlElement) -> None:
    """Remove an element and its subtree, keeping any tail text **and a word boundary**.

    The tail matters: `<p>see <script>x</script> below</p>` must not lose " below".

    The boundary matters more. Splicing the tail straight onto the preceding text welds
    the words on either side of the removed element together, so
    `<p>real text<script>payload</script>more real text</p>` became
    `"real textmore real text"` -- a token that appears nowhere on the page and that
    every downstream pattern then has to cope with. A space is inserted instead.

    This deliberately differs from browser rendering, where an invisible element implies
    no whitespace and "ab" is correct for `a<script>x</script>b`. For extraction a
    welded token is the worse error: `normalise_text` collapses runs of whitespace, so
    an extra space costs nothing, while an invented word costs a missed match.
    """
    tail = element.tail
    parent = element.getparent()
    if parent is None:
        return
    if tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = _joined(previous.tail, tail)
        else:
            parent.text = _joined(parent.text, tail)
    elif (previous := element.getprevious()) is not None:
        previous.tail = _joined(previous.tail, " ")
    else:
        parent.text = _joined(parent.text, " ")
    parent.remove(element)


def _joined(before: str | None, after: str) -> str:
    """Concatenate two runs of text without welding the words at the seam."""
    left = before or ""
    if left and not left[-1].isspace() and not after[:1].isspace():
        return left + " " + after
    return left + after


def _text_of(element: html.HtmlElement) -> str:
    return normalise_text(element.text_content())


#: Tags that are a content unit inside a block: a list item, a definition term or a
#: definition body. A catalogue's programme link is link-only within one of these even
#: though the list around it is not.
_ITEM_TAGS = ("li", "dt", "dd")


def _is_link_only_unit(element: html.HtmlElement) -> bool:
    """Is this content unit's entire text one anchor's text?"""
    anchors = element.findall(".//a")
    if len(anchors) != 1:
        return False
    text = normalise_text(element.text_content())
    return bool(text) and text == normalise_text(anchors[0].text_content())


@dataclass(frozen=True, slots=True)
class _Anchor:
    """One anchor, with where it sat. Internal -- becomes a `Link`."""

    element: html.HtmlElement
    container: str | None
    in_chrome: bool
    block_index: int | None
    ancestry: str | None
    is_link_only_block: bool
    is_link_only_item: bool


def _ancestry_of(element: html.HtmlElement) -> str | None:
    """Ancestor tag names, outermost first, capped at `_MAX_ANCESTRY`.

    Tag names only. Section 2 forbids an unstable full DOM path and section 5 forbids
    leaning on CSS class names, so nothing here reads an attribute: two parses of one
    document give the same string, and so does the same page after a restyle that does
    not restructure it.
    """
    chain: list[str] = []
    node = element.getparent()
    while node is not None and isinstance(node.tag, str):
        tag = node.tag.lower()
        if tag in ("html", "body"):
            break
        chain.append(tag)
        node = node.getparent()
    if not chain:
        return None
    return "/".join(reversed(chain[:_MAX_ANCESTRY]))


def _link_profile(element: html.HtmlElement, text: str) -> LinkProfile | None:
    """How much of `text` is anchor text. None when the element holds no anchor.

    The denominator is the block's own normalised text, so the fraction is comparable
    across blocks of any length. Rounded to three places because the artifact hash is
    computed over this value and a float's last bits must not decide it.
    """
    anchors = element.findall(".//a")
    if not anchors:
        return None
    total = len(text)
    linked = sum(len(normalise_text(anchor.text_content())) for anchor in anchors)
    fraction = round(min(linked, total) / total, 3) if total else 0.0
    single = normalise_text(anchors[0].text_content()) if len(anchors) == 1 else ""
    return LinkProfile(
        count=len(anchors),
        text_fraction=fraction,
        is_link_only=bool(single) and single == text,
        is_link_dominated=fraction >= LINK_DOMINATED_FRACTION,
        link_only_items=sum(1 for item in element.iter(*_ITEM_TAGS) if _is_link_only_unit(item)),
    )


def _collect_anchors(
    element: html.HtmlElement,
    anchors: list[_Anchor],
    *,
    container: str | None,
    in_chrome: bool,
    block_index: int | None,
    profile: LinkProfile | None,
) -> None:
    """Record every anchor inside an emitted block, with that block's provenance."""
    for anchor in element.findall(".//a"):
        anchors.append(
            _Anchor(
                element=anchor,
                container=container,
                in_chrome=in_chrome,
                block_index=block_index,
                ancestry=_ancestry_of(anchor),
                is_link_only_block=bool(profile and profile.is_link_only),
                is_link_only_item=_enclosing_item_is_link_only(anchor, element),
            )
        )


def _enclosing_item_is_link_only(anchor: html.HtmlElement, block: html.HtmlElement) -> bool:
    """Is the list item around this anchor nothing but this anchor?

    Walks up only as far as the block, so an anchor outside any list item answers
    False rather than borrowing an ancestor from further up the page.
    """
    node = anchor.getparent()
    while node is not None and node is not block:
        if isinstance(node.tag, str) and node.tag.lower() in _ITEM_TAGS:
            return _is_link_only_unit(node)
        node = node.getparent()
    return False


def _walk(
    element: html.HtmlElement,
    blocks: list[Block],
    tables: list[Table],
    anchors: list[_Anchor],
    *,
    container: str | None,
    in_chrome: bool = False,
) -> None:
    """Emit blocks in document order, descending only where needed.

    A block tag's subtree is consumed by the block, so nested paragraphs inside a
    table cell do not also appear as top-level paragraphs -- which would duplicate the
    text and make character counts meaningless.
    """
    for child in element:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag in METADATA_TAGS:
            continue
        here = tag if tag in CONTAINER_TAGS else container
        # Sticky: a `<section>` inside a `<nav>` is still navigation, and `here` would
        # say `section`. Once inside chrome, always inside chrome.
        chrome_here = in_chrome or tag in CHROME_CONTAINERS

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _text_of(child)
            if text:
                profile = _link_profile(child, text)
                blocks.append(
                    Block(
                        kind=BlockKind.HEADING,
                        text=text,
                        level=int(tag[1]),
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag == "p":
            text = _text_of(child)
            if text:
                profile = _link_profile(child, text)
                blocks.append(
                    Block(
                        kind=BlockKind.PARAGRAPH,
                        text=text,
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag in ("ul", "ol"):
            items = [
                normalise_text(item.text_content())
                for item in child.findall("./li")
                if normalise_text(item.text_content())
            ]
            if items:
                profile = _link_profile(child, " ".join(items))
                blocks.append(
                    Block(
                        kind=BlockKind.LIST,
                        items=items,
                        ordered=tag == "ol",
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag == "dl":
            items = [
                normalise_text(item.text_content())
                for item in child.findall("./dt") + child.findall("./dd")
                if normalise_text(item.text_content())
            ]
            if items:
                profile = _link_profile(child, " ".join(items))
                blocks.append(
                    Block(
                        kind=BlockKind.LIST,
                        items=items,
                        ordered=False,
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag == "table":
            table = _table(child)
            if table.rows or table.header_rows:
                tables.append(table)
                profile = _link_profile(child, _text_of(child))
                blocks.append(
                    Block(
                        kind=BlockKind.TABLE,
                        table_index=len(tables) - 1,
                        text=table.caption or "",
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag == "pre":
            # Whitespace is meaningful here, so only the Unicode form is normalised.
            raw = child.text_content()
            if raw.strip():
                blocks.append(
                    Block(
                        kind=BlockKind.PREFORMATTED,
                        text=normalise_text(raw),
                        container=here,
                        in_chrome=chrome_here,
                    )
                )
            continue
        if tag == "blockquote":
            text = _text_of(child)
            if text:
                profile = _link_profile(child, text)
                blocks.append(
                    Block(
                        kind=BlockKind.QUOTE,
                        text=text,
                        container=here,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=here,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue
        if tag == NOSCRIPT:
            text = _text_of(child)
            if text:
                profile = _link_profile(child, text)
                blocks.append(
                    Block(
                        kind=BlockKind.PARAGRAPH,
                        text=text,
                        container=NOSCRIPT,
                        in_chrome=chrome_here,
                        link_profile=profile,
                    )
                )
                _collect_anchors(
                    child,
                    anchors,
                    container=NOSCRIPT,
                    in_chrome=chrome_here,
                    block_index=len(blocks) - 1,
                    profile=profile,
                )
            continue

        _walk(child, blocks, tables, anchors, container=here, in_chrome=chrome_here)

    # Text sitting directly in a non-block container -- a `<div>` with a bare sentence
    # in it, which is extremely common -- would otherwise be lost entirely.
    if isinstance(element.tag, str) and element.tag.lower() not in BLOCK_TAGS:
        direct = normalise_text(
            " ".join(
                part
                for part in ([element.text] + [child.tail for child in element])
                if part and part.strip()
            )
        )
        if direct:
            # Bare text in a `<div>`. Any anchors here belong to a child element
            # rather than to this text, so the block carries no link profile; those
            # anchors are recorded when their own block is emitted, or by `_links`'
            # document-order sweep when they belong to no block at all.
            blocks.append(
                Block(
                    kind=BlockKind.PARAGRAPH,
                    text=direct,
                    container=container,
                    in_chrome=in_chrome,
                )
            )


def _table(element: html.HtmlElement) -> Table:
    """Rows and cells, with spans where they are declared (section 11)."""
    caption_element = element.find(".//caption")
    caption = _text_of(caption_element) if caption_element is not None else None

    def row_cells(row: html.HtmlElement) -> list[TableCell]:
        cells: list[TableCell] = []
        for cell in row:
            if not isinstance(cell.tag, str) or cell.tag.lower() not in ("td", "th"):
                continue
            cells.append(
                TableCell(
                    text=_text_of(cell),
                    header=cell.tag.lower() == "th",
                    rowspan=_span(cell.get("rowspan")),
                    colspan=_span(cell.get("colspan")),
                )
            )
        return cells

    header_rows: list[list[TableCell]] = []
    rows: list[list[TableCell]] = []
    for row in element.findall(".//tr"):
        cells = row_cells(row)
        if not cells:
            continue
        in_thead = any(
            isinstance(parent.tag, str) and parent.tag.lower() == "thead"
            for parent in row.iterancestors()
        )
        if in_thead or (not rows and not header_rows and all(cell.header for cell in cells)):
            header_rows.append(cells)
        else:
            rows.append(cells)
    return Table(caption=caption, header_rows=header_rows, rows=rows)


def _span(value: str | None) -> int:
    """A span attribute, defensively. `colspan="100%"` exists in the wild."""
    if not value:
        return 1
    try:
        return max(1, min(int(value.strip()), 1000))
    except ValueError:
        return 1


def _links(tree: html.HtmlElement, anchors: list[_Anchor], effective_url: str | None) -> list[Link]:
    """Anchors with a location, in document order. Recorded for inventory; never followed.

    The walk supplies provenance -- which block and which container an anchor sat in --
    and this sweeps the whole tree so that an anchor outside every emitted block is
    still inventoried, with `block_index=None`. Dropping those would have lost the
    inventory for a page whose anchors sit bare in `<body>`.

    Provenance is keyed on `getpath` rather than object identity: lxml caches an element
    proxy only while something holds a reference to it, so a fresh iteration can be
    handed a different proxy for the same element.

    Deduplication is on `(text, href, container, block_index)`. The same link in a nav
    and in the body is two facts about the page, and collapsing them was part of why a
    catalogue link could not be told from a site-wide picker.
    """
    # `getpath` lives on the ElementTree, not on the element.
    paths = tree.getroottree()
    provenance = {paths.getpath(anchor.element): anchor for anchor in anchors}

    links: list[Link] = []
    seen: set[tuple[str, str, str | None, int | None]] = set()
    for element in tree.iter("a"):
        href = (element.get("href") or "").strip()
        if not href:
            continue
        found = provenance.get(paths.getpath(element))
        text = normalise_text(element.text_content())[:_MAX_LINK_TEXT]
        container = found.container if found else None
        block_index = found.block_index if found else None
        key = (text, href, container, block_index)
        if key in seen:
            continue
        seen.add(key)
        resolved = _resolve(href, effective_url)
        host = urlsplit(resolved).hostname if resolved else None
        links.append(
            Link(
                text=text,
                href=href,
                resolved=resolved,
                rel=(element.get("rel") or None),
                is_pdf=bool(resolved and urlsplit(resolved).path.lower().endswith(".pdf")),
                host=host.lower() if host else None,
                container=container,
                in_chrome=bool(found and found.in_chrome),
                block_index=block_index,
                ancestry=found.ancestry if found else _ancestry_of(element),
                is_link_only_block=bool(found and found.is_link_only_block),
                is_link_only_item=bool(found and found.is_link_only_item),
            )
        )
    return links


def _resolve(href: str, effective_url: str | None) -> str | None:
    """Absolute URL, or None when the href does not name a location.

    `javascript:` and `data:` are excluded deliberately: resolving them produces
    something that looks like a URL and is not one, and a later step might try to
    fetch it.
    """
    lowered = href.lower()
    if lowered.startswith(("javascript:", "data:", "#")):
        return None
    if lowered.startswith(("mailto:", "tel:")):
        return None
    if not effective_url:
        return href if lowered.startswith(("http://", "https://")) else None
    try:
        joined = urljoin(effective_url, href)
    except ValueError:
        return None
    return joined if joined.lower().startswith(("http://", "https://")) else None


def _embeds(tree: html.HtmlElement) -> list[Embed]:
    embeds: list[Embed] = []
    for tag in ("iframe", "embed", "object"):
        for element in tree.findall(f".//{tag}"):
            src = element.get("src") or element.get("data")
            embeds.append(
                Embed(tag=tag, src=(src.strip() if src else None), title=element.get("title"))
            )
    return embeds


def _structured_data(tree: html.HtmlElement, warnings: list[str]) -> StructuredData:
    """JSON-LD and safely-identifiable JSON (sections 9-10).

    A malformed block is a warning and never a failure: structured data is optional,
    and refusing a page's 40,000 characters of real text because one JSON-LD block has
    a trailing comma would be the wrong trade.
    """
    json_ld: list[object] = []
    embedded: list[object] = []
    unparsed: list[str] = []
    types: set[str] = set()

    for script in tree.findall(".//script"):
        script_type = (script.get("type") or "").split(";")[0].strip().lower()
        script_id = (script.get("id") or "").strip()
        body = script.text_content() or ""

        if script_type not in _JSON_SCRIPT_TYPES and script_id not in _JSON_SCRIPT_IDS:
            if body.strip():
                unparsed.append(script_type or "text/javascript")
            continue
        if len(body.encode("utf-8", "replace")) > _MAX_JSON_BYTES:
            warnings.append(f"structured-data block over {_MAX_JSON_BYTES} bytes skipped")
            continue
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            warnings.append(f"malformed {script_type or script_id} block: {exc}")
            continue

        if script_type == _JSON_LD_TYPE:
            flattened = _flatten_json_ld(parsed)
            json_ld.extend(flattened)
            for item in flattened:
                types.update(_types_of(item))
        else:
            embedded.append(parsed)

    return StructuredData(
        json_ld=json_ld,
        json_ld_types=sorted(types),
        embedded_json=embedded,
        unparsed_payloads=sorted(set(unparsed)),
    )


def _flatten_json_ld(parsed: object) -> list[object]:
    """Accept the three legal shapes: an object, an array, or an `@graph`."""
    if isinstance(parsed, list):
        out: list[object] = []
        for item in parsed:
            out.extend(_flatten_json_ld(item))
        return out
    if isinstance(parsed, dict):
        graph = parsed.get("@graph")
        if isinstance(graph, list):
            out = list(graph)
            # An object carrying @graph may still describe itself; keep both.
            rest = {key: value for key, value in parsed.items() if key != "@graph"}
            if len(rest) > 1:
                out.append(rest)
            return out
        return [parsed]
    return []


def _types_of(item: object) -> set[str]:
    if not isinstance(item, dict):
        return set()
    raw = item.get("@type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {value for value in raw if isinstance(value, str)}
    return set()


def _title(tree: html.HtmlElement) -> str | None:
    found = tree.find(".//title")
    if found is None:
        return None
    text = normalise_text(found.text_content())
    return text or None


def _language(tree: html.HtmlElement) -> str | None:
    for attribute in ("lang", "{http://www.w3.org/XML/1998/namespace}lang"):
        value = str(tree.get(attribute) or "")
        if value.strip():
            return value.strip()
    meta = tree.find('.//meta[@http-equiv="content-language"]')
    if meta is not None and meta.get("content"):
        return (meta.get("content") or "").strip() or None
    return None


def _canonical(tree: html.HtmlElement, effective_url: str | None) -> str | None:
    for link in tree.findall(".//link"):
        rel = (link.get("rel") or "").strip().lower()
        if rel == "canonical" and link.get("href"):
            return _resolve((link.get("href") or "").strip(), effective_url)
    return None


def _metadata(tree: html.HtmlElement) -> dict[str, str]:
    """`<meta name=...>` and OpenGraph, verbatim.

    Kept whole rather than cherry-picked: a later extractor may want a field we did
    not anticipate, and these are cheap.
    """
    out: dict[str, str] = {}
    for meta in tree.findall(".//meta"):
        name = (meta.get("name") or meta.get("property") or "").strip().lower()
        content = (meta.get("content") or "").strip()
        if not name or not content:
            continue
        out.setdefault(name, normalise_text(content)[:2000])
    return out


def _with_statistics(document: NormalizedDocument, *, script_count: int) -> NormalizedDocument:
    from dataclasses import replace

    text = document.visible_text()
    statistics = DocumentStatistics(
        text_characters=len(text),
        blocks=len(document.blocks),
        headings=sum(1 for block in document.blocks if block.kind is BlockKind.HEADING),
        paragraphs=sum(1 for block in document.blocks if block.kind is BlockKind.PARAGRAPH),
        lists=sum(1 for block in document.blocks if block.kind is BlockKind.LIST),
        list_items=sum(len(block.items) for block in document.blocks),
        tables=len(document.tables),
        links=len(document.links),
        pdf_links=sum(1 for link in document.links if link.is_pdf),
        embeds=len(document.embeds),
        json_ld_blocks=len(document.structured_data.json_ld),
        embedded_json_blocks=len(document.structured_data.embedded_json),
        script_elements=script_count,
    )
    return replace(document, statistics=statistics)


__all__ = ["CONTAINER_TAGS", "DROPPED_TAGS", "parse_html_document"]
