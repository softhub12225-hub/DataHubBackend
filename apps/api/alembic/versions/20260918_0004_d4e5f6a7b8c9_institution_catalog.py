"""Domain group 2: institutions

University, campus and the faculty tree.

`faculty.parent_faculty_id` uses a **composite** foreign key to `(id, university_id)`
so a faculty cannot be nested under another institution's faculty. The same trick
gives `faculty_campus` its guarantee: both sides are pinned to the university named
on the row.

`university.canonical_id` is immutable once created (B6); the trigger that enforces
that is installed with the privileges revision.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "university",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("canonical_id", sa.String(length=160), nullable=False),
        sa.Column("destination_code", sa.String(length=8), nullable=False),
        sa.Column("name_en", sa.String(length=300), nullable=False),
        sa.Column("name_zh", sa.String(length=300), nullable=True),
        sa.Column("city", sa.String(length=160), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("institution_type", sa.String(length=64), nullable=True),
        sa.Column("established_year", sa.Integer(), nullable=True),
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
            "profile_blocks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Descriptive profile sections; low-risk fields",
        ),
        sa.Column("logo_asset_key", sa.Text(), nullable=True),
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
            "canonical_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_university_canonical_id_shape"),
        ),
        sa.CheckConstraint(
            "established_year IS NULL OR established_year BETWEEN 800 AND 2200",
            name=op.f("ck_university_established_year_plausible"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_university_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_university")),
        sa.UniqueConstraint("canonical_id", name=op.f("uq_university_canonical_id")),
        comment="Institutions. Versioned root; canonical_id immutable (B6).",
    )
    op.create_index(
        "ix_university_destination_code", "university", ["destination_code"], unique=False
    )
    op.create_table(
        "campus",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=False),
        sa.Column("name_zh", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=160), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["country_code"],
            ["destination.code"],
            name=op.f("fk_campus_country_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_campus_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campus")),
        sa.UniqueConstraint("id", "university_id", name="uq_campus_id_university_id"),
        sa.UniqueConstraint("university_id", "code", name="uq_campus_university_id_code"),
        comment="Physical campuses. A location axis referenced by offerings.",
    )
    op.create_index("ix_campus_university_id", "campus", ["university_id"], unique=False)
    op.create_table(
        "faculty",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column("parent_faculty_id", sa.UUID(), nullable=True),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("name_en", sa.String(length=300), nullable=False),
        sa.Column("name_zh", sa.String(length=300), nullable=True),
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
            "parent_faculty_id IS NULL OR parent_faculty_id <> id",
            name=op.f("ck_faculty_not_own_parent"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_faculty_parent_faculty_id_university_id_faculty",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_faculty_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faculty")),
        sa.UniqueConstraint("id", "university_id", name="uq_faculty_id_university_id"),
        sa.UniqueConstraint("university_id", "code", name="uq_faculty_university_id_code"),
        comment="Faculty/school/department tree within one university.",
    )
    op.create_index("ix_faculty_parent_faculty_id", "faculty", ["parent_faculty_id"], unique=False)
    op.create_index("ix_faculty_university_id", "faculty", ["university_id"], unique=False)
    op.create_table(
        "faculty_campus",
        sa.Column("faculty_id", sa.UUID(), nullable=False),
        sa.Column("campus_id", sa.UUID(), nullable=False),
        sa.Column("university_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campus_id", "university_id"],
            ["campus.id", "campus.university_id"],
            name="fk_faculty_campus_campus_id_university_id_campus",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_faculty_campus_faculty_id_university_id_faculty",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "faculty_id", "campus_id", name="uq_faculty_campus_faculty_id_campus_id"
        ),
        comment="Faculty <-> campus association, constrained to one university.",
    )
    op.create_index("ix_faculty_campus_campus_id", "faculty_campus", ["campus_id"], unique=False)

    # Deferred from the reference_taxonomy revision: these reference `university`,
    # which this revision has just created.
    op.create_foreign_key(
        op.f("fk_qualification_group_published_by_university_id_university"),
        "qualification_group",
        "university",
        ["published_by_university_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_qualification_group_member_member_university_id_university"),
        "qualification_group_member",
        "university",
        ["member_university_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_qualification_group_member_member_university_id_university"),
        "qualification_group_member",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_qualification_group_published_by_university_id_university"),
        "qualification_group",
        type_="foreignkey",
    )
    op.drop_table("faculty_campus")
    op.drop_table("faculty")
    op.drop_table("campus")
    op.drop_table("university")
