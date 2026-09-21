"""Step 5A: the official source list, and the URL/responsibility split

WHAT THE REAL FILE FORCED
=========================
The client's final pilot file is `university_official_sources.xlsx`: 35 institutions,
one row each, eleven URL columns. Reading it before writing anything against it
produced two facts that the staging schema could not represent.

**1. One page legitimately answers for several categories.** Of its 385 URL cells,
only 319 are distinct: 66 repeat a URL already given under another heading. Imperial's
`/study/apply/` is both the application-deadlines page and the postgraduate-admissions
page; 32 of the 35 "Important Additional Source" cells repeat a core column outright.

The old `UNIQUE (submission_id, url_sha256)` asserted *one URL = one row*, so importing
this file would have thrown away 66 claimed responsibilities or failed outright. It is
replaced by an explicit split:

* a `pilot_collected_source` row is **one claimed responsibility** — this URL, for this
  category;
* `duplicate_of_source_ref` names the row that first registered that URL, so the rows
  with NULL there are the **distinct physical pages**;
* a partial unique index enforces one physical page per URL per institution.

Acquisition later follows the NULL rows and fetches each page once. Review follows
every row, because "this page is on the university's domain" and "this page is the
authority for tuition" are different questions, and only the second is asked per
category.

**2. A supplied page may have no category.** Three of the additional-source URLs are
genuinely new — a Stanford fee-rates page, an HKUST admission flyer, an Imperial
undergraduate landing page. Nothing in the workbook says what they are, and guessing
from the column heading would be inventing a claim: calling them `OFFICIAL_PDF`
because they arrived in the extra column is exactly the kind of inference this system
refuses everywhere else. They import as `UNCLASSIFIED`, which is a staging-only value
and deliberately **not** a `SourceCategory` member, so no canonical row can carry it.

A CHECK allows an unclassified page to be rejected — "not a page we want" needs no
category — but not verified, because verification asserts a page is authoritative
*for something* and there is nothing yet for it to be authoritative for.

WHY THE SELECTED-INSTITUTION CHECK IS LOOSENED
==============================================
`selected_names_itself` required an official English name. This workbook's University
Name column holds "exact original QS target-list labels" — its own README says so — so
it is the *client's list* label, not a collected official name. Writing it into a
column meaning "what the institution calls itself" is precisely the confusion D18
exists to prevent, and `university.name_en` is governed by C27 anyway, so the string
was never going to be published.

The replacement, `selected_is_reachable`, requires the homepage instead: a selected
institution is one we are about to schedule acquisition against, and a row with no
homepage names nothing. The generic collection template still asks for a real official
name and its validator still reports a missing one — a template rule, kept in the
template.

SCOPE
=====
`pilot_submission` gains `submission_kind` and `defines_pilot_scope`. An
official-source list says *which* institutions the pilot covers; a collection workbook
says what was read about them. A CHECK stops a facts workbook claiming scope, because
otherwise a workbook that merely omitted a university would silently drop it.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# No import from app.* (C21/C22): frozen literals only.

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every `source_category` member as of this revision, plus the staging-only
#: `UNCLASSIFIED`. Frozen: adding a category later is its own migration, and a test
#: asserts this list still matches `SourceCategory` plus the sentinel.
STAGED_SOURCE_TYPES: tuple[str, ...] = (
    "ACADEMIC_CALENDAR",
    "APPLICATION_DEADLINES",
    "AUTHORIZED_RANKING",
    "ENTRY_REQUIREMENTS",
    "FACULTY_OR_SCHOOL",
    "GOVERNMENT",
    "LANGUAGE_REQUIREMENTS",
    "OFFICIAL_PDF",
    "PHD_ADMISSIONS",
    "POSTGRADUATE_ADMISSIONS",
    "PROGRAM_CATALOG",
    "PROGRAM_PAGE",
    "TUITION_FEES",
    "UNDERGRADUATE_ADMISSIONS",
    "UNIVERSITY_HOME",
    "UNCLASSIFIED",
)

SUBMISSION_KINDS: tuple[str, ...] = ("OFFICIAL_SOURCE_LIST", "COLLECTION_WORKBOOK")


QUEUE_VIEW = "pilot_source_verification_queue"

#: The view as revision 0021 built it, kept verbatim so the downgrade restores
#: exactly what was there rather than an approximation of it.
PREVIOUS_QUEUE_VIEW_SQL = """
CREATE VIEW pilot_source_verification_queue AS
WITH collector_name AS (
    -- The collector's own claim about the official name, from the same submission.
    -- Not `university.name_en`: that column is governed (C27) and this name has no
    -- source and no status behind it.
    SELECT su.submission_id,
           su.target_institution_id,
           su.official_name_en,
           su.is_selected
      FROM pilot_selected_university su
),
list_name AS (
    -- The name the client's list used, for recognition only. Never published.
    SELECT DISTINCT ON (e.target_institution_id)
           e.target_institution_id,
           e.qs_name
      FROM target_list_entry e
      JOIN target_list l ON l.id = e.target_list_id
     ORDER BY e.target_institution_id, l.imported_at DESC, e.recorded_at DESC
)
-- Enum columns are cast to text: a view consumer filtering with a bound
-- parameter would otherwise need the enum type name to appear in its own
-- query, which couples every reader to the vocabulary's spelling.
SELECT c.id                         AS candidate_id,
       c.submission_id,
       s.original_filename,
       s.imported_at,
       s.import_status::text        AS import_status,
       c.target_institution_id,
       ti.match_key,
       ti.destination_code,
       ti.onboarding_status::text  AS onboarding_status,
       ln.qs_name                   AS list_name,
       cn.official_name_en          AS collector_official_name,
       coalesce(cn.is_selected, false) AS institution_is_selected,
       c.source_ref,
       c.source_type,
       c.degree_scope,
       c.official_url,
       c.normalized_url,
       c.host,
       c.is_third_party,
       c.checked_at                 AS collector_checked_at,
       c.collector_notes,
       c.verification_state::text  AS verification_state,
       c.verified_at,
       c.verified_by,
       c.verification_reason,
       c.promoted_source_mapping_id,
       -- EVIDENCE for the reviewer, never a decision. See this revision's header.
       dom.domain_host,
       dom.domain_verification_status,
       dom.domain_covers_subdomains,
       (dom.domain_verification_status = 'VERIFIED_OFFICIAL')  AS host_matches_verified_domain,
       (dom.domain_verification_status = 'AUTHORIZED_EXTERNAL') AS host_matches_authorized_domain
  FROM pilot_collected_source c
  JOIN pilot_submission s ON s.id = c.submission_id
  JOIN target_institution ti ON ti.id = c.target_institution_id
  LEFT JOIN list_name ln ON ln.target_institution_id = c.target_institution_id
  LEFT JOIN collector_name cn
         ON cn.submission_id = c.submission_id
        AND cn.target_institution_id = c.target_institution_id
  LEFT JOIN LATERAL (
        SELECT d.host                       AS domain_host,
               d.verification_status::text  AS domain_verification_status,
               d.covers_subdomains          AS domain_covers_subdomains
          FROM official_domain d
         WHERE d.target_institution_id = c.target_institution_id
           AND d.is_active
           AND (
                 d.host = c.host
                 OR (d.covers_subdomains
                     AND right(c.host, length(d.host) + 1) = '.' || d.host)
               )
         ORDER BY CASE d.verification_status
                      WHEN 'VERIFIED_OFFICIAL'   THEN 0
                      WHEN 'AUTHORIZED_EXTERNAL' THEN 1
                      WHEN 'CANDIDATE'           THEN 2
                      WHEN 'LEGACY'              THEN 3
                      ELSE 4
                  END,
                  -- The most specific matching host wins, so a subdomain rule never
                  -- hides a more precise decision about the exact host.
                  length(d.host) DESC
         LIMIT 1
  ) dom ON TRUE
