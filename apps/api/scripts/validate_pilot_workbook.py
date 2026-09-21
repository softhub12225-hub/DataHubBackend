"""Check a returned pilot collection workbook before anyone tries to import it.

Usage::

    uv run python apps/api/scripts/validate_pilot_workbook.py returned.xlsx
    uv run python apps/api/scripts/validate_pilot_workbook.py returned.xlsx --expect-selected 35

Reads the workbook and reports what is wrong with it. **Writes nothing.** The
importer is deliberately not built until the client returns the real file, so this
is the tool for the conversation that happens first -- handing back "Tuition row 14,
program_ref P0007 is not defined on the Programs sheet" instead of a rejected file.

`--expect-selected` is a check, never an instruction. If the client was asked for 35
and marked 34, that is a question to put to them, not a cue to pick the missing one.

Exit codes: 0 when there are no errors (warnings alone still exit 0 -- a parked
applicant scope is expected, not a failure), 1 when the workbook cannot be imported
as it stands, 2 on a usage problem.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.onboarding.pilot_workbook import validate_workbook
from app.domains.onboarding.workbook import WorkbookRejectedError


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
        "--role",
        default=DatabaseRole.API.value,
        choices=[role.value for role in DatabaseRole],
        help="database identity to read as (default: api; this command only reads)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole(args.role)), future=True)
    try:
        with engine.connect() as connection:
            try:
                report = validate_workbook(
                    connection, args.workbook, expected_selected=args.expect_selected
                )
            except WorkbookRejectedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        engine.dispose()

    print(report.summary())
    for sheet, count in sorted(report.rows_by_sheet.items()):
        print(f"  {sheet:24} {count} row(s)")

    if report.issues:
        grouped: dict[str, list[str]] = defaultdict(list)
        for issue in report.issues:
            where = f"row {issue.row}" if issue.row else "sheet"
            grouped[f"{issue.severity} {issue.code}"].append(
                f"      {issue.sheet} {where}: {issue.message}"
            )
        print()
        for heading in sorted(grouped):
            lines = grouped[heading]
            print(f"  {heading} ({len(lines)})")
            # Twenty of the same mistake is one conversation, not twenty.
            for line in lines[:20]:
                print(line)
            if len(lines) > 20:
                print(f"      ... and {len(lines) - 20} more")

    print()
    if report.is_importable:
        parked = report.scope_parked
        print("No errors. The workbook can be imported.")
        if parked:
            print(
                f"{parked} row(s) have an applicant scope that needs a human to map it. "
                "They will be held for review, never treated as applying to everybody."
            )
        return 0

    print(f"{len(report.errors)} error(s). Please send these back to the client.")
    print("Nothing was written, and nothing will be until the file is clean.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
