"""Deterministic document normalisation over stored evidence (Step 5C.1).

Raw immutable evidence -> deterministic document extraction -> a versioned normalised
document representation. **Not** raw page -> university fact: field-level business
claims are Step 5C.2, and nothing here creates a `field_claim`.

Offline only. No JavaScript is executed, no network call is made, no LLM is involved.
"""

from app.domains.extraction.document import (
    EXTRACTOR_VERSION,
    HTML_EXTRACTOR,
    PDF_EXTRACTOR,
    NormalizedDocument,
)
from app.domains.extraction.html_document import parse_html_document
from app.domains.extraction.pdf_document import parse_pdf_document

__all__ = [
    "EXTRACTOR_VERSION",
    "HTML_EXTRACTOR",
    "PDF_EXTRACTOR",
    "NormalizedDocument",
    "parse_html_document",
    "parse_pdf_document",
]
