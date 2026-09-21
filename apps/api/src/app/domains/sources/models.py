"""Evidence plane: where a fact comes from and what proves it.

Append-only by design (architecture C1). `snapshot`, `extraction`, `field_claim` and
`claim_resolution` are written once and never revised: they record what a source said
at a moment in time, and revising that would destroy the only reason to keep it.

Raw bodies are **not** stored here. `snapshot` carries a content hash plus an
object-storage key; the HTML or PDF itself lives in S3/MinIO, because thousands of
multi-megabyte documents in PostgreSQL would bloat every backup and buy nothing.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import (
    EXTRACTION_STATUS,
    FETCH_ELIGIBILITY,
    FETCH_STATUS,
    FIELD_STATUS,
    PUBLICATION_ELIGIBILITY,
    SOURCE_ACCESS_STATE,
    SOURCE_RESPONSIBILITY,
    ExtractionStatus,
    FetchEligibility,
    FetchStatus,
    FieldStatus,
    PublicationEligibility,
    SourceAccessState,
    SourceResponsibility,
)
from app.db.mixins import RecordedAtMixin, TimestampedMixin, uuid_pk

#: What a human may decide about a candidate (Step 5C.3 section 3). Deliberately
#: excludes `SUPERSEDED` and `SOURCE_NOT_VERIFIED`, which the instruction suggested as
#: states but which no human decides: a machine knows both at any moment, from the
#: candidate's rule version and the source's eligibility. Mixing them in would make one
#: column answer two questions (D39) and would hide a reviewer's judgement behind a fact
#: about the source, which section 30 forbids. `candidate_review_state` exposes them as
#: separate columns instead.
CANDIDATE_DECISIONS_SQL: tuple[str, ...] = (
    "ACCEPTED",
    "REJECTED",
    "NEEDS_CONTEXT",
    "NEEDS_SCOPE_MAPPING",
)

#: Why a reviewer decided what they decided. The first six are exactly
#: `review_decision.reason_code`'s vocabulary, reused rather than re-spelled: a reviewer
#: rejecting a candidate and a reviewer returning a proposal reject for the same kinds
#: of reason, and two spellings of one reason cannot be reported together. The rest come
#: from what the Step 5C.2 audit actually found on real pages.
CANDIDATE_REASON_CODES_SQL: tuple[str, ...] = (
    "EVIDENCE_INSUFFICIENT",
    "WRONG_SOURCE",
    "PARSE_ERROR",
    "NOT_OFFICIAL",
    "NEEDS_RECOLLECTION",
    "OTHER",
    "NOT_A_FACT_OF_THIS_KIND",
    "SITE_CHROME",
    "MARKETING_PROSE",
    "SCOPE_AMBIGUOUS",
    "PROGRAM_CONTEXT_MISSING",
    "SUPERSEDED_BY_NEWER_RULE",
    "CONFLICTS_WITH_ANOTHER_SOURCE",
    "CORRECT_AS_EXTRACTED",
)


#: Field kinds `field_claim_candidate.field_kind` accepts (Step 5C.2). A CHECK
#: rather than a PostgreSQL enum: these are extractor output categories that grow
#: as rules are added, and growing a CHECK is a one-line migration while growing
#: an enum is a type change that cannot be rolled back.
FIELD_KINDS_SQL: tuple[str, ...] = (
    "PROGRAM_NAME",
    "DEGREE_LEVEL",
    "DURATION",
    "STUDY_MODE",
    "CAMPUS",
    "DISCIPLINE_HINT",
    "FACULTY_OR_SCHOOL",
    "ADMISSION_REQUIREMENT",
    "LANGUAGE_TEST",
    "LANGUAGE_OVERALL_SCORE",
    "LANGUAGE_COMPONENT_SCORE",
    "TUITION",
    "APPLICATION_DEADLINE",
    "ACADEMIC_CALENDAR_EVENT",
)


class Source(TimestampedMixin, Base):
    """A registered official URL.

    `access_state` implements D6: a source that blocks automated access is
    reclassified as `MANUAL_ONLY`, never circumvented. The robots and ToS columns
    exist so that activating a source is a recorded decision, not an assumption.

    `publication_eligibility` (C27) is what makes "may determine scope but may not
    become a fact" checkable. It defaults to `NOT_ELIGIBLE`, so a newly registered
    source supports nothing until someone classifies it, and triggers on
    `field_claim` and `field_provenance` refuse evidence whose class does not permit
    the fact being asserted.

    It is deliberately distinct from `authority_tier`, which ranks *how
    authoritative* an eligible source is when two of them disagree.
    Eligibility answers a prior question -- may this source be cited at all -- and
    collapsing the two would make "tier 5" mean both "least trusted" and "forbidden".

    FETCHING AND PUBLISHING ARE DIFFERENT QUESTIONS (Step 5B)
    ========================================================
    `fetch_eligibility` says whether a worker may send a request; it is decided by
    syntax, scheme, SSRF validation and the site's own behaviour, and a machine can
    answer it. `publication_eligibility` says whether a fact from this source may be
    published; only a person can answer that, and C27 still refuses everything by
    default.

    The normal state for every source in the pilot today is `FETCHABLE` +
    `NOT_ELIGIBLE`: we may look, and nothing we see may be published yet. Requiring
    verification before acquisition -- which Step 5A effectively did -- meant a
    reviewer had to judge a page they could not see through our own record.

    **A source existing is not a trust signal.** Registration means "this URL is a
    known acquisition target", nothing more.
    """

    __tablename__ = "source"

    id: Mapped[uuid.UUID] = uuid_pk()
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="sha256 of the normalised URL"
    )
    source_type: Mapped[str] = mapped_column(String(48), nullable=False)
    owner_entity_type: Mapped[str | None] = mapped_column(String(64))
    owner_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    authority_tier: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    crawl_frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    fetch_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    tos_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tos_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_state: Mapped[SourceAccessState] = mapped_column(
        SOURCE_ACCESS_STATE, nullable=False, server_default=SourceAccessState.OK.value
    )
    #: C27. Whether this source may support a published fact, and of what kind.
    #: Closed by default.
    publication_eligibility: Mapped[PublicationEligibility] = mapped_column(
        PUBLICATION_ELIGIBILITY,
        nullable=False,
        server_default=PublicationEligibility.NOT_ELIGIBLE.value,
        comment=(
            "C27: what this source may substantiate. Earned by promoting a verified "
            "source_mapping, never asserted. AUTHORIZED_RANKING additionally requires "
            "a live display-allowed authorisation at citation time."
        ),
    )
    #: The recorded licence or delegation behind an AUTHORIZED_* class. Required for
    #: those two classes by CHECK, so "authorised" always names its authorisation.
    eligibility_authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_authorization.id", ondelete="RESTRICT")
    )
    eligibility_set_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    eligibility_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligibility_reason: Mapped[str | None] = mapped_column(
        Text, comment="Why this class was assigned; shown to a reviewer"
    )
    #: Step 5B. May a worker request this URL? Operational, never a trust claim.
    fetch_eligibility: Mapped[FetchEligibility] = mapped_column(
        FETCH_ELIGIBILITY,
        nullable=False,
        server_default=FetchEligibility.NEEDS_MANUAL_REVIEW.value,
        comment=(
            "Step 5B: may a worker send a request? NOT publication eligibility. "
            "Defaults closed so a row created without passing validation is not "
            "fetched by default."
        ),
    )
    fetch_eligibility_reason: Mapped[str | None] = mapped_column(
        Text, comment="Why this state; for BLOCKED it is what the site actually did"
    )
    fetch_eligibility_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Politeness, per host rather than per source: 120 hosts serve the pilot's 319
    #: pages, and a university notices 11 simultaneous requests.
    min_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="2", comment="Floor between requests to this host"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    deactivated_reason: Mapped[str | None] = mapped_column(Text)
    #: The source that replaced this one, when a reviewer supplied a new URL for a dead
    #: page (Step 5C.5 section 18). The OLD row is never edited to point at the new URL:
    #: its snapshots were fetched from the old one, and repointing it would make the
    #: evidence say it came from somewhere it did not.
    superseded_by_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "source.id", ondelete="RESTRICT", name="fk_source_superseded_by_source_id_source"
        ),
        nullable=True,
        index=True,
        comment=(
            "The source that replaced this one, when a reviewer supplied a new URL for "
            "a dead page. The OLD row is never edited to point at the new URL: its "
            "snapshots were fetched from the old one, and repointing it would make the "
            "evidence say it came from somewhere it did not. Set only on an inactive "
            "source, because a replaced source that is still fetched is two sources "
            "for one thing."
        ),
    )
    registered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # --- operational timing, deliberately not part of `fetch_eligibility` ----------
    #
    # "May we fetch this at all?" and "may we fetch it *now*?" are different
    # questions, and Step 5B.2 exists because they had been answered by one column:
    # a single 429 set `fetch_eligibility = 'BLOCKED'` and nothing could undo it.
    #
    # A cooldown is not a new eligibility member on purpose. Making it one would mean
    # every reader of that column had to know that one of its values expires, and the
    # scheduler would become the only thing able to say whether a source was really
    # blocked. Timing belongs in a timestamp.
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="Not schedulable before this instant. Expires on its own; no judgement.",
    )
    cooldown_reason: Mapped[str | None] = mapped_column(Text)
    cooldown_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Consecutive 429s. Reset by any success, so "this host throttles us constantly"
    #: is distinguishable from "this host throttled us once in March".
    rate_limit_strikes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    bindings: Mapped[list[SourceFieldBinding]] = relationship(back_populates="source")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('university_site', 'faculty_site', 'admissions_page', "
            "'fee_page', 'official_pdf', 'government_regulator', 'authorized_ranking', "
            "'unclassified')",
            name="source_type_known",
        ),
        # An inactive source is never fetchable: two switches that could disagree is
        # one switch too many.
        CheckConstraint(
            "is_active OR fetch_eligibility <> 'FETCHABLE'",
            name="an_inactive_source_is_not_fetchable",
        ),
        # A source the site has blocked must say what it did, or the operator cannot
        # tell a WAF from a typo, and the difference decides whether to retry at all.
        CheckConstraint(
            "fetch_eligibility <> 'BLOCKED' "
            "OR btrim(coalesce(fetch_eligibility_reason, '')) <> ''",
            name="blocked_names_what_happened",
        ),
        CheckConstraint("min_interval_seconds >= 0", name="min_interval_non_negative"),
        Index("ix_source_fetch_eligibility", "fetch_eligibility"),
        # A pause that does not say why is indistinguishable from a bug, and the
        # operator asking "why is this quiet?" is the whole audience for the column.
        CheckConstraint(
            "cooldown_until IS NULL OR cooldown_reason IS NOT NULL",
            name="cooldown_names_its_reason",
        ),
        CheckConstraint("rate_limit_strikes >= 0", name="rate_limit_strikes_non_negative"),
        CheckConstraint(
            "crawl_frequency IN ('HIGH_RISK_3X_DAILY', 'DAILY', 'WEEKLY', 'MONTHLY', "
            "'EVENT_DRIVEN')",
            name="crawl_frequency_known",
        ),
        CheckConstraint(
            "fetch_strategy IN ('STATIC', 'BROWSER', 'DOCUMENT', 'MANUAL')",
            name="fetch_strategy_known",
        ),
        CheckConstraint("authority_tier BETWEEN 1 AND 5", name="authority_tier_range"),
        CheckConstraint(
            "is_active = true OR deactivated_reason IS NOT NULL",
            name="deactivation_has_a_reason",
        ),
        CheckConstraint(
            "superseded_by_source_id IS NULL OR superseded_by_source_id <> id",
            name="not_its_own_successor",
        ),
        # A replaced source that is still being fetched is two sources for one thing.
        CheckConstraint(
            "superseded_by_source_id IS NULL OR is_active = false",
            name="superseded_source_is_inactive",
        ),
        # C27. An AUTHORIZED_* class is a claim that someone granted permission, so
        # it must name the grant. Without this the two authorised classes would be
        # indistinguishable from an unevidenced assertion.
        CheckConstraint(
            "publication_eligibility NOT IN ('AUTHORIZED_EXTERNAL', 'AUTHORIZED_RANKING') "
            "OR eligibility_authorization_id IS NOT NULL",
            name="authorized_eligibility_names_its_grant",
        ),
        # ...and only those classes may carry one, so the column cannot be used to
        # dress up an unauthorised source.
        CheckConstraint(
            "publication_eligibility IN ('AUTHORIZED_EXTERNAL', 'AUTHORIZED_RANKING') "
            "OR eligibility_authorization_id IS NULL",
            name="grant_only_for_authorized_eligibility",
        ),
        # Leaving the closed default is a recorded decision by a named person.
        CheckConstraint(
            "publication_eligibility = 'NOT_ELIGIBLE' "
            "OR (eligibility_set_by IS NOT NULL AND eligibility_set_at IS NOT NULL)",
            name="eligibility_records_actor_and_time",
        ),
        Index(
            "ix_source_owner_entity_type_owner_entity_id", "owner_entity_type", "owner_entity_id"
        ),
        Index("ix_source_access_state", "access_state"),
        Index("ix_source_publication_eligibility", "publication_eligibility"),
        {"comment": "Registered official sources. Blocked sources become MANUAL_ONLY (D6)."},
    )


class SourceFieldBinding(TimestampedMixin, Base):
    """The 字段归责 matrix, made executable.

    Which source is authoritative for which field is a rule the PRD states in prose.
    Storing it means conflict resolution can reference a recorded decision instead of
    re-deciding case by case.
    """

    __tablename__ = "source_field_binding"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    responsibility: Mapped[SourceResponsibility] = mapped_column(
        SOURCE_RESPONSIBILITY, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="bindings")

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "entity_type",
            "field_path",
            name="uq_source_field_binding_source_id_entity_type_field_path",
        ),
        Index("ix_source_field_binding_entity_type_field_path", "entity_type", "field_path"),
        {"comment": "Field responsibility per source (PRD section 3)."},
    )


class SourceAuthorization(TimestampedMixin, Base):
    """A recorded grant of permission, e.g. a ranking licence.

    `display_allowed` here is what gates the ranking feature (D8). With no row,
    nothing is displayed and nothing is ingested.
    """

    __tablename__ = "source_authorization"

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    grantor: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_key: Mapped[str | None] = mapped_column(
        Text, comment="Object-storage key of the licence document"
    )
    granted_at: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[date | None] = mapped_column(Date)
    display_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        CheckConstraint(
            "expires_at IS NULL OR expires_at >= granted_at", name="validity_is_ordered"
        ),
        Index("ix_source_authorization_scope", "scope"),
        {"comment": "Authorisation grants. Drives the D8 ranking gate."},
    )


class HostCooldown(Base):
    """How long a host is owed before we ask it for anything again (Step 5B.2 §13).

    WHY THIS IS PER HOST AND NOT PER PAGE
    =====================================
    A `429` is a statement about the server, not about the URL that happened to
    receive it. One pilot host serves eleven pages; honouring the throttle on the one
    page we asked for and then immediately asking for the other ten is not honouring
    it at all -- it is the behaviour that turns a temporary throttle into a permanent
    block, which is exactly what this step exists to stop happening.

    WHY IT IS A TABLE AND NOT IN-PROCESS STATE
    ==========================================
    `HostGate` already pauses a host inside one cycle, and that is not enough: the
    next cycle is a new process with an empty gate, so a pause set at 18:05 would be
    forgotten by the run at 18:10. A cooldown a worker forgets is not a cooldown.

    Keyed by hostname alone. There is no version history and no audit row -- a pause
    is an obligation we took on, not a decision about an institution, and expiring
    rows are discarded freely (§18).
    """

    __tablename__ = "host_cooldown"

    host: Mapped[str] = mapped_column(Text, primary_key=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Which page provoked it, for the operator who asks why a host is quiet. Nullable
    #: and ON DELETE SET NULL: the host is still owed its pause if that source goes.
    triggered_by_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # Lowercased at the boundary so a lookup cannot miss a pause on a case
        # difference -- `WWW.Example.AC.UK` and `www.example.ac.uk` are one host.
        CheckConstraint("host = lower(host)", name="host_is_lowercase"),
        CheckConstraint("length(host) > 0", name="host_not_empty"),
        Index("ix_host_cooldown_until", "cooldown_until"),
    )


class FetchAttempt(TimestampedMixin, Base):
    """Queued and in-flight fetch work. MUTABLE WORKING STATE.

    Separated from `fetch_run` because the two have opposite natures. Celery needs a
    row it can claim, heartbeat and re-lease; the historical record must never
    change. One table for both meant either mutating immutable history or having no
    representation of "running" at all.

    Lifecycle::

        QUEUED --claim--> RUNNING --terminate--> FINALIZED  (+ one fetch_run row)
                              |
                              +--lease expiry-> ABANDONED  (+ one fetch_run row,
                                                            status ABANDONED)

    A crashed worker stops renewing `lease_expires_at`. The sweeper finds the row via
    the partial index below, writes an immutable `fetch_run` recording the
    abandonment, and marks this row ABANDONED. A crash therefore becomes a permanent,
    queryable fact rather than a row silently stuck in RUNNING.

    Retries are new attempts: `attempt_no` increments and a fresh row is created, so
    every try keeps its own outcome.

    LEASE FENCING (Step 5B, deferred from Step 3.5)
    ===============================================
    A lease that expires is not enough on its own. The classic failure: worker A is
    mid-fetch when its process stalls past `lease_expires_at`; the sweeper marks the
    attempt ABANDONED; worker B claims a fresh attempt; worker A wakes up and
    finalises -- writing an authoritative `fetch_run` and a snapshot for work whose
    ownership it lost. Nothing about the timestamps prevents that, because A's clock
    and the database's disagree by exactly the amount that caused the problem.

    `lease_token` is the fence. Every successful claim mints a new UUID, and every
    heartbeat and finalisation is a conditional write requiring
    `state = 'RUNNING' AND lease_token = <the token I was given>`. A worker that lost
    its lease updates zero rows and is told so, rather than succeeding late. The
    token is the *only* thing that grants the right to write; the timestamp merely
    decides when someone else may take it.
    """

    __tablename__ = "fetch_attempt"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="1 for the first try of a cycle"
    )
    cycle_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Groups retries of one logical check, e.g. 2027-01-15T06 for the 06:00 sweep",
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="QUEUED")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(128), comment="Worker identity")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Past this the worker is presumed dead"
    )
    #: The fencing token. A fresh UUID per successful claim; heartbeat and
    #: finalisation are conditional on matching it, so a worker that lost its lease
    #: cannot write late. See the class docstring.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="Fencing token; minted per claim, never reused"
    )
    #: How many times this row has been claimed. Monotonic, so "was it re-leased?" is
    #: answerable from the row itself rather than inferred from timestamps.
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'FINALIZED', 'ABANDONED')", name="state_known"
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_is_positive"),
        # A claimed attempt must say who holds it and until when, or a crash is
        # undetectable.
        CheckConstraint(
            "state <> 'RUNNING' OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND lease_token IS NOT NULL)",
            name="running_attempt_is_leased",
        ),
        CheckConstraint("lease_generation >= 0", name="lease_generation_non_negative"),
        # A QUEUED row holds no lease, so a token left behind by an expired claim
        # cannot be replayed against a row that was requeued.
        CheckConstraint(
            "state <> 'QUEUED' OR lease_token IS NULL", name="a_queued_attempt_holds_no_token"
        ),
        CheckConstraint(
            "state NOT IN ('FINALIZED', 'ABANDONED') OR finalized_at IS NOT NULL",
            name="terminal_state_has_a_timestamp",
        ),
        UniqueConstraint(
            "source_id", "cycle_key", "attempt_no", name="uq_fetch_attempt_source_cycle_attempt"
        ),
        # The stuck-work query. Partial, because healthy attempts are the
        # overwhelming majority and this index only needs to find the sick ones.
        Index(
            "ix_fetch_attempt_expired_lease",
            "lease_expires_at",
            postgresql_where="state = 'RUNNING'",
        ),
        Index("ix_fetch_attempt_queued", "scheduled_for", postgresql_where="state = 'QUEUED'"),
        {"comment": "MUTABLE working state: queued and in-flight fetches, leased."},
    )


class FetchRun(RecordedAtMixin, Base):
    """One **completed** fetch attempt. APPEND-ONLY.

    Written exactly once, when an attempt reaches a terminal state, including
    ABANDONED for a crashed worker. Every attempt is recorded, UNCHANGED ones
    included, because "we checked today and nothing had changed" is itself a fact the
    PRD requires us to be able to show.

    A snapshot always references a completed run: the terminal write is a single
    transaction containing this row and any snapshots it produced.

    304 NOT MODIFIED (Step 5B)
    ==========================
    A conditional request answered `304` produces a run with status `UNCHANGED` and
    **no snapshot**, because no bytes were received and a snapshot means "we saw
    these bytes". `unchanged_content_hash` records which blob the server confirmed is
    still current, so the run is not a dead end.

    This is deliberately different from a `200` that happens to return identical
    bytes: that one *did* transfer a body, so it produces a real snapshot sharing the
    existing blob. The two are different transport observations and the record says
    which happened -- `http_status` 304 against 200, and a snapshot against none.
    Collapsing them would lose the ability to tell "the server told us nothing
    changed" from "we downloaded it again and compared".
    """

    __tablename__ = "fetch_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fetch_attempt.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The working-state row this run concluded",
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[FetchStatus] = mapped_column(FETCH_STATUS, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetcher: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(128))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    worker_name: Mapped[str | None] = mapped_column(String(128))
    #: 304 only: the bytes the server confirmed are still current. No snapshot is
    #: written, because none were transferred.
    unchanged_content_hash: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("content_blob.content_hash", ondelete="RESTRICT"),
        comment="304 only: the blob the server said is still current",
    )
    #: Whether we sent If-None-Match / If-Modified-Since. A 200 with a conditional
    #: request sent means the server chose to re-send; without one it had no choice.
    conditional_request_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: Bytes actually transferred, so a 304's saving is visible in the record.
    bytes_downloaded: Mapped[int | None] = mapped_column(BigInteger)
    #: Where the request actually ended up, and every hop it took. Recorded here as
    #: well as on `snapshot` because a fetch that redirects twice and *then* times out
    #: produces no snapshot -- and the chain is exactly what an operator needs to
    #: diagnose it. Found in the Step 5B.1 smoke run: UBC's calendar redirects four
    #: times and the connect timed out on a later hop, and the trail was lost.
    effective_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB(none_as_null=True))

    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_is_positive"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
        ),
        # A failure must say what failed, or the source-health dashboard cannot
        # explain itself.
        CheckConstraint(
            "status IN ('OK', 'UNCHANGED') OR error_class IS NOT NULL",
            name="a_failure_names_its_error",
        ),
        # Only a 304 may claim unchanged content, and only against a real blob. A
        # failed fetch asserting "nothing changed" would silently extend the life of
        # evidence nobody re-checked.
        CheckConstraint(
            "unchanged_content_hash IS NULL " "OR (status = 'UNCHANGED' AND http_status = 304)",
            name="only_a_304_confirms_unchanged_content",
        ),
        CheckConstraint(
            "bytes_downloaded IS NULL OR bytes_downloaded >= 0", name="bytes_non_negative"
        ),
        # One completed run per attempt. This is what keeps retries separate: a new
        # try is a new attempt row, so it gets its own run.
        UniqueConstraint("attempt_id", name="uq_fetch_run_attempt_id"),
        Index("ix_fetch_run_source_id_started_at", "source_id", "started_at"),
        {"comment": "APPEND-ONLY. One row per COMPLETED attempt, abandonments included."},
    )


class ContentBlob(Base):
    """CONTENT IDENTITY: one row per distinct sequence of bytes. APPEND-ONLY.

    Content-addressed by sha256. `storage_key` points into object storage; the bytes
    themselves never live in PostgreSQL, because thousands of multi-megabyte
    documents would bloat every backup for no benefit.

    Deduplication lives **here and only here**. The same bytes fetched from two URLs,
    or from one URL on two dates, resolve to this single blob, while each fetch stays
    a separate `snapshot` observation. That separation is the whole point: dedup must
    save storage without erasing the record of having looked.

    The application writes an object only when the key is absent, so the evidence
    behind a published field can never change underneath it (C12).
    """

    __tablename__ = "content_blob"

    content_hash: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="sha256 hex of the bytes; this is the identity"
    )
    storage_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Object-storage key; bodies never live in PostgreSQL"
    )
    content_type: Mapped[str | None] = mapped_column(String(128))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    rendered_text_key: Mapped[str | None] = mapped_column(Text)
    screenshot_key: Mapped[str | None] = mapped_column(Text)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When these bytes were first seen"
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_is_sha256_hex"),
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="byte_size_non_negative"),
        CheckConstraint("length(btrim(storage_key)) > 0", name="storage_key_is_not_blank"),
        # No `last_observed_at`: that would be a mutable column on append-only
        # content. Observation dates belong to `snapshot`.
        {"comment": "APPEND-ONLY content identity. The dedup boundary; one row per sha256."},
    )


class Snapshot(RecordedAtMixin, Base):
    """OBSERVATION: one row per act of fetching something. APPEND-ONLY.

    Distinct from content identity, which lives in `content_blob`. A snapshot records
    *that we looked*: from which source, through which fetch attempt, at what time,
    at which URL, and what the server said.

    Several snapshots legitimately share one blob:

    * the same source re-fetched on a later date, bytes unchanged;
    * two different official URLs serving identical content;
    * a redirect chain ending at the same document;
    * a separate verification fetch during review.

    Each of those must remain independently traceable, so there is deliberately
    **no** unique constraint on `(source_id, content_hash)`. An earlier version had
    one, and it silently discarded the second observation, destroying exactly the
    "we checked on this date" record the PRD requires.
    """

    __tablename__ = "snapshot"

    id: Mapped[uuid.UUID] = uuid_pk()
    fetch_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fetch_run.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("content_blob.content_hash", ondelete="RESTRICT"),
        nullable=False,
        comment="The bytes this observation saw; many observations may share one blob",
    )
    requested_url: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The URL actually requested"
    )
    effective_url: Mapped[str | None] = mapped_column(
        Text, comment="Final URL after redirects, when it differs from the request"
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetcher: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="STATIC / BROWSER / DOCUMENT / MANUAL"
    )
    response_headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    #: Pulled out of the headers because the next conditional request needs them by
    #: lookup, not by JSON traversal, and because a validator disappearing between
    #: fetches is itself worth seeing.
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(
        String(128), comment="Verbatim HTTP-date string; never reinterpreted (D17)"
    )
    content_type: Mapped[str | None] = mapped_column(String(256))
    #: Every hop, in order: `[{"status": 301, "url": "..."}, ...]`. Kept because a
    #: page that redirects off the institution's host is exactly what a source
    #: reviewer needs to see, and because "which host actually served this?" must be
    #: answerable from the record rather than re-derived by fetching again.
    redirect_chain: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB(none_as_null=True))
    #: Operational only: `<title>` and declared charset. Explicitly NOT extraction --
    #: no programme, fee, deadline or requirement is read from a page in this phase.
    technical_metadata: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
        ),
        CheckConstraint(
            "fetcher IN ('STATIC', 'BROWSER', 'DOCUMENT', 'MANUAL')", name="fetcher_known"
        ),
        # Deliberately NOT unique on (source_id, content_hash). See the docstring.
        Index("ix_snapshot_content_hash", "content_hash"),
        # "What did we last see for this source?" -- the conditional-request lookup,
        # run once per page per cycle.
        Index("ix_snapshot_source_latest", "source_id", "observed_at", "id"),
        Index("ix_snapshot_fetch_run_id", "fetch_run_id"),
        Index("ix_snapshot_source_id_observed_at", "source_id", "observed_at"),
        {"comment": "APPEND-ONLY observation. Many observations may share one blob."},
    )


class Extraction(RecordedAtMixin, Base):
    """One deterministic extraction result per (snapshot, extractor, version).

    APPEND-ONLY. `extractor_version` is recorded because a value that changed only
    because the extractor changed is an extractor-drift signal, not a source change
    (D4) -- and because Step 5C.1 makes the result a pure function of (input bytes,
    version), which is what allows re-running to be a no-op.

    The normalised document payload lives in the object store under `document_hash`;
    this row holds the metadata and the lineage. **Extraction confers no trust**: a
    document derived from a `NOT_ELIGIBLE` source stays `NOT_ELIGIBLE` (C27).
    """

    __tablename__ = "extraction"

    id: Mapped[uuid.UUID] = uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshot.id", ondelete="RESTRICT"), nullable=False
    )
    extractor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[ExtractionStatus] = mapped_column(EXTRACTION_STATUS, nullable=False)
    output: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    selector_trace: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    error_detail: Mapped[str | None] = mapped_column(Text)

    # --- Step 5C.1: the derived document lives in the object store ----------------
    #
    # `output` is jsonb and could hold a normalised document, but for 174 real pages
    # the payloads run to tens of megabytes of data that is *reproducible from bytes
    # we already store*. Putting it in PostgreSQL means every backup and every
    # replica carries it. The row keeps the metadata and the pointer.
    #: sha256 of the input bytes. Denormalised from the snapshot so that "same bytes,
    #: same extractor version" is answerable without a join.
    input_content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="sha256 of the input bytes, denormalised from the snapshot so "
        "'same bytes, same extractor version' is answerable without a join",
    )
    #: sha256 of the canonical derived document. A **pure function of (input bytes,
    #: extractor version)**: no ids, no timestamps, so identical bytes from two
    #: sources produce one artifact and determinism is checkable by comparing hashes.
    document_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="sha256 of the canonical derived document; a pure function of "
        "(input bytes, extractor version) and free of ids and timestamps",
    )
    document_storage_key: Mapped[str | None] = mapped_column(Text)
    document_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    #: Why a PARTIAL is partial -- a malformed JSON-LD block, a charset fallback.
    #: Distinct from `error_detail`, which explains a FAILED.
    warnings: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        comment="Why a PARTIAL is partial: a malformed JSON-LD block, a charset "
        "fallback. Distinct from error_detail, which explains a FAILED.",
    )

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"
        ),
        CheckConstraint(
            "document_hash IS NULL OR document_hash ~ '^[0-9a-f]{64}$'",
            name="document_hash_is_sha256_hex",
        ),
        CheckConstraint(
            "input_content_hash IS NULL OR input_content_hash ~ '^[0-9a-f]{64}$'",
            name="input_content_hash_is_sha256_hex",
        ),
        # A stored document names where it is and how big it is, or it is not stored:
        # the dangling-reference failure C12 prevents, one plane further down.
        CheckConstraint(
            "(document_hash IS NULL AND document_storage_key IS NULL "
            "  AND document_byte_size IS NULL) "
            "OR (document_hash IS NOT NULL AND document_storage_key IS NOT NULL "
            "  AND document_byte_size IS NOT NULL AND document_byte_size >= 0)",
            name="document_is_completely_described",
        ),
        # One result per (snapshot, extractor, version). The result is a pure function
        # of its inputs, so a second row could only be a duplicate; a different result
        # needs a new version, and the old row is retained because this table is
        # append-only.
        # The uniqueness is a **partial** index, not a constraint, and lives in the
        # migration because autogenerate does not round-trip a WHERE clause: one
        # successful-or-partial result per (snapshot, extractor, version), with FAILED
        # rows excluded so a transient environmental failure can be retried without
        # pretending the extraction logic changed (Step 5C.2 section 0). See
        # `uq_extraction_result_per_version` and `MIGRATION_OWNED_INDEXES`.
        Index("ix_extraction_document_hash", "document_hash"),
        Index("ix_extraction_snapshot_id", "snapshot_id"),
        Index(
            "ix_extraction_extractor_name_extractor_version",
            "extractor_name",
            "extractor_version",
        ),
        {
            "comment": (
                "One deterministic extraction result per (snapshot, extractor, "
                "version). Append-only. The normalised document payload lives in "
                "the object store under document_hash; this row holds the "
                "metadata and the lineage. Extraction confers no trust: a "
                "document derived from a NOT_ELIGIBLE source stays NOT_ELIGIBLE "
                "(C27)."
            )
        },
    )


class FieldClaim(RecordedAtMixin, Base):
    """An assertion about one field, extracted from one snapshot. APPEND-ONLY.

    `char_offset_start/end` point into the snapshot's rendered text, which is what
    lets the review UI highlight the exact sentence a reviewer is approving instead
    of making them hunt through the page.

    `entity_id` is the extractor's *proposed* target and may be null or wrong; the
    authoritative binding is a `claim_resolution` row.
    """

    __tablename__ = "field_claim"

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="Proposed target; authoritative binding is claim_resolution"
    )
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    proposed_field_status: Mapped[FieldStatus] = mapped_column(FIELD_STATUS, nullable=False)
    value_normalized: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    value_raw_text: Mapped[str | None] = mapped_column(Text)
    char_offset_start: Mapped[int | None] = mapped_column(Integer)
    char_offset_end: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    __table_args__ = (
        CheckConstraint(
            "(char_offset_start IS NULL) = (char_offset_end IS NULL)",
            name="offsets_come_as_a_pair",
        ),
        CheckConstraint(
            "char_offset_start IS NULL OR char_offset_end >= char_offset_start",
            name="offsets_are_ordered",
        ),
        CheckConstraint(
            "char_offset_start IS NULL OR char_offset_start >= 0", name="offsets_non_negative"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="effectivity_is_ordered",
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"
        ),
        # A claim asserting a value must carry one; an absence claim must not.
        CheckConstraint(
            "proposed_field_status = 'PUBLISHED' OR value_normalized IS NULL",
            name="absence_claim_has_no_value",
        ),
        Index("ix_field_claim_extraction_id", "extraction_id"),
        Index(
            "ix_field_claim_entity_type_entity_id_field_path",
            "entity_type",
            "entity_id",
            "field_path",
        ),
        {"comment": "APPEND-ONLY. Extracted assertion with offsets into the snapshot."},
    )


class FieldClaimCandidate(RecordedAtMixin, Base):
    """One candidate fact an extractor found in one exact region of one document.

    APPEND-ONLY. It asserts exactly this much: *"this extractor, at this version,
    found this candidate in this evidence."* It does **not** assert that the fact is
    verified, canonical, publishable or conflict-free.

    WHY THIS IS NOT `field_claim`
    =============================
    `field_claim` is the publication claim plane, and C27's
    `field_claim_requires_eligible_evidence` trigger refuses any row whose source is
    not `OFFICIAL_VERIFIED` / `AUTHORIZED_EXTERNAL` / `AUTHORIZED_RANKING`. Every
    pilot source is `NOT_ELIGIBLE`, so a pilot `field_claim` is rejected by the
    database -- which is C27 working, not a defect.

    What this step produces is earlier and weaker than a publication claim, and true
    of a page nobody has verified. It is the same distinction Step 5B drew between
    `FETCHABLE` and `PUBLICATION_ELIGIBLE` (D33), one plane up: **finding a value is
    how you learn what a page says; it is not permission to publish it.** Collapsing
    the two would mean weakening C27 or faking verification.

    Promotion into `field_claim` is a later step, and it will need the eligibility
    C27 asks for.

    TWO REPRESENTATIONS, ALWAYS
    ===========================
    `value_normalized` carries the structured candidate as far as a deterministic rule
    can take it -- and `NULL` is legitimate, with `unresolved_reason` saying why, for an
    ambiguous award or an unresolvable applicant scope. `evidence_text` carries the
    official wording that supported it. A reviewer needs both: the number to check, and
    the sentence to check it against.
    """

    __tablename__ = "field_claim_candidate"

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False
    )
    #: Which responsibility authorised this extractor to run over this page. A page
    #: claimed only as `UNIVERSITY_HOME` must not emit tuition, and recording the
    #: authorising claim makes that auditable rather than a property of unseen code.
    pilot_collected_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pilot_collected_source.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_responsibility: Mapped[str] = mapped_column(String(64), nullable=False)
    field_kind: Mapped[str] = mapped_column(String(48), nullable=False)

    value_normalized: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    unresolved_reason: Mapped[str | None] = mapped_column(
        Text,
        comment="Why value_normalized is NULL or incomplete: SCOPE_MAPPING_REQUIRED, "
        "DEGREE_LEVEL_AMBIGUOUS, BILLING_UNIT_UNRESOLVED, CURRENCY_ABSENT, and so on",
    )
    value_raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)

    #: Where in the normalised document this came from. JSONB because the shape differs
    #: by document kind -- block index and heading path for HTML, page and block for
    #: PDF, table/row/column for a cell -- and a column per variant would be mostly
    #: NULL.
    locator: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    extractor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)

    #: An interpretable band with a stated reason, not a fabricated decimal. A
    #: calibrated probability would need a calibration set, which does not exist.
    confidence_band: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence_reason: Mapped[str] = mapped_column(Text, nullable=False)

    #: Stable identity so a repeat pass inserts nothing. Deliberately **not** derived
    #: from the value: two official pages stating the same fee are two pieces of
    #: evidence and must remain two claims.
    claim_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("claim_fingerprint"),
        CheckConstraint(
            "field_kind IN (" + ", ".join(f"'{k}'" for k in FIELD_KINDS_SQL) + ")",
            name="field_kind_known",
        ),
        CheckConstraint(
            "confidence_band IN ('HIGH', 'MEDIUM', 'LOW')", name="confidence_band_known"
        ),
        # A claim whose evidence is blank cannot be reviewed, so it is not a claim.
        CheckConstraint("btrim(evidence_text) <> ''", name="evidence_is_not_blank"),
        CheckConstraint("btrim(value_raw_text) <> ''", name="raw_text_is_not_blank"),
        CheckConstraint("btrim(confidence_reason) <> ''", name="confidence_is_explained"),
        # A locator of `{}` is no locator, which is what using `extraction_id` alone
        # would amount to with extra steps.
        CheckConstraint(
            "locator <> '{}'::jsonb AND jsonb_typeof(locator) = 'object'",
            name="locator_is_present",
        ),
        CheckConstraint(
            "value_normalized IS NOT NULL OR unresolved_reason IS NOT NULL",
            name="unresolved_says_why",
        ),
        CheckConstraint("claim_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_is_sha256_hex"),
        Index("ix_field_claim_candidate_extraction_id", "extraction_id"),
        Index("ix_field_claim_candidate_field_kind", "field_kind"),
        Index("ix_field_claim_candidate_extractor", "extractor_name", "extractor_version"),
        {
            "comment": (
                "APPEND-ONLY. One candidate fact an extractor found in one exact "
                "region of one normalised document. It asserts only that: not "
                "verified, not canonical, not publishable, not conflict-free. "
                "Distinct from field_claim, which C27 gates on earned publication "
                "eligibility -- finding a value is how you learn what a page says, "
                "not permission to publish it (Step 5C.2)."
            )
        },
    )


class ClaimResolution(RecordedAtMixin, Base):
    """The authoritative binding of a claim to a catalog entity. APPEND-ONLY.

    This replaces what was a mutable `field_claim.resolution_status` column: the
    claim itself is immutable evidence, so its interpretation is recorded separately
    (C1).
    """

    __tablename__ = "claim_resolution"

    id: Mapped[uuid.UUID] = uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_claim.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    method: Mapped[str] = mapped_column(String(48), nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "method IN ('OFFICIAL_CODE', 'URL_IDENTITY', 'FUZZY_MATCH', 'MANUAL')",
            name="method_known",
        ),
        # A human resolution must name the human who made it.
        CheckConstraint(
            "method <> 'MANUAL' OR resolved_by IS NOT NULL", name="manual_names_the_resolver"
        ),
        UniqueConstraint("claim_id", name="uq_claim_resolution_claim_id"),
        Index("ix_claim_resolution_entity_type_entity_id", "entity_type", "entity_id"),
        {"comment": "APPEND-ONLY. Authoritative claim to entity binding."},
    )


class ResolutionCandidate(TimestampedMixin, Base):
    """Mutable queue of claims a machine could not confidently resolve.

    Working state, not history: an item is claimed, worked and closed. Identity is
    never guessed, which is the whole reason this queue exists.
    """

    __tablename__ = "resolution_candidate"

    id: Mapped[uuid.UUID] = uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_claim.id", ondelete="CASCADE"), nullable=False
    )
    suggested_entity_type: Mapped[str | None] = mapped_column(String(64))
    suggested_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('OPEN', 'ASSIGNED', 'RESOLVED', 'DISCARDED')", name="state_known"
        ),
        CheckConstraint(
            "match_score IS NULL OR match_score BETWEEN 0 AND 1", name="match_score_range"
        ),
        UniqueConstraint("claim_id", name="uq_resolution_candidate_claim_id"),
        {"comment": "MUTABLE working queue for unresolved claims."},
    )


__all__ = [
    "ClaimResolution",
    "ContentBlob",
    "Extraction",
    "FetchAttempt",
    "FetchRun",
    "FieldClaim",
    "FieldClaimCandidate",
    "HostCooldown",
    "ResolutionCandidate",
    "Snapshot",
    "Source",
    "SourceAuthorization",
    "SourceFieldBinding",
]


class FieldClaimCandidateReview(RecordedAtMixin, Base):
    """One human decision about one candidate claim. APPEND-ONLY.

    WHY NOT `review_decision`
    =========================
    That table is the *proposal* review plane and is bolted to it by NOT NULL foreign
    keys: `review_task.proposal_id` -> `change_proposal.id`. Reviewing a candidate
    through it would require creating a `change_proposal`, which Step 5C.3 forbids. Its
    vocabulary does not fit either -- `review_decision_kind` is
    `APPROVE | RETURN | CORRECT`, which is what you do to a proposed *change*, and it
    cannot express `REJECTED` or `NEEDS_SCOPE_MAPPING`.

    SEVERAL ROWS PER CANDIDATE ARE CORRECT
    ======================================
    A reviewer may revisit a decision. The earlier one is history, not an error, and the
    table is append-only so it survives. `candidate_review_state` projects the latest.

    WHAT A DECISION IS NOT
    ======================
    It is not permission to publish. Accepting a candidate says the extraction is right
    about what the page says; whether that page may support a published fact is the
    source's `publication_eligibility`, which no reviewer of candidates can change
    (section 30).
    """

    __tablename__ = "field_claim_candidate_review"

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_claim_candidate.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: NOT NULL: an anonymous decision is not auditable, and being answerable for it is
    #: the reason the row exists.
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When the human decided, distinct from when it was written down (D3).
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision IN (" + ", ".join(f"'{name}'" for name in CANDIDATE_DECISIONS_SQL) + ")",
            name="ck_field_claim_candidate_review_decision_known",
        ),
        CheckConstraint(
            "reason_code IN ("
            + ", ".join(f"'{name}'" for name in CANDIDATE_REASON_CODES_SQL)
            + ")",
            name="ck_field_claim_candidate_review_reason_code_known",
        ),
        CheckConstraint(
            "reason_code <> 'OTHER' OR btrim(coalesce(reason_text, '')) <> ''",
            name="ck_field_claim_candidate_review_other_reason_is_explained",
        ),
        CheckConstraint(
            "reason_text IS NULL OR btrim(reason_text) <> ''",
            name="ck_field_claim_candidate_review_reason_text_is_not_blank",
        ),
        CheckConstraint(
            "decided_at <= recorded_at",
            name="ck_field_claim_candidate_review_decided_before_recorded",
        ),
        Index(
            "ix_field_claim_candidate_review_candidate",
            "candidate_id",
            text("decided_at DESC"),
        ),
        Index("ix_field_claim_candidate_review_actor", "actor_id", "decided_at"),
        {
            "comment": (
                "APPEND-ONLY log of human decisions about candidate claims. Several "
                "rows per candidate are expected and correct: a reviewer may revisit a "
                "decision, and the earlier one is history rather than an error. The "
                "current state is the latest row, projected by the "
                "candidate_review_state view. Accepting a candidate does NOT make it "
                "publishable: promotion additionally requires the source to be eligible "
                "for that field, which is a separate question (Step 5C.3 section 30)."
            )
        },
    )


class ClaimRuleVersion(Base):
    """Which version of each claim extractor is currently live. MUTABLE.

    WHY THIS IS NOT A CANDIDATE TABLE
    =================================
    It holds no claim, no evidence and no judgement -- one row per extractor saying
    which version of it is live, which is the only thing SQL could not work out for
    itself. `candidate_review_state` joins it to tell a current candidate from a
    superseded one.

    WHY IT IS NOT A CONSTANT
    ========================
    Because it was one, and that was wrong within four rule corrections. Revision
    `e0f1a2b3c4d5` wrote the pairs into the view as a literal; by the time the heading
    path, the round label, the year-range-as-day and the postcode-as-score were all
    fixed, the view reported **every current candidate as superseded** -- silently, with
    a total that happened to look plausible. A frozen copy of something that changes is
    wrong by construction, and the cost here is a review queue built from history.

    The claim runner rewrites this from its own registry on every pass, so the code
    stays the source of truth and publishes what it knows.

    WHY MUTABLE
    ===========
    "Which version is current" has one answer at a time. The history of which versions
    have ever existed is already in `field_claim_candidate.extractor_version`, where it
    is append-only and cannot be lost.
    """

    __tablename__ = "claim_rule_version"

    extractor_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(48), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        {
            "comment": (
                "MUTABLE. One row per extractor naming the version that is currently "
                "live, so SQL can tell a current candidate from a superseded one "
                "without a migration every time a rule is corrected. Written by the "
                "claim runner from its own registry; the history of which versions "
                "ever existed lives in field_claim_candidate.extractor_version and is "
                "never touched here."
            )
        },
    )


class DocumentArtifactVersion(Base):
    """Which parse of a page is currently live, per normaliser. MUTABLE.

    THE SECOND AXIS
    ===============
    A candidate can be stale two ways: its rule was corrected, or the page was
    re-parsed. `ClaimRuleVersion` answers the first. This answers the second, and the
    two stay apart because they are different questions -- D39.

    It is load-bearing rather than tidy. Re-extraction keeps the previous artifact
    (Step 5C.4 section 1), so once the normaliser went to 2.0.0 every snapshot had two
    extractions. Without this table the claims read from the superseded parse would
    still count as current, and since `extraction_id` is part of the claim fingerprint
    nothing would collapse them: every count in every report would simply double.

    WHY NOT BUMP THE RULE VERSIONS INSTEAD
    ======================================
    Because four of the six rules were measured to produce byte-identical statements
    across the change, and section 8 forbids bumping a rule that did not change. Doing
    it anyway would mark 472 correct claims superseded in order to express a fact about
    the parser.

    WHY NOT A LITERAL IN THE VIEW
    =============================
    That is the mistake `ClaimRuleVersion` exists to undo. See its docstring.
    """

    __tablename__ = "document_artifact_version"

    extractor_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(48), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        {
            "comment": (
                "MUTABLE. One row per document normaliser naming the artifact version "
                "that is currently live. Re-extraction keeps the previous artifact, so "
                "without this a candidate read from a superseded parse of a page would "
                "still count as current and every report would double. Written by the "
                "extraction runner from its own constants; the history of which "
                "versions existed lives in extraction and is never touched here."
            )
        },
    )


#: The human scope states. Mirrors migration b9c0d1e2f3a4 and `claims.precedence`.
SCOPE_STATES_SQL: tuple[str, ...] = (
    "UNSCOPED",
    "APPLICANT_JURISDICTION",
    "QUALIFICATION_SYSTEM",
    "JURISDICTION_AND_QUALIFICATION",
    "UNIVERSAL_EXPLICIT",
    "NOT_APPLICABLE",
)

#: What a reviewer may do with a conflict or duplicate group.
CONFLICT_ACTIONS_SQL: tuple[str, ...] = (
    "RESOLVED_DUPLICATE",
    "SELECTED_SUPPORTED_CLAIM",
    "REJECTED_CONFLICTING",
    "LEFT_UNRESOLVED",
)


class CandidateScopeResolution(RecordedAtMixin, Base):
    """One human applicant-scope decision about one candidate. APPEND-ONLY.

    WHY THIS IS NOT A COLUMN ON `field_claim_candidate_review`
    ==========================================================
    That table answers *is this candidate correctly extracted*. Scope is a different
    question with a different answer: a candidate can be extracted perfectly and still
    have no stated audience. Folding them together would make "accepted" ambiguous.

    UNSCOPED IS NEVER UNIVERSAL
    ===========================
    `ck_candidate_scope_resolution_unscoped_names_no_scope` is the schema refusing to let
    an unresolved row look resolved. A requirement written for A-level applicants must not
    become everyone's requirement because the page failed to say so.
    """

    __tablename__ = "candidate_scope_resolution"

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_claim_candidate.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    scope_state: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Null while the client taxonomy has nothing to point at. A state of
    #: APPLICANT_JURISDICTION with a null id is complete and honest: the reviewer said
    #: which dimension applies, and no scope row exists to name it yet.
    applicant_scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="RESTRICT")
    )
    #: The criteria the reviewer selected, per dimension. jsonb because this is the record
    #: of a decision, not a queryable taxonomy -- `applicant_scope_criterion` is where it
    #: lands once the taxonomy can hold it.
    selected_criteria: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope_state IN (" + ", ".join(f"'{name}'" for name in SCOPE_STATES_SQL) + ")",
            name="ck_candidate_scope_resolution_state_known",
        ),
        CheckConstraint(
            "btrim(reason) <> ''", name="ck_candidate_scope_resolution_reason_is_not_blank"
        ),
        CheckConstraint(
            "scope_state <> 'UNSCOPED' OR applicant_scope_id IS NULL",
            name="ck_candidate_scope_resolution_unscoped_names_no_scope",
        ),
        CheckConstraint(
            "decided_at <= recorded_at",
            name="ck_candidate_scope_resolution_decided_before_recorded",
        ),
        Index("ix_candidate_scope_resolution_candidate_id", "candidate_id"),
        {
            "comment": (
                "Human applicant-scope decision for one candidate. Append-only; the "
                "latest row by decided_at is current. UNSCOPED is never inferred as "
                "universal."
            )
        },
    )


class CandidateConflictResolution(RecordedAtMixin, Base):
    """One human resolution of one agreement/conflict group. APPEND-ONLY.

    KEYED BY A FINGERPRINT, NOT A GROUP ID
    ======================================
    `claims.grouping` computes groups on read and stores nothing, deliberately: a stored
    group is a cache with no invalidation. A *resolution* has to outlive the request, so
    it is keyed on the SHA-256 of the deterministic context key. If the context changes
    the fingerprint changes and the old resolution matches nothing -- which is correct, as
    a decision about one question must not transfer to a different one.

    NO WINNER IS EVER IMPLIED
    =========================
    There is no "highest confidence" action. Confidence describes extraction quality, not
    truth, and `LEFT_UNRESOLVED` is a first-class outcome so that "somebody looked and
    could not tell" is distinguishable from "nobody has looked".
    """

    __tablename__ = "candidate_conflict_resolution"

    id: Mapped[uuid.UUID] = uuid_pk()
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    institution: Mapped[str] = mapped_column(String(256), nullable=False)
    field_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    selected_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_claim_candidate.id", ondelete="RESTRICT")
    )
    #: The members the decision was made over, so a later reader can tell whether the
    #: group has since gained or lost candidates.
    member_candidate_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    verdict_at_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{name}'" for name in CONFLICT_ACTIONS_SQL) + ")",
            name="ck_candidate_conflict_resolution_action_known",
        ),
        # A selection must name what was selected, and the actions that are not a
        # selection must not name one. Without this, SELECTED_SUPPORTED_CLAIM with a null
        # id would be a resolution that resolved nothing.
        CheckConstraint(
            "(action = 'SELECTED_SUPPORTED_CLAIM') = (selected_candidate_id IS NOT NULL)",
            name="ck_candidate_conflict_resolution_selection_names_a_candidate",
        ),
        CheckConstraint(
            "btrim(reason) <> ''", name="ck_candidate_conflict_resolution_reason_is_not_blank"
        ),
        CheckConstraint(
            "decided_at <= recorded_at",
            name="ck_candidate_conflict_resolution_decided_before_recorded",
        ),
        Index("ix_candidate_conflict_resolution_fingerprint", "context_fingerprint"),
        {
            "comment": (
                "Human resolution of one agreement/conflict group, keyed by the SHA-256 "
                "of its deterministic context key. Append-only; latest by decided_at is "
                "current."
            )
        },
    )
