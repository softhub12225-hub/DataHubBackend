"""Fill missing page evidence online: register → enqueue → fetch → extract.

The review console shows BODY_EVIDENCE_NOT_AVAILABLE when a host is VERIFIED but no
snapshot/extraction exists. This module contacts the real URLs from the deployed API
process, stores bodies in the configured evidence backend (S3), and normalises them so
the evidence panel can render.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.core.logging import get_logger
from app.domains.acquisition.lease import enqueue_cycle, oldest_open_cycle
from app.domains.acquisition.registration import register_acquisition_targets
from app.domains.acquisition.runner import run_cycle
from app.domains.acquisition.storage import build_evidence_store
from app.domains.extraction.runner import DERIVED_PREFIX, run_extraction
from app.workers.tasks.crawl import cycle_key_for

logger = get_logger(__name__)

DEFAULT_MAX_PAGES = 10
HARD_MAX_PAGES = 20


def worker_engine() -> Engine:
    return create_engine(
        get_settings().database.sync_dsn(DatabaseRole.WORKER),
        future=True,
        connect_args={"connect_timeout": 20},
    )


def api_engine() -> Engine:
    return create_engine(
        get_settings().database.sync_dsn(DatabaseRole.API),
        future=True,
        connect_args={"connect_timeout": 20},
    )


def resolve_institution_id(connection: Connection, institution: str) -> uuid.UUID | None:
    """Match by match_key / display name substring (case-insensitive)."""
    needle = institution.strip().lower()
    if not needle:
        return None
    row = connection.execute(
        text(
            "SELECT id FROM target_institution "
            " WHERE lower(match_key) LIKE :q OR lower(coalesce(display_name, '')) LIKE :q "
            " ORDER BY match_key LIMIT 1"
        ),
        {"q": f"%{needle}%"},
    ).one_or_none()
    return row.id if row else None


def evidence_gap(connection: Connection, *, institution_id: uuid.UUID | None) -> dict[str, Any]:
    """How many pilot sources for this institution lack a stored body."""
    rows = connection.execute(
        text(
            """
            WITH latest_snapshot AS (
                SELECT DISTINCT ON (sn.source_id) sn.source_id, sn.id AS snapshot_id
                  FROM snapshot sn ORDER BY sn.source_id, sn.observed_at DESC
            ),
            current_extraction AS (
                SELECT DISTINCT ON (e.snapshot_id) e.snapshot_id, e.document_hash
                  FROM extraction e
                 WHERE e.document_hash IS NOT NULL
                 ORDER BY e.snapshot_id, e.recorded_at DESC
            )
            SELECT count(*) AS pilot_rows,
                   count(*) FILTER (
                     WHERE pcs.acquisition_source_id IS NULL
                        OR ls.snapshot_id IS NULL
                        OR ce.document_hash IS NULL
                   ) AS missing_body
              FROM pilot_collected_source pcs
              LEFT JOIN latest_snapshot ls ON ls.source_id = pcs.acquisition_source_id
              LEFT JOIN current_extraction ce ON ce.snapshot_id = ls.snapshot_id
             WHERE pcs.duplicate_of_source_ref IS NULL
               AND (cast(:institution AS uuid) IS NULL
                    OR pcs.target_institution_id = :institution)
            """
        ),
        {"institution": institution_id},
    ).one()
    return {
        "pilot_physical_rows": int(rows.pilot_rows),
        "missing_body": int(rows.missing_body),
    }


def fill_evidence(
    *,
    institution: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    acknowledge: bool = False,
    registered_by: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Register, enqueue, fetch, and extract for one institution (or a page sample)."""
    if max_pages > HARD_MAX_PAGES and not acknowledge:
        raise RuntimeError(
            f"max_pages {max_pages} exceeds {HARD_MAX_PAGES}; pass acknowledge=True"
        )
    if max_pages < 1:
        raise RuntimeError("max_pages must be >= 1")

    api = api_engine()
    worker = worker_engine()
    settings = get_settings()
    steps: list[dict[str, Any]] = []
    try:
        with api.connect() as connection:
            institution_id = (
                resolve_institution_id(connection, institution) if institution else None
            )
            if institution and institution_id is None:
                raise RuntimeError(f"no target_institution matching {institution!r}")
            before = evidence_gap(connection, institution_id=institution_id)
            name = None
            if institution_id is not None:
                name = connection.execute(
                    text("SELECT match_key FROM target_institution WHERE id = :i"),
                    {"i": institution_id},
                ).scalar_one()

        with api.begin() as connection:
            registered = register_acquisition_targets(
                connection,
                registered_by=registered_by,
            )
        steps.append(
            {
                "step": "register",
                "sources_created": registered.sources_created,
                "sources_existing": registered.sources_existing,
                "claims_linked": registered.claims_linked,
                "fetchable": registered.fetchable,
            }
        )

        cycle = cycle_key_for()
        with worker.begin() as connection:
            queued = enqueue_cycle(
                connection,
                cycle_key=cycle,
                target_institution_id=institution_id,
                pilot_only=True,
                limit=max_pages if institution_id is None else None,
            )
        steps.append(
            {
                "step": "enqueue",
                "cycle_key": queued.cycle_key,
                "attempts_created": queued.attempts_created,
                "already_queued": queued.already_queued,
                "blocked_or_disabled": queued.blocked_or_disabled,
                "physical_pages_selected": queued.physical_pages_selected,
            }
        )

        # Prefer the cycle we just wrote; fall back to any open work.
        with worker.connect() as connection:
            open_cycle = oldest_open_cycle(connection) or cycle

        store = build_evidence_store(settings, local_root=".evidence")
        fetch_report = asyncio.run(
            run_cycle(
                worker,
                cycle_key=open_cycle,
                store=store,
                worker=f"api-online:{uuid.uuid4().hex[:8]}",
                max_pages=max_pages,
                global_concurrency=2,
            )
        )
        steps.append(
            {
                "step": "fetch",
                "cycle_key": fetch_report.cycle_key,
                "attempted": fetch_report.attempted,
                "ok": fetch_report.ok,
                "unchanged": fetch_report.unchanged,
                "snapshots": fetch_report.snapshots,
                "blobs_created": fetch_report.blobs_created,
                "http_error": fetch_report.http_error,
                "urls_contacted": list(fetch_report.urls_contacted)[:max_pages],
            }
        )

        artifacts = build_evidence_store(
            settings, prefix=DERIVED_PREFIX, local_root=".artifacts"
        )
        extract_report = run_extraction(
            worker, evidence=store, artifacts=artifacts, limit=max_pages * 2
        )
        steps.append(
            {
                "step": "extract",
                "succeeded": extract_report.succeeded,
                "partial": extract_report.partial,
                "failed": extract_report.failed,
                "summary": extract_report.summary(),
                "failures": list(extract_report.failures)[:10],
            }
        )

        with api.connect() as connection:
            after = evidence_gap(connection, institution_id=institution_id)

        return {
            "status": "ok",
            "institution": name,
            "institution_id": str(institution_id) if institution_id else None,
            "max_pages": max_pages,
            "before": before,
            "after": after,
            "steps": steps,
            "message": (
                "Page bodies were fetched and normalised on this online backend. "
                "Reload the evidence panel; BODY_EVIDENCE_NOT_AVAILABLE clears once "
                "that URL has a snapshot and extraction."
            ),
        }
    finally:
        api.dispose()
        worker.dispose()


__all__ = [
    "DEFAULT_MAX_PAGES",
    "HARD_MAX_PAGES",
    "evidence_gap",
    "fill_evidence",
    "resolve_institution_id",
]
