"""Deterministic document normalisation, on fixtures (Step 5C.1 sections 16-18, 31).

Fixtures, not the real fleet: the 175 real documents are the subject of a manual
extraction command, and turning a university's markup into a CI dependency would make
the suite fail when they redesign their site. The fixtures here are small documents
written to contain the specific shapes the real fleet turned out to have -- an
ISO-8859-15 declaration, a malformed JSON-LD block, a `@graph`, a table with spans, a
page whose only content is in a nav.
"""

from __future__ import annotations

import json
import socket

import pytest

from app.domains.extraction.charset import decode_document
from app.domains.extraction.document import BlockKind, normalise_text
from app.domains.extraction.html_document import parse_html_document
from app.domains.extraction.pdf_document import (
    OCR_REQUIRED,
    PdfExtractionError,
    parse_pdf_document,
)

# ===========================================================================
# Fixture documents
# ===========================================================================

SIMPLE = b"""<!DOCTYPE html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <title>  Tuition   fees  2027  </title>
  <meta name="description" content="What it costs.">
  <link rel="canonical" href="/study/fees">
</head>
<body>
  <nav><a href="/apply">Apply</a></nav>
  <main>
    <h1>Tuition fees</h1>
    <p>Fees for 2027 entry are shown below.</p>
    <h2>Overseas</h2>
    <ul><li>Band 1: 38,000</li><li>Band 2: 41,000</li></ul>
    <table>
      <caption>Fees by band</caption>
      <thead><tr><th>Band</th><th>Amount</th></tr></thead>
      <tbody>
        <tr><td>1</td><td>38,000</td></tr>
        <tr><td colspan="2">Subject to annual review</td></tr>
      </tbody>
    </table>
    <p>See the <a href="/docs/fees.pdf">fee schedule</a>.</p>
  </main>
  <footer><p>Deadline: 15 January.</p></footer>
  <script>var tracking = 1;</script>
  <style>body { color: red }</style>
</body>
</html>
"""


def _json_ld(payload: str) -> bytes:
    return (
        b"<html><head><title>T</title>"
        b'<script type="application/ld+json">' + payload.encode() + b"</script>"
        b"</head><body><p>Body text that is long enough to matter.</p></body></html>"
    )


# ===========================================================================
# 5. Normalised representation
# ===========================================================================


def test_the_document_keeps_its_structure_in_order() -> None:
    """Section 5. Not flattened into one string: blocks stay addressable.

    "The paragraph after the Overseas heading" has to remain a thing a later extractor
    can say, which a single text blob cannot express.
    """
    doc = parse_html_document(SIMPLE, content_type="text/html", effective_url="https://x.ac.uk/f")

    kinds = [block.kind for block in doc.blocks]
    assert BlockKind.HEADING in kinds
    assert BlockKind.LIST in kinds
    assert BlockKind.TABLE in kinds

    headings = [b for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert [(b.level, b.text) for b in headings] == [(1, "Tuition fees"), (2, "Overseas")]

    # Document order: the h1 precedes its paragraph, which precedes the h2.
    order = [doc.blocks.index(b) for b in headings]
    assert order == sorted(order)
    first_paragraph = next(
        b for b in doc.blocks if b.kind is BlockKind.PARAGRAPH and "2027" in b.text
    )
    assert order[0] < doc.blocks.index(first_paragraph) < order[1]

    assert doc.title == "Tuition fees 2027", "whitespace was not collapsed"
    assert doc.language == "en-GB"
    assert doc.canonical_url == "https://x.ac.uk/study/fees"
    assert doc.meta_description == "What it costs."


def test_lists_keep_their_item_boundaries() -> None:
    """Section 7. A list is items, not a sentence with commas in it."""
    doc = parse_html_document(SIMPLE, content_type="text/html")
    lists = [b for b in doc.blocks if b.kind is BlockKind.LIST]
    assert any(b.items == ["Band 1: 38,000", "Band 2: 41,000"] for b in lists)
    assert all(b.ordered is False for b in lists if b.items and "Band 1: 38,000" in b.items)


def test_tables_keep_rows_cells_and_spans() -> None:
    """Section 11. Preserved structurally and interpreted as nothing."""
    doc = parse_html_document(SIMPLE, content_type="text/html")
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert table.caption == "Fees by band"
    assert [cell.text for cell in table.header_rows[0]] == ["Band", "Amount"]
    assert all(cell.header for cell in table.header_rows[0])
    assert [cell.text for cell in table.rows[0]] == ["1", "38,000"]
    assert table.rows[1][0].colspan == 2
    assert table.column_count == 2

    # The block referencing it sits in document order and points at the table.
    block = next(b for b in doc.blocks if b.kind is BlockKind.TABLE)
    assert doc.tables[block.table_index or 0] is table


def test_links_are_inventoried_and_pdfs_flagged() -> None:
    """Section 12. Recorded and resolved; never crawled."""
    doc = parse_html_document(
        SIMPLE, content_type="text/html", effective_url="https://x.ac.uk/study/fees"
    )
    by_text = {link.text: link for link in doc.links}
    assert by_text["Apply"].resolved == "https://x.ac.uk/apply"
    schedule = by_text["fee schedule"]
    assert schedule.is_pdf is True
    assert schedule.resolved == "https://x.ac.uk/docs/fees.pdf"
    assert schedule.host == "x.ac.uk"
    assert doc.statistics.pdf_links == 1


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "data:text/html,<b>x", "#section", "mailto:a@b.c", "tel:+1"],
)
def test_non_locations_are_never_resolved(href: str) -> None:
    """A resolved `javascript:` URL is something a later step might try to fetch."""
    page = f'<html><body><a href="{href}">x</a></body></html>'.encode()
    doc = parse_html_document(page, effective_url="https://x.ac.uk/")
    assert doc.links[0].resolved is None


