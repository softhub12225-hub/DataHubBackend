"""Validating a filled-in pilot collection workbook.

This reads a returned workbook and reports what is wrong with it. It performs **no
database writes**: the real importer is deliberately not built until the client
returns the actual file, because an importer written against a guessed file shape is
how a mismatch becomes silent data corruption.

What it is for is the conversation that happens *before* import — handing back a
precise list of "sheet Tuition, row 14, program_ref P0007 is not defined on the
Programs sheet" rather than a rejected file and a shrug.

WHY REFERENCES INSTEAD OF NAMES
===============================
Nothing here joins sheets on programme-name text. Two programmes at one university
are routinely called the same thing (full-time and part-time), a name gets retyped
with a different dash on the next sheet, and Excel's autocomplete edits it for you. A
short workbook-local code — `P0001`, `S0001` — is the only thing a person can copy
reliably and a machine can check exactly.

`program_ref` and `source_ref` are **collection identifiers only**. They live in one
workbook, mean nothing outside it, and are never the canonical `program.id` or
`source.id`. A second workbook may reuse `P0001` for a different programme, which is
exactly why they are scoped to the file and resolved during import rather than stored.

THE SCOPE RULE
==============
Only the literal `UNIVERSAL` resolves to the universal applicant scope. Every other
value — including a blank — parks as `SCOPE_MAPPING_REQUIRED` for a human.

That asymmetry is deliberate. `admission_requirement.applicant_scope_id` is NOT NULL
and `UNIVERSAL` is the only seeded scope, so the path of least resistance for an
importer is to map everything to "all applicants". Applied to "IELTS 7.0, or 6.5 for
holders of a Chinese bachelor degree from a 985/211 institution", that publishes a
requirement on everybody that the university never stated. A parked row is a worklist
item; a wrongly universal row is a wrong published fact nobody will notice.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import Connection

from app.core.logging import get_logger
from app.domains.onboarding import pilot_matching
from app.domains.onboarding.pilot_template import (
    HIGH_RISK_SHEETS,
    MATCH_KEY_COLUMN,
    PILOT_SELECTION_VALUES,
    PROGRAM_REF_PATTERN,
    SOURCE_REF_PATTERN,
    TEMPLATE_SHEETS,
    TUITION_AMOUNT_KINDS,
    TemplateSheet,
)
from app.domains.onboarding.workbook import LoadedWorkbook, load_workbook

logger = get_logger(__name__)

#: Sheets whose rows carry facts about a programme, as opposed to defining one.
FACT_SHEETS: tuple[str, ...] = (
    "Admissions",
    "Language_Requirements",
    "Tuition",
    "Deadlines",
)

#: The only `applicant_scope_hint` value that resolves without a human.
UNIVERSAL_SCOPE_HINT = "UNIVERSAL"

#: Statuses that assert an observation and therefore need a cited page.
ASSERTED_STATUSES = ("PUBLISHED", "OFFICIALLY_NOT_PUBLISHED")

#: `tuition.academic_year` and `intake.academic_year` both carry this CHECK. The
#: workbook asks for the year "as the page writes it", so "2027-28", "2027/28 entry"
#: and "September 2027 entry" all arrive and all fail at insert time. Checked here
#: instead, while the client can still fix it.
ACADEMIC_YEAR_PATTERN = re.compile(r"^[0-9]{4}(/[0-9]{2,4})?$")

#: Which student categories exist per destination. `LOCAL` is ambiguous between HK
#: and MO by code alone, and nothing stops a collector choosing HOME for a Hong Kong
#: institution -- so the pairing is checked against the institution's destination.
STUDENT_CATEGORIES_BY_DESTINATION: dict[str, frozenset[str]] = {
    "GB": frozenset({"HOME", "INTERNATIONAL"}),
    "HK": frozenset({"LOCAL", "NON_LOCAL"}),
    "MO": frozenset({"LOCAL", "NON_LOCAL"}),
}


class Severity(StrEnum):
    ERROR = "ERROR"
    """The workbook cannot be imported as it stands."""

    WARNING = "WARNING"
    """Importable, but a human must decide something first."""


class IssueCode(StrEnum):
    """Every way a returned workbook can be wrong, named.

    Named rather than free-text so the console can group them, tests can assert on
    them, and an operator can be told "seven rows have PROGRAM_REF_UNKNOWN" instead
    of reading seven sentences.
    """

    SHEET_MISSING = "SHEET_MISSING"
    COLUMN_MISSING = "COLUMN_MISSING"

    SELECTED_COUNT_MISMATCH = "SELECTED_COUNT_MISMATCH"
    SELECTED_VALUE_UNRECOGNISED = "SELECTED_VALUE_UNRECOGNISED"
    SELECTED_WITHOUT_DETAILS = "SELECTED_WITHOUT_DETAILS"
    DATA_FOR_UNSELECTED_INSTITUTION = "DATA_FOR_UNSELECTED_INSTITUTION"

    TARGET_UNRESOLVED = "TARGET_UNRESOLVED"

    PROGRAM_REF_MALFORMED = "PROGRAM_REF_MALFORMED"
    PROGRAM_REF_DUPLICATE = "PROGRAM_REF_DUPLICATE"
    PROGRAM_REF_UNKNOWN = "PROGRAM_REF_UNKNOWN"
    PROGRAM_REF_INSTITUTION_MISMATCH = "PROGRAM_REF_INSTITUTION_MISMATCH"

    SOURCE_REF_MALFORMED = "SOURCE_REF_MALFORMED"
    SOURCE_REF_DUPLICATE = "SOURCE_REF_DUPLICATE"
    SOURCE_REF_UNKNOWN = "SOURCE_REF_UNKNOWN"
    SOURCE_REF_INSTITUTION_MISMATCH = "SOURCE_REF_INSTITUTION_MISMATCH"
    SOURCE_REF_MISSING = "SOURCE_REF_MISSING"

    REQUIRED_VALUE_MISSING = "REQUIRED_VALUE_MISSING"
    PUBLISHED_WITHOUT_VALUE = "PUBLISHED_WITHOUT_VALUE"
    VALUE_WITH_UNPUBLISHED_STATUS = "VALUE_WITH_UNPUBLISHED_STATUS"

    SCOPE_MAPPING_REQUIRED = "SCOPE_MAPPING_REQUIRED"

    TUITION_AMOUNT_KIND_UNKNOWN = "TUITION_AMOUNT_KIND_UNKNOWN"
    TUITION_AMOUNT_SHAPE_INCONSISTENT = "TUITION_AMOUNT_SHAPE_INCONSISTENT"
    TUITION_AMOUNT_NOT_A_NUMBER = "TUITION_AMOUNT_NOT_A_NUMBER"

    ACADEMIC_YEAR_MALFORMED = "ACADEMIC_YEAR_MALFORMED"
    STUDENT_CATEGORY_NOT_IN_DESTINATION = "STUDENT_CATEGORY_NOT_IN_DESTINATION"
    DEADLINE_PARTS_INCONSISTENT = "DEADLINE_PARTS_INCONSISTENT"


@dataclass(frozen=True, slots=True)
class Issue:
    """One problem, located precisely enough to fix."""

    code: IssueCode
    severity: Severity
    sheet: str
    row: int | None
    message: str

    def __str__(self) -> str:
        where = f"{self.sheet} row {self.row}" if self.row else self.sheet
        return f"[{self.severity}] {self.code} — {where}: {self.message}"


class ScopeResolution(StrEnum):
    UNIVERSAL = "UNIVERSAL"
    SCOPE_MAPPING_REQUIRED = "SCOPE_MAPPING_REQUIRED"


def resolve_applicant_scope(hint: object) -> ScopeResolution:
    """Map a collected scope hint. Only the literal `UNIVERSAL` resolves.

    A blank hint does **not** mean "everyone"; it means the collector did not say.
    Both blank and any specific wording park for a human, because the alternative --
    defaulting to universal -- silently widens a requirement the university scoped
    narrowly.
    """
    if isinstance(hint, str) and hint.strip().upper() == UNIVERSAL_SCOPE_HINT:
        return ScopeResolution.UNIVERSAL
    return ScopeResolution.SCOPE_MAPPING_REQUIRED


@dataclass
class WorkbookReport:
    """What a returned workbook contains, and what is wrong with it."""

    path: str
    issues: list[Issue] = field(default_factory=list)
    selected_institutions: list[uuid.UUID] = field(default_factory=list)
    program_refs: dict[str, uuid.UUID] = field(default_factory=dict)
    source_refs: dict[str, uuid.UUID] = field(default_factory=dict)
    rows_by_sheet: dict[str, int] = field(default_factory=dict)
    scope_parked: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.WARNING]

    @property
    def is_importable(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        return dict(Counter(issue.code.value for issue in self.issues))

    def summary(self) -> str:
        head = (
            f"{len(self.selected_institutions)} selected, "
            f"{len(self.program_refs)} programmes, {len(self.source_refs)} sources"
        )
        if self.is_importable:
            tail = f"no errors, {len(self.warnings)} warning(s)"
        else:
            tail = f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        return f"{head}; {tail}"


def validate_workbook(
    connection: Connection,
    path: str | Path,
    *,
    expected_selected: int | None = None,
) -> WorkbookReport:
    """Read a returned workbook and report every problem found.

    `expected_selected` is a **validation** figure, never a selection instruction. If
    the client was asked for 35 and marked 34, that is an error to raise with them --
    not a cue to pick the missing one.
    """
    loaded = load_workbook(path)
    report = WorkbookReport(path=str(path))

    sheets = {spec.name: spec for spec in TEMPLATE_SHEETS}
    for name in sheets:
        if name not in loaded.sheet_names:
            report.issues.append(
                Issue(
                    IssueCode.SHEET_MISSING,
                    Severity.ERROR,
                    name,
                    None,
                    "the sheet is missing; export a fresh template and re-enter the data "
                    "rather than renaming a sheet",
                )
            )

    selected = _validate_selection(connection, loaded, report, expected_selected=expected_selected)
    destinations = _destinations(connection)
    _collect_program_refs(connection, loaded, report)
    _collect_source_refs(connection, loaded, report)
    for sheet_name in FACT_SHEETS:
        if sheet_name in loaded.sheet_names:
            _validate_fact_sheet(
                loaded,
                sheets[sheet_name],
                report,
                selected=selected,
                destinations=destinations,
            )

    logger.info(
        "pilot_workbook_validated",
        path=str(path),
        errors=len(report.errors),
        warnings=len(report.warnings),
        selected=len(report.selected_institutions),
    )
    return report


# ---------------------------------------------------------------------------
# Reading a sheet
# ---------------------------------------------------------------------------


def _destinations(connection: Connection) -> dict[uuid.UUID, str]:
    """target_institution.id -> destination_code, for the student-category check."""
    from sqlalchemy import text

    return {
        row[0]: row[1]
        for row in connection.execute(
            text(
                "SELECT id, destination_code FROM target_institution "
                " WHERE destination_code IS NOT NULL"
            )
        ).all()
    }


def _header(loaded: LoadedWorkbook, sheet: str) -> dict[str, int]:
    """Column name -> column index, from row 1."""
    header: dict[str, int] = {}
    for column in range(1, loaded.max_column(sheet) + 1):
        value = loaded.value(sheet, 1, column)
        if isinstance(value, str) and value.strip():
            header[value.strip()] = column
    return header


def _cell(loaded: LoadedWorkbook, sheet: str, row: int, header: dict[str, int], name: str) -> Any:
    column = header.get(name)
    return loaded.value(sheet, row, column) if column else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _data_rows(loaded: LoadedWorkbook, sheet: str, header: dict[str, int]) -> list[int]:
    """Row numbers that carry anything at all, below the header."""
    rows: list[int] = []
    for row in range(2, loaded.max_row(sheet) + 1):
        if any(loaded.value(sheet, row, column) is not None for column in header.values()):
            rows.append(row)
    return rows


def _require_columns(
    loaded: LoadedWorkbook, spec: TemplateSheet, report: WorkbookReport
) -> dict[str, int] | None:
    header = _header(loaded, spec.name)
    missing = [
        column.name
        for column in spec.columns
        if column.requirement in {"REQUIRED", "CONDITIONAL"} and column.name not in header
    ]
    if missing:
        report.issues.append(
            Issue(
                IssueCode.COLUMN_MISSING,
                Severity.ERROR,
                spec.name,
                1,
                f"these columns are missing or renamed: {', '.join(missing)}",
            )
        )
        return None
    return header


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _validate_selection(
    connection: Connection,
    loaded: LoadedWorkbook,
    report: WorkbookReport,
    *,
    expected_selected: int | None,
) -> set[uuid.UUID]:
    sheet = "Pilot_Universities"
    if sheet not in loaded.sheet_names:
        return set()
    spec = next(s for s in TEMPLATE_SHEETS if s.name == sheet)
    header = _require_columns(loaded, spec, report)
    if header is None:
        return set()

    selected: set[uuid.UUID] = set()
    for row in _data_rows(loaded, sheet, header):
        raw_id = _cell(loaded, sheet, row, header, MATCH_KEY_COLUMN)
        match = pilot_matching.resolve_row(
            connection,
            raw_id=raw_id,
            raw_name=_text(_cell(loaded, sheet, row, header, "qs_name")) or None,
            sheet=sheet,
            row_number=row,
        )
        if not match.is_resolved:
            report.issues.append(
                Issue(IssueCode.TARGET_UNRESOLVED, Severity.ERROR, sheet, row, match.message)
            )
            continue

        assert match.target_institution_id is not None
        answer = _text(_cell(loaded, sheet, row, header, "selected")).upper()

        # An unrecognised answer is an error, never a silent "no". A collector who
        # writes Y, 是 or x means yes; treating that as not-selected would drop an
        # institution from the pilot without telling anybody, and the count would
        # still look plausible.
        if answer and answer not in {value.upper() for value in PILOT_SELECTION_VALUES}:
            report.issues.append(
                Issue(
                    IssueCode.SELECTED_VALUE_UNRECOGNISED,
                    Severity.ERROR,
                    sheet,
                    row,
                    f"selected is {answer!r}. Please use exactly YES, NO or UNDECIDED "
                    "-- we will not guess whether this counts as selected.",
                )
            )
            continue

        if answer != "YES":
            continue

        selected.add(match.target_institution_id)
        for column_name in ("official_name_en", "official_homepage"):
            if not _text(_cell(loaded, sheet, row, header, column_name)):
                report.issues.append(
                    Issue(
                        IssueCode.SELECTED_WITHOUT_DETAILS,
                        Severity.ERROR,
                        sheet,
                        row,
                        f"marked selected = YES but {column_name} is blank",
                    )
                )

    report.selected_institutions = sorted(selected, key=str)
    report.rows_by_sheet[sheet] = len(_data_rows(loaded, sheet, header))

    if expected_selected is not None and len(selected) != expected_selected:
        report.issues.append(
            Issue(
                IssueCode.SELECTED_COUNT_MISMATCH,
                Severity.ERROR,
                sheet,
                None,
                f"{len(selected)} institutions are marked selected = YES, but "
                f"{expected_selected} were expected. Please mark exactly "
                f"{expected_selected} — we will not choose the difference.",
            )
        )
    return selected


# ---------------------------------------------------------------------------
# Workbook-local references
# ---------------------------------------------------------------------------


def _collect_refs(
    connection: Connection,
    loaded: LoadedWorkbook,
    report: WorkbookReport,
    *,
    sheet_name: str,
    ref_column: str,
    pattern: str,
    malformed: IssueCode,
    duplicate: IssueCode,
) -> dict[str, uuid.UUID]:
    """Read a definition sheet's refs, returning ref -> owning institution."""
    if sheet_name not in loaded.sheet_names:
        return {}
    spec = next(s for s in TEMPLATE_SHEETS if s.name == sheet_name)
    header = _require_columns(loaded, spec, report)
    if header is None:
        return {}

    compiled = re.compile(pattern)
    defined: dict[str, uuid.UUID] = {}
    seen_rows: dict[str, int] = {}

    for row in _data_rows(loaded, sheet_name, header):
        ref = _text(_cell(loaded, sheet_name, row, header, ref_column))
        if not ref:
            report.issues.append(
                Issue(
                    IssueCode.REQUIRED_VALUE_MISSING,
                    Severity.ERROR,
                    sheet_name,
                    row,
                    f"{ref_column} is blank; every row on this sheet needs its own code",
                )
            )
            continue
        if not compiled.match(ref):
            report.issues.append(
                Issue(
                    malformed,
                    Severity.ERROR,
                    sheet_name,
                    row,
                    f"{ref_column} {ref!r} is not in the expected form "
                    f"({pattern.strip('^$')}), e.g. {ref_column[0].upper()}0001",
                )
            )
            continue
        if ref in defined:
            report.issues.append(
                Issue(
                    duplicate,
                    Severity.ERROR,
                    sheet_name,
                    row,
                    f"{ref_column} {ref} is already used on row {seen_rows[ref]}. "
                    "Each code must be defined once — a copied row usually keeps the "
                    "code of the row it came from.",
                )
            )
            continue

        match = pilot_matching.resolve_row(
            connection,
            raw_id=_cell(loaded, sheet_name, row, header, MATCH_KEY_COLUMN),
            sheet=sheet_name,
            row_number=row,
        )
        if not match.is_resolved:
            report.issues.append(
                Issue(IssueCode.TARGET_UNRESOLVED, Severity.ERROR, sheet_name, row, match.message)
            )
            continue
        assert match.target_institution_id is not None
        defined[ref] = match.target_institution_id
        seen_rows[ref] = row

    report.rows_by_sheet[sheet_name] = len(_data_rows(loaded, sheet_name, header))
    return defined


