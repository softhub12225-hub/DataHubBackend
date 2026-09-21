"""Import a completed pilot collection workbook into the staging plane (U12).

Usage::

    uv run python apps/api/scripts/import_pilot_workbook.py returned.xlsx
    uv run python apps/api/scripts/import_pilot_workbook.py returned.xlsx --expect-selected 35
    uv run python apps/api/scripts/import_pilot_workbook.py returned.xlsx --dry-run

**This writes staging rows and nothing else.** No `university`, no `program`, no
`source`, no `field_claim`, no published fact. The workbook is a person's account of
what they read on some pages; it becomes evidence only once those pages are
registered as sources, fetched and snapshotted, and none of that happens here or is
triggered by this command. Nothing is fetched at any point.

Every collected URL lands as a `PENDING` candidate for the verification queue. Run
`source_verification_report.py` afterwards to see the worklist.

Re-running with the same file is a no-op -- identical bytes are recognised by hash.
A *corrected* workbook creates a new submission and marks the previous one
superseded; the old rows stay exactly where they are, so the two can be compared.

Exit codes: 0 on success (including a no-op re-import), 1 when the workbook has
validation errors and nothing was written, 2 on a usage or file problem.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.onboarding.workbook import WorkbookRejectedError
from app.domains.pilot.import_workbook import WorkbookNotImportableError, import_pilot_workbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workbook", type=Path, help="the returned .xlsx")
    parser.add_argument(
        "--expect-selected",
        type=int,
        default=None,
        metavar="N",
        help="how many institutions the client was asked to select. Checked, never "
        "used to choose them.",
    )
    parser.add_argument(
        "--imported-by",
        type=uuid.UUID,
        default=None,
        metavar="USER_ID",
        help="the app_user recording this import",
    )
    parser.add_argument(
        "--keep-previous",
        action="store_true",
        help="do not mark earlier submissions superseded (for importing two genuinely "
        "different workbooks side by side, rather than one correcting the other)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do the whole import, print what it would have written, then roll back",
    )
    parser.add_argument(
        "--role",
        default=DatabaseRole.API.value,
        choices=[role.value for role in DatabaseRole],
        help="database identity to write as (default: api)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole(args.role)), future=True)
    try:
        with engine.begin() as connection:
            try:
                report = import_pilot_workbook(
                    connection,
                    args.workbook,
                    expected_selected=args.expect_selected,
                    imported_by=args.imported_by,
                    supersede=not args.keep_previous,
                )
            except WorkbookNotImportableError as exc:
                _print_errors(exc)
                return 1
            except WorkbookRejectedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

            if args.dry_run:
                _print_report(report, dry_run=True)
                # Raising is how a SQLAlchemy transaction block rolls back; the
                # alternative is to commit and then try to undo it, which is not an
                # alternative at all.
                raise _RollbackError
        _print_report(report, dry_run=False)
    except _RollbackError:
        print("\n--dry-run: everything above was rolled back. Nothing was written.")
    finally:
        engine.dispose()
    return 0


class _RollbackError(Exception):
    """Internal: unwinds the transaction after a dry run."""


def _print_errors(exc: WorkbookNotImportableError) -> None:
    report = exc.report
    print(f"{len(report.errors)} error(s) in {report.path}. Nothing was imported.")
    grouped: dict[str, list[str]] = defaultdict(list)
    for issue in report.errors:
        where = f"row {issue.row}" if issue.row else "sheet"
        grouped[issue.code.value].append(f"      {issue.sheet} {where}: {issue.message}")
    for heading in sorted(grouped):
        lines = grouped[heading]
        print(f"  {heading} ({len(lines)})")
        for line in lines[:20]:
            print(line)
        if len(lines) > 20:
            print(f"      ... and {len(lines) - 20} more")
    print("\nSend these back to the client; the file is not importable as it stands.")


def _print_report(report: object, *, dry_run: bool) -> None:
    from app.domains.pilot.import_workbook import StagingImportReport

    assert isinstance(report, StagingImportReport)
    print(report.summary())
    if report.already_imported:
        return

    print(f"  submission           {report.submission_id}")
    print(f"  sha256               {report.file_sha256}")
    print(f"  institutions on file {report.institution_rows}")
    print(f"  selected             {report.selected_universities}")
    print(f"  programmes           {report.programs}")
    print(f"  source candidates    {report.sources} (all PENDING)")
    for sheet, count in sorted(report.facts_by_sheet.items()):
        print(f"  {sheet:20} {count} fact row(s)")

    if report.rejected_urls:
        print(f"\n  {len(report.rejected_urls)} URL(s) refused and NOT stored:")
        for line in report.rejected_urls[:20]:
            print(f"      {line}")
        if len(report.rejected_urls) > 20:
            print(f"      ... and {len(report.rejected_urls) - 20} more")

    if report.superseded_submission_ids:
        print(
            f"\n  {len(report.superseded_submission_ids)} earlier submission(s) marked "
            "SUPERSEDED. Their rows are untouched and still queryable."
        )

    if not dry_run:
        print(
            "\nStaging only: no university, programme, source or published fact was "
            "created. Next step is the source verification queue:\n"
            "  make source-verification-report"
        )


if __name__ == "__main__":
    raise SystemExit(main())
