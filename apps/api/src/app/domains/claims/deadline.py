"""Application-deadline candidates (Step 5C.2 sections 16-17, 22).

PRECISION IS PRESERVED, NEVER MANUFACTURED
==========================================
"15 January" has a day and a month and **no year**. The year is not inferred from the
current date, from a nearby "2027 entry", or from anything else: C9/C13 exist because a
date-only fact converted to midnight UTC is a fabricated instant, and a deadline is the
one field where being an hour wrong matters.

So a candidate records exactly the components the page stated — year, month, day, time,
timezone — and leaves the rest null.

A CALENDAR IS NOT A DEADLINE
============================
The Caltech PDF is registered as `ACADEMIC_CALENDAR`, and it is full of dates:
"Beginning of instruction", "Registration ends". None of those is an application
deadline. Routing keeps the deadline extractor away from calendar pages entirely, and
the calendar extractor emits `ACADEMIC_CALENDAR_EVENT` — preserved for later use,
deliberately not misfiled as an admissions fact (section 22).
"""

# ruff: noqa: RUF001 -- the non-ASCII characters in the patterns below are
# deliberate. Real university pages write fee ranges with EN DASH and possessives
# with RIGHT SINGLE QUOTATION MARK; a pattern matching only the ASCII hyphen and
# apostrophe would silently miss those pages, which is the bug this rule would
# otherwise talk us into.

from __future__ import annotations

import calendar
import re

from app.db.enums import DeadlineKind
from app.domains.claims.locator import Locator, heading_path_for
from app.domains.claims.model import Candidate, Confidence, FieldKind, is_chrome

EXTRACTOR = "deadline-rule-extractor"
#: Version 3. Both bumps came from checking the output against the real fleet:
#:
#: * v1 -> v2: site navigation was read as page content, so a menu item reading "Dates
#:   and deadlines" was matched as deadline wording.
#: * v2 -> v3: a time found anywhere in the block was attached to the block's first
#:   date, so UCL's "except those with a 15 October deadline, should arrive at UCAS by
#:   18:00 ... on 13 January 2027" produced "15 October 18:00". A time is now bound to
#:   its own date. The same version reads a bracketed timezone -- "6pm (GMT)" used to
#:   record TIMEZONE_NOT_STATED.
#: * v3 -> v4: a round label taken from the heading was stamped on a date whose own
#:   wording named a different round, and a year range was read as a day. See
#:   `_context_for` and `_DATE`.
VERSION = "4"

CALENDAR_EXTRACTOR = "calendar-rule-extractor"
#: The calendar rule carries its **own** version, because it is its own extractor and
#: the two change for different reasons. Sharing one constant meant that correcting
#: the calendar locator would have relabelled every deadline claim as well, producing
#: a second copy of 81 claims that had not changed -- which is the opposite of what
#: versioning is for.
#:
#: It starts at 2: version 1 located an entry by a clamped window around its date, so
#: two dates a few characters apart in one block shared a locator and the second was
#: discarded on insert. Those rows are retained as superseded history. Version 3
#: additionally stops reading dates out of site navigation and footers. Version 4 stops
#: reading a year range as a day: "winter term 2026-27 December 4" produced "27
#: December", and the bad match then made "4 November" out of the text after it.
CALENDAR_VERSION = "4"

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})

_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: "15 January 2027", "January 15, 2027", "15 Jan", "January 2027".
#: The day alternative carries a lookbehind. Without it "winter term 2026-27 December
#: 4" produced the date "27 December" -- `\b` is satisfied between "-" and "27" -- and
#: the bad match consumed "December", so the next match became "4 November" out of
#: "December 4 November 26-27". A day is never preceded by a digit, a hyphen or a slash.
_DATE = re.compile(
    rf"\b(?:(?<![\d/-])(?P<day1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month1>{_MONTH_NAMES})"
    rf"(?:\s+(?P<year1>\d{{4}}))?"
    rf"|(?P<month2>{_MONTH_NAMES})\s+(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(?P<year2>\d{{4}}))?"
    rf"|(?P<month3>{_MONTH_NAMES})\s+(?P<year3>\d{{4}}))\b",
    re.I,
)