def _collect_program_refs(
    connection: Connection, loaded: LoadedWorkbook, report: WorkbookReport
) -> None:
    report.program_refs = _collect_refs(
        connection,
        loaded,
        report,
        sheet_name="Programs",
        ref_column="program_ref",
        pattern=PROGRAM_REF_PATTERN,
        malformed=IssueCode.PROGRAM_REF_MALFORMED,
        duplicate=IssueCode.PROGRAM_REF_DUPLICATE,
    )


def _collect_source_refs(
    connection: Connection, loaded: LoadedWorkbook, report: WorkbookReport
) -> None:
    report.source_refs = _collect_refs(
        connection,
        loaded,
        report,
        sheet_name="Official_Sources",
        ref_column="source_ref",
        pattern=SOURCE_REF_PATTERN,
        malformed=IssueCode.SOURCE_REF_MALFORMED,
        duplicate=IssueCode.SOURCE_REF_DUPLICATE,
    )


# ---------------------------------------------------------------------------
# Fact sheets
# ---------------------------------------------------------------------------


def _status_column_name(spec: TemplateSheet) -> str | None:
    for column in spec.columns:
        if column.name.endswith("_status"):
            return column.name
    return None


#: The columns a status governs, per sheet. A `PUBLISHED` status with none of them
#: filled, or any of them filled with `OFFICIALLY_NOT_PUBLISHED`, is a contradiction
#: either way round.
#:
#: Language requirements deliberately accept EITHER an overall score or per-section
#: minimums: a page stating only "no component below 6.0" publishes a real
#: requirement and has no overall figure, and demanding one would make the collector
#: invent it -- the exact thing requirement 6 forbids.
_VALUE_COLUMNS: dict[str, tuple[str, ...]] = {
    "Admissions": ("official_text",),
    "Language_Requirements": ("overall_score", "subscore_minimums"),
    # U14: the *shape* is what a published fee always has. A VARIABLE fee has no
    # figure at all, so requiring `amount_min` here would reject the very case the
    # range model was added for.
    "Tuition": ("amount_kind",),
    "Deadlines": ("deadline_text_verbatim",),
}


