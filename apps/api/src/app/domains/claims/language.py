"""Language-requirement candidates (Step 5C.2 sections 11-12).

THE RULE THAT MATTERS MOST
=========================
*"English proficiency required"* must produce **no score claim**. A page that discusses
English without giving a number has told us something, and it is not a number; turning
it into one is the single most damaging thing an extractor here could do, because a
fabricated 6.5 is indistinguishable from a real one downstream.

So a score claim exists only where a number appears next to a test name.

OPERATOR SEMANTICS ARE PRESERVED
================================
"at least 7.0", "7.0 or above", "minimum 7.0" and "above 7.0" do not all mean the same
thing, and flattening them to `7.0` loses the difference between a student with exactly
7.0 being admissible and not. The operator is recorded (`AT_LEAST` / `ABOVE` / `EXACT`),
and "no component below 6.5" becomes a component minimum with `AT_LEAST`, not a
component score of 6.5.
"""

# ruff: noqa: RUF001 -- the non-ASCII characters in the patterns below are
# deliberate. Real university pages write fee ranges with EN DASH and possessives
# with RIGHT SINGLE QUOTATION MARK; a pattern matching only the ASCII hyphen and
# apostrophe would silently miss those pages, which is the bug this rule would
# otherwise talk us into.

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.domains.claims.locator import Locator, heading_path_for
from app.domains.claims.model import Candidate, Confidence, FieldKind, is_chrome

#: Tests with a known score scale, so an implausible value is detectable. Unknown test
#: names are preserved rather than dropped (section 11) -- there is no list of every
#: English test a university might accept, and dropping the ones we do not know would
#: silently narrow the fleet.
KNOWN_TESTS: dict[str, tuple[float, float]] = {
    "IELTS": (0.0, 9.0),
    "TOEFL": (0.0, 120.0),
    "PTE": (10.0, 90.0),
    "DUOLINGO": (10.0, 160.0),
    "CAE": (80.0, 230.0),
    "CPE": (80.0, 230.0),
}

#: The lowest *overall* score a university plausibly publishes as a requirement.
#:
#: `KNOWN_TESTS` bounds the scale; this bounds what a requirement can be. The audit
#: found "IELTS 1.0", "TOEFL 1.0" and "TOEFL 3.0" recorded as overall requirements --
#: all of them fragments of institution codes and dates sitting near a test name on
#: UCL's and Cambridge's pages. Every one is inside the test's scale and none is a
#: requirement anybody published.
#:
#: This refuses a claim rather than inventing one, which is the safe direction: the
#: cost of being wrong is a missed requirement a reviewer can add, not a fabricated
#: one a student acts on. Component scores are deliberately not floored here, because
#: a TOEFL component requirement of 21 is ordinary.
OVERALL_REQUIREMENT_FLOORS: dict[str, float] = {
    "IELTS": 4.0,
    "TOEFL": 30.0,
    "PTE": 30.0,
    "DUOLINGO": 50.0,
    "CAE": 140.0,
    "CPE": 140.0,
}

_TEST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("IELTS", re.compile(r"\bIELTS\b", re.I)),
    ("TOEFL", re.compile(r"\bTOEFL\b(?:\s*iBT)?", re.I)),
    ("PTE", re.compile(r"\bPTE\b(?:\s*Academic)?", re.I)),
    ("DUOLINGO", re.compile(r"\bDuolingo\b(?:\s*English\s*Test)?", re.I)),
    ("CAE", re.compile(r"\bC1\s+Advanced\b|\bCAE\b", re.I)),
    ("CPE", re.compile(r"\bC2\s+Proficiency\b|\bCPE\b", re.I)),
)

#: A number, with the comparison word that governs it. Ordered longest-first so
#: "no less than" is not matched as "less than".
_SCORE = re.compile(
    r"(?P<op>at\s+least|no\s+less\s+than|not\s+less\s+than|minimum\s+of|minimum|"
    r"min\.?|or\s+above|or\s+higher|or\s+better|above|over|greater\s+than|"
    r"of\s+at\s+least)?"
    r"\s*(?P<value>\d{1,3}(?:\.\d{1,2})?)",
    re.I,
)

