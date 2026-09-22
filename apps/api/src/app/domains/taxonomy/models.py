"""Reference and controlled-vocabulary tables.

Tables, not PostgreSQL enums, because every vocabulary here must be extensible by an
administrator without a deployment (N1). Truly closed technical states live in
`app.db.enums` instead.

Small vocabularies use a stable text `code` as the primary key. That makes seeds,
partial indexes (`WHERE round_code <> 'INSTITUTION_DEFINED'`) and hand-written
maintenance SQL readable, and it removes a join from every read path. Vocabularies
that need per-destination scoping use a surrogate key because their natural key is
composite.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import SCOPE_OPERATOR, ScopeOperator
from app.db.mixins import TimestampedMixin, uuid_pk


class Destination(TimestampedMixin, Base):
    """A study destination: country or special administrative region."""

    __tablename__ = "destination"

    code: Mapped[str] = mapped_column(String(8), primary_key=True, comment="ISO 3166 alpha-2")
    name_en: Mapped[str] = mapped_column(String(128), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(128))
    is_pilot: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    rollout_wave: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("code = upper(code)", name="code_is_uppercase"),
        {"comment": "Study destinations. UK/HK/MO are the pilot scope."},
    )


class Discipline(TimestampedMixin, Base):
    """Subject taxonomy as a tree.

    Cycle prevention needs recursion and therefore lives in the service layer; the
    self-reference CHECK only catches the trivial one-row cycle.
    """

    __tablename__ = "discipline"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discipline.id", ondelete="RESTRICT")
    )
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(160))
    external_mappings: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="HECoS/JACS/CIP codes keyed by scheme"
    )

    parent: Mapped[Discipline | None] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list[Discipline]] = relationship(back_populates="parent")

    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_discipline_parent_id", "parent_id"),
        {"comment": "Hierarchical subject taxonomy (Business, CS & Data, Engineering...)"},
    )


class DegreeLevel(TimestampedMixin, Base):
    """Bachelor / master. Doctorate is reserved, not in pilot scope."""

    __tablename__ = "degree_level"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_in_scope: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = ({"comment": "Degree levels. Extensible: doctorate arrives in phase 3."},)


class IntakeSeason(TimestampedMixin, Base):
    __tablename__ = "intake_season"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(64))
    typical_start_month: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "typical_start_month IS NULL OR typical_start_month BETWEEN 1 AND 12",
            name="typical_start_month_range",
        ),
        {"comment": "Intake seasons (September, January, ...)"},
    )


class Currency(TimestampedMixin, Base):
    """ISO 4217. No conversion rates: fees are never normalised (U6)."""

    __tablename__ = "currency"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    minor_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")

    __table_args__ = (
        CheckConstraint("code = upper(code) AND length(code) = 3", name="iso4217_shape"),
        CheckConstraint("minor_units BETWEEN 0 AND 4", name="minor_units_range"),
        {"comment": "ISO 4217 currencies. Deliberately holds no exchange rates."},
    )


class BillingUnit(TimestampedMixin, Base):
    """What a tuition amount is *per*. A fee without this is meaningless."""

    __tablename__ = "billing_unit"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        {"comment": "per_year / per_credit / total_program / per_module / per_semester"},
    )


class TestType(TimestampedMixin, Base):
    """Language tests. No cross-test equivalence is ever computed (PRD section 3)."""

    __tablename__ = "test_type"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(96), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(96))
    subscore_keys: Mapped[list[str] | None] = mapped_column(
        JSONB, comment="Component names this test reports, e.g. listening/reading/..."
    )
    max_overall: Mapped[float | None] = mapped_column()

    __table_args__ = ({"comment": "IELTS, TOEFL, PTE... Never mutually convertible."},)


class StudentCategory(TimestampedMixin, Base):
    """Fee category, optionally scoped to a destination (B5).

    UK publishes Home/International; Hong Kong publishes Local/Non-local. Forcing
    one shared vocabulary would misrepresent both, so `destination_code` scopes the
    term and NULL means it applies everywhere.
    """

    __tablename__ = "student_category"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    destination_code: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("destination.code", ondelete="RESTRICT")
    )
    name_en: Mapped[str] = mapped_column(String(96), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        # NULLS NOT DISTINCT so a single global term cannot be duplicated.
        UniqueConstraint(
            "destination_code",
            "code",
            name="uq_student_category_destination_code_code",
            postgresql_nulls_not_distinct=True,
        ),
        {"comment": "Fee categories. NULL destination_code = applies to all."},
    )


class ScopeDimension(TimestampedMixin, Base):
    """A dimension an applicant scope can be expressed over (B1).

    Adding a dimension is an INSERT, not a migration. That is the point: no
    jurisdiction's admissions rules are encoded in the schema.
    """

    __tablename__ = "scope_dimension"

    code: Mapped[str] = mapped_column(String(48), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(96), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(96))
    description: Mapped[str | None] = mapped_column(Text)
    expects_group_ref: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="True when criteria on this dimension point at a qualification_group",
    )

    __table_args__ = (
        {"comment": "applicant_country, qualification_type, qualification_group, ..."},
    )


class ApplicantScope(TimestampedMixin, Base):
    """A named applicability scope: the conjunction of its criteria.

    A scope with zero criteria is the universal scope. `precedence` resolves the case
    where two scopes match one applicant (open question U1); the default policy is
    most-specific-wins by criterion count, with this column breaking ties.
    """

    __tablename__ = "applicant_scope"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(160))
    is_universal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    precedence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)

    criteria: Mapped[list[ApplicantScopeCriterion]] = relationship(
        back_populates="scope", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # "is_universal implies zero criteria" counts other rows, so it belongs to the
        # service layer (documented in ARCHITECTURE section 8.2), not to a CHECK.
        {"comment": "Named applicability scope. Zero criteria = universal."},
    )


class ApplicantScopeCriterion(TimestampedMixin, Base):
    """One criterion within a scope: (dimension, operator, value | value_ref)."""

    __tablename__ = "applicant_scope_criterion"

    id: Mapped[uuid.UUID] = uuid_pk()
    scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applicant_scope.id", ondelete="CASCADE"), nullable=False
    )
    dimension_code: Mapped[str] = mapped_column(
        String(48), ForeignKey("scope_dimension.code", ondelete="RESTRICT"), nullable=False
    )
    operator: Mapped[ScopeOperator] = mapped_column(SCOPE_OPERATOR, nullable=False)
    value: Mapped[str | None] = mapped_column(String(256))
    value_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qualification_group.id", ondelete="RESTRICT")
    )

    scope: Mapped[ApplicantScope] = relationship(back_populates="criteria")

    __table_args__ = (
        CheckConstraint(
            "(value IS NULL) <> (value_ref IS NULL)", name="exactly_one_of_value_or_ref"
        ),
        UniqueConstraint(
            "scope_id",
            "dimension_code",
            "operator",
            "value",
            "value_ref",
            # Shortened deliberately: the convention-generated name exceeds PostgreSQL's
            # 63-character identifier limit.
            name="uq_applicant_scope_criterion_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_applicant_scope_criterion_scope_id", "scope_id"),
        {"comment": "Conjunctive criteria. Extensible via scope_dimension (B1)."},
    )


class QualificationGroup(TimestampedMixin, Base):
    """A named list of institutions or qualifications, **as a source publishes it**.

    This is how an institution's own tier list is represented — as ordinary
    source-backed data with its own provenance, not as a rule baked into the schema.
    """

    __tablename__ = "qualification_group"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(200))
    published_by_university_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("university.id", ondelete="RESTRICT"),
        comment="Set when a specific institution publishes this list",
    )
    description: Mapped[str | None] = mapped_column(Text)

    members: Mapped[list[QualificationGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_qualification_group_published_by_university_id", "published_by_university_id"),
        {"comment": "Source-published lists of institutions/qualifications."},
    )


class QualificationGroupMember(TimestampedMixin, Base):
    """A member of a qualification group.

    `member_university_id` links a member that exists in our own catalog;
    `member_label` carries the source's wording for one that does not.
    """

    __tablename__ = "qualification_group_member"

    id: Mapped[uuid.UUID] = uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("qualification_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_university_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT")
    )
    member_label: Mapped[str | None] = mapped_column(String(256))
    member_code: Mapped[str | None] = mapped_column(String(96))

    group: Mapped[QualificationGroup] = relationship(back_populates="members")

    __table_args__ = (
        CheckConstraint(
            "member_university_id IS NOT NULL OR member_label IS NOT NULL",
            name="member_is_identified",
        ),
        UniqueConstraint(
            "group_id",
            "member_university_id",
            "member_label",
            name="uq_qualification_group_member_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_qualification_group_member_group_id", "group_id"),
        {"comment": "Members of a source-published qualification/institution list."},
    )


class ApplicationRoundType(TimestampedMixin, Base):
    """Controlled round codes (C5).

    `INSTITUTION_DEFINED` is the escape hatch for wording that fits no controlled
    code; rounds using it are identified by their normalised label instead (C10).
    """

    __tablename__ = "application_round_type"

    code: Mapped[str] = mapped_column(String(48), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(96), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(96))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_institution_defined: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Exactly one row should have this set: INSTITUTION_DEFINED",
    )

    __table_args__ = (
        {"comment": "ROUND_1, PRIORITY, EARLY_ACTION, MAIN, ROLLING, INSTITUTION_DEFINED..."},
    )


__all__ = [
    "ApplicantScope",
    "ApplicantScopeCriterion",
    "ApplicationRoundType",
    "BillingUnit",
    "Currency",
    "DegreeLevel",
    "Destination",
    "Discipline",
    "IntakeSeason",
    "QualificationGroup",
    "QualificationGroupMember",
    "ScopeDimension",
    "StudentCategory",
    "TestType",
]