def _validate_fact_sheet(
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    report: WorkbookReport,
    *,
    selected: set[uuid.UUID],
    destinations: dict[uuid.UUID, str],
) -> None:
    header = _require_columns(loaded, spec, report)
    if header is None:
        return

    status_column = _status_column_name(spec)
    value_columns = _VALUE_COLUMNS.get(spec.name, ())
    rows = _data_rows(loaded, spec.name, header)
    report.rows_by_sheet[spec.name] = len(rows)

    for row in rows:
        raw_id = _cell(loaded, spec.name, row, header, MATCH_KEY_COLUMN)
        institution = _parse_uuid(raw_id)
        if institution is None:
            report.issues.append(
                Issue(
                    IssueCode.TARGET_UNRESOLVED,
                    Severity.ERROR,
                    spec.name,
                    row,
                    f"{MATCH_KEY_COLUMN} is blank or not a valid code; copy it from the "
                    "Pilot_Universities sheet",
                )
            )
        elif selected and institution not in selected:
            report.issues.append(
                Issue(
                    IssueCode.DATA_FOR_UNSELECTED_INSTITUTION,
                    Severity.WARNING,
                    spec.name,
                    row,
                    "this institution is not marked selected = YES on "
                    "Pilot_Universities; the row will be kept but not published",
                )
            )

        # Without a resolved institution there is no scope to resolve a reference
        # against, and a second error on the same row would point at the wrong cell.
        if institution is None:
            continue

        _check_shape(
            loaded,
            spec,
            report,
            row=row,
            header=header,
            destination=destinations.get(institution),
        )

        _check_ref(
            report,
            spec.name,
            row,
            ref=_text(_cell(loaded, spec.name, row, header, "program_ref")),
            defined=report.program_refs,
            institution=institution,
            unknown=IssueCode.PROGRAM_REF_UNKNOWN,
            mismatch=IssueCode.PROGRAM_REF_INSTITUTION_MISMATCH,
            label="program_ref",
            owner_sheet="Programs",
            required=False,
        )

        status = _text(_cell(loaded, spec.name, row, header, status_column or "")).upper()
        source_ref = _text(_cell(loaded, spec.name, row, header, "source_ref"))

        _check_ref(
            report,
            spec.name,
            row,
            ref=source_ref,
            defined=report.source_refs,
            institution=institution,
            unknown=IssueCode.SOURCE_REF_UNKNOWN,
            mismatch=IssueCode.SOURCE_REF_INSTITUTION_MISMATCH,
            label="source_ref",
            owner_sheet="Official_Sources",
            required=False,
        )

        # A high-risk fact that asserts anything must cite the page it came from --
        # including OFFICIALLY_NOT_PUBLISHED, which is a claim about what a specific
        # page does not say and is meaningless without naming the page.
        if spec.name in HIGH_RISK_SHEETS and status in ASSERTED_STATUSES and not source_ref:
            report.issues.append(
                Issue(
                    IssueCode.SOURCE_REF_MISSING,
                    Severity.ERROR,
                    spec.name,
                    row,
                    f"status is {status} but no source_ref is given. Add the page to "
                    "Official_Sources and put its code here.",
                )
            )

        if value_columns:
            present = [
                name for name in value_columns if _text(_cell(loaded, spec.name, row, header, name))
            ]
            value_column = " or ".join(value_columns)
            value = ", ".join(present)
            if status == "PUBLISHED" and not present:
                report.issues.append(
                    Issue(
                        IssueCode.PUBLISHED_WITHOUT_VALUE,
                        Severity.ERROR,
                        spec.name,
                        row,
                        f"status is PUBLISHED but {value_column} is blank. If the page "
                        "states nothing here, use OFFICIALLY_NOT_PUBLISHED instead — "
                        "please do not invent a value.",
                    )
                )
            if status == "OFFICIALLY_NOT_PUBLISHED" and value:
                report.issues.append(
                    Issue(
                        IssueCode.VALUE_WITH_UNPUBLISHED_STATUS,
                        Severity.ERROR,
                        spec.name,
                        row,
                        f"status is OFFICIALLY_NOT_PUBLISHED but {value_column} has a "
                        "value. Either the page states it (use PUBLISHED) or it does "
                        "not (clear the value).",
                    )
                )

        if "applicant_scope_hint" in header and status in ASSERTED_STATUSES:
            hint = _cell(loaded, spec.name, row, header, "applicant_scope_hint")
            if resolve_applicant_scope(hint) is ScopeResolution.SCOPE_MAPPING_REQUIRED:
                report.scope_parked += 1
                report.issues.append(
                    Issue(
                        IssueCode.SCOPE_MAPPING_REQUIRED,
                        Severity.WARNING,
                        spec.name,
                        row,
                        f"applicant_scope_hint {_text(hint)!r} needs a human to map it. "
                        "It will NOT be treated as applying to everybody.",
                    )
                )