#: A time, only where stated. "23:59", "5pm", "17:00 GMT".
_TIME = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\b"
    # The zone may be bracketed: "6pm (GMT)" stated a timezone and the previous
    # pattern recorded TIMEZONE_NOT_STATED, because `\s*` cannot cross the "(".
    r"(?:\s*[(\[]?\s*(?P<zone>GMT|UTC|BST|EST|EDT|PST|PDT|CET|CEST|HKT|SGT|"
    r"AEST|AEDT|NZST|JST|CST)\s*[)\]]?)?",
    re.I,
)
_TIME_CONTEXT = re.compile(r"\bby\b|\bbefore\b|\bat\b|\bdeadline\b|\bcloses?\b", re.I)

_ROLLING = re.compile(r"\brolling\s+(?:admission|basis|deadline)?\b|\bon\s+a\s+rolling\b", re.I)
_UNTIL_FILLED = re.compile(
    r"\buntil\s+(?:all\s+)?(?:places|spaces|seats)\s+(?:are\s+)?(?:filled|full)\b"
    r"|\bwhile\s+places\s+(?:remain|last)\b",
    re.I,
)
_NO_FIXED = re.compile(
    r"\bno\s+(?:fixed|formal|set|specific)\s+(?:closing\s+)?(?:date|deadline)\b"
    r"|\bthere\s+is\s+no\s+deadline\b",
    re.I,
)
_CLOSED = re.compile(
    r"\b(?:not\s+currently\s+accepting|applications\s+are\s+closed|closed\s+for\s+"
    r"(?:\d{4}|entry)|no\s+longer\s+accepting)\b",
    re.I,
)

_DEADLINE_WORD = re.compile(
    r"\bdeadline|\bclosing\s+date|\bapplications?\s+(?:close|open|due)|\bapply\s+by\b"
    r"|\bsubmit\s+by\b|\bcloses?\s+on\b",
    re.I,
)
_HEADING_HINT = re.compile(r"deadline|closing|key\s+dates?|important\s+dates?|apply|timeline", re.I)

#: Round labels the page states. A label is preserved verbatim; a *number* is only
#: recorded when the page gives one -- "Round 1" has a number, "Priority" does not, and
#: inventing one for Priority would make an ordering the university never published.
_ROUND = re.compile(
    r"\b(?P<label>round\s*(?P<number>\d+)|(?:restrictive\s+|single[- ]choice\s+)?"
    r"early\s+(?:action|decision)|priority|regular\s+decision|main\s+round|"
    r"first\s+round|second\s+round|third\s+round|final\s+round)\b",
    re.I,
)

_ACADEMIC_YEAR = re.compile(r"\b(?P<start>20\d{2})\s*[/–—-]\s*(?P<end>\d{2,4})\b")
_ENTRY_YEAR = re.compile(r"\b(?P<year>20\d{2})\s+entry\b", re.I)


def _date_parts(text: str) -> tuple[dict[str, int | None], re.Match[str]] | None:
    """The date components the wording actually states, and the match that found them.

    The match rather than its text: `_time_parts` needs the *position* to bind a time
    to this date rather than to one 700 characters away.
    """
    match = _DATE.search(text)
    if not match:
        return None
    groups = match.groupdict()
    month_name = groups["month1"] or groups["month2"] or groups["month3"]
    day = groups["day1"] or groups["day2"]
    year = groups["year1"] or groups["year2"] or groups["year3"]
    parts: dict[str, int | None] = {
        "year": int(year) if year else None,
        "month": _MONTHS.get((month_name or "").lower()),
        "day": int(day) if day else None,
    }
    if parts["month"] is None:
        return None
    if parts["day"] is not None and not 1 <= parts["day"] <= 31:
        return None
    return parts, match


#: How far a time may sit from the date it qualifies. Real pairings are adjacent --
#: "13 January 2027 at 6pm" is a gap of four characters, "15 October 2026 (18:00 UK
#: time)" is a gap of one. Beyond this the number belongs to another sentence, and the
#: cases that proved it are in this module's docstring.
_MAX_TIME_GAP = 30


