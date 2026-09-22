"""Run OpenAI field-claim extraction over stored pilot documents.

Usage::

    uv run python scripts/extract_claims_openai.py run
    uv run python scripts/extract_claims_openai.py run --limit 5
    uv run python scripts/extract_claims_openai.py run --institution "imperial"

Requires ``OPENAI_API_KEY``. Reads normalised artifacts only; writes
``field_claim_candidate`` rows tagged ``openai_structured`` / ``1``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.claims.openai_runner import run_openai_claims

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_repo_dotenv() -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="extract candidates for the pilot fleet")
    run.add_argument("--limit", type=int, default=None, help="max documents to process")
    run.add_argument(
        "--institution",
        default=None,
        help="filter by institution name substring (case-insensitive)",
    )
    run.add_argument(
        "--artifact-root",
        type=Path,
        default=None,
        help="override ARTIFACT_ROOT (default: from settings)",
    )
    run.add_argument(
        "--role",
        default=DatabaseRole.WORKER.value,
        choices=[role.value for role in DatabaseRole],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_repo_dotenv()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.command != "run":
        return 2

    key = settings.openai.api_key
    if key is None or not key.get_secret_value().strip():
        print("OPENAI_API_KEY is not set in the environment / .env", file=sys.stderr)
        return 2

    artifact_root = args.artifact_root or Path(settings.artifact_root)
    engine = create_engine(
        settings.database.sync_dsn(DatabaseRole(args.role)), future=True
    )
    store = FilesystemEvidenceStore(artifact_root)

    print(f"artifact root: {artifact_root.resolve()}")
    print(f"model: {settings.openai.model}")
    print("offline over stored documents; writing field_claim_candidate only")

    report = run_openai_claims(
        engine,
        artifacts=store,
        limit=args.limit,
        institution=args.institution,
    )
    print(report.summary())
    if report.failures:
        print(f"{len(report.failures)} failure(s); first: {report.failures[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
