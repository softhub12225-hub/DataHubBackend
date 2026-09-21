"""Step 4: target onboarding, official domains and source mapping

Adds the onboarding plane: which institutions the client asked us to cover, how we
established where each publishes official information, and how ready each is for
collection.

WHAT THIS REVISION DELIBERATELY DOES NOT DO
===========================================
It creates no university, no field claim, no change proposal and no ranking entry.
The client's target list is scope, not evidence, and the schema is arranged so that
this is enforced rather than merely intended:

* `app_publisher` -- the only role that may write canonical tables -- receives
  **SELECT and nothing else** on every table added here.
* `app_api` and `app_worker`, which may write these tables, hold no write privilege
  on any canonical table (revision 0013).

So there is no database identity capable of copying a QS value into published data,
whatever the application code asks for. QS-originated values are confined to
`target_list_entry`, which is append-only.

INVARIANTS ADDED
================
1. `target_list_entry` and `target_list_diff` are append-only (privileges + trigger),
   like the rest of the platform's history.
2. A host may have at most one trusted owner, via a partial unique index: two
   institutions cannot both hold a host as officially theirs.
3. A source mapping may not be trusted unless the host it sits on is a trusted
   registry entry, and an `AUTHORIZED_EXTERNAL` host may not back a
   `VERIFIED_OFFICIAL` mapping. Enforced by trigger, because a CHECK cannot read
   another table. This is what stops a third-party application platform becoming
   "official" because a university links to it.
4. Only http/https URLs may be stored at all (CHECK), so importing a spreadsheet or
   pasting a link is not a way past the fetch-time SSRF controls.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Enum types
#
# Members are FROZEN LITERALS, not imported from `app.db.enums` (C21). A later
# revision that adds a member must not retroactively change what this one created,
# or a fresh database would be built differently from an upgraded one.
# ---------------------------------------------------------------------------

NEW_ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "onboarding_status",
        (
            "NOT_STARTED",
            "IDENTITY_VERIFICATION",
            "DOMAIN_CANDIDATE",
            "DOMAIN_VERIFIED",
            "SOURCE_MAPPING",
            "READY_FOR_COLLECTION",
            "ACTIVE",
            "BLOCKED",
            "NEEDS_MANUAL_REVIEW",
        ),
    ),
    (
        "official_verification_status",
        ("VERIFIED_OFFICIAL", "CANDIDATE", "REJECTED", "LEGACY", "AUTHORIZED_EXTERNAL"),
    ),
    (
        "domain_verification_method",
        (
            "GOVERNMENT_REGISTRY",
            "ACCREDITATION_BODY",
            "UNIVERSITY_SELF_DECLARATION",
            "TLS_CERTIFICATE_SUBJECT",
            "MANUAL_STAFF_REVIEW",
            "AUTHORIZED_PARTNER_AGREEMENT",
        ),
    ),
    (
        "source_category",
        (
            "UNIVERSITY_HOME",
            "UNDERGRADUATE_ADMISSIONS",
            "POSTGRADUATE_ADMISSIONS",
            "PHD_ADMISSIONS",
            "PROGRAM_CATALOG",
            "FACULTY_OR_SCHOOL",
            "PROGRAM_PAGE",
            "ENTRY_REQUIREMENTS",
            "LANGUAGE_REQUIREMENTS",
            "TUITION_FEES",
            "APPLICATION_DEADLINES",
            "OFFICIAL_PDF",
            "ACADEMIC_CALENDAR",
            "GOVERNMENT",
            "AUTHORIZED_RANKING",
        ),
    ),
    ("degree_scope", ("UNDERGRADUATE", "TAUGHT_POSTGRADUATE", "RESEARCH_POSTGRADUATE")),
    ("acquisition_fetch_strategy", ("HTTP", "BROWSER", "DOCUMENT", "MANUAL")),
    (
        "target_change_kind",
        (
            "ADDED_TARGET",
            "REMOVED_FROM_NEW_LIST",
            "RANK_CHANGED",
            "SCORE_CHANGED",
            "NAME_CHANGED",
            "REGION_CHANGED",
        ),
    ),
)

#: Tables this revision adds, FROZEN. Revision 0013's grant lists were frozen before
#: these existed, so their privileges are granted here (C21).
IMMUTABLE_TABLES_ADDED = ("target_list_entry", "target_list_diff")
MUTABLE_TABLES_ADDED = (
    "target_list",
    "target_institution",
    "official_domain",
    "source_mapping",
)
#: Membership sets rather than history: which audiences and disciplines a page serves
#: is current configuration, so a wrongly ticked scope is removed, not deactivated.
#: The change itself is still recorded in `audit_log`.
SCOPE_TABLES_ADDED = ("source_degree_scope", "source_discipline_scope")

COVERAGE_VIEW = "target_source_coverage"


def _create_tables() -> None:
    """Tables, columns, CHECKs, foreign keys and plain indexes.

    Autogenerated from the models and then frozen here. Enum types are
    referenced with ``create_type=False`` because ``upgrade`` creates them
    once above; letting each table's DDL create them again fails on the
    second table that uses one.
    """
    op.create_table(
        "target_list",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("list_name", sa.String(length=200), nullable=False),
        sa.Column("list_version", sa.String(length=64), nullable=False),
        sa.Column(
            "source_description",
            sa.Text(),
            nullable=True,
            comment="Origin as stated by the supplied file itself, verbatim",
        ),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.Date(),
            nullable=True,
            comment="Publication date if the file stated one; NULL is never guessed",
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("imported_by", sa.UUID(), nullable=True),
        sa.Column("file_name", sa.String(length=400), nullable=False),
        sa.Column(
            "file_sha256",
            sa.String(length=64),
            nullable=False,
            comment="sha256 of the workbook bytes as supplied",
        ),
        sa.Column("file_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sheet_name", sa.String(length=200), nullable=False),
        sa.Column("declared_row_count", sa.Integer(), nullable=True),
        sa.Column("imported_row_count", sa.Integer(), nullable=False),
        sa.Column("declared_region_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.CheckConstraint(
            "file_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_target_list_file_sha256_is_hex")
        ),
        sa.CheckConstraint(
            "source_url IS NULL OR source_url ~* '^https?://'",
            name=op.f("ck_target_list_source_url_is_http"),
        ),
        sa.CheckConstraint(
            "declared_row_count IS NULL OR declared_row_count >= 0",
            name=op.f("ck_target_list_declared_row_count_non_negative"),
        ),
        sa.CheckConstraint(
            "file_byte_size > 0", name=op.f("ck_target_list_file_byte_size_positive")
        ),
        sa.CheckConstraint(
            "imported_row_count >= 0", name=op.f("ck_target_list_imported_row_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["imported_by"],
            ["app_user.id"],
            name=op.f("fk_target_list_imported_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_list")),
        sa.UniqueConstraint("file_sha256", name="uq_target_list_file_sha256"),
        sa.UniqueConstraint(
            "list_name", "list_version", name="uq_target_list_list_name_list_version"
        ),
        comment="One imported version of a client scope list. Append-only history: versions accumulate and are never overwritten. Authoritative for project scope only, never for university facts.",
    )
    op.create_table(
        "target_institution",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("match_key", sa.String(length=400), nullable=False),
        sa.Column("first_seen_list_id", sa.UUID(), nullable=False),
        sa.Column("latest_list_id", sa.UUID(), nullable=False),
        sa.Column(
            "is_in_current_list",
            sa.Boolean(),
            server_default="true",
            nullable=False,
            comment="False when a newer list omits it. Never a reason to delete anything.",
        ),
        sa.Column(
            "removed_from_list_id",
            sa.UUID(),
            nullable=True,
            comment="The list version that first omitted this institution",
        ),
        sa.Column("destination_code", sa.String(length=8), nullable=True),
        sa.Column(
            "onboarding_status",
            postgresql.ENUM(
                "NOT_STARTED",
                "IDENTITY_VERIFICATION",
                "DOMAIN_CANDIDATE",
                "DOMAIN_VERIFIED",
                "SOURCE_MAPPING",
                "READY_FOR_COLLECTION",
                "ACTIVE",
                "BLOCKED",
                "NEEDS_MANUAL_REVIEW",
                name="onboarding_status",
                create_type=False,
            ),
            server_default="NOT_STARTED",
            nullable=False,
        ),
        sa.Column(
            "pilot_wave",
            sa.SmallInteger(),
            nullable=True,
            comment="Pilot wave, assigned by the client. NULL until they decide.",
        ),
        sa.Column(
            "matched_university_id",
            sa.UUID(),
            nullable=True,
            comment="Set only by human identity resolution. NULL is a normal state.",
        ),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_by", sa.UUID(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "onboarding_status NOT IN ('BLOCKED', 'NEEDS_MANUAL_REVIEW') OR blocked_reason IS NOT NULL",
            name=op.f("ck_target_institution_blocked_target_has_a_reason"),
        ),
        sa.CheckConstraint(
            "onboarding_status NOT IN ('SOURCE_MAPPING', 'READY_FOR_COLLECTION', 'ACTIVE') OR matched_university_id IS NOT NULL",
            name=op.f("ck_target_institution_advanced_status_requires_a_match"),
        ),
        sa.CheckConstraint(
            "is_in_current_list = false OR removed_from_list_id IS NULL",
            name=op.f("ck_target_institution_present_target_has_no_removal_list"),
        ),
        sa.CheckConstraint(
            "is_in_current_list = true OR removed_from_list_id IS NOT NULL",
            name=op.f("ck_target_institution_removal_names_the_list_that_omitted_it"),
        ),
        sa.CheckConstraint(
            "matched_university_id IS NULL OR (matched_at IS NOT NULL AND matched_by IS NOT NULL)",
            name=op.f("ck_target_institution_match_records_who_and_when"),
        ),
        sa.CheckConstraint(
            "pilot_wave IS NULL OR pilot_wave >= 1",
            name=op.f("ck_target_institution_pilot_wave_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_target_institution_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_institution_first_seen_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["latest_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_institution_latest_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_by"],
            ["app_user.id"],
            name=op.f("fk_target_institution_matched_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_university_id"],
            ["university.id"],
            name=op.f("fk_target_institution_matched_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["removed_from_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_institution_removed_from_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_institution")),
        sa.UniqueConstraint("match_key", name=op.f("uq_target_institution_match_key")),
        sa.UniqueConstraint(
            "matched_university_id", name=op.f("uq_target_institution_matched_university_id")
        ),
        comment="Client scope + onboarding progress. Holds no institutional fact and no QS attribute; those live on target_list_entry and university.",
    )
    op.create_index(
        "ix_target_institution_destination_code",
        "target_institution",
        ["destination_code"],
        unique=False,
    )
    op.create_index(
        "ix_target_institution_latest_list_id",
        "target_institution",
        ["latest_list_id"],
        unique=False,
    )
    op.create_index(
        "ix_target_institution_onboarding_status",
        "target_institution",
        ["onboarding_status"],
        unique=False,
    )
    op.create_index(
        "ix_target_institution_pilot_wave", "target_institution", ["pilot_wave"], unique=False
    )
    op.create_table(
        "official_domain",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=True),
        sa.Column("university_id", sa.UUID(), nullable=True),
        sa.Column(
            "host",
            sa.String(length=253),
            nullable=False,
            comment="Lowercase DNS name. No scheme, no port, no path.",
        ),
        sa.Column(
            "covers_subdomains",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="Whether verification extends to hosts under this one",
        ),
        sa.Column(
            "verification_status",
            postgresql.ENUM(
                "VERIFIED_OFFICIAL",
                "CANDIDATE",
                "REJECTED",
                "LEGACY",
                "AUTHORIZED_EXTERNAL",
                name="official_verification_status",
                create_type=False,
            ),
            server_default="CANDIDATE",
            nullable=False,
        ),
        sa.Column(
            "verification_method",
            postgresql.ENUM(
                "GOVERNMENT_REGISTRY",
                "ACCREDITATION_BODY",
                "UNIVERSITY_SELF_DECLARATION",
                "TLS_CERTIFICATE_SUBJECT",
                "MANUAL_STAFF_REVIEW",
                "AUTHORIZED_PARTNER_AGREEMENT",
                name="domain_verification_method",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "verification_evidence",
            sa.Text(),
            nullable=True,
            comment="What was checked: registry entry, certificate subject, page URL",
        ),
        sa.Column(
            "authorization_reference",
            sa.Text(),
            nullable=True,
            comment="Required for AUTHORIZED_EXTERNAL: why we concluded the university sanctioned it",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("superseded_by_id", sa.UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("discovered_by", sa.UUID(), nullable=True),
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
            "host !~ '^[0-9.]+$'", name=op.f("ck_official_domain_host_is_not_an_ipv4_literal")
        ),
        sa.CheckConstraint(
            "host ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'",
            name=op.f("ck_official_domain_host_is_a_dns_name"),
        ),
        sa.CheckConstraint(
            "verification_status <> 'AUTHORIZED_EXTERNAL' OR authorization_reference IS NOT NULL",
            name=op.f("ck_official_domain_authorized_external_names_its_authorization"),
        ),
        sa.CheckConstraint(
            "verification_status <> 'LEGACY' OR is_active = false",
            name=op.f("ck_official_domain_legacy_domain_is_inactive"),
        ),
        sa.CheckConstraint(
            "verification_status <> 'REJECTED' OR is_active = false",
            name=op.f("ck_official_domain_rejected_domain_is_inactive"),
        ),
        sa.CheckConstraint(
            "verification_status <> 'REJECTED' OR rejected_reason IS NOT NULL",
            name=op.f("ck_official_domain_rejected_domain_has_a_reason"),
        ),
        sa.CheckConstraint(
            "verification_status = 'AUTHORIZED_EXTERNAL' OR authorization_reference IS NULL",
            name=op.f("ck_official_domain_authorization_only_for_external"),
        ),
        sa.CheckConstraint(
            "verification_status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') OR (verification_method IS NOT NULL AND verified_at IS NOT NULL     AND verified_by IS NOT NULL)",
            name=op.f("ck_official_domain_verified_domain_records_its_basis"),
        ),
        sa.CheckConstraint("host = lower(host)", name=op.f("ck_official_domain_host_is_lowercase")),
        sa.CheckConstraint(
            "length(host) BETWEEN 4 AND 253",
            name=op.f("ck_official_domain_host_length_is_plausible"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(target_institution_id, university_id) >= 1",
            name=op.f("ck_official_domain_domain_belongs_to_a_target_or_a_university"),
        ),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name=op.f("ck_official_domain_not_own_successor"),
        ),
        sa.ForeignKeyConstraint(
            ["discovered_by"],
            ["app_user.id"],
            name=op.f("fk_official_domain_discovered_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["official_domain.id"],
            name=op.f("fk_official_domain_superseded_by_id_official_domain"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_official_domain_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_official_domain_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["app_user.id"],
            name=op.f("fk_official_domain_verified_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_official_domain")),
        sa.UniqueConstraint(
            "target_institution_id", "host", name="uq_official_domain_target_institution_id_host"
        ),
        comment="Hosts belonging to an institution. CANDIDATE is the default and is never promoted automatically; AUTHORIZED_EXTERNAL is not official.",
    )
    op.create_index("ix_official_domain_host", "official_domain", ["host"], unique=False)
    op.create_index(
        "ix_official_domain_university_id", "official_domain", ["university_id"], unique=False
    )
    op.create_index(
        "ix_official_domain_verification_status",
        "official_domain",
        ["verification_status"],
        unique=False,
    )
    op.create_table(
        "target_list_diff",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "target_list_id",
            sa.UUID(),
            nullable=False,
            comment="The newly imported list this difference was found in",
        ),
        sa.Column(
            "previous_target_list_id",
            sa.UUID(),
            nullable=True,
            comment="The list compared against; NULL for a first import",
        ),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column(
            "change_kind",
            postgresql.ENUM(
                "ADDED_TARGET",
                "REMOVED_FROM_NEW_LIST",
                "RANK_CHANGED",
                "SCORE_CHANGED",
                "NAME_CHANGED",
                "REGION_CHANGED",
                name="target_change_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(change_kind = 'ADDED_TARGET' AND before_value IS NULL      AND after_value IS NOT NULL) OR (change_kind = 'REMOVED_FROM_NEW_LIST' AND before_value IS NOT NULL      AND after_value IS NULL) OR (change_kind NOT IN ('ADDED_TARGET', 'REMOVED_FROM_NEW_LIST')      AND before_value IS NOT NULL AND after_value IS NOT NULL)",
            name=op.f("ck_target_list_diff_diff_values_match_the_change_kind"),
        ),
        sa.CheckConstraint(
            "target_list_id <> previous_target_list_id",
            name=op.f("ck_target_list_diff_diff_compares_two_lists"),
        ),
        sa.ForeignKeyConstraint(
            ["previous_target_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_list_diff_previous_target_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_target_list_diff_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_list_diff_target_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_list_diff")),
        sa.UniqueConstraint(
            "target_list_id",
            "target_institution_id",
            "change_kind",
            name="uq_target_list_diff_list_institution_kind",
        ),
        comment="APPEND-ONLY import difference report. Never cascades into canonical data.",
    )
    op.create_index(
        "ix_target_list_diff_target_institution_id",
        "target_list_diff",
        ["target_institution_id"],
        unique=False,
    )
    op.create_index(
        "ix_target_list_diff_target_list_id_change_kind",
        "target_list_diff",
        ["target_list_id", "change_kind"],
        unique=False,
    )
    op.create_table(
        "target_list_entry",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("target_list_id", sa.UUID(), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column(
            "source_row",
            sa.Integer(),
            nullable=False,
            comment="1-based worksheet row, for traceability back to the file",
        ),
        sa.Column(
            "sequence_no",
            sa.Integer(),
            nullable=True,
            comment="The file's own ordinal column, when it has one",
        ),
        sa.Column(
            "qs_name",
            sa.String(length=400),
            nullable=False,
            comment="QS-supplied name, verbatim. NOT a publishable name.",
        ),
        sa.Column("qs_name_normalized", sa.String(length=400), nullable=False),
        sa.Column(
            "qs_rank",
            sa.Integer(),
            nullable=True,
            comment="Numeric rank as listed. Ties are expected; not unique.",
        ),
        sa.Column("qs_score", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column(
            "region_label",
            sa.String(length=120),
            nullable=False,
            comment="Region as the client wrote it (Chinese in QS 2027)",
        ),
        sa.Column(
            "country_territory",
            sa.String(length=200),
            nullable=True,
            comment="Country/Territory as the list stated it",
        ),
        sa.Column(
            "destination_code",
            sa.String(length=8),
            nullable=True,
            comment="Mapped from region_label; NULL when the region is unrecognised",
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(qs_name) = qs_name AND qs_name <> ''",
            name=op.f("ck_target_list_entry_qs_name_is_trimmed"),
        ),
        sa.CheckConstraint(
            "qs_rank IS NULL OR qs_rank >= 1", name=op.f("ck_target_list_entry_qs_rank_is_positive")
        ),
        sa.CheckConstraint(
            "qs_score IS NULL OR qs_score BETWEEN 0 AND 100",
            name=op.f("ck_target_list_entry_qs_score_in_range"),
        ),
        sa.CheckConstraint(
            "source_row >= 1", name=op.f("ck_target_list_entry_source_row_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_target_list_entry_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_target_list_entry_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_list_id"],
            ["target_list.id"],
            name=op.f("fk_target_list_entry_target_list_id_target_list"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_list_entry")),
        sa.UniqueConstraint(
            "target_list_id",
            "qs_name_normalized",
            name="uq_target_list_entry_target_list_id_qs_name_normalized",
        ),
        sa.UniqueConstraint(
            "target_list_id", "source_row", name="uq_target_list_entry_target_list_id_source_row"
        ),
        sa.UniqueConstraint(
            "target_list_id",
            "target_institution_id",
            name="uq_target_list_entry_target_list_id_target_institution_id",
        ),
        comment="APPEND-ONLY. One spreadsheet row as supplied. The only home of QS-originated values; never evidence for a university fact.",
    )
    op.create_index(
        "ix_target_list_entry_qs_name_normalized",
        "target_list_entry",
        ["qs_name_normalized"],
        unique=False,
    )
    op.create_index(
        "ix_target_list_entry_target_institution_id",
        "target_list_entry",
        ["target_institution_id"],
        unique=False,
    )
    op.create_table(
        "source_mapping",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=True),
        sa.Column("university_id", sa.UUID(), nullable=True),
        sa.Column(
            "source_category",
            postgresql.ENUM(
                "UNIVERSITY_HOME",
                "UNDERGRADUATE_ADMISSIONS",
                "POSTGRADUATE_ADMISSIONS",
                "PHD_ADMISSIONS",
                "PROGRAM_CATALOG",
                "FACULTY_OR_SCHOOL",
                "PROGRAM_PAGE",
                "ENTRY_REQUIREMENTS",
                "LANGUAGE_REQUIREMENTS",
                "TUITION_FEES",
                "APPLICATION_DEADLINES",
                "OFFICIAL_PDF",
                "ACADEMIC_CALENDAR",
                "GOVERNMENT",
                "AUTHORIZED_RANKING",
                name="source_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False, comment="As supplied by the operator"),
        sa.Column(
            "normalized_url",
            sa.Text(),
            nullable=False,
            comment="Scheme/host lowercased, default port and fragment removed",
        ),
        sa.Column("url_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "host",
            sa.String(length=253),
            nullable=False,
            comment="Denormalised from the URL so scheduling and rate limiting need no parse",
        ),
        sa.Column("official_domain_id", sa.UUID(), nullable=True),
        sa.Column(
            "verification_status",
            postgresql.ENUM(
                "VERIFIED_OFFICIAL",
                "CANDIDATE",
                "REJECTED",
                "LEGACY",
                "AUTHORIZED_EXTERNAL",
                name="official_verification_status",
                create_type=False,
            ),
            server_default="CANDIDATE",
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column(
            "fetch_strategy",
            postgresql.ENUM(
                "HTTP",
                "BROWSER",
                "DOCUMENT",
                "MANUAL",
                name="acquisition_fetch_strategy",
                create_type=False,
            ),
            server_default="HTTP",
            nullable=False,
        ),
        sa.Column(
            "collection_priority",
            sa.SmallInteger(),
            server_default="3",
            nullable=False,
            comment="1 = highest",
        ),
        sa.Column(
            "collection_frequency", sa.String(length=32), server_default="MONTHLY", nullable=False
        ),
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
        sa.Column(
            "promoted_source_id",
            sa.UUID(),
            nullable=True,
            comment="Set when this mapping is registered for collection. NULL throughout Step 4.",
        ),
        sa.Column("discovered_by", sa.UUID(), nullable=True),
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
        sa.CheckConstraint(
            "collection_frequency IN ('HIGH_RISK_3X_DAILY', 'DAILY', 'WEEKLY', 'MONTHLY', 'EVENT_DRIVEN')",
            name=op.f("ck_source_mapping_collection_frequency_known"),
        ),
        sa.CheckConstraint(
            "normalized_url ~ '^https?://'",
            name=op.f("ck_source_mapping_normalized_url_is_http_or_https"),
        ),
        sa.CheckConstraint(
            "promoted_source_id IS NULL OR verification_status IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')",
            name=op.f("ck_source_mapping_only_a_trusted_mapping_may_be_promoted"),
        ),
        sa.CheckConstraint(
            "url !~ '[[:space:]]'", name=op.f("ck_source_mapping_url_has_no_whitespace")
        ),
        sa.CheckConstraint(
            "url ~* '^https?://'", name=op.f("ck_source_mapping_url_is_http_or_https")
        ),
        sa.CheckConstraint(
            "url_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_source_mapping_url_sha256_is_hex")
        ),
        sa.CheckConstraint(
            "verification_status <> 'REJECTED' OR rejected_reason IS NOT NULL",
            name=op.f("ck_source_mapping_rejected_mapping_has_a_reason"),
        ),
        sa.CheckConstraint(
            "verification_status = 'CANDIDATE' OR (verified_at IS NOT NULL AND verified_by IS NOT NULL)",
            name=op.f("ck_source_mapping_verified_mapping_records_actor_and_time"),
        ),
        sa.CheckConstraint(
            "verification_status NOT IN ('REJECTED', 'LEGACY') OR is_active = false",
            name=op.f("ck_source_mapping_rejected_or_legacy_mapping_is_inactive"),
        ),
        sa.CheckConstraint(
            "collection_priority BETWEEN 1 AND 5",
            name=op.f("ck_source_mapping_collection_priority_in_range"),
        ),
        sa.CheckConstraint("host = lower(host)", name=op.f("ck_source_mapping_host_is_lowercase")),
        sa.CheckConstraint(
            "is_active = true OR deactivated_reason IS NOT NULL",
            name=op.f("ck_source_mapping_deactivation_has_a_reason"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(target_institution_id, university_id) >= 1",
            name=op.f("ck_source_mapping_mapping_belongs_to_a_target_or_a_university"),
        ),
        sa.ForeignKeyConstraint(
            ["discovered_by"],
            ["app_user.id"],
            name=op.f("fk_source_mapping_discovered_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["official_domain_id"],
            ["official_domain.id"],
            name=op.f("fk_source_mapping_official_domain_id_official_domain"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_source_id"],
            ["source.id"],
            name=op.f("fk_source_mapping_promoted_source_id_source"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_source_mapping_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["university.id"],
            name=op.f("fk_source_mapping_university_id_university"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["app_user.id"],
            name=op.f("fk_source_mapping_verified_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_mapping")),
        sa.UniqueConstraint(
            "promoted_source_id", name=op.f("uq_source_mapping_promoted_source_id")
        ),
        sa.UniqueConstraint(
            "target_institution_id",
            "source_category",
            "url_sha256",
            name="uq_source_mapping_target_category_url",
        ),
        comment="Governance record of which official URL carries which category of information. Never fetched during Step 4; promotion to `source` is a later, explicit step.",
    )
    op.create_index("ix_source_mapping_host", "source_mapping", ["host"], unique=False)
    op.create_index(
        "ix_source_mapping_target_institution_id_source_category",
        "source_mapping",
        ["target_institution_id", "source_category"],
        unique=False,
    )
    op.create_index(
        "ix_source_mapping_university_id", "source_mapping", ["university_id"], unique=False
    )
    op.create_index(
        "ix_source_mapping_verification_status",
        "source_mapping",
        ["verification_status"],
        unique=False,
    )
    op.create_table(
        "source_degree_scope",
        sa.Column("source_mapping_id", sa.UUID(), nullable=False),
        sa.Column(
            "degree_scope",
            postgresql.ENUM(
                "UNDERGRADUATE",
                "TAUGHT_POSTGRADUATE",
                "RESEARCH_POSTGRADUATE",
                name="degree_scope",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_mapping_id"],
            ["source_mapping.id"],
            name=op.f("fk_source_degree_scope_source_mapping_id_source_mapping"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_mapping_id", "degree_scope", name=op.f("pk_source_degree_scope")
        ),
        comment="Audience applicability of a mapped source. Absence means 'not covered'.",
    )
    op.create_table(
        "source_discipline_scope",
        sa.Column("source_mapping_id", sa.UUID(), nullable=False),
        sa.Column("discipline_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["discipline_id"],
            ["discipline.id"],
            name=op.f("fk_source_discipline_scope_discipline_id_discipline"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_mapping_id"],
            ["source_mapping.id"],
            name=op.f("fk_source_discipline_scope_source_mapping_id_source_mapping"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_mapping_id", "discipline_id", name=op.f("pk_source_discipline_scope")
        ),
        comment="Discipline applicability of a mapped source. No rows means all.",
    )


# ---------------------------------------------------------------------------
# Trigger: a mapping may only be trusted on a trusted host
#
# A CHECK constraint cannot read another table, so this is the only place the rule
# can live. It is the database-level statement of the client's requirement that an
# application platform hosted outside the university's domain must not become
# VERIFIED_OFFICIAL merely because it is linked from an official page.
#
# Locking: the domain row is taken FOR SHARE. Callers must already hold
# `official_domain` before `source_mapping` (see domains/onboarding/verification.py),
# so this never introduces a new lock order and cannot deadlock against the
# rejection path, which locks them in the same direction.
# ---------------------------------------------------------------------------

SOURCE_MAPPING_TRUST_FN = """
CREATE OR REPLACE FUNCTION app_source_mapping_requires_trusted_host() RETURNS trigger AS $$
DECLARE
    domain_status text;
    domain_host   text;
    domain_active boolean;
    domain_subs   boolean;
