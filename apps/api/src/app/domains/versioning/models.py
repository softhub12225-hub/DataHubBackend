"""History, provenance, audit, and the current-state projections.

This module holds the two halves of the canonical plane, and the distinction between
them is the single most important thing in the schema (architecture C1 / ADR 0004):

**IMMUTABLE HISTORY** — `entity_version`, `field_provenance`, `audit_log`,
`change_event`. Insert only. `UPDATE` and `DELETE` are revoked from every application
role and blocked by trigger. There is no `superseded_at` column anywhere: field-level
supersession is expressed by *version chronology* — the current provenance row for a
field is the one with the greatest `root_version_no`. Being older *is* being
superseded.

**MUTABLE PROJECTIONS** — `entity_head`, `field_current`. Recomputable pointers into
the history above. A projection may be overwritten; a history row may not.

**DELIVERY INFRASTRUCTURE** — `outbox_message`, deliberately mutable and explicitly
exempt from the immutability rule (C7). It changes on every retry and is pruned once
drained; losing it loses no business fact, because `change_event` holds the history.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.db.enums import (
    ACTOR_TYPE,
    CHANGE_KIND,
    FIELD_STATUS,
    OUTBOX_STATUS,
    RISK_LEVEL,
    ROOT_ENTITY_TYPE,
    TRUST_STATUS,
    ActorType,
    ChangeKind,
    FieldStatus,
    OutboxStatus,
    RiskLevel,
    RootEntityType,
    TrustStatus,
)
from app.db.mixins import TimestampedMixin, uuid_pk

# ---------------------------------------------------------------------------
# Immutable history
# ---------------------------------------------------------------------------


class EntityVersion(Base):
    """One published version of a versioned root. APPEND-ONLY.

    Only `university` and `program` are roots (D13). An offering, intake, deadline or
    fee versions as part of its program, because "what did this program look like on
    1 September" is the question a consultant actually asks, and answering it from a
    dozen independent version streams would be both slow and easy to get wrong.

    `state_snapshot` holds the full published state of the root at this version, so
    that historical reads are one indexed lookup rather than an event replay (D9).

    There is deliberately **no `is_current` flag**: that would be a mutable column on
    an immutable table. The current version is `entity_head.current_version_no`.
    """

    __tablename__ = "entity_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    root_type: Mapped[RootEntityType] = mapped_column(ROOT_ENTITY_TYPE, nullable=False)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diff_summary: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rollback_of_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_version.id", ondelete="RESTRICT")
    )
    correction_of_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_version.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        # Unique and monotonically increasing per root. Gaplessness is NOT required
        # (C3): allocation happens under a row lock inside the publication
        # transaction, and nothing downstream depends on there being no holes.
        UniqueConstraint(
            "root_type", "root_id", "version_no", name="uq_entity_version_root_version_no"
        ),
        CheckConstraint("version_no >= 1", name="version_no_is_positive"),
        CheckConstraint(
            "rollback_of_version_id IS NULL OR rollback_of_version_id <> id",
            name="not_own_rollback",
        ),
        Index(
            "ix_entity_version_root_type_root_id_version_no", "root_type", "root_id", "version_no"
        ),
        Index("ix_entity_version_published_at", "published_at"),
        {"comment": "APPEND-ONLY. Full published state per root version. No is_current."},
    )


class FieldProvenance(Base):
    """Why one published field has the value it has. APPEND-ONLY. The audit spine.

    Keyed by `(entity_type, entity_id, field_path, root_version_no)`. Supersession is
    chronology: the current row is the one with the greatest `root_version_no`.
    Nothing is ever marked superseded, because marking would mean updating history.

    `risk_level` is denormalised onto the row at insert. That is what allows the
    "high-risk facts must cite evidence and a reviewer" rule to be a genuine
    single-row CHECK rather than a hope.
    """

    __tablename__ = "field_provenance"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    root_type: Mapped[RootEntityType] = mapped_column(ROOT_ENTITY_TYPE, nullable=False)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    field_status: Mapped[FieldStatus] = mapped_column(FIELD_STATUS, nullable=False)
    value: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    risk_level: Mapped[RiskLevel] = mapped_column(RISK_LEVEL, nullable=False)
    trust_status: Mapped[TrustStatus] = mapped_column(
        TRUST_STATUS, nullable=False, server_default=TrustStatus.VERIFIED.value
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT")
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshot.id", ondelete="RESTRICT")
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_claim.id", ondelete="RESTRICT")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            "root_version_no",
            name="uq_field_provenance_entity_field_root_version",
        ),
        # A value accompanies a PUBLISHED status and is absent otherwise (D2).
        CheckConstraint(
            "(field_status = 'PUBLISHED') = (value IS NOT NULL)",
            name="value_matches_field_status",
        ),
        # The single-row form of "no high-risk field publishes without evidence and a
        # reviewer" (invariant I3).
        CheckConstraint(
            "risk_level <> 'HIGH' OR (snapshot_id IS NOT NULL AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="high_risk_requires_evidence_and_review",
        ),
        # C27. Every status except NOT_CHECKED asserts an observation, so it must cite
        # one. Before this, `app_publisher` could insert a PUBLISHED provenance row
        # with source_id, snapshot_id and claim_id all NULL -- verified against the
        # live database -- which made the whole eligibility concept bypassable by
        # simply citing nothing at all.
        #
        # NOT_CHECKED is the sole exemption, and correctly so: it is the one status
        # that means "nobody has looked", and requiring evidence for the absence of
        # an observation would be incoherent.
        CheckConstraint(
            "field_status = 'NOT_CHECKED' "
            "OR num_nonnulls(source_id, snapshot_id, claim_id) >= 1",
            name="asserted_status_cites_evidence",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="effectivity_is_ordered",
        ),
        CheckConstraint(
            "observed_at IS NULL OR observed_at <= published_at",
            name="observed_before_published",
        ),
        CheckConstraint("root_version_no >= 1", name="root_version_no_is_positive"),
        # Deliberately NOT an EXCLUDE constraint on effective periods: a correction
        # legitimately produces two rows covering the same period at different
        # versions, and an exclusion constraint would reject valid corrections.
        {"comment": "APPEND-ONLY audit spine. Supersession = version chronology (C1)."},
    )


class AuditChainHead(Base):
    """The serialisation point for audit appends. MUTABLE POINTER, single row.

    Every append locks this row (``SELECT ... FOR UPDATE``) before choosing its
    predecessor, which is what makes the chain deterministic under concurrency. See
    the audit-chain migration for the locking argument in full.

    Runtime roles hold **no** privileges here: the trigger that maintains it is
    ``SECURITY DEFINER``, so the counter and the head hash cannot be touched except
    by appending to ``audit_log``.
    """

    __tablename__ = "audit_chain_head"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default="true")
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_row_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Exactly one row, ever. A second chain head would mean two chains.
        CheckConstraint("singleton IS TRUE", name="exactly_one_row"),
        CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
        {"comment": "MUTABLE single-row pointer. The audit append serialisation point."},
    )


class AuditLog(Base):
    """Every consequential action. APPEND-ONLY, hash-chained.

    **Chain order is `seq`, not `(occurred_at, id)`.** Identity and ordering are
    separate concerns: a UUID primary key says nothing about sequence, and two rows
    written in one transaction share `occurred_at` exactly. Ordering by a random
    UUID to break that tie made the chain non-deterministic — the defect this
    replaces.

    `seq` is assigned from `audit_chain_head` under a row lock, so it is gapless,
    strictly increasing, and matches the hash chain exactly. `occurred_at` remains
    as informational wall-clock time and orders nothing.

    `prev_hash`/`row_hash` are computed server-side by trigger, so a caller cannot
    forge them; whatever the application supplies is discarded.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Chain position. Assigned by trigger under the head row lock.",
    )
    actor_type: Mapped[ActorType] = mapped_column(ACTOR_TYPE, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before_state: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str | None] = mapped_column(
        String(64), comment="Computed by trigger; never supplied by the caller"
    )

    __table_args__ = (
        # A human action must name the human.
        CheckConstraint(
            "actor_type <> 'USER' OR actor_id IS NOT NULL", name="user_action_names_the_user"
        ),
        # The chain's identity constraints. The head-row lock already prevents a
        # fork; these turn "cannot happen" into "cannot be stored", which is what
        # makes the guarantee survive a future change to the trigger.
        UniqueConstraint("seq", name="uq_audit_log_seq"),
        UniqueConstraint("prev_hash", name="uq_audit_log_prev_hash"),
        UniqueConstraint("row_hash", name="uq_audit_log_row_hash"),
        CheckConstraint("seq >= 1", name="seq_is_positive"),
        # Access paths follow the chain order, not wall-clock time.
        Index("ix_audit_log_object_type_object_id_seq", "object_type", "object_id", "seq"),
        Index("ix_audit_log_actor_id_seq", "actor_id", "seq"),
        {"comment": "APPEND-ONLY, hash-chained. Chain order is seq, never a timestamp."},
    )


