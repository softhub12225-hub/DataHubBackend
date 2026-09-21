"""Domain group 1a: PostgreSQL enum types

Created in their own revision, ahead of every table, for two reasons:

* Several tables reference the same type. If each table's DDL tried to create it,
  the second table would fail with "type already exists" — which is why every enum
  column in the models is declared with ``create_type=False``.
* An enum is a schema object with its own lifecycle. Adding a member is a deliberate
  migration, and keeping them here makes that history easy to read.

Evolving controlled vocabularies are **tables**, not enums (see the next revision):
operations must be able to add a destination, a currency or a round type without a
deployment. Only closed technical states live here.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Deliberately no import from `app.db.enums`: a historical migration must not read
# live application state (C21). The members below are FROZEN as of this revision.
#
# Importing the live list is how revision 0016 broke a *fresh* database while an
# already-upgraded one stayed fine: adding a Step 4 enum to `app.db.enums` made this
# revision create it too, and 0016's own CREATE TYPE then failed with "already
# exists". An upgraded database never re-ran this revision, so the fault was
# invisible except on a clean build.
#
# `fetch_status` here deliberately omits ABANDONED: revision 0015 adds that member,
# and this revision must keep creating the type as it was originally created.

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "field_status",
        (
            "NOT_CHECKED",
            "OFFICIALLY_NOT_PUBLISHED",
            "PUBLISHED",
            "WITHDRAWN",
        ),
    ),
    (
        "month_part",
        (
            "EARLY",
            "MID",
            "LATE",
        ),
    ),
    (
        "deadline_kind",
        (
            "FIXED_DATE",
            "ROLLING",
            "UNTIL_FILLED",
            "NO_FIXED_DEADLINE",
            "NOT_CURRENTLY_ACCEPTING",
        ),
    ),
    (
        "risk_level",
        (
            "HIGH",
            "MEDIUM",
            "LOW",
        ),
    ),
    (
        "trust_status",
        (
            "VERIFIED",
            "CONFLICTED",
            "STALE",
        ),
    ),
    (
        "lifecycle_status",
        (
            "ACTIVE",
            "SUSPENDED",
            "WITHDRAWN",
            "NOT_OFFERED_THIS_CYCLE",
        ),
    ),
    (
        "scope_operator",
        (
            "EQUALS",
            "IN_GROUP",
            "NOT_EQUALS",
            "NOT_IN_GROUP",
        ),
    ),
    (
        "source_responsibility",
        (
            "PRIMARY",
            "SECONDARY",
            "CORROBORATING",
        ),
    ),
    (
        "source_access_state",
        (
            "OK",
            "BLOCKED",
            "MANUAL_ONLY",
        ),
    ),
    (
        "fetch_status",
        (
            "OK",
            "UNCHANGED",
            "HTTP_ERROR",
            "BLOCKED",
            "TIMEOUT",
            "PARSE_FAILED",
        ),
    ),
    (
        "extraction_status",
        (
            "OK",
            "PARTIAL",
            "FAILED",
        ),
    ),
    (
        "detection_type",
        (
            "AUTO_DIFF",
            "MANUAL_EDIT",
            "IMPORT",
            "CORRECTION",
        ),
    ),
    (
        "proposal_status",
        (
            "DRAFT",
            "PENDING",
            "PENDING_SECOND_REVIEW",
            "APPROVED",
            "RETURNED",
            "PUBLISHED",
            "DISCARDED",
        ),
    ),
    (
        "review_decision_kind",
        (
            "APPROVE",
            "RETURN",
            "CORRECT",
        ),
    ),
    (
        "review_task_state",
        (
            "OPEN",
            "COMPLETED",
            "ESCALATED",
            "REASSIGNED",
        ),
    ),
    (
        "entity_relationship_kind",
        (
            "SUPERSEDED_BY",
            "MERGED_INTO",
            "SPLIT_INTO",
        ),
    ),
    (
        "alias_kind",
        (
            "FORMER_NAME",
            "TRADE_NAME",
            "ABBREVIATION",
            "TRANSLITERATION",
            "EXTERNAL_ID",
        ),
    ),
    (
        "actor_type",
        (
            "USER",
            "SYSTEM",
            "API_CLIENT",
        ),
    ),
    (
        "outbox_status",
        (
            "PENDING",
            "INFLIGHT",
            "DELIVERED",
            "FAILED",
            "DEAD",
        ),
    ),
    (
        "change_kind",
        (
            "DEADLINE_CHANGED",
            "TUITION_CHANGED",
            "REQUIREMENT_CHANGED",
            "LANGUAGE_REQUIREMENT_CHANGED",
            "PROGRAM_OPENED",
            "PROGRAM_CLOSED",
            "PROGRAM_SUSPENDED",
            "OFFERING_ADDED",
            "OFFERING_WITHDRAWN",
            "INTAKE_ADDED",
            "RANKING_PUBLISHED",
            "PROFILE_UPDATED",
        ),
    ),
    (
        "root_entity_type",
        (
            "university",
            "program",
        ),
    ),
)


def upgrade() -> None:
    for type_name, members in ENUM_TYPES:
        values = ", ".join(f"'{member}'" for member in members)
        # IF NOT EXISTS is not available for CREATE TYPE, so guard on the catalog.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{type_name}') THEN
                    CREATE TYPE {type_name} AS ENUM ({values});
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for type_name, _members in reversed(ENUM_TYPES):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
