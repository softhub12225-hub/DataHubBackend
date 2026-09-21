"""Running document extraction over stored evidence (Step 5C.1 sections 3-4, 15-17).

THE TRUST BOUNDARY, UNCHANGED
=============================
Extraction reads evidence and writes a derived document. It writes **nothing** to
`publication_eligibility`, `official_domain`, `source_mapping` or any pilot
verification state, and it creates no `field_claim`. A document parsed perfectly from a
`NOT_ELIGIBLE` source is a perfectly parsed document from a source nobody may publish
(C27). Successful parsing earns nothing.

WHAT IS EXTRACTED, AND WHAT IS NOT
==================================
Only body-bearing evidence. A `BLOCKED`, `404`, TLS-failed, timed-out or unresolved
page has no stored bytes, so there is nothing to parse and no extraction row is
created -- an empty extraction would assert we looked at a document that does not
exist. A `304` resolves through `latest_effective_evidence` to the snapshot that
actually carried bytes, so an unchanged page extracts from the body it last served
rather than manufacturing one.

STORAGE BOUNDARY
================
PostgreSQL holds the metadata: status, extractor identity, warnings, the input hash and
the artifact's hash, key and size. The object store holds the payload, under `derived/`
so it can never collide with raw evidence under `evidence/`.

**Object first, then the transaction**, exactly as the acquisition plane does (D36). An
orphaned artifact is wasted bytes; a row pointing at an artifact that was never written
is a document we claim to have and cannot produce.

IDEMPOTENCY
===========
One result per (snapshot, extractor, version), enforced by a unique constraint. A
second run over the same snapshot at the same version does nothing: the result is a pure
function of its inputs, so another row could only be a duplicate. There is no separate
"run record" -- an extraction *is* the result, and re-deriving it is not an event worth
storing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, text

from app.core.logging import get_logger
from app.db.enums import ExtractionStatus
from app.domains.acquisition.storage import EvidenceStore, storage_key_for
from app.domains.extraction.document import (
    EXTRACTOR_VERSION,
    HTML_EXTRACTOR,
    PDF_EXTRACTOR,
    NormalizedDocument,
)
from app.domains.extraction.html_document import parse_html_document
from app.domains.extraction.pdf_document import PdfExtractionError, parse_pdf_document

logger = get_logger(__name__)

#: Derived artifacts live under their own prefix. Raw evidence is never overwritten and
#: cannot be: a derived document and a raw body would have to collide on sha256 *and*
#: share a prefix for that to be possible, and they do not share a prefix.
DERIVED_PREFIX = "derived"

#: The normalisers whose artifact version counts as live. Both share one version
#: constant today; the mapping is per-name so that rebuilding the PDF parse need not
#: supersede every HTML parse.
LIVE_DOCUMENT_VERSIONS: tuple[tuple[str, str], ...] = (
    (HTML_EXTRACTOR, EXTRACTOR_VERSION),
    (PDF_EXTRACTOR, EXTRACTOR_VERSION),
)


def publish_document_versions(connection: Connection) -> None:
    """Tell SQL which parse of a page is live, from this module's own constants.

    `candidate_review_state` has to tell a candidate read from the current document
    from one read from a superseded parse, and it cannot work that out for itself.
    Freezing the version into the view was tried for the rule versions and was wrong
    within four corrections, silently -- so the code publishes what it knows instead.

    Idempotent, and a no-op when nothing changed, so any pass that depends on the
    answer being true may call it.
    """
    for name, version in LIVE_DOCUMENT_VERSIONS:
        connection.execute(
            text(
                "INSERT INTO document_artifact_version (extractor_name, version) "
                "VALUES (:name, :version) "
                "ON CONFLICT (extractor_name) DO UPDATE "
                "  SET version = EXCLUDED.version, updated_at = now() "
                " WHERE document_artifact_version.version <> EXCLUDED.version"
            ),
            {"name": name, "version": version},
        )


@dataclass(slots=True)
class ExtractionReport:
    """What one extraction pass did."""

    attempted: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    skipped_existing: int = 0
    skipped_no_evidence: int = 0
    artifacts_written: int = 0
    artifacts_deduplicated: int = 0
    bytes_written: int = 0
    by_media: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.attempted} document(s) attempted -- {self.succeeded} succeeded, "
            f"{self.partial} partial, {self.failed} failed; "
            f"{self.skipped_existing} already extracted at this version, "
            f"{self.skipped_no_evidence} had no body-bearing evidence; "
            f"{self.artifacts_written} artifact(s) written "
            f"({self.artifacts_deduplicated} deduplicated), "
            f"{self.bytes_written:,} bytes"
        )


@dataclass(frozen=True, slots=True)
class ExtractionTarget:
    """One snapshot worth parsing, with the lineage needed to record it."""

    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    content_hash: str
    content_type: str | None
    effective_url: str | None
    url: str


def targets_for_extraction(connection: Connection) -> list[ExtractionTarget]:
    """Every source whose latest effective evidence carries a body.

    Built on the same resolution `latest_effective_evidence` performs -- the newest
    body-bearing snapshot per source -- so a `304` extracts the bytes it confirmed
    rather than nothing. Sources with no snapshot at all are simply absent.
    """
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (sn.source_id)
                   sn.source_id, sn.id AS snapshot_id, sn.content_hash,
                   sn.content_type, sn.effective_url, s.url
              FROM snapshot sn
              JOIN source s ON s.id = sn.source_id
              JOIN content_blob b ON b.content_hash = sn.content_hash
             ORDER BY sn.source_id, sn.observed_at DESC, sn.id DESC
            """
        )
    ).all()
    return [
        ExtractionTarget(
            source_id=row.source_id,
            snapshot_id=row.snapshot_id,
            content_hash=row.content_hash,
            content_type=row.content_type,
            effective_url=row.effective_url or row.url,
            url=row.url,
        )
        for row in rows
    ]


