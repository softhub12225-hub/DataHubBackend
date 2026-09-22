"""U15: source verification queue

WHAT THIS IS FOR
================
The returned workbook will carry roughly 200 URLs -- about six official pages for
each of 36 institutions -- and not one of them is an official source yet. A URL a
collector pasted is a suggestion. Turning it into something a published fact may cite
requires a human to look at it, and this view is what they look at.

**No URL is auto-classified as OFFICIAL_VERIFIED, and none can be.** The promotion
path is unchanged and still four separate steps, each with its own decision:

    collected candidate  ->  official_domain verified  ->  source_mapping promoted
                         ->  source earns its eligibility class

C27 made the last step structural: `source.publication_eligibility` is checked by
`source_eligibility_is_earned`, which refuses `OFFICIAL_VERIFIED` unless a promoted
`source_mapping` row already points at that source. **A source cannot be born
verified** -- not by an importer, not by a bulk update, not by this queue.

THE HOSTNAME COLUMN IS EVIDENCE, NOT A DECISION
===============================================
The view surfaces `domain_verification_status` and `host_matches_verified_domain`:
whether this candidate's hostname falls under a host already verified as officially
belonging to *this* institution. That is the single most useful thing to show a
reviewer, and it is also the most tempting thing to act on automatically.

It is not acted on automatically, deliberately. A matching hostname says the page
lives on the institution's domain; it does not say the page is what the collector
claims it is. `ox.ac.uk/news/2019/some-old-post` matches every check a hostname rule
can make and is not a tuition page. Worse, universities host student societies,
personal staff pages and departmental wikis on their own domains, and a rule that
verifies on hostname alone would promote all of them.

So the column is named for what it is -- evidence -- and the decision functions
require an actor and a reason regardless of what it says.

NO INSTITUTIONAL IDENTITY IS INFERRED
=====================================
The view joins `official_domain` only on `target_institution_id`: the same
institution the candidate was collected for. It never searches for a host that
resembles the institution's name, and never matches a domain belonging to another
institution because the names look similar. That is the same refusal
`pilot_matching.py` makes for institution names, one level down, and for the same
reason: a similarity threshold loose enough to match `Essex, University of` to
`University of Essex` also matches `University of Canterbury` to `Canterbury Christ
Church University`.

NO FRONTEND
===========
A view and a CLI report. Wiring a review console to an unreviewed API surface is a
later step, and the queue is useful now precisely because it does not need one.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# No import from app.* (C21/C22): frozen literals only.

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


QUEUE_VIEW = "pilot_source_verification_queue"

# `right(host, length(d.host) + 1) = '.' || d.host` rather than a LIKE pattern: a
# hostname may legitimately contain characters LIKE treats as wildcards, and
# `_` in particular would make `a_ac.uk` match `ox.ac.uk`. String equality cannot.
QUEUE_VIEW_SQL = f"""
CREATE VIEW {QUEUE_VIEW} AS
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

VIEW_COMMENT = (
    "U15 source verification worklist. A matching verified host is EVIDENCE for a "
    "reviewer, never grounds to auto-verify."
)

GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
        -- app_publisher is deliberately absent. It holds no privilege on the
        -- staging tables (revision 0020), and a view over them would hand back
        -- exactly the read that was withheld -- which is how `target_source_coverage`
        -- would have leaked `qs_name` past C27's REVOKE if it had not been named
        -- explicitly there.
        EXECUTE 'GRANT SELECT ON {view} TO app_api, app_worker';
    END IF;
END
$$;
""".replace("{view}", QUEUE_VIEW)


def upgrade() -> None:
    op.execute(QUEUE_VIEW_SQL)
    op.execute(f"COMMENT ON VIEW {QUEUE_VIEW} IS '{VIEW_COMMENT}'")
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {QUEUE_VIEW}")
