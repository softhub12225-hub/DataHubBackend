"""Run field-claim extraction over stored documents, and audit what it produced.

Usage::

    uv run python apps/api/scripts/extract_claims.py run
    uv run python apps/api/scripts/extract_claims.py coverage
    uv run python apps/api/scripts/extract_claims.py sanity
    uv run python apps/api/scripts/extract_claims.py audit --kind TUITION --limit 20
    uv run python apps/api/scripts/extract_claims.py lineage
    uv run python apps/api/scripts/extract_claims.py untouched

**Offline, and nothing is published.** This reads normalised documents already in the
artifact store. No network request, no browser, no LLM. It writes only
`field_claim_candidate` rows -- never `field_claim`, `field_provenance`,
`change_proposal`, or any canonical university/program/tuition/deadline row. `untouched`
proves that rather than asserting it.

A manual command rather than a CI job, for the same reason extraction is: the automated
suite is fixture-based, and turning 174 universities' markup into a test dependency
would make it fail when they redesign their sites.
"""

# ruff: noqa: S608 -- the only thing interpolated into any query here is a table
# alias, written as a literal at every call site. Every value travels as a bound
# parameter, including the current-version list. Nothing reaching these queries
# comes from a fetched page, a workbook cell or an argument.

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import EvidenceStore, build_evidence_store
from app.domains.claims.language import KNOWN_TESTS
from app.domains.claims.locator import resolve
from app.domains.claims.model import ROUTING
from app.domains.claims.review import CURRENT_PARAMS, CURRENT_RULES, current_only
from app.domains.claims.runner import load_document, run_claims
from app.domains.extraction.runner import DERIVED_PREFIX

#: (extractor, version) pairs the current rules produce, and the predicate that selects
#: them, both **imported** rather than restated. This file used to carry its own copy of
#: both. They agreed by coincidence; when "current" grew a second axis in Step 5C.4 the
#: copy would have gone on counting superseded rows as live while the review queue did
#: not, and nothing would have said so.
CURRENT_VERSIONS: tuple[tuple[str, str], ...] = CURRENT_RULES

#: Every value the predicate binds, as one mapping to spread into a query's parameters.
#: Naming them individually is how a call site ends up binding half of them.
CURRENT_VERSION_KEYS = CURRENT_PARAMS


#: The field kinds a precision audit walks, in the order a reviewer would want them.
AUDIT_KINDS = (
    "TUITION",
    "LANGUAGE_OVERALL_SCORE",
    "LANGUAGE_COMPONENT_SCORE",
    "LANGUAGE_TEST",
    "APPLICATION_DEADLINE",
    "ACADEMIC_CALENDAR_EVENT",
    "ADMISSION_REQUIREMENT",
    "PROGRAM_NAME",
    "DEGREE_LEVEL",
    "DISCIPLINE_HINT",
    "DURATION",
    "STUDY_MODE",
)


def _engine(role: DatabaseRole) -> Any:
    settings = get_settings()
    return create_engine(settings.database.sync_dsn(role), future=True)


def _artifacts(root: Path) -> EvidenceStore:
    return build_evidence_store(get_settings(), prefix=DERIVED_PREFIX, local_root=root)


# ===========================================================================
# run (section 30)
# ===========================================================================


def command_run(args: argparse.Namespace) -> int:
    engine = _engine(DatabaseRole.WORKER)
    print(f"artifacts  {args.artifact_root} (prefix {DERIVED_PREFIX}/)")
    print("offline: stored artifacts only, no network request, no LLM")
    print("writes field_claim_candidate only -- nothing publishable\n")

    report = run_claims(engine, artifacts=_artifacts(args.artifact_root), limit=args.limit)
    print(report.summary())
    for title, counts in (
        ("by field kind", report.by_field_kind),
        ("by confidence band", report.by_confidence),
        ("by source responsibility", report.by_responsibility),
    ):
        if counts:
            print(f"\n  {title}:")
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
                print(f"    {count:>5}  {key}")
    if report.failures:
        print(f"\n  failures ({len(report.failures)}):")
        for line in report.failures[:20]:
            print(f"    {line}")
    engine.dispose()
    return 0


# ===========================================================================
# coverage (section 31)
# ===========================================================================


def command_coverage(args: argparse.Namespace) -> int:
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        _coverage_by_responsibility(connection)
        _coverage_by_institution(connection)
        _coverage_gaps(connection)
    engine.dispose()
    return 0