BEGIN
    -- A candidate, rejected or legacy mapping carries no claim, so there is nothing
    -- to substantiate.
    IF NEW.verification_status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') THEN
        RETURN NEW;
    END IF;

    IF NEW.official_domain_id IS NULL THEN
        RAISE EXCEPTION
            'source_mapping on host % cannot be %: no official_domain is registered for it',
            NEW.host, NEW.verification_status
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Register the host in official_domain and verify it first.';
    END IF;

    SELECT verification_status::text, host, is_active, covers_subdomains
      INTO domain_status, domain_host, domain_active, domain_subs
      FROM official_domain
     WHERE id = NEW.official_domain_id
       FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'official_domain % does not exist', NEW.official_domain_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- The registry entry must actually cover this URL's host. Without this a mapping
    -- could point at any host while citing an unrelated verified domain.
    IF NOT (
        NEW.host = domain_host
        OR (domain_subs AND right(NEW.host, length(domain_host) + 1) = '.' || domain_host)
    ) THEN
        RAISE EXCEPTION
            'source_mapping host % is not covered by official_domain % (%)',
            NEW.host, NEW.official_domain_id, domain_host
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Register this host, or set covers_subdomains on the parent domain.';
    END IF;

    IF domain_status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') OR NOT domain_active THEN
        RAISE EXCEPTION
            'source_mapping cannot be % : host % is % (active=%)',
            NEW.verification_status, domain_host, domain_status, domain_active
            USING ERRCODE = 'restrict_violation',
                  HINT = 'A source is only as official as the host it sits on.';
    END IF;

    -- The rule that matters most. An authorised third-party host may carry official
    -- information, but it is not the university's own domain, and a mapping on it
    -- may not claim otherwise.
    IF domain_status = 'AUTHORIZED_EXTERNAL' AND NEW.verification_status = 'VERIFIED_OFFICIAL' THEN
        RAISE EXCEPTION
            'host % is AUTHORIZED_EXTERNAL, so a source on it cannot be VERIFIED_OFFICIAL',
            domain_host
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Use AUTHORIZED_EXTERNAL for the mapping too, or verify the host '
                         'as the university''s own domain if that is what it is.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