#: "no component below 6.5", "no less than 6.5 in each", "minimum 6.5 in all components"
_COMPONENT_MINIMUM = re.compile(
    r"(?:no|not)\s+(?:sub-?score|component|band|section|element)?\s*"
    r"(?:score\s+)?(?:below|less\s+than|lower\s+than|under)\s*"
    r"(?P<value>\d{1,3}(?:\.\d{1,2})?)"
    r"|(?:minimum|min\.?|at\s+least)\s*(?P<value2>\d{1,3}(?:\.\d{1,2})?)\s*"
    r"(?:in|for)\s+(?:each|every|all|any)\s*(?:component|band|section|skill)?",
    re.I,
)

#: A named component with its own score: "Writing 6.5", "Speaking: 7.0".
_NAMED_COMPONENT = re.compile(
    r"\b(?P<component>reading|writing|listening|speaking)\b\s*[:–—-]?\s*"
    r"(?P<value>\d{1,3}(?:\.\d{1,2})?)",
    re.I,
)

_OVERALL_HINT = re.compile(r"\boverall\b|\btotal\b|\bcomposite\b", re.I)

#: A table header that labels a score column. Includes the component vocabulary: a
#: column headed "Each component" is a labelled score column, and leaving it out of
#: this pattern while the component check still looked for it meant such a column was
#: silently dropped rather than classified.
_SCORE_COLUMN = re.compile(
    r"overall|score|band|total|minimum|requirement|component|section|skill|sub-?score|each",
    re.I,
)

#: Of those, the ones that label a *per-component* score. Checked only after
#: _OVERALL_HINT has been ruled out, because "Overall band score" contains "band" and
#: is emphatically not a component.
_COMPONENT_COLUMN = re.compile(r"component|band|section|each|skill|sub-?score", re.I)

#: Wording that discusses English without giving a requirement. Present so the
#: no-claim path is explicit and testable rather than incidental.
_NO_NUMBER = re.compile(r"english\s+(?:language\s+)?(?:proficiency|requirement|ability)\b", re.I)

_HEADING_HINT = re.compile(r"english|language|proficiency|ielts|toefl", re.I)

EXTRACTOR = "language-rule-extractor"
#: Version 3. Both bumps came from auditing real pages:
#:
#: * v1 -> v2: site navigation was read as page content, so a menu item naming IELTS
#:   became a claim that the page accepts IELTS.
#: * v2 -> v3: any number on the test's scale could be an overall requirement, so
#:   institution-code and date fragments near a test name became "IELTS 1.0",
#:   "TOEFL 1.0" and "TOEFL 3.0". See `OVERALL_REQUIREMENT_FLOORS`.
#: * v3 -> v4: a number embedded in a longer token was read as a score, so Toronto's
#:   postcode "M5R 0A3" became IELTS 5.0 and the ZIP "08541-6151" became TOEFL 85.
#:   Both are plausible scores, so no range check could have caught them. See
#:   `_standalone`.
#: * v4 -> v5: a comma was not treated as coordination, so in "7.0 in IELTS, 100 in
#:   TOEFL, or 76 in PTE" the 100 bound to IELTS by proximity -- and since IELTS
#:   already had 7.0, TOEFL was left with no score at all. See `_COORDINATOR`.
#: * v5 -> v6 (Step 5C.5): `_COORDINATOR` held two BACKSPACE characters where `\b`
#:   belonged, so `(?:or|and|either|alternatively)` could never match and only `[/;,]`
#:   ever coordinated. The word half of the rule had never run. Restoring it added one
#:   correct claim -- "a minimum IELTS of 6 or TOEFL of 80" on Toronto's proficiency
#:   page -- and removed none. The output changed, so the version changes with it.
VERSION = "6"


@dataclass(frozen=True, slots=True)
class _Operator:
    code: str
    reason: str


def _operator_for(raw: str | None) -> _Operator:
    """Map a comparison phrase to an operator, conservatively.

    An absent phrase means `AT_LEAST`, not `EXACT`: "IELTS 7.0" on a requirements page
    is universally understood as a floor, and reading it as an exact equality would
    make a 7.5 candidate inadmissible. That is the one inference here, and it is
    stated rather than hidden.
    """
    if raw is None:
        return _Operator("AT_LEAST", "no comparison word; a stated requirement is a floor")
    phrase = " ".join(raw.lower().split())
    if phrase in ("above", "over", "greater than"):
        return _Operator("ABOVE", f"strict inequality from {phrase!r}")
    return _Operator("AT_LEAST", f"inclusive floor from {phrase!r}")