def _check_shape(
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    report: WorkbookReport,
    *,
    row: int,
    header: dict[str, int],
    destination: str | None,
) -> None:
    """Checks that mirror a database CHECK the client cannot see.

    Every one of these would otherwise surface as a constraint violation partway
    through an import, on a file the validator had already called clean. Catching
    them here means the client fixes them while the pages are still open in front of
    them.
    """
    academic_year = _text(_cell(loaded, spec.name, row, header, "academic_year"))
    if academic_year and not ACADEMIC_YEAR_PATTERN.match(academic_year):
        report.issues.append(
            Issue(
                IssueCode.ACADEMIC_YEAR_MALFORMED,
                Severity.ERROR,
                spec.name,
                row,
                f"academic_year {academic_year!r} must be 2027 or 2027/28 "
                "(not '2027-28', and not '2027/28 entry').",
            )
        )

    category = _text(_cell(loaded, spec.name, row, header, "student_category_code"))
    if category and destination:
        allowed = STUDENT_CATEGORIES_BY_DESTINATION.get(destination)
        if allowed and category.upper() not in allowed:
            report.issues.append(
                Issue(
                    IssueCode.STUDENT_CATEGORY_NOT_IN_DESTINATION,
                    Severity.ERROR,
                    spec.name,
                    row,
                    f"{category} is not used in {destination}. "
                    f"{destination} uses {' and '.join(sorted(allowed))}.",
                )
            )

    if spec.name == "Deadlines":
        _check_deadline_parts(loaded, spec, report, row=row, header=header)
    elif spec.name == "Tuition":
        _check_tuition_amounts(loaded, spec, report, row=row, header=header)


