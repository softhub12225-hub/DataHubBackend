"""Domain group 3: programs and offerings

Program, its faculty/discipline associations, and the offering layer (B2, C8).

A one-year full-time MSc and a two-year part-time MSc have different fees and
sometimes different intakes, so they are different offerings. Collapsing them into
the program row would publish a fee that applies to neither.

Offering dimensions are governed, source-backed facts and therefore NOT NULL: an
offering with an unknown study mode is not an offering.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "program",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("canonical_id", sa.String(length=200), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("primary_faculty_id", sa.UUID(), nullable=True),
        sa.Column("name_en", sa.String(length=400), nullable=False),
        sa.Column("name_zh", sa.String(length=400), nullable=True),
        sa.Column("degree_level_code", sa.String(length=32), nullable=False),
        sa.Column("award_title", sa.String(length=96), nullable=True, comment="MSc, MA, BEng, ..."),
        sa.Column("official_program_code", sa.String(length=96), nullable=True),
        sa.Column("ucas_code", sa.String(length=32), nullable=True),
        sa.Column("program_url", sa.Text(), nullable=True),
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
            nullable=True,
        ),
        sa.Column(
            "lifecycle_field_status",
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
        sa.Column("lifecycle_effective_from", sa.Date(), nullable=True),
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
            "(lifecycle_field_status = 'PUBLISHED') = (lifecycle_status IS NOT NULL)",
            name=op.f("ck_program_lifecycle_status_matches_field_status"),
        ),
        sa.CheckConstraint(
            "canonical_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name=op.f("ck_program_canonical_id_shape")
        ),
        sa.ForeignKeyConstraint(
            ["degree_level_code"],
            ["degree_level.code"],
            name=op.f("fk_program_degree_level_code_degree_level"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["primary_faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_program_primary_faculty_id_university_id_faculty",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_program_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_program")),
        sa.UniqueConstraint("canonical_id", name=op.f("uq_program_canonical_id")),
        sa.UniqueConstraint("id", "university_id", name="uq_program_id_university_id"),
        comment="Programs. Versioned root; canonical_id immutable (B6).",
    )
    op.create_index("ix_program_degree_level_code", "program", ["degree_level_code"], unique=False)
    op.create_index("ix_program_university_id", "program", ["university_id"], unique=False)
    op.create_table(
        "program_faculty",
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("faculty_id", sa.UUID(), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(
            ["faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_program_faculty_faculty_id_university_id_faculty",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "university_id"],
            ["program.id", "program.university_id"],
            name="fk_program_faculty_program_id_university_id_program",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "program_id", "faculty_id", name="uq_program_faculty_program_id_faculty_id"
        ),
        comment="Jointly-run programs. Both sides pinned to one university.",
    )
    op.create_index(
        "ix_program_faculty_faculty_id", "program_faculty", ["faculty_id"], unique=False
    )
    op.create_table(
        "program_discipline",
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("discipline_id", sa.UUID(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(
            ["discipline_id"],
            ["discipline.id"],
            name=op.f("fk_program_discipline_discipline_id_discipline"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
            name=op.f("fk_program_discipline_program_id_program"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "program_id", "discipline_id", name="uq_program_discipline_program_id_discipline_id"
        ),
        comment="Program <-> discipline mapping for filtering.",
    )
    op.create_index(
        "ix_program_discipline_discipline_id", "program_discipline", ["discipline_id"], unique=False
    )
    op.create_table(
        "program_offering",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("canonical_id", sa.String(length=240), nullable=False),
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("study_mode", sa.String(length=32), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("duration_value", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("duration_unit", sa.String(length=16), nullable=False),
        sa.Column("campus_id", sa.UUID(), nullable=True, comment="NULL for fully online delivery"),
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
            nullable=True,
        ),
        sa.Column(
            "lifecycle_field_status",
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
        sa.CheckConstraint(
            "(lifecycle_field_status = 'PUBLISHED') = (lifecycle_status IS NOT NULL)",
            name=op.f("ck_program_offering_lifecycle_status_matches_field_status"),
        ),
        sa.CheckConstraint(
            "canonical_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_program_offering_canonical_id_shape"),
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('ON_CAMPUS', 'ONLINE', 'HYBRID', 'DISTANCE')",
            name=op.f("ck_program_offering_delivery_mode_known"),
        ),
        sa.CheckConstraint(
            "duration_unit IN ('YEAR', 'MONTH', 'SEMESTER', 'TERM')",
            name=op.f("ck_program_offering_duration_unit_known"),
        ),
        sa.CheckConstraint(
            "study_mode IN ('FULL_TIME', 'PART_TIME', 'FLEXIBLE')",
            name=op.f("ck_program_offering_study_mode_known"),
        ),
        sa.CheckConstraint(
            "duration_value > 0", name=op.f("ck_program_offering_duration_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["campus_id", "university_id"],
            ["campus.id", "campus.university_id"],
            name="fk_program_offering_campus_id_university_id_campus",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "university_id"],
            ["program.id", "program.university_id"],
            name="fk_program_offering_program_id_university_id_program",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_program_offering")),
        sa.UniqueConstraint("canonical_id", name=op.f("uq_program_offering_canonical_id")),
        sa.UniqueConstraint("id", "program_id", name="uq_program_offering_id_program_id"),
        sa.UniqueConstraint(
            "program_id",
            "study_mode",
            "campus_id",
            "delivery_mode",
            "duration_value",
            "duration_unit",
            name="uq_program_offering_dimensions",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Deliverable variant: study mode x campus x delivery x duration.",
    )
    op.create_index(
        "ix_program_offering_campus_id", "program_offering", ["campus_id"], unique=False
    )
    op.create_index(
        "ix_program_offering_program_id", "program_offering", ["program_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("program_offering")
    op.drop_table("program_discipline")
    op.drop_table("program_faculty")
    op.drop_table("program")
