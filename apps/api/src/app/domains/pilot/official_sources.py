"""Import the client's final official-source list into staging (Step 5A).

WHAT THIS FILE IS
=================
`university_official_sources.xlsx`: 35 institutions, one row each, eleven URL columns
plus a free-text note. It answers one question — *which pages should we eventually
fetch for each pilot university* — and it is authoritative for that and nothing else.

It is **not** a collected-facts workbook. It carries no programme, no fee, no
deadline, no test score, and this module creates none. Everything it produces is a
staging row and a `PENDING` verification-queue entry.

THE THREE THINGS THAT COULD GO WRONG, AND WHAT STOPS THEM
=========================================================
**Matching the wrong university.** Resolution is exact: the supplied name is folded
by `normalize_institution_name` — case, Unicode form, whitespace, decorative
punctuation, nothing else — and must hit exactly one `target_institution`. The
country column is then cross-checked against the destination we already recorded, and
a disagreement is an error rather than a tie-break. There is no similarity threshold
anywhere: a threshold loose enough to match `UCL` to `University College London` also
matches `University of Canterbury` to `Canterbury Christ Church University`, and that
failure is silent and permanent. A single unresolved row aborts the whole import.

**Losing a claimed responsibility to deduplication.** One page legitimately answers
for several categories — in this file, 66 of 385 URL cells repeat a URL already given
under another heading. Every cell therefore becomes its own row (one claimed
responsibility), and `duplicate_of_source_ref` marks the repeats so the *physical*
pages remain countable and fetchable exactly once. Dropping the repeats would discard
the claim that Imperial's `/study/apply/` is the deadlines page as well as the
postgraduate-admissions page.

**Inventing a category.** Three additional-source URLs are genuinely new and the
workbook says nothing about what they are. They import as `UNCLASSIFIED`. Calling
them `OFFICIAL_PDF` because of the column they arrived in would be inventing a claim,
and one of them is not even a PDF.

NOTHING IS FETCHED
==================
Every URL passes `validate_source_url` — http/https only, no IP literals, no
credentials, no local names (D23) — and is then stored. None is dereferenced, here or
anywhere downstream of here. That validation is a storage floor and not the SSRF
defence: the acquisition layer must still resolve, reject non-public addresses, pin
the address and re-check every redirect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, insert, select, text, update

from app.core.logging import get_logger
from app.db.enums import (
    UNCLASSIFIED_SOURCE_TYPE,
    ActorType,
    PilotImportStatus,
    PilotSubmissionKind,
    SourceCandidateState,
    SourceCategory,
)
from app.domains.onboarding.naming import normalize_institution_name
from app.domains.onboarding.regions import resolve_destination
from app.domains.onboarding.urls import UrlRejectedError, validate_source_url
from app.domains.onboarding.workbook import LoadedWorkbook, load_workbook
from app.domains.pilot.models import (
    PilotCollectedSource,
    PilotSelectedUniversity,
    PilotSubmission,
)
from app.domains.versioning.models import AuditLog

logger = get_logger(__name__)

#: The pilot's final size, fixed by the client after the earlier 36 target. One
#: constant so a validator, a report and a test cannot drift apart; the PRD's
#: historical ">= 36 institutions" is left as written, because it is what was asked
#: for at the time.
PILOT_INSTITUTION_COUNT = 35

SHEET_NAME = "Universities"
NAME_COLUMN = "University Name"
REGION_COLUMN = "Country/Region"
NOTES_COLUMN = "Notes"

#: Workbook heading -> the category the collector is claiming for that page.
#:
#: Order matters and is the workbook's own: it decides which cell becomes the
#: *physical* row when a URL repeats, and the homepage-first ordering means a
#: repeated URL is attributed to the most general page that named it.
COLUMN_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Official Homepage", SourceCategory.UNIVERSITY_HOME.value),
    ("Undergraduate Admissions URL", SourceCategory.UNDERGRADUATE_ADMISSIONS.value),
    ("Postgraduate Admissions URL", SourceCategory.POSTGRADUATE_ADMISSIONS.value),
    ("PhD Admissions URL", SourceCategory.PHD_ADMISSIONS.value),
    ("Program/Course Catalogue URL", SourceCategory.PROGRAM_CATALOG.value),
    ("Entry Requirements URL", SourceCategory.ENTRY_REQUIREMENTS.value),
    ("English Language Requirements URL", SourceCategory.LANGUAGE_REQUIREMENTS.value),
    ("Tuition/Fees URL", SourceCategory.TUITION_FEES.value),
    ("Application Deadlines URL", SourceCategory.APPLICATION_DEADLINES.value),
    ("Academic Calendar URL", SourceCategory.ACADEMIC_CALENDAR.value),
)

#: Optional, and carries no category. The heading says the collector thought the page
#: mattered; it does not say what the page is, so nothing here guesses.
ADDITIONAL_COLUMN = "Important Additional Source URL"

#: Which headings the file must have. The additional column is deliberately absent:
#: a file without it is still importable.
REQUIRED_COLUMNS: tuple[str, ...] = (
    NAME_COLUMN,
    REGION_COLUMN,
    *(heading for heading, _ in COLUMN_CATEGORIES),
)

#: The degree audience each category serves, where the column name states it.
#: Absence means "not stated", never "serves everyone" -- `source_degree_scope`
#: membership is positive (D22), so an unstated scope covers nobody until a human
#: says otherwise.
COLUMN_DEGREE_SCOPES: dict[str, str] = {
    SourceCategory.UNDERGRADUATE_ADMISSIONS.value: "UNDERGRADUATE",
    SourceCategory.POSTGRADUATE_ADMISSIONS.value: "TAUGHT_POSTGRADUATE",
    SourceCategory.PHD_ADMISSIONS.value: "RESEARCH_POSTGRADUATE",
}

TEMPLATE_VERSION = "official-sources-2026.09"


class OfficialSourceListError(RuntimeError):
    """The file cannot be imported. The message names every problem found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            f"{len(problems)} problem(s); nothing was imported:\n  " + "\n  ".join(problems)
        )
        self.problems = problems


