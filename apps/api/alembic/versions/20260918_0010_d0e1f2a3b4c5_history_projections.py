"""Domain group 8: immutable history, projections, outbox

The two halves of the canonical plane, plus delivery infrastructure.

**Immutable history**: `entity_version`, `field_provenance`, `audit_log`,
`change_event`. No `superseded_at` column exists anywhere -- supersession is version
chronology, and the current provenance row for a field is simply the one with the
greatest `root_version_no` (C1).

**Mutable projections**: `entity_head` (which also serves as the row the publication
transaction locks to allocate a version) and `field_current`.

**Delivery infrastructure**: `outbox_message`, mutable by design and explicitly
exempt from the immutability rule (C7). Latest Updates reads `change_event`
directly, so a stalled relay cannot make the feed wrong.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_version",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "root_type",
            postgresql.ENUM("university", "program", name="root_entity_type", create_type=False),
            nullable=False,
        ),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("state_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diff_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("rollback_of_version_id", sa.UUID(), nullable=True),
        sa.Column("correction_of_version_id", sa.UUID(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "rollback_of_version_id IS NULL OR rollback_of_version_id <> id",
            name=op.f("ck_entity_version_not_own_rollback"),
        ),
        sa.CheckConstraint(
            "version_no >= 1", name=op.f("ck_entity_version_version_no_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["correction_of_version_id"],
            ["entity_version.id"],
            name=op.f("fk_entity_version_correction_of_version_id_entity_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_of_version_id"],
            ["entity_version.id"],
            name=op.f("fk_entity_version_rollback_of_version_id_entity_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_version")),
        sa.UniqueConstraint(
            "root_type", "root_id", "version_no", name="uq_entity_version_root_version_no"
        ),
        comment="APPEND-ONLY. Full published state per root version. No is_current.",
    )
    op.create_index(
        "ix_entity_version_published_at", "entity_version", ["published_at"], unique=False
    )
    op.create_index(
        "ix_entity_version_root_type_root_id_version_no",
        "entity_version",
        ["root_type", "root_id", "version_no"],
        unique=False,
    )
    op.create_table(
        "field_provenance",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column(
            "root_type",
            postgresql.ENUM("university", "program", name="root_entity_type", create_type=False),
            nullable=False,
        ),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("root_version_no", sa.Integer(), nullable=False),
        sa.Column(
            "field_status",
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
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "risk_level",
            postgresql.ENUM("HIGH", "MEDIUM", "LOW", name="risk_level", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "trust_status",
            postgresql.ENUM(
                "VERIFIED", "CONFLICTED", "STALE", name="trust_status", create_type=False
            ),
            server_default="VERIFIED",
            nullable=False,
        ),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "(field_status = 'PUBLISHED') = (value IS NOT NULL)",
            name=op.f("ck_field_provenance_value_matches_field_status"),
        ),
        sa.CheckConstraint(
            "risk_level <> 'HIGH' OR (snapshot_id IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name=op.f("ck_field_provenance_high_risk_requires_evidence_and_review"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name=op.f("ck_field_provenance_effectivity_is_ordered"),
        ),
        sa.CheckConstraint(
            "observed_at IS NULL OR observed_at <= published_at",
            name=op.f("ck_field_provenance_observed_before_published"),
        ),
        sa.CheckConstraint(
            "root_version_no >= 1", name=op.f("ck_field_provenance_root_version_no_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["field_claim.id"],
            name=op.f("fk_field_provenance_claim_id_field_claim"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
            name=op.f("fk_field_provenance_snapshot_id_snapshot"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_field_provenance_source_id_source"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_provenance")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            "root_version_no",
            name="uq_field_provenance_entity_field_root_version",
        ),
        comment="APPEND-ONLY audit spine. Supersession = version chronology (C1).",
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "actor_type",
            postgresql.ENUM("USER", "SYSTEM", "API_CLIENT", name="actor_type", create_type=False),
            nullable=False,
        ),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=True),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "row_hash",
            sa.String(length=64),
            nullable=True,
            comment="Computed by trigger; never supplied by the caller",
        ),
        sa.CheckConstraint(
            "actor_type <> 'USER' OR actor_id IS NOT NULL",
            name=op.f("ck_audit_log_user_action_names_the_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("prev_hash", name="uq_audit_log_prev_hash"),
        comment="APPEND-ONLY, hash-chained. Tamper-evident action log.",
    )
    op.create_index(
        "ix_audit_log_actor_id_occurred_at", "audit_log", ["actor_id", "occurred_at"], unique=False
    )
    op.create_index(
        "ix_audit_log_object_type_object_id_occurred_at",
        "audit_log",
        ["object_type", "object_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "change_event",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "root_type",
            postgresql.ENUM("university", "program", name="root_entity_type", create_type=False),
            nullable=False,
        ),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("root_version_no", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("field_path", sa.String(length=200), nullable=True),
        sa.Column(
            "change_kind",
            postgresql.ENUM(
                "DEADLINE_CHANGED",
                "TUITION_CHANGED",
                "REQUIREMENT_CHANGED",
                "LANGUAGE_REQUIREMENT_CHANGED",
                "PROGRAM_OPENED",
                "PROGRAM_CLOSED",
                "PROGRAM_SUSPENDED",
                "OFFERING_ADDED",
                "OFFERING_WITHDRAWN",
                "INTAKE_ADDED",
                "RANKING_PUBLISHED",
                "PROFILE_UPDATED",
                name="change_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "risk_level",
            postgresql.ENUM("HIGH", "MEDIUM", "LOW", name="risk_level", create_type=False),
            nullable=False,
        ),
        sa.Column("destination_code", sa.String(length=8), nullable=True),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            nullable=True,
        ),
        sa.Column("version_id", sa.UUID(), nullable=True),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("provenance_id", sa.UUID(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "root_version_no >= 1", name=op.f("ck_change_event_root_version_no_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_change_event_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["field_provenance.id"],
            name=op.f("fk_change_event_provenance_id_field_provenance"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["entity_version.id"],
            name=op.f("fk_change_event_version_id_entity_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_event")),
        comment="APPEND-ONLY business history. Powers Latest Updates (D12).",
    )
    op.create_index(
        "ix_change_event_destination_code_published_at",
        "change_event",
        ["destination_code", "published_at"],
        unique=False,
    )
    op.create_index("ix_change_event_published_at", "change_event", ["published_at"], unique=False)
    op.create_index(
        "ix_change_event_root_type_root_id", "change_event", ["root_type", "root_id"], unique=False
    )
    op.create_table(
        "entity_head",
        sa.Column(
            "root_type",
            postgresql.ENUM("university", "program", name="root_entity_type", create_type=False),
            nullable=False,
        ),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.UUID(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
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
            "current_version_no >= 1", name=op.f("ck_entity_head_current_version_no_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["current_version_id"],
            ["entity_version.id"],
            name=op.f("fk_entity_head_current_version_id_entity_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("root_type", "root_id", name=op.f("pk_entity_head")),
        comment="MUTABLE PROJECTION. Current version per root; the publish lock row.",
    )
    op.create_table(
        "field_current",
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column(
            "field_status",
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
        sa.Column("provenance_id", sa.UUID(), nullable=False),
        sa.Column(
            "root_type",
            postgresql.ENUM("university", "program", name="root_entity_type", create_type=False),
            nullable=False,
        ),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("root_version_no", sa.Integer(), nullable=False),
        sa.Column(
            "trust_status",
            postgresql.ENUM(
                "VERIFIED", "CONFLICTED", "STALE", name="trust_status", create_type=False
            ),
            nullable=False,
        ),
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
            "root_version_no >= 1", name=op.f("ck_field_current_root_version_no_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["field_provenance.id"],
            name=op.f("fk_field_current_provenance_id_field_provenance"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "entity_type", "entity_id", "field_path", name=op.f("pk_field_current")
        ),
        comment="MUTABLE PROJECTION. Current status/provenance pointer per field.",
    )
    op.create_index(
        "ix_field_current_field_status",
        "field_current",
        ["field_status"],
        unique=False,
        postgresql_where="field_status <> 'PUBLISHED'",
    )
    op.create_index(
        "ix_field_current_root_type_root_id",
        "field_current",
        ["root_type", "root_id"],
        unique=False,
    )
    op.create_table(
        "outbox_message",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("topic", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "dedup_key",
            sa.String(length=200),
            nullable=False,
            comment="Makes at-least-once idempotent",
        ),
        sa.Column("aggregate_type", sa.String(length=64), nullable=True),
        sa.Column("aggregate_id", sa.UUID(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="8", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "INFLIGHT",
                "DELIVERED",
                "FAILED",
                "DEAD",
                name="outbox_status",
                create_type=False,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
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
            "status <> 'DELIVERED' OR delivered_at IS NOT NULL",
            name=op.f("ck_outbox_message_delivered_has_a_timestamp"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_message_attempts_non_negative")),
        sa.CheckConstraint(
            "max_attempts >= 1", name=op.f("ck_outbox_message_max_attempts_is_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_message")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_outbox_message_dedup_key")),
        comment="MUTABLE delivery infrastructure. Exempt from immutability (C7).",
    )
    op.create_index(
        "ix_outbox_message_available_at",
        "outbox_message",
        ["available_at"],
        unique=False,
        postgresql_where="status = 'PENDING'",
    )


def downgrade() -> None:
    op.drop_table("outbox_message")
    op.drop_table("field_current")
    op.drop_table("entity_head")
    op.drop_table("change_event")
    op.drop_table("audit_log")
    op.drop_table("field_provenance")
    op.drop_table("entity_version")
