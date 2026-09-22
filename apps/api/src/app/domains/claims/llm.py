"""A language-model field extractor, constrained to what the document actually says.

WHY THIS CAN EXIST AT ALL
=========================
Every other extractor here is a deterministic rule: a regex over a block, a table
header match, a JSON-LD path. Rules are precise and brittle. They read
"IELTS 7.0 overall" and miss "an overall band of seven point zero, with no component
below 6.5", which is how a real prospectus writes it. That gap is where this belongs.

What makes it admissible is not the model's accuracy. It is that **the model is never
believed.** It reads a document this system already fetched, hashed and stored, and
every claim it returns must quote that document verbatim. The quote is checked here,
mechanically, against the block it cites. A claim whose wording does not appear in the
document it was supposedly read from is discarded before it reaches the database --
not flagged, not down-weighted, discarded.

That single check is what separates an extractor from an oracle. A model that invents
a plausible tuition figure produces a quote that is not in the page, and the invention
dies in `_verified`. A model that reads correctly produces a quote a reviewer can find
with ctrl-F.

WHAT IT STILL CANNOT DO
=======================
It writes `field_claim_candidate` rows and nothing else. No `field_claim`, no
`field_provenance`, no `change_proposal`, and it cannot make a source publication
eligible. A HIGH confidence band from a model means the same as a HIGH band from a
regex: a human has not looked at it yet.

It never receives a URL and never browses. If it has not been given the bytes, it has
nothing to say. That is deliberate: a claim sourced from the model's memory of a 2023
prospectus would carry a snapshot it was never read from, which is precisely the
failure this platform exists to prevent.

ABSTENTION IS A RESULT, NOT A FAILURE
=====================================
"This page does not state a tuition figure" is a finding this product publishes --
`OFFICIALLY_NOT_PUBLISHED` is a real state, and a false one is expensive. Models are
famously bad at abstaining, so the prompt asks for it explicitly, the schema has a
place to put it (`unresolved_reason`), and a claim with no quote can only be an
abstention. Measuring abstention on pages where the answer is known to be absent
matters more than measuring extraction on pages where it is present.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.domains.claims.locator import Locator
from app.domains.claims.model import Candidate, Confidence, FieldKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import OpenAISettings
    from app.domains.extraction.document import NormalizedDocument

#: Bumped whenever the prompt, the schema or the verification rule changes.
#:
#: It is part of every candidate's fingerprint, so a re-run after a prompt change
#: produces new candidates beside the old ones rather than silently replacing them --
#: which is what lets two prompt versions be compared on the same documents.
EXTRACTOR_NAME = "llm"
EXTRACTOR_VERSION = "openai-1"

#: The kinds this extractor is asked for. Deliberately not every `FieldKind`: these are
#: the ones where prose defeats a regex. Structural facts a rule gets right are left to
#: the rule, which is cheaper, reproducible and needs no network.
REQUESTED_KINDS: tuple[FieldKind, ...] = (
    FieldKind.LANGUAGE_OVERALL_SCORE,
    FieldKind.LANGUAGE_COMPONENT_SCORE,
    FieldKind.LANGUAGE_TEST,
    FieldKind.TUITION,
    FieldKind.APPLICATION_DEADLINE,
    FieldKind.ADMISSION_REQUIREMENT,
)


class LlmExtractionError(RuntimeError):
    """The model could not be reached, or answered in a shape we cannot read."""


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """What one document produced, including what was thrown away and why.

    The rejected count is not diagnostics. It is the measurement that says whether the
    model is reading or guessing, and it belongs in the operator's report.
    """

    candidates: list[Candidate]
    rejected_unquoted: int
    abstentions: int
    model: str


def _normalise(value: str) -> str:
    """Whitespace-insensitive comparison form.

    A model reflowing a quote across line breaks, or turning a non-breaking space into
    an ordinary one, is not fabrication. Case is preserved: `IELTS` and `ielts` differ
    in a way that matters when the quote is meant to be verbatim.
    """
    return re.sub(r"\s+", " ", value).strip()


def _block_texts(document: NormalizedDocument) -> list[str]:
    return [_normalise(getattr(block, "text", "") or "") for block in document.blocks]


def _verified(quote: str, block_index: int | None, blocks: list[str]) -> tuple[bool, int | None]:
    """Does this wording actually appear in the document, and where?

    Returns the block it was found in, which may not be the one the model cited -- a
    model that reads the right sentence and miscounts the block is still reading. A
    model that quotes something absent from every block is not, and that is the case
    this function exists to catch.
    """
    needle = _normalise(quote)
    if not needle:
        return False, None
    # The cited block first, so a quote that appears more than once on the page keeps
    # the location the model actually read it from.
    if (
        block_index is not None
        and 0 <= block_index < len(blocks)
        and needle in blocks[block_index]
    ):
        return True, block_index
    for index, text in enumerate(blocks):
        if needle in text:
            return True, index
    return False, None


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "field_kind",
                    "value_raw_text",
                    "evidence_text",
                    "block_index",
                    "confidence",
                    "confidence_reason",
                    "not_stated",
                ],
                "properties": {
                    "field_kind": {"type": "string", "enum": [k.value for k in REQUESTED_KINDS]},
                    # The exact wording carrying the value. This is the field the
                    # verification check runs against, so an invented figure has
                    # nowhere to hide.
                    "value_raw_text": {"type": "string"},
                    # The surrounding sentence or row a reviewer needs for context.
                    "evidence_text": {"type": "string"},
                    "block_index": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "confidence_reason": {"type": "string"},
                    # The abstention path. True means the document was read and does
                    # not state this, which is a finding rather than a gap.
                    "not_stated": {"type": "boolean"},
                },
            },
        }
    },
}

_SYSTEM_PROMPT = """You extract admissions facts from one university page.

