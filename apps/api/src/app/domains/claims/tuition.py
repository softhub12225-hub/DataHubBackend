"""Tuition candidates (Step 5C.2 sections 13-15).

THREE RULES THAT ARE NOT NEGOTIABLE
===================================
1. **No midpoint, ever.** A range is `RANGE` with both endpoints. Averaging £28,000 and
   £32,000 into £30,000 invents a number no university published, and the U14 amount
   model exists precisely so the shape survives.
2. **No currency from the country.** A UK university publishing "30,000" has not said
   pounds. If the page does not show a symbol or code, `currency` is unresolved and the
   claim says so -- guessing would turn a fee in RMB into a fee in GBP on a Chinese
   student's screen.
3. **No billing unit from convention.** A displayed fee is not annual because annual is
   common. Unstated means unresolved.

`VARIABLE` is not the same as absent: the page addresses fees and gives no figure. A
page that never mentions fees produces **nothing** (section 18).
"""

# ruff: noqa: RUF001 -- the non-ASCII characters in the patterns below are
# deliberate. Real university pages write fee ranges with EN DASH and possessives
# with RIGHT SINGLE QUOTATION MARK; a pattern matching only the ASCII hyphen and
# apostrophe would silently miss those pages, which is the bug this rule would
# otherwise talk us into.

from __future__ import annotations

import re

from app.domains.claims.locator import Locator, heading_path_for
from app.domains.claims.model import Candidate, Confidence, FieldKind, is_chrome

EXTRACTOR = "tuition-rule-extractor"
#: Version 3. Each bump came from auditing real pages, and each is recorded because
#: superseded rows are retained rather than overwritten:
#:
#: * v1 -> v2: a bare digit run was read as money, so a postcode ("Evanston, IL 60208")
#:   and a settings-payload id became fees; an ancestor heading alone earned MEDIUM, so
#:   a fundraising total did too; and a descending amount pair was flattened to EXACT,
#:   discarding one of the two numbers.
#: * v2 -> v3: a fragment of a longer digit run was read as money, so the CSS colour
#:   `122,0,223` on UBC's page became an amount of 223; and a table cell did not report
#:   an ambiguous amount shape, so Princeton's "$90,574 -$83,000" came back HIGH with
#:   the shape silently asserted.
#: * v3 -> v4: site navigation and footers were read as page content.
VERSION = "4"

#: Symbol or code to ISO currency. Only where the page shows one.
_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("£", "GBP"),
    ("HK$", "HKD"),
    ("US$", "USD"),
    ("A$", "AUD"),
    ("C$", "CAD"),
    ("S$", "SGD"),
    ("MOP$", "MOP"),
    ("NT$", "TWD"),
    ("RMB", "CNY"),
    ("¥", "CNY"),
    ("€", "EUR"),
    ("$", "USD"),
)
_CURRENCY_CODES = re.compile(r"\b(GBP|USD|EUR|HKD|AUD|CAD|SGD|CNY|RMB|MOP|TWD|NZD|JPY|CHF)\b", re.I)

#: A money amount with at least a thousands separator or a currency marker, so a year
#: ("2027") or a credit count ("180") is not read as a fee.
_AMOUNT = re.compile(
    r"(?P<symbol>£|HK\$|US\$|A\$|C\$|S\$|MOP\$|NT\$|RMB|¥|€|\$)?\s*"
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{4,7}(?:\.\d{2})?)",
)

_RANGE_JOINER = re.compile(r"\s*(?:-|–|—|to|and)\s*", re.I)
_FROM = re.compile(r"\b(?:from|starting\s+(?:at|from)|as\s+low\s+as)\b", re.I)
_UP_TO = re.compile(r"\b(?:up\s+to|no\s+more\s+than|maximum\s+of|max\.?)\b", re.I)
_VARIABLE = re.compile(
    r"\b(?:vary|varies|varying|depend(?:s|ing)?\s+on|differ(?:s)?\s+by|"
    r"course[-\s]specific|programme[-\s]specific|program[-\s]specific)\b",
    re.I,
)
_FEE_WORD = re.compile(r"\bfee|\btuition|\bcost\b|\bcharge", re.I)