class ChangeEvent(Base):
    """A published business change. APPEND-ONLY.

    The sole source of "Latest University Updates" (D12), read **directly** by the
    feed rather than through the outbox — so a stalled relay worker cannot make the
    feed stale or wrong.

    `destination_code` is denormalised because the feed is almost always filtered by
    destination, and joining four levels up to `university` for every row would be
    the dominant cost of the query.
    """

    __tablename__ = "change_event"

    id: Mapped[uuid.UUID] = uuid_pk()
    root_type: Mapped[RootEntityType] = mapped_column(ROOT_ENTITY_TYPE, nullable=False)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    field_path: Mapped[str | None] = mapped_column(String(200))
    change_kind: Mapped[ChangeKind] = mapped_column(CHANGE_KIND, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(RISK_LEVEL, nullable=False)
    destination_code: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("destination.code", ondelete="RESTRICT")
    )
    old_value: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    old_field_status: Mapped[FieldStatus | None] = mapped_column(FIELD_STATUS)
    new_field_status: Mapped[FieldStatus | None] = mapped_column(FIELD_STATUS)
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_version.id", ondelete="RESTRICT")
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provenance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_provenance.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("root_version_no >= 1", name="root_version_no_is_positive"),
        Index("ix_change_event_published_at", "published_at"),
        Index("ix_change_event_destination_code_published_at", "destination_code", "published_at"),
        Index("ix_change_event_root_type_root_id", "root_type", "root_id"),
        {"comment": "APPEND-ONLY business history. Powers Latest Updates (D12)."},
    )