def extractor_for(content_type: str | None, payload: bytes) -> str:
    """Which extractor a document gets.

    The magic bytes win over the declared type: a server that labels a PDF
    `text/html` has mislabelled it, and the bytes are the evidence.
    """
    if payload[:5] == b"%PDF-":
        return PDF_EXTRACTOR
    base = (content_type or "").split(";")[0].strip().lower()
    if base == "application/pdf":
        return PDF_EXTRACTOR
    return HTML_EXTRACTOR


def already_extracted(
    connection: Connection, *, snapshot_id: uuid.UUID, extractor: str, version: str
) -> bool:
    """Is there already a *result* for this (snapshot, extractor, version)?

    `FAILED` rows are excluded, and the exclusion is the point (Step 5C.2 section 0).
    A failure is not a result: it records that the environment or the bytes defeated us
    once, and the retry that follows is not a claim the extraction logic changed.

    Deliberately the same predicate as `uq_extraction_result_per_version`. If this
    counted failures and the index did not, a retry would be skipped here and the slot
    would sit empty in the database -- a page that could be extracted, permanently
    reported as done.
    """
    return bool(
        connection.execute(
            text(
                "SELECT 1 FROM extraction WHERE snapshot_id = :s "
                " AND extractor_name = :n AND extractor_version = :v "
                " AND status <> 'FAILED'"
            ),
            {"s": snapshot_id, "n": extractor, "v": version},
        ).first()
    )


def extract_one(
    engine: Engine,
    target: ExtractionTarget,
    *,
    evidence: EvidenceStore,
    artifacts: EvidenceStore,
    report: ExtractionReport,
    version: str = EXTRACTOR_VERSION,
) -> uuid.UUID | None:
    """Parse one snapshot and record the result. Returns the extraction id, or None.

    Each document is its own transaction: a page that fails to parse must not cost the
    169 after it, which is the same lesson the acquisition cycle learned in 5B.2.
    """
    with engine.begin() as connection:
        payload = evidence.get(target.content_hash)
        extractor = extractor_for(target.content_type, payload)
        if already_extracted(
            connection, snapshot_id=target.snapshot_id, extractor=extractor, version=version
        ):
            report.skipped_existing += 1
            return None

    report.attempted += 1
    document: NormalizedDocument | None = None
    error: str | None = None

    try:
        if extractor == PDF_EXTRACTOR:
            document = parse_pdf_document(payload)
        else:
            document = parse_html_document(
                payload,
                content_type=target.content_type,
                effective_url=target.effective_url,
            )
    except PdfExtractionError as exc:
        error = f"PdfExtractionError: {exc}"
    except Exception as exc:
        # One bad document must not end the pass. The same lesson as 5B.2's
        # per-attempt containment: 169 pages should not be lost to the 170th.
        error = f"{type(exc).__name__}: {exc}"

    if document is None:
        report.failed += 1
        report.failures.append(f"{target.url}: {error}")
        logger.warning("extraction_failed", source_id=str(target.source_id), detail=error)
        with engine.begin() as connection:
            return _record(
                connection,
                target=target,
                extractor=extractor,
                version=version,
                status=ExtractionStatus.FAILED,
                document=None,
                stored=None,
                error=error,
            )

    # `ExtractionStatus.OK` is this schema's name for "succeeded"; section 17 asked
    # for SUCCEEDED/PARTIAL/FAILED and the existing enum already draws exactly that
    # distinction, so it is reused rather than grown a synonym.
    status = ExtractionStatus.PARTIAL if document.warnings else ExtractionStatus.OK
    if status is ExtractionStatus.PARTIAL:
        report.partial += 1
    else:
        report.succeeded += 1
    report.by_media[document.media_type] = report.by_media.get(document.media_type, 0) + 1

    # Object first (D36). An orphan is recoverable; a dangling reference is not.
    payload_bytes = document.canonical_bytes()
    existed = artifacts.exists(document.document_hash())
    stored = artifacts.put(payload_bytes, content_type="application/json")
    if existed:
        report.artifacts_deduplicated += 1
    else:
        report.artifacts_written += 1
        report.bytes_written += len(payload_bytes)

    with engine.begin() as connection:
        return _record(
            connection,
            target=target,
            extractor=extractor,
            version=version,
            status=status,
            document=document,
            stored=(stored.content_hash, stored.storage_key, len(payload_bytes)),
            error=None,
        )


