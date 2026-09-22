"""OpenAI-assisted field candidate extraction (offline over stored documents).

Reads a normalised document already in the artifact store. One API call per
(responsibility, document) pair. Output must cite block indices from the supplied
outline; locators are validated with ``resolve`` before a ``Candidate`` is returned.

Does not fetch pages, write canonical rows, or write ``field_claim``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domains.claims.locator import Locator, heading_path_for, resolve
from app.domains.claims.model import (
    Candidate,
    Confidence,
    FieldKind,
    extractors_for,
    is_chrome,
)
from app.domains.extraction.document import NormalizedDocument

logger = get_logger(__name__)

EXTRACTOR = "openai_structured"
VERSION = "1"

_FIELD_KINDS_BY_EXTRACTOR: dict[str, frozenset[FieldKind]] = {
    "tuition": frozenset({FieldKind.TUITION}),
    "language": frozenset(
        {
            FieldKind.LANGUAGE_TEST,
            FieldKind.LANGUAGE_OVERALL_SCORE,
            FieldKind.LANGUAGE_COMPONENT_SCORE,
        }
    ),
    "deadline": frozenset({FieldKind.APPLICATION_DEADLINE}),
    "admission": frozenset({FieldKind.ADMISSION_REQUIREMENT}),
    "program": frozenset(
        {
            FieldKind.PROGRAM_NAME,
            FieldKind.DEGREE_LEVEL,
            FieldKind.DURATION,
            FieldKind.STUDY_MODE,
            FieldKind.DISCIPLINE_HINT,
            FieldKind.CAMPUS,
            FieldKind.FACULTY_OR_SCHOOL,
        }
    ),
    "calendar": frozenset({FieldKind.ACADEMIC_CALENDAR_EVENT}),
}


def allowed_field_kinds(responsibility: str) -> frozenset[FieldKind]:
    kinds: set[FieldKind] = set()
    for key in extractors_for({responsibility}):
        kinds |= _FIELD_KINDS_BY_EXTRACTOR.get(key, frozenset())
    return frozenset(kinds)


def _document_outline(document: NormalizedDocument, *, max_chars: int = 120_000) -> str:
    lines: list[str] = []
    used = 0
    for index, block in enumerate(document.blocks):
        if is_chrome(block):
            continue
        path = heading_path_for(document.blocks, index)
        prefix = f"[block {index}]"
        if path:
            prefix += f" ({' > '.join(path)})"
        text = str(getattr(block, "text", "") or "").strip()
        if not text:
            continue
        line = f"{prefix}\n{text}\n"
        if used + len(line) > max_chars:
            lines.append(f"... truncated after {used} characters ...")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def _validate_item(
    document: NormalizedDocument,
    item: dict[str, Any],
    *,
    allowed: frozenset[FieldKind],
) -> Candidate | None:
    kind_raw = item.get("field_kind")
    if not isinstance(kind_raw, str):
        return None
    try:
        field_kind = FieldKind(kind_raw)
    except ValueError:
        return None
    if field_kind not in allowed:
        return None

    block_index = item.get("block_index")
    if not isinstance(block_index, int) or block_index < 0 or block_index >= len(document.blocks):
        return None

    value_raw = item.get("value_raw_text")
    evidence = item.get("evidence_text")
    if not isinstance(value_raw, str) or not value_raw.strip():
        return None
    if not isinstance(evidence, str) or not evidence.strip():
        return None

    block_text = str(document.blocks[block_index].text or "")
    norm_block = _normalize_for_match(block_text)
    norm_raw = _normalize_for_match(value_raw)
    norm_evidence = _normalize_for_match(evidence)
    if norm_raw not in norm_block:
        return None
    if norm_raw not in norm_evidence and norm_evidence not in norm_block:
        return None

    char_start: int | None = None
    char_end: int | None = None
    folded_block = block_text.casefold()
    folded_raw = value_raw.strip().casefold()
    pos = folded_block.find(folded_raw)
    if pos >= 0:
        char_start = pos
        char_end = pos + len(value_raw.strip())

    locator = Locator(
        kind="block",
        block_index=block_index,
        heading_path=heading_path_for(document.blocks, block_index),
        char_start=char_start,
        char_end=char_end,
    )
    resolved = resolve(document, locator.as_json())
    if resolved is None or _normalize_for_match(resolved) != norm_raw:
        locator = Locator(
            kind="block",
            block_index=block_index,
            heading_path=heading_path_for(document.blocks, block_index),
        )
        resolved = resolve(document, locator.as_json())
        if resolved is None or norm_raw not in _normalize_for_match(resolved):
            return None

    value_normalized = item.get("value_normalized")
    structured: dict[str, object] | None = None
    unresolved: str | None = None
    if isinstance(value_normalized, dict) and value_normalized:
        structured = value_normalized
    elif item.get("unresolved_reason"):
        unresolved = str(item["unresolved_reason"])[:500]
    elif structured is None and unresolved is None:
        structured = {"text": value_raw.strip()}

    confidence_raw = str(item.get("confidence_band", "MEDIUM")).upper()
    try:
        confidence = Confidence(confidence_raw)
    except ValueError:
        confidence = Confidence.MEDIUM

    reason = str(item.get("confidence_reason") or "OpenAI extraction with block citation").strip()
    if not reason:
        reason = "OpenAI extraction with block citation"

    return Candidate(
        field_kind=field_kind,
        value=structured,
        value_raw_text=value_raw.strip()[:2000],
        evidence_text=evidence.strip()[:4000],
        locator=locator,
        confidence=confidence,
        confidence_reason=reason[:500],
        unresolved_reason=unresolved,
    )


def _call_openai(
    *,
    responsibility: str,
    allowed: frozenset[FieldKind],
    outline: str,
    page_title: str | None,
    url_hint: str,
) -> list[dict[str, Any]]:
    settings = get_settings()
    api_key = settings.openai.api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise RuntimeError("OPENAI_API_KEY is not configured")

    from openai import OpenAI

    client = OpenAI(api_key=api_key.get_secret_value())
    kinds_list = sorted(k.value for k in allowed)
    system = (
        "You extract factual admissions-related claims from official university web pages. "
        "Return JSON only. Every claim must quote exact wording from the provided blocks. "
        "Never invent numbers, dates, tests, or fees. If the page does not state a fact, omit it."
    )
    user = (
        f"Page title: {page_title or '(unknown)'}\n"
        f"Source responsibility: {responsibility}\n"
        f"Allowed field_kind values: {', '.join(kinds_list)}\n\n"
        "Document blocks (cite block_index from this outline):\n"
        f"{outline}\n\n"
        "Return a JSON object: {\"claims\": [ ... ]} where each claim has:\n"
        "- field_kind (one of the allowed values)\n"
        "- value_raw_text (exact substring from that block)\n"
        "- evidence_text (sentence or list item containing value_raw_text)\n"
        "- value_normalized (optional JSON object with structured fields when obvious)\n"
        "- unresolved_reason (optional, when value cannot be normalised)\n"
        "- block_index (integer)\n"
        "- confidence_band: HIGH | MEDIUM | LOW\n"
        "- confidence_reason (short)\n"
        f"URL context: {url_hint}\n"
    )

    response = client.chat.completions.create(
        model=settings.openai.model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return []
    return [c for c in claims if isinstance(c, dict)]


def extract(document: NormalizedDocument, *, responsibility: str) -> list[Candidate]:
    """Produce validated candidates for one document and workbook responsibility."""
    allowed = allowed_field_kinds(responsibility)
    if not allowed:
        return []

    outline = _document_outline(document)
    if not outline.strip():
        return []

    url_hint = document.canonical_url or document.title or ""
    try:
        raw_items = _call_openai(
            responsibility=responsibility,
            allowed=allowed,
            outline=outline,
            page_title=document.title,
            url_hint=url_hint,
        )
    except Exception as exc:
        logger.warning("openai_extract_failed", responsibility=responsibility, error=str(exc))
        return []

    out: list[Candidate] = []
    for item in raw_items:
        candidate = _validate_item(document, item, allowed=allowed)
        if candidate is not None:
            out.append(candidate)
    return out


__all__ = [
    "EXTRACTOR",
    "VERSION",
    "allowed_field_kinds",
    "extract",
]
