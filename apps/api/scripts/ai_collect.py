"""AI collection: read the workbook's pages with a model, stage what it found.

Usage::

    uv run python apps/api/scripts/ai_collect.py preflight
    uv run python apps/api/scripts/ai_collect.py run --limit 5
    uv run python apps/api/scripts/ai_collect.py run --i-understand-cost
    uv run python apps/api/scripts/ai_collect.py report

WHAT THIS IS
============
A collection pass over the registered official-source URLs performed by a language
model instead of a person. Each page is retrieved once, the model is asked what it
states, and every answer it can quote from that page is staged in
`pilot_collected_fact` -- the same table a returned collection workbook lands in,
because it is the same kind of artifact: someone's account of what they read.

It does not use the crawler. No cycle, no queue, no lease, no cron, no snapshot. One
HTTP GET per page, inside this command.

WHY NOT `field_claim_candidate`
===============================
Because the database will not take it, and not as a policy check: a
`field_claim_candidate` needs an `extraction_id`, an `extraction` needs a
`snapshot_id`, and a `snapshot` needs a `fetch_run`. Nothing can be stored there that
did not come from a page the acquisition pipeline fetched and hashed. Foreign keys,
not opinion.

The workbook plane has no such ancestry requirement, and that is not a loophole -- it
is the plane's purpose. It exists for facts a collector gathered by reading pages,
with no snapshot behind them, which a reviewer then has to verify before any of it can
move. AI collection is exactly that, with a faster collector and a worse memory.

So every run is its own submission: `COLLECTION_WORKBOOK`, `defines_pilot_scope`
false -- a machine-produced workbook sitting beside the human ones, comparable with
them and subject to the same review.

WHY EACH RUN COPIES THE SOURCE ROWS IT READ
===========================================
`pilot_collected_fact` carries a constraint (`asserted_row_cites_a_source_ref`) that
an asserted row must name a `source_ref`, and the ref resolves *within its own
submission*. A submission therefore cannot assert a fact about a page it does not
itself list. So the run copies the source rows it is about to read into its own
submission, keeping their refs, and every fact cites the page it was read from. The
constraint is right: a collection artifact asserting facts about pages it never
recorded reading is not reviewable.

Only the physical pages are copied -- the rows with no `duplicate_of_source_ref`. In
the supplied file 66 of 385 URL cells repeat a URL already listed under another
heading, and fetching a page once per heading would be scores of needless requests to
universities.

THE MODEL IS STILL NEVER BELIEVED
=================================
It is shown the page's text and nothing else: no URL, no institution name, no
category. Every value it returns must be quoted verbatim from that page, and the quote
is checked here against the retrieved bytes. A claim that cannot be found is
discarded, not down-weighted. That check is the only reason any of this is worth
storing, and it is why the page is fetched at all rather than asking the model what it
knows about the university.

Nothing is converted. A quoted "AUD 45,600 per annum" is stored as that string with
`amount_min` left NULL, because parsing it into a number is a reading a reconciler
makes deliberately, not something a collection pass does in passing.

ABSTENTION IS BOUNDED BY THE PAGE'S REMIT
=========================================
`OFFICIALLY_NOT_PUBLISHED` means the institution does not publish this -- a strong
claim one page cannot establish. "The fees page states no fee" is that finding; "the
admissions page did not mention fees" is not, it is a page about something else.

So an abstention is recorded only when the page was registered as the authority for
that category (`LANGUAGE_REQUIREMENTS`, `TUITION_FEES`, `APPLICATION_DEADLINES`,
`ENTRY_REQUIREMENTS`). Abstentions outside a page's remit are counted and reported,
never stored. Quoted values are stored whatever the page's category, because a fee
quoted on an admissions page is still a fee that page states.

WHAT IT CANNOT DO
=================
Every row lands `NEEDS_REVIEW`. No `field_claim`, no `field_provenance`, no
`change_proposal`, nothing becomes publication eligible, and no `pilot_*` table is
reachable from `field_provenance` by any foreign key. A human decides what is true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.core.config import DatabaseRole, get_settings
from app.db.enums import (
    FieldStatus,
    PilotFactValidationState,
    PilotImportStatus,
    PilotSubmissionKind,
    SourceCategory,
)
from app.domains.claims.llm import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    LlmExtractionError,
    extract,
)
from app.domains.claims.model import FieldKind
from app.domains.extraction.html_document import parse_html_document

#: The sheet name every row this command writes carries, so an AI-collected fact is
#: never mistaken for one a person read. A reviewer should weigh it differently, and
#: cannot do that if it looks like everything else in the plane.
SHEET_NAME = "ai_collect"

#: Above this many pages, `run` wants the cost acknowledged. Each page is one paid API
#: call plus one HTTP request, spent whether or not anything is found.
COST_GATE = 20

#: How the extractor's field kinds land in the workbook's fact vocabulary. `fact_type`
#: is held to `PILOT_FACT_TYPES` by a CHECK, so this mapping is the whole of what may
#: be written.
FACT_FOR_KIND: dict[FieldKind, tuple[str, str]] = {
    FieldKind.LANGUAGE_OVERALL_SCORE: ("LANGUAGE_REQUIREMENT", "language.overall_score"),
    FieldKind.LANGUAGE_COMPONENT_SCORE: ("LANGUAGE_REQUIREMENT", "language.component_score"),
    FieldKind.LANGUAGE_TEST: ("LANGUAGE_REQUIREMENT", "language.test"),
    FieldKind.TUITION: ("TUITION", "tuition.amount"),
    FieldKind.APPLICATION_DEADLINE: ("DEADLINE", "deadline.application"),
    FieldKind.ADMISSION_REQUIREMENT: ("ADMISSION_REQUIREMENT", "admission.requirement"),
}

#: Which registered category makes a page the authority for a fact type -- and so the
#: only case where "this page states none" is a finding rather than a page about
#: something else. Deliberately narrow: an admissions page usually does carry language
#: requirements, but it is not *the* language page, and a false
#: OFFICIALLY_NOT_PUBLISHED is expensive to discover.
ABSTENTION_REMIT: dict[str, str] = {
    SourceCategory.LANGUAGE_REQUIREMENTS.value: "LANGUAGE_REQUIREMENT",
    SourceCategory.TUITION_FEES.value: "TUITION",
    SourceCategory.APPLICATION_DEADLINES.value: "DEADLINE",
    SourceCategory.ENTRY_REQUIREMENTS.value: "ADMISSION_REQUIREMENT",
}


#: Written out twice rather than concatenated: a query assembled from fragments is a
#: query nobody greps for, and the only difference between these two is the LIMIT.
_PHYSICAL_SOURCES = text(
    "SELECT source_ref, target_institution_id, sheet_row_no, source_type, "
    "       degree_scope, workbook_column, official_url, normalized_url, "
    "       url_sha256, host, is_third_party "
    "  FROM pilot_collected_source "
    " WHERE duplicate_of_source_ref IS NULL "
    " ORDER BY source_ref"
)
_PHYSICAL_SOURCES_LIMITED = text(
    "SELECT source_ref, target_institution_id, sheet_row_no, source_type, "
    "       degree_scope, workbook_column, official_url, normalized_url, "
    "       url_sha256, host, is_third_party "
    "  FROM pilot_collected_source "
    " WHERE duplicate_of_source_ref IS NULL "
    " ORDER BY source_ref LIMIT :limit"
)


def abstention_is_a_finding(source_type: str, fact_type: str) -> bool:
    """May "this page states none" be stored as OFFICIALLY_NOT_PUBLISHED?

    Only from the page registered as the authority for that category. Everywhere else
    the model's silence says something about the page, not about the institution, and
    the difference is the whole meaning of the column.
    """
    return ABSTENTION_REMIT.get(source_type) == fact_type


def _engine(role: DatabaseRole) -> Engine:
    return create_engine(get_settings().database.sync_dsn(role), future=True)


def _physical_sources(connection: Connection, limit: int | None) -> list[dict[str, Any]]:
    """The pages to read: one row per distinct URL, not one per claimed responsibility.

    `duplicate_of_source_ref IS NULL` is what draws that line. The schema keeps a
    repeated URL as a separate responsibility claim on purpose, and only the physical
    rows are meant to be fetched.
    """
    if limit is None:
        rows = connection.execute(_PHYSICAL_SOURCES).all()
    else:
        rows = connection.execute(_PHYSICAL_SOURCES_LIMITED, {"limit": limit}).all()
    return [dict(row._mapping) for row in rows]


def _open_submission(
    connection: Connection, *, model: str, sources: list[dict[str, Any]]
) -> uuid.UUID:
    """Create the run's submission and copy into it the pages it is about to read."""
    submission_id = uuid.uuid4()
    stamp = datetime.now(UTC).isoformat(timespec="microseconds")
    institutions = {source["target_institution_id"] for source in sources}
    # No file exists, so the submission's identity is taken over the run's parameters
    # instead. Unique, as the column requires, and a second identical run becomes a
    # second submission rather than an overwrite -- which is how this plane already
    # treats every re-import.
    digest = hashlib.sha256(
        "|".join([SHEET_NAME, model, stamp, *(s["source_ref"] for s in sources)]).encode()
    ).hexdigest()
    connection.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "  file_byte_size, template_version, submission_kind, defines_pilot_scope, "
            "  imported_at, selected_university_count, import_status, notes) "
            "VALUES (:id, :sha, :name, 1, :template, :kind, false, now(), :count, "
            "  :status, :notes)"
        ),
        {
            "id": submission_id,
            "sha": digest,
            "name": f"{SHEET_NAME}-{stamp}",
            "template": f"{EXTRACTOR_NAME}-{EXTRACTOR_VERSION}",
            "kind": PilotSubmissionKind.COLLECTION_WORKBOOK.value,
            "count": len(institutions),
            "status": PilotImportStatus.VALIDATED.value,
            "notes": (
                f"Collected by {SHEET_NAME} using {model} over {len(sources)} page(s). "
                "Every stored value is quoted from the page it was read from; claims "
                "the model could not quote were discarded. No snapshot exists behind "
                "these rows, so every one is NEEDS_REVIEW. file_byte_size is the total "
                "retrieved page bytes, written when the run finishes -- it reads 1 "
                "while the run is in flight, because the column may not be 0."
            ),
        },
    )
    for source in sources:
        connection.execute(
            text(
                "INSERT INTO pilot_collected_source (submission_id, source_ref, "
                "  target_institution_id, sheet_row_no, source_type, degree_scope, "
                "  workbook_column, official_url, normalized_url, url_sha256, host, "
                "  is_third_party, collector_notes) "
                "VALUES (:submission, :ref, :institution, :row, :type, :scope, "
                "  :column, :url, :normalized, :sha, :host, :third_party, :notes)"
            ),
            {
                "submission": submission_id,
                "ref": source["source_ref"],
                "institution": source["target_institution_id"],
                "row": source["sheet_row_no"],
                "type": source["source_type"],
                "scope": source["degree_scope"],
                "column": source["workbook_column"],
                "url": source["official_url"],
                "normalized": source["normalized_url"],
                "sha": source["url_sha256"],
                "host": source["host"],
                "third_party": source["is_third_party"],
                # Left PENDING, like any imported candidate. A machine reading a page
                # is not a human deciding the page is worth registering.
                "notes": f"Read by {SHEET_NAME}; ref kept from the official-source list.",
            },
        )
    return submission_id


