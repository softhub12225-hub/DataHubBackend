"""Domain group 7: governance working state

Proposals, items, review tasks, decisions and conflicts.

Mutable working state (`change_proposal`, `change_proposal_item`, `review_task`,
`field_conflict`) sits alongside immutable record (`review_decision`,
`conflict_resolution`): a decision that can be edited is not a decision.

`change_proposal_item.corrected_by` is the column D7 turns on -- a reviewer who
alters a high-risk value cannot also approve it. That rule itself is cross-row,
cross-user authorisation and lives in the policy layer (C2); only the column is
here.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_proposal",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subject_entity_type", sa.String(length=64), nullable=False),
        sa.Column("subject_entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "root_entity_type",
            sa.String(length=64),
            nullable=False,
            comment="Versioned root this change publishes under (D13)",
        ),
        sa.Column("root_entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "detection_type",
            postgresql.ENUM(
                "AUTO_DIFF",
                "MANUAL_EDIT",
                "IMPORT",
                "CORRECTION",
                name="detection_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "risk_level",
            postgresql.ENUM("HIGH", "MEDIUM", "LOW", name="risk_level", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "DRAFT",
                "PENDING",
                "PENDING_SECOND_REVIEW",
                "APPROVED",
                "RETURNED",
                "PUBLISHED",
                "DISCARDED",
                name="proposal_status",
                create_type=False,
            ),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", sa.UUID(), nullable=True, comment="NULL for system-generated proposals"
        ),
        sa.Column("correction_of_version_id", sa.UUID(), nullable=True),
        sa.Column("published_version_id", sa.UUID(), nullable=True),
        sa.Column("discarded_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "detection_type <> 'CORRECTION' OR correction_of_version_id IS NOT NULL",
            name=op.f("ck_change_proposal_correction_names_the_version_it_corrects"),
        ),
        sa.CheckConstraint(
            "detection_type <> 'MANUAL_EDIT' OR created_by IS NOT NULL",
            name=op.f("ck_change_proposal_manual_proposal_names_its_author"),
        ),
        sa.CheckConstraint(
            "status <> 'DISCARDED' OR discarded_reason IS NOT NULL",
            name=op.f("ck_change_proposal_discard_has_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'PUBLISHED' OR published_version_id IS NOT NULL",
            name=op.f("ck_change_proposal_published_names_its_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_proposal")),
        comment="MUTABLE working state. One change set per subject entity.",
    )
    op.create_index(
        "ix_change_proposal_root",
        "change_proposal",
        ["root_entity_type", "root_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_change_proposal_subject",
        "change_proposal",
        ["subject_entity_type", "subject_entity_id"],
        unique=False,
    )
    op.create_table(
        "change_proposal_item",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column(
            "risk_level",
            postgresql.ENUM("HIGH", "MEDIUM", "LOW", name="risk_level", create_type=False),
            nullable=False,
        ),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "old_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "new_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("claim_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column(
            "corrected_by",
            sa.UUID(),
            nullable=True,
            comment="Set when a reviewer altered the value; drives D7",
        ),
        sa.Column("item_status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(new_field_status = 'PUBLISHED') = (new_value IS NOT NULL)",
            name=op.f("ck_change_proposal_item_new_value_matches_new_status"),
        ),
        sa.CheckConstraint(
            "item_status IN ('PENDING', 'APPROVED', 'RETURNED', 'CORRECTED', 'DISCARDED')",
            name=op.f("ck_change_proposal_item_item_status_known"),
        ),
        sa.CheckConstraint(
            "risk_level <> 'HIGH' OR claim_ids IS NOT NULL OR corrected_by IS NOT NULL",
            name=op.f("ck_change_proposal_item_high_risk_cites_evidence_or_a_corrector"),
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["change_proposal.id"],
            name=op.f("fk_change_proposal_item_proposal_id_change_proposal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_proposal_item")),
        sa.UniqueConstraint(
            "proposal_id",
            "entity_type",
            "entity_id",
            "field_path",
            name="uq_change_proposal_item_proposal_entity_field",
            postgresql_nulls_not_distinct=True,
        ),
        comment="MUTABLE. One field per item; corrected_by drives the D7 flow.",
    )
    op.create_index(
        "ix_change_proposal_item_proposal_id", "change_proposal_item", ["proposal_id"], unique=False
    )
    op.create_table(
        "review_task",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("assignee_id", sa.UUID(), nullable=True),
        sa.Column("review_round", sa.Integer(), server_default="1", nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                "OPEN",
                "COMPLETED",
                "ESCALATED",
                "REASSIGNED",
                name="review_task_state",
                create_type=False,
            ),
            server_default="OPEN",
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state <> 'COMPLETED' OR completed_at IS NOT NULL",
            name=op.f("ck_review_task_completed_task_has_a_timestamp"),
        ),
        sa.CheckConstraint(
            "review_round BETWEEN 1 AND 2", name=op.f("ck_review_task_review_round_range")
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["change_proposal.id"],
            name=op.f("fk_review_task_proposal_id_change_proposal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_task")),
        sa.UniqueConstraint(
            "proposal_id", "review_round", name="uq_review_task_proposal_id_review_round"
        ),
        comment="MUTABLE. Assignment per proposal per review round.",
    )
    op.create_table(
        "review_decision",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column(
            "item_id",
            sa.UUID(),
            nullable=True,
            comment="NULL for a decision taken on the whole proposal",
        ),
        sa.Column("reviewer_id", sa.UUID(), nullable=False),
        sa.Column(
            "decision",
            postgresql.ENUM(
                "APPROVE", "RETURN", "CORRECT", name="review_decision_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision <> 'RETURN' OR reason_code IS NOT NULL",
            name=op.f("ck_review_decision_return_has_a_reason_code"),
        ),
        sa.CheckConstraint(
            "reason_code <> 'OTHER' OR reason_text IS NOT NULL",
            name=op.f("ck_review_decision_other_reason_is_explained"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('EVIDENCE_INSUFFICIENT', 'WRONG_SOURCE', 'PARSE_ERROR', 'NOT_OFFICIAL', 'NEEDS_RECOLLECTION', 'OTHER')",
            name=op.f("ck_review_decision_reason_code_known"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["change_proposal_item.id"],
            name=op.f("fk_review_decision_item_id_change_proposal_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["review_task.id"],
            name=op.f("fk_review_decision_task_id_review_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decision")),
        comment="APPEND-ONLY. Reviewer judgement; returns must carry a reason.",
    )
    op.create_index("ix_review_decision_item_id", "review_decision", ["item_id"], unique=False)
    op.create_index(
        "ix_review_decision_reviewer_id_reviewed_at",
        "review_decision",
        ["reviewer_id", "reviewed_at"],
        unique=False,
    )
    op.create_index("ix_review_decision_task_id", "review_decision", ["task_id"], unique=False)
    op.create_table(
        "field_conflict",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column("competing_claim_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_resolved", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "array_length(competing_claim_ids, 1) >= 2",
            name=op.f("ck_field_conflict_a_conflict_needs_two_claims"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_conflict")),
        comment="MUTABLE until resolved. Blocks publication of the field.",
    )
    op.create_index(
        "ix_field_conflict_entity_type_entity_id_field_path",
        "field_conflict",
        ["entity_type", "entity_id", "field_path"],
        unique=False,
    )
    op.create_index(
        "ix_field_conflict_unresolved",
        "field_conflict",
        ["detected_at"],
        unique=False,
        postgresql_where="is_resolved = false",
    )
    op.create_table(
        "conflict_resolution",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conflict_id", sa.UUID(), nullable=False),
        sa.Column("adopted_claim_id", sa.UUID(), nullable=False),
        sa.Column("adoption_rule", sa.String(length=96), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("resolved_by", sa.UUID(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "adoption_rule IN ('MOST_RECENT_EFFECTIVE_DATE', 'HIGHEST_AUTHORITY_TIER', 'PRIMARY_BINDING', 'MANUAL_JUDGEMENT')",
            name=op.f("ck_conflict_resolution_adoption_rule_known"),
        ),
        sa.CheckConstraint(
            "length(btrim(rationale)) > 0",
            name=op.f("ck_conflict_resolution_rationale_is_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["conflict_id"],
            ["field_conflict.id"],
            name=op.f("fk_conflict_resolution_conflict_id_field_conflict"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conflict_resolution")),
        sa.UniqueConstraint("conflict_id", name="uq_conflict_resolution_conflict_id"),
        comment="APPEND-ONLY. Adoption rule and rationale are mandatory.",
    )


def downgrade() -> None:
    op.drop_table("conflict_resolution")
    op.drop_table("field_conflict")
    op.drop_table("review_decision")
    op.drop_table("review_task")
    op.drop_table("change_proposal_item")
    op.drop_table("change_proposal")
