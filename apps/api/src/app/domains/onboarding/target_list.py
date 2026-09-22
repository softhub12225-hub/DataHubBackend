"""Parsing and validating a client target-list workbook.

WHY THIS IS STRUCTURAL RATHER THAN POSITIONAL
=============================================
The 181 institutions are **not** written down anywhere in this codebase, and must not
be. They are the client's scope decision; hardcoding them would mean a corrected list
could disagree with the software, and the software would win.

Nor are the file's coordinates hardcoded. The supplied QS 2027 workbook happens to
put its header on row 13 and its data in columns A-F, with five preamble rows and a
per-region summary block off to the right. A QS 2028 file, or a corrected 2027 file,
will differ. So the parser:

* locates the worksheet by name when told, or takes the only sheet when there is one;
* finds the header row by **matching column labels against patterns**, so
  ``QS 2027 世界排名`` and ``QS 2028 世界排名`` both resolve to the rank column;
* reads data rows until the table ends;
* extracts list metadata from the file's own preamble, transcribing rather than
  asserting.

WHAT IS VALIDATED, AND WHY EACH MATTERS
=======================================
Every problem found is collected and reported together, because an operator fixing a
supplied file wants the whole list, not the first line that failed.

* **Expected sheet** -- importing the wrong sheet of a multi-sheet file would import
  a different scope silently.
* **Expected columns** -- a missing rank or name column means the file is not this
  kind of list.
* **Duplicate institutions** -- the same institution twice would double-count scope
  and, on re-import, race to own the same match key.
* **Region values** -- an unmapped region cannot be assigned a destination, and
  guessing one would place an institution in the wrong market.
* **Numeric rank** -- ``"501+"`` or ``"joint 40"`` is not a rank we can store; ties
  are expected and legal, so rank is explicitly *not* required to be unique.
* **Optional score** -- absent is fine, non-numeric is not.
* **Total row count** -- cross-checked against the count the file declares about
  itself, in both its preamble text and its region summary, so a truncated paste is
  caught rather than imported as a smaller scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from app.domains.onboarding.naming import normalize_institution_name
from app.domains.onboarding.regions import resolve_destination
from app.domains.onboarding.workbook import LoadedWorkbook

#: How far below the top of a sheet a header row may be found. The supplied file uses
#: 13; a generous ceiling tolerates more preamble without scanning a whole sheet.
MAX_HEADER_SEARCH_ROWS: Final = 40

#: How many consecutive blank rows end the table. A single blank row inside a
#: hand-maintained list is a formatting artefact, not the end of the data.
BLANK_ROW_RUN_ENDS_TABLE: Final = 3


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One expected column, recognised by pattern rather than position."""

    key: str
    patterns: tuple[str, ...]
    required: bool

    def matches(self, label: str) -> bool:
        return any(re.search(pattern, label, re.IGNORECASE) for pattern in self.patterns)


#: The columns a target list must or may have. Patterns match both the Chinese
#: labels the client uses and plain English equivalents, so a list produced by
#: another team is still readable.
COLUMN_SPECS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("sequence_no", (r"^\s*序号\s*$", r"^\s*(no|num|#|index)\.?\s*$"), required=False),
    ColumnSpec("qs_rank", (r"排名", r"\brank\b"), required=True),
    ColumnSpec(
        "qs_name",
        (
            r"院校名称",
            r"学校名称",
            r"大学名称",
            r"institution",
            r"university\s*name",
            r"^\s*name\s*$",
        ),
        required=True,
    ),
    ColumnSpec(
        "region_label",
        (r"^\s*地区\s*$", r"^\s*region\s*$", r"^\s*市场\s*$"),
        required=True,
    ),
    ColumnSpec(
        "country_territory",
        (r"country", r"territory", r"^\s*国家(/地区)?\s*$"),
        required=False,
    ),
    ColumnSpec("qs_score", (r"得分", r"\bscore\b"), required=False),
)

_REQUIRED_KEYS: Final = tuple(spec.key for spec in COLUMN_SPECS if spec.required)