"""

#: Step 5A adds the columns that make the URL/responsibility split legible: which
#: workbook column claimed this page, whether this row *is* the physical page or
#: repeats one, and the URL hash the two share.
#:
#: Rebuilt rather than `CREATE OR REPLACE`d: that form can only append columns, and
#: these belong beside the URL they describe. The view holds no data.
QUEUE_VIEW_SQL = """
CREATE VIEW pilot_source_verification_queue AS
WITH collector_name AS (
    -- The collector's own claim about the official name, from the same submission.
    -- Not `university.name_en`: that column is governed (C27) and this name has no
    -- source and no status behind it.
    SELECT su.submission_id,
           su.target_institution_id,
           su.official_name_en,
           su.is_selected
      FROM pilot_selected_university su
),
list_name AS (
    -- The name the client's list used, for recognition only. Never published.
    SELECT DISTINCT ON (e.target_institution_id)
           e.target_institution_id,
           e.qs_name
      FROM target_list_entry e
      JOIN target_list l ON l.id = e.target_list_id
     ORDER BY e.target_institution_id, l.imported_at DESC, e.recorded_at DESC
)
-- Enum columns are cast to text: a view consumer filtering with a bound
-- parameter would otherwise need the enum type name to appear in its own
-- query, which couples every reader to the vocabulary's spelling.
SELECT c.id                         AS candidate_id,
       c.submission_id,
       s.original_filename,
       s.imported_at,
       s.import_status::text        AS import_status,
       c.target_institution_id,
       ti.match_key,
       ti.destination_code,
       ti.onboarding_status::text  AS onboarding_status,
       ln.qs_name                   AS list_name,
       cn.official_name_en          AS collector_official_name,
       coalesce(cn.is_selected, false) AS institution_is_selected,
       c.source_ref,
       c.source_type,
       c.degree_scope,
       c.workbook_column,
       c.duplicate_of_source_ref,
       (c.duplicate_of_source_ref IS NULL)  AS is_physical_source,
       c.url_sha256,
       c.official_url,
       c.normalized_url,
       c.host,
       c.is_third_party,
       c.checked_at                 AS collector_checked_at,
       c.collector_notes,
       c.verification_state::text  AS verification_state,
       c.verified_at,
       c.verified_by,
       c.verification_reason,
       c.promoted_source_mapping_id,
       -- EVIDENCE for the reviewer, never a decision. See this revision's header.
       dom.domain_host,
       dom.domain_verification_status,
       dom.domain_covers_subdomains,
       (dom.domain_verification_status = 'VERIFIED_OFFICIAL')  AS host_matches_verified_domain,
       (dom.domain_verification_status = 'AUTHORIZED_EXTERNAL') AS host_matches_authorized_domain
  FROM pilot_collected_source c
  JOIN pilot_submission s ON s.id = c.submission_id
  JOIN target_institution ti ON ti.id = c.target_institution_id
  LEFT JOIN list_name ln ON ln.target_institution_id = c.target_institution_id
  LEFT JOIN collector_name cn
         ON cn.submission_id = c.submission_id
        AND cn.target_institution_id = c.target_institution_id
  LEFT JOIN LATERAL (
        SELECT d.host                       AS domain_host,
               d.verification_status::text  AS domain_verification_status,
               d.covers_subdomains          AS domain_covers_subdomains
          FROM official_domain d
         WHERE d.target_institution_id = c.target_institution_id
           AND d.is_active
           AND (
                 d.host = c.host
                 OR (d.covers_subdomains
                     AND right(c.host, length(d.host) + 1) = '.' || d.host)
               )
         ORDER BY CASE d.verification_status
                      WHEN 'VERIFIED_OFFICIAL'   THEN 0
                      WHEN 'AUTHORIZED_EXTERNAL' THEN 1
                      WHEN 'CANDIDATE'           THEN 2
                      WHEN 'LEGACY'              THEN 3
                      ELSE 4
                  END,
                  -- The most specific matching host wins, so a subdomain rule never
                  -- hides a more precise decision about the exact host.
                  length(d.host) DESC
         LIMIT 1
  ) dom ON TRUE
