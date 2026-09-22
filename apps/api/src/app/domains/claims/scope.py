"""Applicant-scope reconciliation: proposals, never resolutions (sections 12-13).

WHAT THIS REFUSES TO DO
=======================
Section 12 is unambiguous: **do not infer `scope = UNIVERSAL` because no specific
country was named.** A requirement whose scope nobody stated applies to whoever the
university meant, and the page did not say. Asserting universality from silence is how a
rule written for A-level applicants becomes a Chinese applicant's requirement.

So nothing here resolves a scope. It *proposes*: a raw wording, a country hint, a
qualification-system hint, and a resolution status that is honest about how far it got.

THREE THINGS KEPT SEPARATE
==========================
Section 13 asks for `raw_scope_text`, a country hint, a qualification hint and a
nullable `applicant_scope_id`, kept apart. They are different claims about different
things:

* a **country** hint says who the applicant is;
* a **qualification-system** hint says what they studied, which is not the same thing --
  an International Baccalaureate candidate may be of any nationality, and mapping IB to
  a country would invent a nationality the page never mentioned;
* the **scope id** is a reference to the client's taxonomy, and there is nothing to
  point at yet.

WHERE THE WORDING IS LOOKED FOR
===============================
The evidence text, and then the heading path. The heading path is not an afterthought:
the audit found scope wording in the headings of 146 currently-unresolved candidates
against 102 with any scope token in their prose, and the headings are the unambiguous
ones -- "Domestic student eligibility", "Entry requirements > International Baccalaureate
> IBDP", "Apply > Admission Information for: > International Students". The extractor
already reads the heading path for degree level and did not for scope.

Which of the two it came from is recorded, because "the sentence said so" and "the
section it sits under said so" are different strengths of evidence and a reviewer needs
to know which one they are checking.

NOTHING IS WRITTEN
==================
A proposal is computed on read. The stored candidate is untouched -- it is append-only,
and a scope guessed at review time is not evidence about the page.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app.domains.claims.grouping import CandidateRow


class ScopeResolution(StrEnum):
    """How far a deterministic mapper got."""

    RESOLVED = "RESOLVED"
    """An explicit scope, and an `applicant_scope_id` to point it at. Impossible today:
    the only applicant scope that exists is `UNIVERSAL`, which section 12 forbids
    inferring."""

    PARTIALLY_RESOLVED = "PARTIALLY_RESOLVED"
    """An explicit country or qualification system, with no id to map it to."""

    UNRESOLVED = "UNRESOLVED"
    """The page stated no scope. **Not** universal -- unknown."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """This field kind does not carry an applicant scope at all."""


#: Field kinds that can meaningfully carry an applicant scope. A programme's name has no
#: applicant scope, and reporting one as unresolved would inflate the unresolved count
#: with rows that were never going to have one.
SCOPED_FIELD_KINDS: frozenset[str] = frozenset(
    {
        "ADMISSION_REQUIREMENT",
        "APPLICATION_DEADLINE",
        "TUITION",
        "LANGUAGE_TEST",
        "LANGUAGE_OVERALL_SCORE",
        "LANGUAGE_COMPONENT_SCORE",
    }
)

#: Country adjectives and names the extractor's marker list does not cover, mapped to
#: ISO codes. Only forms that name a country unambiguously: "Welsh" and "Scottish" are
#: absent on purpose, because on a UK university's page they describe a *qualification
#: system* rather than an applicant's nationality.
#:
#: **Case is per-alternative, not per-pattern.** A NAME is case-insensitive; an ACRONYM
#: is not, because `\bUSA?\b` under `re.I` matches the pronoun "us" and once asserted a
#: United States scope on pages reading "contact us". Writing the whole pattern
#: lowercase and dropping `re.I` fixed the acronym and broke every name: "China",
#: "Chinese" and "Hong Kong" matched nothing at all.
_COUNTRY_FORMS: tuple[tuple[str, str], ...] = (
    ("CN", r"(?i:\bchin(?:a|ese)\b)|\bPRC\b"),
    ("HK", r"(?i:\bhong\s*kong\b)|\bHKSAR\b"),
    ("TW", r"(?i:\btaiwan(?:ese)?\b)"),
    ("IN", r"(?i:\bindian?\b)"),
    ("SG", r"(?i:\bsingapore(?:an)?\b)"),
    ("MY", r"(?i:\bmalaysia(?:n)?\b)"),
    ("US", r"(?i:\bunited\s+states\b|\bamerican\b)|\bUSA?\b(?!\w)"),
    ("CA", r"(?i:\bcanad(?:a|ian)\b)"),
    ("AU", r"(?i:\baustralian?\b)"),
    ("NZ", r"(?i:\bnew\s+zealand(?:er)?\b)"),
    ("GB", r"(?i:\bunited\s+kingdom\b|\bbritish\b)|\bUK\b(?!\w)"),
    ("JP", r"(?i:\bjapan(?:ese)?\b)"),
    ("KR", r"(?i:\b(?:south\s+)?korean?\b)"),
    ("VN", r"(?i:\bvietnam(?:ese)?\b)"),
    ("ID", r"(?i:\bindonesian?\b)"),
    ("TH", r"(?i:\bthailand\b|\bthai\b)"),
)

#: Qualification systems. A system is not a nationality: an International Baccalaureate
#: candidate may hold any passport, and mapping IB to a country would invent one.
_QUALIFICATION_FORMS: tuple[tuple[str, str], ...] = (
    # Capital A required: "at a level of 7.0" is not an A-level.
    ("A_LEVEL", r"\bA[-\s]?[Ll]evels?\b|\bAS[-\s]?[Ll]evels?\b"),
    ("IB", r"(?i:\binternational\s+baccalaureate\b)|\bIBDP?\b"),
    ("GAOKAO", r"(?i:\bgaokao\b|\bnational\s+college\s+entrance\b)"),
    ("AP", r"(?i:\badvanced\s+placement\b)|\bAP\s+exams?\b"),
    ("SAT_ACT", r"\bSAT\b|\bACT\b"),
    ("GCSE", r"\bI?GCSEs?\b"),
    ("ABITUR", r"(?i:\babitur\b)"),
    ("CBSE", r"\bCBSE\b|\bISC\b|\bCISCE\b"),
    ("HKDSE", r"\bHKDSE\b"),
    ("STPM", r"\bSTPM\b"),
    # "higher" is an ordinary English word, so the qualification needs the form the
    # system is actually written in: capitalised, or qualified by Scottish/Advanced.
    ("SCOTTISH_HIGHERS", r"\b(?:Advanced|Scottish)\s+[Hh]ighers?\b|\bHighers\b"),
    ("BTEC", r"\bBTEC\b"),
    ("CEGEP", r"\bCEGEP\b|\bC\u00c9GEP\b"),
    ("FOUNDATION", r"(?i:\bfoundation\s+(?:year|programme|program)\b)"),
    ("HIGH_SCHOOL_DIPLOMA", r"(?i:\bhigh\s+school\s+diploma\b)"),
)

#: Applicant categories a university states. They are scope statements -- "international
#: students" names who a requirement is for -- but they are not countries, and turning
#: "international" into a list of every country but one would be an invention.
_CATEGORY_FORMS: tuple[tuple[str, str], ...] = (
    ("INTERNATIONAL", r"(?i:\binternational\b)"),
    ("DOMESTIC", r"(?i:\bdomestic\b)"),
    ("HOME", r"(?i:\bhome\s+(?:student|fee|applicant)s?\b)"),
    ("OVERSEAS", r"(?i:\boverseas\b)"),
    ("EU", r"\bEU\b|(?i:\beuropean\s+union\b)"),
    ("LOCAL", r"(?i:\blocal\b)"),
    ("NON_LOCAL", r"(?i:\bnon-?local\b)"),
    ("TRANSFER", r"(?i:\btransfer\s+(?:student|applicant)s?\b)"),
    ("MATURE", r"(?i:\bmature\s+(?:student|applicant)s?\b)"),
)

_COUNTRIES = tuple((code, re.compile(pattern)) for code, pattern in _COUNTRY_FORMS)
_QUALIFICATIONS = tuple((code, re.compile(pattern)) for code, pattern in _QUALIFICATION_FORMS)
_CATEGORIES = tuple((code, re.compile(pattern)) for code, pattern in _CATEGORY_FORMS)


@dataclass(frozen=True, slots=True)
class ScopeProposal:
    """What a deterministic mapper can say about who a claim applies to.

    A proposal, not a resolution. `applicant_scope_id` is nullable and is null for
    every candidate in this corpus -- see `ScopeResolution.RESOLVED`.
    """

    resolution: ScopeResolution
    #: The wording that carried the scope, verbatim.
    raw_scope_text: str | None = None
    country_hints: frozenset[str] = field(default_factory=frozenset)
    qualification_hints: frozenset[str] = field(default_factory=frozenset)
    category_hints: frozenset[str] = field(default_factory=frozenset)
    #: `evidence` or `heading`: how strong the statement is. Absent when unresolved.
    evidence_source: str | None = None
    #: A reference into the client's taxonomy. Null until that taxonomy exists.
    applicant_scope_id: uuid.UUID | None = None

    def describe(self) -> str:
        parts = [self.resolution.value]
        if self.country_hints:
            parts.append(f"country={','.join(sorted(self.country_hints))}")
        if self.qualification_hints:
            parts.append(f"qualification={','.join(sorted(self.qualification_hints))}")
        if self.category_hints:
            parts.append(f"category={','.join(sorted(self.category_hints))}")
        if self.evidence_source:
            parts.append(f"from={self.evidence_source}")
        return "  ".join(parts)


def _hints(text: str) -> tuple[frozenset[str], frozenset[str], frozenset[str], str | None]:
    """(countries, qualifications, categories, the first matched wording)."""
    countries = {code for code, pattern in _COUNTRIES if pattern.search(text)}
    qualifications = {code for code, pattern in _QUALIFICATIONS if pattern.search(text)}
    categories = {code for code, pattern in _CATEGORIES if pattern.search(text)}
    matched: str | None = None
    for _, pattern in (*_COUNTRIES, *_QUALIFICATIONS, *_CATEGORIES):
        found = pattern.search(text)
        if found:
            matched = found.group(0)
            break
    return frozenset(countries), frozenset(qualifications), frozenset(categories), matched


def propose_scope(row: CandidateRow) -> ScopeProposal:
    """What a deterministic mapper can say about this candidate's applicant scope.

    The extractor's own markers first, because those are already stored on the row and a
    review-time proposal that disagreed with them would be confusing. Then the evidence
    wording, then the heading path -- and the proposal records which, because a scope
    the sentence stated and a scope its section heading implied are different strengths
    of evidence.
    """
    if row.field_kind not in SCOPED_FIELD_KINDS:
        return ScopeProposal(resolution=ScopeResolution.NOT_APPLICABLE)

    stored = (row.value or {}).get("applicant_scopes")
    if isinstance(stored, list) and stored:
        countries = frozenset(
            str(entry["applicant_country_code"])
            for entry in stored
            if isinstance(entry, dict) and entry.get("applicant_country_code")
        )
        raw = ", ".join(str(entry.get("scope_raw")) for entry in stored if isinstance(entry, dict))
        _, qualifications, categories, _ = _hints(raw)
        return ScopeProposal(
            resolution=ScopeResolution.PARTIALLY_RESOLVED,
            raw_scope_text=raw or None,
            country_hints=countries,
            qualification_hints=qualifications,
            category_hints=categories,
            evidence_source="extractor",
            # Null, and not for want of trying: see `ScopeResolution.RESOLVED`.
            applicant_scope_id=None,
        )

    for source_name, text in (
        ("evidence", row.evidence_text),
        ("heading", " >> ".join(str(part) for part in (row.locator.get("heading_path") or []))),
    ):
        if not text:
            continue
        countries, qualifications, categories, matched = _hints(text)
        if countries or qualifications or categories:
            return ScopeProposal(
                resolution=ScopeResolution.PARTIALLY_RESOLVED,
                raw_scope_text=matched,
                country_hints=countries,
                qualification_hints=qualifications,
                category_hints=categories,
                evidence_source=source_name,
                applicant_scope_id=None,
            )

    # Unresolved, and that is the answer. Not universal.
    return ScopeProposal(resolution=ScopeResolution.UNRESOLVED)


__all__ = [
    "SCOPED_FIELD_KINDS",
    "ScopeProposal",
    "ScopeResolution",
    "propose_scope",
]