@dataclass(frozen=True, slots=True)
class ResolvedInstitution:
    """One workbook row matched to a target we already hold."""

    sheet_row_no: int
    supplied_name: str
    supplied_region: str
    target_institution_id: uuid.UUID
    match_key: str
    destination_code: str | None
    qs_name: str


@dataclass(slots=True)
class OfficialSourceReport:
    """What the import found and wrote. Safe to show an operator verbatim."""

    file_sha256: str
    original_filename: str
    already_imported: bool
    submission_id: uuid.UUID | None = None

    institutions_in_file: int = 0
    institutions_resolved: int = 0
    institutions_ambiguous: int = 0
    institutions_unknown: int = 0

    core_url_cells: int = 0
    additional_url_cells: int = 0
    blank_core_cells: int = 0

    responsibilities: int = 0
    physical_sources: int = 0
    duplicate_responsibilities: int = 0
    unclassified_sources: int = 0
    #: Unclassified rows that are also a *distinct* page -- the ones that genuinely
    #: need a human to say what they are. The rest repeat a page already classified
    #: under another heading, so nobody has to look at them twice.
    unclassified_distinct_sources: int = 0
    rejected_urls: list[str] = field(default_factory=list)

    by_category: dict[str, int] = field(default_factory=dict)
    by_institution: dict[str, int] = field(default_factory=dict)
    duplicates_by_institution: dict[str, int] = field(default_factory=dict)

    pilot_wave_assigned: int = 0
    pilot_wave_cleared: int = 0
    superseded_submission_ids: list[uuid.UUID] = field(default_factory=list)
    readme_notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.already_imported:
            return (
                f"{self.original_filename} was already imported "
                f"(sha256 {self.file_sha256[:12]}...); nothing was written."
            )
        return (
            f"{self.original_filename}: {self.institutions_resolved}/"
            f"{self.institutions_in_file} institutions resolved, "
            f"{self.responsibilities} claimed source responsibilities over "
            f"{self.physical_sources} distinct pages "
            f"({self.duplicate_responsibilities} repeat a page already listed, "
            f"{self.unclassified_distinct_sources} distinct pages unclassified)"
        )