# ---------------------------------------------------------------------------
# Mutable projections
# ---------------------------------------------------------------------------


class EntityHead(TimestampedMixin, Base):
    """Current version pointer per root. MUTABLE PROJECTION.

    Replaces what would otherwise be an `is_current` flag on immutable
    `entity_version` rows. Also the row the publication transaction locks
    (`SELECT ... FOR UPDATE`) to serialise version allocation (C3).
    """

    __tablename__ = "entity_head"

    root_type: Mapped[RootEntityType] = mapped_column(ROOT_ENTITY_TYPE, primary_key=True)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    current_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_version.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("current_version_no >= 1", name="current_version_no_is_positive"),
        {"comment": "MUTABLE PROJECTION. Current version per root; the publish lock row."},
    )


class FieldCurrent(TimestampedMixin, Base):
    """Current field status and provenance pointer. MUTABLE PROJECTION.

    Used for fields whose table does not carry an inline `*_field_status` column.
    High-risk narrow facts (deadlines, fees, lifecycle) keep their status inline so
    value/status agreement is a real single-row CHECK; everything else lives here
    (D14).
    """

    __tablename__ = "field_current"

    entity_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    field_path: Mapped[str] = mapped_column(String(200), primary_key=True)
    field_status: Mapped[FieldStatus] = mapped_column(FIELD_STATUS, nullable=False)
    provenance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_provenance.id", ondelete="RESTRICT"), nullable=False
    )
    root_type: Mapped[RootEntityType] = mapped_column(ROOT_ENTITY_TYPE, nullable=False)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_status: Mapped[TrustStatus] = mapped_column(TRUST_STATUS, nullable=False)

    __table_args__ = (
        CheckConstraint("root_version_no >= 1", name="root_version_no_is_positive"),
        Index("ix_field_current_root_type_root_id", "root_type", "root_id"),
        # Supports the QA sweep "which fields are not published?", which is the only
        # query that scans by status.
        Index(
            "ix_field_current_field_status",
            "field_status",
            postgresql_where="field_status <> 'PUBLISHED'",
        ),
        {"comment": "MUTABLE PROJECTION. Current status/provenance pointer per field."},
    )


# ---------------------------------------------------------------------------
# Delivery infrastructure — mutable by design (C7)
# ---------------------------------------------------------------------------


class OutboxMessage(TimestampedMixin, Base):
    """A side effect to deliver at least once. MUTABLE. Not history.

    Written in the publication transaction so no side effect can be lost, then
    drained by a relay worker with `FOR UPDATE SKIP LOCKED`. Explicitly exempt from
    the immutability rule: it changes on every retry and is pruned once delivered.

    Latest Updates does **not** come through here — that reads `change_event`
    directly (C7).
    """

    __tablename__ = "outbox_message"

    id: Mapped[uuid.UUID] = uuid_pk()
    topic: Mapped[str] = mapped_column(String(96), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    dedup_key: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True, comment="Makes at-least-once idempotent"
    )
    aggregate_type: Mapped[str | None] = mapped_column(String(64))
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    status: Mapped[OutboxStatus] = mapped_column(
        OUTBOX_STATUS, nullable=False, server_default=OutboxStatus.PENDING.value
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_is_positive"),
        CheckConstraint(
            "status <> 'DELIVERED' OR delivered_at IS NOT NULL",
            name="delivered_has_a_timestamp",
        ),
        # The relay's claim query: pending work, oldest first.
        Index(
            "ix_outbox_message_available_at",
            "available_at",
            postgresql_where="status = 'PENDING'",
        ),
        {"comment": "MUTABLE delivery infrastructure. Exempt from immutability (C7)."},
    )


__all__ = [
    "AuditChainHead",
    "AuditLog",
    "ChangeEvent",
    "EntityHead",
    "EntityVersion",
    "FieldCurrent",
    "FieldProvenance",
    "OutboxMessage",
]
