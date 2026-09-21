"""Step 5C.3: candidate review decisions, and a derived review-state projection

WHY NOT `review_task` / `review_decision`
=========================================
They are the *proposal* review plane, and they are bolted to it by NOT NULL foreign
keys: `review_task.proposal_id` → `change_proposal.id`, and `review_decision.task_id` →
`review_task.id`. Reviewing a candidate through them would require creating a
`change_proposal` first, which this step explicitly forbids. Their vocabulary does not
fit either: `review_decision_kind` is `APPROVE | RETURN | CORRECT`, which is what you
do to a *proposed change*, and it cannot express `REJECTED` or `NEEDS_SCOPE_MAPPING`.

Demonstrated rather than asserted: the integration tests attempt both inserts and read
the refusals.

WHY NOT `field_conflict` / `claim_resolution`
=============================================
`claim_resolution` and `resolution_candidate` are refused outright: both anchor on a NOT
NULL `claim_id` foreign key to `field_claim`, which has no rows, whose creation this step
forbids, and which C27 would refuse anyway because every pilot source is `NOT_ELIGIBLE`.
Two independent blocks.

`field_conflict` is **not** refused, and saying it was would be wrong. There is no
foreign key on `entity_id` and none on `competing_claim_ids`, so a row naming twelve real
candidate ids inserts successfully -- demonstrated, in a rolled-back transaction. The
objection is narrower and worth stating exactly: it accepts the row while *meaning*
something else. It is keyed on `(entity_type, entity_id, field_path)`, the **canonical
entity** a conflict is about, and every canonical table is empty, so the only `entity_id`
available is a fiction. `change_proposal` is the same shape -- its subject and root
columns carry no foreign key either, so only the instruction stops that one, not the
schema.

"The database refuses it" is a much easier claim to rely on than "the database would
accept a row that means nothing", and only the second is true here.

So candidate conflicts are **not stored at all**. They are a deterministic function of
the current candidates, computed on read. A stored conflict row would be a cache with no
invalidation: a new candidate, a corrected rule or a review decision changes the answer,
and nothing would update it.

WHAT IS STORED
==============
One table: `field_claim_candidate_review`, an append-only log of human decisions. Plus
one view, `candidate_review_state`, that projects the latest decision per candidate.

THE SUGGESTED STATE SET IS SPLIT, DELIBERATELY
==============================================
The instruction suggested one enum: `UNREVIEWED | ACCEPTED | REJECTED | NEEDS_CONTEXT |
NEEDS_SCOPE_MAPPING | SOURCE_NOT_VERIFIED | SUPERSEDED`.

Three of those are not decisions. `SUPERSEDED` is true when the candidate's rule version
is not the current one; `SOURCE_NOT_VERIFIED` is true when the source's
`publication_eligibility` is `NOT_ELIGIBLE`; scope resolution is a property of the
candidate's own `unresolved_reason`. A machine knows all three at any moment, and none
of them is something a reviewer decides.

Putting them in one column with the human decision would make it answer two questions at
once, which is the mistake D39 corrected for `fetch_eligibility`, and it would mask a
reviewer's judgement behind a fact about the source — exactly what section 30 forbids
("Keep candidate review and source eligibility separate"). A reviewer who accepts a
candidate from an unverified source has made a real decision, and it must survive.

So the stored decision holds only what a human decided, and the view exposes the derived
states beside it as separate columns. Nothing is lost and nothing is conflated.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only.

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What a human can decide about a candidate. Deliberately excludes `SUPERSEDED` and
#: `SOURCE_NOT_VERIFIED`, which are derived — see the module docstring.
DECISIONS = (
    "ACCEPTED",
    "REJECTED",
    "NEEDS_CONTEXT",
    "NEEDS_SCOPE_MAPPING",
)

#: Why. Extends the vocabulary `review_decision.reason_code` already uses, because a
#: reviewer rejecting a candidate and a reviewer returning a proposal are rejecting for
#: the same kinds of reason, and two spellings of one reason cannot be reported together.
REASON_CODES = (
    # Shared with review_decision.reason_code
    "EVIDENCE_INSUFFICIENT",
    "WRONG_SOURCE",
    "PARSE_ERROR",
    "NOT_OFFICIAL",
    "NEEDS_RECOLLECTION",
    "OTHER",
    # Candidate-specific, from what the Step 5C.2 audit actually found
    "NOT_A_FACT_OF_THIS_KIND",
    "SITE_CHROME",
    "MARKETING_PROSE",
    "SCOPE_AMBIGUOUS",
    "PROGRAM_CONTEXT_MISSING",
    "SUPERSEDED_BY_NEWER_RULE",
    "CONFLICTS_WITH_ANOTHER_SOURCE",
    "CORRECT_AS_EXTRACTED",
)

VIEW = "candidate_review_state"

#: The current rule versions, frozen into the view.
#:
#: **This was a mistake, and the next revision (`f1a2b3c4d5e6`) undoes it.** Four rule
#: corrections later the view reported every current candidate as superseded, silently,
#: with a total that happened to look plausible. A frozen copy of something that changes
#: is wrong by construction, and the cost of being wrong here is a review queue built
#: from history. The versions now live in `claim_rule_version`, which the claim runner
#: keeps true. Left here because rewriting an applied migration would falsify the
#: history of what was actually run.
CURRENT_RULES = (
    ("admission-rule-extractor", "3"),
    ("program-rule-extractor", "3"),
    ("language-rule-extractor", "3"),
    ("deadline-rule-extractor", "3"),
    ("calendar-rule-extractor", "3"),
    ("tuition-rule-extractor", "4"),
)


def upgrade() -> None:
    op.create_table(
        "field_claim_candidate_review",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        # Who decided. NOT NULL: an anonymous review decision is not auditable, and the
        # whole point of storing the decision is that somebody stands behind it.
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=True),
        # When the human decided, distinct from when the row was written (D3).
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_claim_candidate.id"],
            name=op.f("fk_field_claim_candidate_review_candidate_id_field_claim_candidate"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            name=op.f("fk_field_claim_candidate_review_actor_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_claim_candidate_review")),
        sa.CheckConstraint(
            "decision IN (" + ", ".join(f"'{name}'" for name in DECISIONS) + ")",
            name=op.f("ck_field_claim_candidate_review_decision_known"),
        ),
        sa.CheckConstraint(
            "reason_code IN (" + ", ".join(f"'{name}'" for name in REASON_CODES) + ")",
            name=op.f("ck_field_claim_candidate_review_reason_code_known"),
        ),
        # A reason code of OTHER explains nothing on its own, which is the same rule
        # `review_decision` already applies to proposals.
        sa.CheckConstraint(
            "reason_code <> 'OTHER' OR btrim(coalesce(reason_text, '')) <> ''",
            name=op.f("ck_field_claim_candidate_review_other_reason_is_explained"),
        ),
        sa.CheckConstraint(
            "reason_text IS NULL OR btrim(reason_text) <> ''",
            name=op.f("ck_field_claim_candidate_review_reason_text_is_not_blank"),
        ),
        # A decision cannot be recorded as having been made after it was written down.
        sa.CheckConstraint(
            "decided_at <= recorded_at",
            name=op.f("ck_field_claim_candidate_review_decided_before_recorded"),
        ),
    )
    op.create_index(
        "ix_field_claim_candidate_review_candidate",
        "field_claim_candidate_review",
        ["candidate_id", sa.text("decided_at DESC")],
    )
    op.create_index(
        "ix_field_claim_candidate_review_actor",
        "field_claim_candidate_review",
        ["actor_id", "decided_at"],
    )
    op.execute(
        "COMMENT ON TABLE field_claim_candidate_review IS "
        "'APPEND-ONLY log of human decisions about candidate claims. Several rows per "
        "candidate are expected and correct: a reviewer may revisit a decision, and the "
        "earlier one is history rather than an error. The current state is the latest "
        "row, projected by the candidate_review_state view. Accepting a candidate does "
        "NOT make it publishable: promotion additionally requires the source to be "
        "eligible for that field, which is a separate question (Step 5C.3 section 30).'"
    )

    # Append-only, like every other decision record in this schema.
    op.execute(
        "CREATE TRIGGER field_claim_candidate_review_forbid_mutation "
        "BEFORE UPDATE OR DELETE ON field_claim_candidate_review "
        "FOR EACH ROW EXECUTE FUNCTION app_forbid_mutation()"
    )

    # --- the projection -------------------------------------------------------------
    # A view rather than a table: the state is a pure function of the decision log, the
    # candidate's own columns and the source's eligibility, so a stored copy would be a
    # cache that nothing invalidates.
    current = ", ".join(f"('{name}', '{version}')" for name, version in CURRENT_RULES)
    op.execute(
        f"""
        CREATE VIEW {VIEW} AS
        SELECT c.id                                AS candidate_id,
               c.field_kind,
               c.confidence_band,
               c.source_responsibility,
               c.extractor_name,
               c.extractor_version,
               -- Derived, not decided: the rule that produced this row is no longer the
               -- current one, so the row is history and must stay out of active totals.
               (c.extractor_name, c.extractor_version) NOT IN ({current}) AS is_superseded,
               -- Derived, not decided: nobody has verified this source, so nothing from
               -- it may be published however a reviewer judges the candidate itself.
               s.publication_eligibility                                  AS source_eligibility,
               s.publication_eligibility = 'NOT_ELIGIBLE'                 AS source_not_verified,
               -- Derived, not decided: the extractor could not resolve the applicant
               -- scope, and section 12 forbids defaulting it to UNIVERSAL.
               coalesce(c.unresolved_reason, '') LIKE '%%SCOPE_MAPPING_REQUIRED%%'
                                                                          AS scope_unresolved,
               c.unresolved_reason,
               -- The human's judgement, and only that.
               coalesce(r.decision, 'UNREVIEWED')                         AS decision_state,
               r.reason_code,
               r.reason_text,
               r.actor_id,
               r.decided_at,
               r.review_count
          FROM field_claim_candidate c
          JOIN extraction e ON e.id = c.extraction_id
          JOIN snapshot sn ON sn.id = e.snapshot_id
          JOIN source s ON s.id = sn.source_id
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
        "behind a fact about the source, which section 30 forbids.'"
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT ON field_claim_candidate_review TO app_worker';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                -- The API records review decisions: that is a human action arriving
                -- through it, not a worker one.
                EXECUTE 'GRANT SELECT, INSERT ON field_claim_candidate_review TO app_api';
                EXECUTE 'GRANT SELECT ON candidate_review_state TO app_api';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.execute(
        "DROP TRIGGER IF EXISTS field_claim_candidate_review_forbid_mutation "
        "ON field_claim_candidate_review"
    )
    op.drop_index(
        "ix_field_claim_candidate_review_actor", table_name="field_claim_candidate_review"
    )
    op.drop_index(
        "ix_field_claim_candidate_review_candidate", table_name="field_claim_candidate_review"
    )
    op.drop_table("field_claim_candidate_review")