def _coverage_by_responsibility(connection: Connection) -> None:
    print("=" * 78)
    print("COVERAGE BY SOURCE RESPONSIBILITY (section 31)")
    print("=" * 78)
    print(
        "\n  A page is counted against the responsibility the workbook claimed it for.\n"
        "  'authorised' means routing let at least one business extractor read it;\n"
        "  a page authorised for nothing is a routing decision, not a failure.\n"
    )
    rows = connection.execute(
        text(
            """
            WITH page AS (
                SELECT pcs.source_type AS responsibility,
                       pcs.acquisition_source_id AS source_id,
                       e.id AS extraction_id,
                       (e.output->'statistics'->>'text_characters')::int AS characters
                  FROM pilot_collected_source pcs
                  LEFT JOIN snapshot sn ON sn.source_id = pcs.acquisition_source_id
                  LEFT JOIN extraction e
                         ON e.snapshot_id = sn.id AND e.status <> 'FAILED'
                 WHERE pcs.acquisition_source_id IS NOT NULL
                   AND pcs.duplicate_of_source_ref IS NULL
            )
            SELECT p.responsibility,
                   count(DISTINCT p.source_id) AS pages,
                   count(DISTINCT p.extraction_id) AS extracted,
                   count(DISTINCT p.source_id) FILTER (WHERE p.characters >= 400)
                       AS with_usable_text,
                   count(DISTINCT c.pilot_collected_source_id) AS claimed_pages,
                   count(c.id) AS claims
              FROM page p
              LEFT JOIN field_claim_candidate c
                     ON c.extraction_id = p.extraction_id AND {current}
             GROUP BY 1 ORDER BY 1
            """.format(current=current_only("c"))
        ),
        CURRENT_VERSION_KEYS,
    ).all()
    header = (
        f"  {'responsibility':<28}{'pages':>7}{'extr':>7}{'text':>7}{'w/claims':>10}{'claims':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        authorised = "" if ROUTING.get(row.responsibility) else "  (authorises nothing)"
        print(
            f"  {row.responsibility[:28]:<28}{row.pages:>7}{row.extracted:>7}"
            f"{row.with_usable_text:>7}{row.claimed_pages:>10}{row.claims:>8}{authorised}"
        )
    totals = connection.execute(
        text(
            "SELECT count(*) AS claims, count(DISTINCT extraction_id) AS documents, "
            "       count(DISTINCT pilot_collected_source_id) AS workbook_claims "
            f"  FROM field_claim_candidate c WHERE {current_only('c')}"
        ),
        CURRENT_VERSION_KEYS,
    ).one()
    print(
        f"\n  {totals.claims} candidate claim(s) over {totals.documents} document(s), "
        f"attributed to {totals.workbook_claims} workbook claim(s)"
    )


def _coverage_by_institution(connection: Connection) -> None:
    print("\n" + "=" * 78)
    print("COVERAGE BY INSTITUTION")
    print("=" * 78)
    row = connection.execute(
        text(
            """
            WITH per_institution AS (
                SELECT ti.match_key, count(c.id) AS claims
                  FROM pilot_collected_source pcs
                  JOIN target_institution ti ON ti.id = pcs.target_institution_id
                  LEFT JOIN snapshot sn ON sn.source_id = pcs.acquisition_source_id
                  LEFT JOIN extraction e
                         ON e.snapshot_id = sn.id AND e.status <> 'FAILED'
                  LEFT JOIN field_claim_candidate c
                         ON c.extraction_id = e.id AND {current}
                 WHERE pcs.acquisition_source_id IS NOT NULL
                 GROUP BY 1
            )
            SELECT count(*) AS institutions,
                   count(*) FILTER (WHERE claims > 0) AS with_claims,
                   count(*) FILTER (WHERE claims = 0) AS without_claims,
                   coalesce(percentile_disc(0.5) WITHIN GROUP (ORDER BY claims), 0) AS median,
                   coalesce(max(claims), 0) AS most
              FROM per_institution
            """.format(current=current_only("c"))
        ),
        CURRENT_VERSION_KEYS,
    ).one()
    print(f"\n  institutions in the pilot            {row.institutions}")
    print(f"  with at least one candidate claim    {row.with_claims}")
    print(f"  with none                            {row.without_claims}")
    print(f"  median claims per institution        {row.median}")
    print(f"  most claims for one institution      {row.most}")

    print("\n  institutions with no candidate claim at all:")
    empty = connection.execute(
        text(
            """
            SELECT ti.match_key, count(DISTINCT pcs.acquisition_source_id) AS pages
              FROM pilot_collected_source pcs
              JOIN target_institution ti ON ti.id = pcs.target_institution_id
              LEFT JOIN snapshot sn ON sn.source_id = pcs.acquisition_source_id
              LEFT JOIN extraction e ON e.snapshot_id = sn.id AND e.status <> 'FAILED'
              LEFT JOIN field_claim_candidate c
                     ON c.extraction_id = e.id AND {current}
             WHERE pcs.acquisition_source_id IS NOT NULL
             GROUP BY 1 HAVING count(c.id) = 0 ORDER BY 1
            """.format(current=current_only("c"))
        ),
        CURRENT_VERSION_KEYS,
    ).all()
    for entry in empty:
        print(f"    {entry.match_key[:56]:<56} {entry.pages} page(s)")
    if not empty:
        print("    (none)")


def _coverage_gaps(connection: Connection) -> None:
    print("\n" + "=" * 78)
    print("WHERE THE GAPS ARE")
    print("=" * 78)
    print(
        "\n  A page with no claim is one of four different things, and conflating them\n"
        "  is how a coverage number stops meaning anything.\n"
    )
    rows = connection.execute(
        text(
            """
            WITH page AS (
                SELECT DISTINCT pcs.acquisition_source_id AS source_id,
                       e.id AS extraction_id,
                       e.status::text AS status,
                       (e.output->'statistics'->>'text_characters')::int AS characters
                  FROM pilot_collected_source pcs
                  LEFT JOIN snapshot sn ON sn.source_id = pcs.acquisition_source_id
                  LEFT JOIN extraction e ON e.snapshot_id = sn.id
                 WHERE pcs.acquisition_source_id IS NOT NULL
            ),
            authorising AS (
                SELECT DISTINCT acquisition_source_id AS source_id
                  FROM pilot_collected_source
                 WHERE source_type = ANY(:routed)
            )
            SELECT
              count(*) FILTER (WHERE p.extraction_id IS NULL) AS never_extracted,
              count(*) FILTER (WHERE p.status = 'FAILED') AS extraction_failed,
              count(*) FILTER (WHERE p.status <> 'FAILED' AND a.source_id IS NULL)
                  AS not_authorised,
              count(*) FILTER (WHERE p.status <> 'FAILED' AND a.source_id IS NOT NULL
                               AND p.characters < 400) AS insufficient_text,
              count(*) FILTER (WHERE p.status <> 'FAILED' AND a.source_id IS NOT NULL
                               AND p.characters >= 400
                               AND NOT EXISTS (SELECT 1 FROM field_claim_candidate c
                                                WHERE c.extraction_id = p.extraction_id
                                                  AND {current}))
                  AS rules_found_nothing,
              count(*) FILTER (WHERE EXISTS (SELECT 1 FROM field_claim_candidate c
                                              WHERE c.extraction_id = p.extraction_id
                                                AND {current}))
                  AS produced_claims
              FROM page p LEFT JOIN authorising a ON a.source_id = p.source_id
            """.format(current=current_only("c"))
        ),
        {
            "routed": [name for name, allowed in ROUTING.items() if allowed],
            **CURRENT_VERSION_KEYS,
        },
    ).one()
    for label, column in (
        ("never extracted (no stored body)", "never_extracted"),
        ("extraction failed", "extraction_failed"),
        ("authorised for no business extractor", "not_authorised"),
        ("insufficient static text (<400 chars)", "insufficient_text"),
        ("authorised, readable, rules found nothing", "rules_found_nothing"),
        ("produced at least one candidate claim", "produced_claims"),
    ):
        print(f"    {label:<48} {int(getattr(rows, column) or 0)}")


# ===========================================================================
# sanity (section 33)
# ===========================================================================

#: Each check is a name, a reason it would matter, and SQL returning the offenders.
#: Written as queries rather than as Python over a fetched set so the count and the
#: examples cannot disagree.
SANITY_CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "tuition amount is zero or negative",
        "a fee of 0 is either free tuition the page did not claim, or a parse error",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.field_kind = 'TUITION'
           AND (coalesce((c.value_normalized->>'amount_min')::numeric, 1) <= 0
             OR coalesce((c.value_normalized->>'amount_max')::numeric, 1) <= 0)
        """,
    ),
    (
        "tuition RANGE with min above max",
        "the endpoints were read in the wrong order, so both are suspect",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.field_kind = 'TUITION'
           AND c.value_normalized->>'amount_kind' = 'RANGE'
           AND (c.value_normalized->>'amount_min')::numeric
             > (c.value_normalized->>'amount_max')::numeric
        """,
    ),
    (
        "implausible language score for a known test",
        "an IELTS of 100 is a page we misread, not a requirement",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c, jsonb_each(cast(:bounds AS jsonb)) AS b(test, span)
         WHERE {current}
           AND c.field_kind IN ('LANGUAGE_OVERALL_SCORE', 'LANGUAGE_COMPONENT_SCORE')
           AND c.value_normalized->>'test' = b.test
           AND ((c.value_normalized->>'score')::numeric < (b.span->>0)::numeric
             OR (c.value_normalized->>'score')::numeric > (b.span->>1)::numeric)
        """,
    ),
    (
        "component score above the overall score for the same test on the same page",
        "a component floor cannot exceed the overall requirement it sits under",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
          JOIN field_claim_candidate overall
            ON overall.extraction_id = c.extraction_id
           AND overall.field_kind = 'LANGUAGE_OVERALL_SCORE'
           AND overall.value_normalized->>'test' = c.value_normalized->>'test'
         WHERE {current} AND c.field_kind = 'LANGUAGE_COMPONENT_SCORE'
           AND (c.value_normalized->>'score')::numeric
             > (overall.value_normalized->>'score')::numeric
        """,
    ),
    (
        "impossible calendar date",
        "31 February is a misparse, and a deadline is where that matters most",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current}
           AND c.field_kind IN ('APPLICATION_DEADLINE', 'ACADEMIC_CALENDAR_EVENT')
           AND c.value_normalized->>'day' IS NOT NULL
           AND c.value_normalized->>'month' IS NOT NULL
           AND (c.value_normalized->>'day')::int > (
                 CASE (c.value_normalized->>'month')::int
                   WHEN 2 THEN 29
                   WHEN 4 THEN 30 WHEN 6 THEN 30 WHEN 9 THEN 30 WHEN 11 THEN 30
                   ELSE 31 END)
        """,
    ),
    (
        "a deadline with no stated time that acquired one anyway",
        "C9/C13: a date-only fact must not become midnight in any timezone",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.field_kind = 'APPLICATION_DEADLINE'
           AND c.value_normalized->>'hour' IS NOT NULL
           AND c.unresolved_reason LIKE '%TIME_NOT_STATED%'
        """,
    ),
    (
        "a timezone with no time",
        "a zone without an hour is not a more precise instant, it is a contradiction",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.field_kind = 'APPLICATION_DEADLINE'
           AND c.value_normalized->>'timezone' IS NOT NULL
           AND c.value_normalized->>'hour' IS NULL
        """,
    ),
    (
        "an admission requirement asserting a universal applicant scope",
        "section 10: scope is never defaulted, so UNIVERSAL must never appear here",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.field_kind = 'ADMISSION_REQUIREMENT'
           AND c.value_normalized::text ILIKE '%UNIVERSAL%'
        """,
    ),
    (
        "empty evidence text",
        "a claim nobody can review is not a claim; the schema also refuses this",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND btrim(c.evidence_text) = ''
        """,
    ),
    (
        "missing or empty locator",
        "extraction_id alone is not provenance (section 3)",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND (c.locator IS NULL OR c.locator = '{{}}'::jsonb)
        """,
    ),
    (
        "a value with neither a normalised form nor a reason",
        "a null value with no reason is a claim that says nothing",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current}
           AND c.value_normalized IS NULL AND c.unresolved_reason IS NULL
        """,
    ),
    (
        "a confidence band with no stated reason",
        "section 25: a band is only meaningful with the reason beside it",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND btrim(c.confidence_reason) = ''
        """,
    ),
    (
        "a fake decimal confidence smuggled into the band",
        "section 25: 0.873421 implies a calibration that does not exist",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current} AND c.confidence_band NOT IN ('HIGH', 'MEDIUM', 'LOW')
        """,
    ),
    (
        "two current claims sharing one fingerprint",
        "a duplicate is dropped by ON CONFLICT and the pass still reports success, so "
        "a collision is invisible unless something looks for it",
        """
        SELECT c.id, c.field_kind, c.value_normalized, c.value_raw_text, c.evidence_text
          FROM field_claim_candidate c
         WHERE {current}
           AND EXISTS (SELECT 1 FROM field_claim_candidate other
                        WHERE other.claim_fingerprint = c.claim_fingerprint
                          AND other.id <> c.id)
        """,
    ),
)


def command_sanity(args: argparse.Namespace) -> int:
    engine = _engine(DatabaseRole.API)
    bounds = json.dumps({test: list(span) for test, span in KNOWN_TESTS.items()})
    failures = 0
    with engine.connect() as connection:
        print("=" * 78)
        print("AUTOMATED SANITY CHECKS (section 33)")
        print("=" * 78)
        print(
            "\n  These do not decide whether a claim is true. They find claims that\n"
            "  cannot be true, which is a different and much cheaper question.\n"
        )
        for name, why, sql in SANITY_CHECKS:
            rows = connection.execute(
                text(sql.format(current=current_only("c"))),
                {"bounds": bounds, **CURRENT_VERSION_KEYS},
            ).all()
            marker = "FAIL" if rows else "ok  "
            print(f"  [{marker}] {name}  ({len(rows)})")
            if rows:
                failures += 1
                print(f"         why it matters: {why}")
                for row in rows[:5]:
                    value = json.dumps(row.value_normalized, ensure_ascii=False)[:100]
                    print(f"         {row.field_kind}  {value}")
                    print(f'           raw: "{row.value_raw_text[:80]}"')
        _routing_violations(connection)
        _unresolved_summary(connection)
        _locator_resolution(connection, args.artifact_root, limit=args.resolve_limit)
    engine.dispose()
    print(f"\n  {failures} check(s) found something." if failures else "\n  All checks clean.")
    return 0


def _routing_violations(connection: Connection) -> None:
    """A claim whose field kind its responsibility does not authorise.

    The runner enforces this, so a row here means the routing table and the stored
    attribution disagree -- which would make the `source_responsibility` column a
    decoration rather than an audit trail.
    """
    print("\n  [routing] claims whose responsibility does not authorise their extractor:")
    allowed: list[dict[str, str]] = []
    for responsibility, extractors in ROUTING.items():
        for extractor in extractors:
            allowed.append({"responsibility": responsibility, "extractor": extractor})
    rows = connection.execute(
        text(
            """
            SELECT c.source_responsibility, c.extractor_name, count(*) AS n
              FROM field_claim_candidate c
             WHERE {current}
             GROUP BY 1, 2 ORDER BY 1, 2
            """.format(current=current_only("c"))
        ),
        CURRENT_VERSION_KEYS,
    ).all()
    keys = {(entry["responsibility"], entry["extractor"]) for entry in allowed}
    prefixes = {
        "tuition-rule-extractor": "tuition",
        "language-rule-extractor": "language",
        "deadline-rule-extractor": "deadline",
        "admission-rule-extractor": "admission",
        "program-rule-extractor": "program",
        "calendar-rule-extractor": "calendar",
    }
    bad = 0
    for row in rows:
        key = (row.source_responsibility, prefixes.get(row.extractor_name, "?"))
        if key not in keys:
            bad += 1
            print(f"    VIOLATION  {row.source_responsibility} -> {row.extractor_name} ({row.n})")
    print("    (none)" if not bad else f"    {bad} violating pair(s)")


def _unresolved_summary(connection: Connection) -> None:
    print("\n  what stayed unresolved, and why (sections 10, 13, 16):")
    rows = connection.execute(
        text(
            "SELECT c.unresolved_reason, count(*) AS n FROM field_claim_candidate c "
            f" WHERE {current_only('c')} AND c.unresolved_reason IS NOT NULL "
            " GROUP BY 1 ORDER BY 2 DESC"
        ),
        CURRENT_VERSION_KEYS,
    ).all()
    for row in rows:
        print(f"    {row.n:>5}  {row.unresolved_reason[:66]}")
    if not rows:
        print("    (nothing unresolved, which for this fleet would be suspicious)")


def _locator_resolution(connection: Connection, artifact_root: Path, *, limit: int) -> None:
    """Does every locator actually land on the wording its claim quotes?

    A locator nobody can follow is decoration (section 34). This samples rather than
    walking all of them because each check loads a document from the artifact store.
    """
    print(f"\n  [locator] resolving a sample of {limit} locators against their documents:")
    artifacts = _artifacts(artifact_root)
    rows = connection.execute(
        text(
            """
            SELECT c.id, c.field_kind, c.locator, c.value_raw_text, c.evidence_text,
                   e.document_hash
              FROM field_claim_candidate c
              JOIN extraction e ON e.id = c.extraction_id
             WHERE {current}
             ORDER BY md5(c.id::text) LIMIT :limit
            """.format(current=current_only("c"))
        ),
        {"limit": limit, **CURRENT_VERSION_KEYS},
    ).all()
    cache: dict[str, Any] = {}
    unresolved = 0
    mismatched = 0
    for row in rows:
        if row.document_hash not in cache:
            try:
                cache[row.document_hash] = load_document(artifacts, row.document_hash)
            except (KeyError, OSError, ValueError) as exc:
                print(f"    UNREADABLE  {row.field_kind}: {exc}")
                unresolved += 1
                continue
        resolved = resolve(cache[row.document_hash], row.locator)
        if resolved is None:
            unresolved += 1
            print(f"    UNRESOLVED  {row.field_kind}  {json.dumps(row.locator)}")
        elif row.value_raw_text not in resolved and resolved.strip() != row.evidence_text.strip():
            mismatched += 1
            print(f"    MISMATCH    {row.field_kind}")
            print(f'      locator points at: "{resolved[:90]}"')
            print(f'      claim quotes:      "{row.value_raw_text[:90]}"')
    print(f"    {len(rows) - unresolved - mismatched}/{len(rows)} resolved to the quoted wording")


# ===========================================================================
# audit (section 32)
# ===========================================================================


def command_audit(args: argparse.Namespace) -> int:
    """Samples for manual precision review, with everything needed to judge them."""
    engine = _engine(DatabaseRole.API)
    kinds = [args.kind] if args.kind else list(AUDIT_KINDS)
    with engine.connect() as connection:
        print("=" * 78)
        print("PRECISION AUDIT SAMPLES (section 32)")
        print("=" * 78)
        print(
            "\n  Each sample carries the institution, the URL, the responsibility that\n"
            "  authorised the extractor, the value, the evidence wording, the locator\n"
            "  and the rule. Judging one should not need a second query or the page.\n"
            "  Sampling is deterministic (ordered by md5 of the id), so a second run\n"
            "  audits the same rows and two people can compare verdicts.\n"
        )
        for kind in kinds:
            _audit_kind(connection, kind, args.limit)
    engine.dispose()
    return 0


def _audit_kind(connection: Connection, kind: str, limit: int) -> None:
    total = connection.execute(
        text(
            "SELECT count(*) FROM field_claim_candidate c "
            f" WHERE {current_only('c')} AND c.field_kind = :k"
        ),
        {"k": kind, **CURRENT_VERSION_KEYS},
    ).scalar_one()
    print("\n" + "-" * 78)
    print(f"{kind}  ({total} claim(s) in total, sampling {min(limit, total)})")
    print("-" * 78)
    if not total:
        print("  (none)")
        return
    rows = connection.execute(
        text(
            """
            SELECT c.id, c.value_normalized, c.unresolved_reason, c.value_raw_text,
                   c.evidence_text, c.locator, c.extractor_name, c.extractor_version,
                   c.confidence_band, c.confidence_reason, c.source_responsibility,
                   s.url, coalesce(ti.match_key, '?') AS institution,
                   pcs.source_ref
              FROM field_claim_candidate c
              JOIN extraction e ON e.id = c.extraction_id
              JOIN snapshot sn ON sn.id = e.snapshot_id
              JOIN source s ON s.id = sn.source_id
              JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
              LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
             WHERE {current} AND c.field_kind = :k
             ORDER BY md5(c.id::text) LIMIT :limit
            """.format(current=current_only("c"))
        ),
        {"k": kind, "limit": limit, **CURRENT_VERSION_KEYS},
    ).all()
    for index, row in enumerate(rows, start=1):
        value = (
            json.dumps(row.value_normalized, ensure_ascii=False, sort_keys=True)
            if row.value_normalized is not None
            else f"NULL ({row.unresolved_reason})"
        )
        print(f"\n  {index:>2}. {row.institution[:52]}   [{row.source_ref}]")
        print(f"      url             {row.url[:92]}")
        print(f"      authorised by   {row.source_responsibility}")
        print(f"      value           {value[:220]}")
        if row.unresolved_reason and row.value_normalized is not None:
            print(f"      unresolved      {row.unresolved_reason}")
        print(f'      matched text    "{row.value_raw_text[:110]}"')
        print(f'      evidence        "{row.evidence_text[:200]}"')
        print(f"      locator         {json.dumps(row.locator, ensure_ascii=False)[:150]}")
        print(f"      rule            {row.extractor_name} v{row.extractor_version}")
        print(f"      confidence      {row.confidence_band} -- {row.confidence_reason[:130]}")


# ===========================================================================
# lineage (section 34)
# ===========================================================================


def command_lineage(args: argparse.Namespace) -> int:
    """One claim, walked all the way back, including the locator resolving."""
    engine = _engine(DatabaseRole.API)
    artifacts = _artifacts(args.artifact_root)
    with engine.connect() as connection:
        print("=" * 78)
        print("CLAIM LINEAGE PROOF (section 34)")
        print("=" * 78)
        row = connection.execute(
            text(
                """
                SELECT c.id AS claim_id, c.field_kind, c.value_normalized,
                       c.unresolved_reason, c.value_raw_text, c.evidence_text, c.locator,
                       c.extractor_name, c.extractor_version, c.confidence_band,
                       c.confidence_reason, c.claim_fingerprint, c.recorded_at,
                       c.source_responsibility,
                       e.id AS extraction_id, e.document_hash, e.document_storage_key,
                       e.extractor_name AS doc_extractor, e.extractor_version AS doc_version,
                       e.status::text AS extraction_status, e.input_content_hash,
                       sn.id AS snapshot_id, sn.content_hash, sn.observed_at,
                       sn.requested_url, sn.effective_url,
                       cb.storage_key AS evidence_key, cb.byte_size,
                       r.id AS fetch_run_id, r.status::text AS run_status, r.finished_at,
                       a.id AS attempt_id, a.cycle_key, a.attempt_no,
                       s.id AS source_id, s.url, s.publication_eligibility::text AS eligibility,
                       pcs.source_ref, pcs.source_type, pcs.workbook_column,
                       ti.match_key, ti.destination_code
                  FROM field_claim_candidate c
                  JOIN extraction e ON e.id = c.extraction_id
                  JOIN snapshot sn ON sn.id = e.snapshot_id
                  JOIN content_blob cb ON cb.content_hash = sn.content_hash
                  JOIN fetch_run r ON r.id = sn.fetch_run_id
                  JOIN fetch_attempt a ON a.id = r.attempt_id
                  JOIN source s ON s.id = sn.source_id
                  JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
                  LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
                 WHERE {current}
                   AND (cast(:claim_id AS uuid) IS NULL
                        OR c.id = cast(:claim_id AS uuid))
                 ORDER BY c.recorded_at LIMIT 1
                """.format(current=current_only("c"))
            ),
            {"claim_id": args.claim_id, **CURRENT_VERSION_KEYS},
        ).first()
        if row is None:
            print("\n  No candidate claim exists yet. Run `extract_claims.py run` first.")
            return 1

        steps = (
            ("institution", f"{row.match_key}  ({row.destination_code})"),
            ("workbook claim", f"{row.source_ref}  {row.source_type}  [{row.workbook_column}]"),
            ("source", f"{row.source_id}  {row.url}"),
            ("publication eligibility", f"{row.eligibility}  <- still not publishable"),
            ("fetch attempt", f"{row.attempt_id}  cycle {row.cycle_key}  attempt {row.attempt_no}"),
            ("fetch run", f"{row.fetch_run_id}  {row.run_status}  {row.finished_at}"),
            ("snapshot", f"{row.snapshot_id}  observed {row.observed_at}"),
            ("requested url", row.requested_url),
            ("effective url", row.effective_url or "(no redirect)"),
            ("raw evidence", f"{row.content_hash}  {row.byte_size:,} bytes  {row.evidence_key}"),
            (
                "extraction",
                f"{row.extraction_id}  {row.doc_extractor} v{row.doc_version}  "
                f"{row.extraction_status}",
            ),
            ("input content hash", f"{row.input_content_hash}  <- matches the snapshot"),
            ("normalised document", f"{row.document_hash}  {row.document_storage_key}"),
            ("claim", f"{row.claim_id}  {row.field_kind}"),
            ("authorised by", row.source_responsibility),
            ("rule", f"{row.extractor_name} v{row.extractor_version}"),
            ("confidence", f"{row.confidence_band} -- {row.confidence_reason}"),
            ("fingerprint", row.claim_fingerprint),
            ("recorded at", str(row.recorded_at)),
        )
        print()
        for label, value in steps:
            print(f"  {label:<24} {value}")

        print("\n  value:")
        print(
            "    "
            + (
                json.dumps(
                    row.value_normalized, ensure_ascii=False, indent=2, sort_keys=True
                ).replace("\n", "\n    ")
                if row.value_normalized is not None
                else f"NULL -- {row.unresolved_reason}"
            )
        )
        print(f'\n  matched wording:  "{row.value_raw_text}"')
        print(f'  evidence:         "{row.evidence_text[:300]}"')
        print(f"  locator:          {json.dumps(row.locator, ensure_ascii=False)}")

        print("\n  RESOLVING THE LOCATOR AGAINST THE STORED DOCUMENT")
        print("  " + "-" * 74)
        assert (
            row.input_content_hash == row.content_hash
        ), "the extraction's input hash does not match its snapshot: lineage is broken"
        try:
            document = load_document(artifacts, row.document_hash)
        except (KeyError, OSError, ValueError) as exc:
            print(f"  artifact unreadable: {exc}")
            return 1
        resolved = resolve(document, row.locator)
        print(f'  the locator points at: "{resolved}"')
        if resolved is None:
            print("  FAIL: the locator does not resolve")
            return 1
        if row.value_raw_text in resolved or resolved.strip() == row.evidence_text.strip():
            print("  PASS: the pointer lands on the wording the claim quotes")
        else:
            print("  FAIL: the pointer and the quoted wording disagree")
            return 1
    engine.dispose()
    return 0


# ===========================================================================
# untouched (section 35)
# ===========================================================================

#: Tables a published fact passes through, and canonical rows a claim must not create.
DOWNSTREAM_TABLES = (
    "field_claim",
    "field_provenance",
    "change_proposal",
    "change_proposal_item",
)
CANONICAL_TABLES = (
    "university",
    "campus",
    "faculty",
    "program",
    "program_offering",
    "intake",
    "application_round",
    "tuition",
    "admission_requirement",
    "language_requirement",
    "application_deadline",
)


def command_untouched(args: argparse.Namespace) -> int:
    """Prove the publication and canonical planes did not move."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        print("=" * 78)
        print("NOTHING WAS PUBLISHED (section 35)")
        print("=" * 78)
        print("\n  publication plane:")
        clean = True
        for table in DOWNSTREAM_TABLES:
            exists = connection.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}
            ).scalar_one()
            if not exists:
                print(f"    {table:<28} (table does not exist)")
                continue
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            print(f"    {table:<28} {count} row(s)")
            clean = clean and count == 0

        print("\n  canonical plane:")
        for table in CANONICAL_TABLES:
            exists = connection.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}
            ).scalar_one()
            if not exists:
                print(f"    {table:<28} (table does not exist)")
                continue
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            print(f"    {table:<28} {count} row(s)")
            clean = clean and count == 0

        print("\n  publication eligibility of every pilot source:")
        rows = connection.execute(
            text(
                """
                SELECT s.publication_eligibility::text AS eligibility, count(*) AS n
                  FROM source s
                 WHERE EXISTS (SELECT 1 FROM pilot_collected_source pcs
                                WHERE pcs.acquisition_source_id = s.id)
                 GROUP BY 1 ORDER BY 2 DESC
                """
            )
        ).all()
        for row in rows:
            print(f"    {row.eligibility:<28} {row.n}")
        eligible = sum(row.n for row in rows if row.eligibility != "NOT_ELIGIBLE")

        print("\n  official domain verification:")
        domains = connection.execute(
            text(
                "SELECT verification_status::text AS status, count(*) AS n "
                "  FROM official_domain GROUP BY 1 ORDER BY 2 DESC"
            )
        ).all()
        for row in domains:
            print(f"    {row.status:<28} {row.n}")
        if not domains:
            print("    (no official domain has been verified)")

        print("\n  candidate claims (this step's only output):")
        candidates = connection.execute(
            text(
                "SELECT count(*) AS claims, count(DISTINCT extraction_id) AS documents, "
                "       count(*) FILTER (WHERE NOT " + current_only("c") + ") AS superseded "
                "  FROM field_claim_candidate c"
            ),
            CURRENT_VERSION_KEYS,
        ).one()
        print(f"    field_claim_candidate        {candidates.claims} row(s)")
        print(f"    over                         {candidates.documents} document(s)")
        print(f"    of which superseded          {candidates.superseded} (earlier rule version)")

        verdict = "PASS" if clean and eligible == 0 else "FAIL"
        print(f"\n  {verdict}: nothing publishable was created and no eligibility changed.")
        if verdict == "FAIL":
            return 1
    engine.dispose()
    return 0


