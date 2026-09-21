"""Step 5B: safe acquisition and immutable evidence capture

THE TRUST BOUNDARY WAS IN THE WRONG PLACE
=========================================
Step 5A made a source's *existence* depend on verification: `source_mapping` requires
a verified `official_domain`, and registration went through a mapping, so nothing
could be fetched until a human had finished verifying it.

That is backwards, and it made the review pass harder than it needed to be. Fetching a
page is how we find out what it is. Requiring the answer first means a reviewer has to
judge a URL they cannot see through our own record -- so they open it in a browser
instead, and the system learns nothing.

The two questions are now separate:

* **fetchable** -- may a worker send a request? Syntax, scheme, SSRF validation,
  whether the source is active, whether the site has told us to stop. A machine
  answers it, and `source.fetch_eligibility` records the answer.
* **publication eligible** -- may a fact from this source be published? Domain
  verification, mapping promotion, responsibility verification. Only a person answers
  it, and **C27 is unchanged**: `publication_eligibility` still defaults to
  `NOT_ELIGIBLE`, `source_eligibility_is_earned` still refuses a class that no
  promoted mapping vouches for, and the `field_claim` / `field_provenance` gates still
  refuse evidence whose class does not permit the fact.

The normal state for every source in the pilot is therefore `FETCHABLE` +
`NOT_ELIGIBLE`. We may look; nothing we see may be published yet. **A source existing
is not a trust signal** -- it means "this URL is a known acquisition target", and
`registered_by` says who said so.

No schema change was needed to allow that, which is worth recording: C27 always
permitted a `NOT_ELIGIBLE` source to exist without a mapping. What had to change was
the *code path* that created sources, plus a link from the workbook to the acquisition
target that does not run through `source_mapping`
(`pilot_collected_source.acquisition_source_id`).

LEASE FENCING (deferred from Step 3.5, now required)
====================================================
A lease that merely expires is not enough. Worker A stalls mid-fetch past
`lease_expires_at`; the sweeper marks the attempt ABANDONED; worker B claims a new
attempt; A wakes and finalises, writing an authoritative `fetch_run` and a snapshot for
work it no longer owns. Timestamps cannot prevent this, because the clock disagreement
is what caused it.

`fetch_attempt.lease_token` is the fence: a fresh UUID per claim, and every heartbeat
and finalisation is a conditional write requiring `state = 'RUNNING' AND lease_token =
<mine>`. A worker that lost its lease updates zero rows and is told so.
`lease_generation` counts claims, so "was this re-leased?" is answerable from the row.

304 IS NOT A 200 THAT MATCHED
=============================
A conditional request answered `304` produces a `fetch_run` with status `UNCHANGED`,
`http_status = 304`, `unchanged_content_hash` naming the blob the server confirmed --
and **no snapshot**, because no bytes arrived and a snapshot means "we saw these
bytes". A `200` returning identical bytes *did* transfer a body, so it produces a real
snapshot sharing the existing blob. Different transport observations, different
records; a CHECK stops anything but a 304 claiming unchanged content.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only.

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FETCH_ELIGIBILITY_VALUES: tuple[str, ...] = (
    "FETCHABLE",
    "BLOCKED",
    "DISABLED",
    "NEEDS_MANUAL_REVIEW",
)

#: CHECKs alembic cannot autogenerate, as (table, name, expression).
NEW_CHECKS: tuple[tuple[str, str, str], ...] = (
    # Two switches that could disagree is one switch too many.
    (
        "source",
        "an_inactive_source_is_not_fetchable",
        "is_active OR fetch_eligibility <> 'FETCHABLE'",
    ),
    # A blocked source must say what the site actually did: the operator cannot tell
    # a WAF from a typo otherwise, and the difference decides whether to retry at all.
    (
        "source",
        "blocked_names_what_happened",
        "fetch_eligibility <> 'BLOCKED' " "OR btrim(coalesce(fetch_eligibility_reason, '')) <> ''",
    ),
    ("source", "min_interval_non_negative", "min_interval_seconds >= 0"),
    # Fencing: a RUNNING attempt holds a token, a QUEUED one holds none -- so a token
    # left behind by an expired claim cannot be replayed against a requeued row.
    (
        "fetch_attempt",
        "a_queued_attempt_holds_no_token",
        "state <> 'QUEUED' OR lease_token IS NULL",
    ),
    ("fetch_attempt", "lease_generation_non_negative", "lease_generation >= 0"),
    # Only a 304 may claim unchanged content. A failed fetch asserting "nothing
    # changed" would silently extend the life of evidence nobody re-checked.
    (
        "fetch_run",
        "only_a_304_confirms_unchanged_content",
        "unchanged_content_hash IS NULL OR (status = 'UNCHANGED' AND http_status = 304)",
    ),
    ("fetch_run", "bytes_non_negative", "bytes_downloaded IS NULL OR bytes_downloaded >= 0"),
)

#: The `source_type` CHECK gains `unclassified`, because a page supplied with no
#: stated category (Step 5A, D32) is still an acquisition target. Guessing a type from
#: the URL would be the inference D32 refuses.
OLD_SOURCE_TYPES = (
    "'university_site', 'faculty_site', 'admissions_page', 'fee_page', "
    "'official_pdf', 'government_regulator', 'authorized_ranking'"
)
NEW_SOURCE_TYPES = OLD_SOURCE_TYPES + ", 'unclassified'"

#: The fencing CHECK, replaced rather than added: it already exists and must now also
#: require a token.
RUNNING_IS_LEASED = (
    "state <> 'RUNNING' OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
    "AND lease_expires_at IS NOT NULL AND lease_token IS NOT NULL)"
)
OLD_RUNNING_IS_LEASED = (
    "state <> 'RUNNING' OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
    "AND lease_expires_at IS NOT NULL)"
)

SOURCE_HEALTH_VIEW = "source_health"

#: Operational health, derived from immutable history rather than maintained beside
#: it. A view cannot drift from the runs it summarises, and there is no second write
#: to forget: "consecutive failures" is a question about `fetch_run`, so it is asked
#: of `fetch_run`.
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
           -- `id DESC` is the last tiebreak and a random uuid, so two runs sharing a
           -- `started_at` to the microsecond order arbitrarily. Real fetches are
           -- seconds apart; a caller that needs a total order must space them.
           row_number() OVER (PARTITION BY r.source_id ORDER BY r.started_at DESC, r.id DESC)
               AS recency,
           -- Runs since the most recent success. A window over a boolean marker is
           -- cheaper and clearer than a recursive walk, and gives the streak directly.
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
           -- Stale is about age, not failure: a source succeeding every time and
           -- last checked 60 days ago is a different problem from one erroring.
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

ACQUISITION_TARGET_VIEW = "acquisition_target"

#: What the scheduler reads: one row per *physical page*, with its workbook lineage
#: and the claims riding on it. Built on `pilot_collected_source` rather than on
#: `source_mapping`, because a mapping needs a verified host and nothing is verified
#: yet -- and because scheduling on claims would fetch the same page three times.
ACQUISITION_TARGET_SQL = """
CREATE VIEW acquisition_target AS
SELECT src.id                                   AS source_id,
       src.url,
       src.url_hash,
       src.source_type,
       src.fetch_strategy,
       src.fetch_eligibility::text              AS fetch_eligibility,
       src.publication_eligibility::text        AS publication_eligibility,
       src.access_state::text                   AS access_state,
       src.is_active,
       src.min_interval_seconds,
       physical.submission_id,
       physical.source_ref                      AS physical_source_ref,
       physical.target_institution_id,
       physical.official_url                    AS workbook_url,
       physical.normalized_url,
       physical.host,
       physical.workbook_column                 AS first_claimed_by_column,
       ti.match_key,
       ti.destination_code,
       ti.pilot_wave,
       claims.claim_count,
       claims.categories,
       claims.source_refs
  FROM source src
  -- The physical row: the one whose URL this source was registered from.
  JOIN pilot_collected_source physical
    ON physical.acquisition_source_id = src.id
   AND physical.duplicate_of_source_ref IS NULL
  JOIN target_institution ti ON ti.id = physical.target_institution_id
  LEFT JOIN LATERAL (
        SELECT count(*)                                        AS claim_count,
               array_agg(DISTINCT c.source_type ORDER BY c.source_type) AS categories,
               array_agg(c.source_ref ORDER BY c.source_ref)    AS source_refs
          FROM pilot_collected_source c
         WHERE c.acquisition_source_id = src.id
  ) claims ON TRUE