def _tests_in(text: str) -> list[tuple[str, str]]:
    """(canonical name, matched wording) for every test mentioned."""
    found: list[tuple[str, str]] = []
    for name, pattern in _TEST_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append((name, match.group(0)))
    return found


def _plausible(test: str, value: float) -> bool:
    """Is this value on the test's scale at all?"""
    bounds = KNOWN_TESTS.get(test)
    if bounds is None:
        return True
    return bounds[0] <= value <= bounds[1]


def _plausible_overall(test: str, value: float) -> bool:
    """Is this value something a university would publish as an overall requirement?

    Stricter than `_plausible`, and only for overall scores. See
    `OVERALL_REQUIREMENT_FLOORS`.
    """
    if not _plausible(test, value):
        return False
    floor = OVERALL_REQUIREMENT_FLOORS.get(test)
    return floor is None or value >= floor


def extract(document: object, *, responsibility: str) -> list[Candidate]:
    """Language candidates from one normalised document."""
    from app.domains.extraction.document import BlockKind

    blocks = getattr(document, "blocks", [])
    candidates: list[Candidate] = []

    for index, block in enumerate(blocks):
        if is_chrome(block):
            # Site furniture, not the page's answer. See `is_chrome`.
            continue
        heading_path = heading_path_for(blocks, index)
        under_language_heading = any(_HEADING_HINT.search(part) for part in heading_path)

        units: list[tuple[str, Locator]] = []
        if block.kind is BlockKind.LIST:
            for item_index, item in enumerate(block.items):
                units.append(
                    (
                        item,
                        Locator(
                            kind="list_item",
                            block_index=index,
                            list_item_index=item_index,
                            heading_path=heading_path,
                        ),
                    )
                )
        elif block.text:
            units.append(
                (block.text, Locator(kind="block", block_index=index, heading_path=heading_path))
            )

        for text, locator in units:
            candidates.extend(
                _from_text(
                    text,
                    locator,
                    under_language_heading=under_language_heading,
                    responsibility=responsibility,
                )
            )

    candidates.extend(_from_tables(document))
    return candidates


#: How far a number may sit from the test name it is attributed to. Beyond this the
#: association is guesswork rather than reading: a paragraph that opens "IELTS is
#: required" and closes "taken within 2 years" does not state a requirement of 2.0.
_MAX_TEST_DISTANCE = 80


#: Characters that, next to a number, mean it is part of a longer token rather than a
#: score. Toronto's page gave IELTS 5.0 from the postcode "M5R 0A3" and TOEFL 85 from
#: the ZIP "08541-6151" (raw text "085") -- both inside the plausible range, so the
#: requirement floors could not catch them. A score stands alone or it is not a score.
_TOKEN_BEFORE = re.compile(r"[0-9A-Za-z./-]")
_TOKEN_AFTER = re.compile(r"[0-9A-Za-z/-]")


def _standalone(text: str, start: int, end: int) -> bool:
    """Is `text[start:end]` a number in its own right?"""
    if start > 0 and _TOKEN_BEFORE.match(text[start - 1]):
        return False
    return not (end < len(text) and _TOKEN_AFTER.match(text[end]))


def _mentions_in(text: str) -> list[tuple[int, int, str]]:
    """(start, end, test) for every test mention, in document order.

    Every occurrence, not the first: "IELTS 7.0 ... or TOEFL 100" needs both positions
    for a number to be attributed to the right one.
    """
    found: list[tuple[int, int, str]] = []
    for name, pattern in _TEST_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.end(), name))
    found.sort()
    return found


def _distance(position: int, mention: tuple[int, int, str]) -> int:
    start, end, _ = mention
    if start <= position <= end:
        return 0
    return min(abs(position - start), abs(position - end))