def _time_parts(text: str, date_match: re.Match[str]) -> dict[str, object] | None:
    """A time, only when the wording gives one **for this date**.

    Takes the date's match rather than its text: the position is the whole point, and
    the previous signature could only blank the date out of the entire block and then
    search all of it. A time 700 characters away is not this date's time.
    """
    start, end = date_match.start(), date_match.end()
    before = text[max(0, start - _MAX_TIME_GAP) : start]
    after = text[end : end + _MAX_TIME_GAP]

    for window in (after, before):
        # Another date inside the window means the window spans two statements, and
        # which one the time belongs to is no longer determined by proximity.
        if _DATE.search(window):
            continue
        if not _TIME_CONTEXT.search(window):
            continue
        found = _first_time(window)
        if found is not None:
            return found
    return None


def _first_time(window: str) -> dict[str, object] | None:
    for match in _TIME.finditer(window):
        hour_text = match.group("hour")
        if hour_text is None:  # pragma: no cover - the group is not optional
            continue
        hour = int(hour_text)
        meridiem = (match.group("meridiem") or "").lower()
        minute = int(match.group("minute") or 0)
        if not meridiem and match.group("minute") is None:
            # A bare number is not a time. "Round 1" must not become 01:00.
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            continue
        zone = match.group("zone")
        return {
            "hour": hour,
            "minute": minute,
            "timezone": zone.upper() if zone else None,
            "raw": match.group(0).strip(" ()[]"),
        }
    return None


def _context_for(text: str, heading_path: list[str]) -> dict[str, object]:
    """Explicit context only (section 17).

    The round label is taken from the **wording first**, and the claim records which of
    the two it came from. Searching the heading and the text together took the first
    match in either, so under Caltech's "Early Action" heading a list item reading
    "January 4, 2027 for Regular Decision" was stored as Early Action -- a date filed
    under the wrong round, which is worse than no round at all. A label the page only
    implied by section heading is still recorded, and marked `heading` so grouping can
    decline to separate rounds on it.
    """
    joined = " ".join([*heading_path, text])
    context: dict[str, object] = {}
    in_text = _ROUND.search(text)
    round_match = in_text or _ROUND.search(" ".join(heading_path))
    if round_match:
        context["round_label"] = round_match.group("label")
        context["round_label_source"] = "text" if in_text else "heading"
        if round_match.group("number"):
            context["round_number"] = int(round_match.group("number"))
    academic = _ACADEMIC_YEAR.search(joined)
    if academic:
        context["academic_year_raw"] = academic.group(0)
    entry = _ENTRY_YEAR.search(joined)
    if entry:
        context["entry_year"] = int(entry.group("year"))
    return context


def extract(document: object, *, responsibility: str) -> list[Candidate]:
    from app.domains.extraction.document import BlockKind

    blocks = getattr(document, "blocks", [])
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        if is_chrome(block):
            # Site furniture, not the page's answer. See `is_chrome`.
            continue
        heading_path = heading_path_for(blocks, index)
        under_heading = any(_HEADING_HINT.search(part) for part in heading_path)

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
            if not (_DEADLINE_WORD.search(text) or under_heading):
                continue
            out.extend(
                _from_text(
                    text, locator, under_heading=under_heading, responsibility=responsibility
                )
            )
    return out


