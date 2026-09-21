"""Source coverage: what we still do not know where to find.

The coverage matrix answers, per institution in scope: is its official identity
settled, and do we have a verified official source for each category of information
the product needs?

WHY COVERAGE COUNTS ONLY *VERIFIED* SOURCES
===========================================
A candidate URL is a guess. Counting it toward coverage would make the report say we
are ready to collect from a page nobody has confirmed, which is precisely the failure
the platform exists to prevent. Only `VERIFIED_OFFICIAL` and `AUTHORIZED_EXTERNAL`
mappings that are active count.

WHY POSTGRADUATE AND DOCTORAL COVERAGE ARE SEPARATE
===================================================
A taught-Masters admissions page is not evidence about doctoral admissions. They are
different pages run by different offices at almost every institution in the list, so
`has_taught_postgraduate_admissions` and `has_research_postgraduate_admissions` are
independent columns. A mapping contributes to doctoral coverage only if it carries an
explicit `RESEARCH_POSTGRADUATE` scope row -- silence never implies coverage.

Coverage is read from the `target_source_coverage` view, which lives in the migration
because it is a query rather than a table. The constants here are the contract the
view implements, and a test asserts the two agree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, text

from app.db.enums import DegreeScope, SourceCategory

#: The view this module reads.
COVERAGE_VIEW = "target_source_coverage"


class CoverageStatus(StrEnum):
    """Readiness of one institution for collection.

    Deliberately ordered as a funnel: an institution cannot be complete before its
    identity is settled, and reporting "sources missing" for an institution we have
    not even identified would hide the real blocker.
    """

    IDENTITY_NOT_VERIFIED = "IDENTITY_NOT_VERIFIED"
    NO_SOURCES_MAPPED = "NO_SOURCES_MAPPED"
    SOURCE_MAPPING_INCOMPLETE = "SOURCE_MAPPING_INCOMPLETE"
    SOURCE_MAPPING_COMPLETE = "SOURCE_MAPPING_COMPLETE"
    BLOCKED = "BLOCKED"


#: Categories required before an institution counts as fully mapped, keyed by the
#: coverage column that reports them. Each value is the set of source categories that
#: can satisfy that requirement -- more than one, because universities publish the
#: same information in different places.
REQUIRED_COVERAGE: dict[str, frozenset[SourceCategory]] = {
    "has_homepage": frozenset({SourceCategory.UNIVERSITY_HOME}),
    "has_undergraduate_admissions": frozenset({SourceCategory.UNDERGRADUATE_ADMISSIONS}),
    "has_taught_postgraduate_admissions": frozenset({SourceCategory.POSTGRADUATE_ADMISSIONS}),
    "has_research_postgraduate_admissions": frozenset(
        {SourceCategory.PHD_ADMISSIONS, SourceCategory.POSTGRADUATE_ADMISSIONS}
    ),
    "has_program_catalog": frozenset({SourceCategory.PROGRAM_CATALOG, SourceCategory.PROGRAM_PAGE}),
    "has_entry_requirements": frozenset({SourceCategory.ENTRY_REQUIREMENTS}),
    "has_language_requirements": frozenset({SourceCategory.LANGUAGE_REQUIREMENTS}),
    "has_tuition": frozenset({SourceCategory.TUITION_FEES}),
    "has_deadlines": frozenset(
        {SourceCategory.APPLICATION_DEADLINES, SourceCategory.ACADEMIC_CALENDAR}
    ),
}

#: Coverage columns that additionally require a matching degree scope. Admissions
#: coverage for doctoral applicants is not satisfied by a page scoped only to taught
#: postgraduates, even though `POSTGRADUATE_ADMISSIONS` appears in both sets above.
REQUIRED_DEGREE_SCOPE: dict[str, DegreeScope] = {
    "has_undergraduate_admissions": DegreeScope.UNDERGRADUATE,
    "has_taught_postgraduate_admissions": DegreeScope.TAUGHT_POSTGRADUATE,
    "has_research_postgraduate_admissions": DegreeScope.RESEARCH_POSTGRADUATE,
}


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Coverage for one institution in scope."""

    target_institution_id: uuid.UUID
    match_key: str
    qs_name: str | None
    destination_code: str | None
    onboarding_status: str
    identity_verified: bool
    has_homepage: bool
    has_undergraduate_admissions: bool
    has_taught_postgraduate_admissions: bool
    has_research_postgraduate_admissions: bool
    has_program_catalog: bool
    has_entry_requirements: bool
    has_language_requirements: bool
    has_tuition: bool
    has_deadlines: bool
    verified_source_count: int
    candidate_source_count: int
    verified_domain_count: int
    coverage_status: str

    def missing(self) -> list[str]:
        """Which coverage requirements are unmet, for an operator's worklist."""
        return [column for column in REQUIRED_COVERAGE if not getattr(self, column)]


def coverage_report(
    connection: Connection,
    *,
    destination_code: str | None = None,
    only_current: bool = True,
    limit: int | None = None,
) -> list[CoverageRow]:
    """Read the coverage matrix.

    ``only_current`` restricts to institutions the latest list still names. An
    institution dropped from scope keeps its row in the view -- its sources and
    evidence are still real -- so the default is to hide it from the worklist rather
    than to pretend it never existed.
    """
    clauses = []
    params: dict[str, object] = {}
    if only_current:
        clauses.append("is_in_current_list")
    if destination_code is not None:
        clauses.append("destination_code = :destination_code")
        params["destination_code"] = destination_code

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT :limit"
        params["limit"] = limit

    rows = connection.execute(
        text(
            f"SELECT * FROM {COVERAGE_VIEW} {where} "  # noqa: S608 - identifiers are constants
            f"ORDER BY coverage_status, destination_code NULLS LAST, match_key {limit_sql}"
        ),
        params,
    ).mappings()

    return [
        CoverageRow(
            target_institution_id=row["target_institution_id"],
            match_key=row["match_key"],
            qs_name=row["qs_name"],
            destination_code=row["destination_code"],
            onboarding_status=row["onboarding_status"],
            identity_verified=row["identity_verified"],
            has_homepage=row["has_homepage"],
            has_undergraduate_admissions=row["has_undergraduate_admissions"],
            has_taught_postgraduate_admissions=row["has_taught_postgraduate_admissions"],
            has_research_postgraduate_admissions=row["has_research_postgraduate_admissions"],
            has_program_catalog=row["has_program_catalog"],
            has_entry_requirements=row["has_entry_requirements"],
            has_language_requirements=row["has_language_requirements"],
            has_tuition=row["has_tuition"],
            has_deadlines=row["has_deadlines"],
            verified_source_count=row["verified_source_count"],
            candidate_source_count=row["candidate_source_count"],
            verified_domain_count=row["verified_domain_count"],
            coverage_status=row["coverage_status"],
        )
        for row in rows
    ]


def coverage_totals(connection: Connection, *, only_current: bool = True) -> dict[str, int]:
    """Counts by coverage status, for a dashboard tile or an operations report."""
    where = "WHERE is_in_current_list" if only_current else ""
    rows = connection.execute(
        text(
            f"SELECT coverage_status, count(*) AS n FROM {COVERAGE_VIEW} {where} "  # noqa: S608
            "GROUP BY coverage_status"
        )
    ).all()
    return {row.coverage_status: row.n for row in rows}


__all__ = [
    "COVERAGE_VIEW",
    "REQUIRED_COVERAGE",
    "REQUIRED_DEGREE_SCOPE",
    "CoverageRow",
    "CoverageStatus",
    "coverage_report",
    "coverage_totals",
]
