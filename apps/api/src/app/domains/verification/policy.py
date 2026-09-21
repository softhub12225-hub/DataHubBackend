"""Which responsibility may publish which field, and why that is not the routing table.

THE DEFECT THIS EXISTS TO CLOSE
===============================
C27's gate is asymmetric. `AUTHORIZED_RANKING` is scoped to ranking entity types and
`AUTHORIZED_EXTERNAL` is scoped to an exact `(entity_type, field_path)` binding --
but `OFFICIAL_VERIFIED`, the class every university page will hold, was checked at
**source level only**. Once a page was eligible, every claim from it was eligible.

The pilot makes that concrete: 385 responsibility claims over 319 URLs, so one URL can
be submitted as both `LANGUAGE_REQUIREMENTS` and `TUITION_FEES`. A reviewer who verifies
the language responsibility and rejects the tuition one has made two decisions, and a
source-level gate honours only the first. The tuition claim would publish on the
strength of the language verification, from the same source, the same snapshot and the
same extraction.

TWO TABLES, NOT ONE
===================
`claims.model.ROUTING` already maps a source category to the extractors allowed to
**read** that page. It is tempting to reuse it here, and wrong: "may this rule read this
page?" and "may this page publish this fact?" are different questions, and one table
answering both is the mistake D39 corrected. Widening extraction to improve recall would
silently widen publication authority.

So `FIELD_AUTHORITY` is its own table. What ties the two together is an **invariant**
rather than a shared definition: publication authority must be a subset of extraction
authority, because a page nobody was allowed to read cannot be a page something may be
published from. `test_publication_authority_is_within_extraction_authority` asserts it.

NOTHING HERE IS INFERRED FROM CONTENT
=====================================
Compatibility is decided by the responsibility the workbook claimed and a reviewer
verified, never by what the extractor happened to find. A page that yielded forty
tuition amounts is not thereby a fees page; that is section 20 of the previous step and
it still holds.
"""

from __future__ import annotations

from enum import StrEnum

from app.db.enums import SourceCategory
from app.domains.claims.model import ROUTING, FieldKind

#: Which extractor produces each field kind. Stated once so the invariant test can
#: compare this module against `ROUTING` without either of them guessing.
FIELD_KIND_EXTRACTOR: dict[str, str] = {
    FieldKind.TUITION.value: "tuition",
    FieldKind.LANGUAGE_TEST.value: "language",
    FieldKind.LANGUAGE_OVERALL_SCORE.value: "language",
    FieldKind.LANGUAGE_COMPONENT_SCORE.value: "language",
    FieldKind.APPLICATION_DEADLINE.value: "deadline",
    FieldKind.ACADEMIC_CALENDAR_EVENT.value: "calendar",
    FieldKind.ADMISSION_REQUIREMENT.value: "admission",
    FieldKind.PROGRAM_NAME.value: "program",
    FieldKind.DEGREE_LEVEL.value: "program",
    FieldKind.DISCIPLINE_HINT.value: "program",
    FieldKind.DURATION.value: "program",
    FieldKind.STUDY_MODE.value: "program",
    FieldKind.CAMPUS.value: "program",
    FieldKind.FACULTY_OR_SCHOOL.value: "program",
}

_ADMISSIONS = frozenset(
    {
        SourceCategory.ENTRY_REQUIREMENTS.value,
        SourceCategory.UNDERGRADUATE_ADMISSIONS.value,
        SourceCategory.POSTGRADUATE_ADMISSIONS.value,
        SourceCategory.PHD_ADMISSIONS.value,
    }
)

#: The admissions pages that may publish a deadline. `ENTRY_REQUIREMENTS` is excluded
#: and the exclusion was not a judgement call: `ROUTING` does not let the deadline
#: extractor read an entry-requirements page at all, so no deadline candidate can ever
#: originate from one and granting publication authority there would authorise something
#: that cannot exist. The subset invariant found it; it was written the other way first.
_ADMISSIONS_WITH_DEADLINES = _ADMISSIONS - {SourceCategory.ENTRY_REQUIREMENTS.value}

