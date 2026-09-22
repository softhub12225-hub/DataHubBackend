"""Collect university facts via OpenAI knowledge (ChatGPT-style), stage for review.

Unlike ``ai_collect.py`` (fetch page → quote check) and ``extract_claims_openai.py``
(read stored artifacts), this asks the model what it knows about each university's
registered responsibilities, then stages every answer as ``NEEDS_REVIEW`` in
``pilot_collected_fact`` for a human to compare against the official URL.

No crawler. No local artifact store. Run on the deployed API service (Railway), not
on a laptop::

    python apps/api/scripts/ai_knowledge_collect.py preflight
    python apps/api/scripts/ai_knowledge_collect.py run --limit 3
    python apps/api/scripts/ai_knowledge_collect.py run --i-understand-cost
    python apps/api/scripts/ai_knowledge_collect.py report

Or trigger online::

    POST /api/v1/review/operations/ai-knowledge
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.domains.pilot import ai_knowledge as ak


def _preflight() -> int:
    engine = ak.engine_for()
    try:
        info = ak.preflight(engine)
    finally:
        engine.dispose()
    print(f"institutions to query              {info['institutions']}")
    print(f"workbook URLs attached             {info['workbook_urls']}")
    print(f"previous {ak.SHEET_NAME} runs             {info['previous_runs']}")
    print(f"facts staged by {ak.SHEET_NAME} so far    {info['facts_staged']}")
    print()
    print(f"model     {info['model']}")
    print(f"api key   {'set' if info['api_key_set'] else 'NOT SET'}")
    print()
    print(
        f"A full run is {info['institutions']} paid OpenAI calls "
        "(one per institution). No crawler / no local artifacts."
    )
    return 0


def _run(*, limit: int | None, acknowledged: bool) -> int:
    engine = ak.engine_for()
    try:
        def progress(name: str, kept: int, error: str | None) -> None:
            if error:
                print(f"  {name}: model failed ({error})")
            else:
                print(f"  {name}: {kept} fact(s) staged")

        try:
            result = ak.run_collection(
                engine,
                limit=limit,
                acknowledged=acknowledged,
                progress=progress,
            )
        except RuntimeError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
    finally:
        engine.dispose()

    print()
    if result.get("submission_id"):
        print(f"submission {result['submission_id']}")
    print(f"mode: {ak.SHEET_NAME} (model knowledge → NEEDS_REVIEW)\n")
    print(f"institutions queried  {result['institutions']}")
    print(f"facts staged          {result['facts_staged']}")
    print(f"model failures        {result['failures']}")
    print()
    print(result["message"])
    if result["status"] == "failed":
        return 1
    return 0


def _report() -> int:
    engine = ak.engine_for()
    try:
        rows = ak.report(engine)
    finally:
        engine.dispose()
    if not rows:
        print(f"no {ak.SHEET_NAME} submissions yet")
        return 0
    print(f"{'imported_at':28} {'unis':>5} {'facts':>7}  id")
    for row in rows:
        print(
            f"{(row['imported_at'] or '')[:28]:28} {row['universities']:5} "
            f"{row['facts']:7}  {row['id']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="counts and config; writes nothing")
    run = sub.add_parser("run", help="query OpenAI per institution; stage NEEDS_REVIEW facts")
    run.add_argument("--limit", type=int, default=None, help="max institutions")
    run.add_argument(
        "--i-understand-cost",
        action="store_true",
        help=f"allow more than {ak.COST_GATE} institutions",
    )
    sub.add_parser("report", help="list recent ai_knowledge submissions")
    args = parser.parse_args(argv)

    if args.command == "preflight":
        return _preflight()
    if args.command == "report":
        return _report()
    return _run(limit=args.limit, acknowledged=args.i_understand_cost)


if __name__ == "__main__":
    raise SystemExit(main())
