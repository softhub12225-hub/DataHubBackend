"""Reading the source verification queue (U15).

The client's official-source list carries 385 claimed responsibilities over 319
distinct pages for 35 institutions, and every one of them needs a human to look at it
before anything it says can be published. This module is how that worklist is read: a
query layer over the `pilot_source_verification_queue` view, plus the counts an
operator needs to know whether the pass is nearly done or barely started.

Reading only. Every decision goes through `pilot/verification.py`, which requires an
actor and a reason and appends to the audit chain. Nothing here writes, and nothing
here classifies: a candidate's state changes only because a person changed it.

THE COUNTS ARE THE POINT
========================
"143 pending" is the number that decides whether to start collecting more or finish
triaging what we have. Grouped by institution it says which universities are
blocked; grouped by source type it says whether it is the tuition pages or the
deadline pages that nobody has got to; grouped by degree level it says whether the
postgraduate half of the pilot is ready. Those three groupings are the ones asked
for, and they are cheap because the view's partial index covers exactly the rows that
are still undecided.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Column, Connection, MetaData, Table, and_, func, select
from sqlalchemy import types as sa_types

from app.db.enums import (
    UNCLASSIFIED_SOURCE_TYPE,
    OfficialVerificationStatus,
    SourceCandidateState,
)

#: The view, described rather than mapped: it is read-only by construction and has no
#: primary key, so an ORM entity would imply an identity and a writability it does
#: not have. Its own `MetaData` keeps it out of `Base.metadata`, where Alembic would
#: try to create it as a table.
_metadata = MetaData()

QUEUE_VIEW = Table(
    "pilot_source_verification_queue",
    _metadata,
    Column("candidate_id", sa_types.Uuid),
    Column("submission_id", sa_types.Uuid),
    Column("original_filename", sa_types.String),
    Column("imported_at", sa_types.DateTime(timezone=True)),
    Column("import_status", sa_types.String),
    Column("target_institution_id", sa_types.Uuid),
    Column("match_key", sa_types.String),
    Column("destination_code", sa_types.String),
    Column("onboarding_status", sa_types.String),
    Column("list_name", sa_types.String),
    Column("collector_official_name", sa_types.String),
    Column("institution_is_selected", sa_types.Boolean),
    Column("source_ref", sa_types.String),
    Column("source_type", sa_types.String),
    Column("degree_scope", sa_types.String),
    Column("workbook_column", sa_types.String),
    Column("duplicate_of_source_ref", sa_types.String),
    Column("is_physical_source", sa_types.Boolean),
    Column("url_sha256", sa_types.String),
    Column("official_url", sa_types.Text),
    Column("normalized_url", sa_types.Text),
    Column("host", sa_types.String),
    Column("is_third_party", sa_types.Boolean),
    Column("collector_checked_at", sa_types.Date),
    Column("collector_notes", sa_types.Text),
    Column("verification_state", sa_types.String),
    Column("verified_at", sa_types.DateTime(timezone=True)),
    Column("verified_by", sa_types.Uuid),
    Column("verification_reason", sa_types.Text),
    Column("promoted_source_mapping_id", sa_types.Uuid),
    Column("domain_host", sa_types.String),
    Column("domain_verification_status", sa_types.String),
    Column("domain_covers_subdomains", sa_types.Boolean),
    Column("host_matches_verified_domain", sa_types.Boolean),
    Column("host_matches_authorized_domain", sa_types.Boolean),
)


@dataclass(frozen=True, slots=True)
class DomainEvidence:
    """What we already know about the candidate's hostname. Evidence, not a verdict.

    `matches_verified_domain` being true is a reason for a reviewer to look, and is
    never on its own a reason to verify. See this module's header.
    """

    host: str
    matched_domain_host: str | None
    domain_verification_status: str | None
    covers_subdomains: bool | None

    @property
    def matches_verified_domain(self) -> bool:
        return self.domain_verification_status == OfficialVerificationStatus.VERIFIED_OFFICIAL.value

    @property
    def matches_authorized_domain(self) -> bool:
        return (
            self.domain_verification_status == OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value
        )


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    """One queue row, with everything a reviewer needs to decide."""

    candidate_id: uuid.UUID
    submission_id: uuid.UUID
    target_institution_id: uuid.UUID
    match_key: str
    destination_code: str
    list_name: str | None
    collector_official_name: str | None
    institution_is_selected: bool
    source_ref: str
    source_type: str
    degree_scope: str | None
    #: Which workbook heading claimed this page. Import lineage.
    workbook_column: str | None
    #: The row that first registered this URL for this institution, when this one
    #: repeats it. NULL means this row *is* the physical page.
    duplicate_of_source_ref: str | None
    is_physical_source: bool
    url_sha256: str
    official_url: str
    normalized_url: str
    host: str
    is_third_party: bool | None
    collector_checked_at: date | None
    collector_notes: str | None
    verification_state: str
    verified_at: datetime | None
    verified_by: uuid.UUID | None
    verification_reason: str | None
    promoted_source_mapping_id: uuid.UUID | None
    domain: DomainEvidence


#: What "still to do" means: a candidate nobody has decided, and one somebody
#: explicitly asked a second person to look at.
OPEN_STATES: tuple[str, ...] = (
    SourceCandidateState.PENDING.value,
    SourceCandidateState.NEEDS_REVIEW.value,
)


@dataclass(frozen=True, slots=True)
class StateCounts:
    """How many candidates are in each state, for one grouping."""

    total: int = 0
    pending: int = 0
    verified: int = 0
    rejected: int = 0
    needs_review: int = 0

    @property
    def open(self) -> int:
        return self.pending + self.needs_review

    @property
    def decided(self) -> int:
        return self.verified + self.rejected


@dataclass(slots=True)
class QueueSummary:
    """The worklist, counted three ways.

    Group keys are ordered the way a report should print them: institutions by how
    much work is left, source types and degree levels alphabetically so two runs are
    comparable.
    """

    overall: StateCounts = field(default_factory=StateCounts)
    by_institution: dict[str, StateCounts] = field(default_factory=dict)
    by_source_type: dict[str, StateCounts] = field(default_factory=dict)
    by_degree_level: dict[str, StateCounts] = field(default_factory=dict)
    #: How many open candidates sit on a host already verified for that institution.
    #: Evidence for prioritising, never grounds to auto-verify.
    open_on_verified_host: int = 0
    #: Every candidate whose host matches a verified domain, decided or not. Reported
    #: because an operator asks "how many of these are already on a known domain?",
    #: and the honest answer must not be confused with "how many are verified".
    host_matches_verified_domain: int = 0

    #: Physical identity, as distinct from claimed responsibility (Step 5A section 7).
    #: `total` counts responsibilities; a page answering for three categories is three
    #: rows and one eventual acquisition target.
    distinct_urls: int = 0
    physical_sources: int = 0
    duplicate_responsibilities: int = 0
    #: Distinct pages nobody has categorised yet. These are the ones needing a human.
    unclassified_sources: int = 0

    def responsibility_summary(self) -> str:
        """The URL/responsibility split in one line.

        Worth saying separately: "385 candidates" and "319 pages to fetch" are both
        true, and an operator planning acquisition needs the second while a reviewer
        working the queue needs the first.
        """
        return (
            f"{self.overall.total} claimed responsibilit"
            f"{'y' if self.overall.total == 1 else 'ies'} over "
            f"{self.physical_sources} distinct page"
            f"{'' if self.physical_sources == 1 else 's'} "
            f"({self.duplicate_responsibilities} repeat a page already listed; "
            f"{self.unclassified_sources} page"
            f"{'' if self.unclassified_sources == 1 else 's'} unclassified)"
        )

    def summary(self) -> str:
        counts = self.overall
        institutions = len(self.by_institution)
        return (
            f"{counts.total} candidate{'' if counts.total == 1 else 's'} across "
            f"{institutions} institution{'' if institutions == 1 else 's'}: "
            f"{counts.pending} pending, {counts.needs_review} need review, "
            f"{counts.verified} verified, {counts.rejected} rejected"
        )


def queue_summary(
    connection: Connection,
    *,
    submission_id: uuid.UUID | None = None,
    selected_only: bool = True,
) -> QueueSummary:
    """Count the worklist by institution, source type and degree level.

    `selected_only` defaults to true because a candidate collected for an institution
    the client did not select is not blocking anything. Pass false to see everything
    the workbook contained.
    """
    rows = connection.execute(_scoped(submission_id, selected_only)).all()

    summary = QueueSummary()
    overall: Counter[str] = Counter()
    per_institution: dict[str, Counter[str]] = {}
    per_source_type: dict[str, Counter[str]] = {}
    per_degree: dict[str, Counter[str]] = {}

    seen_urls: set[str] = set()
    for row in rows:
        state = str(row.verification_state)
        overall[state] += 1
        institution = row.collector_official_name or row.list_name or str(row.match_key)
        per_institution.setdefault(institution, Counter())[state] += 1
        per_source_type.setdefault(str(row.source_type), Counter())[state] += 1
        # A source page that serves every applicant group has no degree level, and
        # saying so is more honest than filing it under one.
        per_degree.setdefault(row.degree_scope or "(unspecified)", Counter())[state] += 1
        if row.host_matches_verified_domain:
            summary.host_matches_verified_domain += 1
            if state in OPEN_STATES:
                summary.open_on_verified_host += 1

        seen_urls.add(row.url_sha256)
        if row.is_physical_source:
            summary.physical_sources += 1
        else:
            summary.duplicate_responsibilities += 1
        if row.source_type == UNCLASSIFIED_SOURCE_TYPE and row.is_physical_source:
            summary.unclassified_sources += 1
    summary.distinct_urls = len(seen_urls)

    summary.overall = _counts(overall)
    summary.by_institution = dict(
        sorted(
            ((name, _counts(counter)) for name, counter in per_institution.items()),
            key=lambda item: (-item[1].open, item[0]),
        )
    )
    summary.by_source_type = {
        name: _counts(counter) for name, counter in sorted(per_source_type.items())
    }
    summary.by_degree_level = {
        name: _counts(counter) for name, counter in sorted(per_degree.items())
    }
    return summary


def open_candidates(
    connection: Connection,
    *,
    submission_id: uuid.UUID | None = None,
    target_institution_id: uuid.UUID | None = None,
    source_type: str | None = None,
    selected_only: bool = True,
    limit: int | None = None,
) -> list[CandidateDetail]:
    """The rows still awaiting a decision, in a stable review order.

    Ordered by institution then source type then ref, so a reviewer working the list
    twice sees it the same way round, and two people splitting it by institution do
    not collide.
    """
    statement = _scoped(submission_id, selected_only).where(
        QUEUE_VIEW.c.verification_state.in_(OPEN_STATES)
    )
    if target_institution_id is not None:
        statement = statement.where(QUEUE_VIEW.c.target_institution_id == target_institution_id)
    if source_type is not None:
        statement = statement.where(QUEUE_VIEW.c.source_type == source_type)
    statement = statement.order_by(
        QUEUE_VIEW.c.match_key, QUEUE_VIEW.c.source_type, QUEUE_VIEW.c.source_ref
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [row_to_detail(row) for row in connection.execute(statement)]


def physical_sources(
    connection: Connection,
    *,
    submission_id: uuid.UUID | None = None,
    selected_only: bool = True,
    verified_only: bool = False,
) -> list[CandidateDetail]:
    """The distinct pages, one row each -- what acquisition will eventually fetch.

    A page claimed for three categories appears once here and three times in
    `open_candidates`, because fetching is per page and review is per claim.

    `verified_only` is the form the acquisition layer will want, and it returns
    nothing at all until a human has verified something. That is the intended
    behaviour, not an empty-result bug.
    """
    statement = _scoped(submission_id, selected_only).where(
        QUEUE_VIEW.c.duplicate_of_source_ref.is_(None)
    )
    if verified_only:
        statement = statement.where(
            QUEUE_VIEW.c.verification_state == SourceCandidateState.VERIFIED.value
        )
    statement = statement.order_by(QUEUE_VIEW.c.match_key, QUEUE_VIEW.c.source_ref)
    return [row_to_detail(row) for row in connection.execute(statement)]


def open_count(connection: Connection, *, selected_only: bool = True) -> int:
    """How many candidates still need a human. The one number worth alerting on."""
    statement = (
        select(func.count())
        .select_from(QUEUE_VIEW)
        .where(QUEUE_VIEW.c.verification_state.in_(OPEN_STATES))
    )
    if selected_only:
        statement = statement.where(QUEUE_VIEW.c.institution_is_selected.is_(True))
    return connection.execute(statement).scalar_one()


def _scoped(submission_id: uuid.UUID | None, selected_only: bool):  # type: ignore[no-untyped-def]
    conditions = []
    if submission_id is not None:
        conditions.append(QUEUE_VIEW.c.submission_id == submission_id)
    if selected_only:
        conditions.append(QUEUE_VIEW.c.institution_is_selected.is_(True))
    statement = select(QUEUE_VIEW)
    return statement.where(and_(*conditions)) if conditions else statement


def _counts(counter: Counter[str]) -> StateCounts:
    return StateCounts(
        total=sum(counter.values()),
        pending=counter.get(SourceCandidateState.PENDING.value, 0),
        verified=counter.get(SourceCandidateState.VERIFIED.value, 0),
        rejected=counter.get(SourceCandidateState.REJECTED.value, 0),
        needs_review=counter.get(SourceCandidateState.NEEDS_REVIEW.value, 0),
    )


def row_to_detail(row: Any) -> CandidateDetail:
    """One view row as a typed record.

    Worth the transcription: a `Row` is addressed by attribute at runtime and checked
    by nobody, so a renamed view column becomes an AttributeError in a report rather
    than a type error at the edit.
    """
    return CandidateDetail(
        candidate_id=row.candidate_id,
        submission_id=row.submission_id,
        target_institution_id=row.target_institution_id,
        match_key=row.match_key,
        destination_code=row.destination_code,
        list_name=row.list_name,
        collector_official_name=row.collector_official_name,
        institution_is_selected=bool(row.institution_is_selected),
        source_ref=row.source_ref,
        source_type=row.source_type,
        degree_scope=row.degree_scope,
        workbook_column=row.workbook_column,
        duplicate_of_source_ref=row.duplicate_of_source_ref,
        is_physical_source=bool(row.is_physical_source),
        url_sha256=row.url_sha256,
        official_url=row.official_url,
        normalized_url=row.normalized_url,
        host=row.host,
        is_third_party=row.is_third_party,
        collector_checked_at=row.collector_checked_at,
        collector_notes=row.collector_notes,
        verification_state=str(row.verification_state),
        verified_at=row.verified_at,
        verified_by=row.verified_by,
        verification_reason=row.verification_reason,
        promoted_source_mapping_id=row.promoted_source_mapping_id,
        domain=DomainEvidence(
            host=row.host,
            matched_domain_host=row.domain_host,
            domain_verification_status=row.domain_verification_status,
            covers_subdomains=row.domain_covers_subdomains,
        ),
    )


__all__ = [
    "OPEN_STATES",
    "QUEUE_VIEW",
    "CandidateDetail",
    "DomainEvidence",
    "QueueSummary",
    "StateCounts",
    "open_candidates",
    "open_count",
    "physical_sources",
    "queue_summary",
    "row_to_detail",
]
