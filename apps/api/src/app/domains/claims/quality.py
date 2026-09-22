"""Secondary interpretation of candidates: what shape of thing each one is.

WHAT THIS IS NOT
================
It does not change a candidate, and it does not decide whether one is true. A quality
class is a **review hint**: it tells a reviewer what they are looking at before they
read it, so that 985 admission candidates can be worked through in an order that makes
sense instead of alphabetically.

Sections 10 and 14 are both explicit that these are secondary and that no class may be
promoted automatically -- in particular, "do not automatically promote only TUITION_FEE".
A `COST_OF_ATTENDANCE_COMPONENT` may be exactly the number a student needs.

STRUCTURAL EVIDENCE, NOT SEMANTIC GUESSING
==========================================
Section 10 asks for structural evidence. So every predicate here reads the locator kind,
the heading path, the container the parser recorded, the presence of a requirement
pattern that the extractor itself uses, and the shape of the evidence text. None of them
interprets meaning, and none of them uses an LLM (section 33).

`NAVIGATION_OR_CHROME` exists because C47's fix works on `Block.container`, and the
audit showed exactly what that does and does not catch. It catches everything it tests:
not one of the 985 admission candidates comes from a `nav`, `header`, `footer` or
`noscript` block. And 178 chrome rows still reached `main`, `article` and untagged
blocks -- 146 whose evidence is character-identical to a link label in the same
document, 30 inline `<script>` payloads the normaliser passed through as paragraphs, and
2 navigation menus long enough to saturate the stored evidence. Two of them carry MEDIUM
confidence, so they are indistinguishable by band from a real requirement sentence.

They are classified here, not deleted, and the detection is reported rather than applied
silently. The durable fixes belong upstream -- see the note at the end of this module.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from app.domains.claims.admission import states_requirement


class AdmissionQuality(StrEnum):
    """What kind of thing an `ADMISSION_REQUIREMENT` candidate actually is."""

    REQUIREMENT_TABLE_ROW = "REQUIREMENT_TABLE_ROW"
    """A labelled table cell or row. The strongest structure a page can give."""

    REQUIREMENT_LIST_ITEM = "REQUIREMENT_LIST_ITEM"
    """A list item that states a requirement. Usually one condition per item."""

    EXPLICIT_REQUIREMENT_SENTENCE = "EXPLICIT_REQUIREMENT_SENTENCE"
    """Prose that states a requirement in so many words."""

    QUALIFICATION_EXAMPLE = "QUALIFICATION_EXAMPLE"
    """Names a qualification or grade without stating it as this course's requirement --
    an equivalence table's prose, a worked example, an "we also accept" aside."""

    GENERIC_REQUIREMENT_PROSE = "GENERIC_REQUIREMENT_PROSE"
    """Under a requirements heading, saying nothing that is itself a requirement. The
    841 LOW rows are mostly this, and it is why they are LOW."""

    NAVIGATION_OR_CHROME = "NAVIGATION_OR_CHROME"
    """Site furniture that reached a content block: a link label, an inline script
    payload, or a menu long enough to saturate the stored evidence."""

    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    """Too little evidence, or nowhere in the page, to tell what it is."""


#: Below this, a fragment cannot carry a reviewable requirement. It is the extractor's
#: own floor (`_MIN_REQUIREMENT_CHARS`), repeated here so the report and the rule agree
#: on what "too short to judge" means.
MIN_REVIEWABLE_CHARS = 25

#: `evidence_text` is truncated on insert. A row at exactly this length is saturated,
#: which in this corpus only ever happened to two 30,000-character navigation menus.
EVIDENCE_TRUNCATION = 4000

#: Inline script and JSON payloads that the HTML normaliser passed through as paragraph
#: text. Deliberately narrow: adding bare `function`, `const` or `let` pulls in real
#: requirement prose ("...please contact the admissions team, who will l[et]...").
_SCRIPT_PAYLOAD = re.compile(
    r"(?:^|[^A-Za-z])(?:var|typeof)(?:[^A-Za-z]|$)"
    r"|document\.|window\.|=>|CDATA|querySelector|JSON\.parse|dataLayer|\$\("
    r"|^\{\"",
)

#: Heading paths that name a qualification system. The usable `QUALIFICATION_EXAMPLE`
#: signal, and independent of `qualification_hint`, which is nearly orthogonal to it.
_QUALIFICATION_HEADING = re.compile(
    r"A[-\s]?levels?|AS[-\s]?levels?|GCSEs?|IGCSE|International\s+Baccalaureate|IBDP"
    r"|BTEC|Advanced\s+Placement|Gaokao|Abitur|HKDSE|CEGEP|Resits?|equivalenc(?:y|ies)"
    r"|Access\s+to\s+HE|high\s+school\s+diploma|Cambridge\s+Pre-?U|Scottish\s+Highers?"
    r"|Extended\s+Project|Foundation\s+Year|qualification",
    re.I,
)

