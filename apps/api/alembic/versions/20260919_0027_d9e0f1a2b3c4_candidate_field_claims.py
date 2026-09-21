"""Step 5C.2: retry semantics for extraction, and a candidate-claim plane

TWO CHANGES, FOR TWO REASONS
============================

1. EXTRACTION RETRY (section 0)
-------------------------------
Step 5C.1 put `UNIQUE (snapshot_id, extractor_name, extractor_version)` on `extraction`
and the table is append-only. For a *successful* result that is right. For a `FAILED`
one it conflated two different things: `extractor_version` came to mean both "the
extraction logic changed" and "the filesystem was briefly unavailable", so a transient
failure could only be retried by lying about the first.

The fix is the constraint, not a new table: the uniqueness is made **partial**, over
non-`FAILED` rows only. So

  * one successful-or-partial result per (snapshot, extractor, version) -- still
    enforced, so a repeat pass is still a no-op;
  * a `FAILED` row does not occupy that slot, so a retry at the same version is
    permitted;
  * failures accumulate as history rather than being overwritten, because the table
    stays append-only.

WHY NOT AN `extraction_attempt` TABLE
=====================================
The instruction offered that split, and the acquisition plane really does have it
(`fetch_attempt` / `fetch_run`) -- because a *fetch* has genuine in-flight mutable
state: a lease, a heartbeat, a worker that can die mid-request, a sweeper that has to
reclaim it.

Extraction has none of that. It is a pure function over bytes already held, offline,
in-process, with no lease to lose and no liveness to track. A mutable attempt table
would be modelling a lifecycle that does not exist, so the smallest correct change is
the one that changes only what was wrong.

2. CANDIDATE CLAIMS (sections 1-5)
----------------------------------
`field_claim` cannot hold these claims, and that is not an oversight -- it is C27
working. Its `field_claim_requires_eligible_evidence` trigger refuses any claim whose
source is not `OFFICIAL_VERIFIED`, `AUTHORIZED_EXTERNAL` or `AUTHORIZED_RANKING`, and
all 319 pilot sources are `NOT_ELIGIBLE`. Demonstrated rather than assumed: inserting
one raises `source ... is NOT_ELIGIBLE, which may not support a published fact`.

So `field_claim` is the **publication** claim plane: a row there asserts that a value
may become a published fact, gated on eligibility somebody earned. What this step
produces is something weaker and earlier -- *"this extractor found this candidate in
this exact evidence"* -- which is true of a page nobody has verified.

That is the same distinction Step 5B had to draw between `FETCHABLE` and
`PUBLICATION_ELIGIBLE` (D33), one plane further up: **finding a value is how you learn
what a page says; it is not permission to publish it.** Collapsing the two would either
weaken C27 or require faking verification, and both were explicitly forbidden.

`field_claim_candidate` is therefore a separate append-only table with no eligibility
gate and no path to publication. Promotion into `field_claim` is a later step, and it
will need the eligibility C27 asks for -- which is the point.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only.

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_UNIQUE = "uq_extraction_snapshot_id_extractor_name_extractor_version"
NEW_INDEX = "uq_extraction_result_per_version"

#: The field kinds this step implements. A CHECK rather than a PostgreSQL enum: these
#: are extractor output categories that will grow as rules are added, and growing a
#: CHECK is a one-line migration while growing an enum is a type change that cannot be
#: rolled back.
FIELD_KINDS = (
    "PROGRAM_NAME",
    "DEGREE_LEVEL",
    "DURATION",
    "STUDY_MODE",
    "CAMPUS",
    "DISCIPLINE_HINT",
    "FACULTY_OR_SCHOOL",
    "ADMISSION_REQUIREMENT",
    "LANGUAGE_TEST",
    "LANGUAGE_OVERALL_SCORE",
    "LANGUAGE_COMPONENT_SCORE",
    "TUITION",
    "APPLICATION_DEADLINE",
    "ACADEMIC_CALENDAR_EVENT",
)

CONFIDENCE_BANDS = ("HIGH", "MEDIUM", "LOW")


def upgrade() -> None:
    # --- 1. extraction retry semantics -------------------------------------------
    op.drop_constraint(op.f(OLD_UNIQUE), "extraction", type_="unique")
    op.execute(
        f"CREATE UNIQUE INDEX {NEW_INDEX} ON extraction "
        "(snapshot_id, extractor_name, extractor_version) "
        "WHERE status <> 'FAILED'"
    )
    op.execute(
        f"COMMENT ON INDEX {NEW_INDEX} IS "
        "'One successful-or-partial result per (snapshot, extractor, version). "
        "FAILED rows are excluded so a transient environmental failure can be retried "
        "without pretending the extraction logic changed, and the failures remain as "
        "append-only history (Step 5C.2 section 0).'"
    )

    # --- 2. the candidate-claim plane --------------------------------------------
    op.create_table(
        "field_claim_candidate",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # Lineage. Through the extraction to the snapshot, the run, the source and the
        # institution -- the chain section 34 requires to be walkable.
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        # Which *responsibility* authorised this extractor to run over this page
        # (section 4). A page claimed only as UNIVERSITY_HOME must not emit tuition,
        # and this column is what makes that auditable after the fact rather than a
        # property of code nobody can see.
        sa.Column("pilot_collected_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_responsibility", sa.String(length=64), nullable=False),
        sa.Column("field_kind", sa.String(length=48), nullable=False),
        # The candidate, normalised as far as a deterministic rule can take it. NULL is
        # a legitimate value: an ambiguous degree award or an unresolvable applicant
        # scope keeps its raw wording and stays unresolved rather than being forced.
        sa.Column(
            "value_normalized",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "unresolved_reason",
            sa.Text(),
            nullable=True,
            comment="Why value_normalized is NULL or incomplete: "
            "SCOPE_MAPPING_REQUIRED, DEGREE_LEVEL_AMBIGUOUS, "
            "BILLING_UNIT_UNRESOLVED, CURRENCY_ABSENT, and so on",
        ),
        # Section 27: the structured candidate AND the wording, always both. A reviewer
        # comparing our number against the page has to be able to find the sentence.
        sa.Column("value_raw_text", sa.Text(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        # Section 3: where in the normalised document this came from. JSONB because the
        # shape differs by document kind -- block index and heading path for HTML, page
        # and block for PDF, table/row/column for a cell, a path for JSON-LD -- and a
        # column per variant would be mostly NULL.
        sa.Column(
            "locator",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("extractor_name", sa.String(length=128), nullable=False),
        sa.Column("extractor_version", sa.String(length=48), nullable=False),
        # Section 25: an interpretable band with a stated reason, not a fake decimal.
        sa.Column("confidence_band", sa.String(length=8), nullable=False),
        sa.Column("confidence_reason", sa.Text(), nullable=False),
        # Section 5: stable identity, so a repeat pass inserts nothing. Deliberately
        # NOT derived from the value: two official pages stating the same fee are two
        # pieces of evidence and must remain two claims (section 26).
        sa.Column("claim_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "recorded_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extraction.id"],
            name=op.f("fk_field_claim_candidate_extraction_id_extraction"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pilot_collected_source_id"],
            ["pilot_collected_source.id"],
            name=op.f("fk_field_claim_candidate_pilot_collected_source_id_pilot_collected_source"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_claim_candidate")),
        sa.UniqueConstraint(
            "claim_fingerprint", name=op.f("uq_field_claim_candidate_claim_fingerprint")
        ),
        sa.CheckConstraint(
            "field_kind IN (" + ", ".join(f"'{kind}'" for kind in FIELD_KINDS) + ")",
            name=op.f("ck_field_claim_candidate_field_kind_known"),
        ),
        sa.CheckConstraint(
            "confidence_band IN (" + ", ".join(f"'{band}'" for band in CONFIDENCE_BANDS) + ")",
            name=op.f("ck_field_claim_candidate_confidence_band_known"),
        ),
        # Section 33 flags empty evidence as suspicious; the schema simply refuses it.
        # A claim whose evidence is blank cannot be reviewed, so it is not a claim.
        sa.CheckConstraint(
            "btrim(evidence_text) <> ''",
            name=op.f("ck_field_claim_candidate_evidence_is_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(value_raw_text) <> ''",
            name=op.f("ck_field_claim_candidate_raw_text_is_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(confidence_reason) <> ''",
            name=op.f("ck_field_claim_candidate_confidence_is_explained"),
        ),
        # A locator of `{}` is no locator. Section 3 forbids using extraction_id alone
        # as provenance, and an empty object would be exactly that with extra steps.
        sa.CheckConstraint(
            "locator <> '{}'::jsonb AND jsonb_typeof(locator) = 'object'",
            name=op.f("ck_field_claim_candidate_locator_is_present"),
        ),
        sa.CheckConstraint(
            "value_normalized IS NOT NULL OR unresolved_reason IS NOT NULL",
            name=op.f("ck_field_claim_candidate_unresolved_says_why"),
        ),
        sa.CheckConstraint(
            "claim_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_field_claim_candidate_fingerprint_is_sha256_hex"),
        ),
    )
    op.create_index(
        "ix_field_claim_candidate_extraction_id", "field_claim_candidate", ["extraction_id"]
    )
    op.create_index("ix_field_claim_candidate_field_kind", "field_claim_candidate", ["field_kind"])
    op.create_index(
        "ix_field_claim_candidate_extractor",
        "field_claim_candidate",
        ["extractor_name", "extractor_version"],
    )
    op.execute(
        "COMMENT ON TABLE field_claim_candidate IS "
        "'APPEND-ONLY. One candidate fact an extractor found in one exact region of one "
        "normalised document. It asserts only that: not verified, not canonical, not "
        "publishable, not conflict-free. Distinct from field_claim, which C27 gates on "
        "earned publication eligibility -- finding a value is how you learn what a page "
        "says, not permission to publish it (Step 5C.2).'"
    )

    # Append-only, using the same trigger the rest of the evidence plane uses.
    op.execute(
        "CREATE TRIGGER field_claim_candidate_forbid_mutation "
        "BEFORE UPDATE OR DELETE ON field_claim_candidate "
        "FOR EACH ROW EXECUTE FUNCTION app_forbid_mutation()"
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT, INSERT ON field_claim_candidate TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON field_claim_candidate TO app_api';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS field_claim_candidate_forbid_mutation ON field_claim_candidate"
    )
    op.drop_index("ix_field_claim_candidate_extractor", table_name="field_claim_candidate")
    op.drop_index("ix_field_claim_candidate_field_kind", table_name="field_claim_candidate")
    op.drop_index("ix_field_claim_candidate_extraction_id", table_name="field_claim_candidate")
    op.drop_table("field_claim_candidate")

    op.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    op.create_unique_constraint(
        OLD_UNIQUE, "extraction", ["snapshot_id", "extractor_name", "extractor_version"]
    )
