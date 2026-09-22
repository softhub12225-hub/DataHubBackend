"""Human scope and conflict resolution for field-claim candidates.

WHY TWO TABLES AND NOT A COLUMN ON THE REVIEW
=============================================
`field_claim_candidate_review` records *is this candidate correctly extracted*. Scope and
conflict are different questions with different answers, and one row cannot hold all
three without making "accepted" ambiguous:

* a candidate can be extracted perfectly and still have no stated scope;
* a conflict is a property of a *group*, not of any single candidate, so it cannot live
  on a candidate row at all.

So: `candidate_scope_resolution` is per candidate, `candidate_conflict_resolution` is per
context group.

WHY THE CONFLICT TABLE IS KEYED BY A FINGERPRINT
================================================
`grouping.group_candidates` computes groups on read and stores nothing, deliberately: a
stored group is a cache with no invalidation, since one new candidate or one corrected
rule changes the answer. But a *resolution* has to outlive the request that made it, so it
is keyed on `context_fingerprint` -- the SHA-256 of the deterministic, order-independent
`context_key_for` tuple. If the context changes, the fingerprint changes, and the old
resolution simply no longer matches any group. That is the correct behaviour: a decision
about one question must not silently transfer to a different one.

NOTHING HERE MAKES ANYTHING PUBLISHABLE
=======================================
Both tables are append-only records of human judgement. No trigger reads them, no
eligibility depends on them yet, and `field_claim` is untouched. They exist so that the
readiness report can say *a human has resolved this* instead of inferring it.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


#: The human scope states. `UNSCOPED` is the honest default and is never inferred to be
#: universal (section 12 of Step 5C.5, restated by 5C.9). `UNIVERSAL_EXPLICIT` exists so
#: that a reviewer *can* say "this really does apply to everyone" -- and the name carries
#: the reason it is safe: somebody said so.
SCOPE_STATES = (
    "UNSCOPED",
    "APPLICANT_JURISDICTION",
    "QUALIFICATION_SYSTEM",
    "JURISDICTION_AND_QUALIFICATION",
    "UNIVERSAL_EXPLICIT",
    "NOT_APPLICABLE",
)

#: What a reviewer may do with a conflict or duplicate group. There is no "pick the
#: highest confidence" action: confidence describes extraction quality, not truth.
CONFLICT_ACTIONS = (
    "RESOLVED_DUPLICATE",
    "SELECTED_SUPPORTED_CLAIM",
    "REJECTED_CONFLICTING",
    "LEFT_UNRESOLVED",
)


def _in_list(column: str, allowed: tuple[str, ...]) -> str:
    """`col IN ('A', 'B')`, written out rather than relying on `str(tuple)`.

    A tuple repr is valid SQL by coincidence and stops being valid the moment the list
    has one member -- `('A',)` is a syntax error. Spelling it out costs three lines and
    removes a trap from a file nobody reads twice.
    """
    quoted = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "candidate_scope_resolution",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_state", sa.String(48), nullable=False),
        # Null while the client taxonomy has nothing to point at. A state of
        # APPLICANT_JURISDICTION with a null id is a complete, honest record: the reviewer
        # said which dimension applies and no scope row exists to name it yet.
        sa.Column("applicant_scope_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        # What the reviewer actually selected, per dimension, before any scope row exists.
        # jsonb rather than a child table: this is the *record of a decision*, not a
        # queryable taxonomy, and `applicant_scope_criterion` is where it lands once the
        # taxonomy can hold it.
        sa.Column(
            "selected_criteria",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_claim_candidate.id"],
            ondelete="RESTRICT",
            name="fk_candidate_scope_resolution_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
            name="fk_candidate_scope_resolution_actor",
        ),
        sa.ForeignKeyConstraint(
            ["applicant_scope_id"],
            ["applicant_scope.id"],
            ondelete="RESTRICT",
            name="fk_candidate_scope_resolution_scope",
        ),
        sa.CheckConstraint(
            _in_list("scope_state", SCOPE_STATES),
            name="ck_candidate_scope_resolution_state_known",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''", name="ck_candidate_scope_resolution_reason_is_not_blank"
        ),
        # UNSCOPED means "nobody has said", so it must not carry a scope id. The check is
        # the schema refusing to let an unresolved row look resolved.
        sa.CheckConstraint(
            "scope_state <> 'UNSCOPED' OR applicant_scope_id IS NULL",
            name="ck_candidate_scope_resolution_unscoped_names_no_scope",
        ),
        sa.CheckConstraint(
            "decided_at <= recorded_at",
            name="ck_candidate_scope_resolution_decided_before_recorded",
        ),
        comment=(
            "Human applicant-scope decision for one candidate. Append-only; the latest "
            "row by decided_at is current. UNSCOPED is never inferred as universal."
        ),
    )
    op.create_index(
        "ix_candidate_scope_resolution_candidate_id",
        "candidate_scope_resolution",
        ["candidate_id"],
    )

    op.create_table(
        "candidate_conflict_resolution",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The SHA-256 of grouping.context_key_for(). See the module docstring for why a
        # fingerprint and not a foreign key to a stored group.
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("institution", sa.String(256), nullable=False),
        sa.Column("field_kind", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        # Which candidate the reviewer chose to support, when the action is a selection.
        sa.Column(
            "selected_candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
        # The members the decision was made over, so a later reader can tell whether the
        # group has since gained or lost candidates.
        sa.Column(
            "member_candidate_ids",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("verdict_at_decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
            name="fk_candidate_conflict_resolution_actor",
        ),
        sa.ForeignKeyConstraint(
            ["selected_candidate_id"],
            ["field_claim_candidate.id"],
            ondelete="RESTRICT",
            name="fk_candidate_conflict_resolution_selected",
        ),
        sa.CheckConstraint(
            _in_list("action", CONFLICT_ACTIONS),
            name="ck_candidate_conflict_resolution_action_known",
        ),
        # A selection must name what was selected, and the actions that are not a
        # selection must not name one. Without this, "SELECTED_SUPPORTED_CLAIM" with a
        # null id would be a resolution that resolved nothing.
        sa.CheckConstraint(
            "(action = 'SELECTED_SUPPORTED_CLAIM') = (selected_candidate_id IS NOT NULL)",
            name="ck_candidate_conflict_resolution_selection_names_a_candidate",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''", name="ck_candidate_conflict_resolution_reason_is_not_blank"
        ),
        sa.CheckConstraint(
            "decided_at <= recorded_at",
            name="ck_candidate_conflict_resolution_decided_before_recorded",
        ),
        comment=(
            "Human resolution of one agreement/conflict group, keyed by the SHA-256 of "
            "its deterministic context key. Append-only; latest by decided_at is current."
        ),
    )
    op.create_index(
        "ix_candidate_conflict_resolution_fingerprint",
        "candidate_conflict_resolution",
        ["context_fingerprint"],
    )

    # `app_api` handles console requests; it may record a human decision and read it back,
    # and it may not delete one. Append-only is a grant, not a convention.
    op.execute(
        "GRANT SELECT, INSERT ON candidate_scope_resolution, "
        "candidate_conflict_resolution TO app_api"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE ALL ON candidate_scope_resolution, candidate_conflict_resolution FROM app_api"
    )
    op.drop_index(
        "ix_candidate_conflict_resolution_fingerprint",
        table_name="candidate_conflict_resolution",
    )
    op.drop_table("candidate_conflict_resolution")
    op.drop_index(
        "ix_candidate_scope_resolution_candidate_id", table_name="candidate_scope_resolution"
    )
    op.drop_table("candidate_scope_resolution")