_BILLING_UNITS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PER_YEAR",
        re.compile(r"\bper\s+(?:academic\s+)?year\b|\bp\.?a\.?\b|\bannual(?:ly)?\b", re.I),
    ),
    ("PER_SEMESTER", re.compile(r"\bper\s+semester\b|\bper\s+term\b", re.I)),
    ("PER_CREDIT", re.compile(r"\bper\s+credit(?:\s+hour)?\b|\bper\s+unit\b", re.I)),
    (
        "TOTAL_PROGRAM",
        re.compile(
            r"\b(?:total|whole|entire|full)\s+(?:programme|program|course|degree)\b|\bin\s+total\b",
            re.I,
        ),
    ),
    ("PER_MODULE", re.compile(r"\bper\s+module\b", re.I)),
)

#: Explicit student categories. Never inferred from currency (section 14).
_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("INTERNATIONAL", re.compile(r"\binternational\b", re.I)),
    ("OVERSEAS", re.compile(r"\boverseas\b", re.I)),
    ("HOME", re.compile(r"\bhome\b(?!\s*page)", re.I)),
    ("DOMESTIC", re.compile(r"\bdomestic\b", re.I)),
    ("EU", re.compile(r"\bEU\b|\bEuropean\s+Union\b", re.I)),
    ("LOCAL", re.compile(r"\blocal\b", re.I)),
    ("NON_LOCAL", re.compile(r"\bnon-?local\b", re.I)),
)

_HEADING_HINT = re.compile(r"fee|tuition|cost|financ|expense", re.I)


def _currency_in(text: str) -> tuple[str | None, str | None]:
    """(ISO code, the wording that showed it). Both None when the page is silent."""
    code = _CURRENCY_CODES.search(text)
    if code:
        raw = code.group(1).upper()
        return ("CNY" if raw == "RMB" else raw), code.group(0)
    for symbol, iso in _CURRENCY_SYMBOLS:
        if symbol in text:
            return iso, symbol
    return None, None


def _billing_unit_in(text: str) -> tuple[str | None, str | None]:
    for unit, pattern in _BILLING_UNITS:
        match = pattern.search(text)
        if match:
            return unit, match.group(0)
    return None, None


