"""Step 5B.2: recovery from temporary failure, and a way back from a permanent one

WHY
===
Four operational blockers stood between the smoke test and a 319-page run, and three of
them share a shape: **a temporary condition was recorded as a permanent one, and nothing
could undo it.**

A single `429` set `fetch_eligibility = 'BLOCKED'`. A resolver timeout took the same
path as *"this hostname resolves to loopback"*. And no code anywhere set
`fetch_eligibility` back to `FETCHABLE` -- registration is `ON CONFLICT DO NOTHING` by
design, so re-importing could not clear it either. One rate limit or one DNS hiccup
during a 319-page run would silently drop a legitimate page for good.

The fix separates two questions that were conflated:

* **May we fetch this at all?** `fetch_eligibility` -- a durable judgement, changed by a
  machine only on evidence of refusal, and otherwise only by an audited human action.
* **May we fetch it *now*?** `cooldown_until` -- operational timing, set and cleared
  automatically, carrying no judgement about the source whatsoever.

A cooldown is deliberately **not** a new `fetch_eligibility` member. Making it one would
mean every reader of that column had to know that one of its values expires, and the
scheduler would be the only thing that could tell you whether a source was really
blocked. Timing state belongs in a timestamp.

HOST COOLDOWN
=============
A `429` is a statement about the host, not only about the page. One pilot host serves
eleven pages, so honouring the throttle on the one page we happened to ask for and then
immediately asking for ten more is not honouring it at all. `host_cooldown` is one row
per hostname, consulted by the scheduler -- the smallest thing that is actually safe,
and persisted rather than in-process because a cooldown that a new worker forgets is not
a cooldown.

Revision ID: b7c8d9e0f1a2
Revises: f4a5b6c7d8e9
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# No import from app.* (C21/C22): frozen literals only. Three separate revisions have
# made that mistake and each one broke fresh-database builds while upgraded ones stayed
# green, so the view SQL below is a copy on purpose.

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: New `fetch_status` members. Added rather than folded into existing ones because the
#: operational consequence differs: `RATE_LIMITED` earns a cooldown and a later retry,
#: `DNS_TEMPORARY` earns a backoff, `NAME_NOT_RESOLVED` earns a human, and
#: `INTERNAL_ERROR` earns none of those -- it is our bug, not the site's behaviour, and
#: retrying it would hammer a university over a defect on our side.
NEW_FETCH_STATUSES = (
    "RATE_LIMITED",
    "DNS_TEMPORARY",
    "NAME_NOT_RESOLVED",
    "INTERNAL_ERROR",
)

#: Operational health and *schedulability* are different questions and the view now
#: answers both. `health` is about the source's record; `schedule_state` is what a
#: scheduler needs and is the only one that knows about cooldown.
#:
#: Two branch orders matter here and are not arbitrary:
#:   * `NEEDS_MANUAL_REVIEW` is tested before `BLOCKED`, because a source a person has
#:     been asked to look at should not read as "the site refused us";
#:   * cooldown and the temporary statuses map to `DEGRADED`, never `BLOCKED` or
#:     `FAILING` -- a single 429 reporting `BLOCKED` is the defect this revision exists
#:     to remove.
SOURCE_HEALTH_SQL = """
CREATE VIEW source_health AS
WITH ordered AS (
    SELECT r.source_id,
           r.id,
           r.status::text        AS status,
           r.http_status,
           r.error_class,
           r.started_at,
           r.finished_at,
           r.unchanged_content_hash,
           row_number() OVER (PARTITION BY r.source_id ORDER BY r.started_at DESC, r.id DESC)
               AS recency,
           sum(CASE WHEN r.status IN ('OK', 'UNCHANGED') THEN 1 ELSE 0 END)
               OVER (PARTITION BY r.source_id ORDER BY r.started_at DESC, r.id DESC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS successes_so_far
      FROM fetch_run r
),
latest AS (
    SELECT * FROM ordered WHERE recency = 1
),
streak AS (
    SELECT source_id, count(*) AS consecutive_failures
      FROM ordered
     WHERE successes_so_far = 0
     GROUP BY source_id
),
last_success AS (
    SELECT DISTINCT ON (source_id) source_id, started_at, id
      FROM fetch_run
     WHERE status IN ('OK', 'UNCHANGED')
     ORDER BY source_id, started_at DESC, id DESC
),
last_content AS (
    SELECT DISTINCT ON (s.source_id) s.source_id, s.content_hash, s.observed_at,
           s.effective_url, s.content_type
      FROM snapshot s
     ORDER BY s.source_id, s.observed_at DESC, s.id DESC
),
counts AS (
    SELECT source_id, count(*) AS total_runs FROM fetch_run GROUP BY source_id
)
SELECT src.id                                   AS source_id,
       src.url,
       src.source_type,
       src.fetch_eligibility::text              AS fetch_eligibility,
       src.access_state::text                   AS access_state,
       src.publication_eligibility::text        AS publication_eligibility,
       src.is_active,
       src.cooldown_until,
       src.cooldown_reason,
       src.rate_limit_strikes,
       host_cool.cooldown_until                 AS host_cooldown_until,
       coalesce(counts.total_runs, 0)           AS total_runs,
       latest.started_at                        AS last_attempt_at,
       latest.status                            AS last_status,
       latest.http_status                       AS last_http_status,
       latest.error_class                       AS last_error_class,
       last_success.started_at                  AS last_success_at,
       coalesce(streak.consecutive_failures, 0) AS consecutive_failures,
       last_content.content_hash                AS last_content_hash,
       last_content.observed_at                 AS last_content_observed_at,
       last_content.effective_url               AS last_effective_url,
       last_content.content_type                AS last_content_type,
       CASE
           WHEN NOT src.is_active                       THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'DISABLED'      THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'NEEDS_MANUAL_REVIEW'
                                                        THEN 'NEEDS_MANUAL_REVIEW'
           WHEN src.fetch_eligibility = 'BLOCKED'       THEN 'BLOCKED'
           WHEN src.access_state <> 'OK'                THEN 'BLOCKED'
           WHEN latest.source_id IS NULL                THEN 'NEVER_FETCHED'
           -- Temporary by nature: the site asked us to wait, or the resolver did not
           -- answer. Neither says the source is bad.
           WHEN latest.status IN ('RATE_LIMITED', 'DNS_TEMPORARY') THEN 'DEGRADED'
           WHEN src.cooldown_until IS NOT NULL AND src.cooldown_until > now()
                                                        THEN 'DEGRADED'
           WHEN coalesce(streak.consecutive_failures, 0) >= 3  THEN 'FAILING'
           WHEN coalesce(streak.consecutive_failures, 0) >= 1  THEN 'DEGRADED'
           WHEN last_success.started_at < now() - interval '30 days' THEN 'STALE'
           ELSE 'HEALTHY'
       END                                      AS health,
       CASE
           WHEN NOT src.is_active                       THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'DISABLED'      THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'NEEDS_MANUAL_REVIEW'
                                                        THEN 'NEEDS_MANUAL_REVIEW'
           WHEN src.fetch_eligibility = 'BLOCKED'       THEN 'BLOCKED'
           WHEN src.access_state <> 'OK'                THEN 'BLOCKED'
           WHEN src.cooldown_until IS NOT NULL AND src.cooldown_until > now()
                                                        THEN 'COOLDOWN'
           WHEN host_cool.cooldown_until IS NOT NULL AND host_cool.cooldown_until > now()
                                                        THEN 'COOLDOWN'
           ELSE 'FETCHABLE_NOW'
       END                                      AS schedule_state
  FROM source src
  LEFT JOIN counts       ON counts.source_id = src.id
  LEFT JOIN latest       ON latest.source_id = src.id
  LEFT JOIN streak       ON streak.source_id = src.id
  LEFT JOIN last_success ON last_success.source_id = src.id
  LEFT JOIN last_content ON last_content.source_id = src.id
  LEFT JOIN host_cooldown host_cool
         ON host_cool.host = lower(split_part(
                regexp_replace(src.url, '^[^:]+://([^/?#]*).*$', '\\1'), ':', 1))
"""

#: The previous definition, restored on downgrade. A downgrade that left the new
#: columns referenced would fail at the first read rather than at the migration.
OLD_SOURCE_HEALTH_SQL = """
CREATE VIEW source_health AS
WITH ordered AS (
    SELECT r.source_id,
           r.id,
           r.status::text        AS status,
           r.http_status,
           r.error_class,
           r.started_at,
           r.finished_at,
           r.unchanged_content_hash,
           row_number() OVER (PARTITION BY r.source_id ORDER BY r.started_at DESC, r.id DESC)
               AS recency,
           sum(CASE WHEN r.status IN ('OK', 'UNCHANGED') THEN 1 ELSE 0 END)
               OVER (PARTITION BY r.source_id ORDER BY r.started_at DESC, r.id DESC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS successes_so_far
      FROM fetch_run r
),
latest AS (
    SELECT * FROM ordered WHERE recency = 1
),
streak AS (
    SELECT source_id, count(*) AS consecutive_failures
      FROM ordered
     WHERE successes_so_far = 0
     GROUP BY source_id
),
last_success AS (
    SELECT DISTINCT ON (source_id) source_id, started_at, id
      FROM fetch_run
     WHERE status IN ('OK', 'UNCHANGED')
     ORDER BY source_id, started_at DESC, id DESC
),
last_content AS (
    SELECT DISTINCT ON (s.source_id) s.source_id, s.content_hash, s.observed_at,
           s.effective_url, s.content_type
      FROM snapshot s
     ORDER BY s.source_id, s.observed_at DESC, s.id DESC
),
counts AS (
    SELECT source_id, count(*) AS total_runs FROM fetch_run GROUP BY source_id
)
SELECT src.id                                   AS source_id,
       src.url,
       src.source_type,
       src.fetch_eligibility::text              AS fetch_eligibility,
       src.access_state::text                   AS access_state,
       src.publication_eligibility::text        AS publication_eligibility,
       src.is_active,
       coalesce(counts.total_runs, 0)           AS total_runs,
       latest.started_at                        AS last_attempt_at,
       latest.status                            AS last_status,
       latest.http_status                       AS last_http_status,
       latest.error_class                       AS last_error_class,
       last_success.started_at                  AS last_success_at,
       coalesce(streak.consecutive_failures, 0) AS consecutive_failures,
       last_content.content_hash                AS last_content_hash,
       last_content.observed_at                 AS last_content_observed_at,
       last_content.effective_url               AS last_effective_url,
       last_content.content_type                AS last_content_type,
       CASE
           WHEN NOT src.is_active                       THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'DISABLED'      THEN 'DISABLED'
           WHEN src.fetch_eligibility = 'BLOCKED'       THEN 'BLOCKED'
           WHEN src.access_state <> 'OK'                THEN 'BLOCKED'
           WHEN latest.source_id IS NULL                THEN 'NEVER_FETCHED'
           WHEN coalesce(streak.consecutive_failures, 0) >= 3  THEN 'FAILING'
           WHEN coalesce(streak.consecutive_failures, 0) >= 1  THEN 'DEGRADED'
           WHEN last_success.started_at < now() - interval '30 days' THEN 'STALE'
           ELSE 'HEALTHY'
       END                                      AS health
  FROM source src
  LEFT JOIN counts       ON counts.source_id = src.id
  LEFT JOIN latest       ON latest.source_id = src.id
  LEFT JOIN streak       ON streak.source_id = src.id
  LEFT JOIN last_success ON last_success.source_id = src.id
  LEFT JOIN last_content ON last_content.source_id = src.id
"""


def upgrade() -> None:
    for value in NEW_FETCH_STATUSES:
        op.execute(f"ALTER TYPE fetch_status ADD VALUE IF NOT EXISTS '{value}'")

    # --- operational timing on the source -----------------------------------------
    op.add_column(
        "source",
        sa.Column(
            "cooldown_until",
            sa.TIMESTAMP(timezone=True),
            comment="Not schedulable before this instant. Expires on its own; no judgement.",
        ),
    )
    op.add_column("source", sa.Column("cooldown_reason", sa.Text()))
    op.add_column("source", sa.Column("cooldown_set_at", sa.TIMESTAMP(timezone=True)))
    op.add_column(
        "source",
        sa.Column(
            "rate_limit_strikes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "cooldown_names_its_reason",
        "source",
        "cooldown_until IS NULL OR cooldown_reason IS NOT NULL",
    )
    op.create_check_constraint(
        "rate_limit_strikes_non_negative",
        "source",
        "rate_limit_strikes >= 0",
    )
    # Partial: the scheduler asks "is anything in cooldown", and the answer is almost
    # always a handful of rows out of 319.
    op.create_index(
        "ix_source_cooldown_until",
        "source",
        ["cooldown_until"],
        postgresql_where=sa.text("cooldown_until IS NOT NULL"),
    )

    # --- host-level cooldown -------------------------------------------------------
    op.create_table(
        "host_cooldown",
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("cooldown_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "set_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Which page provoked it, for the operator who asks why a host is quiet.
        # ON DELETE SET NULL: the cooldown outlives the source row's removal, because
        # the host is still owed its pause.
        sa.Column("triggered_by_source_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["triggered_by_source_id"],
            ["source.id"],
            name=op.f("fk_host_cooldown_triggered_by_source_id_source"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("host", name=op.f("pk_host_cooldown")),
        sa.CheckConstraint("host = lower(host)", name=op.f("ck_host_cooldown_host_is_lowercase")),
        sa.CheckConstraint("length(host) > 0", name=op.f("ck_host_cooldown_host_not_empty")),
    )
    op.create_index("ix_host_cooldown_until", "host_cooldown", ["cooldown_until"])

    # --- the view, rebuilt over the new columns ------------------------------------
    op.execute("DROP VIEW IF EXISTS source_health")
    op.execute(SOURCE_HEALTH_SQL)
    op.execute(
        "COMMENT ON VIEW source_health IS "
        "'Operational health and schedulability, derived from immutable fetch history "
        "plus operational state. `health` describes the record; `schedule_state` is "
        "what a scheduler reads and is the only one that knows about cooldown. "
        "Carries no publication trust: see source.publication_eligibility (C27).'"
    )

    # `app_worker` owns the fetch plane and therefore the cooldowns; `app_api` may
    # read them and nothing else. `app_publisher` gets neither -- a publisher has no
    # business knowing which pages are currently quiet.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON host_cooldown TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON host_cooldown TO app_api';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON source_health TO app_api, app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
                EXECUTE 'GRANT SELECT ON source_health TO app_publisher';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS source_health")
    op.drop_index("ix_host_cooldown_until", table_name="host_cooldown")
    op.drop_table("host_cooldown")
    op.drop_index("ix_source_cooldown_until", table_name="source")
    op.drop_constraint(op.f("ck_source_rate_limit_strikes_non_negative"), "source", type_="check")
    op.drop_constraint(op.f("ck_source_cooldown_names_its_reason"), "source", type_="check")
    op.drop_column("source", "rate_limit_strikes")
    op.drop_column("source", "cooldown_set_at")
    op.drop_column("source", "cooldown_reason")
    op.drop_column("source", "cooldown_until")
    op.execute(OLD_SOURCE_HEALTH_SQL)
    op.execute(
        "COMMENT ON VIEW source_health IS "
        "'Operational health derived from immutable fetch history. Carries no "
        "publication trust: see source.publication_eligibility (C27).'"
    )
    # PostgreSQL cannot remove an enum value. The four new `fetch_status` members stay
    # in the type after a downgrade, which is harmless -- nothing references them once
    # the code is rolled back -- and is stated here rather than silently tolerated.