def _check_tuition_amounts(
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    report: WorkbookReport,
    *,
    row: int,
    header: dict[str, int],
) -> None:
    """The U14 constraints, applied to the workbook rather than at insert time.

    Each of these is a database CHECK on `tuition`. Discovering them mid-import means
    a batch fails on row 400 of 900 with a message naming a constraint, long after
    the collector has closed the pages they would need to fix it.

    The rule this exists to protect is the one about midpoints: a RANGE keeps both
    ends, and nothing here or downstream computes an average from them.
    """

    def part(name: str) -> str:
        return _text(_cell(loaded, spec.name, row, header, name))

    kind = part("amount_kind").upper()
    raw_min, raw_max = part("amount_min"), part("amount_max")

    if not kind and not raw_min and not raw_max:
        return

    if kind and kind not in TUITION_AMOUNT_KINDS:
        report.issues.append(
            Issue(
                IssueCode.TUITION_AMOUNT_KIND_UNKNOWN,
                Severity.ERROR,
                spec.name,
                row,
                f"amount_kind {kind!r} is not one of "
                f"{', '.join(TUITION_AMOUNT_KINDS)}. OFFICIALLY_NOT_PUBLISHED belongs "
                "in amount_status, not here -- it is a status, not a shape.",
            )
        )
        return

    def number(label: str, raw: str) -> Decimal | None:
        if not raw:
            return None
        try:
            return Decimal(raw.replace(",", "").replace("\u00a0", "").strip())
        except (InvalidOperation, ValueError):
            report.issues.append(
                Issue(
                    IssueCode.TUITION_AMOUNT_NOT_A_NUMBER,
                    Severity.ERROR,
                    spec.name,
                    row,
                    f"{label} {raw!r} is not a number. Digits only -- no currency "
                    "symbol, no 'per year', no range in one cell.",
                )
            )
            return None

    low, high = number("amount_min", raw_min), number("amount_max", raw_max)
    if (raw_min and low is None) or (raw_max and high is None):
        return

    def complain(message: str) -> None:
        report.issues.append(
            Issue(
                IssueCode.TUITION_AMOUNT_SHAPE_INCONSISTENT,
                Severity.ERROR,
                spec.name,
                row,
                message,
            )
        )

    if not kind:
        complain(
            "an amount is filled in but amount_kind is blank; say which shape the "
            "page gives it in (EXACT for one figure, RANGE for two)"
        )
        return

    if kind == "EXACT":
        if low is None or high is None:
            complain("EXACT needs the same figure in both amount_min and amount_max")
        elif low != high:
            complain(
                f"EXACT means one figure, but amount_min ({low}) and amount_max "
                f"({high}) differ. If the page gives two figures, this is a RANGE."
            )
    elif kind == "RANGE":
        if low is None or high is None:
            complain("RANGE needs both amount_min and amount_max; use FROM or UP_TO for one end")
        elif low > high:
            complain(f"amount_min ({low}) is above amount_max ({high})")
    elif kind == "FROM":
        if low is None:
            complain("FROM needs amount_min -- the floor the page states")
        if high is not None:
            complain("FROM has no upper figure; if the page gives one, this is a RANGE")
    elif kind == "UP_TO":
        if high is None:
            complain("UP_TO needs amount_max -- the ceiling the page states")
        if low is not None:
            complain("UP_TO has no lower figure; if the page gives one, this is a RANGE")
    elif kind == "VARIABLE":
        if low is not None or high is not None:
            complain(
                "VARIABLE means the page gives no figure. If it gives one, use EXACT, "
                "RANGE, FROM or UP_TO."
            )
        if not part("official_text"):
            complain("VARIABLE needs official_text: their wording is the whole fact")

    if (low is not None and low < 0) or (high is not None and high < 0):
        complain("a fee cannot be negative")

    if (low is not None or high is not None) and not (
        part("currency_code") and part("billing_unit_code")
    ):
        complain(
            "an amount without a currency and a billing unit is a number, not a fee; "
            "fill in currency_code and billing_unit_code"
        )