def import_official_source_list(
    connection: Connection,
    path: str | Path,
    *,
    expected_institutions: int = PILOT_INSTITUTION_COUNT,
    imported_by: uuid.UUID | None = None,
    submitted_at: datetime | None = None,
    assign_pilot_wave: bool = True,
    supersede: bool = True,
) -> OfficialSourceReport:
    """Import the official-source list inside the caller's transaction.

    The caller owns the transaction boundary, so an import can be staged, inspected
    and rolled back. Nothing here commits and nothing here fetches.

    `expected_institutions` is a **check**. A file with 34 or 36 rows is a question
    for the client, never a cue to add or drop one.
    """
    loaded = load_workbook(path)

    existing = connection.execute(
        select(PilotSubmission.id, PilotSubmission.original_filename).where(
            PilotSubmission.file_sha256 == loaded.file_sha256
        )
    ).one_or_none()
    if existing is not None:
        logger.info(
            "official_source_list_skipped_identical_file",
            submission_id=str(existing.id),
            file_sha256=loaded.file_sha256,
        )
        return OfficialSourceReport(
            file_sha256=loaded.file_sha256,
            original_filename=existing.original_filename,
            already_imported=True,
            submission_id=existing.id,
        )

    report = OfficialSourceReport(
        file_sha256=loaded.file_sha256,
        original_filename=loaded.file_name,
        already_imported=False,
    )
    header = _header(loaded, report)
    rows = _data_rows(loaded, header)
    report.institutions_in_file = len(rows)
    report.readme_notes.extend(_readme_notes(loaded, len(rows)))

    problems: list[str] = []
    resolved = _resolve_institutions(connection, loaded, header, rows, report, problems)
    if len(resolved) != len(rows) or problems:
        raise OfficialSourceListError(problems)
    if report.institutions_in_file != expected_institutions:
        raise OfficialSourceListError(
            [
                f"the file holds {report.institutions_in_file} institutions and the "
                f"pilot is fixed at {expected_institutions}. Nothing was imported; "
                "this is a question for the client, not a row to add or drop."
            ]
        )

    submission_id = uuid.uuid4()
    report.submission_id = submission_id
    candidates = _build_candidates(loaded, header, resolved, submission_id, report, problems)
    if problems:
        raise OfficialSourceListError(problems)

    connection.execute(
        insert(PilotSubmission).values(
            id=submission_id,
            file_sha256=loaded.file_sha256,
            original_filename=loaded.file_name,
            file_byte_size=loaded.file_byte_size,
            template_version=TEMPLATE_VERSION,
            submission_kind=PilotSubmissionKind.OFFICIAL_SOURCE_LIST.value,
            defines_pilot_scope=True,
            submitted_at=submitted_at,
            imported_by=imported_by,
            selected_university_count=len(resolved),
            expected_university_count=expected_institutions,
            import_status=PilotImportStatus.VALIDATED.value,
            validation_summary=_validation_summary(report),
            notes=(
                "Final pilot source list. Defines the pilot at "
                f"{len(resolved)} institutions and their acquisition targets. "
                "No collected facts; no page fetched."
            ),
        )
    )

    connection.execute(
        insert(PilotSelectedUniversity),
        [
            {
                "submission_id": submission_id,
                "target_institution_id": row.target_institution_id,
                "sheet_row_no": row.sheet_row_no,
                "is_selected": True,
                "selection_value": "OFFICIAL_SOURCE_LIST",
                # Deliberately NOT the workbook's name: it is the client's list
                # label, not a collected official name. See the migration header.
                "official_name_en": None,
                "official_homepage": _homepage(loaded, header, row),
                "collector_notes": _cell_text(loaded, row.sheet_row_no, header, NOTES_COLUMN)
                or None,
            }
            for row in resolved
        ],
    )

    # Ordered so a duplicate never precedes the row it points at: the composite FK
    # resolves within the same statement only if the target already exists.
    physical = [c for c in candidates if c["duplicate_of_source_ref"] is None]
    repeats = [c for c in candidates if c["duplicate_of_source_ref"] is not None]
    connection.execute(insert(PilotCollectedSource), physical)
    if repeats:
        connection.execute(insert(PilotCollectedSource), repeats)

    if assign_pilot_wave:
        _assign_pilot_wave(connection, resolved, report, imported_by)
    if supersede:
        _supersede_earlier(connection, submission_id, report)

    logger.info(
        "official_source_list_imported",
        submission_id=str(submission_id),
        file_sha256=loaded.file_sha256,
        institutions=len(resolved),
        responsibilities=report.responsibilities,
        physical_sources=report.physical_sources,
    )
    return report


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _header(loaded: LoadedWorkbook, report: OfficialSourceReport) -> dict[str, int]:
    if SHEET_NAME not in loaded.sheet_names:
        raise OfficialSourceListError(
            [f"the workbook has no {SHEET_NAME!r} sheet; found {list(loaded.sheet_names)}"]
        )
    header: dict[str, int] = {}
    for column in range(1, loaded.max_column(SHEET_NAME) + 1):
        name = _text(loaded.value(SHEET_NAME, 1, column))
        if name:
            header[name] = column
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise OfficialSourceListError(
            [f"{SHEET_NAME} is missing required column(s): {', '.join(missing)}"]
        )
    if ADDITIONAL_COLUMN not in header:
        report.readme_notes.append(
            f"{ADDITIONAL_COLUMN!r} is absent. It is optional, so the import continues."
        )
    return header


