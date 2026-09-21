"""Target onboarding plane: *which* institutions we were asked to cover, and how we
established where their official information actually lives.

This is a **fourth plane**, sitting beside Evidence, Governance and Canonical. It
exists because the client's scope and the institutional truth are different kinds of
fact, and the platform's central rule collapses if they are stored together.

QS SEPARATION -- THE POINT OF THIS MODULE
=========================================
The client supplied a QS-derived spreadsheet. It answers exactly one question:
**which universities are in project scope.** It is authoritative for that and for
nothing else.

Accordingly, every QS-originated value in this module lives on `target_list_entry`,
an append-only record of what one version of one spreadsheet said. There is
deliberately no path from here into `field_claim`, `change_proposal` or any
canonical table:

* `target_institution` carries **no** descriptive attributes -- no name we would
  publish, no city, no website. Its QS name lives on the import entry and is used
  only for matching and for display inside the onboarding console.
* `qs_rank` / `qs_score` are on the entry, tied to `target_list.list_version`. They
  reach `ranking_entry` only through an explicit, licence-gated mapping step (D8 /
  Step 4 requirement 12) that is not implemented here and must not be inferred.
* Nothing in this module writes to the canonical plane, and no role holds both the
  read on these tables and the write on the canonical ones: `app_publisher` has no
  privilege here at all (C27 revoked its SELECT too), and the roles that may write
  here cannot write `university`.

  That separation is necessary and it is **not sufficient**, which an earlier version
  of this docstring got wrong. Grants classify tables; "this value may set scope but
  may not become a fact" is a statement about a value's origin. The rule is enforced
  by `source.publication_eligibility` and the triggers on `field_claim` and
  `field_provenance` -- see `app.db.enums.PublicationEligibility` and C27.

A university becomes a real, publishable entity only by the ordinary route --
official sources, extraction, claims, review, publication -- and `matched_university_id`
records *that this target corresponds to that entity*, never *that QS described it*.

TARGET RECORD vs CANONICAL IDENTITY
===================================
Two separate things, kept separate (Step 4 requirement 2):

* `target_institution` -- **client scope**. "We were asked to cover this." Durable
  across list versions, carries onboarding progress, never carries a published fact.
* `university` -- **verified institutional identity**, in `domains/catalog`. Exists
  only once someone confirmed what the institution actually is.

A target with `matched_university_id IS NULL` is normal and is not a defect: it means
identity resolution has not concluded.

WHY THE LIST IS SPLIT INTO TWO TABLES
=====================================
The specification's recommended `target_institution` shape carries `list_version`
alongside `onboarding_status`. Implemented literally, an institution appearing in
both QS 2027 and QS 2028 would become two rows, and its onboarding progress,
verified domains and matched university would fork -- so re-importing a list would
either duplicate work or silently discard it.

Split instead into:

* `target_list_entry` -- immutable, one row per (list version, spreadsheet row).
  What that file said, preserved verbatim. Never updated, never deleted.
* `target_institution` -- one row per institution in scope, carrying governance
  state only.

Every field the specification names is present; `list_version`, `source_row`,
`qs_name`, `qs_rank`, `qs_score`, `region` and `country_territory` are reached
through the entry, which is also what makes the required difference report and
multi-version history possible at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.enums import (
    ACQUISITION_FETCH_STRATEGY,
    DEGREE_SCOPE,
    DOMAIN_VERIFICATION_METHOD,
    OFFICIAL_VERIFICATION_STATUS,
    ONBOARDING_STATUS,
    SOURCE_ACCESS_STATE,
    SOURCE_CATEGORY,
    TARGET_CHANGE_KIND,
    AcquisitionFetchStrategy,
    DegreeScope,
    DomainVerificationMethod,
    OfficialVerificationStatus,
    OnboardingStatus,
    SourceAccessState,
    SourceCategory,
    TargetChangeKind,
)
from app.db.mixins import RecordedAtMixin, TimestampedMixin, uuid_pk

#: JSONB that maps Python ``None`` onto SQL ``NULL`` rather than onto the JSON
#: literal ``null``.
#:
#: SQLAlchemy's default is the opposite, and the difference is not cosmetic here.
#: `target_list_diff` has a CHECK requiring ``before_value IS NULL`` for an addition;
#: with the default, a Python ``None`` is stored as JSON ``null``, which is a
#: perfectly good non-NULL value, and every addition violates the constraint. More
#: generally "there is no before value" is an absence, and storing an absence as a
#: JSON ``null`` makes it indistinguishable from a source that published null.
#:
#: This is a bind-parameter behaviour only: the emitted DDL is plain ``JSONB``, so it
#: introduces no migration and no autogenerate drift.
NullableJsonb = JSONB(none_as_null=True)

#: Collection frequencies an onboarding operator may pre-select. Mirrors
#: `source.crawl_frequency` so promotion into the evidence plane is a copy, not a
#: translation.
COLLECTION_FREQUENCIES = (
    "HIGH_RISK_3X_DAILY",
    "DAILY",
    "WEEKLY",
    "MONTHLY",
    "EVENT_DRIVEN",
)

#: `AcquisitionFetchStrategy` -> `source.fetch_strategy`, applied when a verified
#: mapping is promoted to a registered source. Only `HTTP` differs by name.
FETCH_STRATEGY_TO_SOURCE = {
    AcquisitionFetchStrategy.HTTP: "STATIC",
    AcquisitionFetchStrategy.BROWSER: "BROWSER",
    AcquisitionFetchStrategy.DOCUMENT: "DOCUMENT",
    AcquisitionFetchStrategy.MANUAL: "MANUAL",
}

#: A host is trustworthy for collection in exactly these two states. Kept as one
#: constant because it appears in a CHECK, in a trigger and in the coverage view,
#: and the three must not diverge.
TRUSTED_VERIFICATION_STATUSES = (
    OfficialVerificationStatus.VERIFIED_OFFICIAL,
    OfficialVerificationStatus.AUTHORIZED_EXTERNAL,
)

_TRUSTED_SQL = ", ".join(f"'{status.value}'" for status in TRUSTED_VERIFICATION_STATUSES)


#: How a mapping's verification translates into what it may substantiate (C27).
#:
#: Category is tested **first** so that `AUTHORIZED_RANKING` caps both trusted
#: statuses: a ranking page is a ranking source even when it sits on the
#: university's own verified domain, because the licence question is about the
#: ranking publisher's data, not about who hosts the page.
#:
#: `TARGET_SCOPE_ONLY` is deliberately unreachable from here. A mapping is a URL
#: someone verified; the client's spreadsheet is not, and never becomes one. That
#: class is carried only by `source` rows, and only by classifying them directly.
PUBLICATION_ELIGIBILITY_SQL = f"""
    CASE
        WHEN verification_status NOT IN ({_TRUSTED_SQL}) THEN 'NOT_ELIGIBLE'
        WHEN source_category = 'AUTHORIZED_RANKING'       THEN 'AUTHORIZED_RANKING'
        WHEN verification_status = 'AUTHORIZED_EXTERNAL'   THEN 'AUTHORIZED_EXTERNAL'
        ELSE 'OFFICIAL_VERIFIED'
    END