def _check_deadline_parts(
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    report: WorkbookReport,
    *,
    row: int,
    header: dict[str, int],
) -> None:
    """The D17 rules, applied to the workbook rather than discovered at insert time.

    These exist to stop precision being manufactured: a time without a day, or a
    timezone without a time, is a claim the page did not make.
    """

    def part(name: str) -> str:
        return _text(_cell(loaded, spec.name, row, header, name))

    kind = part("deadline_kind").upper()
    year, month, day = part("year"), part("month"), part("day")
    month_part, time_of_day, timezone = (
        part("month_part"),
        part("time_of_day"),
        part("timezone"),
    )

    def complain(message: str) -> None:
        report.issues.append(
            Issue(IssueCode.DEADLINE_PARTS_INCONSISTENT, Severity.ERROR, spec.name, row, message)
        )

    if kind == "FIXED_DATE" and not year:
        complain("deadline_kind is FIXED_DATE but no year is given.")
    if kind in {"NO_FIXED_DEADLINE", "NOT_CURRENTLY_ACCEPTING"} and year:
        complain(
            f"deadline_kind is {kind}, so there is no date to record. "
            "Clear the year, month and day."
        )
    if day and not month:
        complain("a day was given without a month.")
    if month and not year:
        complain("a month was given without a year.")
    if time_of_day and not day:
        complain(
            "a time was given without an exact day. If the page does not give a day, "
            "it does not give a time either -- please clear it."
        )
    if timezone and not time_of_day:
        complain("a timezone was given without a time. Clear it unless the page states both.")
    if month_part and day:
        complain(
            "month_part and day cannot both be given. 'mid March' has no day; "
            "'15 March' has no month_part."
        )