# ===========================================================================
# 6. Conservative noise removal
# ===========================================================================


def test_scripts_and_styles_are_dropped_but_navigation_is_kept() -> None:
    """Section 6. Universities put real information in footers.

    A parser that deleted a `<footer>` because it looked like navigation would lose the
    application deadline in this fixture -- which is exactly the failure the
    instruction warns about.
    """
    doc = parse_html_document(SIMPLE, content_type="text/html")
    text = doc.visible_text()

    assert "var tracking" not in text, "script code leaked into document text"
    assert "color: red" not in text, "stylesheet leaked into document text"

    assert "Deadline: 15 January." in text, "the footer's content was discarded"
    footer_blocks = [b for b in doc.blocks if b.container == "footer"]
    assert footer_blocks, "footer content was kept but not labelled"
    assert any(b.container == "nav" for b in doc.blocks) or any(
        link.text == "Apply" for link in doc.links
    )
    # Counted before removal, so the page's shape is still reportable.
    assert doc.statistics.script_elements == 1


def test_text_around_a_dropped_element_survives() -> None:
    """The tail text of a removed node is not removed with it."""
    page = b"<html><body><p>see <script>x=1</script> below</p></body></html>"
    doc = parse_html_document(page)
    assert "see below" in doc.visible_text()


def test_bare_text_in_a_div_is_not_lost() -> None:
    """Extremely common in the wild, and trivially easy to drop."""
    page = b"<html><body><div>Applications close on 15 January.</div></body></html>"
    doc = parse_html_document(page)
    assert "Applications close on 15 January." in doc.visible_text()


# ===========================================================================
# 7. Text normalisation
# ===========================================================================


def test_normalisation_collapses_whitespace_and_nothing_else() -> None:
    """Section 7. No translation, no summary, no rewriting."""
    assert normalise_text("  a \n\t b  ") == "a b"
    # NFC: the same character spelled two ways must hash alike.
    assert normalise_text("é") == normalise_text("é")
    # Wording is preserved to the character otherwise.
    original = "Tuition is £38,000 (2027 entry) — subject to review."
    assert normalise_text(original) == original


# ===========================================================================
# 8. Character encoding
# ===========================================================================


def test_the_http_charset_wins_over_the_document() -> None:
    """Section 8. The server said it about these bytes."""
    payload = '<html><head><meta charset="utf-8"><title>Fée</title></head></html>'.encode(
        "iso-8859-15"
    )
    decoded = decode_document(payload, content_type="text/html; charset=ISO-8859-15")
    assert decoded.used == "iso8859-15"
    assert decoded.fallback_used is False
    assert "Fée" in decoded.text


def test_a_document_declaration_is_used_when_http_is_silent() -> None:
    payload = '<html><head><meta charset="iso-8859-15"><title>Fée</title></head></html>'.encode(
        "iso-8859-15"
    )
    decoded = decode_document(payload, content_type="text/html")
    assert decoded.used == "iso8859-15"
    assert decoded.declared_http is None
    assert decoded.declared_document == "iso-8859-15"