#: **The policy.** Candidate field kind -> the source responsibilities that may publish
#: it. Deliberately narrow: a responsibility is added here because someone decided the
#: page type is authoritative for that fact, not because an extractor was allowed to
#: read it.
FIELD_AUTHORITY: dict[str, frozenset[str]] = {
    # A fee is published by a fees page. An admissions page that mentions fees is
    # describing them, and the fees page is where the institution states them.
    FieldKind.TUITION.value: frozenset({SourceCategory.TUITION_FEES.value}),
    # Language requirements are routinely stated on admissions pages as well as on the
    # dedicated page, and the institution means both.
    FieldKind.LANGUAGE_TEST.value: _ADMISSIONS | {SourceCategory.LANGUAGE_REQUIREMENTS.value},
    FieldKind.LANGUAGE_OVERALL_SCORE.value: _ADMISSIONS
    | {SourceCategory.LANGUAGE_REQUIREMENTS.value},
    FieldKind.LANGUAGE_COMPONENT_SCORE.value: _ADMISSIONS
    | {SourceCategory.LANGUAGE_REQUIREMENTS.value},
    # A deadline belongs to the deadlines page, and to admissions pages, which is where
    # most institutions actually publish the closing date.
    FieldKind.APPLICATION_DEADLINE.value: _ADMISSIONS_WITH_DEADLINES
    | {SourceCategory.APPLICATION_DEADLINES.value},
    # An academic calendar entry is NOT a deadline (see `FieldKind`), so its authority
    # is the calendar and nothing else.
    FieldKind.ACADEMIC_CALENDAR_EVENT.value: frozenset({SourceCategory.ACADEMIC_CALENDAR.value}),
    FieldKind.ADMISSION_REQUIREMENT.value: _ADMISSIONS,
    FieldKind.PROGRAM_NAME.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.DEGREE_LEVEL.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.DISCIPLINE_HINT.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.DURATION.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.STUDY_MODE.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.CAMPUS.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
    FieldKind.FACULTY_OR_SCHOOL.value: frozenset(
        {SourceCategory.PROGRAM_CATALOG.value, SourceCategory.PROGRAM_PAGE.value}
    ),
}

#: The canonical `(entity_type, field_path)` pairs each responsibility authorises, which
#: is what the promotion writer records as `source_field_binding` rows and what C27
#: checks. These are the *governed* columns -- the ones whose provenance triggers already
#: refuse an uncited write -- plus the composite fact names those triggers cover (D16: a
#: composite fact carries one status, named for the fact).
PUBLICATION_BINDINGS: dict[str, tuple[tuple[str, str], ...]] = {
    SourceCategory.TUITION_FEES.value: (
        ("tuition", "amount"),
        ("tuition", "amount_kind"),
        ("tuition", "amount_min"),
        ("tuition", "amount_max"),
    ),
    SourceCategory.LANGUAGE_REQUIREMENTS.value: (
        ("language_requirement", "test_type_code"),
        ("language_requirement", "overall_score"),
        ("language_requirement", "subscores"),
    ),
    SourceCategory.APPLICATION_DEADLINES.value: (
        ("application_deadline", "deadline"),
        ("application_deadline", "deadline_text"),
        ("application_round", "opens"),
    ),
    SourceCategory.ACADEMIC_CALENDAR.value: (("application_round", "opens"),),
    SourceCategory.PROGRAM_CATALOG.value: (
        ("program", "name_en"),
        ("program", "name_zh"),
        ("program", "degree_level_code"),
    ),
    SourceCategory.PROGRAM_PAGE.value: (
        ("program", "name_en"),
        ("program", "name_zh"),
        ("program", "degree_level_code"),
        ("language_requirement", "test_type_code"),
        ("language_requirement", "overall_score"),
        ("admission_requirement", "academic_background_text"),
    ),
    SourceCategory.UNIVERSITY_HOME.value: (
        ("university", "name_en"),
        ("university", "name_zh"),
        ("university", "city"),
    ),
}
for _category in _ADMISSIONS:
    PUBLICATION_BINDINGS[_category] = (
        ("admission_requirement", "academic_background_text"),
        ("admission_requirement", "documents"),
        ("language_requirement", "test_type_code"),
        ("language_requirement", "overall_score"),
        ("language_requirement", "subscores"),
        ("application_deadline", "deadline"),
        ("application_deadline", "deadline_text"),
    )