# ---------------------------------------------------------------------------
# Coverage view
#
# A query, not a table, so it lives here rather than on a model. Built in two layers
# because the readiness status is a function of the same booleans the report shows,
# and repeating nine EXISTS clauses inside a CASE would be unreadable and would
# invite the two from drifting.
#
# Only active, trusted mappings count. A candidate URL is a guess, and counting it
# would make the report claim readiness to collect from an unconfirmed page.
# ---------------------------------------------------------------------------

COVERAGE_VIEW_SQL = """
CREATE VIEW target_source_coverage AS
SELECT
    base.*,
    CASE
        WHEN base.onboarding_status IN ('BLOCKED', 'NEEDS_MANUAL_REVIEW') THEN 'BLOCKED'
        WHEN NOT base.identity_verified THEN 'IDENTITY_NOT_VERIFIED'
        WHEN base.verified_source_count = 0 THEN 'NO_SOURCES_MAPPED'
        WHEN base.has_homepage
             AND base.has_undergraduate_admissions
             AND base.has_taught_postgraduate_admissions
             AND base.has_research_postgraduate_admissions
             AND base.has_program_catalog
             AND base.has_entry_requirements
             AND base.has_language_requirements
             AND base.has_tuition
             AND base.has_deadlines
            THEN 'SOURCE_MAPPING_COMPLETE'
        ELSE 'SOURCE_MAPPING_INCOMPLETE'
    END AS coverage_status
FROM (
    WITH current_name AS (
        -- The name from the most recently imported list that named this institution.
        -- For display only; it is a QS string, never a publishable name.
        SELECT DISTINCT ON (e.target_institution_id)
               e.target_institution_id,
               e.qs_name
          FROM target_list_entry e
          JOIN target_list l ON l.id = e.target_list_id
         ORDER BY e.target_institution_id, l.imported_at DESC, e.recorded_at DESC
    ),
    mapping AS (
        -- A mapping reaches an institution either directly, or through the canonical
        -- university it was matched to.
        SELECT ti.id AS target_institution_id,
               m.id  AS mapping_id,
               m.source_category::text AS source_category,
               m.verification_status::text AS verification_status,
               m.is_active
          FROM target_institution ti
          JOIN source_mapping m
            ON m.target_institution_id = ti.id
            OR (ti.matched_university_id IS NOT NULL
                AND m.university_id = ti.matched_university_id)
    ),
    trusted AS (
        SELECT * FROM mapping
         WHERE is_active
           AND verification_status IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')
    ),
    scoped AS (
        SELECT t.target_institution_id,
               t.source_category,
               s.degree_scope::text AS degree_scope
          FROM trusted t
          JOIN source_degree_scope s ON s.source_mapping_id = t.mapping_id
    )
    SELECT
        ti.id AS target_institution_id,
        ti.match_key,
        cn.qs_name,
        ti.destination_code,
        ti.onboarding_status::text AS onboarding_status,
        ti.is_in_current_list,
        ti.pilot_wave,
        -- Identity is settled only when a human matched this target to a verified
        -- canonical university.
        (ti.matched_university_id IS NOT NULL) AS identity_verified,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category = 'UNIVERSITY_HOME'
        ) AS has_homepage,
        EXISTS (
            SELECT 1 FROM scoped s
             WHERE s.target_institution_id = ti.id
               AND s.source_category = 'UNDERGRADUATE_ADMISSIONS'
               AND s.degree_scope = 'UNDERGRADUATE'
        ) AS has_undergraduate_admissions,
        EXISTS (
            SELECT 1 FROM scoped s
             WHERE s.target_institution_id = ti.id
               AND s.source_category = 'POSTGRADUATE_ADMISSIONS'
               AND s.degree_scope = 'TAUGHT_POSTGRADUATE'
        ) AS has_taught_postgraduate_admissions,
        -- Doctoral coverage requires a page explicitly scoped to research
        -- postgraduates. A taught-Masters page never satisfies it.
        EXISTS (
            SELECT 1 FROM scoped s
             WHERE s.target_institution_id = ti.id
               AND s.source_category IN ('PHD_ADMISSIONS', 'POSTGRADUATE_ADMISSIONS')
               AND s.degree_scope = 'RESEARCH_POSTGRADUATE'
        ) AS has_research_postgraduate_admissions,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category IN ('PROGRAM_CATALOG', 'PROGRAM_PAGE')
        ) AS has_program_catalog,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category = 'ENTRY_REQUIREMENTS'
        ) AS has_entry_requirements,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category = 'LANGUAGE_REQUIREMENTS'
        ) AS has_language_requirements,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category = 'TUITION_FEES'
        ) AS has_tuition,
        EXISTS (
            SELECT 1 FROM trusted t
             WHERE t.target_institution_id = ti.id
               AND t.source_category IN ('APPLICATION_DEADLINES', 'ACADEMIC_CALENDAR')
        ) AS has_deadlines,
        (SELECT count(*) FROM trusted t WHERE t.target_institution_id = ti.id)
            AS verified_source_count,
        (SELECT count(*) FROM mapping m
          WHERE m.target_institution_id = ti.id
            AND m.verification_status = 'CANDIDATE') AS candidate_source_count,
        (SELECT count(*) FROM official_domain d
          WHERE (d.target_institution_id = ti.id
                 OR (ti.matched_university_id IS NOT NULL
                     AND d.university_id = ti.matched_university_id))
            AND d.is_active
            AND d.verification_status IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL'))
            AS verified_domain_count
      FROM target_institution ti
      LEFT JOIN current_name cn ON cn.target_institution_id = ti.id
) AS base;
"""