def _from_text(
    text: str, locator: Locator, *, under_heading: bool, responsibility: str
) -> list[Candidate]:
    context = _context_for(text, locator.heading_path)
    context["responsibility"] = responsibility
    band = Confidence.MEDIUM if under_heading else Confidence.LOW
    reason_suffix = "under a deadline heading" if under_heading else "in text naming a deadline"

    # The non-date kinds first: a page saying "rolling" and also naming a date is
    # describing two things, and the explicit statement is the stronger signal.
    for pattern, kind in (
        (_CLOSED, DeadlineKind.NOT_CURRENTLY_ACCEPTING),
        (_UNTIL_FILLED, DeadlineKind.UNTIL_FILLED),
        (_NO_FIXED, DeadlineKind.NO_FIXED_DEADLINE),
        (_ROLLING, DeadlineKind.ROLLING),
    ):
        match = pattern.search(text)
        if match:
            return [
                Candidate(
                    field_kind=FieldKind.APPLICATION_DEADLINE,
                    value={"deadline_kind": kind.value},
                    value_raw_text=match.group(0).strip(),
                    evidence_text=text,
                    locator=locator,
                    confidence=band,
                    confidence_reason=f"explicit {kind.value} wording {reason_suffix}",
                    context=context,
                )
            ]

    found = _date_parts(text)
    if not found:
        return []
    parts, date_match = found
    raw_date = date_match.group(0)
    time_parts = _time_parts(text, date_match)

    unresolved: list[str] = []
    if parts["year"] is None:
        unresolved.append("YEAR_NOT_STATED")
    if parts["day"] is None:
        unresolved.append("DAY_NOT_STATED")
    if time_parts is None:
        unresolved.append("TIME_NOT_STATED")
    elif time_parts.get("timezone") is None:
        unresolved.append("TIMEZONE_NOT_STATED")

    value: dict[str, object] = {
        "deadline_kind": DeadlineKind.FIXED_DATE.value,
        "year": parts["year"],
        "month": parts["month"],
        "day": parts["day"],
        "hour": time_parts["hour"] if time_parts else None,
        "minute": time_parts["minute"] if time_parts else None,
        "timezone": time_parts.get("timezone") if time_parts else None,
    }
    return [
        Candidate(
            field_kind=FieldKind.APPLICATION_DEADLINE,
            value=value,
            value_raw_text=(raw_date + (f" {time_parts['raw']}" if time_parts else "")).strip(),
            evidence_text=text,
            locator=locator,
            confidence=band,
            confidence_reason=(
                f"a stated date {reason_suffix}; "
                "only the components the page gave are recorded"
                + (f" (absent: {', '.join(unresolved)})" if unresolved else "")
            ),
            unresolved_reason=", ".join(unresolved) or None,
            context=context,
        )
    ]


def extract_calendar(document: object, *, responsibility: str) -> list[Candidate]:
    """Dated calendar entries, explicitly **not** deadlines (section 22).

    Preserved structurally so a later step can use an academic calendar for what it is
    -- term dates, registration windows -- rather than having its dates silently
    reinterpreted as admissions deadlines now.
    """
    blocks = getattr(document, "blocks", [])
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        text = block.text
        if not text or is_chrome(block):
            continue
        heading_path = heading_path_for(blocks, index)
        page = block.level if getattr(block.kind, "value", "") == "pdf_page" else None
        # A calendar page is many dated entries, sometimes several to a block. Each
        # date match is one entry, located at the match: two dates a few characters
        # apart must not end up with the same locator, because the fingerprint is
        # built from it and the second claim would be discarded on insert.
        for match in _DATE.finditer(text):
            found = _date_parts(match.group(0))
            if not found:
                continue
            parts, _ = found
            raw_date = match.group(0)
            line = _line_around(text, match)
            out.append(
                Candidate(
                    field_kind=FieldKind.ACADEMIC_CALENDAR_EVENT,
                    value={
                        "year": parts["year"],
                        "month": parts["month"],
                        "day": parts["day"],
                        "event_text": line[:300],
                    },
                    value_raw_text=raw_date,
                    evidence_text=line,
                    locator=Locator(
                        kind="pdf_page" if page else "block",
                        block_index=index,
                        pdf_page=page,
                        heading_path=heading_path,
                        char_start=match.start(),
                        char_end=match.end(),
                    ),
                    confidence=Confidence.MEDIUM,
                    confidence_reason=(
                        "a dated entry on a page registered as ACADEMIC_CALENDAR; "
                        "recorded as a calendar event, NOT an application deadline"
                    ),
                    unresolved_reason=(None if parts["year"] is not None else "YEAR_NOT_STATED"),
                    context={"responsibility": responsibility},
                )
            )
    return out


def _line_around(text: str, match: re.Match[str], *, window: int = 160) -> str:
    """The wording around a date, for the evidence a reviewer reads.

    A PDF calendar page arrives as one block of collapsed text, so quoting the whole
    block as evidence would quote 2,000 characters. This is context only -- the
    locator points at the date itself, so two entries in one window stay distinct.
    """
    start = max(0, match.start() - 20)
    end = min(len(text), match.end() + window)
    return text[start:end].strip()


__all__ = [
    "CALENDAR_EXTRACTOR",
    "CALENDAR_VERSION",
    "EXTRACTOR",
    "VERSION",
    "extract",
    "extract_calendar",
]
