"""Manual collection staging plane (U12).

WHY A SEPARATE PLANE
====================
A hand-filled workbook is not evidence, and the schema should not let it pretend to
be. `field_claim` means *this extractor read this value out of this snapshot of this
page*, and it can prove it: `field_claim.extraction_id` → `extraction.snapshot_id` →
`snapshot.source_id`/`content_hash` → `content_blob`, every link NOT NULL. A person
typing a fee into Excel produces none of that. Routing the workbook into `field_claim`
would have meant either inventing a fetch that never happened, or loosening a chain
whose whole value is that it cannot be loosened.

So the workbook lands here instead, in its own tables, with its own vocabulary, and
with no route onward that does not pass through real acquisition.

WHAT THIS PLANE IS
==================
**A human collection artifact that points at official sources.** It records what a
named person read on a named page on a named date. That is genuinely useful — it is
how we learn *where to look* for the pilot universities — and it is not a published
about any of them.

Four things follow, and all four are enforced rather than intended:

* Staging rows are **not publication eligible**. No `pilot_*` table is reachable from
  `field_provenance` by any foreign key, so a staged row cannot be named as
  provenance. A test enumerates the FK graph to prove it stays that way.
* Staging rows **cannot become a `field_claim`** without real acquisition. There is no
  column that would let them: a claim needs an extraction, which needs a snapshot,
  which needs a fetch of a registered source.
* Staging rows **cannot modify canonical facts**. `app_publisher`, the only role that
  may write the canonical plane, holds no privilege here at all — not even `SELECT`,
  for the same reason it lost its read on the onboarding tables in C27.
* A collected URL is **never born verified**. `pilot_collected_source` starts at
  `PENDING` and reaching `VERIFIED` is a recorded human act that still does not make
  it an official source; the existing domain and mapping workflow has to run (U15).

PHYSICAL URL vs CLAIMED RESPONSIBILITY
=====================================
These are two different things and the schema keeps them apart.

A `pilot_collected_source` row is **one claimed responsibility**: the collector says
*this URL is where you find this category of information for this institution*. Two
rows may carry the same URL, because one admissions page really does answer for
undergraduate admissions, entry requirements and application deadlines at once — in
the supplied 35-university file, 66 of 385 URL cells repeat a URL already given under
another heading.

`duplicate_of_source_ref` names the row that first registered that URL, so **physical
identity is explicit**: the rows with a NULL there are the distinct pages, and a
partial unique index enforces one per URL per institution. Acquisition later fetches
those once; review still happens per responsibility, because "this page is on the
university's domain" and "this page is the authority for tuition" are different
questions and only the second is answered per category.

SUBMISSION-LOCAL REFERENCES
===========================
`program_ref` (`P0001`) and `source_ref` (`S0001`) are workbook-local. They are
**never** global identifiers, and the schema says so structurally: both definition
tables are keyed on `(submission_id, ref)`, and the fact table reaches them through a
composite foreign key on the same pair. A second workbook may legitimately reuse
`P0001` for a different programme, and no constraint here is even tempted to conflate
them.

RE-IMPORT
=========
A submission is never overwritten. `file_sha256` is unique, so re-importing identical
bytes is recognised and does nothing; a corrected workbook is a *new* submission
sitting beside the old one, and both stay queryable so the two can be compared. That
is the same rule `target_list` already follows, for the same reason: the history of
what we were given is not ours to edit.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import (
    FIELD_STATUS,
    PILOT_FACT_VALIDATION_STATE,
    PILOT_IMPORT_STATUS,
    SOURCE_CANDIDATE_STATE,
    TUITION_AMOUNT_KIND,
    UNCLASSIFIED_SOURCE_TYPE,
    FieldStatus,
    PilotFactValidationState,
    PilotImportStatus,
    PilotSubmissionKind,
    SourceCandidateState,
    SourceCategory,
    TuitionAmountKind,
)
from app.db.mixins import RecordedAtMixin, TimestampedMixin, uuid_pk

#: `None` -> SQL NULL rather than the JSON literal `null` (C25).
NullableJsonb = JSONB(none_as_null=True)

#: What a staged responsibility may claim: every canonical source category, plus the
#: staging-only `UNCLASSIFIED` for a page supplied without one. Ordered so the CHECK
#: this builds is stable across migrations.
STAGED_SOURCE_TYPES: tuple[str, ...] = (
    *sorted(category.value for category in SourceCategory),
    UNCLASSIFIED_SOURCE_TYPE,
)

#: Which fact sheet a staged row came from. Kept as a CHECK rather than an enum: these
#: are sheet names in a file format the client can change, not closed technical states.
PILOT_FACT_TYPES: tuple[str, ...] = (
    "ADMISSION_REQUIREMENT",
    "LANGUAGE_REQUIREMENT",
    "TUITION",
    "DEADLINE",
    "PROGRAM",
    "UNIVERSITY_PROFILE",
)


class PilotSubmission(TimestampedMixin, Base):
    """One returned workbook. Never overwritten (U12).

    `file_sha256` is the idempotence key: re-importing identical bytes is recognised
    and writes nothing. A corrected workbook has different bytes and therefore becomes
    a new submission, with the previous one kept and queryable so the two can be
    compared.
    """

    __tablename__ = "pilot_submission"

    id: Mapped[uuid.UUID] = uuid_pk()
    file_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="sha256 of the workbook as supplied"
    )
    original_filename: Mapped[str] = mapped_column(String(400), nullable=False)
    file_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Which template shape the file was filled in against, so a future importer can
    #: tell a stale workbook from a current one rather than guessing from columns.
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: What kind of artifact this is. An official-source list names the pilot and its
    #: acquisition targets; a collection workbook reports what was read.
    submission_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=PilotSubmissionKind.COLLECTION_WORKBOOK.value
    )
    #: True when this file *defines* which institutions the pilot covers. Only an
    #: official-source list may claim it, and a facts workbook must not: otherwise a
    #: workbook that happened to omit a university would silently drop it from scope.
    defines_pilot_scope: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the client says they finished it, if stated"
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    imported_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    selected_university_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_university_count: Mapped[int | None] = mapped_column(
        Integer, comment="What the client was asked for; checked, never used to choose"
    )
    import_status: Mapped[PilotImportStatus] = mapped_column(
        PILOT_IMPORT_STATUS, nullable=False, server_default=PilotImportStatus.VALIDATED.value
    )
    #: The validator's report, stored verbatim. A submission that was accepted with
    #: warnings should still say what they were, years later.
    validation_summary: Mapped[dict[str, object] | None] = mapped_column(NullableJsonb)
    notes: Mapped[str | None] = mapped_column(Text)

    selected_universities: Mapped[list[PilotSelectedUniversity]] = relationship(
        back_populates="submission"
    )
    programs: Mapped[list[PilotCollectedProgram]] = relationship(back_populates="submission")
    sources: Mapped[list[PilotCollectedSource]] = relationship(back_populates="submission")
    facts: Mapped[list[PilotCollectedFact]] = relationship(back_populates="submission")

    __table_args__ = (
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_is_hex"),
        CheckConstraint("file_byte_size > 0", name="file_byte_size_positive"),
        CheckConstraint(
            "selected_university_count >= 0", name="selected_university_count_non_negative"
        ),
        CheckConstraint(
            "submission_kind IN ('OFFICIAL_SOURCE_LIST', 'COLLECTION_WORKBOOK')",
            name="submission_kind_known",
        ),
        CheckConstraint(
            "NOT defines_pilot_scope OR submission_kind = 'OFFICIAL_SOURCE_LIST'",
            name="only_a_source_list_defines_scope",
        ),
        Index("ix_pilot_submission_imported_at", "imported_at"),
        {
            "comment": (
                "One returned collection workbook. A human artifact pointing at official "
                "sources -- never evidence, never publication eligible (U12)."
            )
        },
    )


class PilotSelectedUniversity(RecordedAtMixin, Base):
    """The `Pilot_Universities` sheet, one row per institution. APPEND-ONLY.

    This is where the client's pilot choice is recorded, and where the collector's
    answer to "what is this institution actually called?" lands.

    **It writes no `university` row, and that is deliberate.** `university.name_en` is
    one of the eight governed columns (C27): writing it requires a `field_provenance`
    row citing an eligible source for that exact string, in the same transaction. The
    sheet collects a name with no source and no status, so there is nothing to cite.
    The name stays here until identity resolution -- which `target_institution`
    already models as a recorded human act, through `matched_university_id` /
    `matched_at` / `matched_by`.

    `is_selected` carries the decision; `selection_value` keeps the cell as written,
    because "YES" and "Y" and a stray "x" are all things a person types and the
    difference between them is occasionally the interesting part.
    """

    __tablename__ = "pilot_selected_university"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pilot_submission.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    sheet_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selection_value: Mapped[str | None] = mapped_column(
        String(32), comment="The `selected` cell exactly as the collector wrote it"
    )
    #: The collector's claim about the official name. Not `university.name_en`.
    official_name_en: Mapped[str | None] = mapped_column(String(300))
    official_name_zh: Mapped[str | None] = mapped_column(String(300))
    official_homepage: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    collector_notes: Mapped[str | None] = mapped_column(Text)

    submission: Mapped[PilotSubmission] = relationship(back_populates="selected_universities")

    __table_args__ = (
        CheckConstraint("sheet_row_no >= 2", name="sheet_row_no_is_a_data_row"),
        # Step 4 requirement 15 again: a workbook cannot introduce a non-HTTP URL.
        CheckConstraint(
            "official_homepage IS NULL OR official_homepage ~* '^https?://'",
            name="official_homepage_is_http",
        ),
        # A selected institution must be reachable: we are about to schedule
        # acquisition against it, and a row with no homepage names nothing.
        #
        # It deliberately does NOT require `official_name_en`. The 35-university
        # source list carries the *client's list* label ("exact original QS
        # target-list labels", per its own README), and writing that into a column
        # meaning "what the institution calls itself" is the confusion D18 exists to
        # prevent. The generic collection template still asks for a real official
        # name, and its validator still reports a missing one — that is a template
        # rule, which is the right place for it.
        CheckConstraint(
            "NOT is_selected OR official_homepage IS NOT NULL",
            name="selected_is_reachable",
        ),
        Index(
            "ix_pilot_selected_university_selected",
            "submission_id",
            "is_selected",
        ),
        {
            "comment": (
                "APPEND-ONLY. The pilot selection and the collector's official-name "
                "claim. Creates no `university` row (U12, C27)."
            )
        },
    )


class PilotCollectedProgram(RecordedAtMixin, Base):
    """A programme as one workbook described it. APPEND-ONLY.

    Keyed on `(submission_id, program_ref)`, which is what makes `P0001`
    submission-local *structurally* rather than by convention. The fact table reaches
    it by composite foreign key on the same pair, so a fact row can never cite a
    programme from a different workbook.

    This is **not** a `program`. It carries no canonical id and creates none;
    reconciling it to a real programme is a later, separate act.
    """

    __tablename__ = "pilot_collected_program"

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pilot_submission.id", ondelete="CASCADE"),
        nullable=False,
    )
    program_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sheet_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    program_name_en: Mapped[str] = mapped_column(String(400), nullable=False)
    degree_level_code: Mapped[str | None] = mapped_column(String(32))
    discipline_code: Mapped[str | None] = mapped_column(String(64))
    #: The university's own wording, kept exactly. The controlled taxonomy has three
    #: top-level codes; this is where the real subject lives until someone maps it.
    discipline_hint: Mapped[str | None] = mapped_column(String(200))
    faculty_or_school: Mapped[str | None] = mapped_column(String(300))
    study_mode: Mapped[str | None] = mapped_column(String(32))
    delivery_mode: Mapped[str | None] = mapped_column(String(32))
    duration_value: Mapped[int | None] = mapped_column(Integer)
    duration_unit: Mapped[str | None] = mapped_column(String(16))
    campus_name: Mapped[str | None] = mapped_column(String(200))
    lifecycle_status: Mapped[str | None] = mapped_column(String(32))
    collector_notes: Mapped[str | None] = mapped_column(Text)
    #: Set only when a human later reconciles this to a real programme. Never set by
    #: the importer.
    reconciled_program_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("program.id", ondelete="RESTRICT")
    )

    submission: Mapped[PilotSubmission] = relationship(back_populates="programs")

    __table_args__ = (
        # What the composite foreign key from `pilot_collected_fact` resolves
        # against. This is the real key; `id` exists only so an audit entry and a
        # reconciliation record can name a row with one value.
        UniqueConstraint(
            "submission_id", "program_ref", name="uq_pilot_collected_program_submission_ref"
        ),
        CheckConstraint("program_ref ~ '^P[0-9]{4}$'", name="program_ref_shape"),
        CheckConstraint("sheet_row_no >= 2", name="sheet_row_no_is_a_data_row"),
        CheckConstraint("btrim(program_name_en) <> ''", name="program_name_en_is_not_blank"),
        Index("ix_pilot_collected_program_target", "target_institution_id", "submission_id"),
        {"comment": "APPEND-ONLY. A programme as one workbook described it. Not a `program`."},
    )


class PilotCollectedSource(TimestampedMixin, Base):
    """One claimed source responsibility: this URL, for this category (U12 + U15).

    Mutable, unlike the other staging tables, because its whole purpose is to carry a
    verification decision that changes: `PENDING` → `VERIFIED` / `REJECTED` /
    `NEEDS_REVIEW`. The decisions themselves are appended to the audit chain, so the
    history is not lost by the row being updated.

    **Reaching `VERIFIED` here does not make this an official source.** It means a
    human looked at the URL and thinks it is worth registering. Registering it still
    goes through `official_domain` verification and `source_mapping` promotion, and a
    `source` is still never born `OFFICIAL_VERIFIED` (C27). The collector's opinion
    that a URL is official is collection metadata, not the system's decision.
    """

    __tablename__ = "pilot_collected_source"

    #: A surrogate key, and not decoration: `audit_log.object_id` is a single uuid, so
    #: a row identified only by `(submission_id, source_ref)` could not be named in
    #: the audit entry recording its verification. The composite pair remains UNIQUE.
    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pilot_submission.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sheet_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The category the collector claims this page answers for, or `UNCLASSIFIED`
    #: when they supplied a page without saying. Free text rather than the
    #: `source_category` enum because `UNCLASSIFIED` is not a kind of page — see
    #: `UNCLASSIFIED_SOURCE_TYPE`; a CHECK holds it to the known set.
    source_type: Mapped[str] = mapped_column(String(48), nullable=False)
    degree_scope: Mapped[str | None] = mapped_column(String(32))
    #: Which workbook column supplied this row. Import lineage: it is how a reviewer
    #: answers "where did this claim come from?" months later, and how a duplicate
    #: additional-source URL stays visible as something the client did supply.
    workbook_column: Mapped[str | None] = mapped_column(String(80))
    #: The row that first registered this exact URL for this institution, when this
    #: one repeats it. NULL means *this* row is the physical page.
    #:
    #: This is what stops a repeated URL becoming two acquisition targets while still
    #: preserving both claimed responsibilities. Acquisition follows the NULL rows;
    #: review follows every row.
    duplicate_of_source_ref: Mapped[str | None] = mapped_column(String(16))
    official_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    #: The date the collector says they read the page. Their claim, not ours.
    checked_at: Mapped[date | None] = mapped_column(Date)
    is_third_party: Mapped[bool | None] = mapped_column(Boolean)
    collector_notes: Mapped[str | None] = mapped_column(Text)

    verification_state: Mapped[SourceCandidateState] = mapped_column(
        SOURCE_CANDIDATE_STATE,
        nullable=False,
        server_default=SourceCandidateState.PENDING.value,
        comment="U15 triage. Never set to anything but PENDING by an import.",
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    verification_reason: Mapped[str | None] = mapped_column(Text)
    #: Set when the candidate is eventually registered. Until then the URL exists only
    #: as a collector's suggestion.
    promoted_source_mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_mapping.id", ondelete="RESTRICT"), unique=True
    )
    #: Step 5B. The acquisition target this claim rides on -- the `source` registered
    #: for its physical page. Every responsibility claiming the same URL points at the
    #: same source, which is how "one fetch, several claims" stays queryable in both
    #: directions: the scheduler reads sources, a reviewer reads back to the workbook.
    #:
    #: Deliberately **not** `source_mapping`: a mapping requires a verified host
    #: (`source_mapping_requires_trusted_host`), and nothing is verified yet. A source
    #: existing is not a trust claim, so this link carries lineage without implying any.
    acquisition_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT")
    )

    submission: Mapped[PilotSubmission] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint(
            "submission_id", "source_ref", name="uq_pilot_collected_source_submission_ref"
        ),
        CheckConstraint("source_ref ~ '^S[0-9]{4}$'", name="source_ref_shape"),
        # A typo in a category is a responsibility claim nobody can act on, and it
        # would be discovered only when a reviewer went looking for the page.
        CheckConstraint(
            "source_type IN (" + ", ".join(f"'{value}'" for value in STAGED_SOURCE_TYPES) + ")",
            name="source_type_known",
        ),
        CheckConstraint("sheet_row_no >= 2", name="sheet_row_no_is_a_data_row"),
        # Step 4 requirement 15, unchanged: only http/https may be stored at all, so a
        # workbook cannot smuggle another scheme past the fetch-time guard.
        CheckConstraint("official_url ~* '^https?://'", name="official_url_is_http"),
        CheckConstraint("normalized_url ~ '^https?://'", name="normalized_url_is_http"),
        CheckConstraint("url_sha256 ~ '^[0-9a-f]{64}$'", name="url_sha256_is_hex"),
        CheckConstraint("host = lower(host)", name="host_is_lowercase"),
        # Leaving PENDING is a recorded human act with a reason.
        CheckConstraint(
            "verification_state = 'PENDING' "
            "OR (verified_at IS NOT NULL AND verified_by IS NOT NULL "
            "    AND btrim(coalesce(verification_reason, '')) <> '')",
            name="decision_records_who_when_why",
        ),
        # A candidate can only be registered once a human has accepted it -- and even
        # then it becomes a CANDIDATE mapping, never a verified source.
        CheckConstraint(
            "promoted_source_mapping_id IS NULL OR verification_state = 'VERIFIED'",
            name="only_verified_is_registered",
        ),
        # A repeated URL is now legitimate, so the old `UNIQUE (submission, url)` is
        # gone: it asserted one URL = one responsibility, which the real file
        # disproves 66 times. Physical uniqueness is enforced instead by the partial
        # index `ix_pilot_collected_source_physical` (migration-owned), over the rows
        # that are not marked as duplicates.
        ForeignKeyConstraint(
            ["submission_id", "duplicate_of_source_ref"],
            ["pilot_collected_source.submission_id", "pilot_collected_source.source_ref"],
            name="fk_pilot_collected_source_duplicate_of",
        ),
        CheckConstraint(
            "duplicate_of_source_ref IS NULL OR duplicate_of_source_ref <> source_ref",
            name="a_row_is_not_its_own_duplicate",
        ),
        CheckConstraint(
            "duplicate_of_source_ref IS NULL OR duplicate_of_source_ref ~ '^S[0-9]{4}$'",
            name="duplicate_of_source_ref_shape",
        ),
        # Rejecting an unclassified page is fine -- "not a page we want" needs no
        # category. Verifying one is not: verification asserts the page is
        # authoritative *for something*, and there is nothing yet to be authoritative
        # for. Classification is a separate recorded act (Step 5A section 5).
        CheckConstraint(
            "verification_state <> 'VERIFIED' OR source_type <> 'UNCLASSIFIED'",
            name="unclassified_is_not_verifiable",
        ),
        Index("ix_pilot_collected_source_state", "verification_state"),
        Index("ix_pilot_collected_source_url", "submission_id", "url_sha256"),
        Index("ix_pilot_collected_source_acquisition", "acquisition_source_id"),
        Index("ix_pilot_collected_source_host", "host"),
        Index("ix_pilot_collected_source_target", "target_institution_id", "verification_state"),
        {
            "comment": (
                "One claimed source responsibility. Starts PENDING; VERIFIED here "
                "means 'worth registering', never 'official' (U15)."
            )
        },
    )


class PilotCollectedFact(RecordedAtMixin, Base):
    """One fact row from one workbook, as supplied. APPEND-ONLY.

    Deliberately wide and mostly nullable. The point is to preserve what a person
    wrote, including the parts that do not fit the canonical model yet — the scope
    hints, the verbatim wording, the collector's note about a page that would not
    load. Normalising those away at import time would discard exactly the material a
    reconciler needs.

    `collected_values` holds the sheet's own structured columns as JSONB (an amount
    kind and its endpoints, a test code and its score, a deadline's calendar parts),
    because the four fact sheets have genuinely different shapes and four near-empty
    typed tables would be worse than one honest document column. The typed columns
    that *are* here are the ones every sheet shares and every reconciler filters on.
    """

    __tablename__ = "pilot_collected_fact"

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pilot_submission.id", ondelete="CASCADE"), nullable=False
    )
    sheet_name: Mapped[str] = mapped_column(String(64), nullable=False)
    sheet_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    program_ref: Mapped[str | None] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(16))

    fact_type: Mapped[str] = mapped_column(String(48), nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    field_status: Mapped[FieldStatus] = mapped_column(
        FIELD_STATUS, nullable=False, server_default=FieldStatus.NOT_CHECKED.value
    )

    #: The sheet's structured columns, verbatim.
    collected_values: Mapped[dict[str, object] | None] = mapped_column(NullableJsonb)
    #: Typed copies of the two tuition columns worth filtering on before
    #: reconciliation, so "which fees did they find?" needs no JSONB casting.
    amount_kind: Mapped[TuitionAmountKind | None] = mapped_column(TUITION_AMOUNT_KIND)
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    official_text: Mapped[str | None] = mapped_column(
        Text, comment="The official wording, pasted by the collector"
    )
    source_url: Mapped[str | None] = mapped_column(
        Text, comment="As supplied, for readability. source_ref is the link that resolves."
    )

    applicant_scope_hint: Mapped[str | None] = mapped_column(String(300))
    applicant_country_code: Mapped[str | None] = mapped_column(String(8))
    qualification_hint: Mapped[str | None] = mapped_column(String(300))

    collector_notes: Mapped[str | None] = mapped_column(Text)
    validation_state: Mapped[PilotFactValidationState] = mapped_column(
        PILOT_FACT_VALIDATION_STATE,
        nullable=False,
        server_default=PilotFactValidationState.OK.value,
    )

    submission: Mapped[PilotSubmission] = relationship(back_populates="facts")

    __table_args__ = (
        # Submission-local references, enforced structurally. A fact row physically
        # cannot cite a programme or a source from another workbook.
        ForeignKeyConstraint(
            ["submission_id", "program_ref"],
            ["pilot_collected_program.submission_id", "pilot_collected_program.program_ref"],
            name="fk_pilot_collected_fact_submission_id_program_ref",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_id", "source_ref"],
            ["pilot_collected_source.submission_id", "pilot_collected_source.source_ref"],
            name="fk_pilot_collected_fact_submission_id_source_ref",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "submission_id",
            "sheet_name",
            "sheet_row_no",
            name="uq_pilot_collected_fact_submission_sheet_row",
        ),
        CheckConstraint(
            "fact_type IN (" + ", ".join(f"'{value}'" for value in PILOT_FACT_TYPES) + ")",
            name="fact_type_known",
        ),
        CheckConstraint("sheet_row_no >= 2", name="sheet_row_no_is_a_data_row"),
        CheckConstraint(
            "program_ref IS NULL OR program_ref ~ '^P[0-9]{4}$'", name="program_ref_shape"
        ),
        CheckConstraint(
            "source_ref IS NULL OR source_ref ~ '^S[0-9]{4}$'", name="source_ref_shape"
        ),
        # A collector's "the page says nothing here" is only meaningful if it names
        # the page, and a PUBLISHED row must carry something.
        CheckConstraint(
            "field_status NOT IN ('PUBLISHED', 'OFFICIALLY_NOT_PUBLISHED') "
            "OR source_ref IS NOT NULL",
            name="asserted_row_cites_a_source_ref",
        ),
        Index("ix_pilot_collected_fact_submission_sheet", "submission_id", "sheet_name"),
        Index("ix_pilot_collected_fact_target", "target_institution_id", "fact_type"),
        Index("ix_pilot_collected_fact_validation_state", "validation_state"),
        {
            "comment": (
                "APPEND-ONLY. One workbook row as supplied. Not evidence, not "
                "publication eligible, and unreachable from field_provenance (U12)."
            )
        },
    )


__all__ = [
    "PILOT_FACT_TYPES",
    "STAGED_SOURCE_TYPES",
    "PilotCollectedFact",
    "PilotCollectedProgram",
    "PilotCollectedSource",
    "PilotSelectedUniversity",
    "PilotSubmission",
]
