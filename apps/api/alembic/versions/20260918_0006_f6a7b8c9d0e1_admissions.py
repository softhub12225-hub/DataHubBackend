"""Domain group 4: intakes, rounds, deadlines, requirements, tuition

The admissions facts, and the two corrections that shape them.

**Rounds (C10).** Identity is split: a controlled `round_code` occurs once per
intake, while `INSTITUTION_DEFINED` rounds are identified by their normalised label,
so an intake may hold several with different wording. `sequence_no` appears in no
unique constraint.

**Deadlines (C4, C9, C13, C14).** The source-date column group stores only the
calendar parts a source actually published. `precision` is generated; `instant_utc`
exists if and only if date + time + zone were all stated; `cal_range` is a
calendar-local `daterange` query aid that is never labelled UTC and that covers the
whole month for a MONTH_PART fact.

**Requirements (C6).** Nested ownership with composite foreign keys, so PostgreSQL
proves the offering belongs to the program and the intake belongs to that offering.

**Tuition (B5).** An amount without a currency and billing unit is refused.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column(
            "academic_year",
            sa.String(length=16),
            nullable=False,
            comment="As published, e.g. 2027/28",
        ),
        sa.Column("intake_season_code", sa.String(length=32), nullable=False),
        sa.Column(
            "lifecycle_status",
            postgresql.ENUM(
                "ACTIVE",
                "SUSPENDED",
                "WITHDRAWN",
                "NOT_OFFERED_THIS_CYCLE",
                name="lifecycle_status",
                create_type=False,
            ),
            server_default="ACTIVE",
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
            "academic_year ~ '^[0-9]{4}(/[0-9]{2,4})?$'", name=op.f("ck_intake_academic_year_shape")
        ),
        sa.ForeignKeyConstraint(
            ["intake_season_code"],
            ["intake_season.code"],
            name=op.f("fk_intake_intake_season_code_intake_season"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["program_offering.id"],
            name=op.f("fk_intake_offering_id_program_offering"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake")),
        sa.UniqueConstraint("id", "offering_id", name="uq_intake_id_offering_id"),
        sa.UniqueConstraint(
            "offering_id",
            "academic_year",
            "intake_season_code",
            name="uq_intake_offering_id_academic_year_intake_season_code",
        ),
        comment="Admission cycle: offering x academic year x season.",
    )
    op.create_index("ix_intake_offering_id", "intake", ["offering_id"], unique=False)
    op.create_table(
        "application_round",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("intake_id", sa.UUID(), nullable=False),
        sa.Column("round_code", sa.String(length=48), nullable=False),
        sa.Column(
            "round_label",
            sa.String(length=256),
            nullable=True,
            comment="The institution's own wording, verbatim",
        ),
        sa.Column(
            "round_label_norm",
            sa.String(length=256),
            sa.Computed(
                "lower(regexp_replace(btrim(round_label), '\\s+', ' ', 'g'))", persisted=True
            ),
            nullable=True,
            comment="Normalised label; identity for INSTITUTION_DEFINED rounds (C10)",
        ),
        sa.Column(
            "sequence_no",
            sa.Integer(),
            nullable=True,
            comment="Display order ONLY. Never identity (C10).",
        ),
        sa.Column(
            "lifecycle_status",
            postgresql.ENUM(
                "ACTIVE",
                "SUSPENDED",
                "WITHDRAWN",
                "NOT_OFFERED_THIS_CYCLE",
                name="lifecycle_status",
                create_type=False,
            ),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column(
            "opens_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            server_default="NOT_CHECKED",
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
        sa.Column("opens_year", sa.Integer(), nullable=True, comment="Calendar year as stated"),
        sa.Column("opens_month", sa.Integer(), nullable=True, comment="1-12, only if stated"),
        sa.Column("opens_day", sa.Integer(), nullable=True, comment="1-31, only if stated"),
        sa.Column(
            "opens_month_part",
            postgresql.ENUM("EARLY", "MID", "LATE", name="month_part", create_type=False),
            nullable=True,
            comment="EARLY/MID/LATE. Metadata only; never narrows cal_range (C14)",
        ),
        sa.Column("opens_time", sa.Time(), nullable=True, comment="Local wall time"),
        sa.Column(
            "opens_timezone",
            sa.String(length=64),
            nullable=True,
            comment="IANA zone or fixed offset, exactly as the source declared it",
        ),
        sa.Column(
            "opens_precision",
            sa.String(length=16),
            sa.Computed(
                "\n    CASE\n        WHEN opens_year IS NULL           THEN NULL\n        WHEN opens_time IS NOT NULL       THEN 'DATETIME'\n        WHEN opens_day  IS NOT NULL       THEN 'DATE'\n        WHEN opens_month_part IS NOT NULL THEN 'MONTH_PART'\n        WHEN opens_month IS NOT NULL      THEN 'MONTH'\n        ELSE 'YEAR'\n    END\n",
                persisted=True,
            ),
            nullable=True,
            comment="Derived from the stored parts; never assigned. See module docstring",
        ),
        sa.Column(
            "opens_instant_utc",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Exact instant. NULL unless date + time + zone were all stated",
        ),
        sa.Column(
            "opens_cal_range",
            postgresql.DATERANGE(),
            sa.Computed(
                "\n    CASE\n        WHEN opens_year IS NULL THEN NULL\n        WHEN opens_day IS NOT NULL\n             AND opens_month BETWEEN 1 AND 12\n             AND opens_day BETWEEN 1 AND 31 THEN daterange(\n            make_date(opens_year, opens_month, opens_day),\n            make_date(opens_year, opens_month, opens_day) + 1)\n        WHEN opens_month IS NOT NULL AND opens_month BETWEEN 1 AND 12 THEN daterange(\n            make_date(opens_year, opens_month, 1),\n            make_date(opens_year + opens_month / 12, mod(opens_month, 12) + 1, 1))\n        ELSE daterange(\n            make_date(opens_year, 1, 1),\n            make_date(opens_year + 1, 1, 1))\n    END\n",
                persisted=True,
            ),
            nullable=True,
            comment="CALENDAR-LOCAL query aid. Not UTC, not source truth, never displayed",
        ),
        sa.Column(
            "opens_text",
            sa.Text(),
            nullable=True,
            comment="Verbatim source wording; the authoritative human rendering",
        ),
        sa.CheckConstraint(
            "(opens_field_status = 'PUBLISHED') OR opens_year IS NULL",
            name=op.f("ck_application_round_unpublished_open_date_has_no_value"),
        ),
        sa.CheckConstraint(
            "opens_precision IS NULL OR opens_precision IN ('DATETIME', 'DATE', 'MONTH_PART', 'MONTH', 'YEAR')",
            name=op.f("ck_application_round_opens_precision_known"),
        ),
        sa.CheckConstraint(
            "round_code <> 'INSTITUTION_DEFINED' OR round_label IS NOT NULL",
            name=op.f("ck_application_round_institution_defined_requires_label"),
        ),
        sa.CheckConstraint(
            "(opens_instant_utc IS NOT NULL) = (opens_time IS NOT NULL AND opens_timezone IS NOT NULL)",
            name=op.f("ck_application_round_opens_instant_requires_time_and_zone"),
        ),
        sa.CheckConstraint(
            "opens_day IS NULL OR (opens_day BETWEEN 1 AND 31)",
            name=op.f("ck_application_round_opens_day_range"),
        ),
        sa.CheckConstraint(
            "opens_day IS NULL OR opens_month IS NOT NULL",
            name=op.f("ck_application_round_opens_day_requires_month"),
        ),
        sa.CheckConstraint(
            "opens_day IS NULL OR opens_month IS NULL OR make_date(opens_year, opens_month, opens_day) IS NOT NULL",
            name=op.f("ck_application_round_opens_day_is_a_real_date"),
        ),
        sa.CheckConstraint(
            "opens_month IS NULL OR (opens_month BETWEEN 1 AND 12)",
            name=op.f("ck_application_round_opens_month_range"),
        ),
        sa.CheckConstraint(
            "opens_month IS NULL OR opens_year IS NOT NULL",
            name=op.f("ck_application_round_opens_month_requires_year"),
        ),
        sa.CheckConstraint(
            "opens_month_part IS NULL OR (opens_month IS NOT NULL AND opens_day IS NULL)",
            name=op.f("ck_application_round_opens_month_part_excludes_day"),
        ),
        sa.CheckConstraint(
            "opens_time IS NULL OR opens_day IS NOT NULL",
            name=op.f("ck_application_round_opens_time_requires_day"),
        ),
        sa.CheckConstraint(
            "opens_timezone IS NULL OR opens_time IS NOT NULL",
            name=op.f("ck_application_round_opens_timezone_requires_time"),
        ),
        sa.ForeignKeyConstraint(
            ["intake_id"],
            ["intake.id"],
            name=op.f("fk_application_round_intake_id_intake"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["round_code"],
            ["application_round_type.code"],
            name=op.f("fk_application_round_round_code_application_round_type"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_round")),
        sa.UniqueConstraint("id", "intake_id", name="uq_application_round_id_intake_id"),
        comment="Round within an intake. sequence_no is display order only.",
    )
    op.create_index(
        "ix_application_round_intake_id_sequence_no",
        "application_round",
        ["intake_id", "sequence_no"],
        unique=False,
    )
    op.create_index(
        "uq_application_round_intake_code",
        "application_round",
        ["intake_id", "round_code"],
        unique=True,
        postgresql_where=sa.text("round_code != 'INSTITUTION_DEFINED'"),
    )
    op.create_index(
        "uq_application_round_intake_label",
        "application_round",
        ["intake_id", "round_label_norm"],
        unique=True,
        postgresql_where=sa.text("round_code = 'INSTITUTION_DEFINED'"),
    )
    op.create_table(
        "application_deadline",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("applicant_scope_id", sa.UUID(), nullable=False),
        sa.Column(
            "deadline_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            server_default="NOT_CHECKED",
            nullable=False,
        ),
        sa.Column(
            "deadline_kind",
            postgresql.ENUM(
                "FIXED_DATE",
                "ROLLING",
                "UNTIL_FILLED",
                "NO_FIXED_DEADLINE",
                "NOT_CURRENTLY_ACCEPTING",
                name="deadline_kind",
                create_type=False,
            ),
            nullable=True,
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
        sa.Column("deadline_year", sa.Integer(), nullable=True, comment="Calendar year as stated"),
        sa.Column("deadline_month", sa.Integer(), nullable=True, comment="1-12, only if stated"),
        sa.Column("deadline_day", sa.Integer(), nullable=True, comment="1-31, only if stated"),
        sa.Column(
            "deadline_month_part",
            postgresql.ENUM("EARLY", "MID", "LATE", name="month_part", create_type=False),
            nullable=True,
            comment="EARLY/MID/LATE. Metadata only; never narrows cal_range (C14)",
        ),
        sa.Column("deadline_time", sa.Time(), nullable=True, comment="Local wall time"),
        sa.Column(
            "deadline_timezone",
            sa.String(length=64),
            nullable=True,
            comment="IANA zone or fixed offset, exactly as the source declared it",
        ),
        sa.Column(
            "deadline_precision",
            sa.String(length=16),
            sa.Computed(
                "\n    CASE\n        WHEN deadline_year IS NULL           THEN NULL\n        WHEN deadline_time IS NOT NULL       THEN 'DATETIME'\n        WHEN deadline_day  IS NOT NULL       THEN 'DATE'\n        WHEN deadline_month_part IS NOT NULL THEN 'MONTH_PART'\n        WHEN deadline_month IS NOT NULL      THEN 'MONTH'\n        ELSE 'YEAR'\n    END\n",
                persisted=True,
            ),
            nullable=True,
            comment="Derived from the stored parts; never assigned. See module docstring",
        ),
        sa.Column(
            "deadline_instant_utc",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Exact instant. NULL unless date + time + zone were all stated",
        ),
        sa.Column(
            "deadline_cal_range",
            postgresql.DATERANGE(),
            sa.Computed(
                "\n    CASE\n        WHEN deadline_year IS NULL THEN NULL\n        WHEN deadline_day IS NOT NULL\n             AND deadline_month BETWEEN 1 AND 12\n             AND deadline_day BETWEEN 1 AND 31 THEN daterange(\n            make_date(deadline_year, deadline_month, deadline_day),\n            make_date(deadline_year, deadline_month, deadline_day) + 1)\n        WHEN deadline_month IS NOT NULL AND deadline_month BETWEEN 1 AND 12 THEN daterange(\n            make_date(deadline_year, deadline_month, 1),\n            make_date(deadline_year + deadline_month / 12, mod(deadline_month, 12) + 1, 1))\n        ELSE daterange(\n            make_date(deadline_year, 1, 1),\n            make_date(deadline_year + 1, 1, 1))\n    END\n",
                persisted=True,
            ),
            nullable=True,
            comment="CALENDAR-LOCAL query aid. Not UTC, not source truth, never displayed",
        ),
        sa.Column(
            "deadline_text",
            sa.Text(),
            nullable=True,
            comment="Verbatim source wording; the authoritative human rendering",
        ),
        sa.CheckConstraint(
            "(deadline_field_status = 'PUBLISHED') = (deadline_kind IS NOT NULL)",
            name=op.f("ck_application_deadline_kind_present_iff_published"),
        ),
        sa.CheckConstraint(
            "\n            deadline_kind IS NULL OR CASE deadline_kind\n                WHEN 'FIXED_DATE'              THEN deadline_year IS NOT NULL\n                WHEN 'ROLLING'                 THEN true\n                WHEN 'UNTIL_FILLED'            THEN true\n                WHEN 'NO_FIXED_DEADLINE'       THEN deadline_year IS NULL\n                WHEN 'NOT_CURRENTLY_ACCEPTING' THEN deadline_year IS NULL\n            END\n            ",
            name=op.f("ck_application_deadline_date_matches_kind"),
        ),
        sa.CheckConstraint(
            "deadline_field_status <> 'PUBLISHED' OR deadline_text IS NOT NULL",
            name=op.f("ck_application_deadline_published_requires_official_text"),
        ),
        sa.CheckConstraint(
            "deadline_field_status = 'PUBLISHED' OR deadline_year IS NULL",
            name=op.f("ck_application_deadline_unpublished_has_no_date"),
        ),
        sa.CheckConstraint(
            "deadline_precision IS NULL OR deadline_precision IN ('DATETIME', 'DATE', 'MONTH_PART', 'MONTH', 'YEAR')",
            name=op.f("ck_application_deadline_deadline_precision_known"),
        ),
        sa.CheckConstraint(
            "(deadline_instant_utc IS NOT NULL) = (deadline_time IS NOT NULL AND deadline_timezone IS NOT NULL)",
            name=op.f("ck_application_deadline_deadline_instant_requires_time_and_zone"),
        ),
        sa.CheckConstraint(
            "deadline_day IS NULL OR (deadline_day BETWEEN 1 AND 31)",
            name=op.f("ck_application_deadline_deadline_day_range"),
        ),
        sa.CheckConstraint(
            "deadline_day IS NULL OR deadline_month IS NOT NULL",
            name=op.f("ck_application_deadline_deadline_day_requires_month"),
        ),
        sa.CheckConstraint(
            "deadline_day IS NULL OR deadline_month IS NULL OR make_date(deadline_year, deadline_month, deadline_day) IS NOT NULL",
            name=op.f("ck_application_deadline_deadline_day_is_a_real_date"),
        ),
        sa.CheckConstraint(
            "deadline_month IS NULL OR (deadline_month BETWEEN 1 AND 12)",
            name=op.f("ck_application_deadline_deadline_month_range"),
        ),
        sa.CheckConstraint(
            "deadline_month IS NULL OR deadline_year IS NOT NULL",
            name=op.f("ck_application_deadline_deadline_month_requires_year"),
        ),
        sa.CheckConstraint(
            "deadline_month_part IS NULL OR (deadline_month IS NOT NULL AND deadline_day IS NULL)",
            name=op.f("ck_application_deadline_deadline_month_part_excludes_day"),
        ),
        sa.CheckConstraint(
            "deadline_time IS NULL OR deadline_day IS NOT NULL",
            name=op.f("ck_application_deadline_deadline_time_requires_day"),
        ),
        sa.CheckConstraint(
            "deadline_timezone IS NULL OR deadline_time IS NOT NULL",
            name=op.f("ck_application_deadline_deadline_timezone_requires_time"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_scope_id"],
            ["applicant_scope.id"],
            name=op.f("fk_application_deadline_applicant_scope_id_applicant_scope"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["application_round.id"],
            name=op.f("fk_application_deadline_round_id_application_round"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_deadline")),
        sa.UniqueConstraint(
            "round_id",
            "applicant_scope_id",
            name="uq_application_deadline_round_id_applicant_scope_id",
        ),
        comment="Deadline per round x applicant scope. No invented instants (C9).",
    )
    op.create_index(
        "ix_application_deadline_round_id", "application_deadline", ["round_id"], unique=False
    )
    # ### end Alembic commands ###
    op.create_table(
        "admission_requirement",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=True),
        sa.Column("intake_id", sa.UUID(), nullable=True),
        sa.Column("applicant_scope_id", sa.UUID(), nullable=False),
        sa.Column("requirement_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "grain",
            sa.String(length=16),
            sa.Computed(
                "CASE WHEN intake_id IS NOT NULL THEN 'INTAKE' WHEN offering_id IS NOT NULL THEN 'OFFERING' ELSE 'PROGRAM' END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "requirement_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            server_default="NOT_CHECKED",
            nullable=False,
        ),
        sa.Column(
            "academic_background_text",
            sa.Text(),
            nullable=True,
            comment="Official wording, always stored",
        ),
        sa.Column("academic_structured", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("documents", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("interview_required", sa.Boolean(), nullable=True),
        sa.Column("portfolio_required", sa.Boolean(), nullable=True),
        sa.Column("official_excerpt", sa.Text(), nullable=True),
        sa.Column("official_url", sa.Text(), nullable=True),
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
            "grain IN ('PROGRAM', 'OFFERING', 'INTAKE')",
            name=op.f("ck_admission_requirement_grain_known"),
        ),
        sa.CheckConstraint(
            "requirement_field_status <> 'PUBLISHED' OR academic_background_text IS NOT NULL",
            name=op.f("ck_admission_requirement_published_requires_official_text"),
        ),
        sa.CheckConstraint(
            "intake_id IS NULL OR offering_id IS NOT NULL",
            name=op.f("ck_admission_requirement_intake_requires_offering"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_scope_id"],
            ["applicant_scope.id"],
            name=op.f("fk_admission_requirement_applicant_scope_id_applicant_scope"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id", "intake_id"],
            ["intake.offering_id", "intake.id"],
            name="fk_admission_requirement_intake",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "offering_id"],
            ["program_offering.program_id", "program_offering.id"],
            name="fk_admission_requirement_program_offering",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
            name="fk_admission_requirement_program",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admission_requirement")),
        sa.UniqueConstraint(
            "program_id",
            "offering_id",
            "intake_id",
            "applicant_scope_id",
            "requirement_kind",
            name="uq_admission_requirement_scope_kind",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Entry requirements at the narrowest correct grain (C6).",
    )
    op.create_index(
        "ix_admission_requirement_intake_id",
        "admission_requirement",
        ["intake_id"],
        unique=False,
        postgresql_where=sa.text("intake_id IS NOT NULL"),
    )
    op.create_index(
        "ix_admission_requirement_offering_id",
        "admission_requirement",
        ["offering_id"],
        unique=False,
        postgresql_where=sa.text("offering_id IS NOT NULL"),
    )
    op.create_index(
        "ix_admission_requirement_program_id_grain",
        "admission_requirement",
        ["program_id", "grain"],
        unique=False,
    )
    op.create_table(
        "language_requirement",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=True),
        sa.Column("intake_id", sa.UUID(), nullable=True),
        sa.Column("applicant_scope_id", sa.UUID(), nullable=False),
        sa.Column("test_type_code", sa.String(length=32), nullable=False),
        sa.Column(
            "grain",
            sa.String(length=16),
            sa.Computed(
                "CASE WHEN intake_id IS NOT NULL THEN 'INTAKE' WHEN offering_id IS NOT NULL THEN 'OFFERING' ELSE 'PROGRAM' END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "requirement_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            server_default="NOT_CHECKED",
            nullable=False,
        ),
        sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column(
            "subscores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Component minima exactly as published",
        ),
        sa.Column("waiver_conditions_text", sa.Text(), nullable=True),
        sa.Column("official_excerpt", sa.Text(), nullable=True),
        sa.Column("official_url", sa.Text(), nullable=True),
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
            "grain IN ('PROGRAM', 'OFFERING', 'INTAKE')",
            name=op.f("ck_language_requirement_grain_known"),
        ),
        sa.CheckConstraint(
            "intake_id IS NULL OR offering_id IS NOT NULL",
            name=op.f("ck_language_requirement_intake_requires_offering"),
        ),
        sa.CheckConstraint(
            "overall_score IS NULL OR overall_score >= 0",
            name=op.f("ck_language_requirement_overall_score_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_scope_id"],
            ["applicant_scope.id"],
            name=op.f("fk_language_requirement_applicant_scope_id_applicant_scope"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id", "intake_id"],
            ["intake.offering_id", "intake.id"],
            name="fk_language_requirement_intake",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "offering_id"],
            ["program_offering.program_id", "program_offering.id"],
            name="fk_language_requirement_program_offering",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
            name="fk_language_requirement_program",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_type_code"],
            ["test_type.code"],
            name=op.f("fk_language_requirement_test_type_code_test_type"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_language_requirement")),
        sa.UniqueConstraint(
            "program_id",
            "offering_id",
            "intake_id",
            "applicant_scope_id",
            "test_type_code",
            name="uq_language_requirement_scope_test",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Language requirements. No cross-test conversion, ever.",
    )
    op.create_index(
        "ix_language_requirement_program_id_grain",
        "language_requirement",
        ["program_id", "grain"],
        unique=False,
    )
    op.create_index(
        "ix_language_requirement_test_type_code",
        "language_requirement",
        ["test_type_code"],
        unique=False,
    )
    op.create_table(
        "tuition",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("offering_id", sa.UUID(), nullable=False),
        sa.Column("academic_year", sa.String(length=16), nullable=False),
        sa.Column("student_category_id", sa.UUID(), nullable=False),
        sa.Column(
            "amount_field_status",
            postgresql.ENUM(
                "NOT_CHECKED",
                "OFFICIALLY_NOT_PUBLISHED",
                "PUBLISHED",
                "WITHDRAWN",
                name="field_status",
                create_type=False,
            ),
            server_default="NOT_CHECKED",
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=True),
        sa.Column("billing_unit_code", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "(amount_field_status = 'PUBLISHED') = (amount IS NOT NULL)",
            name=op.f("ck_tuition_amount_matches_field_status"),
        ),
        sa.CheckConstraint(
            "academic_year ~ '^[0-9]{4}(/[0-9]{2,4})?$'",
            name=op.f("ck_tuition_academic_year_shape"),
        ),
        sa.CheckConstraint(
            "amount IS NULL OR (currency_code IS NOT NULL AND billing_unit_code IS NOT NULL)",
            name=op.f("ck_tuition_amount_requires_currency_and_billing_unit"),
        ),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0", name=op.f("ck_tuition_amount_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["billing_unit_code"],
            ["billing_unit.code"],
            name=op.f("fk_tuition_billing_unit_code_billing_unit"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currency.code"],
            name=op.f("fk_tuition_currency_code_currency"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"],
            ["program_offering.id"],
            name=op.f("fk_tuition_offering_id_program_offering"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["student_category_id"],
            ["student_category.id"],
            name=op.f("fk_tuition_student_category_id_student_category"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tuition")),
        sa.UniqueConstraint(
            "offering_id",
            "academic_year",
            "student_category_id",
            name="uq_tuition_offering_id_academic_year_student_category_id",
        ),
        comment="Fee per offering x academic year x student category.",
    )
    op.create_index(
        "ix_tuition_currency_code_amount", "tuition", ["currency_code", "amount"], unique=False
    )
    op.create_index(
        "ix_tuition_offering_id_academic_year",
        "tuition",
        ["offering_id", "academic_year"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("tuition")
    op.drop_table("language_requirement")
    op.drop_table("admission_requirement")
    op.drop_table("application_deadline")
    op.drop_table("application_round")
    op.drop_table("intake")