class Blocker(StrEnum):
    """Why an accepted candidate is not promotable (section 24).

    A boolean would say a candidate is not ready and leave a reviewer to work out which
    of eight things to go and do. Every value here names one action.
    """

    NOT_CURRENT = "NOT_CURRENT"
    """Its rule or the document it was read from has been superseded."""

    NOT_ACCEPTED = "NOT_ACCEPTED"
    """No reviewer has accepted the extraction. Confidence is not a decision."""

    SOURCE_NOT_ELIGIBLE = "SOURCE_NOT_ELIGIBLE"
    """The source carries no publication class."""

    DOMAIN_NOT_VERIFIED = "DOMAIN_NOT_VERIFIED"
    """No active, verified `official_domain` covers this host."""

    RESPONSIBILITY_NOT_VERIFIED = "RESPONSIBILITY_NOT_VERIFIED"
    """This page's claim to carry this responsibility has not been verified."""

    RESPONSIBILITY_INCOMPATIBLE = "RESPONSIBILITY_INCOMPATIBLE"
    """A responsibility IS verified, and it does not authorise this field kind."""

    MAPPING_NOT_PROMOTED = "MAPPING_NOT_PROMOTED"
    """The verified mapping has not been promoted onto the acquisition source."""

    SOURCE_SUPERSEDED = "SOURCE_SUPERSEDED"
    """The source was replaced. New claims belong to its successor."""

    SCOPE_UNRESOLVED = "SCOPE_UNRESOLVED"
    """The applicant scope is undetermined, and absence is not UNIVERSAL."""

    CONFLICT_PRESENT = "CONFLICT_PRESENT"
    """Another current candidate disagrees about the same thing."""

    LOCATOR_INVALID = "LOCATOR_INVALID"
    """The locator no longer resolves to the quoted wording."""


def responsibilities_for(field_kind: str) -> frozenset[str]:
    """Which responsibilities may publish this field kind. Unknown kinds authorise none.

    An unknown field kind returning the empty set rather than raising is deliberate: a
    new extractor should be unable to publish until somebody adds it here, and a crash
    in a readiness report is a worse way to learn that than a blocker code.
    """
    return FIELD_AUTHORITY.get(field_kind, frozenset())


def is_compatible(field_kind: str, responsibility: str | None) -> bool:
    """May a source verified for `responsibility` publish a `field_kind` claim?"""
    return bool(responsibility) and responsibility in responsibilities_for(field_kind)


def bindings_for(responsibility: str) -> tuple[tuple[str, str], ...]:
    """The `(entity_type, field_path)` pairs this responsibility authorises."""
    return PUBLICATION_BINDINGS.get(responsibility, ())


def extraction_authority(field_kind: str) -> frozenset[str]:
    """The responsibilities whose ROUTING admits the extractor producing this kind.

    Only used by the invariant test. Publication authority must be a subset of this:
    a page nobody was allowed to read cannot be one something is published from.
    """
    extractor = FIELD_KIND_EXTRACTOR.get(field_kind)
    if extractor is None:
        return frozenset()
    return frozenset(
        category for category, extractors in ROUTING.items() if extractor in extractors
    )


__all__ = [
    "FIELD_AUTHORITY",
    "FIELD_KIND_EXTRACTOR",
    "PUBLICATION_BINDINGS",
    "Blocker",
    "bindings_for",
    "extraction_authority",
    "is_compatible",
    "responsibilities_for",
]