def test_a_wrong_declaration_falls_through_rather_than_corrupting() -> None:
    """A page can lie about its charset. The ladder notices and records it."""
    payload = "Fée coûte".encode("iso-8859-15")
    decoded = decode_document(payload, content_type="text/html; charset=utf-8")
    assert decoded.fallback_used is True
    assert any("utf-8" in attempt for attempt in decoded.attempts)
    assert decoded.used == "cp1252"
    # cp1252 decodes every byte, so nothing is replaced -- the text is recovered.
    assert decoded.replacement_characters == 0


def test_a_bom_outranks_a_contradicting_meta_tag() -> None:
    payload = b"\xef\xbb\xbf" + b'<html><head><meta charset="iso-8859-15">x</head></html>'
    decoded = decode_document(payload)
    assert decoded.used in ("utf-8-sig", "utf-8")


def test_an_unknown_declared_charset_moves_down_the_ladder() -> None:
    """`charset=unicode` is real and is not a codec."""
    payload = b"<html><body><p>plain ascii</p></body></html>"
    decoded = decode_document(payload, content_type="text/html; charset=unicode")
    assert decoded.used == "utf-8"
    assert any("unknown codec" in attempt for attempt in decoded.attempts)


def test_the_encoding_decision_is_recorded_on_the_document() -> None:
    """Section 8 asks for these three facts to be recorded, not just used."""
    payload = "Fée".encode("iso-8859-15")
    doc = parse_html_document(payload, content_type="text/html; charset=ISO-8859-15")
    assert doc.encoding["declared_http"] == "iso-8859-15"
    assert doc.encoding["used"] == "iso8859-15"
    assert doc.encoding["fallback_used"] is False


def test_a_charset_fallback_makes_the_extraction_partial() -> None:
    """Section 17. A fallback is a warning, and a warning is what PARTIAL means."""
    doc = parse_html_document("Fée".encode("iso-8859-15"), content_type="text/html")
    assert doc.warnings, "a fallback decode recorded no warning"
    assert any("fallback" in warning for warning in doc.warnings)


# ===========================================================================
# 9. JSON-LD
# ===========================================================================


def test_a_single_json_ld_object_is_parsed() -> None:
    doc = parse_html_document(_json_ld('{"@type": "CollegeOrUniversity", "name": "X"}'))
    assert doc.structured_data.json_ld == [{"@type": "CollegeOrUniversity", "name": "X"}]
    assert doc.structured_data.json_ld_types == ["CollegeOrUniversity"]


def test_a_json_ld_array_is_flattened() -> None:
    doc = parse_html_document(_json_ld('[{"@type": "WebPage"}, {"@type": "Course"}]'))
    assert doc.structured_data.json_ld_types == ["Course", "WebPage"]
    assert len(doc.structured_data.json_ld) == 2


def test_a_json_ld_graph_is_flattened() -> None:
    """The third legal shape, and the one most likely to be mishandled."""
    payload = (
        '{"@context": "https://schema.org", "@graph": '
        '[{"@type": "Organization"}, {"@type": "BreadcrumbList"}]}'
    )
    doc = parse_html_document(_json_ld(payload))
    assert doc.structured_data.json_ld_types == ["BreadcrumbList", "Organization"]


def test_a_list_valued_type_is_inventoried_fully() -> None:
    doc = parse_html_document(_json_ld('{"@type": ["Course", "Product"]}'))
    assert doc.structured_data.json_ld_types == ["Course", "Product"]


def test_malformed_json_ld_warns_and_keeps_the_rest_of_the_document() -> None:
    """Section 9 and 17. The optional block must not fail the whole page.

    This is the case the instruction calls out: real text plus one malformed block is
    PARTIAL, not FAILED, because refusing 40,000 characters of real content over a
    trailing comma would be the wrong trade.
    """
    doc = parse_html_document(_json_ld('{"@type": "Course",}'))
    assert doc.structured_data.json_ld == []
    assert any("malformed" in warning for warning in doc.warnings)
    assert "Body text that is long enough to matter." in doc.visible_text()
    assert doc.statistics.text_characters > 0


