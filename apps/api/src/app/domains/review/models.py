"""Governance plane: proposals, review tasks, decisions, conflicts.

A deliberate split runs through this module:

* **Mutable working state** — `change_proposal`, `change_proposal_item`,
  `review_task`, `field_conflict`. A proposal legitimately moves through states, and
  an assignment legitimately changes hands.
* **Immutable record** — `review_decision`, `conflict_resolution`. A decision that
  can be edited is not a decision, and the audit requirement is precisely that the
  reviewer's judgement at the time is recoverable.

No workflow logic lives here. Whether a reviewer may approve a given item is
cross-row, cross-user authorisation that belongs to the policy layer and is
re-validated inside the publication transaction (C2).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import (
    DETECTION_TYPE,
    FIELD_STATUS,
    PROPOSAL_STATUS,
    REVIEW_DECISION_KIND,
    REVIEW_TASK_STATE,
    RISK_LEVEL,
    DetectionType,
    FieldStatus,
    ProposalStatus,
    ReviewDecisionKind,
    ReviewTaskState,
    RiskLevel,
)
from app.db.mixins import RecordedAtMixin, TimestampedMixin, uuid_pk


class ChangeProposal(TimestampedMixin, Base):
    """A proposed change set against one subject entity. MUTABLE working state.

    `sla_due_at` carries the PRD's review deadline: a change detected before 18:00
    Beijing time is due by 22:00 the same day. Computing it is the detector's job;
    storing it here is what makes the overdue queue a simple indexed query.
    """

    __tablename__ = "change_proposal"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Versioned root this change publishes under (D13)"
    )
    root_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    detection_type: Mapped[DetectionType] = mapped_column(DETECTION_TYPE, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(RISK_LEVEL, nullable=False)
    status: Mapped[ProposalStatus] = mapped_column(
        PROPOSAL_STATUS, nullable=False, server_default=ProposalStatus.DRAFT.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="NULL for system-generated proposals"
    )
    correction_of_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    discarded_reason: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[ChangeProposalItem]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[ReviewTask]] = relationship(back_populates="proposal")

    __table_args__ = (
        # A manual proposal must name its author; a system proposal must not pretend
        # to have one. This is the column the segregation-of-duties rule reads.
        CheckConstraint(
            "detection_type <> 'MANUAL_EDIT' OR created_by IS NOT NULL",
            name="manual_proposal_names_its_author",
        ),
        CheckConstraint(
            "detection_type <> 'CORRECTION' OR correction_of_version_id IS NOT NULL",
            name="correction_names_the_version_it_corrects",
        ),
        CheckConstraint(
            "status <> 'DISCARDED' OR discarded_reason IS NOT NULL",
            name="discard_has_a_reason",
        ),
        CheckConstraint(
            "status <> 'PUBLISHED' OR published_version_id IS NOT NULL",
            name="published_names_its_version",
        ),
        Index("ix_change_proposal_subject", "subject_entity_type", "subject_entity_id"),
        Index("ix_change_proposal_root", "root_entity_type", "root_entity_id"),
        {"comment": "MUTABLE working state. One change set per subject entity."},
    )


class ChangeProposalItem(TimestampedMixin, Base):
    """One field within a proposal. MUTABLE working state.

    `corrected_by` is the column D7 turns on: when a reviewer alters a high-risk
    candidate value themselves, that reviewer may not also approve it, and the
    proposal moves to a second review.
    """

    __tablename__ = "change_proposal_item"

    id: Mapped[uuid.UUID] = uuid_pk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("change_proposal.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(RISK_LEVEL, nullable=False)
    old_value: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    old_field_status: Mapped[FieldStatus | None] = mapped_column(FIELD_STATUS)
    new_value: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    new_field_status: Mapped[FieldStatus] = mapped_column(FIELD_STATUS, nullable=False)
    claim_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="Set when a reviewer altered the value; drives D7"
    )
    item_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PENDING")

    proposal: Mapped[ChangeProposal] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "entity_type",
            "entity_id",
            "field_path",
            name="uq_change_proposal_item_proposal_entity_field",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "item_status IN ('PENDING', 'APPROVED', 'RETURNED', 'CORRECTED', 'DISCARDED')",
            name="item_status_known",
        ),
        # A value must accompany a PUBLISHED status and must be absent otherwise, the
        # same rule the canonical tables enforce (D2).
        CheckConstraint(
            "(new_field_status = 'PUBLISHED') = (new_value IS NOT NULL)",
            name="new_value_matches_new_status",
        ),
        # High-risk items must cite evidence. Medium and low risk may be manual.
        CheckConstraint(
            "risk_level <> 'HIGH' OR claim_ids IS NOT NULL OR corrected_by IS NOT NULL",
            name="high_risk_cites_evidence_or_a_corrector",
        ),
        Index("ix_change_proposal_item_proposal_id", "proposal_id"),
        {"comment": "MUTABLE. One field per item; corrected_by drives the D7 flow."},
    )


class ReviewTask(TimestampedMixin, Base):
    """An assignment of a proposal to a reviewer for one round. MUTABLE.

    `review_round` distinguishes the first review from the second review that D7
    requires after a reviewer corrects a high-risk value.
    """

    __tablename__ = "review_task"

    id: Mapped[uuid.UUID] = uuid_pk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("change_proposal.id", ondelete="CASCADE"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[ReviewTaskState] = mapped_column(
        REVIEW_TASK_STATE, nullable=False, server_default=ReviewTaskState.OPEN.value
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    proposal: Mapped[ChangeProposal] = relationship(back_populates="tasks")
    decisions: Mapped[list[ReviewDecision]] = relationship(back_populates="task")

    __table_args__ = (
        CheckConstraint("review_round BETWEEN 1 AND 2", name="review_round_range"),
        UniqueConstraint(
            "proposal_id",
            "review_round",
            name="uq_review_task_proposal_id_review_round",
        ),
        CheckConstraint(
            "state <> 'COMPLETED' OR completed_at IS NOT NULL",
            name="completed_task_has_a_timestamp",
        ),
        {"comment": "MUTABLE. Assignment per proposal per review round."},
    )


class ReviewDecision(RecordedAtMixin, Base):
    """A reviewer's decision on one item. APPEND-ONLY.

    Immutable because the audit requirement is that the judgement made at the time is
    recoverable. Changing one's mind produces a new decision, not an edit.
    """

    __tablename__ = "review_decision"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_task.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("change_proposal_item.id", ondelete="RESTRICT"),
        comment="NULL for a decision taken on the whole proposal",
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decision: Mapped[ReviewDecisionKind] = mapped_column(REVIEW_DECISION_KIND, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason_text: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    task: Mapped[ReviewTask] = relationship(back_populates="decisions")

    __table_args__ = (
        # A return must say why. This is the PRD's 退回必须有原因 rule, and it is one
        # of the few review rules that is genuinely single-row.
        CheckConstraint(
            "decision <> 'RETURN' OR reason_code IS NOT NULL", name="return_has_a_reason_code"
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('EVIDENCE_INSUFFICIENT', 'WRONG_SOURCE', "
            "'PARSE_ERROR', 'NOT_OFFICIAL', 'NEEDS_RECOLLECTION', 'OTHER')",
            name="reason_code_known",
        ),
        CheckConstraint(
            "reason_code <> 'OTHER' OR reason_text IS NOT NULL",
            name="other_reason_is_explained",
        ),
        Index("ix_review_decision_task_id", "task_id"),
        Index("ix_review_decision_item_id", "item_id"),
        Index("ix_review_decision_reviewer_id_reviewed_at", "reviewer_id", "reviewed_at"),
        {"comment": "APPEND-ONLY. Reviewer judgement; returns must carry a reason."},
    )


class FieldConflict(TimestampedMixin, Base):
    """A field where two primary-bound sources disagree. MUTABLE until resolved.

    Holding this separately is what makes silent overwrite impossible: a conflicted
    field cannot publish until a human records which source was adopted and why.
    """

    __tablename__ = "field_conflict"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    competing_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        CheckConstraint(
            "array_length(competing_claim_ids, 1) >= 2", name="a_conflict_needs_two_claims"
        ),
        Index(
            "ix_field_conflict_entity_type_entity_id_field_path",
            "entity_type",
            "entity_id",
            "field_path",
        ),
        Index(
            "ix_field_conflict_unresolved",
            "detected_at",
            postgresql_where="is_resolved = false",
        ),
        {"comment": "MUTABLE until resolved. Blocks publication of the field."},
    )


class ConflictResolution(RecordedAtMixin, Base):
    """How a conflict was settled. APPEND-ONLY.

    The adoption rule and the rationale are mandatory: the PRD forbids silent
    overwrite, and "we picked one" without a recorded reason is exactly that.
    """

    __tablename__ = "conflict_resolution"

    id: Mapped[uuid.UUID] = uuid_pk()
    conflict_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_conflict.id", ondelete="RESTRICT"), nullable=False
    )
    adopted_claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    adoption_rule: Mapped[str] = mapped_column(String(96), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(btrim(rationale)) > 0", name="rationale_is_not_blank"),
        CheckConstraint(
            "adoption_rule IN ('MOST_RECENT_EFFECTIVE_DATE', 'HIGHEST_AUTHORITY_TIER', "
            "'PRIMARY_BINDING', 'MANUAL_JUDGEMENT')",
            name="adoption_rule_known",
        ),
        UniqueConstraint("conflict_id", name="uq_conflict_resolution_conflict_id"),
        {"comment": "APPEND-ONLY. Adoption rule and rationale are mandatory."},
    )


__all__ = [
    "ChangeProposal",
    "ChangeProposalItem",
    "ConflictResolution",
    "FieldConflict",
    "ReviewDecision",
    "ReviewTask",
]