def _fetch(url: str, *, timeout: float) -> tuple[bytes, str | None, str]:
    """One GET. Not the crawler: no lease, no politeness window, no snapshot.

    Redirects are followed because the supplied URLs have already been observed to
    move, and the URL that actually answered is recorded with every fact, so a reviewer
    opens the page that was read rather than the one that was listed.
    """
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        response = client.get(url, headers={"User-Agent": f"datahub-{SHEET_NAME}"})
        response.raise_for_status()
        return response.content, response.headers.get("content-type"), str(response.url)


def _insert_fact(
    connection: Connection,
    *,
    submission_id: uuid.UUID,
    row_no: int,
    source: dict[str, Any],
    fact_type: str,
    field_path: str,
    value_text: str,
    evidence: str,
    effective_url: str,
    not_stated: bool,
    model: str,
    detail: dict[str, Any],
) -> None:
    connection.execute(
        text(
            "INSERT INTO pilot_collected_fact (submission_id, sheet_name, sheet_row_no, "
            "  target_institution_id, source_ref, fact_type, field_path, field_status, "
            "  collected_values, official_text, source_url, collector_notes, "
            "  validation_state) "
            "VALUES (:submission, :sheet, :row, :institution, :ref, :fact_type, "
            "  :field_path, :status, CAST(:collected AS jsonb), :official_text, :url, "
            "  :notes, :state)"
        ),
        {
            "submission": submission_id,
            "sheet": SHEET_NAME,
            "row": row_no,
            "institution": source["target_institution_id"],
            # The constraint that shaped this command: an asserted row names the page
            # it was read from, and the ref resolves inside this submission.
            "ref": source["source_ref"],
            "fact_type": fact_type,
            "field_path": field_path,
            # The distinction this column exists to preserve: the source publishing
            # nothing is a different fact from nobody having looked.
            "status": (
                FieldStatus.OFFICIALLY_NOT_PUBLISHED.value
                if not_stated
                else FieldStatus.PUBLISHED.value
            ),
            "collected": json.dumps(detail),
            # The wording, unparsed. `amount_min`/`amount_max` stay NULL even on a
            # tuition row: turning "AUD 45,600 per annum" into a number is a reading,
            # and this plane records what was read, not what it was taken to mean.
            "official_text": (evidence or value_text)[:4000] or None,
            "url": effective_url,
            "notes": (
                f"{SHEET_NAME} / {model} / {EXTRACTOR_NAME} {EXTRACTOR_VERSION}. "
                + (
                    "The page was retrieved and read, and states no value for this; "
                    "it is the registered authority for this category."
                    if not_stated
                    else f'Quoted from the page: "{value_text[:300]}"'
                )
            ),
            # Never OK. There is no snapshot behind this row, and no human has read it.
            "state": PilotFactValidationState.NEEDS_REVIEW.value,
        },
    )


