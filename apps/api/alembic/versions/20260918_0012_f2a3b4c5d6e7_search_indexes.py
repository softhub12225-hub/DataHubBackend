"""Domain group 10: search and access-path indexes

Indexes that belong to a *query* rather than to a table's structure, and are
therefore not declared on the models: trigram, full-text, and the GiST index over
the calendar range.

Every index here exists for a query the platform is known to run. Speculative
indexes are not free — each one slows every write and consumes cache — so nothing is
added "just in case".

| Index | The query it serves |
|---|---|
| trigram on institution/program names | FR-01 name search, tolerant of typos and partial words |
| trigram on `entity_alias.value` | finding an institution by a former or trade name |
| GIN full-text on names | multi-word English search |
| GiST on `deadline_cal_range` | FR-01 deadline filtering across mixed precision (C13) |
| partial on `deadline_instant_utc` | exact ordering for the facts that have an instant |
| partial on rolling/until-filled | "still open" filters, without scanning dated rows |
| review queue partials | the pending/overdue queue, the most frequent ops read |
| `fetch_run` failure partial | source-health dashboard and the consecutive-failure rule |
| provenance / version lookups | field history and whole-root provenance loads (D13) |
| `change_event` feed indexes | Latest University Updates (D12) |

Ordinary single-column and composite indexes live with their tables, in the group
migration that creates them: a table and its access paths are easier to review
together.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Deliberately no import from `app.db.classification`: a historical migration must
# not read live application state (C21). See the note in `upgrade()`.

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (index name, DDL). Raw SQL because expression and operator-class indexes read far
# more clearly this way than through the SQLAlchemy shims.
INDEXES: tuple[tuple[str, str], ...] = (
    # --- Name search -------------------------------------------------------
    (
        "ix_university_name_en_trgm",
        "CREATE INDEX ix_university_name_en_trgm ON university " "USING gin (name_en gin_trgm_ops)",
    ),
    (
        "ix_university_name_zh_trgm",
        "CREATE INDEX ix_university_name_zh_trgm ON university " "USING gin (name_zh gin_trgm_ops)",
    ),
    (
        "ix_program_name_en_trgm",
        "CREATE INDEX ix_program_name_en_trgm ON program USING gin (name_en gin_trgm_ops)",
    ),
    (
        "ix_program_name_zh_trgm",
        "CREATE INDEX ix_program_name_zh_trgm ON program USING gin (name_zh gin_trgm_ops)",
    ),
    (
        "ix_entity_alias_value_trgm",
        "CREATE INDEX ix_entity_alias_value_trgm ON entity_alias " "USING gin (value gin_trgm_ops)",
    ),
    # Full text over the English names only. PostgreSQL has no Chinese text-search
    # configuration, and the 'simple' one would split Chinese incorrectly, so
    # Chinese search relies on the trigram indexes above.
    (
        "ix_program_name_en_fts",
        "CREATE INDEX ix_program_name_en_fts ON program "
        "USING gin (to_tsvector('english', coalesce(name_en, '')))",
    ),
    (
        "ix_university_name_en_fts",
        "CREATE INDEX ix_university_name_en_fts ON university "
        "USING gin (to_tsvector('english', coalesce(name_en, '')))",
    ),
    # --- Deadlines (C13) ---------------------------------------------------
    # The only index that can serve a date filter across mixed precision. A
    # daterange, so nothing in the index implies a timezone.
    (
        "ix_application_deadline_cal_range",
        "CREATE INDEX ix_application_deadline_cal_range ON application_deadline "
        "USING gist (deadline_cal_range)",
    ),
    # Exact ordering, for the subset of facts that genuinely have an instant.
    (
        "ix_application_deadline_instant_utc",
        "CREATE INDEX ix_application_deadline_instant_utc ON application_deadline "
        "(deadline_instant_utc) WHERE deadline_instant_utc IS NOT NULL",
    ),
    # Calendar-component ordering for everything else: there is no instant to sort
    # mixed-precision facts by, so the parts are the sort key.
    (
        "ix_application_deadline_calendar_parts",
        "CREATE INDEX ix_application_deadline_calendar_parts ON application_deadline "
        "(deadline_year, deadline_month, deadline_day NULLS FIRST)",
    ),
    (
        "ix_application_deadline_open_ended",
        "CREATE INDEX ix_application_deadline_open_ended ON application_deadline "
        "(round_id) WHERE deadline_kind IN ('ROLLING', 'UNTIL_FILLED')",
    ),
    # --- Review queue ------------------------------------------------------
    (
        "ix_change_proposal_pending_sla",
        "CREATE INDEX ix_change_proposal_pending_sla ON change_proposal "
        "(sla_due_at) WHERE status IN ('PENDING', 'PENDING_SECOND_REVIEW')",
    ),
    (
        "ix_review_task_open_assignee",
        "CREATE INDEX ix_review_task_open_assignee ON review_task "
        "(assignee_id, sla_due_at) WHERE state = 'OPEN'",
    ),
    # --- Source health -----------------------------------------------------
    # Successes are the vast majority, so a partial index stays small.
    (
        "ix_fetch_run_failures",
        "CREATE INDEX ix_fetch_run_failures ON fetch_run (source_id, started_at) "
        "WHERE status <> 'OK'",
    ),
    # --- History lookups ---------------------------------------------------
    (
        "ix_field_provenance_root_lookup",
        "CREATE INDEX ix_field_provenance_root_lookup ON field_provenance "
        "(root_type, root_id, root_version_no DESC)",
    ),
    (
        "ix_field_provenance_field_history",
        "CREATE INDEX ix_field_provenance_field_history ON field_provenance "
        "(entity_type, entity_id, field_path, root_version_no DESC)",
    ),
    (
        "ix_entity_version_root_desc",
        "CREATE INDEX ix_entity_version_root_desc ON entity_version "
        "(root_type, root_id, version_no DESC)",
    ),
    # --- Latest Updates feed (D12) ----------------------------------------
    (
        "ix_change_event_feed",
        "CREATE INDEX ix_change_event_feed ON change_event (published_at DESC)",
    ),
    (
        "ix_change_event_high_risk_feed",
        "CREATE INDEX ix_change_event_high_risk_feed ON change_event "
        "(published_at DESC) WHERE risk_level = 'HIGH'",
    ),
)


def upgrade() -> None:
    # This migration previously asserted that its own index names were exactly
    # `app.db.classification.MIGRATION_OWNED_INDEXES`. That was the C21 defect: a
    # historical migration must not read live application state, because a later
    # revision adding an index then makes a *fresh* database fail to build while an
    # already-upgraded one is fine.
    #
    # Revision 0016 adds four more migration-owned indexes, so no single revision's
    # list can equal the whole set any more. The agreement between the migrations and
    # the exclusion list that `alembic/env.py` uses is now checked where it belongs,
    # against the built database:
    # `tests/integration/test_schema.py::test_migration_owned_indexes_exist_and_are_complete`.
    for _name, ddl in INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