def _data_rows(loaded: LoadedWorkbook, header: dict[str, int]) -> list[int]:
    columns = set(header.values())
    rows = {
        row
        for (row, column), value in loaded.cells.get(SHEET_NAME, {}).items()
        if row > 1 and column in columns and _text(value)
    }
    return sorted(rows)


def _readme_notes(loaded: LoadedWorkbook, institution_count: int) -> list[str]:
    """Check the README's own arithmetic, and never fail the import over it.

    The supplied file claims "455 populated / 455 expected" for its URL fields. There
    are eleven URL columns, so the real figure is 385 — the README counted A:M, which
    includes the name, the region and the notes. It is documentation metadata about a
    spreadsheet, not a database invariant, so it is reported and moved past.
    """
    notes: list[str] = []
    core = len(COLUMN_CATEGORIES) * institution_count
    optional = institution_count
    if "README" not in loaded.sheet_names:
        return notes
    for row in range(1, loaded.max_row("README") + 1):
        claim = " ".join(
            _text(loaded.value("README", row, column))
            for column in range(1, loaded.max_column("README") + 1)
        )
        if "expected" in claim.lower() and str(core + optional) not in claim:
            notes.append(
                f"README says {claim.strip()!r}. The correct count is "
                f"{core + optional} URL cells ({core} core + {optional} optional "
                f"additional), not what it states. Documentation only; the import "
                "does not depend on it."
            )
    return notes


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _resolve_institutions(
    connection: Connection,
    loaded: LoadedWorkbook,
    header: dict[str, int],
    rows: list[int],
    report: OfficialSourceReport,
    problems: list[str],
) -> list[ResolvedInstitution]:
    """Exact resolution only. One unresolved row stops the whole import.

    The alternative — importing the 34 that matched — leaves a pilot that looks
    complete and is quietly missing a university, which is the failure mode this
    project exists to avoid.
    """
    known = {
        row.match_key: row
        for row in connection.execute(
            text(
                """
                SELECT ti.id, ti.match_key, ti.destination_code,
                       (SELECT e.qs_name
                          FROM target_list_entry e
                          JOIN target_list l ON l.id = e.target_list_id
                         WHERE e.target_institution_id = ti.id
                         ORDER BY l.imported_at DESC, e.recorded_at DESC
                         LIMIT 1) AS qs_name
                  FROM target_institution ti
                """
            )
        )
    }

    resolved: list[ResolvedInstitution] = []
    seen_keys: dict[str, int] = {}

    for row_no in rows:
        name = _cell_text(loaded, row_no, header, NAME_COLUMN)
        region = _cell_text(loaded, row_no, header, REGION_COLUMN)
        if not name:
            problems.append(f"row {row_no}: {NAME_COLUMN} is blank")
            report.institutions_unknown += 1
            continue

        key = normalize_institution_name(name)
        if key in seen_keys:
            problems.append(
                f"row {row_no}: {name!r} already appears on row {seen_keys[key]}. "
                "A university may be listed once; two rows would give one institution "
                "two sets of sources with no way to say which is current."
            )
            report.institutions_ambiguous += 1
            continue
        seen_keys[key] = row_no

        target = known.get(key)
        if target is None:
            report.institutions_unknown += 1
            problems.append(
                f"row {row_no}: {name!r} does not match any imported target institution "
                f"(match key {key!r}). Names must be the exact target-list labels; "
                "nothing here guesses at a near match."
            )
            continue

        destination, problem = resolve_destination(None, region)
        if problem is not None:
            problems.append(f"row {row_no}: {name!r} -- {problem}")
            continue
        if target.destination_code and destination != target.destination_code:
            problems.append(
                f"row {row_no}: {name!r} is {region!r} ({destination}) in this file but "
                f"{target.destination_code} in the target list. One of them is wrong, "
                "and the importer must not pick."
            )
            continue

        report.institutions_resolved += 1
        resolved.append(
            ResolvedInstitution(
                sheet_row_no=row_no,
                supplied_name=name,
                supplied_region=region,
                target_institution_id=target.id,
                match_key=key,
                destination_code=target.destination_code,
                qs_name=target.qs_name or name,
            )
        )
    return resolved


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _build_candidates(
    loaded: LoadedWorkbook,
    header: dict[str, int],
    resolved: list[ResolvedInstitution],
    submission_id: uuid.UUID,
    report: OfficialSourceReport,
    problems: list[str],
) -> list[dict[str, Any]]:
    """One row per claimed responsibility, with repeats linked to the physical page.

    Refs are assigned in workbook order — row by row, column by column — so the same
    file always produces the same `S0001`..`Snnnn`. They are submission-local and
    never global source ids.
    """
    candidates: list[dict[str, Any]] = []
    ref_number = 0

    columns: list[tuple[str, str | None]] = [
        (heading, category) for heading, category in COLUMN_CATEGORIES
    ]
    if ADDITIONAL_COLUMN in header:
        columns.append((ADDITIONAL_COLUMN, None))

    for institution in resolved:
        # Physical identity is scoped to the institution: two universities that
        # happened to share a page would each need their own acquisition target,
        # because scope, not the URL, is what a mapping belongs to.
        first_ref_for_url: dict[str, str] = {}
        label = institution.qs_name

        for heading, category in columns:
            raw = _cell_text(loaded, institution.sheet_row_no, header, heading)
            is_additional = category is None
            if not raw:
                if is_additional:
                    continue
                report.blank_core_cells += 1
                problems.append(
                    f"row {institution.sheet_row_no}: {institution.supplied_name!r} "
                    f"has no {heading}. Core URL columns are required."
                )
                continue

            if is_additional:
                report.additional_url_cells += 1
            else:
                report.core_url_cells += 1

            try:
                url = validate_source_url(raw)
            except UrlRejectedError as exc:
                # Reported and skipped rather than fatal: one unusable address among
                # 385 should not cost the other 384, and the rejection is visible.
                report.rejected_urls.append(f"row {institution.sheet_row_no} {heading}: {exc}")
                continue

            ref_number += 1
            source_ref = f"S{ref_number:04d}"
            duplicate_of = first_ref_for_url.get(url.sha256)
            if duplicate_of is None:
                first_ref_for_url[url.sha256] = source_ref
                report.physical_sources += 1
            else:
                report.duplicate_responsibilities += 1
                report.duplicates_by_institution[label] = (
                    report.duplicates_by_institution.get(label, 0) + 1
                )

            source_type = category or UNCLASSIFIED_SOURCE_TYPE
            if category is None:
                report.unclassified_sources += 1
                if duplicate_of is None:
                    report.unclassified_distinct_sources += 1
            report.by_category[source_type] = report.by_category.get(source_type, 0) + 1
            report.by_institution[label] = report.by_institution.get(label, 0) + 1
            report.responsibilities += 1

            candidates.append(
                {
                    "submission_id": submission_id,
                    "source_ref": source_ref,
                    "target_institution_id": institution.target_institution_id,
                    "sheet_row_no": institution.sheet_row_no,
                    "source_type": source_type,
                    "degree_scope": COLUMN_DEGREE_SCOPES.get(source_type),
                    "workbook_column": heading,
                    "duplicate_of_source_ref": duplicate_of,
                    "official_url": url.original,
                    "normalized_url": url.normalized,
                    "url_sha256": url.sha256,
                    "host": url.host,
                    "checked_at": None,
                    "is_third_party": None,
                    "collector_notes": None,
                    # Hardcoded. No cell in any workbook sets a verification state.
                    "verification_state": SourceCandidateState.PENDING.value,
                }
            )
    return candidates