def _preflight(engine: Engine) -> int:
    settings = get_settings()
    with engine.connect() as connection:
        sources = _physical_sources(connection, None)
        registered = connection.execute(
            text("SELECT count(*) FROM pilot_collected_source")
        ).scalar_one()
        staged = connection.execute(
            text("SELECT count(*) FROM pilot_collected_fact WHERE sheet_name = :s"),
            {"s": SHEET_NAME},
        ).scalar_one()
        runs = connection.execute(
            text("SELECT count(*) FROM pilot_submission WHERE template_version LIKE :t"),
            {"t": f"{EXTRACTOR_NAME}-%"},
        ).scalar_one()

    remit = sum(1 for source in sources if source["source_type"] in ABSTENTION_REMIT)
    key = settings.openai.api_key.get_secret_value()
    print(f"responsibility claims registered      {registered}")
    print(f"distinct pages to read                {len(sources)}")
    print(f"  of which may report 'not published' {remit}")
    print(f"previous {SHEET_NAME} runs                {runs}")
    print(f"facts staged by {SHEET_NAME} so far       {staged}")
    print()
    print(f"model     {settings.openai.model}")
    print(f"endpoint  {settings.openai.base_url or 'api.openai.com (default)'}")
    print(f"api key   {'set' if key and key != 'change-me' else 'NOT SET -- run will fail'}")
    print()
    print(f"A full run is {len(sources)} HTTP requests and {len(sources)} paid API calls.")
    print("Nothing was fetched, called or written by this command.")
    return 0


