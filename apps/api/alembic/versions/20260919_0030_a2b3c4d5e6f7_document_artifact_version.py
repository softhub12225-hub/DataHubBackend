"""Step 5C.4: the document artifact is a second axis of "current"

WHY A SECOND AXIS
=================
Re-extracting at document 2.0.0 kept the 1.0.0 artifacts, as section 1 required. So
every snapshot now has two extractions, and a candidate can be stale in two independent
ways: its **rule** can have been corrected, or the **document** it was read from can
have been re-parsed. Those are different questions with different answers, and D39 is
the standing lesson that one column must not answer two.

WHY NOT JUST BUMP EVERY RULE VERSION
====================================
Because it would be a lie about what changed, and section 8 forbids it. Running each
rule's current code over both artifacts of all 175 dual-artifact snapshots:

    calendar   242 -> 242 rows    0 statements added, 0 removed, 0 re-worded
    language   113 -> 113 rows    0 added, 0 removed, 0 re-worded
    tuition     40 ->  40 rows    0 added, 0 removed, 0 re-worded
    deadline    77 ->  77 rows    0 added, 0 removed, 1 re-worded

Four of the six rules say exactly the same things about the same pages; only
`block_index` moved, on one document each, where the parser stopped emitting script
blocks ahead of them. Bumping those versions would mark 472 correct claims superseded
and relabel 472 unchanged ones as a new rule's output.

WHY A TABLE AND NOT A LITERAL
=============================
Revision `f1a2b3c4d5e6` exists because the revision before it froze the live rule
versions into the view, and the view was wrong within four corrections -- silently, with
a plausible-looking total. Freezing `2.0.0` into this view would be the same mistake with
the same shape, so the document versions live in a registry the extraction runner keeps
true, exactly as the rule versions do.

REVIEW_SUPERSEDED (section 11)
==============================
A decision recorded against a candidate that has since been superseded is **not**
transferred to whatever replaced it. Section 11 forbids that, and it is right to: two
candidates with equal normalised values are not the same observation, and a reviewer who
accepted one has not looked at the other. The decision stays exactly where it was made,
and the view surfaces `review_superseded` so the queue can show it as work to redo
rather than as a judgement already given.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No import from app.* (C21/C22): frozen literals only. A SEED, not a contract -- the
# extraction runner rewrites these on every pass.
SEED = (
    ("html-document-normaliser", "2.0.0"),
    ("pdf-document-normaliser", "2.0.0"),
)

VIEW = "candidate_review_state"


def upgrade() -> None:
    op.create_table(
        "document_artifact_version",
        sa.Column("extractor_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=48), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("extractor_name", name=op.f("pk_document_artifact_version")),
    )
    op.execute(
        "COMMENT ON TABLE document_artifact_version IS "
        "'MUTABLE. One row per document normaliser naming the artifact version that is "
        "currently live. Re-extraction keeps the previous artifact, so without this a "
        "candidate read from a superseded parse of a page would still count as current "
        "and every report would double. Written by the extraction runner from its own "
        "constants; the history of which versions existed lives in extraction and is "
        "never touched here.'"
    )
    for name, version in SEED:
        op.execute(
            sa.text(
                "INSERT INTO document_artifact_version (extractor_name, version) "
                "VALUES (:name, :version) "
                "ON CONFLICT (extractor_name) DO UPDATE SET version = EXCLUDED.version, "
                "  updated_at = now()"
            ).bindparams(name=name, version=version)
        )

    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.execute(
        f"""
        CREATE VIEW {VIEW} AS
        SELECT c.id                                AS candidate_id,
               c.field_kind,
               c.confidence_band,
               c.source_responsibility,
               c.extractor_name,
               c.extractor_version,
               -- The two axes, kept apart so each answers one question.
               (rv.version IS NULL OR rv.version <> c.extractor_version)
                                                                         AS rule_superseded,
               (dv.version IS NULL OR dv.version <> e.extractor_version)
                                                                         AS document_superseded,
               e.extractor_version                                       AS document_version,
               -- And their combination, which is what "is this candidate live" means.
               ((rv.version IS NULL OR rv.version <> c.extractor_version)
                OR (dv.version IS NULL OR dv.version <> e.extractor_version))
                                                                         AS is_superseded,
               -- A human decision stranded on a candidate that is no longer live. NOT
               -- transferred to its replacement: section 11 forbids inferring that two
               -- candidates are the same observation because their values match.
               (r.decision IS NOT NULL
                AND ((rv.version IS NULL OR rv.version <> c.extractor_version)
                     OR (dv.version IS NULL OR dv.version <> e.extractor_version)))
                                                                         AS review_superseded,
               s.publication_eligibility                                 AS source_eligibility,
               s.publication_eligibility = 'NOT_ELIGIBLE'                AS source_not_verified,
               coalesce(c.unresolved_reason, '') LIKE '%%SCOPE_MAPPING_REQUIRED%%'
                                                                         AS scope_unresolved,
               c.unresolved_reason,
               coalesce(r.decision, 'UNREVIEWED')                        AS decision_state,
               r.reason_code,
               r.reason_text,
               r.actor_id,
               r.decided_at,
               r.review_count
          FROM field_claim_candidate c
          JOIN extraction e ON e.id = c.extraction_id
          JOIN snapshot sn ON sn.id = e.snapshot_id
          JOIN source s ON s.id = sn.source_id
          LEFT JOIN claim_rule_version rv ON rv.extractor_name = c.extractor_name
          LEFT JOIN document_artifact_version dv ON dv.extractor_name = e.extractor_name
          LEFT JOIN (
              SELECT DISTINCT ON (candidate_id)
                     candidate_id, decision, reason_code, reason_text, actor_id, decided_at,
                     count(*) OVER (PARTITION BY candidate_id) AS review_count
                FROM field_claim_candidate_review
               ORDER BY candidate_id, decided_at DESC, recorded_at DESC, id DESC
          ) r ON r.candidate_id = c.id
        """
    )
    op.execute(
        f"COMMENT ON VIEW {VIEW} IS "
        "'Current review state per candidate claim. `decision_state` is what a human "
        "decided and nothing else; everything beside it is a fact a machine knows at "
        "any moment. A candidate is superseded along two independent axes -- its rule "
        "was corrected (rule_superseded) or the page was re-parsed "
        "(document_superseded) -- and is_superseded is their combination. Both read "
        "registries rather than frozen lists, because a frozen list of something that "
        "changes was wrong within four rule corrections. review_superseded marks a "
        "decision stranded on a candidate that is no longer live; it is surfaced, never "
        "transferred, because equal normalised values do not make two candidates the "
        "same observation.'"
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON document_artifact_version TO app_worker';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON document_artifact_version TO app_api';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_api';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.drop_table("document_artifact_version")
    # The previous revision's view is restored by its own downgrade; recreating it here
    # would duplicate that definition and the two copies would diverge.