# ---------------------------------------------------------------------------
# Scope and lineage
# ---------------------------------------------------------------------------


def _assign_pilot_wave(
    connection: Connection,
    resolved: list[ResolvedInstitution],
    report: OfficialSourceReport,
    actor_id: uuid.UUID | None,
) -> None:
    """Set `pilot_wave = 1` for exactly the institutions this file names.

    This is the moment U8 described: the pilot is never inferred, and it is the
    client's own file that names it. Every other institution keeps `pilot_wave` NULL
    and stays a valid future target -- nothing is deleted or demoted.

    A previously waved institution absent from this file is cleared, because this is
    the list that *defines* scope. That clearing is counted and reported, so a row
    silently dropped from the client's file is visible rather than assumed.
    """
    from app.domains.onboarding.models import TargetInstitution

    chosen = [row.target_institution_id for row in resolved]

    cleared = connection.execute(
        update(TargetInstitution)
        .where(
            TargetInstitution.pilot_wave.is_not(None),
            TargetInstitution.id.not_in(chosen),
        )
        .values(pilot_wave=None)
        .returning(TargetInstitution.id)
    ).scalars()
    report.pilot_wave_cleared = len(list(cleared))

    assigned = connection.execute(
        update(TargetInstitution)
        .where(TargetInstitution.id.in_(chosen))
        .values(pilot_wave=1)
        .returning(TargetInstitution.id)
    ).scalars()
    report.pilot_wave_assigned = len(list(assigned))

    if actor_id is not None:
        connection.execute(
            insert(AuditLog).values(
                actor_type=ActorType.USER.value,
                actor_id=actor_id,
                action="PILOT_SCOPE_SET",
                object_type="target_institution",
                object_id=None,
                before_state={"waved": report.pilot_wave_cleared},
                after_state={"waved": report.pilot_wave_assigned},
                reason=(
                    "Pilot scope set from the client's final official-source list "
                    f"({report.pilot_wave_assigned} institutions)."
                ),
            )
        )


