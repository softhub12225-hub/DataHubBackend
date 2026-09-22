"""Run the language-model field extractor over stored documents (OpenAI).

Usage::

    uv run python apps/api/scripts/llm_claims.py preflight
    uv run python apps/api/scripts/llm_claims.py run --limit 5
    uv run python apps/api/scripts/llm_claims.py run --i-understand-cost
    uv run python apps/api/scripts/llm_claims.py report

WHY THIS READS DOCUMENTS RATHER THAN URLS
=========================================
The obvious shape for this command is "give the model the workbook's links and write
what it says". The database refuses that, and not as a policy check: a
`field_claim_candidate` needs an `extraction_id`, an `extraction` needs a `snapshot_id`,
and a `snapshot` needs a `fetch_run`. There is no way to store a claim that does not
descend from a page this system fetched and hashed. Foreign keys, not opinion.

That is the same reason the model is never handed a URL. A claim it produced from a
link would have no stored bytes behind it, so the quote check -- the thing that makes
its output admissible at all -- would have nothing to check against, and the reviewer
looking at the claim would have nothing to open.

So the input is the set of documents already extracted from stored snapshots, which is
what `extract_documents.py run` produces. Pages that have not been fetched yet are not
skipped silently: `preflight` reports how many of the workbook's URLs have a document
and how many do not, because "the model found nothing" and "there was nothing to read"
must never look the same.

COST IS NOT INCIDENTAL
======================
Every document is one API call carrying up to a few thousand tokens of page text. Over
319 pages that is real money, and unlike the rest of this pipeline it is spent whether
or not the run produces anything. `run` therefore refuses to process more than
`--limit` documents without `--i-understand-cost`, mirroring how the acquisition worker
refuses to contact a lot of universities without `--i-understand`.

NOTHING HERE IS PUBLISHED
=========================
It writes `field_claim_candidate` rows. No `field_claim`, no `field_provenance`, no
`change_proposal`, and no source becomes publication eligible. A HIGH confidence band
from a model means what a HIGH band from a regex means: nobody has looked yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import build_evidence_store
from app.domains.claims.llm import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    LlmExtractionError,
    extract,
)
from app.domains.claims.runner import (
    _insert,
    load_document,
    targets_for_claims,
)
from app.domains.extraction.runner import DERIVED_PREFIX

#: Above this, `run` wants `--i-understand-cost`. Twenty documents is enough to see
#: whether the extractor is reading or guessing before paying for three hundred.
COST_GATE = 20


def _engine(role: DatabaseRole) -> Engine:
    settings = get_settings()
    return create_engine(settings.database.sync_dsn(role), future=True)


def _preflight(engine: Engine) -> int:
    """What the model would be given, and what is missing.

    The second number is the one that matters. A run over 19 of 319 pages is not a
    failed extraction, it is an incomplete corpus, and reporting only the claims it
    produced would hide that.
    """
    settings = get_settings()
    with engine.connect() as connection:
        targets = targets_for_claims(connection)
        registered = connection.execute(
            text("SELECT count(*) FROM pilot_collected_source")
        ).scalar_one()
        fetched = connection.execute(text("SELECT count(*) FROM snapshot")).scalar_one()

    print(f"workbook rows registered     {registered}")
    print(f"pages fetched and snapshotted {fetched}")
    print(f"documents ready to extract    {len(targets)}")
    print(f"pages with nothing to read    {registered - fetched}")
    print()
    print(f"model    {settings.openai.model}")
    print(f"endpoint {settings.openai.base_url or 'api.openai.com (default)'}")
    key = settings.openai.api_key.get_secret_value()
    print(f"api key  {'set' if key and key != 'change-me' else 'NOT SET -- run will fail'}")
    print()
    print("No API call was made. No document was read. Nothing was written.")
    return 0


def _run(engine: Engine, *, limit: int | None, acknowledged: bool) -> int:
    settings = get_settings()
    key = settings.openai.api_key.get_secret_value()
    if not key or key == "change-me":
        print("refusing: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    store = build_evidence_store(settings, prefix=DERIVED_PREFIX)
    with engine.connect() as connection:
        targets = targets_for_claims(connection)
    if limit is not None:
        targets = targets[:limit]

    if len(targets) > COST_GATE and not acknowledged:
        print(
            f"refusing: {len(targets)} documents is {len(targets)} paid API calls. "
            f"Re-run with --limit {COST_GATE} to sample, or --i-understand-cost to "
            "process them all.",
            file=sys.stderr,
        )
        return 2

    created = rejected = abstained = failed = 0
    for target in targets:
        try:
            document = load_document(store, target.document_hash)
        except (KeyError, OSError, ValueError) as exc:
            print(f"  {target.url}: artifact unreadable ({exc})", file=sys.stderr)
            failed += 1
            continue

        try:
            outcome = extract(document, settings.openai)
        except LlmExtractionError as exc:
            # Loudly, and counted. A failed call must not be indistinguishable from a
            # page the model read and found nothing in.
            print(f"  {target.url}: {exc}", file=sys.stderr)
            failed += 1
            continue

        rejected += outcome.rejected_unquoted
        abstained += outcome.abstentions

        with engine.begin() as connection:
            for candidate in outcome.candidates:
                # Every responsibility this page was claimed for gets the candidate:
                # one page can be the official source for several fields, and a claim
                # belongs to each of them.
                for claim_id, responsibility in target.responsibilities:
                    if _insert(
                        connection,
                        target=target,
                        candidate=candidate,
                        pilot_claim_id=claim_id,
                        responsibility=responsibility,
                        extractor=EXTRACTOR_NAME,
                        version=EXTRACTOR_VERSION,
                    ):
                        created += 1

        print(
            f"  {target.url}  "
            f"{len(outcome.candidates)} kept, {outcome.rejected_unquoted} unquoted, "
            f"{outcome.abstentions} not-stated"
        )

    print()
    print(f"documents read        {len(targets) - failed}")
    print(f"candidates created    {created}")
    print(f"abstentions recorded  {abstained}")
    print(f"discarded as unquoted {rejected}")
    print(f"documents failed      {failed}")
    print()
    if rejected:
        # The signal worth watching. A model that reads returns quotes that are in the
        # page; a rising share of unquotable claims is the extractor drifting toward
        # invention, and it is visible here before a reviewer ever sees it.
        share = rejected / max(1, rejected + created)
        print(f"NOTE: {share:.0%} of claims could not be quoted from their document.")
    print(
        "Candidates only. No field_claim, no change proposal, and nothing became "
        "publication eligible."
    )
    return 0


def _report(engine: Engine) -> int:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT field_kind, confidence_band, count(*) AS n "
                "  FROM field_claim_candidate "
                " WHERE extractor_name = :name "
                " GROUP BY field_kind, confidence_band "
                " ORDER BY n DESC"
            ),
            {"name": EXTRACTOR_NAME},
        ).all()
        unresolved = connection.execute(
            text(
                "SELECT count(*) FROM field_claim_candidate "
                " WHERE extractor_name = :name AND unresolved_reason IS NOT NULL"
            ),
            {"name": EXTRACTOR_NAME},
        ).scalar_one()

    if not rows:
        print(f"no candidates from extractor {EXTRACTOR_NAME!r} yet.")
        return 0
    print(f"candidates from {EXTRACTOR_NAME} {EXTRACTOR_VERSION}:")
    for row in rows:
        print(f"  {row.field_kind:28} {row.confidence_band:8} {row.n}")
    print(f"\n  of which unresolved (not stated on the page): {unresolved}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="what would be read, and what is missing")
    run = sub.add_parser("run", help="extract candidates (CALLS THE OPENAI API)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--i-understand-cost", action="store_true", dest="acknowledged")
    sub.add_parser("report", help="what this extractor has produced so far")

    args = parser.parse_args()
    role = DatabaseRole(args.role) if args.role else DatabaseRole.API
    engine = _engine(role)

    if args.command == "preflight":
        return _preflight(engine)
    if args.command == "run":
        return _run(engine, limit=args.limit, acknowledged=args.acknowledged)
    if args.command == "report":
        return _report(engine)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