"""

#: The four values the derivation above can produce. Excludes `TARGET_SCOPE_ONLY`.
MAPPING_ELIGIBILITY_VALUES: tuple[str, ...] = (
    "NOT_ELIGIBLE",
    "OFFICIAL_VERIFIED",
    "AUTHORIZED_EXTERNAL",
    "AUTHORIZED_RANKING",
)


# ===========================================================================
# 1. The client's target list, versioned
# ===========================================================================


class TargetList(TimestampedMixin, Base):
    """One imported version of a client-supplied scope list.

    Versions accumulate; none is ever overwritten. A corrected QS 2027 and a future
    QS 2028 are both new rows, and the previous row keeps saying what it always
    said -- which is the only way the required added/removed/rank-changed report can
    be produced after the fact.

    `file_sha256` is what makes the import idempotent: re-importing identical bytes
    is recognised and does nothing. `(list_name, list_version)` is the *logical*
    identity, so the same version arriving with different content is a conflict an
    operator must resolve explicitly rather than a silent overwrite.

    Source metadata is transcribed from the workbook's own preamble, not asserted by
    us. The QS list carries its origin, version and publication date in text; those
    go to `source_description` / `source_url` / `published_at` verbatim, and
    `published_at` stays NULL when no date was stated -- consistent with the
    platform's refusal to manufacture precision (D17).
    """

    __tablename__ = "target_list"

    id: Mapped[uuid.UUID] = uuid_pk()
    list_name: Mapped[str] = mapped_column(String(200), nullable=False)
    list_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_description: Mapped[str | None] = mapped_column(
        Text, comment="Origin as stated by the supplied file itself, verbatim"
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[date | None] = mapped_column(
        Date, comment="Publication date if the file stated one; NULL is never guessed"
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    imported_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    file_name: Mapped[str] = mapped_column(String(400), nullable=False)
    file_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="sha256 of the workbook bytes as supplied"
    )
    file_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: What the file claimed about its own size, when it said so (the QS workbook
    #: states a total in its preamble and again in a per-region summary). Kept so a
    #: truncated or partially pasted file is caught, not imported.
    declared_row_count: Mapped[int | None] = mapped_column(Integer)
    imported_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Region -> count as declared by the file, for the same cross-check.
    declared_region_counts: Mapped[dict[str, object] | None] = mapped_column(NullableJsonb)
    notes: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list[TargetListEntry]] = relationship(back_populates="target_list")

    __table_args__ = (
        UniqueConstraint("list_name", "list_version", name="uq_target_list_list_name_list_version"),
        UniqueConstraint("file_sha256", name="uq_target_list_file_sha256"),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_is_hex"),
        CheckConstraint("imported_row_count >= 0", name="imported_row_count_non_negative"),
        CheckConstraint(
            "declared_row_count IS NULL OR declared_row_count >= 0",
            name="declared_row_count_non_negative",
        ),
        CheckConstraint("file_byte_size > 0", name="file_byte_size_positive"),
        # Only http/https may be recorded, even for a descriptive origin URL: a
        # value stored here is a candidate for later retrieval, and letting a
        # file:// or gopher:// string into the database would make the URL guard at
        # the edge the only line of defence (Step 4 requirement 15).
        CheckConstraint(
            "source_url IS NULL OR source_url ~* '^https?://'", name="source_url_is_http"
        ),
        {
            "comment": (
                "One imported version of a client scope list. Append-only history: "
                "versions accumulate and are never overwritten. Authoritative for "
                "project scope only, never for university facts."
            )
        },
    )


class TargetListEntry(RecordedAtMixin, Base):
    """One row of one imported spreadsheet, exactly as supplied. APPEND-ONLY.

    This is the sole home of QS-originated values. Keeping them immutable and
    version-scoped is what lets the platform answer "where did this rank come
    from?" with a file hash, a version and a row number, and what stops a QS name
    from drifting into a published institutional name.

    `qs_name_normalized` is a conservative match key (see `naming.py`): case and
    whitespace folding only. It is not an identity claim -- two institutions with
    genuinely similar names must not be merged by it, which is why matching to a
    canonical university stays a human decision.
    """

    __tablename__ = "target_list_entry"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("target_list.id", ondelete="RESTRICT"), nullable=False
    )
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_row: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1-based worksheet row, for traceability back to the file"
    )
    sequence_no: Mapped[int | None] = mapped_column(
        Integer, comment="The file's own ordinal column, when it has one"
    )
    qs_name: Mapped[str] = mapped_column(
        String(400), nullable=False, comment="QS-supplied name, verbatim. NOT a publishable name."
    )
    qs_name_normalized: Mapped[str] = mapped_column(String(400), nullable=False)
    qs_rank: Mapped[int | None] = mapped_column(
        Integer, comment="Numeric rank as listed. Ties are expected; not unique."
    )
    qs_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    region_label: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="Region as the client wrote it (Chinese in QS 2027)"
    )
    country_territory: Mapped[str | None] = mapped_column(
        String(200), comment="Country/Territory as the list stated it"
    )
    destination_code: Mapped[str | None] = mapped_column(
        String(8),
        ForeignKey("destination.code", ondelete="RESTRICT"),
        comment="Mapped from region_label; NULL when the region is unrecognised",
    )

    target_list: Mapped[TargetList] = relationship(back_populates="entries")
    target_institution: Mapped[TargetInstitution] = relationship(back_populates="entries")

    __table_args__ = (
        UniqueConstraint(
            "target_list_id", "source_row", name="uq_target_list_entry_target_list_id_source_row"
        ),
        # One institution appears at most once per list version. This is the
        # duplicate-institution validation, enforced by the database rather than
        # only by the importer.
        UniqueConstraint(
            "target_list_id",
            "qs_name_normalized",
            name="uq_target_list_entry_target_list_id_qs_name_normalized",
        ),
        UniqueConstraint(
            "target_list_id",
            "target_institution_id",
            name="uq_target_list_entry_target_list_id_target_institution_id",
        ),
        CheckConstraint("source_row >= 1", name="source_row_is_positive"),
        CheckConstraint("qs_rank IS NULL OR qs_rank >= 1", name="qs_rank_is_positive"),
        CheckConstraint("qs_score IS NULL OR qs_score BETWEEN 0 AND 100", name="qs_score_in_range"),
        CheckConstraint("btrim(qs_name) = qs_name AND qs_name <> ''", name="qs_name_is_trimmed"),
        Index("ix_target_list_entry_qs_name_normalized", "qs_name_normalized"),
        Index("ix_target_list_entry_target_institution_id", "target_institution_id"),
        {
            "comment": (
                "APPEND-ONLY. One spreadsheet row as supplied. The only home of "
                "QS-originated values; never evidence for a university fact."
            )
        },
    )


class TargetInstitution(TimestampedMixin, Base):
    """An institution the client asked us to cover. Governance state only.

    Carries no descriptive attribute of the institution -- deliberately. Everything
    the QS file said lives on `target_list_entry`; everything true about the
    institution lives on `university` once verified. This row is the join between
    the two plus the onboarding workflow's state.

    `is_in_current_list` goes false when a newer list omits the institution. Nothing
    is deleted: the entries stay, the verified domains stay, the matched university
    and all its evidence stay. Re-adding it in a later version simply flips the flag
    back and keeps the accumulated onboarding work.
    """

    __tablename__ = "target_institution"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Stable match key across list versions, derived from the first QS name seen.
    #: Not an identity claim about the institution; see `naming.py`.
    match_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    first_seen_list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("target_list.id", ondelete="RESTRICT"), nullable=False
    )
    latest_list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("target_list.id", ondelete="RESTRICT"), nullable=False
    )
    is_in_current_list: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="False when a newer list omits it. Never a reason to delete anything.",
    )
    removed_from_list_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_list.id", ondelete="RESTRICT"),
        comment="The list version that first omitted this institution",
    )
    destination_code: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("destination.code", ondelete="RESTRICT")
    )
    onboarding_status: Mapped[OnboardingStatus] = mapped_column(
        ONBOARDING_STATUS, nullable=False, server_default=OnboardingStatus.NOT_STARTED.value
    )
    #: Pilot membership. NULL means "not yet assigned", which is the correct state
    #: for all 181 on import: the client's list defines 57 institutions across the
    #: pilot destinations while the PRD speaks of at least 36, and *which* ones is a
    #: decision the client has not made. Inventing it here would be a fabricated
    #: requirement (Step 4 requirement 4).
    pilot_wave: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Pilot wave, assigned by the client. NULL until they decide."
    )
    matched_university_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("university.id", ondelete="RESTRICT"),
        unique=True,
        comment="Set only by human identity resolution. NULL is a normal state.",
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list[TargetListEntry]] = relationship(back_populates="target_institution")
    domains: Mapped[list[OfficialDomain]] = relationship(back_populates="target_institution")

    __table_args__ = (
        CheckConstraint("pilot_wave IS NULL OR pilot_wave >= 1", name="pilot_wave_is_positive"),
        # A blocked or escalated target must say why, or the state is unactionable.
        CheckConstraint(
            "onboarding_status NOT IN ('BLOCKED', 'NEEDS_MANUAL_REVIEW') "
            "OR blocked_reason IS NOT NULL",
            name="blocked_target_has_a_reason",
        ),
        # Identity resolution is a recorded human act, not an inference.
        CheckConstraint(
            "matched_university_id IS NULL "
            "OR (matched_at IS NOT NULL AND matched_by IS NOT NULL)",
            name="match_records_who_and_when",
        ),
        # Progress past identity requires the match to exist. This is the structural
        # reason a QS row cannot become an ACTIVE, collectable institution on its
        # own.
        CheckConstraint(
            "onboarding_status NOT IN ('SOURCE_MAPPING', 'READY_FOR_COLLECTION', 'ACTIVE') "
            "OR matched_university_id IS NOT NULL",
            name="advanced_status_requires_a_match",
        ),
        CheckConstraint(
            "is_in_current_list = true OR removed_from_list_id IS NOT NULL",
            name="removal_names_the_list_that_omitted_it",
        ),
        CheckConstraint(
            "is_in_current_list = false OR removed_from_list_id IS NULL",
            name="present_target_has_no_removal_list",
        ),
        Index("ix_target_institution_onboarding_status", "onboarding_status"),
        Index("ix_target_institution_destination_code", "destination_code"),
        Index("ix_target_institution_pilot_wave", "pilot_wave"),
        Index("ix_target_institution_latest_list_id", "latest_list_id"),
        {
            "comment": (
                "Client scope + onboarding progress. Holds no institutional fact and "
                "no QS attribute; those live on target_list_entry and university."
            )
        },
    )


class TargetListDiff(RecordedAtMixin, Base):
    """A difference between two imported list versions. APPEND-ONLY.

    Produced by the importer, never by a human, and never acted on automatically.
    `REMOVED_FROM_NEW_LIST` in particular is a report line: it records that a list
    stopped naming an institution, and explicitly does not cascade into the
    canonical plane (Step 4 requirement 13).
    """

    __tablename__ = "target_list_diff"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_list.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The newly imported list this difference was found in",
    )
    previous_target_list_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_list.id", ondelete="RESTRICT"),
        comment="The list compared against; NULL for a first import",
    )
    target_institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("target_institution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_kind: Mapped[TargetChangeKind] = mapped_column(TARGET_CHANGE_KIND, nullable=False)
    before_value: Mapped[dict[str, object] | None] = mapped_column(NullableJsonb)
    after_value: Mapped[dict[str, object] | None] = mapped_column(NullableJsonb)

    __table_args__ = (
        UniqueConstraint(
            "target_list_id",
            "target_institution_id",
            "change_kind",
            name="uq_target_list_diff_list_institution_kind",
        ),
        CheckConstraint(
            "target_list_id <> previous_target_list_id", name="diff_compares_two_lists"
        ),
        # An addition has no before; a removal has no after. Anything else must
        # carry both, or the report line says nothing.
        CheckConstraint(
            "(change_kind = 'ADDED_TARGET' AND before_value IS NULL "
            "     AND after_value IS NOT NULL)"
            " OR (change_kind = 'REMOVED_FROM_NEW_LIST' AND before_value IS NOT NULL "
            "     AND after_value IS NULL)"
            " OR (change_kind NOT IN ('ADDED_TARGET', 'REMOVED_FROM_NEW_LIST') "
            "     AND before_value IS NOT NULL AND after_value IS NOT NULL)",
            name="diff_values_match_the_change_kind",
        ),
        Index("ix_target_list_diff_target_list_id_change_kind", "target_list_id", "change_kind"),
        Index("ix_target_list_diff_target_institution_id", "target_institution_id"),
        {"comment": "APPEND-ONLY import difference report. Never cascades into canonical data."},
    )


# ===========================================================================
# 2. Official domain registry
# ===========================================================================


class OfficialDomain(TimestampedMixin, Base):
    """A host asserted, or confirmed, to belong to an institution.

    One institution owns many hosts: a main domain, faculty subdomains, a separate
    postgraduate site, a legacy domain that still resolves, and often a third-party
    application portal. All are registered here; what differs is
    `verification_status`.

    **A similar-looking domain is a `CANDIDATE` and nothing more.** Reaching
    `VERIFIED_OFFICIAL` requires a method, a timestamp and a named human, enforced
    by CHECK. There is no automatic promotion path anywhere in this codebase.

    **An authorised third party is not the university.** A SaaS application
    platform, however prominently the university links to it, can only reach
    `AUTHORIZED_EXTERNAL`, which additionally requires an authorisation reference --
    a recorded reason someone concluded the university sanctioned it.
    """

    __tablename__ = "official_domain"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: A host is discovered while onboarding a target, and/or belongs to a verified
    #: university. At least one must be set; both are set for the normal case of a
    #: target that has been matched.
    target_institution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("target_institution.id", ondelete="RESTRICT")
    )
    university_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT")
    )
    host: Mapped[str] = mapped_column(
        String(253), nullable=False, comment="Lowercase DNS name. No scheme, no port, no path."
    )
    covers_subdomains: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Whether verification extends to hosts under this one",
    )
    verification_status: Mapped[OfficialVerificationStatus] = mapped_column(
        OFFICIAL_VERIFICATION_STATUS,
        nullable=False,
        server_default=OfficialVerificationStatus.CANDIDATE.value,
    )
    verification_method: Mapped[DomainVerificationMethod | None] = mapped_column(
        DOMAIN_VERIFICATION_METHOD
    )
    verification_evidence: Mapped[str | None] = mapped_column(
        Text, comment="What was checked: registry entry, certificate subject, page URL"
    )
    authorization_reference: Mapped[str | None] = mapped_column(
        Text,
        comment="Required for AUTHORIZED_EXTERNAL: why we concluded the university sanctioned it",
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    #: Points at the host that replaced this one, for REPLACE and MARK LEGACY.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("official_domain.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text)
    discovered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )

    target_institution: Mapped[TargetInstitution | None] = relationship(back_populates="domains")
    mappings: Mapped[list[SourceMapping]] = relationship(back_populates="official_domain")

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(target_institution_id, university_id) >= 1",
            name="domain_belongs_to_a_target_or_a_university",
        ),
        # Host shape. Lowercase, dotted, no scheme/port/path/credentials, and not a
        # bare IP literal -- an IP cannot be shown to belong to an institution by
        # any of the recorded verification methods.
        CheckConstraint("host = lower(host)", name="host_is_lowercase"),
        CheckConstraint(
            "host ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'",
            name="host_is_a_dns_name",
        ),
        CheckConstraint("host !~ '^[0-9.]+$'", name="host_is_not_an_ipv4_literal"),
        CheckConstraint("length(host) BETWEEN 4 AND 253", name="host_length_is_plausible"),
        # Becoming *trusted* is a recorded human act. This is the constraint that
        # makes "do not automatically trust a similarly named domain" structural.
        #
        # Scoped to the trusted statuses rather than to "anything but CANDIDATE",
        # because rejecting a host is not verifying it: a rejection has no
        # verification method, and demanding one would make it impossible to record.
        # Who rejected a host, and why, is captured by `rejected_reason` and by the
        # audit chain. `LEGACY` keeps whatever basis it had when it was current.
        CheckConstraint(
            f"verification_status NOT IN ({_TRUSTED_SQL}) "
            "OR (verification_method IS NOT NULL AND verified_at IS NOT NULL "
            "    AND verified_by IS NOT NULL)",
            name="verified_domain_records_its_basis",
        ),
        # An authorised third party must name its authorisation, so it can never be
        # reached by merely observing a link from an official page.
        CheckConstraint(
            "verification_status <> 'AUTHORIZED_EXTERNAL' "
            "OR authorization_reference IS NOT NULL",
            name="authorized_external_names_its_authorization",
        ),
        # ...and conversely, only that status may carry one, so the column cannot be
        # used to smuggle an unauthorised host into VERIFIED_OFFICIAL.
        CheckConstraint(
            "verification_status = 'AUTHORIZED_EXTERNAL' OR authorization_reference IS NULL",
            name="authorization_only_for_external",
        ),
        CheckConstraint(
            "verification_status <> 'REJECTED' OR rejected_reason IS NOT NULL",
            name="rejected_domain_has_a_reason",
        ),
        CheckConstraint(
            "verification_status <> 'LEGACY' OR is_active = false",
            name="legacy_domain_is_inactive",
        ),
        CheckConstraint(
            "verification_status <> 'REJECTED' OR is_active = false",
            name="rejected_domain_is_inactive",
        ),
        CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="not_own_successor",
        ),
        # At most one owner per host among the trusted statuses -- two universities
        # cannot both hold a host as officially theirs. A partial unique index, so it
        # lives in the migration (see MIGRATION_OWNED_INDEXES): PostgreSQL rewrites a
        # partial predicate on read, and autogenerate then proposes dropping and
        # recreating it on every run.
        UniqueConstraint(
            "target_institution_id", "host", name="uq_official_domain_target_institution_id_host"
        ),
        Index("ix_official_domain_university_id", "university_id"),
        Index("ix_official_domain_verification_status", "verification_status"),
        Index("ix_official_domain_host", "host"),
        {
            "comment": (
                "Hosts belonging to an institution. CANDIDATE is the default and is "
                "never promoted automatically; AUTHORIZED_EXTERNAL is not official."
            )
        },
    )


# ===========================================================================
# 3. Official source mapping
# ===========================================================================


class SourceMapping(TimestampedMixin, Base):
    """A URL mapped to the kind of official information it carries.

    This is the **governance** record of where an institution publishes what. Its
    counterpart in the evidence plane is `source`, which is the crawler's
    registered target with its robots/ToS decisions and fetch history. A mapping
    becomes a source when it is verified and collection is authorised; until then
    nothing here is ever fetched.

    `promoted_source_id` records that crossing. It is unique, so one mapping
    promotes to at most one registered source, and it stays NULL through all of
    Step 4 -- **no acquisition is started by this module.**

    Degree and discipline applicability live in child tables rather than in columns
    here, because a page serves a *set* of audiences and because the coverage report
    must join on them. A Masters page that is silent about doctoral study simply has
    no `RESEARCH_POSTGRADUATE` row, and therefore contributes nothing to PhD
    coverage.
    """

    __tablename__ = "source_mapping"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_institution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("target_institution.id", ondelete="RESTRICT")
    )
    university_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("university.id", ondelete="RESTRICT")
    )
    source_category: Mapped[SourceCategory] = mapped_column(SOURCE_CATEGORY, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="As supplied by the operator")
    normalized_url: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Scheme/host lowercased, default port and fragment removed"
    )
    url_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(
        String(253),
        nullable=False,
        comment="Denormalised from the URL so scheduling and rate limiting need no parse",
    )
    #: The registry entry that vouches for `host`. A mapping may only be trusted if
    #: this domain is trusted -- enforced by trigger, since a CHECK cannot read
    #: another table.
    official_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("official_domain.id", ondelete="RESTRICT")
    )
    verification_status: Mapped[OfficialVerificationStatus] = mapped_column(
        OFFICIAL_VERIFICATION_STATUS,
        nullable=False,
        server_default=OfficialVerificationStatus.CANDIDATE.value,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    # --- acquisition preparation (Step 4 requirement 14) -------------------
    # Configuration only. No parser, no selector, no university-specific logic:
    # per-institution extraction rules belong to the extraction phase, and putting
    # them here would make onboarding a place where scraping code accumulates.
    fetch_strategy: Mapped[AcquisitionFetchStrategy] = mapped_column(
        ACQUISITION_FETCH_STRATEGY,
        nullable=False,
        server_default=AcquisitionFetchStrategy.HTTP.value,
    )
    collection_priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="3", comment="1 = highest"
    )
    collection_frequency: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="MONTHLY"
    )
    access_state: Mapped[SourceAccessState] = mapped_column(
        SOURCE_ACCESS_STATE, nullable=False, server_default=SourceAccessState.OK.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    deactivated_reason: Mapped[str | None] = mapped_column(Text)
    #: C27. What this mapping may substantiate once promoted. GENERATED, so no role
    #: can set it -- PostgreSQL rejects an INSERT or UPDATE naming a generated column
    #: for every role including the table owner, which makes this the one place in the
    #: eligibility chain that is not merely constrained but unassertable.
    #:
    #: `text` rather than the enum type, for the C15 reason: a generated expression
    #: must be IMMUTABLE and the text-to-enum cast (`enum_in`) is only STABLE. A CHECK
    #: pins the vocabulary instead, so the column is still impossible to set wrongly.
    publication_eligibility: Mapped[str] = mapped_column(
        String(24),
        Computed(PUBLICATION_ELIGIBILITY_SQL, persisted=True),
        nullable=False,
        comment=(
            "C27, GENERATED from verification_status and source_category. No role "
            "can write it, including the table owner."
        ),
    )
    promoted_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source.id", ondelete="RESTRICT"),
        unique=True,
        comment="Set when this mapping is registered for collection. NULL throughout Step 4.",
    )
    discovered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    official_domain: Mapped[OfficialDomain | None] = relationship(back_populates="mappings")
    degree_scopes: Mapped[list[SourceDegreeScope]] = relationship(back_populates="mapping")
    disciplines: Mapped[list[SourceDisciplineScope]] = relationship(back_populates="mapping")

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(target_institution_id, university_id) >= 1",
            name="mapping_belongs_to_a_target_or_a_university",
        ),
        # Step 4 requirement 15. Only http and https may be stored at all, so an
        # imported or pasted URL cannot smuggle another scheme past the fetch-time
        # guard. Registration-time validation in `urls.py` is stricter still; this
        # is the floor the database itself will not go below.
        CheckConstraint("url ~* '^https?://'", name="url_is_http_or_https"),
        # Case-sensitive on purpose: a normalised URL has a lowercase scheme, so
        # this also asserts normalisation actually ran.
        CheckConstraint("normalized_url ~ '^https?://'", name="normalized_url_is_http_or_https"),
        CheckConstraint("url !~ '[[:space:]]'", name="url_has_no_whitespace"),
        CheckConstraint("url_sha256 ~ '^[0-9a-f]{64}$'", name="url_sha256_is_hex"),
        CheckConstraint("host = lower(host)", name="host_is_lowercase"),
        CheckConstraint("collection_priority BETWEEN 1 AND 5", name="collection_priority_in_range"),
        CheckConstraint(
            "collection_frequency IN ("
            + ", ".join(f"'{value}'" for value in COLLECTION_FREQUENCIES)
            + ")",
            name="collection_frequency_known",
        ),
        CheckConstraint(
            "verification_status = 'CANDIDATE' "
            "OR (verified_at IS NOT NULL AND verified_by IS NOT NULL)",
            name="verified_mapping_records_actor_and_time",
        ),
        CheckConstraint(
            "verification_status <> 'REJECTED' OR rejected_reason IS NOT NULL",
            name="rejected_mapping_has_a_reason",
        ),
        CheckConstraint(
            "verification_status NOT IN ('REJECTED', 'LEGACY') OR is_active = false",
            name="rejected_or_legacy_mapping_is_inactive",
        ),
        CheckConstraint(
            "is_active = true OR deactivated_reason IS NOT NULL",
            name="deactivation_has_a_reason",
        ),
        # A mapping can only be registered for collection once it is trusted.
        CheckConstraint(
            f"promoted_source_id IS NULL OR verification_status IN ({_TRUSTED_SQL})",
            name="only_a_trusted_mapping_may_be_promoted",
        ),
        # C15 pattern: the generated column is `text`, so a CHECK supplies the
        # vocabulary the enum type would otherwise have given it.
        CheckConstraint(
            "publication_eligibility IN ("
            + ", ".join(f"'{value}'" for value in MAPPING_ELIGIBILITY_VALUES)
            + ")",
            name="publication_eligibility_known",
        ),
        # The same URL is registered once per institution per category. Two
        # institutions may legitimately share a URL (a government page), so this is
        # not globally unique.
        UniqueConstraint(
            "target_institution_id",
            "source_category",
            "url_sha256",
            name="uq_source_mapping_target_category_url",
        ),
        Index("ix_source_mapping_university_id", "university_id"),
        Index(
            "ix_source_mapping_target_institution_id_source_category",
            "target_institution_id",
            "source_category",
        ),
        Index("ix_source_mapping_host", "host"),
        Index("ix_source_mapping_verification_status", "verification_status"),
        {
            "comment": (
                "Governance record of which official URL carries which category of "
                "information. Never fetched during Step 4; promotion to `source` is "
                "a later, explicit step."
            )
        },
    )


class SourceDegreeScope(Base):
    """Which applicant audience a mapped source serves.

    Absence is meaningful: a mapping with no `RESEARCH_POSTGRADUATE` row does not
    cover doctoral study, and the coverage report will say so rather than assume a
    postgraduate page speaks for PhD applicants.
    """

    __tablename__ = "source_degree_scope"

    source_mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_mapping.id", ondelete="CASCADE"),
        primary_key=True,
    )
    degree_scope: Mapped[DegreeScope] = mapped_column(DEGREE_SCOPE, primary_key=True)

    mapping: Mapped[SourceMapping] = relationship(back_populates="degree_scopes")

    __table_args__ = (
        {"comment": "Audience applicability of a mapped source. Absence means 'not covered'."},
    )


class SourceDisciplineScope(Base):
    """Which disciplines a mapped source covers. No rows means "all disciplines".

    A university's central fee page applies to everything and gets no rows; a
    business-school entry-requirements page gets one.
    """

    __tablename__ = "source_discipline_scope"

    source_mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_mapping.id", ondelete="CASCADE"),
        primary_key=True,
    )
    discipline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discipline.id", ondelete="RESTRICT"), primary_key=True
    )

    mapping: Mapped[SourceMapping] = relationship(back_populates="disciplines")

    __table_args__ = (
        {"comment": "Discipline applicability of a mapped source. No rows means all."},
    )


__all__ = [
    "COLLECTION_FREQUENCIES",
    "FETCH_STRATEGY_TO_SOURCE",
    "MAPPING_ELIGIBILITY_VALUES",
    "PUBLICATION_ELIGIBILITY_SQL",
    "TRUSTED_VERIFICATION_STATUSES",
    "OfficialDomain",
    "SourceDegreeScope",
    "SourceDisciplineScope",
    "SourceMapping",
    "TargetInstitution",
    "TargetList",
    "TargetListDiff",
    "TargetListEntry",
]