_SENTENCE_TERMINATOR = re.compile(r"[.!?]")

#: The value hints the rule extracts. A row with none of them and no sentence carries
#: nothing to judge.
_VALUE_HINTS = ("qualification_hint", "degree_level_hint", "degree_level_raw")


def looks_like_chrome(
    evidence_text: str, *, link_texts: frozenset[str] = frozenset()
) -> tuple[bool, str]:
    """Is this site furniture that reached a content block? (reason included.)

    Three disjoint tests, each validated against the real corpus:

    1. an inline script or JSON payload the normaliser passed through as a paragraph;
    2. a saturated `evidence_text`, which only ever happened to two navigation menus;
    3. evidence that is character-identical to a link label in the same document.

    The third finds 146 of the 178 and needs the document, which is why this takes
    `link_texts`. Without them the test still runs, and reports 32 instead of 178 --
    the caller is expected to supply them, and the report says when it could not.
    """
    text = evidence_text.strip()
    if _SCRIPT_PAYLOAD.search(text):
        return True, "an inline script or JSON payload read as paragraph text"
    if len(evidence_text) >= EVIDENCE_TRUNCATION:
        return True, (
            f"evidence saturated at {EVIDENCE_TRUNCATION} characters; in this corpus "
            "only navigation menus reach that length"
        )
    if text and text in link_texts:
        return True, "character-identical to a link label in the same document"
    return False, ""


def classify_admission(
    *,
    locator: dict[str, Any],
    evidence_text: str,
    value: dict[str, Any] | None,
    link_texts: frozenset[str] = frozenset(),
) -> tuple[AdmissionQuality, str]:
    """(class, the structural reason for it). The classes partition.

    Ordered, first match wins. Chrome first because a menu is a menu whatever words it
    contains; then the qualification examples, which are defined by *not* stating a
    requirement; then the two requirement classes; then what is left.
    """
    text = evidence_text.strip()
    headings = locator.get("heading_path") or []
    heading_text = " >> ".join(str(part) for part in headings) if isinstance(headings, list) else ""
    values = value or {}

    is_chrome, why = looks_like_chrome(evidence_text, link_texts=link_texts)
    if is_chrome:
        return AdmissionQuality.NAVIGATION_OR_CHROME, why

    states = states_requirement(text)

    if not states and _QUALIFICATION_HEADING.search(heading_text):
        match = _QUALIFICATION_HEADING.search(heading_text)
        assert match is not None
        return (
            AdmissionQuality.QUALIFICATION_EXAMPLE,
            f"heading names a qualification system ({match.group(0)!r}) and the wording "
            "states no requirement of its own",
        )
    if locator.get("kind") == "table_cell":
        # Unreachable today: `admission.extract` builds units only from list items and
        # paragraph/quote blocks, so no requirement candidate has ever come from a
        # table. Kept, and reported as zero, because "we looked and there are none" is
        # a different statement from "we never looked".
        return AdmissionQuality.REQUIREMENT_TABLE_ROW, f"a table cell under {heading_text!r}"
    if states and locator.get("kind") == "list_item":
        return (
            AdmissionQuality.REQUIREMENT_LIST_ITEM,
            "a list item whose wording states a requirement",
        )
    if states:
        return (
            AdmissionQuality.EXPLICIT_REQUIREMENT_SENTENCE,
            "a prose block whose wording states a requirement",
        )
    if (
        not _SENTENCE_TERMINATOR.search(text)
        and not any(values.get(name) for name in _VALUE_HINTS)
        and not values.get("applicant_scopes")
    ):
        return (
            AdmissionQuality.INSUFFICIENT_CONTEXT,
            f"a bare fragment of {len(text)} characters: no sentence, no qualification, "
            "no degree level, no applicant scope",
        )
    return (
        AdmissionQuality.GENERIC_REQUIREMENT_PROSE,
        "under a requirements heading, sentence-shaped or carrying an extracted hint, "
        "but the wording states no requirement",
    )


# ===========================================================================
# 14. Tuition financial context
# ===========================================================================


class FinancialContext(StrEnum):
    """What kind of money a `TUITION` candidate is about.

    A secondary hint. Section 14 is explicit that only `TUITION_FEE` must not be
    promoted: a cost-of-attendance total is frequently the number a student actually
    needs, and discarding it because it is not strictly tuition would answer a different
    question from the one they asked.
    """

    TUITION_FEE = "TUITION_FEE"
    OTHER_MANDATORY_FEE = "OTHER_MANDATORY_FEE"
    COST_OF_ATTENDANCE_COMPONENT = "COST_OF_ATTENDANCE_COMPONENT"
    HOUSING = "HOUSING"
    MEAL_PLAN = "MEAL_PLAN"
    ESTIMATED_TOTAL_COST = "ESTIMATED_TOTAL_COST"
    UNKNOWN_FINANCIAL_AMOUNT = "UNKNOWN_FINANCIAL_AMOUNT"


