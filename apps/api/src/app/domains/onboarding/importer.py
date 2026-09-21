"""Importing a client target list: idempotent, non-destructive, and diff-producing.

THE THREE GUARANTEES
====================

**Idempotent.** Re-running the same file is recognised by its SHA-256 and does
nothing. The function returns a report describing the existing import rather than
raising, because "run the importer again" is a normal operational reflex and it must
be safe.

**Non-destructive.** A later list that omits an institution never deletes anything.
The institution's `target_institution` row survives with `is_in_current_list = false`,
its historical `target_list_entry` rows survive untouched, and its verified domains,
mapped sources, matched canonical university and every piece of evidence survive
completely. Scope shrinking is a statement about scope, not a licence to destroy
governed data (Step 4 requirement 13). If that institution reappears in a subsequent
list, the accumulated onboarding work is still there.

**Diff-producing.** Every difference against the previous list of the same name is
recorded as a `target_list_diff` row, so the questions the client asked -- what was
added, what disappeared, whose rank moved, who was renamed, whose region was
corrected -- are answered from stored data rather than by re-reading spreadsheets.

WHAT THE IMPORTER DELIBERATELY DOES NOT DO
==========================================
* It creates **no** `university`, `field_claim`, `change_proposal`, `ranking_entry`
  or any other canonical or governance row. The import's entire output lives in the
  four onboarding tables. Database privileges enforce this independently: the role
  running an import has no write grant on the canonical plane.
* It performs **no** network access. Not to verify a URL, not to resolve a domain,
  not to check a university's website. The workbook's own `source_url` is stored as
  text and never fetched (Step 4 requirement 15).
* It assigns **no** pilot wave. The list contains 57 institutions across the pilot
  destinations and the PRD speaks of at least 36; which ones is the client's decision,
  so `pilot_wave` stays NULL for everyone.
* It does **not** promote a target to a verified identity. `matched_university_id`
  is only ever set by a human through identity resolution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, and_, func, insert, select, update

from app.core.logging import get_logger
from app.db.enums import ActorType, OnboardingStatus, TargetChangeKind
from app.domains.onboarding.models import (
    TargetInstitution,
    TargetList,
    TargetListDiff,
    TargetListEntry,
)
from app.domains.onboarding.target_list import (
    ParsedRow,
    ParsedTargetList,
    TargetListValidationError,
    parse_target_list,
)
from app.domains.onboarding.workbook import load_workbook
from app.domains.versioning.models import AuditLog

logger = get_logger(__name__)

#: `audit_log.action` for a completed import.
AUDIT_ACTION_IMPORT = "TARGET_LIST_IMPORTED"


class TargetListConflictError(RuntimeError):
    """The same (name, version) already exists with different content.

    Refused rather than resolved. Overwriting would destroy the version history the
    difference report is computed from, and silently creating a second "v1.1" would
    make "which v1.1?" unanswerable. The operator supplies an explicit new version
    string -- which is also an accurate description of what a corrected file is.
    """


@dataclass(slots=True)
class ImportReport:
    """What an import did. Safe to render to an operator verbatim."""

    target_list_id: uuid.UUID
    list_name: str
    list_version: str
    file_sha256: str
    #: True when the file had already been imported and nothing was written.
    already_imported: bool
    rows_read: int = 0
    institutions_created: int = 0
    institutions_rematched: int = 0
    entries_written: int = 0
    previous_target_list_id: uuid.UUID | None = None
    diffs: dict[str, int] = field(default_factory=dict)
    needs_manual_review: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.already_imported:
            return (
                f"{self.list_name} {self.list_version} was already imported "
                f"(sha256 {self.file_sha256[:12]}...); nothing was written."
            )
        parts = [
            f"{self.list_name} {self.list_version}: {self.rows_read} rows read, "
            f"{self.entries_written} entries written, "
            f"{self.institutions_created} new institutions"
        ]
        if self.diffs:
            changes = ", ".join(f"{kind}={count}" for kind, count in sorted(self.diffs.items()))
            parts.append(f"differences: {changes}")
        if self.needs_manual_review:
            parts.append(f"{len(self.needs_manual_review)} need manual review")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warnings")
        return "; ".join(parts)


def import_target_list(
    connection: Connection,
    path: str | Path,
    *,
    sheet_name: str | None = None,
    list_version: str | None = None,
    list_name: str | None = None,
    imported_by: uuid.UUID | None = None,
) -> ImportReport:
    """Import a target-list workbook inside the caller's transaction.

    The caller owns the transaction boundary so that an import can be staged,
    inspected and rolled back. Nothing here commits.
    """
    workbook = load_workbook(path)

    existing = connection.execute(
        select(TargetList.id, TargetList.list_name, TargetList.list_version).where(
            TargetList.file_sha256 == workbook.file_sha256
        )
    ).one_or_none()
    if existing is not None:
        logger.info(
            "target_list_import_skipped_identical_file",
            target_list_id=str(existing.id),
            file_sha256=workbook.file_sha256,
        )
        return ImportReport(
            target_list_id=existing.id,
            list_name=existing.list_name,
            list_version=existing.list_version,
            file_sha256=workbook.file_sha256,
            already_imported=True,
        )

    parsed = parse_target_list(
        workbook, sheet_name=sheet_name, list_version=list_version, list_name=list_name
    )
    assert parsed.list_version is not None  # parse_target_list guarantees it

    clash = connection.execute(
        select(TargetList.id, TargetList.file_sha256).where(
            and_(
                TargetList.list_name == parsed.list_name,
                TargetList.list_version == parsed.list_version,
            )
        )
    ).one_or_none()
    if clash is not None:
        raise TargetListConflictError(
            f"{parsed.list_name!r} version {parsed.list_version!r} was already imported "
            f"from a different file (stored sha256 {clash.file_sha256[:12]}..., "
            f"supplied {workbook.file_sha256[:12]}...). Supply an explicit new "
            "list_version for the corrected file; the existing version is kept."
        )

    previous = _previous_list(connection, parsed.list_name)

    target_list_id = connection.execute(
        insert(TargetList)
        .values(
            list_name=parsed.list_name,
            list_version=parsed.list_version,
            source_description=parsed.source_description,
            source_url=parsed.source_url,
            published_at=parsed.published_at,
            imported_by=imported_by,
            file_name=workbook.file_name,
            file_sha256=workbook.file_sha256,
            file_byte_size=workbook.file_byte_size,
            sheet_name=parsed.sheet_name,
            declared_row_count=parsed.declared_row_count,
            imported_row_count=len(parsed.rows),
            declared_region_counts=parsed.declared_region_counts or None,
            notes=_build_notes(parsed),
        )
        .returning(TargetList.id)
    ).scalar_one()

    report = ImportReport(
        target_list_id=target_list_id,
        list_name=parsed.list_name,
        list_version=parsed.list_version,
        file_sha256=workbook.file_sha256,
        already_imported=False,
        rows_read=len(parsed.rows),
        previous_target_list_id=previous,
        warnings=list(parsed.warnings),
    )

    previous_entries = _previous_entries(connection, previous)
    diffs: list[dict[str, Any]] = []
    seen_institutions: set[uuid.UUID] = set()

    for row in parsed.rows:
        institution_id, created = _upsert_institution(
            connection, row, target_list_id, previous is None
        )
        seen_institutions.add(institution_id)
        if created:
            report.institutions_created += 1

        if row.destination_code is None:
            report.needs_manual_review.append(row.qs_name)

        connection.execute(
            insert(TargetListEntry).values(
                target_list_id=target_list_id,
                target_institution_id=institution_id,
                source_row=row.source_row,
                sequence_no=row.sequence_no,
                qs_name=row.qs_name,
                qs_name_normalized=row.qs_name_normalized,
                qs_rank=row.qs_rank,
                qs_score=row.qs_score,
                region_label=row.region_label,
                country_territory=row.country_territory,
                destination_code=row.destination_code,
            )
        )
        report.entries_written += 1

        diffs.extend(
            _row_diffs(
                row=row,
                institution_id=institution_id,
                target_list_id=target_list_id,
                previous_list_id=previous,
                previous_entry=previous_entries.get(institution_id),
                is_new=created,
            )
        )

    diffs.extend(
        _removal_diffs(
            connection,
            target_list_id=target_list_id,
            previous_list_id=previous,
            previous_entries=previous_entries,
            seen=seen_institutions,
        )
    )

    if diffs:
        connection.execute(insert(TargetListDiff), diffs)
        for entry in diffs:
            kind = str(entry["change_kind"])
            report.diffs[kind] = report.diffs.get(kind, 0) + 1

    # Audit last. The onboarding rows are written and their locks held before the
    # audit chain's head row is touched, so the chain's serialisation point is taken
    # as late as possible and in a consistent order everywhere (C18 lock order).
    connection.execute(
        insert(AuditLog).values(
            actor_type=ActorType.USER.value if imported_by else ActorType.SYSTEM.value,
            actor_id=imported_by,
            action=AUDIT_ACTION_IMPORT,
            object_type="target_list",
            object_id=target_list_id,
            after_state={
                "list_name": parsed.list_name,
                "list_version": parsed.list_version,
                "file_name": workbook.file_name,
                "file_sha256": workbook.file_sha256,
                "rows_read": report.rows_read,
                "institutions_created": report.institutions_created,
                "diffs": report.diffs,
            },
            reason=f"Imported target list from {workbook.file_name}",
        )
    )

    logger.info(
        "target_list_imported",
        target_list_id=str(target_list_id),
        list_name=parsed.list_name,
        list_version=parsed.list_version,
        rows_read=report.rows_read,
        institutions_created=report.institutions_created,
        diffs=report.diffs,
    )
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_notes(parsed: ParsedTargetList) -> str | None:
    """Keep the file's scope and methodology statements, verbatim.

    The QS workbook states that ties share a rank and that its own site supersedes
    the file on correction. That is context a future reviewer needs and that nothing
    else in the schema would otherwise retain.
    """
    lines = [note for note in (parsed.scope_note, parsed.method_note) if note]
    return "\n".join(lines) if lines else None


def _previous_list(connection: Connection, list_name: str) -> uuid.UUID | None:
    """The most recently imported list of the same name, if any.

    Comparison is per list *name*: a QS 2028 import is diffed against the previous
    QS-derived list, not against an unrelated list a different client supplied.
    """
    return connection.execute(
        select(TargetList.id)
        .where(TargetList.list_name == list_name)
        .order_by(TargetList.imported_at.desc(), TargetList.list_version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _previous_entries(
    connection: Connection, previous_list_id: uuid.UUID | None
) -> dict[uuid.UUID, Any]:
    if previous_list_id is None:
        return {}
    rows = connection.execute(
        select(
            TargetListEntry.target_institution_id,
            TargetListEntry.qs_name,
            TargetListEntry.qs_rank,
            TargetListEntry.qs_score,
            TargetListEntry.region_label,
            TargetListEntry.destination_code,
        ).where(TargetListEntry.target_list_id == previous_list_id)
    ).all()
    return {row.target_institution_id: row for row in rows}


def _upsert_institution(
    connection: Connection,
    row: ParsedRow,
    target_list_id: uuid.UUID,
    is_first_import: bool,
) -> tuple[uuid.UUID, bool]:
    """Find the institution by match key, or create it. Returns (id, created).

    An existing institution keeps its onboarding progress. The only status change a
    re-import may make is to flag an institution whose region became unresolvable,
    and then only if it had not yet started -- an import must never knock a target
    that is already ACTIVE back into review.
    """
    existing = connection.execute(
        select(
            TargetInstitution.id,
            TargetInstitution.onboarding_status,
            TargetInstitution.destination_code,
        ).where(TargetInstitution.match_key == row.qs_name_normalized)
    ).one_or_none()

    if existing is None:
        needs_review = row.destination_code is None
        institution_id = connection.execute(
            insert(TargetInstitution)
            .values(
                match_key=row.qs_name_normalized,
                first_seen_list_id=target_list_id,
                latest_list_id=target_list_id,
                is_in_current_list=True,
                removed_from_list_id=None,
                destination_code=row.destination_code,
                onboarding_status=(
                    OnboardingStatus.NEEDS_MANUAL_REVIEW.value
                    if needs_review
                    else OnboardingStatus.NOT_STARTED.value
                ),
                blocked_reason=(
                    f"region {row.region_label!r} could not be mapped to a destination"
                    if needs_review
                    else None
                ),
            )
            .returning(TargetInstitution.id)
        ).scalar_one()
        return institution_id, True

    values: dict[str, Any] = {
        "latest_list_id": target_list_id,
        "is_in_current_list": True,
        # Re-appearing in a list clears the removal marker; the historical entries
        # still record the gap.
        "removed_from_list_id": None,
    }
    # A resolved destination is recorded; an unresolved one never overwrites a
    # destination a human already accepted.
    if row.destination_code is not None:
        values["destination_code"] = row.destination_code
    elif existing.onboarding_status == OnboardingStatus.NOT_STARTED:
        values["onboarding_status"] = OnboardingStatus.NEEDS_MANUAL_REVIEW.value
        values["blocked_reason"] = (
            f"region {row.region_label!r} could not be mapped to a destination"
        )

    connection.execute(
        update(TargetInstitution).where(TargetInstitution.id == existing.id).values(**values)
    )
    return existing.id, False


def _row_diffs(
    *,
    row: ParsedRow,
    institution_id: uuid.UUID,
    target_list_id: uuid.UUID,
    previous_list_id: uuid.UUID | None,
    previous_entry: Any | None,
    is_new: bool,
) -> list[dict[str, Any]]:
    """Differences for one row against its counterpart in the previous list."""
    base = {
        "target_list_id": target_list_id,
        "previous_target_list_id": previous_list_id,
        "target_institution_id": institution_id,
    }

    if previous_entry is None:
        # New to this list. On a first import every row is an addition, which is an
        # accurate description of what the import did.
        return [
            {
                **base,
                "change_kind": TargetChangeKind.ADDED_TARGET.value,
                "before_value": None,
                "after_value": _snapshot(row),
            }
        ]

    diffs: list[dict[str, Any]] = []

    if _rank_of(previous_entry) != row.qs_rank:
        diffs.append(
            {
                **base,
                "change_kind": TargetChangeKind.RANK_CHANGED.value,
                "before_value": {"qs_rank": _rank_of(previous_entry)},
                "after_value": {"qs_rank": row.qs_rank},
            }
        )

    before_score = _score_of(previous_entry)
    if before_score != row.qs_score:
        diffs.append(
            {
                **base,
                "change_kind": TargetChangeKind.SCORE_CHANGED.value,
                "before_value": {"qs_score": _as_json_number(before_score)},
                "after_value": {"qs_score": _as_json_number(row.qs_score)},
            }
        )

    # Compared on the raw name: the match key is by construction identical, so any
    # difference here is a presentation change the client made and may want to see.
    if previous_entry.qs_name != row.qs_name:
        diffs.append(
            {
                **base,
                "change_kind": TargetChangeKind.NAME_CHANGED.value,
                "before_value": {"qs_name": previous_entry.qs_name},
                "after_value": {"qs_name": row.qs_name},
            }
        )

    if (
        previous_entry.region_label != row.region_label
        or previous_entry.destination_code != row.destination_code
    ):
        diffs.append(
            {
                **base,
                "change_kind": TargetChangeKind.REGION_CHANGED.value,
                "before_value": {
                    "region_label": previous_entry.region_label,
                    "destination_code": previous_entry.destination_code,
                },
                "after_value": {
                    "region_label": row.region_label,
                    "destination_code": row.destination_code,
                },
            }
        )

    return diffs


def _removal_diffs(
    connection: Connection,
    *,
    target_list_id: uuid.UUID,
    previous_list_id: uuid.UUID | None,
    previous_entries: dict[uuid.UUID, Any],
    seen: set[uuid.UUID],
) -> list[dict[str, Any]]:
    """Report institutions the previous list named and this one does not.

    The only mutation is a flag. Nothing is deleted, here or anywhere downstream:
    the institution keeps its entries, its verified domains, its mapped sources, its
    matched university and all of that university's evidence and history.
    """
    if previous_list_id is None:
        return []

    removed = [institution_id for institution_id in previous_entries if institution_id not in seen]
    if not removed:
        return []

    connection.execute(
        update(TargetInstitution)
        .where(TargetInstitution.id.in_(removed))
        .values(
            is_in_current_list=False,
            removed_from_list_id=target_list_id,
            latest_list_id=target_list_id,
        )
    )

    return [
        {
            "target_list_id": target_list_id,
            "previous_target_list_id": previous_list_id,
            "target_institution_id": institution_id,
            "change_kind": TargetChangeKind.REMOVED_FROM_NEW_LIST.value,
            "before_value": {
                "qs_name": previous_entries[institution_id].qs_name,
                "qs_rank": _rank_of(previous_entries[institution_id]),
                "region_label": previous_entries[institution_id].region_label,
            },
            "after_value": None,
        }
        for institution_id in removed
    ]


def _snapshot(row: ParsedRow) -> dict[str, Any]:
    return {
        "qs_name": row.qs_name,
        "qs_rank": row.qs_rank,
        "qs_score": _as_json_number(row.qs_score),
        "region_label": row.region_label,
        "country_territory": row.country_territory,
        "destination_code": row.destination_code,
        "source_row": row.source_row,
    }


def _rank_of(entry: Any) -> int | None:
    value = entry.qs_rank
    return int(value) if value is not None else None


def _score_of(entry: Any) -> Decimal | None:
    value = entry.qs_score
    return Decimal(value) if value is not None else None


def _as_json_number(value: Decimal | None) -> float | None:
    """Render a score for JSONB.

    A `Decimal` is not JSON-serialisable and `str` would make a stored diff
    awkward to query, so scores become floats in the report. The authoritative value
    stays `Numeric` on `target_list_entry`; this is a difference report, not the
    record of the score.
    """
    return float(value) if value is not None else None


def current_target_count(connection: Connection) -> int:
    """How many institutions are in the current scope."""
    return connection.execute(
        select(func.count())
        .select_from(TargetInstitution)
        .where(TargetInstitution.is_in_current_list.is_(True))
    ).scalar_one()


__all__ = [
    "AUDIT_ACTION_IMPORT",
    "ImportReport",
    "TargetListConflictError",
    "TargetListValidationError",
    "current_target_count",
    "import_target_list",
]
