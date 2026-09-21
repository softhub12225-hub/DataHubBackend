"""Import a client target-list workbook.

Usage::

    uv run python apps/api/scripts/import_target_list.py path/to/list.xlsx
    uv run python apps/api/scripts/import_target_list.py list.xlsx --dry-run
    uv run python apps/api/scripts/import_target_list.py list.xlsx --list-version 1.2

``--dry-run`` performs the whole import inside a transaction and rolls it back, so an
operator can see the difference report a new list would produce *before* committing
it. That is the intended way to answer "what changed in QS 2028?".

The script connects as the **migration/owner** role by default only because it is an
operational tool run by a person; when the same import is triggered from the console
it runs as ``app_api``, which holds exactly the grants it needs (INSERT on the
append-only entry tables, INSERT/UPDATE on the mutable ones) and no write access to
anything canonical.

Nothing here fetches anything. Reading a workbook and recording scope are local
operations; the acquisition crawler is a separate, later phase and is not started by
this script.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.onboarding.importer import (
    ImportReport,
    TargetListConflictError,
    import_target_list,
)
from app.domains.onboarding.target_list import TargetListValidationError
from app.domains.onboarding.workbook import WorkbookRejectedError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workbook", type=Path, help="path to the .xlsx target list")
    parser.add_argument(
        "--sheet",
        default=None,
        help="worksheet to import; required only when the file has several populated sheets",
    )
    parser.add_argument(
        "--list-version",
        default=None,
        help="override the version the file declares (use for a corrected file that "
        "did not bump its own version string)",
    )
    parser.add_argument(
        "--list-name",
        default=None,
        help="override the list name the file declares",
    )
    parser.add_argument(
        "--imported-by",
        default=None,
        help="app_user UUID to record as the importing actor",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="roll back instead of committing; prints the report that would result",
    )
    parser.add_argument(
        "--role",
        default=DatabaseRole.MIGRATION.value,
        choices=[role.value for role in DatabaseRole],
        help="database identity to connect as (default: migration/owner)",
    )
    return parser


def render(report: ImportReport, *, dry_run: bool) -> None:
    print(report.summary())
    if report.already_imported:
        return

    print(f"  target_list id : {report.target_list_id}")
    if report.previous_target_list_id:
        print(f"  compared with  : {report.previous_target_list_id}")
    else:
        print("  compared with  : (first import of this list)")

    if report.diffs:
        print("  differences:")
        for kind, count in sorted(report.diffs.items()):
            print(f"    {kind:24} {count}")

    if report.needs_manual_review:
        print(f"  needs manual review ({len(report.needs_manual_review)}):")
        for name in report.needs_manual_review[:20]:
            print(f"    - {name}")
        if len(report.needs_manual_review) > 20:
            print(f"    ... and {len(report.needs_manual_review) - 20} more")

    if report.warnings:
        print(f"  warnings ({len(report.warnings)}):")
        for warning in report.warnings[:20]:
            print(f"    - {warning}")
        if len(report.warnings) > 20:
            print(f"    ... and {len(report.warnings) - 20} more")

    print()
    if dry_run:
        print("DRY RUN -- rolled back. Nothing was written.")
    else:
        print("Committed.")
        print(
            "No collection has been started. Sources must be mapped and verified "
            "before anything is fetched."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    imported_by: uuid.UUID | None = None
    if args.imported_by:
        try:
            imported_by = uuid.UUID(args.imported_by)
        except ValueError:
            print(f"error: --imported-by is not a UUID: {args.imported_by!r}", file=sys.stderr)
            return 2

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole(args.role)), future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                report = import_target_list(
                    connection,
                    args.workbook,
                    sheet_name=args.sheet,
                    list_version=args.list_version,
                    list_name=args.list_name,
                    imported_by=imported_by,
                )
            except (WorkbookRejectedError, TargetListValidationError) as exc:
                transaction.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except TargetListConflictError as exc:
                transaction.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 3

            if args.dry_run:
                transaction.rollback()
            else:
                transaction.commit()
            render(report, dry_run=args.dry_run)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