Rules, in order of importance:

1. Report only what the supplied document states. You have no other knowledge of this
   university. If you recall a figure from elsewhere, it is not evidence here.
2. `value_raw_text` MUST be copied verbatim from the document, character for character.
   It is checked against the document automatically; a quote that does not appear there
   is discarded.
3. If the document does not state a value for a field, return one entry for that field
   with `not_stated` set to true and an empty `value_raw_text`. Saying "this page does
   not state it" is a correct and useful answer. Guessing is not.
4. Do not convert, average or combine figures. A page giving fees per unit does not
   state an annual fee.
5. `block_index` is the zero-based index of the block you quoted from.
"""


def _document_payload(document: NormalizedDocument, *, max_blocks: int) -> str:
    """The document as the model sees it: indexed blocks, nothing else.

    No URL, no institution name, no responsibility label. Anything beyond the text
    invites the model to answer from what it knows about that university rather than
    from the page, and the whole point is that it cannot.
    """
    lines: list[str] = []
    if document.title:
        lines.append(f"TITLE: {document.title}")
    for index, block in enumerate(document.blocks[:max_blocks]):
        text = _normalise(getattr(block, "text", "") or "")
        if text:
            lines.append(f"[{index}] {text}")
    return "\n".join(lines)


def extract(
    document: NormalizedDocument,
    settings: OpenAISettings,
    *,
    client: Any = None,
    max_blocks: int = 400,
) -> ExtractionOutcome:
    """Read one stored document and return verified candidates.

    `client` is injectable so the verification behaviour can be tested exhaustively
    without a network call or an API key -- which matters, because the verification is
    the part that has to be right.
    """
    if client is None:  # pragma: no cover - exercised only with real credentials
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LlmExtractionError(
                "the `openai` package is not installed; add it to the api "
                "dependencies before enabling this extractor"
            ) from exc
        client = OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
        )

    payload = _document_payload(document, max_blocks=max_blocks)
    try:
        response = client.chat.completions.create(
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "claims",
                    "schema": _RESPONSE_SCHEMA,
                    "strict": True,
                },
            },
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        raw = response.choices[0].message.content or "{}"
    except Exception as exc:
        # Broad on purpose: the client raises connection errors, rate limits, refusals
        # and schema violations as unrelated types, and every one of them means the
        # same thing here -- this document produced no reading. Narrowing it would let
        # an unanticipated failure surface as zero candidates, which downstream is
        # indistinguishable from "the page states nothing".
        raise LlmExtractionError(f"the model could not be read: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmExtractionError("the model returned content that is not JSON") from exc

    return _to_candidates(parsed, document, model=settings.model)


def _to_candidates(
    parsed: dict[str, Any], document: NormalizedDocument, *, model: str
) -> ExtractionOutcome:
    """Turn the model's answer into candidates, discarding anything unquotable."""
    blocks = _block_texts(document)
    candidates: list[Candidate] = []
    rejected = 0
    abstentions = 0

    for claim in parsed.get("claims", []):
        if not isinstance(claim, dict):
            rejected += 1
            continue
        try:
            kind = FieldKind(str(claim.get("field_kind", "")))
        except ValueError:
            rejected += 1
            continue

        raw_text = str(claim.get("value_raw_text") or "")
        evidence = str(claim.get("evidence_text") or "")
        reason = str(claim.get("confidence_reason") or "")

        if claim.get("not_stated") is True:
            # An abstention carries no value and needs no quote, so there is nothing
            # to verify -- and nothing that could be fabricated.
            abstentions += 1
            candidates.append(
                Candidate(
                    field_kind=kind,
                    value=None,
                    value_raw_text="",
                    evidence_text=evidence,
                    locator=Locator(kind="block"),
                    confidence=Confidence.LOW,
                    confidence_reason=reason or "the model reported no such value",
                    unresolved_reason="NOT_STATED_ON_PAGE",
                )
            )
            continue

        cited = claim.get("block_index")
        found, block_index = _verified(raw_text, cited if isinstance(cited, int) else None, blocks)
        if not found:
            # The load-bearing line of this module.
            rejected += 1
            continue

        try:
            confidence = Confidence(str(claim.get("confidence", "LOW")))
        except ValueError:
            confidence = Confidence.LOW

        candidates.append(
            Candidate(
                field_kind=kind,
                value=None,
                value_raw_text=raw_text,
                evidence_text=evidence or raw_text,
                locator=Locator(kind="block", block_index=block_index),
                confidence=confidence,
                confidence_reason=reason,
                context={"quote_verified": True, "cited_block": cited},
            )
        )

    return ExtractionOutcome(
        candidates=candidates,
        rejected_unquoted=rejected,
        abstentions=abstentions,
        model=model,
    )


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "REQUESTED_KINDS",
    "ExtractionOutcome",
    "LlmExtractionError",
    "extract",
]