def test_json_ld_presence_confers_nothing() -> None:
    """Section 9. Present is not authoritative, and not a claim."""
    doc = parse_html_document(_json_ld('{"@type": "Course", "name": "Free degree"}'))
    # The only place it appears is the derived structured-data inventory.
    assert doc.structured_data.json_ld_types == ["Course"]
    assert doc.media_type == "text/html"
    # Nothing in the document representation asserts a fact or a confidence.
    assert not hasattr(doc, "claims")
    assert not hasattr(doc, "facts")


# ===========================================================================
# 10. Embedded JSON
# ===========================================================================


def test_an_application_json_block_is_parsed() -> None:
    page = (
        b"<html><head><title>T</title>"
        b'<script type="application/json" id="data">{"a": 1}</script>'
        b"</head><body><p>text</p></body></html>"
    )
    doc = parse_html_document(page)
    assert doc.structured_data.embedded_json == [{"a": 1}]


def test_next_data_is_parsed_by_exact_id() -> None:
    """Matched by exact id, not a pattern: a heuristic over script bodies is how a
    parser turns into a JavaScript interpreter."""
    page = (
        b"<html><head><title>T</title>"
        b'<script id="__NEXT_DATA__" type="application/json">{"props": {"x": 2}}</script>'
        b"</head><body><p>text</p></body></html>"
    )
    doc = parse_html_document(page)
    assert doc.structured_data.embedded_json == [{"props": {"x": 2}}]


def test_ordinary_javascript_is_inventoried_and_not_parsed() -> None:
    """Section 10. Inventory for future work; no site-specific parsers now."""
    page = b"<html><body><script>window.__DATA__ = {a:1};</script><p>x</p></body></html>"
    doc = parse_html_document(page)
    assert doc.structured_data.embedded_json == []
    assert "text/javascript" in doc.structured_data.unparsed_payloads


# ===========================================================================
# 16. Determinism
# ===========================================================================


def test_extraction_is_deterministic_for_the_same_bytes_and_version() -> None:
    """Section 16. The artifact hash is a function of input and version, nothing else."""
    first = parse_html_document(SIMPLE, content_type="text/html", effective_url="https://x/f")
    second = parse_html_document(SIMPLE, content_type="text/html", effective_url="https://x/f")

    assert first.document_hash() == second.document_hash()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.to_json() == second.to_json()


def test_no_identifier_or_timestamp_reaches_the_artifact() -> None:
    """Non-vacuity for the test above: determinism would be trivial if the artifact
    were empty, and impossible if it carried a uuid or a clock reading."""
    doc = parse_html_document(SIMPLE, content_type="text/html")
    payload = doc.canonical_bytes().decode()

    assert len(payload) > 500, "the artifact is too small to be meaningful"
    for forbidden in ("recorded_at", "snapshot_id", "source_id", "extraction_id", "2026-"):
        assert forbidden not in payload, f"{forbidden} contaminates the artifact"
    # And what it *does* carry is the extractor identity, which must be in the hash.
    assert json.loads(payload)["extractor_version"]


def test_canonical_bytes_are_key_ordered() -> None:
    """Determinism cannot depend on dict insertion order."""
    doc = parse_html_document(SIMPLE, content_type="text/html")
    payload = json.loads(doc.canonical_bytes())
    assert list(payload) == sorted(payload)


def test_a_version_change_changes_the_hash() -> None:
    """Otherwise a new extractor version could not produce a new artifact."""
    from dataclasses import replace

    doc = parse_html_document(SIMPLE, content_type="text/html")
    bumped = replace(doc, extractor_version="9.9.9")
    assert bumped.document_hash() != doc.document_hash()


# ===========================================================================
# 18. Security: offline parsing only
# ===========================================================================


def test_the_parser_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Section 18. Proved by removing the ability, not by reading the code.

    Every socket entry point raises for the duration of the parse, so an external DTD
    fetch, an iframe load or a PDF launch action would fail the test rather than
    quietly succeed.
    """
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        calls.append("socket")
        raise AssertionError("the parser attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    hostile = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE html SYSTEM "http://attacker.example/evil.dtd">'
        b"<html><head><title>x</title>"
        b'<link rel="stylesheet" href="http://attacker.example/s.css">'
        b"</head><body>"
        b'<iframe src="http://attacker.example/frame"></iframe>'
        b'<img src="http://attacker.example/pixel.png">'
        b"<p>Real content.</p></body></html>"
    )
    doc = parse_html_document(hostile, effective_url="https://x.ac.uk/")

    assert calls == []
    assert "Real content." in doc.visible_text()
    # The iframe is recorded so a reviewer can see it, and was not fetched.
    assert doc.embeds[0].src == "http://attacker.example/frame"


def test_an_external_entity_is_not_expanded() -> None:
    """XXE, against a parser that has no XML entity mechanism to begin with."""
    hostile = (
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<html><body><p>&xxe;</p></body></html>"
    )
    doc = parse_html_document(hostile)
    text = doc.visible_text()
    assert "root:" not in text
    assert "/etc/passwd" not in text


# ===========================================================================
# 13. PDF
# ===========================================================================


def _minimal_pdf(text: str = "Academic Calendar 2027") -> bytes:
    """A hand-built one-page PDF with a real text layer."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


