"""Canonical catalog: the academic hierarchy and the facts hanging off it.

    University -> Faculty/School -> Program -> Program Offering -> Intake
                                                               -> Application Round
                                                               -> Application Deadline

Everything here is a **mutable projection** of published state (architecture D9).
History lives in `entity_version` / `field_provenance`; these tables exist so that
search and API reads are fast, and they are writable only by `app_publisher`.

Two structural ideas carry most of the weight:

* **Composite foreign keys** prove containment (C6). An offering cannot belong to a
  program it is not part of, and an intake cannot belong to another offering's chain
  — PostgreSQL refuses, rather than the application remembering to check.
* **Per-fact `field_status`** (D2/D16) plus the source-date column group (D17) mean
  "the official page publishes no deadline" and "we have not looked" are different
  rows, and no date is ever invented.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import (
    ALIAS_KIND,
    DEADLINE_KIND,
    ENTITY_RELATIONSHIP_KIND,
    FIELD_STATUS,
    LIFECYCLE_STATUS,
    TUITION_AMOUNT_KIND,
    AliasKind,
    DeadlineKind,
    EntityRelationshipKind,
    FieldStatus,
    LifecycleStatus,
    TuitionAmountKind,
)
from app.db.mixins import CANONICAL_ID_PATTERN, TimestampedMixin, uuid_pk
from app.db.temporal import source_date_columns, source_date_constraints

# ---------------------------------------------------------------------------
# Institutions
# ---------------------------------------------------------------------------


class University(TimestampedMixin, Base):
    """An institution. A **versioned aggregate root** (D13).

    `canonical_id` is immutable once created (B6) — enforced by trigger, because a
    CHECK cannot see the previous value. Renames go to `entity_alias`; replacement by
    another institution goes to `entity_relationship`.
    """

    __tablename__ = "university"

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    destination_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("destination.code", ondelete="RESTRICT"), nullable=False
    )
    name_en: Mapped[str] = mapped_column(String(300), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(160))
    website_url: Mapped[str | None] = mapped_column(Text)
    institution_type: Mapped[str | None] = mapped_column(String(64))
    established_year: Mapped[int | None] = mapped_column(Integer)
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LIFECYCLE_STATUS, nullable=False, server_default=LifecycleStatus.ACTIVE.value
    )
    profile_blocks: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="Descriptive profile sections; low-risk fields"
    )
    logo_asset_key: Mapped[str | None] = mapped_column(Text)

    campuses: Mapped[list[Campus]] = relationship(back_populates="university")
    faculties: Mapped[list[Faculty]] = relationship(back_populates="university")
    programs: Mapped[list[Program]] = relationship(back_populates="university")

    __table_args__ = (
        CheckConstraint(f"canonical_id ~ '{CANONICAL_ID_PATTERN}'", name="canonical_id_shape"),
        CheckConstraint(
            "established_year IS NULL OR established_year BETWEEN 800 AND 2200",
            name="established_year_plausible",
        ),
        Index("ix_university_destination_code", "destination_code"),
        {"comment": "Institutions. Versioned root; canonical_id immutable (B6)."},
    )


class Campus(TimestampedMixin, Base):
    __tablename__ = "campus"

    id: Mapped[uuid.UUID] = uuid_pk()
    university_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(160))
    country_code: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("destination.code", ondelete="RESTRICT")
    )
    address: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LIFECYCLE_STATUS, nullable=False, server_default=LifecycleStatus.ACTIVE.value
    )

    university: Mapped[University] = relationship(back_populates="campuses")

    __table_args__ = (
        UniqueConstraint("university_id", "code", name="uq_campus_university_id_code"),
        # Needed so program_offering can prove its campus belongs to the same
        # university as its program.
        UniqueConstraint("id", "university_id", name="uq_campus_id_university_id"),
        Index("ix_campus_university_id", "university_id"),
        {"comment": "Physical campuses. A location axis referenced by offerings."},
    )


class Faculty(TimestampedMixin, Base):
    """Faculty / school / department, self-nesting.

    UCL and HKU both publish Faculty -> Department structures, so a flat list would
    lose the attribution a program actually carries.
    """

    __tablename__ = "faculty"

    id: Mapped[uuid.UUID] = uuid_pk()
    university_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT"), nullable=False
    )
    parent_faculty_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    name_en: Mapped[str] = mapped_column(String(300), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(300))
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LIFECYCLE_STATUS, nullable=False, server_default=LifecycleStatus.ACTIVE.value
    )

    university: Mapped[University] = relationship(back_populates="faculties")
    # `foreign_keys` is required, not decorative. The hierarchy is enforced by the
    # composite FK (parent_faculty_id, university_id) -> (id, university_id), so
    # without it SQLAlchemy infers that `parent`/`children` also write
    # `faculty.university_id` -- which already belongs to `university`/`faculties` --
    # and emits a "relationship will copy column ... which conflicts with" warning
    # at `configure_mappers()`. Under the suite's `filterwarnings = error` that
    # warning becomes an exception and poisons mapper configuration for the process.
    #
    # Naming the single foreign key states what is true: the parent link *is*
    # `parent_faculty_id -> id`. The `university_id` equality in the composite FK is
    # a containment invariant the database enforces, not part of the object
    # relationship, and the ORM must not try to write it from here.
    parent: Mapped[Faculty | None] = relationship(
        remote_side=[id], foreign_keys=[parent_faculty_id], back_populates="children"
    )
    children: Mapped[list[Faculty]] = relationship(
        foreign_keys=[parent_faculty_id], back_populates="parent"
    )

    __table_args__ = (
        UniqueConstraint("university_id", "code", name="uq_faculty_university_id_code"),
        UniqueConstraint("id", "university_id", name="uq_faculty_id_university_id"),
        # A parent faculty must belong to the SAME university. The composite FK makes
        # a cross-institution parent impossible.
        ForeignKeyConstraint(
            ["parent_faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_faculty_parent_faculty_id_university_id_faculty",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_faculty_id IS NULL OR parent_faculty_id <> id", name="not_own_parent"
        ),
        Index("ix_faculty_university_id", "university_id"),
        Index("ix_faculty_parent_faculty_id", "parent_faculty_id"),
        {"comment": "Faculty/school/department tree within one university."},
    )


faculty_campus = Table(
    "faculty_campus",
    Base.metadata,
    Column("faculty_id", UUID(as_uuid=True), nullable=False),
    Column("campus_id", UUID(as_uuid=True), nullable=False),
    Column("university_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("faculty_id", "campus_id", name="uq_faculty_campus_faculty_id_campus_id"),
    # Both sides must belong to the university named on the row, so a faculty cannot
    # be associated with another institution's campus.
    ForeignKeyConstraint(
        ["faculty_id", "university_id"],
        ["faculty.id", "faculty.university_id"],
        name="fk_faculty_campus_faculty_id_university_id_faculty",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["campus_id", "university_id"],
        ["campus.id", "campus.university_id"],
        name="fk_faculty_campus_campus_id_university_id_campus",
        ondelete="CASCADE",
    ),
    Index("ix_faculty_campus_campus_id", "campus_id"),
    comment="Faculty <-> campus association, constrained to one university.",
)


# ---------------------------------------------------------------------------
# Programs and offerings
# ---------------------------------------------------------------------------


class Program(TimestampedMixin, Base):
    """An academic program as the institution names it. A **versioned root** (D13)."""

    __tablename__ = "program"

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    university_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT"), nullable=False
    )
    primary_faculty_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name_en: Mapped[str] = mapped_column(String(400), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(400))
    degree_level_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("degree_level.code", ondelete="RESTRICT"), nullable=False
    )
    award_title: Mapped[str | None] = mapped_column(String(96), comment="MSc, MA, BEng, ...")
    official_program_code: Mapped[str | None] = mapped_column(String(96))
    ucas_code: Mapped[str | None] = mapped_column(String(32))
    program_url: Mapped[str | None] = mapped_column(Text)
    # Nullable, and with no default: "is this programme still running?" is a
    # high-risk fact that may simply be unknown. Defaulting it to ACTIVE would
    # publish an assertion nobody verified, and would make lifecycle_field_status
    # decorative because the CHECK below could never be false.
    lifecycle_status: Mapped[LifecycleStatus | None] = mapped_column(LIFECYCLE_STATUS)
    lifecycle_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )
    lifecycle_effective_from: Mapped[date | None] = mapped_column(Date)

    university: Mapped[University] = relationship(back_populates="programs")
    offerings: Mapped[list[ProgramOffering]] = relationship(back_populates="program")

    __table_args__ = (
        CheckConstraint(f"canonical_id ~ '{CANONICAL_ID_PATTERN}'", name="canonical_id_shape"),
        # Program closure is a high-risk fact, so value and status must agree (D14).
        CheckConstraint(
            "(lifecycle_field_status = 'PUBLISHED') = (lifecycle_status IS NOT NULL)",
            name="lifecycle_status_matches_field_status",
        ),
        UniqueConstraint("id", "university_id", name="uq_program_id_university_id"),
        ForeignKeyConstraint(
            ["primary_faculty_id", "university_id"],
            ["faculty.id", "faculty.university_id"],
            name="fk_program_primary_faculty_id_university_id_faculty",
            ondelete="RESTRICT",
        ),
        Index("ix_program_university_id", "university_id"),
        Index("ix_program_degree_level_code", "degree_level_code"),
        {"comment": "Programs. Versioned root; canonical_id immutable (B6)."},
    )


program_faculty = Table(
    "program_faculty",
    Base.metadata,
    Column("program_id", UUID(as_uuid=True), nullable=False),
    Column("faculty_id", UUID(as_uuid=True), nullable=False),
    Column("university_id", UUID(as_uuid=True), nullable=False),
    Column("is_primary", Boolean, nullable=False, server_default="false"),
    UniqueConstraint("program_id", "faculty_id", name="uq_program_faculty_program_id_faculty_id"),
    ForeignKeyConstraint(
        ["program_id", "university_id"],
        ["program.id", "program.university_id"],
        name="fk_program_faculty_program_id_university_id_program",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["faculty_id", "university_id"],
        ["faculty.id", "faculty.university_id"],
        name="fk_program_faculty_faculty_id_university_id_faculty",
        ondelete="RESTRICT",
    ),
    Index("ix_program_faculty_faculty_id", "faculty_id"),
    comment="Jointly-run programs. Both sides pinned to one university.",
)


program_discipline = Table(
    "program_discipline",
    Base.metadata,
    Column(
        "program_id",
        UUID(as_uuid=True),
        ForeignKey("program.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "discipline_id",
        UUID(as_uuid=True),
        ForeignKey("discipline.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("is_primary", Boolean, nullable=False, server_default="false"),
    UniqueConstraint(
        "program_id", "discipline_id", name="uq_program_discipline_program_id_discipline_id"
    ),
    Index("ix_program_discipline_discipline_id", "discipline_id"),
    comment="Program <-> discipline mapping for filtering.",
)


class ProgramOffering(TimestampedMixin, Base):
    """A deliverable variant of a program (B2, C8).

    A one-year full-time MSc and a two-year part-time MSc have different fees and
    sometimes different intakes, so they are different offerings — collapsing them
    into the program row would publish a fee that applies to neither.

    Its dimensions are **governed, source-backed facts** (C8): they determine
    admissions, tuition and comparison behaviour, so they carry provenance like any
    other published field. They are also identity-bearing and therefore NOT NULL —
    an offering with an unknown study mode is not an offering.
    """

    __tablename__ = "program_offering"

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_id: Mapped[str] = mapped_column(String(240), nullable=False, unique=True)
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    university_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    study_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    duration_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    campus_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="NULL for fully online delivery"
    )
    # Nullable for the same reason as Program.lifecycle_status: an unverified
    # offering must not be recorded as ACTIVE.
    lifecycle_status: Mapped[LifecycleStatus | None] = mapped_column(LIFECYCLE_STATUS)
    lifecycle_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )

    program: Mapped[Program] = relationship(back_populates="offerings")
    intakes: Mapped[list[Intake]] = relationship(back_populates="offering")

    __table_args__ = (
        CheckConstraint(f"canonical_id ~ '{CANONICAL_ID_PATTERN}'", name="canonical_id_shape"),
        CheckConstraint("duration_value > 0", name="duration_is_positive"),
        CheckConstraint(
            "study_mode IN ('FULL_TIME', 'PART_TIME', 'FLEXIBLE')", name="study_mode_known"
        ),
        CheckConstraint(
            "delivery_mode IN ('ON_CAMPUS', 'ONLINE', 'HYBRID', 'DISTANCE')",
            name="delivery_mode_known",
        ),
        CheckConstraint(
            "duration_unit IN ('YEAR', 'MONTH', 'SEMESTER', 'TERM')", name="duration_unit_known"
        ),
        CheckConstraint(
            "(lifecycle_field_status = 'PUBLISHED') = (lifecycle_status IS NOT NULL)",
            name="lifecycle_status_matches_field_status",
        ),
        # Natural key. NULLS NOT DISTINCT so a second fully-online offering with the
        # same dimensions collides instead of silently duplicating.
        UniqueConstraint(
            "program_id",
            "study_mode",
            "campus_id",
            "delivery_mode",
            "duration_value",
            "duration_unit",
            name="uq_program_offering_dimensions",
            postgresql_nulls_not_distinct=True,
        ),
        # Prerequisite for the nested-ownership FKs on requirements (C6).
        UniqueConstraint("id", "program_id", name="uq_program_offering_id_program_id"),
        ForeignKeyConstraint(
            ["program_id", "university_id"],
            ["program.id", "program.university_id"],
            name="fk_program_offering_program_id_university_id_program",
            ondelete="CASCADE",
        ),
        # The campus must belong to the same university as the program.
        ForeignKeyConstraint(
            ["campus_id", "university_id"],
            ["campus.id", "campus.university_id"],
            name="fk_program_offering_campus_id_university_id_campus",
            ondelete="RESTRICT",
        ),
        Index("ix_program_offering_program_id", "program_id"),
        Index("ix_program_offering_campus_id", "campus_id"),
        {"comment": "Deliverable variant: study mode x campus x delivery x duration."},
    )


# ---------------------------------------------------------------------------
# Intakes, rounds, deadlines
# ---------------------------------------------------------------------------


class Intake(TimestampedMixin, Base):
    """An admission cycle for one offering."""

    __tablename__ = "intake"

    id: Mapped[uuid.UUID] = uuid_pk()
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("program_offering.id", ondelete="CASCADE"),
        nullable=False,
    )
    academic_year: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="As published, e.g. 2027/28"
    )
    intake_season_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("intake_season.code", ondelete="RESTRICT"), nullable=False
    )
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LIFECYCLE_STATUS, nullable=False, server_default=LifecycleStatus.ACTIVE.value
    )

    offering: Mapped[ProgramOffering] = relationship(back_populates="intakes")
    rounds: Mapped[list[ApplicationRound]] = relationship(back_populates="intake")

    __table_args__ = (
        UniqueConstraint(
            "offering_id",
            "academic_year",
            "intake_season_code",
            name="uq_intake_offering_id_academic_year_intake_season_code",
        ),
        # Prerequisite for the nested-ownership FKs on requirements (C6).
        UniqueConstraint("id", "offering_id", name="uq_intake_id_offering_id"),
        CheckConstraint("academic_year ~ '^[0-9]{4}(/[0-9]{2,4})?$'", name="academic_year_shape"),
        Index("ix_intake_offering_id", "offering_id"),
        {"comment": "Admission cycle: offering x academic year x season."},
    )


class ApplicationRound(TimestampedMixin, Base):
    """A round within an intake (C5, C10).

    Identity is deliberately split in two: a controlled `round_code` occurs once per
    intake, while `INSTITUTION_DEFINED` rounds are identified by their normalised
    label so an intake may hold several with different wording.

    `sequence_no` orders a display list and appears in **no** unique constraint. The
    earlier `UNIQUE (intake_id, round_code, sequence_no) NULLS NOT DISTINCT` was
    wrong: it forbade exactly the multiple-institution-defined case above.
    """

    __tablename__ = "application_round"

    id: Mapped[uuid.UUID] = uuid_pk()
    intake_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intake.id", ondelete="CASCADE"), nullable=False
    )
    round_code: Mapped[str] = mapped_column(
        String(48), ForeignKey("application_round_type.code", ondelete="RESTRICT"), nullable=False
    )
    round_label: Mapped[str | None] = mapped_column(
        String(256), comment="The institution's own wording, verbatim"
    )
    round_label_norm: Mapped[str | None] = mapped_column(
        String(256),
        Computed("lower(regexp_replace(btrim(round_label), '\\s+', ' ', 'g'))", persisted=True),
        comment="Normalised label; identity for INSTITUTION_DEFINED rounds (C10)",
    )
    sequence_no: Mapped[int | None] = mapped_column(
        Integer, comment="Display order ONLY. Never identity (C10)."
    )
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LIFECYCLE_STATUS, nullable=False, server_default=LifecycleStatus.ACTIVE.value
    )
    opens_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )

    intake: Mapped[Intake] = relationship(back_populates="rounds")
    deadlines: Mapped[list[ApplicationDeadline]] = relationship(back_populates="round")

    __table_args__ = (
        *source_date_columns("opens"),
        *source_date_constraints("opens"),
        CheckConstraint(
            "round_code <> 'INSTITUTION_DEFINED' OR round_label IS NOT NULL",
            name="institution_defined_requires_label",
        ),
        CheckConstraint(
            "(opens_field_status = 'PUBLISHED') OR opens_year IS NULL",
            name="unpublished_open_date_has_no_value",
        ),
        UniqueConstraint("id", "intake_id", name="uq_application_round_id_intake_id"),
        # The two identity rules are partial unique INDEXES, created in the index
        # migration -- a UniqueConstraint cannot carry a WHERE clause.
        Index(
            "uq_application_round_intake_code",
            "intake_id",
            "round_code",
            unique=True,
            postgresql_where=Column("round_code") != "INSTITUTION_DEFINED",
        ),
        Index(
            "uq_application_round_intake_label",
            "intake_id",
            "round_label_norm",
            unique=True,
            postgresql_where=Column("round_code") == "INSTITUTION_DEFINED",
        ),
        Index("ix_application_round_intake_id_sequence_no", "intake_id", "sequence_no"),
        {"comment": "Round within an intake. sequence_no is display order only."},
    )


class ApplicationDeadline(TimestampedMixin, Base):
    """A closing statement for one round and one applicant scope (B4, C4, C9).

    The deadline fact is the triple (kind, date parts, official text) and carries one
    `field_status` for the whole fact (D16). A date is optional: `ROLLING` and
    `UNTIL_FILLED` may or may not name a cut-off, and `NO_FIXED_DEADLINE` /
    `NOT_CURRENTLY_ACCEPTING` never do.

    Campus is deliberately absent from the key: it is determined by the offering that
    owns the intake (B4).
    """

    __tablename__ = "application_deadline"

    id: Mapped[uuid.UUID] = uuid_pk()
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application_round.id", ondelete="CASCADE"), nullable=False
    )
    applicant_scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="RESTRICT"), nullable=False
    )
    deadline_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )
    deadline_kind: Mapped[DeadlineKind | None] = mapped_column(DEADLINE_KIND)

    round: Mapped[ApplicationRound] = relationship(back_populates="deadlines")

    __table_args__ = (
        *source_date_columns("deadline"),
        *source_date_constraints("deadline"),
        UniqueConstraint(
            "round_id",
            "applicant_scope_id",
            name="uq_application_deadline_round_id_applicant_scope_id",
        ),
        # The kind is known exactly when the fact is published.
        CheckConstraint(
            "(deadline_field_status = 'PUBLISHED') = (deadline_kind IS NOT NULL)",
            name="kind_present_iff_published",
        ),
        # An unpublished fact carries no date.
        CheckConstraint(
            "deadline_field_status = 'PUBLISHED' OR deadline_year IS NULL",
            name="unpublished_has_no_date",
        ),
        # Per-kind date requirement. Note what is NOT here: no rule forces a date on
        # ROLLING or UNTIL_FILLED, because institutions publish those both ways.
        CheckConstraint(
            """
            deadline_kind IS NULL OR CASE deadline_kind
                WHEN 'FIXED_DATE'              THEN deadline_year IS NOT NULL
                WHEN 'ROLLING'                 THEN true
                WHEN 'UNTIL_FILLED'            THEN true
                WHEN 'NO_FIXED_DEADLINE'       THEN deadline_year IS NULL
                WHEN 'NOT_CURRENTLY_ACCEPTING' THEN deadline_year IS NULL
            END
            """,
            name="date_matches_kind",
        ),
        # For every non-date kind the wording IS the value, so require it whenever
        # the fact is published.
        CheckConstraint(
            "deadline_field_status <> 'PUBLISHED' OR deadline_text IS NOT NULL",
            name="published_requires_official_text",
        ),
        Index("ix_application_deadline_round_id", "round_id"),
        {"comment": "Deadline per round x applicant scope. No invented instants (C9)."},
    )


# ---------------------------------------------------------------------------
# Requirements — nested ownership with enforced containment (C6)
# ---------------------------------------------------------------------------


def _nested_ownership_args(table: str) -> tuple[object, ...]:
    """Constraints shared by both requirement tables.

    The three levels are nested, not disjoint, so "exactly one owner" is the wrong
    shape: the grain is the deepest non-null level. Composite foreign keys make
    PostgreSQL prove the offering belongs to the program and the intake belongs to
    that offering.
    """
    return (
        ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
            name=f"fk_{table}_program",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["program_id", "offering_id"],
            ["program_offering.program_id", "program_offering.id"],
            name=f"fk_{table}_program_offering",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["offering_id", "intake_id"],
            ["intake.offering_id", "intake.id"],
            name=f"fk_{table}_intake",
            ondelete="CASCADE",
        ),
        # An intake without an offering is unrepresentable.
        CheckConstraint(
            "intake_id IS NULL OR offering_id IS NOT NULL", name="intake_requires_offering"
        ),
        CheckConstraint("grain IN ('PROGRAM', 'OFFERING', 'INTAKE')", name="grain_known"),
    )


class AdmissionRequirement(TimestampedMixin, Base):
    """Academic entry requirements (B1, C6).

    `applicant_scope_id` is part of the key because UK institutions routinely publish
    different requirements per applicant country and qualification. The official
    wording is always retained: structuring a requirement never replaces it.
    """

    __tablename__ = "admission_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    offering_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    intake_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    applicant_scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="RESTRICT"), nullable=False
    )
    requirement_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # Generated text, not an enum: PostgreSQL requires generated expressions to be
    # IMMUTABLE and the text-to-enum cast (`enum_in`) is only STABLE. A CHECK
    # constrains the values; `RequirementGrain` stays the application vocabulary.
    grain: Mapped[str] = mapped_column(
        String(16),
        Computed(
            "CASE WHEN intake_id IS NOT NULL THEN 'INTAKE' "
            "WHEN offering_id IS NOT NULL THEN 'OFFERING' "
            "ELSE 'PROGRAM' END",
            persisted=True,
        ),
        nullable=False,
    )
    requirement_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )
    academic_background_text: Mapped[str | None] = mapped_column(
        Text, comment="Official wording, always stored"
    )
    academic_structured: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    documents: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    interview_required: Mapped[bool | None] = mapped_column(Boolean)
    portfolio_required: Mapped[bool | None] = mapped_column(Boolean)
    official_excerpt: Mapped[str | None] = mapped_column(Text)
    official_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        *_nested_ownership_args("admission_requirement"),
        UniqueConstraint(
            "program_id",
            "offering_id",
            "intake_id",
            "applicant_scope_id",
            "requirement_kind",
            name="uq_admission_requirement_scope_kind",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "requirement_field_status <> 'PUBLISHED' OR academic_background_text IS NOT NULL",
            name="published_requires_official_text",
        ),
        Index("ix_admission_requirement_program_id_grain", "program_id", "grain"),
        Index(
            "ix_admission_requirement_offering_id",
            "offering_id",
            postgresql_where=Column("offering_id").isnot(None),
        ),
        Index(
            "ix_admission_requirement_intake_id",
            "intake_id",
            postgresql_where=Column("intake_id").isnot(None),
        ),
        {"comment": "Entry requirements at the narrowest correct grain (C6)."},
    )


class LanguageRequirement(TimestampedMixin, Base):
    """Language test requirements.

    Subscores are stored as published. **No equivalence between tests is ever
    computed** — that inference belongs to admissions officers, not to this platform.
    """

    __tablename__ = "language_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    offering_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    intake_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    applicant_scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="RESTRICT"), nullable=False
    )
    test_type_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("test_type.code", ondelete="RESTRICT"), nullable=False
    )
    # Generated text, not an enum: PostgreSQL requires generated expressions to be
    # IMMUTABLE and the text-to-enum cast (`enum_in`) is only STABLE. A CHECK
    # constrains the values; `RequirementGrain` stays the application vocabulary.
    grain: Mapped[str] = mapped_column(
        String(16),
        Computed(
            "CASE WHEN intake_id IS NOT NULL THEN 'INTAKE' "
            "WHEN offering_id IS NOT NULL THEN 'OFFERING' "
            "ELSE 'PROGRAM' END",
            persisted=True,
        ),
        nullable=False,
    )
    requirement_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    subscores: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="Component minima exactly as published"
    )
    waiver_conditions_text: Mapped[str | None] = mapped_column(Text)
    official_excerpt: Mapped[str | None] = mapped_column(Text)
    official_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        *_nested_ownership_args("language_requirement"),
        UniqueConstraint(
            "program_id",
            "offering_id",
            "intake_id",
            "applicant_scope_id",
            "test_type_code",
            name="uq_language_requirement_scope_test",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "overall_score IS NULL OR overall_score >= 0", name="overall_score_non_negative"
        ),
        Index("ix_language_requirement_program_id_grain", "program_id", "grain"),
        Index("ix_language_requirement_test_type_code", "test_type_code"),
        {"comment": "Language requirements. No cross-test conversion, ever."},
    )


# ---------------------------------------------------------------------------
# Tuition
# ---------------------------------------------------------------------------


class Tuition(TimestampedMixin, Base):
    """A published fee (B5, D16, U14).

    An amount without a currency and a billing unit is not a fee, it is a number, so
    the CHECK refuses it. Nothing is ever converted between currencies.

    WHY THERE IS NO SINGLE `amount`
    ===============================
    There was one, and it could not represent what universities actually publish.
    "GBP 28,000-32,000 depending on pathway" is a *published* fee with no scalar; so
    is "from GBP 24,500" and "fees vary by module selection". A single
    `Numeric` column offered three ways to record those, and all three were wrong:
    invent a number (the collector picks the low end and it is published as exact),
    call it `OFFICIALLY_NOT_PUBLISHED` (false -- the page does publish), or leave it
    `NOT_CHECKED` (also false).

    So the shape of the amount is itself recorded. `amount_kind` says what the page
    published; `amount_min`/`amount_max` carry the endpoints it gave, and no more.

    ===========  ==========  ==========  ==================================
    amount_kind  amount_min  amount_max  meaning
    ===========  ==========  ==========  ==================================
    EXACT        required    required    equal; one stated figure
    RANGE        required    required    min <= max; both stated
    FROM         required    NULL        "from GBP 24,500"
    UP_TO        NULL        required    "up to GBP 9,250"
    VARIABLE     NULL ok     NULL ok     stated, but not as a figure;
                                         `official_text` is then mandatory
    ===========  ==========  ==========  ==================================

    **No midpoint is ever derived for a RANGE.** A fee of "28,000-32,000" is not
    30,000, and a consultant quoting 30,000 to a family would be quoting a number no
    university published. Any averaging is a presentation choice for a query layer
    that must show its working, never a stored value.

    `amount_kind` is **not** a field status. "The university publishes no fee" stays
    `amount_field_status = OFFICIALLY_NOT_PUBLISHED` with no kind at all; `VARIABLE`
    means the opposite -- the page *does* address fees, just not numerically.
    """

    __tablename__ = "tuition"

    id: Mapped[uuid.UUID] = uuid_pk()
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("program_offering.id", ondelete="CASCADE"),
        nullable=False,
    )
    academic_year: Mapped[str] = mapped_column(String(16), nullable=False)
    student_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("student_category.id", ondelete="RESTRICT"), nullable=False
    )
    amount_field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )
    amount_kind: Mapped[TuitionAmountKind | None] = mapped_column(
        TUITION_AMOUNT_KIND,
        comment="What shape the official page published the fee in (U14)",
    )
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency_code: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("currency.code", ondelete="RESTRICT")
    )
    billing_unit_code: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("billing_unit.code", ondelete="RESTRICT")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    official_url: Mapped[str | None] = mapped_column(Text)
    official_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "offering_id",
            "academic_year",
            "student_category_id",
            name="uq_tuition_offering_id_academic_year_student_category_id",
        ),
        # A published fee states a shape; an unpublished one states nothing.
        CheckConstraint(
            "(amount_field_status = 'PUBLISHED') = (amount_kind IS NOT NULL)",
            name="amount_kind_matches_field_status",
        ),
        CheckConstraint(
            "amount_kind IS NOT NULL OR (amount_min IS NULL AND amount_max IS NULL)",
            name="no_amounts_without_a_kind",
        ),
        CheckConstraint(
            "amount_kind <> 'EXACT' OR (amount_min IS NOT NULL AND amount_max IS NOT NULL "
            "AND amount_min = amount_max)",
            name="exact_has_equal_endpoints",
        ),
        CheckConstraint(
            "amount_kind <> 'RANGE' OR (amount_min IS NOT NULL AND amount_max IS NOT NULL "
            "AND amount_min <= amount_max)",
            name="range_is_ordered",
        ),
        CheckConstraint(
            "amount_kind <> 'FROM' OR (amount_min IS NOT NULL AND amount_max IS NULL)",
            name="from_has_only_a_minimum",
        ),
        CheckConstraint(
            "amount_kind <> 'UP_TO' OR (amount_min IS NULL AND amount_max IS NOT NULL)",
            name="up_to_has_only_a_maximum",
        ),
        # VARIABLE may carry no figure at all, so the wording is the fact.
        CheckConstraint(
            "amount_kind <> 'VARIABLE' OR btrim(coalesce(official_text, '')) <> ''",
            name="variable_states_its_wording",
        ),
        # The rule that makes a bare number unrepresentable.
        CheckConstraint(
            "(amount_min IS NULL AND amount_max IS NULL) "
            "OR (currency_code IS NOT NULL AND billing_unit_code IS NOT NULL)",
            name="amounts_require_currency_and_billing_unit",
        ),
        CheckConstraint(
            "(amount_min IS NULL OR amount_min >= 0) AND (amount_max IS NULL OR amount_max >= 0)",
            name="amounts_non_negative",
        ),
        CheckConstraint("academic_year ~ '^[0-9]{4}(/[0-9]{2,4})?$'", name="academic_year_shape"),
        Index("ix_tuition_offering_id_academic_year", "offering_id", "academic_year"),
        # Filtering "fees under X" is a range question now, so the index covers both
        # endpoints rather than one scalar.
        Index("ix_tuition_currency_code_amounts", "currency_code", "amount_min", "amount_max"),
        {"comment": "Fee per offering x academic year x student category. Ranges are U14."},
    )


# ---------------------------------------------------------------------------
# Rankings — schema only, feature-gated (D8)
# ---------------------------------------------------------------------------


class RankingPublisher(TimestampedMixin, Base):
    __tablename__ = "ranking_publisher"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = ({"comment": "QS, THE, ARWU... No data seeded without a licence."},)


class RankingEdition(TimestampedMixin, Base):
    """One published ranking edition.

    `display_allowed` is the gate: with no valid authorisation the API omits the
    field and the console hides the route (D8). Ingestion is equally gated, so
    unlicensed data never lands in the first place.
    """

    __tablename__ = "ranking_edition"

    id: Mapped[uuid.UUID] = uuid_pk()
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ranking_publisher.id", ondelete="RESTRICT"), nullable=False
    )
    ranking_name: Mapped[str] = mapped_column(String(200), nullable=False)
    edition_year: Mapped[int] = mapped_column(Integer, nullable=False)
    methodology_version: Mapped[str | None] = mapped_column(String(96))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: C27. The recorded licence. Previously an unconstrained UUID, so "authorised"
    #: could name an authorisation that did not exist; now a real foreign key, and a
    #: trigger additionally requires it to be live and display-allowed before any
    #: ranking fact may cite the source.
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_authorization.id", ondelete="RESTRICT")
    )
    display_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="D8 gate; default closed"
    )
    licence_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "publisher_id",
            "ranking_name",
            "edition_year",
            name="uq_ranking_edition_publisher_id_ranking_name_edition_year",
        ),
        CheckConstraint("edition_year BETWEEN 1900 AND 2200", name="edition_year_plausible"),
        # Display requires a recorded authorisation. Whether that authorisation is
        # still valid is time-dependent and checked at read time.
        CheckConstraint(
            "display_allowed = false OR authorization_id IS NOT NULL",
            name="display_requires_authorization",
        ),
        {"comment": "Ranking edition. display_allowed defaults false (D8)."},
    )


class RankingEntry(TimestampedMixin, Base):
    __tablename__ = "ranking_entry"

    id: Mapped[uuid.UUID] = uuid_pk()
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ranking_edition.id", ondelete="CASCADE"), nullable=False
    )
    university_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="CASCADE"), nullable=False
    )
    discipline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discipline.id", ondelete="RESTRICT")
    )
    rank_value: Mapped[int | None] = mapped_column(Integer)
    rank_low: Mapped[int | None] = mapped_column(Integer)
    rank_high: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    is_tied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        UniqueConstraint(
            "edition_id",
            "university_id",
            "discipline_id",
            name="uq_ranking_entry_edition_id_university_id_discipline_id",
            postgresql_nulls_not_distinct=True,
        ),
        # Either an exact rank or a band, not both and not neither.
        CheckConstraint(
            "(rank_value IS NOT NULL) <> (rank_low IS NOT NULL AND rank_high IS NOT NULL)",
            name="exact_rank_or_band",
        ),
        CheckConstraint(
            "rank_low IS NULL OR rank_high IS NULL OR rank_low <= rank_high",
            name="band_is_ordered",
        ),
        Index("ix_ranking_entry_university_id", "university_id"),
        {"comment": "A university's placement in one ranking edition."},
    )


# ---------------------------------------------------------------------------
# Identity graph
# ---------------------------------------------------------------------------


class EntityAlias(TimestampedMixin, Base):
    """Former names, trade names, abbreviations and external ids (B6).

    A rename adds a row here; it never changes `canonical_id`. A mutable projection:
    the authoritative history of a name change is the entity's version record.
    """

    __tablename__ = "entity_alias"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alias_kind: Mapped[AliasKind] = mapped_column(ALIAS_KIND, nullable=False)
    value: Mapped[str] = mapped_column(String(400), nullable=False)
    locale: Mapped[str | None] = mapped_column(String(16))
    external_system: Mapped[str | None] = mapped_column(String(96))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "alias_kind",
            "value",
            "locale",
            name="uq_entity_alias_entity_type_entity_id_alias_kind_value_locale",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="validity_is_ordered",
        ),
        CheckConstraint(
            "alias_kind <> 'EXTERNAL_ID' OR external_system IS NOT NULL",
            name="external_id_names_its_system",
        ),
        Index("ix_entity_alias_entity_type_entity_id", "entity_type", "entity_id"),
        {"comment": "Aliases. A rename never changes canonical_id (B6)."},
    )


class EntityRelationship(Base):
    """Append-only assertion that one real entity replaced another (B6).

    Immutable: a supersession is a historical statement, and retracting it would
    rewrite the record. A mistake is corrected by asserting the inverse with a later
    `effective_from`, which leaves both statements visible.
    """

    __tablename__ = "entity_relationship"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    to_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_kind: Mapped[EntityRelationshipKind] = mapped_column(
        ENTITY_RELATIONSHIP_KIND, nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("from_entity_id <> to_entity_id", name="no_self_relationship"),
        UniqueConstraint(
            "entity_type",
            "from_entity_id",
            "to_entity_id",
            "relationship_kind",
            "effective_from",
            name="uq_entity_relationship_identity",
        ),
        Index("ix_entity_relationship_from", "entity_type", "from_entity_id"),
        Index("ix_entity_relationship_to", "entity_type", "to_entity_id"),
        {"comment": "APPEND-ONLY. SUPERSEDED_BY / MERGED_INTO / SPLIT_INTO (B6)."},
    )


class FactAbsence(TimestampedMixin, Base):
    """ "The official source publishes no facts of this kind here" (D15).

    An inline `field_status` can say a field is unpublished, but it cannot describe a
    row that does not exist — and "this offering has no published international fee"
    is the more common case.
    """

    __tablename__ = "fact_absence"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    collection_path: Mapped[str] = mapped_column(String(200), nullable=False)
    applicant_scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="RESTRICT")
    )
    field_status: Mapped[FieldStatus] = mapped_column(FIELD_STATUS, nullable=False)
    official_url: Mapped[str | None] = mapped_column(Text)
    official_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "collection_path",
            "applicant_scope_id",
            name="uq_fact_absence_entity_collection_scope",
            postgresql_nulls_not_distinct=True,
        ),
        # Only absence statuses belong here; a present value belongs in its own table.
        CheckConstraint(
            "field_status IN ('OFFICIALLY_NOT_PUBLISHED', 'NOT_CHECKED', 'WITHDRAWN')",
            name="only_absence_statuses",
        ),
        Index("ix_fact_absence_entity_type_entity_id", "entity_type", "entity_id"),
        {"comment": "Explicit absence of a whole fact collection (D15)."},
    )


__all__ = [
    "AdmissionRequirement",
    "ApplicationDeadline",
    "ApplicationRound",
    "Campus",
    "EntityAlias",
    "EntityRelationship",
    "FactAbsence",
    "Faculty",
    "Intake",
    "LanguageRequirement",
    "Program",
    "ProgramOffering",
    "RankingEdition",
    "RankingEntry",
    "RankingPublisher",
    "Tuition",
    "University",
    "faculty_campus",
    "program_discipline",
    "program_faculty",
]
