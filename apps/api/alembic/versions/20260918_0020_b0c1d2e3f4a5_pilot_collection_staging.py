"""U12: manual collection staging plane

WHY A NEW PLANE RATHER THAN A NEW WRITER INTO THE OLD ONE
=========================================================
The completed 36-university workbook is filled in by hand. It cannot produce a
``field_claim``, because a claim is a statement about a *snapshot*: the chain
``field_claim.extraction_id -> extraction.snapshot_id -> snapshot.source_id`` plus
``snapshot.content_hash -> content_blob`` is NOT NULL end to end, and its entire value
is that it proves a machine read a stored byte sequence fetched from a registered
source. A person typing a number into Excel produces none of that.

There were only two honest options. Route the workbook into the evidence plane by
manufacturing a fetch that never happened -- which makes the chain a formality and
quietly converts "we have evidence" into "someone asserted it". Or give the workbook
its own tables and keep the evidence plane meaning what it says. This revision does
the second.

WHAT THESE TABLES ARE, AND THE FOUR THINGS THAT FOLLOW
======================================================
They hold **a human collection artifact that points at official sources**: what a
named person read on a named page on a named date. That is genuinely useful -- it is
how we find out *where to look* for 36 institutions -- and it is not a published fact
about any of them.

1. **Not publication eligible.** No ``pilot_*`` table is reachable from
   ``field_provenance`` by any foreign key, so no staged row can be cited as
   provenance for a canonical value. A test walks the FK graph to keep it that way.
2. **Cannot become a ``field_claim``** without real acquisition. There is no column
   that would allow it; the claim chain still demands an extraction of a snapshot.
3. **Cannot modify canonical facts.** ``app_publisher`` receives no privilege here at
   all -- not even ``SELECT``, matching what C27 did to the onboarding plane. Grants
   are not what makes staging unpublishable (C27's whole lesson was that grants alone
   cannot express a semantic rule); they make the hand-copy route need a second
   credential rather than one.
4. **A source is never born verified.** ``pilot_collected_source`` starts at
   ``PENDING``. Reaching ``VERIFIED`` is a recorded human act with an actor, a
   timestamp and a reason, and it still does not make the URL an official source: it
   means "worth registering". Registration runs the existing path -- an
   ``official_domain`` verification, then a ``source_mapping`` promotion -- and a
   ``source`` still earns its eligibility rather than being handed it (C27).

SUBMISSION-LOCAL REFERENCES, ENFORCED BY THE KEYS
=================================================
``program_ref`` (``P0001``) and ``source_ref`` (``S0001``) are workbook-local. Both
definition tables are keyed on ``(submission_id, ref)`` and ``pilot_collected_fact``
reaches them through a composite foreign key on the same pair. So a fact row
physically cannot cite a programme or a source belonging to another workbook, and a
second workbook reusing ``P0001`` for a different programme is not a collision.

Unknown and cross-institution references never become NULL silently: the workbook
validator rejects them before import, and the composite FK rejects them again here.

RE-IMPORT: A CORRECTION IS A NEW SUBMISSION
===========================================
``file_sha256`` is UNIQUE, so re-importing identical bytes is recognised and writes
nothing. A corrected workbook has different bytes, so it becomes a *new* submission
sitting beside the old one; the earlier one is marked ``SUPERSEDED`` and stays
queryable. Nothing is overwritten, because the point of keeping both is that they can
be compared -- and a comparison against a row that was edited in place is a comparison
against nothing. ``pilot_collected_program`` and ``pilot_collected_fact`` are
therefore append-only, guarded by the same ``app_forbid_mutation()`` trigger the rest
of the history tables use.

``pilot_collected_source`` is deliberately the exception: it is mutable because its
job is to carry a verification decision that changes. The decisions themselves are
appended to the audit chain, so updating the row loses no history.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only.

revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # What happened to a whole workbook. SUPERSEDED is set on the *earlier*
    # submission when a correction arrives -- it is not a deletion, and the rows stay
    # readable.
    ("pilot_import_status", ("VALIDATED", "REJECTED", "SUPERSEDED")),
    # U15 triage of a collector-supplied URL.
    ("source_candidate_state", ("PENDING", "VERIFIED", "REJECTED", "NEEDS_REVIEW")),
    # Why a staged row cannot be reconciled without a human. SCOPE_MAPPING_REQUIRED is
    # the common one: anything but the literal UNIVERSAL parks here rather than being
    # mapped to "all applicants".
    ("pilot_fact_validation_state", ("OK", "SCOPE_MAPPING_REQUIRED", "NEEDS_REVIEW")),
)

#: Append-only: what the collector actually wrote. A correction is a new submission.
IMMUTABLE_TABLES_ADDED: tuple[str, ...] = (
    "pilot_selected_university",
    "pilot_collected_program",
    "pilot_collected_fact",
)

#: Mutable, because a decision has to land somewhere: the submission's status and the
#: candidate's verification state.
MUTABLE_TABLES_ADDED: tuple[str, ...] = (
    "pilot_submission",
    "pilot_collected_source",
)


def _grants() -> str:
    """Privileges for the staging plane.

    ``app_publisher`` gets nothing -- not write, and not read. It is the only role
    that may write canonical data, so the fewer places it can read an unverified
    string from, the fewer ways a spreadsheet value reaches a published field. This is
    a mitigation, not the enforcement: what actually stops a staged row being
    published is that ``field_provenance`` cannot reference one and ``field_claim``
    still requires a real extraction.

    ``app_worker`` gets read only. It has no business importing a workbook, and the
    acquisition phase (not yet built) will read the verified candidates to decide what
    to fetch.
    """
    everything = ", ".join(MUTABLE_TABLES_ADDED + IMMUTABLE_TABLES_ADDED)
    mutable = ", ".join(MUTABLE_TABLES_ADDED)
    immutable = ", ".join(IMMUTABLE_TABLES_ADDED)

    statements = [
        f"REVOKE ALL ON {everything} FROM app_api, app_worker, app_publisher",
        f"GRANT SELECT ON {everything} TO app_api, app_worker",
        # The import and the verification queue both run as the API role.
        f"GRANT INSERT, UPDATE ON {mutable} TO app_api",
        # Append-only: INSERT and nothing more. UPDATE/DELETE are additionally
        # refused by trigger, so removing this grant is not the only thing standing
        # between a staged row and an edit.
        f"GRANT INSERT ON {immutable} TO app_api",
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
    # The verification queue reads only what is still undecided, and there will be
    # ~200 candidates against a table that grows with every submission. Partial, so
    # the index stays the size of the worklist rather than the size of the history.
    op.execute(
        """
        CREATE INDEX ix_pilot_collected_source_pending
            ON pilot_collected_source (target_institution_id, source_type, source_ref)
         WHERE verification_state IN ('PENDING', 'NEEDS_REVIEW');
        """
    )

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
        values = ", ".join("'" + member + "'" for member in members)
        op.execute("CREATE TYPE " + name + " AS ENUM (" + values + ")")

    _create_tables()
    _post_table_ddl()


def _create_tables() -> None:
    op.create_table(
        "pilot_submission",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "file_sha256",
            sa.String(length=64),
            nullable=False,
            comment="sha256 of the workbook as supplied",
        ),
        sa.Column("original_filename", sa.String(length=400), nullable=False),
        sa.Column("file_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("template_version", sa.String(length=32), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the client says they finished it, if stated",
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("imported_by", sa.UUID(), nullable=True),
        sa.Column("selected_university_count", sa.Integer(), nullable=False),
        sa.Column(
            "expected_university_count",
            sa.Integer(),
            nullable=True,
            comment="What the client was asked for; checked, never used to choose",
        ),
        sa.Column(
            "import_status",
            postgresql.ENUM(
                "VALIDATED", "REJECTED", "SUPERSEDED", name="pilot_import_status", create_type=False
            ),
            server_default="VALIDATED",
            nullable=False,
        ),
        sa.Column(
            "validation_summary",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
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
            "file_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_pilot_submission_file_sha256_is_hex")
        ),
        sa.CheckConstraint(
            "file_byte_size > 0", name=op.f("ck_pilot_submission_file_byte_size_positive")
        ),
        sa.CheckConstraint(
            "selected_university_count >= 0",
            name=op.f("ck_pilot_submission_selected_university_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["imported_by"],
            ["app_user.id"],
            name=op.f("fk_pilot_submission_imported_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pilot_submission")),
        sa.UniqueConstraint("file_sha256", name=op.f("uq_pilot_submission_file_sha256")),
        comment="One returned collection workbook. A human artifact pointing at official sources -- never evidence, never publication eligible (U12).",
    )
    op.create_index(
        "ix_pilot_submission_imported_at", "pilot_submission", ["imported_at"], unique=False
    )
    op.create_table(
        "pilot_selected_university",
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column("sheet_row_no", sa.Integer(), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False),
        sa.Column(
            "selection_value",
            sa.String(length=32),
            nullable=True,
            comment="The `selected` cell exactly as the collector wrote it",
        ),
        sa.Column("official_name_en", sa.String(length=300), nullable=True),
        sa.Column("official_name_zh", sa.String(length=300), nullable=True),
        sa.Column("official_homepage", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("collector_notes", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT is_selected OR (btrim(coalesce(official_name_en, '')) <> ''     AND official_homepage IS NOT NULL)",
            name=op.f("ck_pilot_selected_university_selected_names_itself"),
        ),
        sa.CheckConstraint(
            "official_homepage IS NULL OR official_homepage ~* '^https?://'",
            name=op.f("ck_pilot_selected_university_official_homepage_is_http"),
        ),
        sa.CheckConstraint(
            "sheet_row_no >= 2",
            name=op.f("ck_pilot_selected_university_sheet_row_no_is_a_data_row"),
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["pilot_submission.id"],
            name=op.f("fk_pilot_selected_university_submission_id_pilot_submission"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_pilot_selected_university_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "submission_id", "target_institution_id", name=op.f("pk_pilot_selected_university")
        ),
        comment="APPEND-ONLY. The pilot selection and the collector's official-name claim. Creates no `university` row (U12, C27).",
    )
    op.create_index(
        "ix_pilot_selected_university_selected",
        "pilot_selected_university",
        ["submission_id", "is_selected"],
        unique=False,
    )
    op.create_table(
        "pilot_collected_program",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("program_ref", sa.String(length=16), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column("sheet_row_no", sa.Integer(), nullable=False),
        sa.Column("program_name_en", sa.String(length=400), nullable=False),
        sa.Column("degree_level_code", sa.String(length=32), nullable=True),
        sa.Column("discipline_code", sa.String(length=64), nullable=True),
        sa.Column("discipline_hint", sa.String(length=200), nullable=True),
        sa.Column("faculty_or_school", sa.String(length=300), nullable=True),
        sa.Column("study_mode", sa.String(length=32), nullable=True),
        sa.Column("delivery_mode", sa.String(length=32), nullable=True),
        sa.Column("duration_value", sa.Integer(), nullable=True),
        sa.Column("duration_unit", sa.String(length=16), nullable=True),
        sa.Column("campus_name", sa.String(length=200), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=True),
        sa.Column("collector_notes", sa.Text(), nullable=True),
        sa.Column("reconciled_program_id", sa.UUID(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(program_name_en) <> ''",
            name=op.f("ck_pilot_collected_program_program_name_en_is_not_blank"),
        ),
        sa.CheckConstraint(
            "program_ref ~ '^P[0-9]{4}$'", name=op.f("ck_pilot_collected_program_program_ref_shape")
        ),
        sa.CheckConstraint(
            "sheet_row_no >= 2", name=op.f("ck_pilot_collected_program_sheet_row_no_is_a_data_row")
        ),
        sa.ForeignKeyConstraint(
            ["reconciled_program_id"],
            ["program.id"],
            name=op.f("fk_pilot_collected_program_reconciled_program_id_program"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["pilot_submission.id"],
            name=op.f("fk_pilot_collected_program_submission_id_pilot_submission"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_pilot_collected_program_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pilot_collected_program")),
        sa.UniqueConstraint(
            "submission_id", "program_ref", name="uq_pilot_collected_program_submission_ref"
        ),
        comment="APPEND-ONLY. A programme as one workbook described it. Not a `program`.",
    )
    op.create_index(
        "ix_pilot_collected_program_target",
        "pilot_collected_program",
        ["target_institution_id", "submission_id"],
        unique=False,
    )
    op.create_table(
        "pilot_collected_source",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("source_ref", sa.String(length=16), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column("sheet_row_no", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=48), nullable=False),
        sa.Column("degree_scope", sa.String(length=32), nullable=True),
        sa.Column("official_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_sha256", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("checked_at", sa.Date(), nullable=True),
        sa.Column("is_third_party", sa.Boolean(), nullable=True),
        sa.Column("collector_notes", sa.Text(), nullable=True),
        sa.Column(
            "verification_state",
            postgresql.ENUM(
                "PENDING",
                "VERIFIED",
                "REJECTED",
                "NEEDS_REVIEW",
                name="source_candidate_state",
                create_type=False,
            ),
            server_default="PENDING",
            nullable=False,
            comment="U15 triage. Never set to anything but PENDING by an import.",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("verification_reason", sa.Text(), nullable=True),
        sa.Column("promoted_source_mapping_id", sa.UUID(), nullable=True),
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
            "normalized_url ~ '^https?://'",
            name=op.f("ck_pilot_collected_source_normalized_url_is_http"),
        ),
        sa.CheckConstraint(
            "official_url ~* '^https?://'",
            name=op.f("ck_pilot_collected_source_official_url_is_http"),
        ),
        sa.CheckConstraint(
            "promoted_source_mapping_id IS NULL OR verification_state = 'VERIFIED'",
            name=op.f("ck_pilot_collected_source_only_verified_is_registered"),
        ),
        sa.CheckConstraint(
            "source_ref ~ '^S[0-9]{4}$'", name=op.f("ck_pilot_collected_source_source_ref_shape")
        ),
        sa.CheckConstraint(
            "url_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_pilot_collected_source_url_sha256_is_hex"),
        ),
        sa.CheckConstraint(
            "verification_state = 'PENDING' OR (verified_at IS NOT NULL AND verified_by IS NOT NULL     AND btrim(coalesce(verification_reason, '')) <> '')",
            name=op.f("ck_pilot_collected_source_decision_records_who_when_why"),
        ),
        sa.CheckConstraint(
            "host = lower(host)", name=op.f("ck_pilot_collected_source_host_is_lowercase")
        ),
        sa.CheckConstraint(
            "sheet_row_no >= 2", name=op.f("ck_pilot_collected_source_sheet_row_no_is_a_data_row")
        ),
        sa.ForeignKeyConstraint(
            ["promoted_source_mapping_id"],
            ["source_mapping.id"],
            name=op.f("fk_pilot_collected_source_promoted_source_mapping_id_source_mapping"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["pilot_submission.id"],
            name=op.f("fk_pilot_collected_source_submission_id_pilot_submission"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_pilot_collected_source_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["app_user.id"],
            name=op.f("fk_pilot_collected_source_verified_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pilot_collected_source")),
        sa.UniqueConstraint(
            "promoted_source_mapping_id",
            name=op.f("uq_pilot_collected_source_promoted_source_mapping_id"),
        ),
        sa.UniqueConstraint(
            "submission_id", "source_ref", name="uq_pilot_collected_source_submission_ref"
        ),
        sa.UniqueConstraint(
            "submission_id", "url_sha256", name="uq_pilot_collected_source_submission_url"
        ),
        comment="A URL a collector supplied. Starts PENDING; VERIFIED here means 'worth registering', never 'official' (U15).",
    )
    op.create_index(
        "ix_pilot_collected_source_host", "pilot_collected_source", ["host"], unique=False
    )
    op.create_index(
        "ix_pilot_collected_source_state",
        "pilot_collected_source",
        ["verification_state"],
        unique=False,
    )
    op.create_index(
        "ix_pilot_collected_source_target",
        "pilot_collected_source",
        ["target_institution_id", "verification_state"],
        unique=False,
    )
    op.create_table(
        "pilot_collected_fact",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("sheet_name", sa.String(length=64), nullable=False),
        sa.Column("sheet_row_no", sa.Integer(), nullable=False),
        sa.Column("target_institution_id", sa.UUID(), nullable=False),
        sa.Column("program_ref", sa.String(length=16), nullable=True),
        sa.Column("source_ref", sa.String(length=16), nullable=True),
        sa.Column("fact_type", sa.String(length=48), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
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
            server_default="NOT_CHECKED",
            nullable=False,
        ),
        sa.Column(
            "collected_values",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "amount_kind",
            postgresql.ENUM(
                "EXACT",
                "RANGE",
                "FROM",
                "UP_TO",
                "VARIABLE",
                name="tuition_amount_kind",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("amount_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("amount_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "official_text",
            sa.Text(),
            nullable=True,
            comment="The official wording, pasted by the collector",
        ),
        sa.Column(
            "source_url",
            sa.Text(),
            nullable=True,
            comment="As supplied, for readability. source_ref is the link that resolves.",
        ),
        sa.Column("applicant_scope_hint", sa.String(length=300), nullable=True),
        sa.Column("applicant_country_code", sa.String(length=8), nullable=True),
        sa.Column("qualification_hint", sa.String(length=300), nullable=True),
        sa.Column("collector_notes", sa.Text(), nullable=True),
        sa.Column(
            "validation_state",
            postgresql.ENUM(
                "OK",
                "SCOPE_MAPPING_REQUIRED",
                "NEEDS_REVIEW",
                name="pilot_fact_validation_state",
                create_type=False,
            ),
            server_default="OK",
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fact_type IN ('ADMISSION_REQUIREMENT', 'LANGUAGE_REQUIREMENT', 'TUITION', 'DEADLINE', 'PROGRAM', 'UNIVERSITY_PROFILE')",
            name=op.f("ck_pilot_collected_fact_fact_type_known"),
        ),
        sa.CheckConstraint(
            "field_status NOT IN ('PUBLISHED', 'OFFICIALLY_NOT_PUBLISHED') OR source_ref IS NOT NULL",
            name=op.f("ck_pilot_collected_fact_asserted_row_cites_a_source_ref"),
        ),
        sa.CheckConstraint(
            "program_ref IS NULL OR program_ref ~ '^P[0-9]{4}$'",
            name=op.f("ck_pilot_collected_fact_program_ref_shape"),
        ),
        sa.CheckConstraint(
            "source_ref IS NULL OR source_ref ~ '^S[0-9]{4}$'",
            name=op.f("ck_pilot_collected_fact_source_ref_shape"),
        ),
        sa.CheckConstraint(
            "sheet_row_no >= 2", name=op.f("ck_pilot_collected_fact_sheet_row_no_is_a_data_row")
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "program_ref"],
            ["pilot_collected_program.submission_id", "pilot_collected_program.program_ref"],
            name="fk_pilot_collected_fact_submission_id_program_ref",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "source_ref"],
            ["pilot_collected_source.submission_id", "pilot_collected_source.source_ref"],
            name="fk_pilot_collected_fact_submission_id_source_ref",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["pilot_submission.id"],
            name=op.f("fk_pilot_collected_fact_submission_id_pilot_submission"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_institution_id"],
            ["target_institution.id"],
            name=op.f("fk_pilot_collected_fact_target_institution_id_target_institution"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pilot_collected_fact")),
        sa.UniqueConstraint(
            "submission_id",
            "sheet_name",
            "sheet_row_no",
            name="uq_pilot_collected_fact_submission_sheet_row",
        ),
        comment="APPEND-ONLY. One workbook row as supplied. Not evidence, not publication eligible, and unreachable from field_provenance (U12).",
    )
    op.create_index(
        "ix_pilot_collected_fact_submission_sheet",
        "pilot_collected_fact",
        ["submission_id", "sheet_name"],
        unique=False,
    )
    op.create_index(
        "ix_pilot_collected_fact_target",
        "pilot_collected_fact",
        ["target_institution_id", "fact_type"],
        unique=False,
    )
    op.create_index(
        "ix_pilot_collected_fact_validation_state",
        "pilot_collected_fact",
        ["validation_state"],
        unique=False,
    )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES_ADDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_mutation ON {table}")
    op.execute("DROP INDEX IF EXISTS ix_pilot_collected_source_pending")

    _drop_tables()

    for name, _ in NEW_ENUM_TYPES:
        op.execute("DROP TYPE " + name)


def _drop_tables() -> None:
    op.drop_index("ix_pilot_collected_fact_validation_state", table_name="pilot_collected_fact")
    op.drop_index("ix_pilot_collected_fact_target", table_name="pilot_collected_fact")
    op.drop_index("ix_pilot_collected_fact_submission_sheet", table_name="pilot_collected_fact")
    op.drop_table("pilot_collected_fact")
    op.drop_index("ix_pilot_collected_source_target", table_name="pilot_collected_source")
    op.drop_index("ix_pilot_collected_source_state", table_name="pilot_collected_source")
    op.drop_index("ix_pilot_collected_source_host", table_name="pilot_collected_source")
    op.drop_table("pilot_collected_source")
    op.drop_index("ix_pilot_collected_program_target", table_name="pilot_collected_program")
    op.drop_table("pilot_collected_program")
    op.drop_index("ix_pilot_selected_university_selected", table_name="pilot_selected_university")
    op.drop_table("pilot_selected_university")
    op.drop_index("ix_pilot_submission_imported_at", table_name="pilot_submission")
    op.drop_table("pilot_submission")