def test_a_pdf_text_layer_is_extracted_per_page() -> None:
    """Section 13. One block per page, so a page number stays citable."""
    doc = parse_pdf_document(_minimal_pdf())
    assert doc.media_type == "application/pdf"
    assert doc.statistics.pdf_pages == 1
    pages = [block for block in doc.blocks if block.kind is BlockKind.PDF_PAGE]
    assert len(pages) == 1
    assert pages[0].level == 1
    assert "Academic Calendar 2027" in pages[0].text


def test_a_pdf_with_no_text_layer_is_marked_rather_than_ocrd() -> None:
    """Section 13. OCR guesses, and a guessed figure is worse than a missing one."""
    doc = parse_pdf_document(_minimal_pdf(text=""))
    assert any(OCR_REQUIRED in warning for warning in doc.warnings)
    assert doc.statistics.text_characters < 8


def test_unreadable_bytes_raise_rather_than_producing_an_empty_document() -> None:
    """An empty document would assert we read something we did not."""
    with pytest.raises(PdfExtractionError):
        parse_pdf_document(b"not a pdf at all")


def test_pdf_extraction_is_deterministic() -> None:
    payload = _minimal_pdf()
    assert (
        parse_pdf_document(payload).document_hash() == parse_pdf_document(payload).document_hash()
    )


# ===========================================================================
# 19. No business claims
# ===========================================================================


def test_the_document_representation_contains_no_business_fact() -> None:
    """Section 19, as a property of the output shape.

    The fixture contains a fee, a band and a deadline. All three survive as *text* in
    their structural position; none becomes a typed value, an amount, a currency or a
    date, because that is Step 5C.2's job and doing it here would skip the review the
    whole system exists to enforce.
    """
    doc = parse_html_document(SIMPLE, content_type="text/html")
    payload = doc.to_json()

    for absent in ("claims", "facts", "tuition", "amount", "currency", "deadline_date"):
        assert absent not in payload

    text = doc.visible_text()
    assert "38,000" in text, "the source wording was not preserved"
    assert "15 January" in text
    # ...and it is still a string in a block, not a parsed value anywhere.
    assert all(isinstance(block.text, str) for block in doc.blocks)


# ===========================================================================
# 12. Step 5C.4: extraction hygiene
# ===========================================================================


def test_script_payload_never_reaches_prose_and_words_do_not_weld() -> None:
    """The fixture section 4 names, exactly as it words it.

    The old drop pass mutated the tree while iterating it, so most of what it meant to
    remove survived: 22 of 29 scripts and 48 of 49 SVGs stayed in the fleet's documents,
    and their payload was emitted as visible prose. Removing `<script>` from the block
    list would not have fixed it, because the parent's text content still contained the
    descendant's text.
    """
    page = b"<html><body><p>real text<script>payload</script>more real text</p></body></html>"
    doc = parse_html_document(page)

    assert doc.blocks[0].text == "real text more real text"
    assert "payload" not in doc.visible_text()
    assert "payload" not in "".join(block.text or "" for block in doc.blocks)


def test_every_dropped_element_is_actually_dropped() -> None:
    """The defect was a loop, not a list: removing while iterating skips siblings.

    Five scripts in a row is the shape that exposed it -- `tree.iter()` advances past
    the next sibling every time the current one is removed, so alternate elements
    survived.
    """
    page = (
        b"<html><body><div>"
        + b"".join(b"<script>leak%d</script>" % index for index in range(5))
        + b"<style>.leak6{}</style><noscript></noscript>"
        + b"<p>kept</p></div></body></html>"
    )
    doc = parse_html_document(page)
    text = doc.visible_text()
    for index in range(5):
        assert f"leak{index}" not in text, f"script {index} survived the drop pass"
    assert ".leak6" not in text
    assert "kept" in text


