"""The source verification worklist (U15).

Usage::

    uv run python apps/api/scripts/source_verification_report.py
    uv run python apps/api/scripts/source_verification_report.py --by source-type
    uv run python apps/api/scripts/source_verification_report.py --list --institution <uuid>
    uv run python apps/api/scripts/source_verification_report.py --all-institutions

About 200 collected URLs need a human decision before anything they support can be
published. This prints how many, grouped by institution, source type or degree level,
and can list the individual rows still waiting.

**Reads only.** Recording a decision goes through `pilot/verification.py`, which
requires an actor and a reason and appends to the audit chain. Nothing here verifies
anything, and there is deliberately no `--verify-all` flag.

The `verified host` column says the candidate sits on a domain already confirmed as
officially the institution's. That is **evidence for the reviewer**, not a decision:
a university's own domain also hosts news articles, student societies and personal
staff pages, none of which is a tuition page. It is printed because it helps someone
triage, and acted on by nobody.

Exit code 0 always: an empty queue and a full one are both valid states to report.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.pilot.queue import (
    CandidateDetail,
    QueueSummary,
    StateCounts,
    open_candidates,
    physical_sources,
    queue_summary,
)

_GROUPINGS = {
    "institution": "by_institution",
    "source-type": "by_source_type",
    "degree-level": "by_degree_level",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--by",
        choices=sorted(_GROUPINGS),
        action="append",
        default=None,
        help="grouping to print; repeatable (default: all three)",
    )
    parser.add_argument(
        "--submission",
        type=uuid.UUID,
        default=None,
        metavar="ID",
        help="restrict to one submission (default: every submission)",
    )
    parser.add_argument(
        "--institution",
        type=uuid.UUID,
        default=None,
        metavar="ID",
        help="restrict the --list output to one target institution",
    )
    parser.add_argument(
        "--all-institutions",
        action="store_true",
        help="include candidates for institutions the client did not select",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the individual candidates still awaiting a decision",
    )
    parser.add_argument(
        "--pages",
        action="store_true",
        help="list the distinct pages instead of the claims -- what acquisition will "
        "eventually fetch, one row each",
    )
    parser.add_argument(
        "--limit", type=int, default=40, metavar="N", help="rows to list (default: 40)"
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
    selected_only = not args.all_institutions

    settings = get_settings()
    engine = create_engine(settings.database.sync_dsn(DatabaseRole(args.role)), future=True)
    try:
        with engine.connect() as connection:
            summary: QueueSummary = queue_summary(
                connection, submission_id=args.submission, selected_only=selected_only
            )
            if args.pages:
                rows = physical_sources(
                    connection, submission_id=args.submission, selected_only=selected_only
                )[: args.limit]
            elif args.list:
                rows = open_candidates(
                    connection,
                    submission_id=args.submission,
                    target_institution_id=args.institution,
                    selected_only=selected_only,
                    limit=args.limit,
                )
            else:
                rows = []
    finally:
        engine.dispose()

    if summary.overall.total == 0:
        print("No collected source candidates. Import a completed workbook first:")
        print("  make import-pilot-workbook f=returned.xlsx")
        return 0

    print(summary.summary())
    print(summary.responsibility_summary())
    print(
        f"{summary.distinct_urls} distinct normalized URL"
        f"{'' if summary.distinct_urls == 1 else 's'}; "
        f"{summary.host_matches_verified_domain} candidate"
        f"{'' if summary.host_matches_verified_domain == 1 else 's'} sit on a host "
        "already verified for that institution."
    )
    if summary.open_on_verified_host:
        print(
            f"{summary.open_on_verified_host} undecided candidate(s) sit on a host already "
            "verified for that institution -- worth looking at first, still a human call."
        )
    print()

    for grouping in args.by or sorted(_GROUPINGS):
        _print_group(grouping, getattr(summary, _GROUPINGS[grouping]))

    if args.list or args.pages:
        _print_rows(rows, limit=args.limit)
    elif summary.overall.open:
        print(f"{summary.overall.open} still open. Re-run with --list to see them.")

    return 0


def _print_group(name: str, counts: dict[str, StateCounts]) -> None:
    width = max((len(key) for key in counts), default=0)
    width = min(max(width, 12), 48)
    print(f"By {name.replace('-', ' ')}:")
    print(f"  {'':{width}}  total  pend   ver   rej  rev")
    for key, row in counts.items():
        label = key if len(key) <= width else key[: width - 1] + "…"
        print(
            f"  {label:{width}}  {row.total:5}  {row.pending:4}  "
            f"{row.verified:4}  {row.rejected:4}  {row.needs_review:3}"
        )
    print()


def _print_rows(rows: list[CandidateDetail], *, limit: int) -> None:
    if not rows:
        print("Nothing awaiting a decision in this scope.")
        return
    print(f"Open candidates ({len(rows)} shown, limit {limit}):")
    for row in rows:
        institution = row.collector_official_name or row.list_name or row.match_key
        if row.domain.matches_verified_domain:
            evidence = "verified host"
        elif row.domain.matches_authorized_domain:
            evidence = "authorized host"
        else:
            evidence = "host not verified"
        identity = "page" if row.is_physical_source else f"repeat of {row.duplicate_of_source_ref}"
        print(
            f"  {row.source_ref}  {institution[:38]:38}  {row.source_type:24}  "
            f"{row.degree_scope or '-':22}  {row.verification_state:12}  {evidence}"
        )
        print(f"      {row.official_url}")
        print(
            f"      {identity}" + (f"    from {row.workbook_column}" if row.workbook_column else "")
        )
        if row.collector_checked_at or row.collector_notes:
            checked = row.collector_checked_at or "-"
            print(f"      checked {checked}    {row.collector_notes or ''}".rstrip())
    print()
    print(
        "To decide one, call pilot.verification.verify_candidate / reject_candidate / "
        "flag_candidate_for_review with an actor and a reason. There is no bulk verify."
    )


if __name__ == "__main__":
    raise SystemExit(main())