"""

VIEW_COMMENT = (
    "U15 source verification worklist. A matching verified host is EVIDENCE for a "
    "reviewer, never grounds to auto-verify."
)

VIEW_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        -- app_publisher stays absent, as in 0021: a view over the staging tables
        -- would hand back exactly the read that was withheld.
        EXECUTE 'GRANT SELECT ON pilot_source_verification_queue TO app_api, app_worker';
    END IF;
END
$$;
"""


def _quoted(values: Sequence[str]) -> str:
    return ", ".join("'" + value + "'" for value in values)


def upgrade() -> None:
    # --- 1. submission kind and pilot scope --------------------------------
    op.add_column(
        "pilot_submission",
        sa.Column(
            "submission_kind",
            sa.String(length=32),
            server_default="COLLECTION_WORKBOOK",
            nullable=False,
        ),
    )
    op.add_column(
        "pilot_submission",
        sa.Column("defines_pilot_scope", sa.Boolean(), server_default="false", nullable=False),
    )
    op.execute(
        "ALTER TABLE pilot_submission ADD CONSTRAINT ck_pilot_submission_submission_kind_known "
        "CHECK (submission_kind IN (" + _quoted(SUBMISSION_KINDS) + "))"
    )
    op.execute(
        "ALTER TABLE pilot_submission "
        "ADD CONSTRAINT ck_pilot_submission_only_a_source_list_defines_scope "
        "CHECK (NOT defines_pilot_scope OR submission_kind = 'OFFICIAL_SOURCE_LIST')"
    )

    # --- 2. a selected institution is reachable, not necessarily named ------
    op.execute(
        "ALTER TABLE pilot_selected_university "
        "DROP CONSTRAINT ck_pilot_selected_university_selected_names_itself"
    )
    op.execute(
        "ALTER TABLE pilot_selected_university "
        "ADD CONSTRAINT ck_pilot_selected_university_selected_is_reachable "
        "CHECK (NOT is_selected OR official_homepage IS NOT NULL)"
    )

    # --- 3. physical URL vs claimed responsibility -------------------------
    op.add_column(
        "pilot_collected_source",
        sa.Column("workbook_column", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "pilot_collected_source",
        sa.Column("duplicate_of_source_ref", sa.String(length=16), nullable=True),
    )
    # This is the constraint that made the real file unimportable.
    op.drop_constraint(
        "uq_pilot_collected_source_submission_url", "pilot_collected_source", type_="unique"
    )
    op.create_foreign_key(
        "fk_pilot_collected_source_duplicate_of",
        "pilot_collected_source",
        "pilot_collected_source",
        ["submission_id", "duplicate_of_source_ref"],
        ["submission_id", "source_ref"],
    )
    op.execute(
        "ALTER TABLE pilot_collected_source "
        "ADD CONSTRAINT ck_pilot_collected_source_a_row_is_not_its_own_duplicate "
        "CHECK (duplicate_of_source_ref IS NULL OR duplicate_of_source_ref <> source_ref)"
    )
    op.execute(
        "ALTER TABLE pilot_collected_source "
        "ADD CONSTRAINT ck_pilot_collected_source_duplicate_of_source_ref_shape "
        "CHECK (duplicate_of_source_ref IS NULL OR duplicate_of_source_ref ~ '^S[0-9]{4}$')"
    )
    op.execute(
        "ALTER TABLE pilot_collected_source "
        "ADD CONSTRAINT ck_pilot_collected_source_source_type_known "
        "CHECK (source_type IN (" + _quoted(STAGED_SOURCE_TYPES) + "))"
    )
    op.execute(
        "ALTER TABLE pilot_collected_source "
        "ADD CONSTRAINT ck_pilot_collected_source_unclassified_is_not_verifiable "
        "CHECK (verification_state <> 'VERIFIED' OR source_type <> 'UNCLASSIFIED')"
    )
    op.create_index(
        "ix_pilot_collected_source_url",
        "pilot_collected_source",
        ["submission_id", "url_sha256"],
        unique=False,
    )
    # One physical page per URL per institution. Partial, because the duplicate rows
    # are the *other* responsibilities that page carries and must be allowed to
    # accumulate -- that is the whole point of the split.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_pilot_collected_source_physical
            ON pilot_collected_source (submission_id, target_institution_id, url_sha256)
         WHERE duplicate_of_source_ref IS NULL;
        """
    )

    # --- 4. the queue shows physical identity as well as responsibility ----
    op.execute(f"DROP VIEW {QUEUE_VIEW}")
    op.execute(QUEUE_VIEW_SQL)
    op.execute(f"COMMENT ON VIEW {QUEUE_VIEW} IS '{VIEW_COMMENT}'")
    op.execute(VIEW_GRANTS)

    op.create_table_comment(
        "pilot_collected_source",
        "One claimed source responsibility. Starts PENDING; VERIFIED here means "
        "'worth registering', never 'official' (U15).",
        existing_comment=(
            "A URL a collector supplied. Starts PENDING; VERIFIED here means "
            "'worth registering', never 'official' (U15)."
        ),
        schema=None,
    )


def downgrade() -> None:
    op.create_table_comment(
        "pilot_collected_source",
        "A URL a collector supplied. Starts PENDING; VERIFIED here means "
        "'worth registering', never 'official' (U15).",
        existing_comment=(
            "One claimed source responsibility. Starts PENDING; VERIFIED here means "
            "'worth registering', never 'official' (U15)."
        ),
        schema=None,
    )
    op.execute(f"DROP VIEW {QUEUE_VIEW}")
    op.execute(PREVIOUS_QUEUE_VIEW_SQL)
    op.execute(f"COMMENT ON VIEW {QUEUE_VIEW} IS '{VIEW_COMMENT}'")
    op.execute(VIEW_GRANTS)

    op.execute("DROP INDEX IF EXISTS ix_pilot_collected_source_physical")
    op.drop_index("ix_pilot_collected_source_url", table_name="pilot_collected_source")
    for name in (
        "unclassified_is_not_verifiable",
        "source_type_known",
        "duplicate_of_source_ref_shape",
        "a_row_is_not_its_own_duplicate",
    ):
        op.execute(
            "ALTER TABLE pilot_collected_source DROP CONSTRAINT ck_pilot_collected_source_" + name
        )
    op.drop_constraint(
        "fk_pilot_collected_source_duplicate_of", "pilot_collected_source", type_="foreignkey"
    )
    op.create_unique_constraint(
        "uq_pilot_collected_source_submission_url",
        "pilot_collected_source",
        ["submission_id", "url_sha256"],
    )
    op.drop_column("pilot_collected_source", "duplicate_of_source_ref")
    op.drop_column("pilot_collected_source", "workbook_column")

    op.execute(
        "ALTER TABLE pilot_selected_university "
        "DROP CONSTRAINT ck_pilot_selected_university_selected_is_reachable"
    )
    op.execute(
        "ALTER TABLE pilot_selected_university "
        "ADD CONSTRAINT ck_pilot_selected_university_selected_names_itself "
        "CHECK (NOT is_selected OR (btrim(coalesce(official_name_en, '')) <> '' "
        "       AND official_homepage IS NOT NULL))"
    )

    for name in ("only_a_source_list_defines_scope", "submission_kind_known"):
        op.execute("ALTER TABLE pilot_submission DROP CONSTRAINT ck_pilot_submission_" + name)
    op.drop_column("pilot_submission", "defines_pilot_scope")
    op.drop_column("pilot_submission", "submission_kind")
