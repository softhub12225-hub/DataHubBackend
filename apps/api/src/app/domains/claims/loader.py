"""Loading current candidates with everything review and grouping need.

ONE LOADER, SO ONE DEFINITION OF "CURRENT"
==========================================
Every report in Step 5C.3 asks the same question first -- *which candidates are live?* --
and the answer is not obvious: 6,015 superseded rows sit beside 1,937 current ones,
because a corrected rule leaves its earlier output in place as history (D48). Six
reports each writing their own filter is six chances to count history as though it were
live, and the resulting number looks entirely plausible.

So the predicate is written once, here, and it comes from the runner's registry rather
than from a literal.

THE DOCUMENT IS PART OF THE ROW
===============================
Three things a reviewer needs cannot be answered from the candidate row alone:

* whether the block sat in `main`, in a footer, or in nothing at all (section 9);
* whether the evidence is character-identical to a link label, which is how 146
  navigation strings reached the admission candidates (section 10);
* whether the locator still resolves to the wording the claim quotes (sections 23-24).

All three need the normalised document, which lives in the artifact store. Loading it
once per document and answering all three is the difference between one pass over 95
documents and three.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from app.domains.acquisition.storage import EvidenceStore
from app.domains.claims.grouping import CandidateRow
from app.domains.claims.locator import resolve
from app.domains.claims.review import CURRENT_RULE_KEYS, BodyConfirmation, body_confirmation
from app.domains.claims.runner import MIN_USABLE_TEXT, load_document


@dataclass(frozen=True, slots=True)
class LoadedCandidate:
    """A current candidate, plus what only the document could tell us."""

    row: CandidateRow
    institution_id: uuid.UUID
    source_ref: str
    confidence_band: str
    decision_state: str
    scope_unresolved: bool
    source_eligibility: str
    container: str | None
    confirmation: BodyConfirmation
    #: Every link label in the same document, for the chrome identity test.
    link_texts: frozenset[str]
    #: What the locator resolves to, and how that relates to the stored evidence.
    resolved_text: str | None
    evidence_relationship: str
    thin_source: bool

    @property
    def candidate_id(self) -> uuid.UUID:
        return self.row.candidate_id


#: How a locator's resolved text relates to what the claim stored, best first.
#:
#: Section 24 asks for a deterministic relationship rather than byte equality, because
#: normalisation legitimately differs: a deadline's `value_raw_text` is the date match
#: joined to the time match ("13 January 2027 6pm") while the page reads "13 January 2027
#: at 6pm", so the raw text is not a substring of its own evidence. Every token of it is
#: present, which is a provable relationship rather than a hopeful one.
RELATIONSHIPS = (
    "EXACT_RAW",
    "CONTAINS_RAW",
    "ALL_RAW_TOKENS_PRESENT",
    "EQUALS_EVIDENCE",
    "WITHIN_EVIDENCE",
    "MISMATCH",
    "UNRESOLVED",
)


def evidence_relationship(resolved: str | None, raw_text: str, evidence_text: str) -> str:
    """How the resolved text relates to the claim's own wording. Never a guess."""
    if resolved is None:
        return "UNRESOLVED"
    raw, evidence = raw_text.strip(), evidence_text.strip()
    if resolved == raw:
        return "EXACT_RAW"
    if raw and raw in resolved:
        return "CONTAINS_RAW"
    tokens = [token for token in raw.split() if token]
    if tokens and all(token in resolved for token in tokens):
        return "ALL_RAW_TOKENS_PRESENT"
    if resolved.strip() == evidence:
        return "EQUALS_EVIDENCE"
    if evidence and (resolved.strip() in evidence or evidence in resolved.strip()):
        return "WITHIN_EVIDENCE"
    return "MISMATCH"


#: A relationship at or above this rank is deterministic proof that the locator lands on
#: the claim's own wording. Below it, the pointer and the quote have drifted apart.
PROVEN_RELATIONSHIPS = frozenset(
    {"EXACT_RAW", "CONTAINS_RAW", "ALL_RAW_TOKENS_PRESENT", "EQUALS_EVIDENCE", "WITHIN_EVIDENCE"}
)