# ===========================================================================
# low (section 22)
# ===========================================================================

#: The shapes a LOW admission candidate turns out to have, in the order a reviewer
#: would triage them. First match wins, so the order is the classification.
LOW_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "NO_VERB",
        "a fragment with no finite verb -- a heading, a label or a list caption that "
        "the requirements heading above it swept in",
    ),
    (
        "CROSS_REFERENCE",
        "points at the requirement instead of stating one: 'see the entry "
        "requirements for your course'",
    ),
    (
        "PROCESS_NOT_REQUIREMENT",
        "describes the application process -- fees, deadlines, how to submit -- which "
        "is a real fact of a different kind",
    ),
    (
        "QUALIFICATION_LIST_ITEM",
        "one entry from a list of accepted qualifications. Short and title-case, and "
        "genuinely part of an entry requirement",
    ),
    (
        "PROSE_WITHOUT_REQUIREMENT_WORDING",
        "ordinary page prose that only became a candidate because a requirements "
        "heading sits above it",
    ),
)

_FINITE_VERB = re.compile(
    r"\b(is|are|was|were|has|have|had|must|should|may|can|will|would|need|needs|"
    r"require|requires|accept|accepts|expect|expects|offer|offers|consider|considers|"
    r"look|looks|welcome|welcomes|do|does|apply|applies)\b",
    re.I,
)
_CROSS_REFERENCE = re.compile(
    r"\b(see|refer to|read more|find out|check|visit|listed on|details on|"
    r"information on|more about)\b",
    re.I,
)
_PROCESS = re.compile(
    r"\b(fee|fees|deadline|submit|application form|apply online|portal|"
    r"personal statement|reference letter|interview date|how to apply)\b",
    re.I,
)


