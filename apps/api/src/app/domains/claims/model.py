"""What a candidate claim is, and what authorises one (Step 5C.2 sections 4-5, 25).

A candidate is **evidence-local**: it says what one extractor found in one region of
one document, and nothing about whether that is true, complete, canonical or
publishable. Everything here is built to keep it that weak.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum

from app.db.enums import SourceCategory
from app.domains.claims.locator import Locator


class FieldKind(StrEnum):
    """Candidate field categories. Extractor outputs, not publication fields."""

    PROGRAM_NAME = "PROGRAM_NAME"
    DEGREE_LEVEL = "DEGREE_LEVEL"
    DURATION = "DURATION"
    STUDY_MODE = "STUDY_MODE"
    CAMPUS = "CAMPUS"
    DISCIPLINE_HINT = "DISCIPLINE_HINT"
    FACULTY_OR_SCHOOL = "FACULTY_OR_SCHOOL"
    ADMISSION_REQUIREMENT = "ADMISSION_REQUIREMENT"
    LANGUAGE_TEST = "LANGUAGE_TEST"
    LANGUAGE_OVERALL_SCORE = "LANGUAGE_OVERALL_SCORE"
    LANGUAGE_COMPONENT_SCORE = "LANGUAGE_COMPONENT_SCORE"
    TUITION = "TUITION"
    APPLICATION_DEADLINE = "APPLICATION_DEADLINE"
    #: A dated calendar entry that is explicitly **not** an application deadline.
    #: The Caltech PDF is registered as `ACADEMIC_CALENDAR`, and converting "Beginning
    #: of instruction" into an admissions deadline would be inventing a fact (§22).
    ACADEMIC_CALENDAR_EVENT = "ACADEMIC_CALENDAR_EVENT"


class Confidence(StrEnum):
    """Interpretable bands with stated reasons (section 25).

    Not a decimal. `0.873421` would imply a calibrated probabilistic model, and there
    is no calibration set behind these rules -- the number would be decoration over a
    judgement, which is worse than the judgement stated plainly.

    **A HIGH band is still not verified and still not publishable.**
    """

    HIGH = "HIGH"
    """An explicitly labelled table cell, or a field whose label is unambiguous."""

    MEDIUM = "MEDIUM"
    """A sentence or list item under a heading that matches the field."""

    LOW = "LOW"
    """A weak contextual match: the right shape of value, thin surrounding context."""


#: Which extractors a source responsibility authorises (section 4). A page claimed
#: only as `UNIVERSITY_HOME` emits nothing: a homepage mentioning "from £28,000" is
#: marketing, and treating it as a fee claim is how a brochure number becomes a
#: published fact.
#:
#: Deliberately narrow. Running every extractor over every document would produce more
#: claims and less signal, and the coverage report would stop meaning anything.
ROUTING: dict[str, frozenset[str]] = {
    SourceCategory.TUITION_FEES.value: frozenset({"tuition"}),
    SourceCategory.LANGUAGE_REQUIREMENTS.value: frozenset({"language"}),
    SourceCategory.APPLICATION_DEADLINES.value: frozenset({"deadline"}),
    SourceCategory.PROGRAM_CATALOG.value: frozenset({"program"}),
    SourceCategory.PROGRAM_PAGE.value: frozenset({"program", "admission", "language"}),
    SourceCategory.ENTRY_REQUIREMENTS.value: frozenset({"admission", "language"}),
    SourceCategory.UNDERGRADUATE_ADMISSIONS.value: frozenset({"admission", "language", "deadline"}),
    SourceCategory.POSTGRADUATE_ADMISSIONS.value: frozenset({"admission", "language", "deadline"}),
    SourceCategory.PHD_ADMISSIONS.value: frozenset({"admission", "language", "deadline"}),
    SourceCategory.ACADEMIC_CALENDAR.value: frozenset({"calendar"}),
    # Explicitly empty rather than absent, so the intent is visible: these pages are
    # fetched and normalised, and no business extractor may read them.
    SourceCategory.UNIVERSITY_HOME.value: frozenset(),
    SourceCategory.FACULTY_OR_SCHOOL.value: frozenset(),
    SourceCategory.OFFICIAL_PDF.value: frozenset(),
    SourceCategory.GOVERNMENT.value: frozenset(),
    SourceCategory.AUTHORIZED_RANKING.value: frozenset(),
    # `UNCLASSIFIED` is not a `SourceCategory` member (D32) and reaches here as a bare
    # string from the workbook. A page with no stated category authorises nothing:
    # guessing what it is for is precisely what D32 refused.
    "UNCLASSIFIED": frozenset(),
}


#: Containers whose text is site chrome rather than the page's answer to anything.
#:
#: Step 5C.1 recorded `Block.container` rather than deleting this text, explicitly so a
#: later extractor could make this decision itself. This is that decision.
#:
#: `aside` and `form` are deliberately absent: a fee table in an aside is real content,
#: and a deadline printed beside an application form is too. What is here is the
#: furniture that repeats on every page of a site.
CHROME_CONTAINERS = frozenset({"nav", "header", "footer", "noscript"})


def is_chrome(block: object) -> bool:
    """Is this block site furniture rather than page content?

    Imperial's navigation menu is one `<li>` containing "Entry requirements",
    "Deadlines", "Fees and funding" and forty other links. Every business rule here
    matched it, and none of them should have: a menu naming a topic is not a statement
    about it.
    """
    return getattr(block, "container", None) in CHROME_CONTAINERS


@dataclass(frozen=True, slots=True)
class Candidate:
    """One candidate claim, before it reaches the database."""

    field_kind: FieldKind
    #: The structured candidate, as far as a deterministic rule goes. `None` is a
    #: legitimate outcome -- see `unresolved_reason`.
    value: dict[str, object] | None
    #: The specific wording that produced the value.
    value_raw_text: str
    #: The surrounding wording a reviewer needs: usually the whole sentence, list item
    #: or row, not just the matched substring.
    evidence_text: str
    locator: Locator
    confidence: Confidence
    confidence_reason: str
    unresolved_reason: str | None = None
    #: Context the rule established from the document itself, never from another page
    #: (section 28) -- a heading, a table column label, a nearby scope phrase.
    context: dict[str, object] = field(default_factory=dict)

    def fingerprint(self, *, extraction_id: str, extractor: str, version: str) -> str:
        """Stable identity for idempotency (section 5).

        Over `(extraction, field kind, locator, extractor, version)` and **not** over
        the value: two official pages stating the same fee are two pieces of evidence
        and must stay two claims (section 26). Including the value would also mean a
        rule change that altered a number produced a *new* claim rather than a second
        version of the same one, which is the opposite of what versioning is for.
        """
        material = json.dumps(
            {
                "extraction_id": extraction_id,
                "field_kind": self.field_kind.value,
                "locator": self.locator.as_json(),
                "extractor": extractor,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def extractors_for(responsibilities: set[str]) -> frozenset[str]:
    """Which extractors the page's responsibilities authorise, together.

    A page claimed for both postgraduate admissions and deadlines may run both sets --
    the responsibilities are a union, because each is a separate statement by the
    collector about what this page answers for.
    """
    allowed: set[str] = set()
    for responsibility in responsibilities:
        allowed |= ROUTING.get(responsibility, frozenset())
    return frozenset(allowed)


__all__ = [
    "CHROME_CONTAINERS",
    "ROUTING",
    "Candidate",
    "Confidence",
    "FieldKind",
    "extractors_for",
    "is_chrome",
]