def _categories_in(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name, pattern in _CATEGORIES:
        match = pattern.search(text)
        if match:
            found.append((name, match.group(0)))
    return found


def _amounts_in(text: str) -> list[tuple[float, str, int, int]]:
    """(value, matched text, start, end) for every plausible money amount."""
    out: list[tuple[float, str, int, int]] = []
    for match in _AMOUNT.finditer(text):
        raw = match.group("value")
        # A fragment of a longer digit-and-comma run is not an amount. UBC's page
        # carries the CSS `--wp-block-synced-color--rgb:122,0,223`, and "0,223"
        # matched as a thousands-separated 223. Requiring the match not to abut a
        # digit or a comma excludes colour triplets, version strings and id lists.
        start = match.start("value")
        if start > 0 and text[start - 1] in "0123456789,.":
            continue
        end = match.end("value")
        if end < len(text) and text[end] in "0123456789,":
            continue
        # Money does not have a leading-zero thousands group.
        if "," in raw and raw.split(",")[0].lstrip("0") == "":
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover
            continue
        # A bare digit run with no thousands separator and no adjacent currency symbol
        # is not money. It is a year ("2027 entry"), a postcode ("Evanston, IL 60208")
        # or an internal id from a settings payload -- all three found on real pages in
        # the pilot, all three previously recorded as fees. The old guard only excluded
        # bare numbers below 10,000, which is the one range postcodes are NOT in.
        if match.group("symbol") is None and "," not in raw:
            continue
        out.append((value, match.group(0).strip(), match.start(), match.end()))
    return out


def _classify(
    text: str, amounts: list[tuple[float, str, int, int]]
) -> tuple[str, float | None, float | None]:
    """The U14 amount shape the wording supports.

    `AMOUNT_SHAPE_AMBIGUOUS` is a real answer. Princeton's fee table contains the cell
    "$90,574 -$83,000": two amounts joined by what reads as a range dash, descending.
    That is either a range written backwards or a net figure after a deduction, and
    choosing between them is a judgement section 26 says not to make here. Falling
    through to `EXACT` -- which is what this used to do -- recorded 90,574, threw the
    other number away, and asserted a shape the page never had.
    """
    if len(amounts) >= 2:
        first, second = amounts[0], amounts[1]
        between = text[first[3] : second[2]]
        if _RANGE_JOINER.fullmatch(between):
            if first[0] <= second[0]:
                return "RANGE", first[0], second[0]
            return "AMOUNT_SHAPE_AMBIGUOUS", min(first[0], second[0]), max(first[0], second[0])
    single = amounts[0][0]
    prefix = text[: amounts[0][2]]
    if _UP_TO.search(prefix):
        return "UP_TO", None, single
    if _FROM.search(prefix):
        return "FROM", single, None
    return "EXACT", single, single


def extract(document: object, *, responsibility: str) -> list[Candidate]:
    from app.domains.extraction.document import BlockKind

    blocks = getattr(document, "blocks", [])
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        if is_chrome(block):
            # Site furniture, not the page's answer. See `is_chrome`.
            continue
        heading_path = heading_path_for(blocks, index)
        under_fee_heading = any(_HEADING_HINT.search(part) for part in heading_path)

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
            mentions_fee = bool(_FEE_WORD.search(text)) or under_fee_heading
            if not mentions_fee:
                continue
            out.extend(
                _from_text(
                    text,
                    locator,
                    under_fee_heading=under_fee_heading,
                    responsibility=responsibility,
                )
            )

    out.extend(_from_tables(document, responsibility=responsibility))
    return out


def _from_text(
    text: str, locator: Locator, *, under_fee_heading: bool, responsibility: str
) -> list[Candidate]:
    amounts = _amounts_in(text)
    currency, currency_raw = _currency_in(text)
    unit, unit_raw = _billing_unit_in(text)
    categories = _categories_in(text)

    if not amounts:
        # The page addresses fees and gives no figure: that is VARIABLE, and it is a
        # real statement. Absence of any fee wording produced no unit at all above.
        if _VARIABLE.search(text) and _FEE_WORD.search(text):
            return [
                Candidate(
                    field_kind=FieldKind.TUITION,
                    value={"amount_kind": "VARIABLE"},
                    value_raw_text=text[:300],
                    evidence_text=text,
                    locator=locator,
                    confidence=Confidence.MEDIUM if under_fee_heading else Confidence.LOW,
                    confidence_reason=(
                        "the wording addresses fees and states that they vary, "
                        "without giving a figure"
                    ),
                    context={"responsibility": responsibility},
                )
            ]
        return []

    kind, minimum, maximum = _classify(text, amounts)
    unresolved: list[str] = []
    if kind == "AMOUNT_SHAPE_AMBIGUOUS":
        unresolved.append("AMOUNT_SHAPE_AMBIGUOUS")
    if currency is None:
        unresolved.append("CURRENCY_ABSENT")
    if unit is None:
        unresolved.append("BILLING_UNIT_UNRESOLVED")
    if not categories:
        unresolved.append("STUDENT_CATEGORY_UNRESOLVED")

    value: dict[str, object] = {
        "amount_kind": kind,
        "amount_min": minimum,
        "amount_max": maximum,
        "currency": currency,
        "billing_unit": unit,
        "student_category_raw": [raw for _, raw in categories] or None,
        "student_category": [name for name, _ in categories] or None,
    }
    # MEDIUM needs the wording itself to be about fees. An ancestor heading alone is
    # weaker than it looks: Princeton's "Cost & Aid > New & Noteworthy > University
    # Raises Funds for United Way" put a donation total under a heading containing
    # "Cost", and the claim was recorded at MEDIUM on that basis alone.
    says_fee = bool(_FEE_WORD.search(text))
    if says_fee and under_fee_heading:
        band, reason = Confidence.MEDIUM, "an amount in wording about fees, under a fee heading"
    elif says_fee:
        band, reason = (
            Confidence.LOW,
            "an amount in wording about fees, with no matching heading above it",
        )
    else:
        band, reason = (
            Confidence.LOW,
            "an amount under a fee heading, in wording that does not itself mention "
            "fees -- the section heading is the only thing making this a fee candidate",
        )
    return [
        Candidate(
            field_kind=FieldKind.TUITION,
            value=value,
            value_raw_text=amounts[0][1] if kind != "RANGE" else f"{amounts[0][1]}–{amounts[1][1]}",
            evidence_text=text,
            locator=locator,
            confidence=band,
            confidence_reason=reason
            + (f"; unresolved: {', '.join(unresolved)}" if unresolved else ""),
            unresolved_reason=", ".join(unresolved) or None,
            context={
                "responsibility": responsibility,
                "currency_wording": currency_raw,
                "billing_wording": unit_raw,
            },
        )
    ]


def _from_tables(document: object, *, responsibility: str) -> list[Candidate]:
    """Fee tables, where a header labels the column (sections 13, 20)."""
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
        headers = [cell.text for cell in table.header_rows[0]]
        fee_columns = {
            position: header for position, header in enumerate(headers) if _FEE_WORD.search(header)
        }
        if not fee_columns:
            continue
        block_index = table_blocks.get(table_index)
        heading_path = heading_path_for(blocks, block_index) if block_index is not None else []

        for row_index, row in enumerate(table.rows):
            row_text = " | ".join(cell.text for cell in row)
            for position, header in fee_columns.items():
                if position >= len(row):
                    continue
                cell = row[position].text.strip()
                amounts = _amounts_in(cell)
                if not amounts:
                    continue
                kind, minimum, maximum = _classify(cell, amounts)
                # The header and the row label are both context the publisher supplied,
                # so a currency or category stated there counts -- that is not
                # cross-page inference, it is the same table.
                scope_text = f"{header} {row[0].text if row else ''}"
                currency, currency_raw = _currency_in(f"{cell} {scope_text}")
                unit, unit_raw = _billing_unit_in(f"{cell} {scope_text}")
                categories = _categories_in(scope_text)
                unresolved = [
                    name
                    for name, missing in (
                        # A cell holding "$90,574 -$83,000" is as ambiguous as the same
                        # wording in a sentence. Omitting this here was why Princeton's
                        # cell came back HIGH with the shape silently asserted.
                        ("AMOUNT_SHAPE_AMBIGUOUS", kind == "AMOUNT_SHAPE_AMBIGUOUS"),
                        ("CURRENCY_ABSENT", currency is None),
                        ("BILLING_UNIT_UNRESOLVED", unit is None),
                        ("STUDENT_CATEGORY_UNRESOLVED", not categories),
                    )
                    if missing
                ]
                out.append(
                    Candidate(
                        field_kind=FieldKind.TUITION,
                        value={
                            "amount_kind": kind,
                            "amount_min": minimum,
                            "amount_max": maximum,
                            "currency": currency,
                            "billing_unit": unit,
                            "student_category": [name for name, _ in categories] or None,
                            "student_category_raw": [raw for _, raw in categories] or None,
                            "row_label": row[0].text if row else None,
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
                        confidence=(
                            Confidence.MEDIUM
                            if kind == "AMOUNT_SHAPE_AMBIGUOUS"
                            else Confidence.HIGH
                        ),
                        confidence_reason=(
                            f"table cell under the fee header {header!r}"
                            + (f"; unresolved: {', '.join(unresolved)}" if unresolved else "")
                        ),
                        unresolved_reason=", ".join(unresolved) or None,
                        context={
                            "responsibility": responsibility,
                            "column_label": header,
                            "currency_wording": currency_raw,
                            "billing_wording": unit_raw,
                        },
                    )
                )
    return out


__all__ = ["EXTRACTOR", "VERSION", "extract"]