def _run(engine: Engine, *, limit: int | None, acknowledged: bool) -> int:
    settings = get_settings()
    key = settings.openai.api_key.get_secret_value()
    if not key or key == "change-me":
        print("refusing: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    with engine.connect() as connection:
        sources = _physical_sources(connection, limit)
    if not sources:
        print("no source URLs registered; import the official-source list first.")
        return 0
    if len(sources) > COST_GATE and not acknowledged:
        print(
            f"refusing: {len(sources)} pages is {len(sources)} HTTP requests and "
            f"{len(sources)} paid API calls. Re-run with --limit {COST_GATE} to sample, "
            "or --i-understand-cost to process them all.",
            file=sys.stderr,
        )
        return 2

    with engine.begin() as connection:
        submission_id = _open_submission(connection, model=settings.openai.model, sources=sources)
    print(f"submission {submission_id}\n")

    row_no = 1  # `sheet_row_no >= 2`, so the first fact written is row 2.
    staged = not_published = rejected = 0
    out_of_remit = unreachable = not_html = model_failed = 0
    total_bytes = 0

    for source in sources:
        url = str(source["official_url"])
        try:
            payload, content_type, effective = _fetch(
                url, timeout=settings.openai.request_timeout_seconds
            )
        except Exception as exc:  # httpx raises many unrelated types; all mean "no reading"
            print(f"  {source['source_ref']} {url}: not retrieved ({type(exc).__name__})")
            unreachable += 1
            continue

        if "html" not in (content_type or "").lower():
            # A PDF fed to the HTML normaliser yields plausible-looking rubbish, and
            # the quote check would then happily verify a claim against it.
            print(f"  {source['source_ref']} {url}: not HTML ({content_type})")
            not_html += 1
            continue

        total_bytes += len(payload)
        document = parse_html_document(payload, content_type=content_type, effective_url=effective)
        try:
            outcome = extract(document, settings.openai)
        except LlmExtractionError as exc:
            # Counted and printed. A failed call must never be indistinguishable from a
            # page that was read and states nothing.
            print(f"  {source['source_ref']} {url}: {exc}")
            model_failed += 1
            continue

        rejected += outcome.rejected_unquoted
        kept = 0
        with engine.begin() as connection:
            for candidate in outcome.candidates:
                mapped = FACT_FOR_KIND.get(candidate.field_kind)
                if mapped is None:
                    continue
                fact_type, field_path = mapped
                abstained = candidate.unresolved_reason is not None
                if abstained and not abstention_is_a_finding(str(source["source_type"]), fact_type):
                    out_of_remit += 1
                    continue
                row_no += 1
                _insert_fact(
                    connection,
                    submission_id=submission_id,
                    row_no=row_no,
                    source=source,
                    fact_type=fact_type,
                    field_path=field_path,
                    value_text=candidate.value_raw_text,
                    evidence=candidate.evidence_text,
                    effective_url=effective,
                    not_stated=abstained,
                    model=settings.openai.model,
                    detail={
                        "field_kind": candidate.field_kind.value,
                        "quote": candidate.value_raw_text,
                        "block_index": candidate.locator.block_index,
                        "confidence": candidate.confidence.value,
                        "confidence_reason": candidate.confidence_reason,
                        "quote_verified": not abstained,
                        "unresolved_reason": candidate.unresolved_reason,
                        "requested_url": url,
                        "effective_url": effective,
                        "registered_source_type": source["source_type"],
                        "model": settings.openai.model,
                        "extractor": f"{EXTRACTOR_NAME} {EXTRACTOR_VERSION}",
                    },
                )
                kept += 1
                staged += 1
                if abstained:
                    not_published += 1

        print(
            f"  {source['source_ref']} {url}  "
            f"{kept} staged, {outcome.rejected_unquoted} unquoted"
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE pilot_submission SET file_byte_size = :size, updated_at = now() "
                " WHERE id = :id"
            ),
            {"size": max(1, total_bytes), "id": submission_id},
        )

    print()
    print(f"facts staged             {staged}")
    print(f"  of which not published {not_published}")
    print(f"discarded as unquoted    {rejected}")
    print(f"abstentions out of remit {out_of_remit}")
    print(f"pages not retrieved      {unreachable}")
    print(f"pages that were not HTML {not_html}")
    print(f"pages the model failed   {model_failed}")
    print()
    if rejected:
        # The number worth watching over time. A model that reads returns quotes that
        # are in the page; a rising share of unquotable claims is the extractor
        # drifting toward invention, and it shows up here before a reviewer sees it.
        share = rejected / max(1, rejected + staged)
        print(f"NOTE: {share:.0%} of the model's claims were not found in their page.")
    print(
        f"Staged as submission {submission_id}. Every row is NEEDS_REVIEW with no "
        "snapshot behind it. No field_claim, no proposal, nothing publication eligible."
    )
    return 0