def test_chrome_is_sticky_so_a_section_inside_a_nav_is_still_chrome() -> None:
    """Adding `section` to the container list created a way to escape a nav.

    `container` records the *innermost* semantic container, so a `<section>` wrapped in
    a `<nav>` reports `section`, and any guard reading only `container` waves it
    through. The fix records the two facts separately rather than conflating them.
    """
    page = (
        b"<html><body><nav><section><p>Entry requirements for applicants</p>"
        b"</section></nav><main><section><p>Applicants need a degree.</p>"
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    inside_nav = next(b for b in doc.blocks if "Entry requirements" in (b.text or ""))
    inside_main = next(b for b in doc.blocks if "Applicants need" in (b.text or ""))

    # Both report the innermost container, which is why `container` alone is not enough.
    assert inside_nav.container == "section"
    assert inside_main.container == "section"
    # The sticky flag is what tells them apart.
    assert inside_nav.in_chrome is True
    assert inside_main.in_chrome is False


def test_a_link_only_block_is_recorded_as_one_without_being_deleted() -> None:
    """Section 3: record the shape, let the field rule decide.

    Deleting link-only content in the parser would fix the navigation labels and lose
    the programme catalogues, which legitimately list programmes as links.
    """
    page = (
        b'<html><body><main><p><a href="/entry">International entry requirements</a></p>'
        b'<p>Applicants must hold a <a href="/deg">bachelor degree</a> to apply here.</p>'
        b"</main></body></html>"
    )
    doc = parse_html_document(page)
    label, prose = doc.blocks[0], doc.blocks[1]

    assert label.link_profile is not None
    assert label.link_profile.is_link_only is True
    assert label.text == "International entry requirements", "the block was not deleted"

    assert prose.link_profile is not None
    assert prose.link_profile.is_link_only is False
    assert prose.link_profile.count == 1


def test_a_list_item_that_is_only_a_link_is_identified_per_item() -> None:
    """A catalogue's item is link-only while the list around it is not."""
    page = (
        b'<html><body><main><ul><li><a href="/a">MSc Computer Science</a></li>'
        b"<li>Applicants must provide two academic references.</li></ul>"
        b"</main></body></html>"
    )
    doc = parse_html_document(page)
    block = next(b for b in doc.blocks if b.kind is BlockKind.LIST)

    assert block.link_profile is not None
    assert block.link_profile.is_link_only is False, "the whole list is not one link"
    assert block.link_profile.link_only_items == 1

    link = next(link for link in doc.links if link.text == "MSc Computer Science")
    assert link.is_link_only_item is True
    assert link.block_index == doc.blocks.index(block)


def test_a_links_structural_origin_is_recorded_deterministically() -> None:
    """Section 2: ancestry is tag names only, so it survives a restyle.

    No classes, no ids, no indices, and capped -- a full DOM path would change whenever
    a wrapper `<div>` was added and would make the artifact hash unstable.
    """
    page = (
        b'<html><body><footer><div><ul><li><a href="/x">Contact us</a></li>'
        b"</ul></div></footer></body></html>"
    )
    doc = parse_html_document(page)
    link = doc.links[0]

    assert link.container == "footer"
    assert link.in_chrome is True
    assert link.ancestry is not None
    assert all(part.isalpha() for part in link.ancestry.split("/")), link.ancestry
    assert "[" not in link.ancestry and "." not in link.ancestry and "#" not in link.ancestry


def test_parsing_is_deterministic_across_runs() -> None:
    """Section 2: the representation must not depend on iteration order or float noise."""
    page = (
        b'<html><body><nav><a href="/a">A</a></nav><main><section>'
        b'<p>Fees are <a href="/f">38,000</a> per year for 2027 entry.</p>'
        b"</section></main></body></html>"
    )
    first, second = parse_html_document(page), parse_html_document(page)

    assert [b.text for b in first.blocks] == [b.text for b in second.blocks]
    assert [(b.container, b.in_chrome) for b in first.blocks] == [
        (b.container, b.in_chrome) for b in second.blocks
    ]
    assert [(link.text, link.container, link.block_index) for link in first.links] == [
        (link.text, link.container, link.block_index) for link in second.links
    ]
    profile = next(b.link_profile for b in first.blocks if b.link_profile)
    assert profile.text_fraction == round(profile.text_fraction, 3)
