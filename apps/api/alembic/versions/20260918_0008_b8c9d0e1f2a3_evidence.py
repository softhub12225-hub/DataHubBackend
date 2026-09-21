"""Domain group 6: evidence plane

Sources, fetch runs, snapshots, extractions, claims and their resolution.

`snapshot` stores a content hash and an object-storage key -- never the HTML or PDF
body, which would bloat every backup for no benefit. Content-addressing plus
write-if-absent is what makes evidence behind a published field unable to change
underneath it (C12).

`snapshot`, `extraction`, `field_claim` and `claim_resolution` are append-only; the
privileges revision enforces that. `claim_resolution` exists because the claim itself
is immutable evidence, so its interpretation is recorded separately rather than by
mutating a status column (C1).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "url_hash", sa.String(length=64), nullable=False, comment="sha256 of the normalised URL"
        ),
        sa.Column("source_type", sa.String(length=48), nullable=False),
        sa.Column("owner_entity_type", sa.String(length=64), nullable=True),
        sa.Column("owner_entity_id", sa.UUID(), nullable=True),
        sa.Column("authority_tier", sa.Integer(), server_default="1", nullable=False),
        sa.Column("crawl_frequency", sa.String(length=32), nullable=False),
        sa.Column("fetch_strategy", sa.String(length=32), nullable=False),
        sa.Column("robots_allowed", sa.Boolean(), nullable=True),
        sa.Column("tos_reviewed_by", sa.UUID(), nullable=True),
        sa.Column("tos_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "access_state",
            postgresql.ENUM(
                "OK", "BLOCKED", "MANUAL_ONLY", name="source_access_state", create_type=False
            ),
            server_default="OK",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("deactivated_reason", sa.Text(), nullable=True),
        sa.Column("registered_by", sa.UUID(), nullable=True),
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
            "crawl_frequency IN ('HIGH_RISK_3X_DAILY', 'DAILY', 'WEEKLY', 'MONTHLY', 'EVENT_DRIVEN')",
            name=op.f("ck_source_crawl_frequency_known"),
        ),
        sa.CheckConstraint(
            "fetch_strategy IN ('STATIC', 'BROWSER', 'DOCUMENT', 'MANUAL')",
            name=op.f("ck_source_fetch_strategy_known"),
        ),
        sa.CheckConstraint(
            "source_type IN ('university_site', 'faculty_site', 'admissions_page', 'fee_page', 'official_pdf', 'government_regulator', 'authorized_ranking')",
            name=op.f("ck_source_source_type_known"),
        ),
        sa.CheckConstraint(
            "authority_tier BETWEEN 1 AND 5", name=op.f("ck_source_authority_tier_range")
        ),
        sa.CheckConstraint(
            "is_active = true OR deactivated_reason IS NOT NULL",
            name=op.f("ck_source_deactivation_has_a_reason"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_source_url_hash")),
        comment="Registered official sources. Blocked sources become MANUAL_ONLY (D6).",
    )
    op.create_index("ix_source_access_state", "source", ["access_state"], unique=False)
    op.create_index(
        "ix_source_owner_entity_type_owner_entity_id",
        "source",
        ["owner_entity_type", "owner_entity_id"],
        unique=False,
    )
    op.create_table(
        "source_field_binding",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column(
            "responsibility",
            postgresql.ENUM(
                "PRIMARY",
                "SECONDARY",
                "CORROBORATING",
                name="source_responsibility",
                create_type=False,
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
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_source_field_binding_source_id_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_field_binding")),
        sa.UniqueConstraint(
            "source_id",
            "entity_type",
            "field_path",
            name="uq_source_field_binding_source_id_entity_type_field_path",
        ),
        comment="Field responsibility per source (PRD section 3).",
    )
    op.create_index(
        "ix_source_field_binding_entity_type_field_path",
        "source_field_binding",
        ["entity_type", "field_path"],
        unique=False,
    )
    op.create_table(
        "source_authorization",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("grantor", sa.String(length=200), nullable=False),
        sa.Column(
            "evidence_key",
            sa.Text(),
            nullable=True,
            comment="Object-storage key of the licence document",
        ),
        sa.Column("granted_at", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column("display_allowed", sa.Boolean(), server_default="false", nullable=False),
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
            "expires_at IS NULL OR expires_at >= granted_at",
            name=op.f("ck_source_authorization_validity_is_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_authorization")),
        comment="Authorisation grants. Drives the D8 ranking gate.",
    )
    op.create_index(
        "ix_source_authorization_scope", "source_authorization", ["scope"], unique=False
    )
    op.create_table(
        "fetch_run",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "OK",
                "UNCHANGED",
                "HTTP_ERROR",
                "BLOCKED",
                "TIMEOUT",
                "PARSE_FAILED",
                name="fetch_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("fetcher", sa.String(length=32), nullable=False),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("worker_name", sa.String(length=128), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_fetch_run_finish_after_start"),
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name=op.f("ck_fetch_run_http_status_range"),
        ),
        sa.CheckConstraint("retry_count >= 0", name=op.f("ck_fetch_run_retry_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_fetch_run_source_id_source"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_run")),
        comment="APPEND-ONLY. Every fetch attempt, including unchanged results.",
    )
    op.create_index(
        "ix_fetch_run_source_id_started_at", "fetch_run", ["source_id", "started_at"], unique=False
    )
    op.create_table(
        "snapshot",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("fetch_run_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=False,
            comment="sha256 hex of the stored bytes",
        ),
        sa.Column(
            "storage_key",
            sa.Text(),
            nullable=False,
            comment="Object-storage key; bodies never live in PostgreSQL",
        ),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("rendered_text_key", sa.Text(), nullable=True),
        sa.Column("screenshot_key", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_snapshot_content_hash_is_sha256_hex")
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0", name=op.f("ck_snapshot_byte_size_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["fetch_run.id"],
            name=op.f("fk_snapshot_fetch_run_id_fetch_run"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_snapshot_source_id_source"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshot")),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_snapshot_source_id_content_hash"),
        comment="APPEND-ONLY. Hash + object-storage key; no bodies in PostgreSQL.",
    )
    op.create_index("ix_snapshot_content_hash", "snapshot", ["content_hash"], unique=False)
    op.create_index("ix_snapshot_fetch_run_id", "snapshot", ["fetch_run_id"], unique=False)
    op.create_table(
        "extraction",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("extractor_name", sa.String(length=128), nullable=False),
        sa.Column("extractor_version", sa.String(length=48), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("OK", "PARTIAL", "FAILED", name="extraction_status", create_type=False),
            nullable=False,
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("selector_trace", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name=op.f("ck_extraction_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
            name=op.f("fk_extraction_snapshot_id_snapshot"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction")),
        comment="APPEND-ONLY. Parse attempt; version enables drift detection.",
    )
    op.create_index(
        "ix_extraction_extractor_name_extractor_version",
        "extraction",
        ["extractor_name", "extractor_version"],
        unique=False,
    )
    op.create_index("ix_extraction_snapshot_id", "extraction", ["snapshot_id"], unique=False)
    op.create_table(
        "field_claim",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("extraction_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column(
            "entity_id",
            sa.UUID(),
            nullable=True,
            comment="Proposed target; authoritative binding is claim_resolution",
        ),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column(
            "proposed_field_status",
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
        sa.Column("value_normalized", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("value_raw_text", sa.Text(), nullable=True),
        sa.Column("char_offset_start", sa.Integer(), nullable=True),
        sa.Column("char_offset_end", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "proposed_field_status = 'PUBLISHED' OR value_normalized IS NULL",
            name=op.f("ck_field_claim_absence_claim_has_no_value"),
        ),
        sa.CheckConstraint(
            "(char_offset_start IS NULL) = (char_offset_end IS NULL)",
            name=op.f("ck_field_claim_offsets_come_as_a_pair"),
        ),
        sa.CheckConstraint(
            "char_offset_start IS NULL OR char_offset_end >= char_offset_start",
            name=op.f("ck_field_claim_offsets_are_ordered"),
        ),
        sa.CheckConstraint(
            "char_offset_start IS NULL OR char_offset_start >= 0",
            name=op.f("ck_field_claim_offsets_non_negative"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name=op.f("ck_field_claim_confidence_range"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name=op.f("ck_field_claim_effectivity_is_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extraction.id"],
            name=op.f("fk_field_claim_extraction_id_extraction"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_claim")),
        comment="APPEND-ONLY. Extracted assertion with offsets into the snapshot.",
    )
    op.create_index(
        "ix_field_claim_entity_type_entity_id_field_path",
        "field_claim",
        ["entity_type", "entity_id", "field_path"],
        unique=False,
    )
    op.create_index("ix_field_claim_extraction_id", "field_claim", ["extraction_id"], unique=False)
    op.create_table(
        "claim_resolution",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=48), nullable=False),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method <> 'MANUAL' OR resolved_by IS NOT NULL",
            name=op.f("ck_claim_resolution_manual_names_the_resolver"),
        ),
        sa.CheckConstraint(
            "method IN ('OFFICIAL_CODE', 'URL_IDENTITY', 'FUZZY_MATCH', 'MANUAL')",
            name=op.f("ck_claim_resolution_method_known"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["field_claim.id"],
            name=op.f("fk_claim_resolution_claim_id_field_claim"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_resolution")),
        sa.UniqueConstraint("claim_id", name="uq_claim_resolution_claim_id"),
        comment="APPEND-ONLY. Authoritative claim to entity binding.",
    )
    op.create_index(
        "ix_claim_resolution_entity_type_entity_id",
        "claim_resolution",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_table(
        "resolution_candidate",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("suggested_entity_type", sa.String(length=64), nullable=True),
        sa.Column("suggested_entity_id", sa.UUID(), nullable=True),
        sa.Column("match_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("state", sa.String(length=32), server_default="OPEN", nullable=False),
        sa.Column("assignee_id", sa.UUID(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('OPEN', 'ASSIGNED', 'RESOLVED', 'DISCARDED')",
            name=op.f("ck_resolution_candidate_state_known"),
        ),
        sa.CheckConstraint(
            "match_score IS NULL OR match_score BETWEEN 0 AND 1",
            name=op.f("ck_resolution_candidate_match_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["field_claim.id"],
            name=op.f("fk_resolution_candidate_claim_id_field_claim"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resolution_candidate")),
        sa.UniqueConstraint("claim_id", name="uq_resolution_candidate_claim_id"),
        comment="MUTABLE working queue for unresolved claims.",
    )


def downgrade() -> None:
    op.drop_table("resolution_candidate")
    op.drop_table("claim_resolution")
    op.drop_table("field_claim")
    op.drop_table("extraction")
    op.drop_table("snapshot")
    op.drop_table("fetch_run")
    op.drop_table("source_authorization")
    op.drop_table("source_field_binding")
    op.drop_table("source")