"""

VIEW_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        -- app_publisher is absent from acquisition_target for the same reason it is
        -- absent from the staging tables (C27): the view exposes workbook columns.
        -- It may read source_health, which is operational and carries no workbook text.
        EXECUTE 'GRANT SELECT ON acquisition_target TO app_api, app_worker';
        EXECUTE 'GRANT SELECT ON source_health TO app_api, app_worker';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
        EXECUTE 'GRANT SELECT ON source_health TO app_publisher';
    END IF;
END
$$;
"""


def upgrade() -> None:
    values = ", ".join("'" + value + "'" for value in FETCH_ELIGIBILITY_VALUES)
    op.execute("CREATE TYPE fetch_eligibility AS ENUM (" + values + ")")

    _columns()

    op.execute(
        "ALTER TABLE source DROP CONSTRAINT ck_source_source_type_known, "
        "ADD CONSTRAINT ck_source_source_type_known "
        "CHECK (source_type IN (" + NEW_SOURCE_TYPES + "))"
    )
    op.execute(
        "ALTER TABLE fetch_attempt DROP CONSTRAINT ck_fetch_attempt_running_attempt_is_leased, "
        "ADD CONSTRAINT ck_fetch_attempt_running_attempt_is_leased "
        "CHECK (" + RUNNING_IS_LEASED + ")"
    )
    for table, name, expression in NEW_CHECKS:
        op.execute(
            "ALTER TABLE "
            + table
            + " ADD CONSTRAINT ck_"
            + table
            + "_"
            + name
            + " CHECK ("
            + expression
            + ")"
        )

    op.execute(SOURCE_HEALTH_SQL)
    op.execute(
        "COMMENT ON VIEW source_health IS "
        "'Operational health derived from immutable fetch history. A view, so it "
        "cannot drift from the runs it summarises.'"
    )
    op.execute(ACQUISITION_TARGET_SQL)
    op.execute(
        "COMMENT ON VIEW acquisition_target IS "
        "'One row per physical page to fetch, with workbook lineage and the claims "
        "riding on it. FETCHABLE is not publication eligibility.'"
    )
    op.execute(VIEW_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS acquisition_target")
    op.execute("DROP VIEW IF EXISTS source_health")

    for table, name, _ in NEW_CHECKS:
        op.execute("ALTER TABLE " + table + " DROP CONSTRAINT ck_" + table + "_" + name)
    op.execute(
        "ALTER TABLE fetch_attempt DROP CONSTRAINT ck_fetch_attempt_running_attempt_is_leased, "
        "ADD CONSTRAINT ck_fetch_attempt_running_attempt_is_leased "
        "CHECK (" + OLD_RUNNING_IS_LEASED + ")"
    )
    op.execute(
        "ALTER TABLE source DROP CONSTRAINT ck_source_source_type_known, "
        "ADD CONSTRAINT ck_source_source_type_known "
        "CHECK (source_type IN (" + OLD_SOURCE_TYPES + "))"
    )

    _drop_columns()
    op.execute("DROP TYPE fetch_eligibility")


def _columns() -> None:
    op.add_column(
        "fetch_attempt",
        sa.Column(
            "lease_token",
            sa.UUID(),
            nullable=True,
            comment="Fencing token; minted per claim, never reused",
        ),
    )
    op.add_column(
        "fetch_attempt",
        sa.Column("lease_generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "fetch_run",
        sa.Column(
            "unchanged_content_hash",
            sa.String(length=64),
            nullable=True,
            comment="304 only: the blob the server said is still current",
        ),
    )
    op.add_column(
        "fetch_run",
        sa.Column("conditional_request_sent", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("fetch_run", sa.Column("bytes_downloaded", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_fetch_run_unchanged_content_hash_content_blob"),
        "fetch_run",
        "content_blob",
        ["unchanged_content_hash"],
        ["content_hash"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "pilot_collected_source", sa.Column("acquisition_source_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "ix_pilot_collected_source_acquisition",
        "pilot_collected_source",
        ["acquisition_source_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_pilot_collected_source_acquisition_source_id_source"),
        "pilot_collected_source",
        "source",
        ["acquisition_source_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("snapshot", sa.Column("etag", sa.String(length=256), nullable=True))
    op.add_column(
        "snapshot",
        sa.Column(
            "last_modified",
            sa.String(length=128),
            nullable=True,
            comment="Verbatim HTTP-date string; never reinterpreted (D17)",
        ),
    )
    op.add_column("snapshot", sa.Column("content_type", sa.String(length=256), nullable=True))
    op.add_column(
        "snapshot",
        sa.Column(
            "redirect_chain",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "snapshot",
        sa.Column(
            "technical_metadata",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_snapshot_source_latest", "snapshot", ["source_id", "observed_at", "id"], unique=False
    )
    op.add_column(
        "source",
        sa.Column(
            "fetch_eligibility",
            postgresql.ENUM(
                "FETCHABLE",
                "BLOCKED",
                "DISABLED",
                "NEEDS_MANUAL_REVIEW",
                name="fetch_eligibility",
                create_type=False,
            ),
            server_default="NEEDS_MANUAL_REVIEW",
            nullable=False,
            comment="Step 5B: may a worker send a request? NOT publication eligibility. Defaults closed so a row created without passing validation is not fetched by default.",
        ),
    )
    op.add_column(
        "source",
        sa.Column(
            "fetch_eligibility_reason",
            sa.Text(),
            nullable=True,
            comment="Why this state; for BLOCKED it is what the site actually did",
        ),
    )
    op.add_column(
        "source", sa.Column("fetch_eligibility_set_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "source",
        sa.Column(
            "min_interval_seconds",
            sa.Integer(),
            server_default="2",
            nullable=False,
            comment="Floor between requests to this host",
        ),
    )
    op.create_index("ix_source_fetch_eligibility", "source", ["fetch_eligibility"], unique=False)


def _drop_columns() -> None:
    op.drop_index("ix_source_fetch_eligibility", table_name="source")
    op.drop_column("source", "min_interval_seconds")
    op.drop_column("source", "fetch_eligibility_set_at")
    op.drop_column("source", "fetch_eligibility_reason")
    op.drop_column("source", "fetch_eligibility")
    op.drop_index("ix_snapshot_source_latest", table_name="snapshot")
    op.drop_column("snapshot", "technical_metadata")
    op.drop_column("snapshot", "redirect_chain")
    op.drop_column("snapshot", "content_type")
    op.drop_column("snapshot", "last_modified")
    op.drop_column("snapshot", "etag")
    op.drop_constraint(
        op.f("fk_pilot_collected_source_acquisition_source_id_source"),
        "pilot_collected_source",
        type_="foreignkey",
    )
    op.drop_index("ix_pilot_collected_source_acquisition", table_name="pilot_collected_source")
    op.drop_column("pilot_collected_source", "acquisition_source_id")
    op.drop_constraint(
        op.f("fk_fetch_run_unchanged_content_hash_content_blob"), "fetch_run", type_="foreignkey"
    )
    op.drop_column("fetch_run", "bytes_downloaded")
    op.drop_column("fetch_run", "conditional_request_sent")
    op.drop_column("fetch_run", "unchanged_content_hash")
    op.drop_column("fetch_attempt", "lease_generation")
    op.drop_column("fetch_attempt", "lease_token")