def _nearest(position: int, mentions: list[tuple[int, int, str]]) -> tuple[str, int]:
    """The test named closest to `position`, and how far away it is.

    Proximity because that is how the sentence reads: in "an overall score of 7.0 in
    IELTS or 100 in TOEFL", the 7.0 belongs to IELTS and the 100 to TOEFL, and running
    the whole sentence once per test would give each of them both numbers.
    """
    best_name, best_distance = mentions[0][2], _distance(position, mentions[0])
    for mention in mentions[1:]:
        distance = _distance(position, mention)
        if distance < best_distance:
            best_name, best_distance = mention[2], distance
    return best_name, best_distance


def _overlaps(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


#: How far a test name and a value may sit apart and still be read as one pairing.
#: "TOEFL iBT with a minimum of 100" is 19 characters of connective wording; a longer
#: gap is a sentence boundary or a list, where the pairing is no longer the wording's.
_MAX_BINDING_GAP = 24

#: Coordination breaks a pairing. In "IELTS or 100 in TOEFL" the "or" separates two
#: alternatives, so the 100 belongs to what follows it and not to what precedes it.
#:
#: A comma coordinates too, and leaving it out was a real failure: in "an overall
#: score of 7.0 in IELTS, 100 in TOEFL, or 76 in PTE" the 100 sits two characters
#: after "IELTS" and four before "TOEFL", so by gap alone IELTS claimed it -- and
#: because IELTS already had 7.0, TOEFL ended up with no score at all.
_COORDINATOR = re.compile(r"\b(?:or|and|either|alternatively)\b|[/;,]", re.I)

_DIGIT = re.compile(r"\d")


def _binds(between: str) -> bool:
    """Does this gap read as one pairing rather than two statements?

    Three refusals, each for a case seen on real pages: a coordinator means the value
    belongs to the other side of it; another digit means an intervening value already
    claimed the name; length means a sentence boundary.
    """
    return (
        len(between) <= _MAX_BINDING_GAP
        and not _COORDINATOR.search(between)
        and not _DIGIT.search(between)
    )


def _attribute(
    start: int, end: int, text: str, mentions: list[tuple[int, int, str]]
) -> tuple[str, int, str]:
    """(test, gap, how) for the value at `text[start:end]`.

    `how` is `binding` when the wording pairs them and `positional` when it does not
    and the nearest name was used instead. It is recorded on the claim, because
    "the page said this" and "we inferred it from position" are different statements
    and a reviewer needs to know which one they are checking.
    """
    bound: list[tuple[int, int, str]] = []
    for mention_start, mention_end, name in mentions:
        if mention_end <= start:
            between, gap, after = text[mention_end:start], start - mention_end, 1
        elif mention_start >= end:
            between, gap, after = text[end:mention_start], mention_start - end, 0
        else:  # the value sits inside the name, e.g. a test named with its version
            between, gap, after = "", 0, 0
        if _binds(between):
            # `after` breaks a tie towards "100 in TOEFL" over "IELTS or 100": a name
            # following the value binds it more tightly than one preceding it.
            bound.append((gap, after, name))
    if bound:
        gap, _, name = min(bound)
        return name, gap, "binding"
    name, distance = _nearest(start, mentions)
    return name, distance, "positional"


def _trimmed_span(text: str, match: re.Match[str]) -> tuple[int, int]:
    """The span of a match with surrounding whitespace excluded.

    `_SCORE` allows whitespace before the number, so `group(0)` can begin with a
    space. The stored `value_raw_text` is stripped, and the locator has to point at
    the same characters or the lineage proof compares two different things.
    """
    start, end = match.start(), match.end()
    matched = match.group(0)
    start += len(matched) - len(matched.lstrip())
    end -= len(matched) - len(matched.rstrip())
    return start, end


def _from_text(
    text: str,
    locator: Locator,
    *,
    under_language_heading: bool,
    responsibility: str,
) -> list[Candidate]:
    """Language candidates from one block, list item or table row's worth of text.

    Every candidate is located to its own character span rather than to the block. Two
    reasons, and the second is the one that bites: a span is the precise pointer
    section 3 asks for, and without it two tests named in one paragraph produce two
    claims with identical locators, identical fingerprints, and one of them silently
    discarded on insert.
    """
    mentions = _mentions_in(text)
    if not mentions:
        return []

    band = Confidence.MEDIUM if under_language_heading else Confidence.LOW
    where = (
        "under a language heading"
        if under_language_heading
        else "in text naming a test, with no matching heading above it"
    )

    def why(how: str) -> str:
        if how == "binding":
            return ""
        return (
            "; no wording pairs this value with a test name, so it is attributed to "
            "the nearest one by position"
        )

    def at(start: int, end: int) -> Locator:
        return replace(locator, char_start=start, char_end=end)

    out: list[Candidate] = []

    # 1. The tests themselves. "We accept IELTS" is a fact about the page even when no
    #    number follows it, and it is recorded without inventing one.
    named: set[str] = set()
    for start, end, name in mentions:
        if name in named:
            continue
        named.add(name)
        out.append(
            Candidate(
                field_kind=FieldKind.LANGUAGE_TEST,
                value={"test": name},
                value_raw_text=text[start:end],
                evidence_text=text,
                locator=at(start, end),
                confidence=band,
                confidence_reason=f"a test name {where}",
                context={"responsibility": responsibility},
            )
        )

    # 2. Component claims first, so their spans can be excluded from the search for an
    #    overall score. "no component below 6.5" must not also become an overall 6.5.
    component_spans: list[tuple[int, int]] = []

    for match in _COMPONENT_MINIMUM.finditer(text):
        value_text = match.group("value") or match.group("value2")
        if value_text is None:  # pragma: no cover - one alternative always matches
            continue
        group = "value" if match.group("value") else "value2"
        if not _standalone(text, match.start(group), match.end(group)):
            continue
        span = _trimmed_span(text, match)
        component_spans.append((match.start(), match.end()))
        test, distance, how = _attribute(span[0], span[1], text, mentions)
        if distance > _MAX_TEST_DISTANCE:
            continue
        score = _as_float(value_text)
        if score is None or not _plausible(test, score):
            continue
        out.append(
            Candidate(
                field_kind=FieldKind.LANGUAGE_COMPONENT_SCORE,
                value={
                    "test": test,
                    "score": score,
                    "operator": "AT_LEAST",
                    "applies_to": "ALL_COMPONENTS",
                },
                value_raw_text=match.group(0).strip(),
                evidence_text=text,
                locator=at(*span),
                confidence=band,
                confidence_reason=(
                    "'no component below X' states a floor for every component, not a "
                    "score for one" + why(how)
                ),
                context={
                    "responsibility": responsibility,
                    "test_attribution": how,
                    "test_name_gap": distance,
                },
            )
        )

    for match in _NAMED_COMPONENT.finditer(text):
        if not _standalone(text, match.start("value"), match.end("value")):
            continue
        span = _trimmed_span(text, match)
        component_spans.append((match.start(), match.end()))
        test, distance, how = _attribute(span[0], span[1], text, mentions)
        if distance > _MAX_TEST_DISTANCE:
            continue
        score = _as_float(match.group("value"))
        if score is None or not _plausible(test, score):
            continue
        out.append(
            Candidate(
                field_kind=FieldKind.LANGUAGE_COMPONENT_SCORE,
                value={
                    "test": test,
                    "score": score,
                    "operator": "AT_LEAST",
                    "applies_to": match.group("component").upper(),
                },
                value_raw_text=match.group(0).strip(),
                evidence_text=text,
                locator=at(*span),
                confidence=band,
                confidence_reason="a named component with its own score" + why(how),
                context={
                    "responsibility": responsibility,
                    "test_attribution": how,
                    "test_name_gap": distance,
                },
            )
        )

    # 3. The overall score: the first number attributed to each test that is not part
    #    of a component claim already made above.
    explicit_overall = bool(_OVERALL_HINT.search(text))
    scored: set[str] = set()
    for match in _SCORE.finditer(text):
        position = match.start("value")
        if _overlaps(position, component_spans):
            continue
        if not _standalone(text, position, match.end("value")):
            continue
        span = _trimmed_span(text, match)
        test, distance, how = _attribute(span[0], span[1], text, mentions)
        if distance > _MAX_TEST_DISTANCE or test in scored:
            continue
        score = _as_float(match.group("value"))
        if score is None or not _plausible_overall(test, score):
            continue
        scored.add(test)
        operator = _operator_for(match.group("op"))
        out.append(
            Candidate(
                field_kind=FieldKind.LANGUAGE_OVERALL_SCORE,
                value={
                    "test": test,
                    "score": score,
                    "operator": operator.code,
                    "explicitly_overall": explicit_overall,
                },
                value_raw_text=match.group(0).strip(),
                evidence_text=text,
                locator=at(*span),
                # HIGH needs the page to say "overall", a matching heading above it,
                # and wording that binds the value to the test. A positional
                # attribution is never the strongest evidence, whatever else is true
                # of the sentence.
                confidence=(
                    Confidence.HIGH
                    if explicit_overall and under_language_heading and how == "binding"
                    else band
                ),
                confidence_reason=(
                    f"{operator.reason}; "
                    + (
                        "the wording says 'overall'"
                        if explicit_overall
                        else "no 'overall' wording, so the scope of the score is inferred"
                    )
                    + why(how)
                ),
                context={
                    "responsibility": responsibility,
                    "test_attribution": how,
                    "test_name_gap": distance,
                },
            )
        )
    return out


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:  # pragma: no cover - the patterns only capture digits
        return None


def _from_tables(document: object) -> list[Candidate]:
    """Score tables, where a header row labels the columns (section 20).

    `HIGH` only here, and only when a header actually labels the column -- a table is
    strong evidence precisely because the publisher labelled it, and a numeric table
    with no header is just numbers.
    """
    from app.domains.extraction.document import BlockKind

    out: list[Candidate] = []
    blocks = getattr(document, "blocks", [])
    tables = getattr(document, "tables", [])

    table_blocks = {
        block.table_index: index
        for index, block in enumerate(blocks)
        if block.kind is BlockKind.TABLE and block.table_index is not None
    }

    for table_index, table in enumerate(tables):
        if not table.header_rows or not table.rows:
            continue
        headers = [cell.text.lower() for cell in table.header_rows[0]]
        score_columns = {
            position: header
            for position, header in enumerate(headers)
            if _SCORE_COLUMN.search(header)
        }
        if not score_columns:
            continue
        block_index = table_blocks.get(table_index)
        heading_path = heading_path_for(blocks, block_index) if block_index is not None else []

        for row_index, row in enumerate(table.rows):
            row_text = " | ".join(cell.text for cell in row)
            tests = _tests_in(row_text)
            if not tests:
                continue
            test = tests[0][0]
            for position, header in score_columns.items():
                if position >= len(row):
                    continue
                cell = row[position].text.strip()
                match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,2})?)", cell)
                if not match:
                    continue
                value = float(match.group(1))
                # "Overall band score" contains "band". Ruling out the overall
                # wording first is what keeps it from being filed as a component
                # requirement, which would turn one 7.0 overall requirement into a
                # floor on all four skills.
                is_component = not _OVERALL_HINT.search(header) and bool(
                    _COMPONENT_COLUMN.search(header)
                )
                acceptable = (
                    _plausible(test, value) if is_component else _plausible_overall(test, value)
                )
                if not acceptable:
                    continue
                out.append(
                    Candidate(
                        field_kind=(
                            FieldKind.LANGUAGE_COMPONENT_SCORE
                            if is_component
                            else FieldKind.LANGUAGE_OVERALL_SCORE
                        ),
                        value={
                            "test": test,
                            "score": value,
                            "operator": "AT_LEAST",
                            "column_label": row[position].text and header,
                        },
                        value_raw_text=cell,
                        evidence_text=row_text,
                        locator=Locator(
                            kind="table_cell",
                            block_index=block_index,
                            table_index=table_index,
                            row_index=row_index,
                            column_index=position,
                            is_header_row=False,
                            heading_path=heading_path,
                        ),
                        confidence=Confidence.HIGH,
                        confidence_reason=(
                            f"table cell under the header {header!r}, in a row naming {test}"
                        ),
                        context={"column_label": header},
                    )
                )
    return out


__all__ = [
    "EXTRACTOR",
    "KNOWN_TESTS",
    "OVERALL_REQUIREMENT_FLOORS",
    "VERSION",
    "extract",
]