def classify_low(text_value: str) -> str:
    """Which shape this LOW candidate has. Description, not a verdict.

    Section 22 asks what is left and why, not for the band to be emptied. A reviewer
    deciding to drop `PROSE_WITHOUT_REQUIREMENT_WORDING` wholesale is making a recall
    trade with the number in front of them, which is the point of counting it.
    """
    stripped = (text_value or "").strip()
    if not _FINITE_VERB.search(stripped):
        return "NO_VERB"
    if _CROSS_REFERENCE.search(stripped):
        return "CROSS_REFERENCE"
    if _PROCESS.search(stripped):
        return "PROCESS_NOT_REQUIREMENT"
    if len(stripped) < 80 and not stripped.endswith((".", "!", "?")):
        return "QUALIFICATION_LIST_ITEM"
    return "PROSE_WITHOUT_REQUIREMENT_WORDING"


def command_low(args: argparse.Namespace) -> int:
    """What remains in the LOW band, by shape (section 22)."""
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.field_kind, c.evidence_text, c.confidence_reason,
                       c.value_raw_text
                  FROM field_claim_candidate c
                 WHERE {current} AND c.confidence_band = 'LOW'
                 ORDER BY c.field_kind
                """.format(current=current_only("c"))
            ),
            CURRENT_VERSION_KEYS,
        ).all()

    print("=" * 78)
    print("WHAT IS LEFT IN THE LOW BAND, AND WHY (section 22)")
    print("=" * 78)
    print(
        "\n  LOW says the extraction was ambiguous, not that the fact is false. Nothing\n"
        "  here is deleted and nothing is resolved by a rule: the shapes are described\n"
        "  so the next step can be chosen with the numbers visible.\n"
    )
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row.field_kind] = by_kind.get(row.field_kind, 0) + 1
    print(f"  {len(rows)} LOW candidate(s) in total:")
    for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"    {kind:30} {count}")

    admission_rows = [r for r in rows if r.field_kind == "ADMISSION_REQUIREMENT"]
    buckets: dict[str, list[str]] = {name: [] for name, _ in LOW_PATTERNS}
    for row in admission_rows:
        buckets[classify_low(row.evidence_text or "")].append(row.evidence_text or "")

    print(f"\n  the {len(admission_rows)} LOW admission requirement(s), by shape:")
    for name, why in LOW_PATTERNS:
        found = buckets[name]
        print(f"\n    {name}  ({len(found)})")
        print(f"      {why}")
        for sample in found[: args.samples]:
            print(f"        {sample[:110]!r}")

    print(
        "\n  None of these is solved here. The largest bucket exists because a page's own\n"
        "  title matched the requirements-heading test, so every paragraph beneath it\n"
        "  became a candidate -- and removing the title from that test would take real\n"
        "  requirement prose with it. That is a recall trade for a reviewer to make."
    )
    engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact-root", type=Path, default=Path(".artifacts-full"))
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="extract candidate claims from stored documents")
    runner.add_argument("--limit", type=int, default=None)
    sub.add_parser("coverage", help="coverage by responsibility, institution and gap kind")
    sanity = sub.add_parser("sanity", help="automated checks for claims that cannot be true")
    sanity.add_argument("--resolve-limit", type=int, default=200)
    audit = sub.add_parser("audit", help="samples for manual precision review")
    audit.add_argument("--kind", default=None, choices=AUDIT_KINDS)
    audit.add_argument("--limit", type=int, default=20)
    lineage = sub.add_parser("lineage", help="walk one claim back to the bytes")
    lineage.add_argument("--claim-id", default=None)
    sub.add_parser("untouched", help="prove nothing publishable or canonical moved")
    low = sub.add_parser("low", help="what is left in the LOW band, by shape")
    low.add_argument("--samples", type=int, default=3)

    args = parser.parse_args(argv)
    commands = {
        "run": command_run,
        "coverage": command_coverage,
        "sanity": command_sanity,
        "audit": command_audit,
        "lineage": command_lineage,
        "untouched": command_untouched,
        "low": command_low,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
