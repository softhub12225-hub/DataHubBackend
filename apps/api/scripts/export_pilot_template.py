"""Export the pilot data-collection workbook from the imported target list.

Usage::

    # Every institution currently in scope:
    uv run python apps/api/scripts/export_pilot_template.py out/pilot.xlsx

    # Only the pilot destinations, with a reminder of the intended pilot size:
    uv run python apps/api/scripts/export_pilot_template.py out/pilot.xlsx \
        --destination GB --destination HK --destination MO --pilot-target 35

The client's own workbook is never read or modified by this command. It writes a new
file, generated from `target_institution`, carrying a stable `target_institution_id`
on every row so a returned workbook can be matched back unambiguously.

Nothing is fetched and nothing is published.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.onboarding.pilot_template import TEMPLATE_SHEETS, build_pilot_template


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("output", type=Path, help="path to write the .xlsx to")
    parser.add_argument(
        "--destination",
        action="append",
        default=None,
        metavar="CODE",
        help="restrict to a destination code (repeatable), e.g. --destination GB",
    )
    parser.add_argument(
        "--pilot-target",
        type=int,
        default=None,
        help="intended number of pilot institutions; written into the README as a "
        "reminder, never enforced",
    )
    parser.add_argument(
        "--include-dropped",
        action="store_true",
        help="also include institutions omitted by the most recent target list",
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

    if args.output.suffix.lower() != ".xlsx":
        print(f"error: output must end in .xlsx, got {args.output.name!r}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole(args.role)), future=True)
    try:
        with engine.connect() as connection:
            try:
                written = build_pilot_template(
                    connection,
                    args.output,
                    destination_codes=args.destination,
                    only_current=not args.include_dropped,
                    pilot_target=args.pilot_target,
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
    finally:
        engine.dispose()

    print(f"wrote {written}")
    print(f"sheets: README, {', '.join(sheet.name for sheet in TEMPLATE_SHEETS)}")
    print()
    print("Every row carries target_institution_id in column A. It is the only thing")
    print("that matches a returned workbook back to our records -- ask the client not")
    print("to edit or remove it. A row without it needs manual resolution; we will not")
    print("guess from a name.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