def _record(
    connection: Connection,
    *,
    target: ExtractionTarget,
    extractor: str,
    version: str,
    status: ExtractionStatus,
    document: NormalizedDocument | None,
    stored: tuple[str, str, int] | None,
    error: str | None,
) -> uuid.UUID:
    """Write the extraction row. `output` carries a small summary, never the payload."""
    extraction_id = uuid.uuid4()
    summary: dict[str, object] | None = None
    warnings: dict[str, object] | None = None
    if document is not None:
        summary = {
            "schema": document.schema,
            "media_type": document.media_type,
            "title": document.title,
            "language": document.language,
            "canonical_url": document.canonical_url,
            "statistics": document.to_json().get("statistics", {}),
            "json_ld_types": document.structured_data.json_ld_types,
            "encoding": document.encoding,
        }
        if document.warnings:
            warnings = {"warnings": document.warnings}

    connection.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
            "  status, output, error_detail, warnings, input_content_hash, "
            "  document_hash, document_storage_key, document_byte_size) "
            "VALUES (:id, :snapshot, :name, :version, :status, "
            "  cast(:output AS jsonb), :error, cast(:warnings AS jsonb), :input_hash, "
            "  :doc_hash, :doc_key, :doc_size)"
        ),
        {
            "id": extraction_id,
            "snapshot": target.snapshot_id,
            "name": extractor,
            "version": version,
            "status": status.value,
            "output": _json(summary),
            "error": error,
            "warnings": _json(warnings),
            "input_hash": target.content_hash,
            "doc_hash": stored[0] if stored else None,
            "doc_key": stored[1] if stored else None,
            "doc_size": stored[2] if stored else None,
        },
    )
    return extraction_id


def _json(value: dict[str, object] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def run_extraction(
    engine: Engine,
    *,
    evidence: EvidenceStore,
    artifacts: EvidenceStore,
    version: str = EXTRACTOR_VERSION,
    limit: int | None = None,
) -> ExtractionReport:
    """Extract every source whose latest evidence carries a body."""
    report = ExtractionReport()
    with engine.connect() as connection:
        targets = targets_for_extraction(connection)
    if limit is not None:
        targets = targets[:limit]

    for target in targets:
        try:
            extract_one(
                engine,
                target,
                evidence=evidence,
                artifacts=artifacts,
                report=report,
                version=version,
            )
        except KeyError:
            # The blob is referenced but missing from the store. Not an extraction
            # failure -- there is nothing to extract -- and the integrity report is
            # where a missing object belongs.
            report.skipped_no_evidence += 1
            report.failures.append(f"{target.url}: stored object missing")
            logger.error(
                "extraction_object_missing",
                source_id=str(target.source_id),
                content_hash=target.content_hash,
            )

    logger.info(
        "extraction_pass_complete",
        attempted=report.attempted,
        succeeded=report.succeeded,
        partial=report.partial,
        failed=report.failed,
    )
    return report


def derived_key_for(document_hash: str) -> str:
    """Where a derived artifact lives, namespaced away from raw evidence."""
    return storage_key_for(document_hash, prefix=DERIVED_PREFIX)


__all__ = [
    "DERIVED_PREFIX",
    "LIVE_DOCUMENT_VERSIONS",
    "ExtractionReport",
    "ExtractionTarget",
    "already_extracted",
    "derived_key_for",
    "extract_one",
    "extractor_for",
    "publish_document_versions",
    "run_extraction",
    "targets_for_extraction",
]