#: Preamble patterns. Each captures a metadata field the file states about itself.
_SOURCE_DESCRIPTION_HINT = re.compile(r"数据来源|资料来源|data\s*source|source\s*:", re.IGNORECASE)
_SCOPE_HINT = re.compile(r"筛选范围|范围|scope|filter", re.IGNORECASE)
_METHOD_HINT = re.compile(r"口径说明|说明|methodology|note", re.IGNORECASE)
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
#: "v1.1", "版本 1.1", "Version 1.1", "1.1 版"
_VERSION = re.compile(
    r"(?:\bv(?:ersion)?\.?\s*|版本\s*)(\d+(?:\.\d+)*)|(\d+\.\d+)\s*版",
    re.IGNORECASE,
)
#: "2026-06-18", "2026/06/18", "2026年6月18日"
_DATE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
#: "共筛得 181 所院校", "合计 181", "Total: 181"
_DECLARED_COUNT = re.compile(
    r"(?:共筛得|共计|合计|总计|total\s*:?)\s*(\d{1,6})\s*(?:所|个|所院校|institutions?)?",
    re.IGNORECASE,
)
_TOTAL_LABEL = re.compile(r"^\s*(合计|总计|total)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One institution as the file stated it, with derived fields attached."""

    source_row: int
    sequence_no: int | None
    qs_name: str
    qs_name_normalized: str
    qs_rank: int | None
    qs_score: Decimal | None
    region_label: str
    country_territory: str | None
    destination_code: str | None


@dataclass(frozen=True, slots=True)
class PreambleMetadata:
    """What the file states about itself, above its header row.

    Every field is optional. A file that says nothing about its publication date
    leaves ``published_at`` as None, and it stays NULL in the database: a date we
    invented would later read as one the source published (D17).
    """

    list_name: str | None
    list_version: str | None
    source_description: str | None
    source_url: str | None
    published_at: date | None
    declared_row_count: int | None
    scope_note: str | None
    method_note: str | None


@dataclass(frozen=True, slots=True)
class ParsedTargetList:
    """A validated workbook, ready to import."""

    sheet_name: str
    header_row: int
    list_name: str
    list_version: str | None
    source_description: str | None
    source_url: str | None
    published_at: date | None
    declared_row_count: int | None
    declared_region_counts: dict[str, int]
    scope_note: str | None
    method_note: str | None
    rows: tuple[ParsedRow, ...]
    #: Non-fatal observations an operator should see: unmapped regions, rows skipped
    #: as blank, a declared count that matched. Never a reason to refuse an import.
    warnings: tuple[str, ...] = field(default=())


class TargetListValidationError(ValueError):
    """The workbook cannot be imported. Carries every problem found, not just one."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(
            f"target list is not importable ({len(problems)} problem(s)):\n  - {joined}"
        )


def parse_target_list(
    workbook: LoadedWorkbook,
    *,
    sheet_name: str | None = None,
    list_version: str | None = None,
    list_name: str | None = None,
) -> ParsedTargetList:
    """Parse and validate a target-list workbook.

    ``list_version`` overrides whatever the file declares, which is how a corrected
    file that forgot to bump its own version string is imported without editing the
    spreadsheet. ``list_name`` likewise.
    """
    problems: list[str] = []
    warnings: list[str] = []

    resolved_sheet = _resolve_sheet(workbook, sheet_name, problems)
    if resolved_sheet is None:
        raise TargetListValidationError(problems)

    header_row, columns = _find_header(workbook, resolved_sheet, problems)
    if header_row is None:
        raise TargetListValidationError(problems)

    metadata = _parse_preamble(workbook, resolved_sheet, header_row)
    region_counts = _parse_region_summary(workbook, resolved_sheet, header_row, columns)

    rows = _parse_rows(workbook, resolved_sheet, header_row, columns, problems, warnings)

    _validate_duplicates(rows, problems)
    declared = metadata.declared_row_count
    _validate_counts(rows, declared, region_counts, problems, warnings)

    if problems:
        raise TargetListValidationError(problems)

    effective_version = list_version or metadata.list_version
    if not effective_version:
        raise TargetListValidationError(
            [
                "the file does not state a list version and none was supplied; "
                "pass an explicit version so this import can be distinguished from a "
                "later corrected list"
            ]
        )

    effective_name = list_name or metadata.list_name
    if not effective_name:
        raise TargetListValidationError(
            ["the file does not state a list name and none was supplied"]
        )

    return ParsedTargetList(
        sheet_name=resolved_sheet,
        header_row=header_row,
        list_name=effective_name,
        list_version=effective_version,
        source_description=metadata.source_description,
        source_url=metadata.source_url,
        published_at=metadata.published_at,
        declared_row_count=declared,
        declared_region_counts=region_counts,
        scope_note=metadata.scope_note,
        method_note=metadata.method_note,
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Sheet and header discovery
# ---------------------------------------------------------------------------


def _resolve_sheet(
    workbook: LoadedWorkbook, requested: str | None, problems: list[str]
) -> str | None:
    if requested is not None:
        if requested not in workbook.sheet_names:
            problems.append(
                f"expected sheet {requested!r} is absent; the workbook contains "
                f"{', '.join(repr(name) for name in workbook.sheet_names)}"
            )
            return None
        return requested

    populated = [name for name in workbook.sheet_names if workbook.cells.get(name)]
    if not populated:
        problems.append("the workbook contains no populated worksheet")
        return None
    if len(populated) > 1:
        # Choosing for the operator risks importing a different scope than intended.
        problems.append(
            "the workbook has more than one populated sheet "
            f"({', '.join(repr(name) for name in populated)}); name the sheet to import"
        )
        return None
    return populated[0]


def _find_header(
    workbook: LoadedWorkbook, sheet: str, problems: list[str]
) -> tuple[int | None, dict[str, int]]:
    """Locate the header row and map each expected column key to its column index.

    A row qualifies when it matches every *required* column spec. Scanning for that,
    rather than trusting a fixed row number, is what makes the parser survive a
    change in how much preamble the file carries.
    """
    limit = min(workbook.max_row(sheet), MAX_HEADER_SEARCH_ROWS)
    best: tuple[int, dict[str, int]] | None = None

    for row in range(1, limit + 1):
        labels: dict[int, str] = {}
        for column in range(1, workbook.max_column(sheet) + 1):
            value = workbook.value(sheet, row, column)
            if isinstance(value, str):
                labels[column] = value

        found: dict[str, int] = {}
        for spec in COLUMN_SPECS:
            for column, label in sorted(labels.items()):
                if spec.matches(label) and column not in found.values():
                    found[spec.key] = column
                    break

        if all(key in found for key in _REQUIRED_KEYS):
            best = (row, found)
            break

    if best is None:
        missing = ", ".join(_REQUIRED_KEYS)
        problems.append(
            f"no header row found in the first {limit} rows of sheet {sheet!r}: "
            f"could not locate all required columns ({missing}). "
            "Expected labels such as 'QS 2027 世界排名', '院校名称（QS 官方英文）', '地区'."
        )
        return None, {}
    return best


# ---------------------------------------------------------------------------
# Preamble and summary metadata
# ---------------------------------------------------------------------------


def _preamble_strings(workbook: LoadedWorkbook, sheet: str, header_row: int) -> list[str]:
    """Every string above the header row, in reading order."""
    values: list[str] = []
    for row in range(1, header_row):
        for column in range(1, workbook.max_column(sheet) + 1):
            value = workbook.value(sheet, row, column)
            if isinstance(value, str):
                values.append(value)
    return values


def _parse_preamble(workbook: LoadedWorkbook, sheet: str, header_row: int) -> PreambleMetadata:
    """Transcribe what the file says about itself.

    Every field is optional. A file that states no publication date leaves
    ``published_at`` NULL rather than acquiring today's date: a date we invented
    would later look like a fact the source published (D17).
    """
    strings = _preamble_strings(workbook, sheet, header_row)

    source_description: str | None = None
    source_url: str | None = None
    scope_note: str | None = None
    method_note: str | None = None
    title: str | None = None

    for value in strings:
        if _URL.match(value.strip()):
            source_url = source_url or value.strip()
        elif _SOURCE_DESCRIPTION_HINT.search(value):
            source_description = source_description or value
        elif _SCOPE_HINT.search(value):
            scope_note = scope_note or value
        elif _METHOD_HINT.search(value):
            method_note = method_note or value
        elif title is None:
            # The first free-text line that is not one of the labelled notes is the
            # list's own title.
            title = value

    # Version, date and declared count may appear in any preamble line; the QS file
    # puts all three in its 数据来源 line.
    haystack = " ".join(strings)
    version_match = _VERSION.search(haystack)
    list_version = None
    if version_match:
        list_version = version_match.group(1) or version_match.group(2)

    published_at: date | None = None
    date_match = _DATE.search(haystack)
    if date_match:
        year, month, day = (int(group) for group in date_match.groups())
        try:
            published_at = date(year, month, day)
        except ValueError:
            # A malformed date is left NULL. It is metadata about the list, not a
            # governed fact, so it does not justify refusing the import.
            published_at = None

    count_match = _DECLARED_COUNT.search(haystack)
    declared_row_count = int(count_match.group(1)) if count_match else None

    return PreambleMetadata(
        list_name=title,
        list_version=list_version,
        source_description=source_description,
        source_url=source_url,
        published_at=published_at,
        declared_row_count=declared_row_count,
        scope_note=scope_note,
        method_note=method_note,
    )


def _parse_region_summary(
    workbook: LoadedWorkbook, sheet: str, header_row: int, columns: dict[str, int]
) -> dict[str, int]:
    """Read a region/count summary block, if the file carries one.

    The supplied workbook puts one in columns H-I: a region label beside a count,
    ending in a 合计 total. It is an independent statement of the same scope, so
    checking the imported rows against it catches a file that was filtered after its
    summary was written.

    Only columns to the right of the data table are considered, so a region *data*
    column is never mistaken for a summary.
    """
    data_columns = set(columns.values())
    first_summary_column = max(data_columns) + 1 if data_columns else 1
    counts: dict[str, int] = {}

    for column in range(first_summary_column, workbook.max_column(sheet) + 1):
        for row in range(1, header_row):
            label = workbook.value(sheet, row, column)
            value = workbook.value(sheet, row, column + 1)
            if not isinstance(label, str):
                continue
            number = _as_int(value)
            if number is None:
                continue
            if _TOTAL_LABEL.match(label):
                counts["__total__"] = number
            else:
                counts[label.strip()] = number
    return counts


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------


def _parse_rows(
    workbook: LoadedWorkbook,
    sheet: str,
    header_row: int,
    columns: dict[str, int],
    problems: list[str],
    warnings: list[str],
) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    blank_run = 0
    max_row = workbook.max_row(sheet)

    for row_index in range(header_row + 1, max_row + 1):
        name_value = workbook.value(sheet, row_index, columns["qs_name"])
        if name_value is None:
            blank_run += 1
            if blank_run >= BLANK_ROW_RUN_ENDS_TABLE:
                break
            continue
        blank_run = 0

        if not isinstance(name_value, str):
            problems.append(f"row {row_index}: institution name is not text ({name_value!r})")
            continue
        qs_name = name_value.strip()
        try:
            normalized = normalize_institution_name(qs_name)
        except ValueError as exc:
            problems.append(f"row {row_index}: {exc}")
            continue

        rank_raw = workbook.value(sheet, row_index, columns["qs_rank"])
        qs_rank = _as_int(rank_raw)
        if rank_raw is not None and qs_rank is None:
            problems.append(
                f"row {row_index} ({qs_name}): rank {rank_raw!r} is not a plain number. "
                "Banded values such as '501+' cannot be stored as a rank."
            )
            continue
        if qs_rank is not None and qs_rank < 1:
            problems.append(f"row {row_index} ({qs_name}): rank {qs_rank} is not positive")
            continue

        qs_score: Decimal | None = None
        if "qs_score" in columns:
            score_raw = workbook.value(sheet, row_index, columns["qs_score"])
            if score_raw is not None:
                qs_score = _as_decimal(score_raw)
                if qs_score is None:
                    problems.append(
                        f"row {row_index} ({qs_name}): score {score_raw!r} is not numeric"
                    )
                    continue
                if not Decimal(0) <= qs_score <= Decimal(100):
                    problems.append(
                        f"row {row_index} ({qs_name}): score {qs_score} is outside 0-100"
                    )
                    continue

        region_raw = workbook.value(sheet, row_index, columns["region_label"])
        if not isinstance(region_raw, str) or not region_raw.strip():
            problems.append(f"row {row_index} ({qs_name}): region is missing")
            continue
        region_label = region_raw.strip()

        country_territory: str | None = None
        if "country_territory" in columns:
            territory_raw = workbook.value(sheet, row_index, columns["country_territory"])
            if isinstance(territory_raw, str) and territory_raw.strip():
                country_territory = territory_raw.strip()

        destination_code, region_problem = resolve_destination(region_label, country_territory)
        if region_problem is not None:
            # Not fatal: the institution is still in scope, it just cannot be
            # assigned a destination yet. The importer marks it for manual review.
            warnings.append(f"row {row_index} ({qs_name}): {region_problem}")

        sequence_no = None
        if "sequence_no" in columns:
            sequence_no = _as_int(workbook.value(sheet, row_index, columns["sequence_no"]))

        rows.append(
            ParsedRow(
                source_row=row_index,
                sequence_no=sequence_no,
                qs_name=qs_name,
                qs_name_normalized=normalized,
                qs_rank=qs_rank,
                qs_score=qs_score,
                region_label=region_label,
                country_territory=country_territory,
                destination_code=destination_code,
            )
        )

    if not rows and not problems:
        problems.append(f"sheet {sheet!r} has a header row but no data rows")
    return rows


# ---------------------------------------------------------------------------
# Whole-file validation
# ---------------------------------------------------------------------------


def _validate_duplicates(rows: list[ParsedRow], problems: list[str]) -> None:
    """Refuse a file naming the same institution twice.

    Ranks are *not* checked for uniqueness: QS ties are normal, and the supplied
    2027 list contains 29 tied ranks including a four-way tie at 411.
    """
    seen: dict[str, ParsedRow] = {}
    for row in rows:
        previous = seen.get(row.qs_name_normalized)
        if previous is not None:
            problems.append(
                f"duplicate institution: {row.qs_name!r} on row {row.source_row} "
                f"repeats {previous.qs_name!r} from row {previous.source_row}"
            )
            continue
        seen[row.qs_name_normalized] = row


def _validate_counts(
    rows: list[ParsedRow],
    declared_row_count: int | None,
    region_counts: dict[str, int],
    problems: list[str],
    warnings: list[str],
) -> None:
    """Cross-check the imported rows against the file's own claims about itself."""
    actual = len(rows)

    if declared_row_count is not None and declared_row_count != actual:
        problems.append(
            f"the file states it contains {declared_row_count} institutions but "
            f"{actual} data rows were read; the file may be truncated or filtered"
        )

    total = region_counts.get("__total__")
    if total is not None and total != actual:
        problems.append(
            f"the file's summary totals {total} institutions but {actual} data rows " "were read"
        )

    per_region = {label: count for label, count in region_counts.items() if label != "__total__"}
    if per_region:
        observed: dict[str, int] = {}
        for row in rows:
            observed[row.region_label] = observed.get(row.region_label, 0) + 1
        for label, expected in sorted(per_region.items()):
            got = observed.get(label, 0)
            if got != expected:
                problems.append(
                    f"the file's summary states {expected} institutions in {label!r} "
                    f"but {got} were read"
                )
        for label, got in sorted(observed.items()):
            if label not in per_region:
                warnings.append(
                    f"region {label!r} has {got} institutions but does not appear in "
                    "the file's own summary block"
                )


def _as_int(value: object) -> int | None:
    """Coerce a cell to an int, or None when it is not plainly one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else None
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if re.fullmatch(r"\d{1,9}", text) else None
    return None


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{1,6}(\.\d{1,4})?", text):
            return Decimal(text)
    return None


__all__ = [
    "BLANK_ROW_RUN_ENDS_TABLE",
    "COLUMN_SPECS",
    "MAX_HEADER_SEARCH_ROWS",
    "ColumnSpec",
    "ParsedRow",
    "ParsedTargetList",
    "TargetListValidationError",
    "parse_target_list",
]