def _enum_ddl(name: str, members: tuple[str, ...]) -> str:
    values = ", ".join(f"'{member}'" for member in members)
    return f"CREATE TYPE {name} AS ENUM ({values})"


def _grants() -> str:
    """Privileges for the tables this revision adds.

    `app_publisher` gets SELECT only, everywhere. That is the load-bearing line: the
    publisher is the sole writer of canonical data, so denying it write access here
    means no single database identity can carry a value from the client's spreadsheet
    into published university data.
    """
    mutable = ", ".join(MUTABLE_TABLES_ADDED)
    immutable = ", ".join(IMMUTABLE_TABLES_ADDED)
    scopes = ", ".join(SCOPE_TABLES_ADDED)
    everything = ", ".join(MUTABLE_TABLES_ADDED + IMMUTABLE_TABLES_ADDED + SCOPE_TABLES_ADDED)

    statements = [
        f"REVOKE ALL ON {everything} FROM app_api, app_worker, app_publisher",
        f"GRANT SELECT ON {everything} TO app_api, app_worker, app_publisher",
        f"GRANT SELECT ON {COVERAGE_VIEW} TO app_api, app_worker, app_publisher",
        # Onboarding is human working state, driven through the API.
        f"GRANT INSERT, UPDATE ON {mutable} TO app_api",
        f"GRANT INSERT, UPDATE, DELETE ON {scopes} TO app_api",
        # Append-only history: INSERT and nothing more. The import runs as app_api.
        f"GRANT INSERT ON {immutable} TO app_api",
        # The crawler may reclassify a source that blocks it (D6), exactly as it may
        # on `source`. It may not invent, verify or retarget a mapping.
        "GRANT UPDATE ON source_mapping TO app_worker",
    ]
    body = "\n".join(
        f"            EXECUTE '{statement.replace(chr(39), chr(39) * 2)}';"
        for statement in statements
    )
    return f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
           AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker')
           AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
{body}
        END IF;
    END
    $$;
    """


def _post_table_ddl() -> None:
    """Indexes, view and triggers that the model layer does not own."""
    # --- migration-owned partial indexes -----------------------------------
    # A host has at most one trusted owner. Partial, because rejected and legacy
    # rows must be allowed to accumulate for the same host: keeping a rejection is
    # what stops the same wrong domain being re-proposed.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_official_domain_one_trusted_owner_per_host
            ON official_domain (host)
         WHERE verification_status IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')
           AND is_active;
        """
    )
    # The onboarding worklist: institutions currently in scope, by status.
    op.execute(
        """
        CREATE INDEX ix_target_institution_current_scope
            ON target_institution (onboarding_status, destination_code)
         WHERE is_in_current_list;
        """
    )
    # Candidate hosts awaiting a human decision.
    op.execute(
        """
        CREATE INDEX ix_official_domain_candidates
            ON official_domain (target_institution_id)
         WHERE verification_status = 'CANDIDATE';
        """
    )
    # What the acquisition phase will schedule, once it exists. Ordered by the
    # columns a scheduler reads, so the query is index-only.
    op.execute(
        """
        CREATE INDEX ix_source_mapping_collectable
            ON source_mapping (collection_priority, host)
         WHERE is_active
           AND access_state = 'OK'
           AND verification_status IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL');
        """
    )

    op.execute(COVERAGE_VIEW_SQL)
    op.execute(
        f"COMMENT ON VIEW {COVERAGE_VIEW} IS "
        "'Per-institution source coverage. Counts only active, verified mappings.'"
    )

    op.execute(SOURCE_MAPPING_TRUST_FN)
    op.execute(
        """
        CREATE TRIGGER source_mapping_requires_trusted_host
        BEFORE INSERT OR UPDATE ON source_mapping
        FOR EACH ROW EXECUTE FUNCTION app_source_mapping_requires_trusted_host();
        """
    )

    # Append-only history, enforced by the same trigger function the rest of the
    # history tables use (revision 0013).
    for table in IMMUTABLE_TABLES_ADDED:
        op.execute(
            f"""
            CREATE TRIGGER {table}_forbid_mutation
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION app_forbid_mutation();
            """
        )

    op.execute(_grants())


def upgrade() -> None:
    for name, members in NEW_ENUM_TYPES:
        op.execute(_enum_ddl(name, members))

    _create_tables()
    _post_table_ddl()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {COVERAGE_VIEW}")
    op.execute("DROP TRIGGER IF EXISTS source_mapping_requires_trusted_host ON source_mapping")
    for table in IMMUTABLE_TABLES_ADDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_mutation ON {table}")
    op.execute("DROP FUNCTION IF EXISTS app_source_mapping_requires_trusted_host()")

    op.drop_table("source_discipline_scope")
    op.drop_table("source_degree_scope")
    op.drop_table("source_mapping")
    op.drop_table("target_list_entry")
    op.drop_table("target_list_diff")
    op.drop_table("official_domain")
    op.drop_table("target_institution")
    op.drop_table("target_list")

    for name, _members in reversed(NEW_ENUM_TYPES):
        op.execute(f"DROP TYPE IF EXISTS {name}")
