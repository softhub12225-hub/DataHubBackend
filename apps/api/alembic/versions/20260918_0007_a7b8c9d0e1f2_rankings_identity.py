"""Domain group 5: rankings and the identity graph

Rankings are schema-only and feature-gated (D8): `ranking_edition.display_allowed`
defaults false and requires a recorded authorisation. No ranking data is seeded.

`entity_alias` handles renames without touching `canonical_id`; `entity_relationship`
is an append-only record of one real entity replacing another (B6). `fact_absence`
states that an official source publishes no facts of a given kind at a given scope
(D15) -- the case an inline field status cannot express.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ranking_publisher",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=False),
        sa.Column("website_url", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_publisher")),
        sa.UniqueConstraint("code", name=op.f("uq_ranking_publisher_code")),
        comment="QS, THE, ARWU... No data seeded without a licence.",
    )
    op.create_table(
        "ranking_edition",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("publisher_id", sa.UUID(), nullable=False),
        sa.Column("ranking_name", sa.String(length=200), nullable=False),
        sa.Column("edition_year", sa.Integer(), nullable=False),
        sa.Column("methodology_version", sa.String(length=96), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorization_id", sa.UUID(), nullable=True),
        sa.Column(
            "display_allowed",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="D8 gate; default closed",
        ),
        sa.Column("licence_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "display_allowed = false OR authorization_id IS NOT NULL",
            name=op.f("ck_ranking_edition_display_requires_authorization"),
        ),
        sa.CheckConstraint(
            "edition_year BETWEEN 1900 AND 2200",
            name=op.f("ck_ranking_edition_edition_year_plausible"),
        ),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["ranking_publisher.id"],
            name=op.f("fk_ranking_edition_publisher_id_ranking_publisher"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_edition")),
        sa.UniqueConstraint(
            "publisher_id",
            "ranking_name",
            "edition_year",
            name="uq_ranking_edition_publisher_id_ranking_name_edition_year",
        ),
        comment="Ranking edition. display_allowed defaults false (D8).",
    )
    op.create_table(
        "ranking_entry",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("edition_id", sa.UUID(), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("discipline_id", sa.UUID(), nullable=True),
        sa.Column("rank_value", sa.Integer(), nullable=True),
        sa.Column("rank_low", sa.Integer(), nullable=True),
        sa.Column("rank_high", sa.Integer(), nullable=True),
        sa.Column("score", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("is_tied", sa.Boolean(), server_default="false", nullable=False),
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
            "(rank_value IS NOT NULL) <> (rank_low IS NOT NULL AND rank_high IS NOT NULL)",
            name=op.f("ck_ranking_entry_exact_rank_or_band"),
        ),
        sa.CheckConstraint(
            "rank_low IS NULL OR rank_high IS NULL OR rank_low <= rank_high",
            name=op.f("ck_ranking_entry_band_is_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["discipline_id"],
            ["discipline.id"],
            name=op.f("fk_ranking_entry_discipline_id_discipline"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["ranking_edition.id"],
            name=op.f("fk_ranking_entry_edition_id_ranking_edition"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_ranking_entry_university_id_university"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_entry")),
        sa.UniqueConstraint(
            "edition_id",
            "university_id",
            "discipline_id",
            name="uq_ranking_entry_edition_id_university_id_discipline_id",
            postgresql_nulls_not_distinct=True,
        ),
        comment="A university's placement in one ranking edition.",
    )
    op.create_index(
        "ix_ranking_entry_university_id", "ranking_entry", ["university_id"], unique=False
    )
    op.create_table(
        "entity_alias",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "alias_kind",
            postgresql.ENUM(
                "FORMER_NAME",
                "TRADE_NAME",
                "ABBREVIATION",
                "TRANSLITERATION",
                "EXTERNAL_ID",
                name="alias_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=400), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=True),
        sa.Column("external_system", sa.String(length=96), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
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
            "alias_kind <> 'EXTERNAL_ID' OR external_system IS NOT NULL",
            name=op.f("ck_entity_alias_external_id_names_its_system"),
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name=op.f("ck_entity_alias_validity_is_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_alias")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "alias_kind",
            "value",
            "locale",
            name="uq_entity_alias_entity_type_entity_id_alias_kind_value_locale",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Aliases. A rename never changes canonical_id (B6).",
    )
    op.create_index(
        "ix_entity_alias_entity_type_entity_id",
        "entity_alias",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_table(
        "entity_relationship",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("from_entity_id", sa.UUID(), nullable=False),
        sa.Column("to_entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "relationship_kind",
            postgresql.ENUM(
                "SUPERSEDED_BY",
                "MERGED_INTO",
                "SPLIT_INTO",
                name="entity_relationship_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_entity_id <> to_entity_id",
            name=op.f("ck_entity_relationship_no_self_relationship"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_relationship")),
        sa.UniqueConstraint(
            "entity_type",
            "from_entity_id",
            "to_entity_id",
            "relationship_kind",
            "effective_from",
            name="uq_entity_relationship_identity",
        ),
        comment="APPEND-ONLY. SUPERSEDED_BY / MERGED_INTO / SPLIT_INTO (B6).",
    )
    op.create_index(
        "ix_entity_relationship_from",
        "entity_relationship",
        ["entity_type", "from_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_entity_relationship_to",
        "entity_relationship",
        ["entity_type", "to_entity_id"],
        unique=False,
    )
    op.create_table(
        "fact_absence",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("collection_path", sa.String(length=200), nullable=False),
        sa.Column("applicant_scope_id", sa.UUID(), nullable=True),
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
        sa.Column("official_url", sa.Text(), nullable=True),
        sa.Column("official_text", sa.Text(), nullable=True),
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
            "field_status IN ('OFFICIALLY_NOT_PUBLISHED', 'NOT_CHECKED', 'WITHDRAWN')",
            name=op.f("ck_fact_absence_only_absence_statuses"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_scope_id"],
            ["applicant_scope.id"],
            name=op.f("fk_fact_absence_applicant_scope_id_applicant_scope"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fact_absence")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "collection_path",
            "applicant_scope_id",
            name="uq_fact_absence_entity_collection_scope",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Explicit absence of a whole fact collection (D15).",
    )
    op.create_index(
        "ix_fact_absence_entity_type_entity_id",
        "fact_absence",
        ["entity_type", "entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("fact_absence")
    op.drop_table("entity_relationship")
    op.drop_table("entity_alias")
    op.drop_table("ranking_entry")
    op.drop_table("ranking_edition")
    op.drop_table("ranking_publisher")
