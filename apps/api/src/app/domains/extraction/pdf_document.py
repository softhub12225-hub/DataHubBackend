"""Deterministic PDF text-layer extraction (Step 5C.1 section 13).

NO OCR
======
A PDF with no text layer is recorded as `OCR_REQUIRED` and left alone. OCR is a
different capability with a different error profile -- it guesses, and a guessed tuition
figure is worse than a missing one -- so it is not smuggled in here as a fallback.

WHY pypdf AND NOT PyMuPDF
=========================
Section 13 suggested PyMuPDF, which does give better block and reading-order data.
It is also AGPL-3.0, and a licence decision on a commercial internal product belongs
to the client rather than to a silent import. The entire pilot fleet contains **one**
registered PDF, so pypdf (BSD-3) is the proportionate choice: it reads the text layer,
the document info dictionary and link annotations, which is what this section asks for.

The extraction report states the text-layer quality actually obtained. If it proves
insufficient, swapping this module is a contained change -- everything downstream
consumes `NormalizedDocument`, not pypdf.

NO PDF ACTIONS ARE EXECUTED
===========================
pypdf parses structure; it does not run embedded JavaScript, launch actions, or fetch
remote resources. Link annotations are *recorded* as links, never followed.
"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.domains.extraction.document import (
    DOCUMENT_SCHEMA,
    EXTRACTOR_VERSION,
    PDF_EXTRACTOR,
    Block,
    BlockKind,
    DocumentStatistics,
    Link,
    NormalizedDocument,
    normalise_text,
)

#: A page yielding fewer than this many characters has no usable text layer. Generous
#: on purpose: a calendar page can legitimately be a handful of words, and calling that
#: "needs OCR" would be wrong.
MIN_PAGE_CHARACTERS = 8

#: Marker recorded in `warnings` when no page had a text layer.
OCR_REQUIRED = "OCR_REQUIRED"


class PdfExtractionError(RuntimeError):
    """The bytes are not a PDF we can read at all. The caller records FAILED."""


def parse_pdf_document(payload: bytes) -> NormalizedDocument:
    """Normalise one PDF's text layer, one block per page.

    Per page rather than one flat string, because a calendar's page number is part of
    how a human cites it -- "page 3 of the academic calendar" has to remain sayable.
    """
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfExtractionError(f"unreadable PDF: {exc}") from exc

    warnings: list[str] = []
    if reader.is_encrypted:
        # An empty-password PDF is common and decrypts silently; a real one does not,
        # and we do not attempt to break it.
        try:
            if reader.decrypt("") == 0:
                raise PdfExtractionError("the PDF is encrypted and needs a password")
        except (PdfReadError, NotImplementedError) as exc:
            raise PdfExtractionError(f"the PDF is encrypted: {exc}") from exc
        warnings.append("the PDF was encrypted with an empty password")

    blocks: list[Block] = []
    links: list[Link] = []
    pages_without_text = 0

    for number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except (PdfReadError, KeyError, ValueError, TypeError) as exc:
            warnings.append(f"page {number}: text extraction failed ({type(exc).__name__}: {exc})")
            pages_without_text += 1
            continue

        text = normalise_text(raw)
        if len(text) < MIN_PAGE_CHARACTERS:
            pages_without_text += 1
        blocks.append(Block(kind=BlockKind.PDF_PAGE, text=text, level=number, container="pdf"))
        links.extend(_page_links(page, number, warnings))

    total_pages = len(reader.pages)
    if total_pages and pages_without_text == total_pages:
        warnings.append(
            f"{OCR_REQUIRED}: no page carried a usable text layer "
            f"({total_pages} page(s) checked); not OCR'd in this step"
        )
    elif pages_without_text:
        warnings.append(
            f"{pages_without_text} of {total_pages} page(s) carried no usable text layer"
        )

    document = NormalizedDocument(
        schema=DOCUMENT_SCHEMA,
        extractor_name=PDF_EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
        media_type="application/pdf",
        title=_metadata_value(reader, "/Title"),
        language=_metadata_value(reader, "/Lang"),
        metadata=_metadata(reader),
        blocks=blocks,
        links=links,
        # A PDF declares no charset: pypdf resolves the font encodings itself. Recorded
        # explicitly so the field is never mistaken for "we did not check".
        encoding={"used": "pdf-internal", "fallback_used": False, "declared_http": None},
        warnings=warnings,
    )
    return _with_statistics(document, pages=total_pages)


def _page_links(page: object, number: int, warnings: list[str]) -> list[Link]:
    """Link annotations on one page. Recorded, never followed."""
    found: list[Link] = []
    annotations = getattr(page, "annotations", None)
    if not annotations:
        return found
    for annotation in annotations:
        try:
            obj = annotation.get_object()
            action = obj.get("/A") or {}
            uri = action.get("/URI")
        except (PdfReadError, AttributeError, KeyError, TypeError) as exc:
            warnings.append(f"page {number}: unreadable annotation ({type(exc).__name__})")
            continue
        if not isinstance(uri, str) or not uri.strip():
            continue
        resolved = uri.strip()
        is_http = resolved.lower().startswith(("http://", "https://"))
        found.append(
            Link(
                text="",
                href=resolved,
                resolved=resolved if is_http else None,
                is_pdf=resolved.lower().split("?")[0].endswith(".pdf"),
            )
        )
    return found


def _metadata(reader: PdfReader) -> dict[str, str]:
    """The info dictionary, verbatim, as strings."""
    out: dict[str, str] = {}
    info = reader.metadata
    if not info:
        return out
    for key, value in info.items():
        if value is None:
            continue
        name = str(key).lstrip("/").lower()
        out[name] = normalise_text(str(value))[:2000]
    return out


def _metadata_value(reader: PdfReader, key: str) -> str | None:
    info = reader.metadata
    if not info:
        return None
    value = info.get(key)
    if value is None:
        return None
    text = normalise_text(str(value))
    return text or None


def _with_statistics(document: NormalizedDocument, *, pages: int) -> NormalizedDocument:
    text = document.visible_text()
    statistics = DocumentStatistics(
        text_characters=len(text),
        blocks=len(document.blocks),
        links=len(document.links),
        pdf_links=sum(1 for link in document.links if link.is_pdf),
        pdf_pages=pages,
    )
    return replace(document, statistics=statistics)


__all__ = ["MIN_PAGE_CHARACTERS", "OCR_REQUIRED", "PdfExtractionError", "parse_pdf_document"]
