"""Import the client's final official-source list into staging (Step 5A).

Usage::

    uv run python apps/api/scripts/import_official_sources.py sources.xlsx --dry-run
    uv run python apps/api/scripts/import_official_sources.py sources.xlsx

**This writes staging rows and nothing else.** No `university`, no `program`, no
`source`, no `source_mapping`, no `field_claim`, no published fact. It establishes
acquisition targets and stops. **Nothing is fetched**, here or as a consequence of
running it; every URL is validated for syntax and scheme and then stored.

Every URL lands `PENDING` in the verification queue. A page believed official by the
collector is not yet official to the system, even when its host already matches a
verified domain.

Matching is exact. If any institution fails to resolve, or the file does not hold
exactly the pilot's institution count, nothing is imported and the problems are
listed — a partial import leaves a pilot that looks complete and is quietly missing a
university.

Exit codes: 0 on success (including a no-op re-import), 1 when the file cannot be
imported and nothing was written, 2 on a usage or file problem.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.onboarding.workbook import WorkbookRejectedError
from app.domains.pilot.official_sources import (
    PILOT_INSTITUTION_COUNT,
    OfficialSourceListError,
    OfficialSourceReport,
    import_official_source_list,
)


class _RollbackError(Exception):
    """Internal: unwinds the transaction after a dry run."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workbook", type=Path, help="the official-source .xlsx")
    parser.add_argument(
        "--expect-institutions",
        type=int,
        default=PILOT_INSTITUTION_COUNT,
        metavar="N",
        help=f"how many institutions the pilot holds (default: {PILOT_INSTITUTION_COUNT}). "
        "Checked, never used to add or drop a row.",
    )
    parser.add_argument(
        "--imported-by",
        type=uuid.UUID,
        default=None,
        metavar="USER_ID",
        help="the app_user recording this import; required to audit the scope change",
    )
    parser.add_argument(
        "--keep-pilot-wave",
        action="store_true",
        help="do not set pilot_wave from this file (default: this file defines scope, "
        "so exactly the institutions it names become wave 1)",
    )
    parser.add_argument(
        "--keep-previous",
        action="store_true",
        help="do not mark earlier source lists superseded",
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
                report = import_official_source_list(
                    connection,
                    args.workbook,
                    expected_institutions=args.expect_institutions,
                    imported_by=args.imported_by,
                    assign_pilot_wave=not args.keep_pilot_wave,
                    supersede=not args.keep_previous,
                )
            except OfficialSourceListError as exc:
                print(f"{len(exc.problems)} problem(s). Nothing was imported.\n")
                for problem in exc.problems[:40]:
                    print(f"  - {problem}")
                if len(exc.problems) > 40:
                    print(f"  ... and {len(exc.problems) - 40} more")
                print("\nResolve these with the client; the file is not importable as it stands.")
                return 1
            except WorkbookRejectedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

            _print_report(report, dry_run=args.dry_run)
            if args.dry_run:
                # Raising is how a SQLAlchemy transaction block rolls back.
                raise _RollbackError
    except _RollbackError:
        print("\n--dry-run: everything above was rolled back. Nothing was written.")
    finally:
        engine.dispose()
    return 0


def _print_report(report: OfficialSourceReport, *, dry_run: bool) -> None:
    print(report.summary())
    if report.already_imported:
        return

    print()
    print(f"  submission              {report.submission_id}")
    print(f"  sha256                  {report.file_sha256}")
    print(f"  institutions in file    {report.institutions_in_file}")
    print(
        f"  resolved                {report.institutions_resolved}"
        f"   ambiguous {report.institutions_ambiguous}"
        f"   unknown {report.institutions_unknown}"
    )
    print(f"  core URL cells          {report.core_url_cells} (required)")
    print(f"  additional URL cells    {report.additional_url_cells} (optional)")
    print(f"  claimed responsibilities{report.responsibilities:6}")
    print(f"  distinct pages          {report.physical_sources:6}  <- acquisition targets")
    print(f"  repeats of a page       {report.duplicate_responsibilities:6}  <- kept, not dropped")
    print(
        f"  unclassified            {report.unclassified_sources:6}"
        f"  ({report.unclassified_distinct_sources} distinct, awaiting classification)"
    )
    print(f"  pilot_wave assigned     {report.pilot_wave_assigned}")
    if report.pilot_wave_cleared:
        print(
            f"  pilot_wave cleared      {report.pilot_wave_cleared}  "
            "<- previously in the pilot and absent from this file"
        )

    if report.by_category:
        print("\n  by source category:")
        for category, count in sorted(report.by_category.items()):
            print(f"    {category:28} {count}")

    repeats = sorted(report.duplicates_by_institution.items(), key=lambda p: (-p[1], p[0]))
    if repeats:
        print(f"\n  pages claimed for more than one category, by institution ({len(repeats)}):")
        for name, count in repeats[:10]:
            print(f"    {name[:46]:46} {count}")
        if len(repeats) > 10:
            print(f"    ... and {len(repeats) - 10} more")

    if report.rejected_urls:
        print(f"\n  {len(report.rejected_urls)} URL(s) refused and NOT stored:")
        for line in report.rejected_urls[:20]:
            print(f"    {line}")

    for note in report.readme_notes:
        print(f"\n  note: {note}")

    if report.superseded_submission_ids:
        print(
            f"\n  {len(report.superseded_submission_ids)} earlier source list(s) marked "
            "SUPERSEDED. Their rows are untouched and still queryable."
        )

    if not dry_run:
        print(
            "\nStaging only: no university, source, mapping or published fact was created, "
            "and no page was fetched.\nEvery URL is PENDING. Next:\n"
            "  make source-verification-report list=1"
        )


if __name__ == "__main__":
    raise SystemExit(main())