def _check_ref(
    report: WorkbookReport,
    sheet: str,
    row: int,
    *,
    ref: str,
    defined: dict[str, uuid.UUID],
    institution: uuid.UUID | None,
    unknown: IssueCode,
    mismatch: IssueCode,
    label: str,
    owner_sheet: str,
    required: bool,
) -> None:
    if not ref:
        if required:
            report.issues.append(
                Issue(
                    IssueCode.REQUIRED_VALUE_MISSING,
                    Severity.ERROR,
                    sheet,
                    row,
                    f"{label} is blank",
                )
            )
        return
    if ref not in defined:
        report.issues.append(
            Issue(
                unknown,
                Severity.ERROR,
                sheet,
                row,
                f"{label} {ref} is not defined on the {owner_sheet} sheet",
            )
        )
        return
    # The ambiguous case: the code exists, but belongs to a different institution.
    # Resolving it by proximity or by name would be exactly the guess this design
    # exists to avoid.
    if institution is not None and defined[ref] != institution:
        report.issues.append(
            Issue(
                mismatch,
                Severity.ERROR,
                sheet,
                row,
                f"{label} {ref} belongs to a different institution on the "
                f"{owner_sheet} sheet. Check the {MATCH_KEY_COLUMN} in column A.",
            )
        )


def _parse_uuid(raw: object) -> uuid.UUID | None:
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        return None


__all__ = [
    "ASSERTED_STATUSES",
    "FACT_SHEETS",
    "UNIVERSAL_SCOPE_HINT",
    "Issue",
    "IssueCode",
    "ScopeResolution",
    "Severity",
    "WorkbookReport",
    "resolve_applicant_scope",
    "validate_workbook",
]
