"""Domain group 1b: reference and controlled vocabularies

Tables rather than enums, so an administrator can add a destination, a currency or
a round type without a deployment (N1). Small vocabularies use a stable text `code`
as the primary key, which keeps seeds and partial indexes readable.

`qualification_group` is how an institution's own tier list is represented -- as
ordinary source-published data, never as a rule baked into the schema (B1). Its
`published_by_university_id` FK is added later, in the catalog revision, because
`university` does not exist yet.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "destination",
        sa.Column("code", sa.String(length=8), nullable=False, comment="ISO 3166 alpha-2"),
        sa.Column("name_en", sa.String(length=128), nullable=False),
        sa.Column("name_zh", sa.String(length=128), nullable=True),
        sa.Column("is_pilot", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("rollout_wave", sa.Integer(), nullable=True),
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
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_destination_code_is_uppercase")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_destination")),
        comment="Study destinations. UK/HK/MO are the pilot scope.",
    )
    op.create_table(
        "discipline",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("name_zh", sa.String(length=160), nullable=True),
        sa.Column(
            "external_mappings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="HECoS/JACS/CIP codes keyed by scheme",
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
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_discipline_not_own_parent")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["discipline.id"],
            name=op.f("fk_discipline_parent_id_discipline"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discipline")),
        sa.UniqueConstraint("code", name=op.f("uq_discipline_code")),
        comment="Hierarchical subject taxonomy (Business, CS & Data, Engineering...)",
    )
    op.create_index("ix_discipline_parent_id", "discipline", ["parent_id"], unique=False)
    op.create_table(
        "degree_level",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_in_scope", sa.Boolean(), server_default="true", nullable=False),
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
        sa.PrimaryKeyConstraint("code", name=op.f("pk_degree_level")),
        comment="Degree levels. Extensible: doctorate arrives in phase 3.",
    )
    op.create_table(
        "intake_season",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
        sa.Column("typical_start_month", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
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
            "typical_start_month IS NULL OR typical_start_month BETWEEN 1 AND 12",
            name=op.f("ck_intake_season_typical_start_month_range"),
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_intake_season")),
        comment="Intake seasons (September, January, ...)",
    )
    op.create_table(
        "currency",
        sa.Column("code", sa.String(length=3), nullable=False),
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("minor_units", sa.Integer(), server_default="2", nullable=False),
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
            "code = upper(code) AND length(code) = 3", name=op.f("ck_currency_iso4217_shape")
        ),
        sa.CheckConstraint(
            "minor_units BETWEEN 0 AND 4", name=op.f("ck_currency_minor_units_range")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currency")),
        comment="ISO 4217 currencies. Deliberately holds no exchange rates.",
    )
    op.create_table(
        "billing_unit",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("code", name=op.f("pk_billing_unit")),
        comment="per_year / per_credit / total_program / per_module / per_semester",
    )
    op.create_table(
        "test_type",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_en", sa.String(length=96), nullable=False),
        sa.Column("name_zh", sa.String(length=96), nullable=True),
        sa.Column(
            "subscore_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Component names this test reports, e.g. listening/reading/...",
        ),
        sa.Column("max_overall", sa.Float(), nullable=True),
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
        sa.PrimaryKeyConstraint("code", name=op.f("pk_test_type")),
        comment="IELTS, TOEFL, PTE... Never mutually convertible.",
    )
    op.create_table(
        "student_category",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("destination_code", sa.String(length=8), nullable=True),
        sa.Column("name_en", sa.String(length=96), nullable=False),
        sa.Column("name_zh", sa.String(length=96), nullable=True),
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
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_student_category_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_student_category")),
        sa.UniqueConstraint(
            "destination_code",
            "code",
            name="uq_student_category_destination_code_code",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Fee categories. NULL destination_code = applies to all.",
    )
    op.create_table(
        "scope_dimension",
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name_en", sa.String(length=96), nullable=False),
        sa.Column("name_zh", sa.String(length=96), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "expects_group_ref",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="True when criteria on this dimension point at a qualification_group",
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
        sa.PrimaryKeyConstraint("code", name=op.f("pk_scope_dimension")),
        comment="applicant_country, qualification_type, qualification_group, ...",
    )
    op.create_table(
        "qualification_group",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=False),
        sa.Column("name_zh", sa.String(length=200), nullable=True),
        sa.Column(
            "published_by_university_id",
            sa.UUID(),
            nullable=True,
            comment="Set when a specific institution publishes this list",
        ),
        sa.Column("description", sa.Text(), nullable=True),
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
        # The FK to `university` is created by the institution_catalog revision:
        # reference data must load before institutions, but a qualification group can
        # name the university that published it.
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qualification_group")),
        sa.UniqueConstraint("code", name=op.f("uq_qualification_group_code")),
        comment="Source-published lists of institutions/qualifications.",
    )
    op.create_index(
        "ix_qualification_group_published_by_university_id",
        "qualification_group",
        ["published_by_university_id"],
        unique=False,
    )
    op.create_table(
        "applicant_scope",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("name_zh", sa.String(length=160), nullable=True),
        sa.Column("is_universal", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("precedence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_scope")),
        sa.UniqueConstraint("code", name=op.f("uq_applicant_scope_code")),
        comment="Named applicability scope. Zero criteria = universal.",
    )
    op.create_table(
        "applicant_scope_criterion",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("dimension_code", sa.String(length=48), nullable=False),
        sa.Column(
            "operator",
            postgresql.ENUM(
                "EQUALS",
                "IN_GROUP",
                "NOT_EQUALS",
                "NOT_IN_GROUP",
                name="scope_operator",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=256), nullable=True),
        sa.Column("value_ref", sa.UUID(), nullable=True),
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
            "(value IS NULL) <> (value_ref IS NULL)",
            name=op.f("ck_applicant_scope_criterion_exactly_one_of_value_or_ref"),
        ),
        sa.ForeignKeyConstraint(
            ["dimension_code"],
            ["scope_dimension.code"],
            name=op.f("fk_applicant_scope_criterion_dimension_code_scope_dimension"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["applicant_scope.id"],
            name=op.f("fk_applicant_scope_criterion_scope_id_applicant_scope"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["value_ref"],
            ["qualification_group.id"],
            name=op.f("fk_applicant_scope_criterion_value_ref_qualification_group"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_scope_criterion")),
        sa.UniqueConstraint(
            "scope_id",
            "dimension_code",
            "operator",
            "value",
            "value_ref",
            name="uq_applicant_scope_criterion_identity",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Conjunctive criteria. Extensible via scope_dimension (B1).",
    )
    op.create_index(
        "ix_applicant_scope_criterion_scope_id",
        "applicant_scope_criterion",
        ["scope_id"],
        unique=False,
    )
    op.create_table(
        "qualification_group_member",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("member_university_id", sa.UUID(), nullable=True),
        sa.Column("member_label", sa.String(length=256), nullable=True),
        sa.Column("member_code", sa.String(length=96), nullable=True),
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
            "member_university_id IS NOT NULL OR member_label IS NOT NULL",
            name=op.f("ck_qualification_group_member_member_is_identified"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["qualification_group.id"],
            name=op.f("fk_qualification_group_member_group_id_qualification_group"),
            ondelete="CASCADE",
        ),
        # The FK to `university` is created by the institution_catalog revision:
        # reference data must load before institutions, but a qualification group can
        # name the university that published it.
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qualification_group_member")),
        sa.UniqueConstraint(
            "group_id",
            "member_university_id",
            "member_label",
            name="uq_qualification_group_member_identity",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Members of a source-published qualification/institution list.",
    )
    op.create_index(
        "ix_qualification_group_member_group_id",
        "qualification_group_member",
        ["group_id"],
        unique=False,
    )
    op.create_table(
        "application_round_type",
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name_en", sa.String(length=96), nullable=False),
        sa.Column("name_zh", sa.String(length=96), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "is_institution_defined",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="Exactly one row should have this set: INSTITUTION_DEFINED",
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
        sa.PrimaryKeyConstraint("code", name=op.f("pk_application_round_type")),
        comment="ROUND_1, PRIORITY, EARLY_ACTION, MAIN, ROLLING, INSTITUTION_DEFINED...",
    )


def downgrade() -> None:
    op.drop_table("application_round_type")
    op.drop_table("qualification_group_member")
    op.drop_table("applicant_scope_criterion")
    op.drop_table("applicant_scope")
    op.drop_table("qualification_group")
    op.drop_table("scope_dimension")
    op.drop_table("student_category")
    op.drop_table("test_type")
    op.drop_table("billing_unit")
    op.drop_table("currency")
    op.drop_table("intake_season")
    op.drop_table("degree_level")
    op.drop_table("discipline")
    op.drop_table("destination")
