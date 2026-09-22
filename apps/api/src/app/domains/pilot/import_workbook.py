"""Import a completed collection workbook into the staging plane (U12).

WHAT THIS DOES NOT DO
=====================
It does not create a `field_claim`, a `field_provenance` row, a `university`, a
`program`, a `source` or a `source_mapping`. It writes four `pilot_*` tables and
stops. Everything downstream -- identity resolution, source promotion, acquisition,
extraction, review, publication -- remains a separate, deliberate act.

That is the entire design. The workbook is a person's account of what they read; it
becomes evidence only when a source is registered, fetched and snapshotted, and this
importer must not be the shortcut around that. A collected URL therefore lands as a
`PENDING` candidate, never as a verified source: **a source is not born
`OFFICIAL_VERIFIED`** (C27), and `verification_state` is hardcoded to `PENDING` here
rather than read from any cell, so no column in any spreadsheet can set it.

It also fetches nothing. Step 4 requirement 15 applies unchanged: URLs are validated
and stored, never dereferenced, and `validate_source_url` still rejects anything that
is not HTTP/HTTPS before a row is written. Importing a URL is not a way past URL
safety.

VALIDATE FIRST, THEN IMPORT
===========================
`validate_workbook` runs first and an importable file is one with **no errors**. A
partial import is worse than a refused one: it leaves a submission that looks
complete, and the rows that failed are exactly the ones nobody knows are missing.
Warnings do not block -- a scope that needs mapping and a row for an unselected
institution are both things we want kept.

RE-IMPORT
=========
`file_sha256` is unique. The same bytes twice is recognised and writes nothing. A
corrected workbook is different bytes, so it becomes a **new** submission; the
previous one is marked `SUPERSEDED` and every one of its rows stays exactly as it
was. Nothing is overwritten, because the only reason to keep both is to compare them,
and a comparison against an edited row compares nothing.

`supersede=False` keeps the earlier submission `VALIDATED` -- for the case where two
genuinely different workbooks are being imported side by side rather than one
correcting the other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, insert, select, update

from app.core.logging import get_logger
from app.db.enums import FieldStatus, PilotFactValidationState, PilotImportStatus
from app.domains.onboarding.pilot_template import (
    MATCH_KEY_COLUMN,
    TEMPLATE_SHEETS,
    TEMPLATE_VERSION,
    TemplateSheet,
)
from app.domains.onboarding.pilot_workbook import (
    UNIVERSAL_SCOPE_HINT,
    ScopeResolution,
    WorkbookReport,
    resolve_applicant_scope,
    validate_workbook,
)
from app.domains.onboarding.urls import UrlRejectedError, validate_source_url
from app.domains.onboarding.workbook import LoadedWorkbook, load_workbook
from app.domains.pilot.models import (
    PilotCollectedFact,
    PilotCollectedProgram,
    PilotCollectedSource,
    PilotSelectedUniversity,
    PilotSubmission,
)

logger = get_logger(__name__)


class WorkbookNotImportableError(RuntimeError):
    """The file has validation errors. The report says which, by sheet and row."""

    def __init__(self, report: WorkbookReport) -> None:
        super().__init__(
            f"{len(report.errors)} error(s) in {report.path}; nothing was imported. "
            "Run the validator for the full list."
        )
        self.report = report


#: Which staged fact type each collection sheet produces, and which canonical field
#: path its status column governs. `field_path` is a *label* for a later reconciler,
#: not a promise that anything was written to that column.
_FACT_SHEETS: dict[str, tuple[str, str]] = {
    "Admissions": ("ADMISSION_REQUIREMENT", "requirement_text"),
    "Language_Requirements": ("LANGUAGE_REQUIREMENT", "overall_score"),
    "Tuition": ("TUITION", "amount"),
    "Deadlines": ("DEADLINE", "deadline_date"),
}

#: Columns that are never copied into `collected_values`: either they are already a
#: typed column on the row, or they are the identifiers that placed the row.
_NOT_COLLECTED_VALUES: frozenset[str] = frozenset(
    {
        MATCH_KEY_COLUMN,
        "program_ref",
        "source_ref",
        "source_url",
        "official_text",
        "applicant_scope_hint",
        "applicant_country_code",
        "qualification_hint",
        "notes",
        "amount_kind",
        "amount_min",
        "amount_max",
    }
)


@dataclass(slots=True)
class StagingImportReport:
    """What the import wrote. Safe to show an operator verbatim."""

    submission_id: uuid.UUID
    file_sha256: str
    original_filename: str
    #: True when these exact bytes were already imported and nothing was written.
    already_imported: bool
    selected_universities: int = 0
    institution_rows: int = 0
    programs: int = 0
    sources: int = 0
    facts: int = 0
    facts_by_sheet: dict[str, int] = field(default_factory=dict)
    scope_parked: int = 0
    superseded_submission_ids: list[uuid.UUID] = field(default_factory=list)
    rejected_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.already_imported:
            return (
                f"{self.original_filename} was already imported "
                f"(sha256 {self.file_sha256[:12]}...); nothing was written."
            )
        parts = [
            f"{self.original_filename}: {self.selected_universities} selected, "
            f"{self.programs} programmes, {self.sources} source candidates, "
            f"{self.facts} facts"
        ]
        if self.scope_parked:
            parts.append(f"{self.scope_parked} rows need a scope mapping")
        if self.superseded_submission_ids:
            parts.append(f"{len(self.superseded_submission_ids)} earlier submission(s) superseded")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return "; ".join(parts)


def import_pilot_workbook(
    connection: Connection,
    path: str | Path,
    *,
    expected_selected: int | None = None,
    imported_by: uuid.UUID | None = None,
    submitted_at: datetime | None = None,
    supersede: bool = True,
) -> StagingImportReport:
    """Import a completed workbook into staging, inside the caller's transaction.

    The caller owns the transaction boundary, so an import can be staged, inspected
    and rolled back. Nothing here commits.

    `expected_selected` is passed through to the validator as a **check**. It never
    selects anything: if the client was asked for 35 and marked 34, that is an error
    to take back to them, not a cue to pick the missing one.
    """
    loaded = load_workbook(path)

    existing = connection.execute(
        select(PilotSubmission.id, PilotSubmission.original_filename).where(
            PilotSubmission.file_sha256 == loaded.file_sha256
        )
    ).one_or_none()
    if existing is not None:
        logger.info(
            "pilot_workbook_import_skipped_identical_file",
            submission_id=str(existing.id),
            file_sha256=loaded.file_sha256,
        )
        return StagingImportReport(
            submission_id=existing.id,
            file_sha256=loaded.file_sha256,
            original_filename=existing.original_filename,
            already_imported=True,
        )

    report = validate_workbook(connection, path, expected_selected=expected_selected)
    if not report.is_importable:
        raise WorkbookNotImportableError(report)

    submission_id = uuid.uuid4()
    result = StagingImportReport(
        submission_id=submission_id,
        file_sha256=loaded.file_sha256,
        original_filename=loaded.file_name,
        already_imported=False,
        scope_parked=report.scope_parked,
    )

    connection.execute(
        insert(PilotSubmission).values(
            id=submission_id,
            file_sha256=loaded.file_sha256,
            original_filename=loaded.file_name,
            file_byte_size=loaded.file_byte_size,
            template_version=TEMPLATE_VERSION,
            submitted_at=submitted_at,
            imported_by=imported_by,
            selected_university_count=len(report.selected_institutions),
            expected_university_count=expected_selected,
            import_status=PilotImportStatus.VALIDATED.value,
            validation_summary={
                "counts": report.counts(),
                "rows_by_sheet": report.rows_by_sheet,
                "warnings": [str(issue) for issue in report.warnings],
                "scope_parked": report.scope_parked,
            },
        )
    )

    specs = {spec.name: spec for spec in TEMPLATE_SHEETS}
    _import_selection(connection, loaded, specs["Pilot_Universities"], submission_id, result)
    _import_programs(connection, loaded, specs["Programs"], submission_id, result)
    _import_sources(connection, loaded, specs["Official_Sources"], submission_id, result)
    for sheet_name in _FACT_SHEETS:
        _import_facts(connection, loaded, specs[sheet_name], submission_id, result)

    if supersede:
        _supersede_earlier(connection, submission_id, result)

    logger.info(
        "pilot_workbook_imported",
        submission_id=str(submission_id),
        file_sha256=loaded.file_sha256,
        selected=result.selected_universities,
        sources=result.sources,
        facts=result.facts,
    )
    return result


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


def _import_selection(
    connection: Connection,
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    submission_id: uuid.UUID,
    result: StagingImportReport,
) -> None:
    """`Pilot_Universities`. Writes no `university` row -- see the model docstring."""
    header = _header(loaded, spec)
    rows: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()

    for row_no in _data_rows(loaded, spec, header):
        institution = _uuid(_cell(loaded, spec.name, row_no, header, MATCH_KEY_COLUMN))
        if institution is None or institution in seen:
            # The validator already reported both as errors, so reaching here means
            # a blank trailing row. Skipping is correct; re-reporting is noise.
            continue
        seen.add(institution)

        raw_selection = _text(_cell(loaded, spec.name, row_no, header, "selected"))
        rows.append(
            {
                "submission_id": submission_id,
                "target_institution_id": institution,
                "sheet_row_no": row_no,
                "is_selected": raw_selection.strip().upper() == "YES",
                "selection_value": raw_selection or None,
                "official_name_en": _text(
                    _cell(loaded, spec.name, row_no, header, "official_name_en")
                )
                or None,
                "official_name_zh": _text(
                    _cell(loaded, spec.name, row_no, header, "official_name_zh")
                )
                or None,
                "official_homepage": _homepage(
                    _text(_cell(loaded, spec.name, row_no, header, "official_homepage")),
                    result,
                ),
                "city": _text(_cell(loaded, spec.name, row_no, header, "city")) or None,
                "collector_notes": _text(_cell(loaded, spec.name, row_no, header, "notes")) or None,
            }
        )

    if rows:
        connection.execute(insert(PilotSelectedUniversity), rows)
    result.institution_rows = len(rows)
    result.selected_universities = sum(1 for row in rows if row["is_selected"])


def _import_programs(
    connection: Connection,
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    submission_id: uuid.UUID,
    result: StagingImportReport,
) -> None:
    header = _header(loaded, spec)
    rows: list[dict[str, Any]] = []

    for row_no in _data_rows(loaded, spec, header):
        institution = _uuid(_cell(loaded, spec.name, row_no, header, MATCH_KEY_COLUMN))
        program_ref = _text(_cell(loaded, spec.name, row_no, header, "program_ref")).upper()
        if institution is None or not program_ref:
            continue
        rows.append(
            {
                "submission_id": submission_id,
                "program_ref": program_ref,
                "target_institution_id": institution,
                "sheet_row_no": row_no,
                "program_name_en": _text(
                    _cell(loaded, spec.name, row_no, header, "program_name_en")
                ),
                "degree_level_code": _code(loaded, spec, row_no, header, "degree_level_code"),
                "discipline_code": _code(loaded, spec, row_no, header, "discipline_code"),
                "discipline_hint": _text(
                    _cell(loaded, spec.name, row_no, header, "discipline_hint")
                )
                or None,
                "faculty_or_school": _text(
                    _cell(loaded, spec.name, row_no, header, "faculty_or_school")
                )
                or None,
                "study_mode": _code(loaded, spec, row_no, header, "study_mode"),
                "delivery_mode": _code(loaded, spec, row_no, header, "delivery_mode"),
                "duration_value": _int(_cell(loaded, spec.name, row_no, header, "duration_value")),
                "duration_unit": _code(loaded, spec, row_no, header, "duration_unit"),
                "campus_name": _text(_cell(loaded, spec.name, row_no, header, "campus_name"))
                or None,
                "lifecycle_status": _code(loaded, spec, row_no, header, "lifecycle_status"),
                "collector_notes": _text(_cell(loaded, spec.name, row_no, header, "notes")) or None,
            }
        )

    if rows:
        connection.execute(insert(PilotCollectedProgram), rows)
    result.programs = len(rows)


def _import_sources(
    connection: Connection,
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    submission_id: uuid.UUID,
    result: StagingImportReport,
) -> None:
    """`Official_Sources`. Every row lands `PENDING`, whatever the workbook says."""
    header = _header(loaded, spec)
    rows: list[dict[str, Any]] = []

    for row_no in _data_rows(loaded, spec, header):
        institution = _uuid(_cell(loaded, spec.name, row_no, header, MATCH_KEY_COLUMN))
        source_ref = _text(_cell(loaded, spec.name, row_no, header, "source_ref")).upper()
        raw_url = _text(_cell(loaded, spec.name, row_no, header, "official_url"))
        if institution is None or not source_ref or not raw_url:
            continue

        try:
            url = validate_source_url(raw_url)
        except UrlRejectedError as exc:
            # Refusing the row rather than the file: one unusable URL among two
            # hundred should not cost the other 199, and the rejection is reported.
            result.rejected_urls.append(f"{spec.name} row {row_no}: {exc}")
            continue

        rows.append(
            {
                "submission_id": submission_id,
                "source_ref": source_ref,
                "target_institution_id": institution,
                "sheet_row_no": row_no,
                "source_type": _text(_cell(loaded, spec.name, row_no, header, "source_type")),
                "degree_scope": _code(loaded, spec, row_no, header, "degree_level"),
                "official_url": url.original,
                "normalized_url": url.normalized,
                "url_sha256": url.sha256,
                "host": url.host,
                "checked_at": _date(_cell(loaded, spec.name, row_no, header, "checked_at")),
                "is_third_party": _yes_no(
                    _text(_cell(loaded, spec.name, row_no, header, "is_third_party"))
                ),
                "collector_notes": _text(_cell(loaded, spec.name, row_no, header, "notes")) or None,
                # Hardcoded, not read from a cell. No spreadsheet column can set a
                # candidate's verification state (C27, U15).
                "verification_state": "PENDING",
            }
        )

    if rows:
        connection.execute(insert(PilotCollectedSource), rows)
    result.sources = len(rows)


def _import_facts(
    connection: Connection,
    loaded: LoadedWorkbook,
    spec: TemplateSheet,
    submission_id: uuid.UUID,
    result: StagingImportReport,
) -> None:
    header = _header(loaded, spec)
    fact_type, field_path = _FACT_SHEETS[spec.name]
    status_column = next(
        (column.name for column in spec.columns if column.name.endswith("_status")), None
    )
    value_columns = [
        column.name
        for column in spec.columns
        if column.kind == "collected" and column.name not in _NOT_COLLECTED_VALUES
    ]
    rows: list[dict[str, Any]] = []

    for row_no in _data_rows(loaded, spec, header):
        institution = _uuid(_cell(loaded, spec.name, row_no, header, MATCH_KEY_COLUMN))
        if institution is None:
            continue

        collected = {
            name: _jsonable(_cell(loaded, spec.name, row_no, header, name))
            for name in value_columns
        }
        collected = {name: value for name, value in collected.items() if value is not None}

        scope_hint = _text(_cell(loaded, spec.name, row_no, header, "applicant_scope_hint"))
        resolution = resolve_applicant_scope(scope_hint)
        validation_state = (
            PilotFactValidationState.OK
            if resolution is ScopeResolution.UNIVERSAL
            else PilotFactValidationState.SCOPE_MAPPING_REQUIRED
        )

        rows.append(
            {
                "submission_id": submission_id,
                "sheet_name": spec.name,
                "sheet_row_no": row_no,
                "target_institution_id": institution,
                "program_ref": _text(
                    _cell(loaded, spec.name, row_no, header, "program_ref")
                ).upper()
                or None,
                "source_ref": _text(_cell(loaded, spec.name, row_no, header, "source_ref")).upper()
                or None,
                "fact_type": fact_type,
                "field_path": field_path,
                "field_status": _field_status(
                    _text(_cell(loaded, spec.name, row_no, header, status_column))
                    if status_column
                    else ""
                ),
                "collected_values": collected or None,
                "amount_kind": _code(loaded, spec, row_no, header, "amount_kind"),
                "amount_min": _decimal(_cell(loaded, spec.name, row_no, header, "amount_min")),
                "amount_max": _decimal(_cell(loaded, spec.name, row_no, header, "amount_max")),
                "official_text": _text(_cell(loaded, spec.name, row_no, header, "official_text"))
                or None,
                "source_url": _text(_cell(loaded, spec.name, row_no, header, "source_url")) or None,
                "applicant_scope_hint": scope_hint or None,
                "applicant_country_code": _code(
                    loaded, spec, row_no, header, "applicant_country_code"
                ),
                "qualification_hint": _text(
                    _cell(loaded, spec.name, row_no, header, "qualification_hint")
                )
                or None,
                "collector_notes": _text(_cell(loaded, spec.name, row_no, header, "notes")) or None,
                "validation_state": validation_state.value,
            }
        )

    if rows:
        connection.execute(insert(PilotCollectedFact), rows)
    result.facts += len(rows)
    result.facts_by_sheet[spec.name] = len(rows)


def _supersede_earlier(
    connection: Connection, submission_id: uuid.UUID, result: StagingImportReport
) -> None:
    """Mark previously-validated submissions as superseded. Deletes nothing.

    The rows of a superseded submission are untouched and stay queryable: that is the
    whole point of not overwriting, and a comparison needs both sides intact.
    """
    superseded = connection.execute(
        update(PilotSubmission)
        .where(
            PilotSubmission.id != submission_id,
            PilotSubmission.import_status == PilotImportStatus.VALIDATED.value,
        )
        .values(import_status=PilotImportStatus.SUPERSEDED.value)
        .returning(PilotSubmission.id)
    ).scalars()
    result.superseded_submission_ids = list(superseded)


# ---------------------------------------------------------------------------
# Cell reading
# ---------------------------------------------------------------------------


def _header(loaded: LoadedWorkbook, spec: TemplateSheet) -> dict[str, int]:
    header: dict[str, int] = {}
    for column in range(1, loaded.max_column(spec.name) + 1):
        name = _text(loaded.value(spec.name, 1, column))
        if name:
            header[name] = column
    return header


def _data_rows(loaded: LoadedWorkbook, spec: TemplateSheet, header: dict[str, int]) -> list[int]:
    """Row numbers holding at least one value. Row 1 is the header."""
    if not header:
        return []
    columns = set(header.values())
    rows = {
        row
        for (row, column), value in loaded.cells.get(spec.name, {}).items()
        if row > 1 and column in columns and _text(value)
    }
    return sorted(rows)


def _cell(
    loaded: LoadedWorkbook, sheet: str, row: int, header: dict[str, int], name: str
) -> object:
    column = header.get(name)
    return None if column is None else loaded.value(sheet, row, column)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _code(
    loaded: LoadedWorkbook, spec: TemplateSheet, row: int, header: dict[str, int], name: str
) -> str | None:
    """A controlled-vocabulary cell, upper-cased. Absent column yields None."""
    value = _text(_cell(loaded, spec.name, row, header, name))
    return value.upper() or None


def _uuid(value: object) -> uuid.UUID | None:
    text = _text(value)
    try:
        return uuid.UUID(text)
    except (ValueError, AttributeError):
        return None


def _int(value: object) -> int | None:
    text = _text(value)
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    text = _text(value).replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _yes_no(value: str) -> bool | None:
    """Tri-state on purpose: blank means the collector did not say."""
    normalized = value.strip().upper()
    if normalized == "YES":
        return True
    if normalized == "NO":
        return False
    return None


def _field_status(value: str) -> str:
    """A blank status is `NOT_CHECKED` -- never `PUBLISHED` (D2).

    An unrecognised value is also `NOT_CHECKED`. The validator rejects those before
    an import can happen, so this branch means a trailing row, and reading "PUBLISHD"
    as published would be exactly the silent misrepresentation D2 exists to prevent.
    """
    normalized = value.strip().upper()
    try:
        return FieldStatus(normalized).value
    except ValueError:
        return FieldStatus.NOT_CHECKED.value


def _homepage(value: str, result: StagingImportReport) -> str | None:
    if not value:
        return None
    try:
        return validate_source_url(value).original
    except UrlRejectedError as exc:
        result.rejected_urls.append(f"Pilot_Universities homepage: {exc}")
        return None


def _jsonable(value: object) -> object:
    """A cell as JSON can hold it, with no precision invented.

    A `Decimal` becomes a string rather than a float: `28000.50` through a float is
    `28000.499999999996`, and a fee is not the kind of number to round-trip through
    binary floating point.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool | int | str):
        return value.strip() if isinstance(value, str) and value.strip() else value
    if isinstance(value, float):
        return value
    return str(value)


__all__ = [
    "UNIVERSAL_SCOPE_HINT",
    "StagingImportReport",
    "WorkbookNotImportableError",
    "import_pilot_workbook",
]