def _report(engine: Engine) -> int:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT fact_type, field_status, count(*) AS n "
                "  FROM pilot_collected_fact WHERE sheet_name = :s "
                " GROUP BY fact_type, field_status ORDER BY fact_type, field_status"
            ),
            {"s": SHEET_NAME},
        ).all()
        institutions = connection.execute(
            text(
                "SELECT count(DISTINCT target_institution_id) "
                "  FROM pilot_collected_fact WHERE sheet_name = :s"
            ),
            {"s": SHEET_NAME},
        ).scalar_one()

    if not rows:
        print(f"no facts staged by {SHEET_NAME} yet.")
        return 0
    print(f"facts staged by {SHEET_NAME}, over {institutions} institution(s):")
    for row in rows:
        print(f"  {row.fact_type:22} {row.field_status:26} {row.n}")
    print("\nAll of them NEEDS_REVIEW. None is publishable as it stands.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        default=DatabaseRole.API.value,
        choices=[role.value for role in DatabaseRole],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="what would be read, and what it costs")
    run = sub.add_parser("run", help="read the pages and stage what the model found")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--i-understand-cost", action="store_true", dest="acknowledged")
    sub.add_parser("report", help="what has been staged so far")

    args = parser.parse_args()
    engine = _engine(DatabaseRole(args.role))

    if args.command == "preflight":
        return _preflight(engine)
    if args.command == "run":
        return _run(engine, limit=args.limit, acknowledged=args.acknowledged)
    if args.command == "report":
        return _report(engine)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
