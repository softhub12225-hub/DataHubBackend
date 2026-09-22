"""End-to-end pilot pipeline for all 35 workbook universities.

Stages (same responsibilities as the review console: 11 source rows each)::

    import   — load university_official_sources.xlsx into pilot staging
    register — create acquisition ``source`` rows (NOT_ELIGIBLE)
    enqueue  — queue fetch attempts for the pilot fleet
    fetch    — contact real university websites (requires --i-understand)
    docs     — normalise HTML/PDF into artifact store
    claims   — deterministic field_claim_candidate extraction
    openai   — OpenAI field_claim_candidate extraction (requires OPENAI_API_KEY)
    all      — import → register → enqueue → fetch → docs → openai

Usage::

    uv run python scripts/run_all_universities.py import
    uv run python scripts/run_all_universities.py fetch --i-understand --max-pages 385
    uv run python scripts/run_all_universities.py openai
    uv run python scripts/run_all_universities.py all --i-understand
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import DatabaseRole, get_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKBOOK = REPO_ROOT / "university_official_sources.xlsx"
SCRIPTS = Path(__file__).resolve().parent


def _load_repo_dotenv() -> None:
    """Load repo-root ``.env`` when scripts run from ``apps/api`` (matches ``api.cmd``)."""
    path = REPO_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=cwd or SCRIPTS.parent)  # noqa: S603


def _engine():
    settings = get_settings()
    return create_engine(
        settings.database.sync_dsn(DatabaseRole.API),
        future=True,
        connect_args={"connect_timeout": 10},
    )


def cmd_import(args: argparse.Namespace) -> int:
    if not args.workbook.is_file():
        print(f"workbook not found: {args.workbook}", file=sys.stderr)
        return 2
    cmd = [
        sys.executable,
        str(SCRIPTS / "import_official_sources.py"),
        str(args.workbook),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.imported_by:
        cmd.extend(["--imported-by", str(args.imported_by)])
    return _run(cmd)


def cmd_register(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "acquisition.py"), "register"]
    if args.dry_run:
        cmd.append("--dry-run")
    return _run(cmd)


def cmd_enqueue(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(SCRIPTS / "acquisition.py"),
        "enqueue",
        "--pilot",
    ]
    if args.cycle:
        cmd.extend(["--cycle", args.cycle])
    if args.dry_run:
        cmd.append("--dry-run")
    return _run(cmd)


def cmd_fetch(args: argparse.Namespace) -> int:
    if not args.i_understand and (args.max_pages is None or args.max_pages > 10):
        print(
            "fetch contacts real sites: pass --i-understand or keep --max-pages <= 10",
            file=sys.stderr,
        )
        return 2
    cmd = [
        sys.executable,
        str(SCRIPTS / "acquisition.py"),
        "worker",
        "--max-pages",
        str(args.max_pages if args.max_pages is not None else 385),
        "--i-understand",
    ]
    if args.evidence_root:
        cmd.extend(["--evidence-root", str(args.evidence_root)])
    return _run(cmd)


def cmd_docs(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "extract_documents.py"), "run"]
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])
    return _run(cmd)


def cmd_claims(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "extract_claims.py"), "run"]
    return _run(cmd)


def cmd_openai(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "extract_claims_openai.py"), "run"]
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])
    if args.institution:
        cmd.extend(["--institution", args.institution])
    return _run(cmd)


def cmd_status(_args: argparse.Namespace) -> int:
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                  (SELECT count(DISTINCT ti.id) FROM target_institution ti
                     JOIN pilot_selected_university psu
                       ON psu.target_institution_id = ti.id) AS institutions,
                  (SELECT count(*) FROM pilot_collected_source) AS pilot_sources,
                  (SELECT count(*) FROM source) AS acquisition_sources,
                  (SELECT count(*) FROM snapshot) AS snapshots,
                  (SELECT count(*) FROM extraction WHERE status <> 'FAILED') AS extractions,
                  (SELECT count(*) FROM field_claim_candidate
                     WHERE extractor_name = 'openai_structured') AS openai_candidates,
                  (SELECT count(*) FROM field_claim_candidate) AS all_candidates
                """
            )
        ).mappings().one()
    for key, value in row.items():
        print(f"{key}: {value}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    steps = [
        ("import", cmd_import),
        ("register", cmd_register),
        ("enqueue", cmd_enqueue),
        ("fetch", cmd_fetch),
        ("docs", cmd_docs),
    ]
    if not args.skip_deterministic_claims:
        steps.append(("claims", cmd_claims))
    if not args.skip_openai:
        steps.append(("openai", cmd_openai))

    for name, handler in steps:
        print(f"\n=== {name} ===", flush=True)
        code = handler(args)
        if code != 0:
            print(f"stage {name} failed with exit {code}", file=sys.stderr)
            return code
    cmd_status(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=DEFAULT_WORKBOOK,
        help=f"official source list (default: {DEFAULT_WORKBOOK.name} in repo root)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--imported-by",
        type=uuid.UUID,
        default=None,
        help="app_user id for the import audit row",
    )
    parser.add_argument("--cycle", default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--i-understand", action="store_true")
    parser.add_argument("--evidence-root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--institution", default=None)
    parser.add_argument("--skip-openai", action="store_true")
    parser.add_argument("--skip-deterministic-claims", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import", help="import workbook into pilot staging")
    sub.add_parser("register", help="register distinct URLs as sources")
    sub.add_parser("enqueue", help="enqueue pilot fetch cycle")
    sub.add_parser("fetch", help="run acquisition worker")
    sub.add_parser("docs", help="extract normalised documents")
    sub.add_parser("claims", help="deterministic claim extraction")
    sub.add_parser("openai", help="OpenAI claim extraction")
    sub.add_parser("status", help="print fleet counts")
    sub.add_parser("all", help="run full pipeline")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_repo_dotenv()
    args = build_parser().parse_args(argv)
    handlers = {
        "import": cmd_import,
        "register": cmd_register,
        "enqueue": cmd_enqueue,
        "fetch": cmd_fetch,
        "docs": cmd_docs,
        "claims": cmd_claims,
        "openai": cmd_openai,
        "status": cmd_status,
        "all": cmd_all,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