#: (class, pattern), in precedence order. The first match wins, so the more specific
#: kinds of money come first: "housing and food" is housing, not a generic component,
#: and "estimated total cost of attendance" is a total rather than one of its parts.
#: `(?<![A-Za-z])` and `(?![A-Za-z])` rather than `\b`, because these labels have to be
#: found inside a table that collapsed to one line. Harvard's reads
#: "Tuition$56,550Fees$5,126Housing$12,922Food$8,268", and there is no word boundary
#: before "Housing": the character before it is a digit, and a digit is a word character.
#: With `\b` the only label that ever matched was the one starting the string, so every
#: amount in the run was classified as tuition.
_FINANCIAL_PATTERNS: tuple[tuple[FinancialContext, re.Pattern[str]], ...] = (
    (
        FinancialContext.MEAL_PLAN,
        re.compile(
            r"(?<![A-Za-z])meal\s*plan(?![A-Za-z])|(?<![A-Za-z])board(?![A-Za-z])|(?<![A-Za-z])dining(?![A-Za-z])|(?<![A-Za-z])food(?![A-Za-z])",
            re.I,
        ),
    ),
    (
        FinancialContext.HOUSING,
        re.compile(
            r"(?<![A-Za-z])housing(?![A-Za-z])|(?<![A-Za-z])accommodation(?![A-Za-z])|(?<![A-Za-z])residence\s+hall(?![A-Za-z])|(?<![A-Za-z])hall\s+of\s+residence(?![A-Za-z])"
            r"|(?<![A-Za-z])rent(?![A-Za-z])|(?<![A-Za-z])dormitor|(?<![A-Za-z])lodging(?![A-Za-z])|(?<![A-Za-z])living\s+arrangement(?![A-Za-z])",
            re.I,
        ),
    ),
    (
        FinancialContext.ESTIMATED_TOTAL_COST,
        re.compile(
            r"(?<![A-Za-z])total\s+cost(?![A-Za-z])|(?<![A-Za-z])cost\s+of\s+attendance(?![A-Za-z])|(?<![A-Za-z])student\s+budget(?![A-Za-z])"
            r"|(?<![A-Za-z])total\s+(?:estimated\s+)?(?:expenses|budget)\b|(?<![A-Za-z])net\s+cost(?![A-Za-z])"
            r"|(?<![A-Za-z])billed\s+costs?\b|(?<![A-Za-z])estimated\s+(?:total|cost)\b",
            re.I,
        ),
    ),
    (
        FinancialContext.TUITION_FEE,
        re.compile(
            r"(?<![A-Za-z])tuition(?![A-Za-z])|(?<![A-Za-z])course\s+fees?\b|(?<![A-Za-z])programme?\s+fees?\b",
            re.I,
        ),
    ),
    (
        FinancialContext.OTHER_MANDATORY_FEE,
        re.compile(
            r"(?<![A-Za-z])compulsory(?![A-Za-z])|(?<![A-Za-z])mandatory(?![A-Za-z])|(?<![A-Za-z])student\s+services(?![A-Za-z])|(?<![A-Za-z])amenities\s+fee(?![A-Za-z])"
            r"|(?<![A-Za-z])registration\s+fee(?![A-Za-z])|(?<![A-Za-z])application\s+fee(?![A-Za-z])|(?<![A-Za-z])college\s+fee(?![A-Za-z])"
            r"|(?<![A-Za-z])administration\s+fee(?![A-Za-z])|(?<![A-Za-z])health\s+(?:insurance|cover)\b|(?<![A-Za-z])SSAF(?![A-Za-z])",
            re.I,
        ),
    ),
    (
        FinancialContext.COST_OF_ATTENDANCE_COMPONENT,
        re.compile(
            r"(?<![A-Za-z])books?\b|(?<![A-Za-z])supplies(?![A-Za-z])|(?<![A-Za-z])transport|(?<![A-Za-z])travel(?![A-Za-z])|(?<![A-Za-z])personal\s+expenses(?![A-Za-z])"
            r"|(?<![A-Za-z])living\s+(?:costs?|expenses)\b|(?<![A-Za-z])insurance(?![A-Za-z])|(?<![A-Za-z])miscellaneous(?![A-Za-z])",
            re.I,
        ),
    ),
)


