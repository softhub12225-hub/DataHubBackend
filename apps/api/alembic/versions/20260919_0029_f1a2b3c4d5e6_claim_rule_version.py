"""Step 5C.3: let SQL learn the current rule versions instead of freezing them

WHY THIS EXISTS, AND WHY IT IS NOT A CANDIDATE TABLE
====================================================
Revision `e0f1a2b3c4d5` wrote the current (extractor, version) pairs into
`candidate_review_state` as a literal. Four rule corrections later -- a heading path, a
round label, a year range read as a day, a postcode read as an English score -- the view
was one generation stale and reported **every current candidate as superseded**. It did
that silently, and the count it produced (1,941) happened to look right.

That is not an argument for remembering to run a migration. A frozen copy of something
that changes is wrong by construction, and the cost of being wrong here is a review
queue built from history.

So the pairs live in a one-row-per-extractor table that the claim runner keeps true on
every pass, and the view joins it. `test_the_view_and_the_registry_agree_on_current_versions`
asserts the table matches `app.domains.claims.runner.EXTRACTORS`, so the two cannot
drift without a test failing.

This is not a second candidate plane (section 1). It holds no claim, no evidence and no
judgement -- one row per extractor saying which version of it is live, which is the only
thing SQL could not work out for itself.

MUTABLE, DELIBERATELY
=====================
Unlike everything else in the candidate plane this table is updated in place. It is
operational state, not history: what the *current* version is has one answer at a time,
and the history of which versions have existed is already in
`field_claim_candidate.extractor_version`, where it cannot be lost.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only. These are a SEED, not a
# contract -- the runner rewrites them on every pass.
revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED = (
    ("admission-rule-extractor", "4"),
    ("program-rule-extractor", "4"),
    ("language-rule-extractor", "4"),
    ("deadline-rule-extractor", "4"),
    ("calendar-rule-extractor", "4"),
    ("tuition-rule-extractor", "4"),
)

VIEW = "candidate_review_state"


def upgrade() -> None:
    op.create_table(
        "claim_rule_version",
        sa.Column("extractor_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=48), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("extractor_name", name=op.f("pk_claim_rule_version")),
    )
    op.execute(
        "COMMENT ON TABLE claim_rule_version IS "
        "'MUTABLE. One row per extractor naming the version that is currently live, so "
        "SQL can tell a current candidate from a superseded one without a migration "
        "every time a rule is corrected. Written by the claim runner from its own "
        "registry; the history of which versions ever existed lives in "
        "field_claim_candidate.extractor_version and is never touched here.'"
    )
    for name, version in SEED:
        op.execute(
            sa.text(
                "INSERT INTO claim_rule_version (extractor_name, version) "
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
               -- Derived, and no longer frozen: a rule whose version is not the live one
               -- produced history, and history must stay out of active review totals.
               (rv.version IS NULL OR rv.version <> c.extractor_version) AS is_superseded,
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
        "decided and nothing else; `is_superseded`, `source_not_verified` and "
        "`scope_unresolved` are derived facts a machine knows at any moment. They are "
        "separate columns on purpose: collapsing them into one status would make it "
        "answer two questions at once (D39) and would hide a reviewer''s judgement "
        "behind a fact about the source, which section 30 forbids. `is_superseded` "
        "reads claim_rule_version rather than a frozen list, because a frozen list of "
        "something that changes was wrong within four rule corrections.'"
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON claim_rule_version TO app_worker';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON claim_rule_version TO app_api';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_api';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.drop_table("claim_rule_version")
    # The previous revision's view is restored by its own downgrade; recreating it here
    # would duplicate that definition and the two copies would diverge.