_SQL = """
    SELECT c.id, c.field_kind, c.value_normalized, c.unresolved_reason, c.confidence_band,
           c.evidence_text, c.value_raw_text, c.locator, c.source_responsibility,
           c.extractor_name, c.extractor_version, c.extraction_id,
           e.document_hash,
           (e.output->'statistics'->>'text_characters')::int AS text_characters,
           s.id AS source_id, s.url, s.publication_eligibility::text AS eligibility,
           pcs.source_ref, pcs.target_institution_id,
           coalesce(ti.match_key, '?') AS institution,
           v.decision_state, v.scope_unresolved
      FROM field_claim_candidate c
      JOIN candidate_review_state v ON v.candidate_id = c.id
      JOIN extraction e ON e.id = c.extraction_id
      JOIN snapshot sn ON sn.id = e.snapshot_id
      JOIN source s ON s.id = sn.source_id
      JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
      LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
     WHERE NOT v.is_superseded
     ORDER BY e.document_hash, c.id
"""


def load_current(
    connection: Connection, artifacts: EvidenceStore, *, resolve_locators: bool = True
) -> list[LoadedCandidate]:
    """Every current candidate, with its document-derived facts.

    Documents are loaded once and cached: 1,937 candidates come from 95 documents, and
    reloading per candidate would read each one twenty times.
    """
    rows = connection.execute(text(_SQL)).all()
    documents: dict[str, Any] = {}
    links: dict[str, frozenset[str]] = {}
    out: list[LoadedCandidate] = []

    for row in rows:
        document = None
        if resolve_locators and row.document_hash:
            if row.document_hash not in documents:
                try:
                    loaded = load_document(artifacts, row.document_hash)
                except (KeyError, OSError, ValueError):
                    loaded = None
                documents[row.document_hash] = loaded
                links[row.document_hash] = (
                    frozenset(link.text.strip() for link in loaded.links if link.text.strip())
                    if loaded is not None
                    else frozenset()
                )
            document = documents[row.document_hash]

        container: str | None = None
        resolved: str | None = None
        if document is not None:
            index = row.locator.get("block_index")
            if (
                row.locator.get("kind") != "link"
                and index is not None
                and index < len(document.blocks)
            ):
                container = document.blocks[index].container
                if document.media_type == "application/pdf":
                    container = container or "pdf"
            resolved = resolve(document, row.locator)

        out.append(
            LoadedCandidate(
                row=CandidateRow(
                    candidate_id=row.id,
                    institution=row.institution,
                    field_kind=row.field_kind,
                    value=row.value_normalized,
                    unresolved_reason=row.unresolved_reason,
                    confidence_band=row.confidence_band,
                    source_id=row.source_id,
                    url=row.url,
                    extraction_id=row.extraction_id,
                    locator=row.locator,
                    responsibility=row.source_responsibility,
                    extractor_name=row.extractor_name,
                    extractor_version=row.extractor_version,
                    evidence_text=row.evidence_text,
                    value_raw_text=row.value_raw_text,
                ),
                institution_id=row.target_institution_id,
                source_ref=row.source_ref,
                confidence_band=row.confidence_band,
                decision_state=row.decision_state,
                scope_unresolved=bool(row.scope_unresolved),
                source_eligibility=row.eligibility,
                container=container,
                confirmation=body_confirmation(row.locator, container),
                link_texts=links.get(row.document_hash or "", frozenset()),
                resolved_text=resolved,
                evidence_relationship=(
                    evidence_relationship(resolved, row.value_raw_text, row.evidence_text)
                    if resolve_locators
                    else "UNRESOLVED"
                ),
                thin_source=(row.text_characters or 0) < MIN_USABLE_TEXT,
            )
        )
    return out


__all__ = [
    "CURRENT_RULE_KEYS",
    "PROVEN_RELATIONSHIPS",
    "RELATIONSHIPS",
    "LoadedCandidate",
    "evidence_relationship",
    "load_current",
]