def _supersede_earlier(
    connection: Connection, submission_id: uuid.UUID, report: OfficialSourceReport
) -> None:
    """Mark earlier *source lists* superseded. Collection workbooks are untouched.

    A new source list replaces the previous source list. It says nothing about a
    facts workbook, and marking one superseded because an unrelated file arrived
    would be a lie about what happened.
    """
    superseded = connection.execute(
        update(PilotSubmission)
        .where(
            PilotSubmission.id != submission_id,
            PilotSubmission.submission_kind == PilotSubmissionKind.OFFICIAL_SOURCE_LIST.value,
            PilotSubmission.import_status == PilotImportStatus.VALIDATED.value,
        )
        .values(import_status=PilotImportStatus.SUPERSEDED.value)
        .returning(PilotSubmission.id)
    ).scalars()
    report.superseded_submission_ids = list(superseded)


def _validation_summary(report: OfficialSourceReport) -> dict[str, Any]:
    return {
        "institutions": {
            "in_file": report.institutions_in_file,
            "resolved": report.institutions_resolved,
            "ambiguous": report.institutions_ambiguous,
            "unknown": report.institutions_unknown,
        },
        "url_cells": {
            "core": report.core_url_cells,
            "additional_optional": report.additional_url_cells,
            "total": report.core_url_cells + report.additional_url_cells,
        },
        "sources": {
            "claimed_responsibilities": report.responsibilities,
            "distinct_pages": report.physical_sources,
            "repeats_of_a_listed_page": report.duplicate_responsibilities,
            "unclassified": report.unclassified_sources,
            "unclassified_needing_review": report.unclassified_distinct_sources,
            "rejected_urls": report.rejected_urls,
        },
        "by_category": report.by_category,
        "readme_notes": report.readme_notes,
    }


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _cell_text(loaded: LoadedWorkbook, row: int, header: dict[str, int], name: str) -> str:
    column = header.get(name)
    return "" if column is None else _text(loaded.value(SHEET_NAME, row, column))


def _homepage(
    loaded: LoadedWorkbook, header: dict[str, int], institution: ResolvedInstitution
) -> str | None:
    raw = _cell_text(loaded, institution.sheet_row_no, header, COLUMN_CATEGORIES[0][0])
    if not raw:
        return None
    try:
        return validate_source_url(raw).original
    except UrlRejectedError:
        return None


def _text(value: object) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


__all__ = [
    "ADDITIONAL_COLUMN",
    "COLUMN_CATEGORIES",
    "PILOT_INSTITUTION_COUNT",
    "OfficialSourceListError",
    "OfficialSourceReport",
    "ResolvedInstitution",
    "import_official_source_list",
]