def classify_financial_context(
    *,
    locator: dict[str, Any],
    evidence_text: str,
    value_raw_text: str,
    value: dict[str, Any] | None,
) -> tuple[FinancialContext, str]:
    """(class, the wording that supports it).

    Consulted in order of how specific each thing is about **this amount**:

    1. a table's column label, then its row label -- the publisher labelled the cell;
    2. the wording, by **proximity** to the amount itself;
    3. the heading path, deepest first.

    Getting that order wrong put Berkeley's housing line under "Cost of Attendance"
    because the page heading was read before the sentence, and taking the first pattern
    match in a sentence rather than the nearest read Harvard's "Tuition$56,550Fees..."
    as a meal plan, because "Food" appears later in the same run of text.

    `UNKNOWN_FINANCIAL_AMOUNT` is a real answer: the page showed a number and did not
    say what it was for.
    """
    inner = (value or {}).get("_context")
    context = inner if isinstance(inner, dict) else {}

    # 1. Labels the publisher attached to the cell.
    for name, label in (
        ("column label", context.get("column_label")),
        ("row label", (value or {}).get("row_label")),
    ):
        if isinstance(label, str) and label.strip():
            found = _first_match(label)
            if found is not None:
                kind, matched = found
                return kind, f"{name} contains {matched!r}"

    # 2. The wording, by proximity to the amount.
    nearest = _nearest_match(evidence_text, value_raw_text)
    if nearest is not None:
        kind, matched, distance = nearest
        return kind, f"{matched!r} is the nearest label in the wording ({distance} chars away)"

    # 3. The headings, deepest first: the innermost section is about less of the page.
    headings = locator.get("heading_path") or []
    if isinstance(headings, list):
        for part in reversed(headings):
            found = _first_match(str(part))
            if found is not None:
                kind, matched = found
                return kind, f"heading {str(part)[:40]!r} contains {matched!r}"

    return (
        FinancialContext.UNKNOWN_FINANCIAL_AMOUNT,
        f"nothing in the labels, headings or wording says what {value_raw_text!r} is for",
    )


def _first_match(wording: str) -> tuple[FinancialContext, str] | None:
    """The most specific class this wording names, by pattern precedence."""
    for kind, pattern in _FINANCIAL_PATTERNS:
        match = pattern.search(wording)
        if match:
            return kind, match.group(0)
    return None


def _nearest_match(
    evidence_text: str, value_raw_text: str
) -> tuple[FinancialContext, str, int] | None:
    """The class whose label sits closest to the amount in the wording.

    A cost-of-attendance table that collapsed to one line names every class at once, and
    the one that belongs to a given number is the one beside it. Ties break by pattern
    precedence, so "housing and food" is a meal plan rather than housing.
    """
    anchor = evidence_text.find(value_raw_text.strip()) if value_raw_text.strip() else -1
    if anchor < 0:
        # The raw text is a join of two matches (a date and a time, an amount range), so
        # it is not a literal substring. Fall back to the start of the wording.
        anchor = 0

    best: tuple[int, int, FinancialContext, str] | None = None
    for precedence, (kind, pattern) in enumerate(_FINANCIAL_PATTERNS):
        for match in pattern.finditer(evidence_text):
            distance = (
                0
                if match.start() <= anchor <= match.end()
                else min(abs(anchor - match.start()), abs(anchor - match.end()))
            )
            entry = (distance, precedence, kind, match.group(0))
            if best is None or entry[:2] < best[:2]:
                best = entry
    if best is None:
        return None
    distance, _, kind, matched = best
    return kind, matched, distance


__all__ = [
    "EVIDENCE_TRUNCATION",
    "MIN_REVIEWABLE_CHARS",
    "AdmissionQuality",
    "FinancialContext",
    "classify_admission",
    "classify_financial_context",
    "looks_like_chrome",
]


# ===========================================================================
# What this module works around, and where the durable fixes belong
# ===========================================================================
#
# Classifying chrome here is the right thing for Step 5C.3, which is about preparing a
# review queue. It is not the right place for the fix, and two upstream changes would
# remove these rows from every extractor at once rather than labelling them in one:
#
# 1. **Inline `<script>` text reaches paragraph blocks.** 30 admission candidates are
#    JavaScript. Dropping `<script>` and `<style>` subtrees in the HTML normaliser is a
#    Step 5C.1 change; it would alter every document hash, so it belongs with a planned
#    re-extraction rather than being slipped in here.
# 2. **A link label rendered as a paragraph is indistinguishable from prose.** The
#    normaliser already has `document.links` in hand when it builds blocks, so the
#    identity test is cheap at extraction time and needs no lexicon. 146 rows, including
#    the two MEDIUM ones.
#
# Both are recorded as blockers rather than done here, because re-extracting 175
# documents supersedes every candidate in the table and that is a decision to take
# deliberately, not as a side effect of a review-tooling step.
