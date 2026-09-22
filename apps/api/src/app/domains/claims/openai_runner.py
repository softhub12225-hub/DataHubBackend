"""Run OpenAI extraction over all stored documents (pilot fleet)."""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

from app.core.logging import get_logger
from app.domains.acquisition.storage import EvidenceStore
from app.domains.claims import openai_extract
from app.domains.claims.model import extractors_for
from app.domains.claims.runner import (
    ClaimReport,
    _insert,
    load_document,
    publish_rule_versions,
    targets_for_claims,
)
from app.domains.extraction.runner import publish_document_versions

logger = get_logger(__name__)


def _publish_openai_rule_version(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO claim_rule_version (extractor_name, version) "
            "VALUES (:name, :version) "
            "ON CONFLICT (extractor_name) DO UPDATE "
            "  SET version = EXCLUDED.version, updated_at = now() "
            " WHERE claim_rule_version.version <> EXCLUDED.version"
        ),
        {"name": openai_extract.EXTRACTOR, "version": openai_extract.VERSION},
    )


def run_openai_claims(
    engine: Engine,
    *,
    artifacts: EvidenceStore,
    limit: int | None = None,
    institution: str | None = None,
) -> ClaimReport:
    """Extract candidates via OpenAI for every authorised pilot document."""
    report = ClaimReport()
    with engine.begin() as connection:
        publish_rule_versions(connection)
        _publish_openai_rule_version(connection)
        publish_document_versions(connection)

    with engine.connect() as connection:
        targets = targets_for_claims(connection)

    if institution:
        needle = institution.casefold()
        targets = [t for t in targets if needle in t.institution.casefold()]

    if limit is not None:
        targets = targets[:limit]

    for target in targets:
        report.documents_considered += 1
        responsibilities = {r for _, r in target.responsibilities}
        if not any(extractors_for({r}) for r in responsibilities):
            report.documents_not_authorised += 1
            continue
        report.documents_eligible += 1

        try:
            document = load_document(artifacts, target.document_hash)
        except (KeyError, OSError, ValueError) as exc:
            report.failures.append(f"{target.url}: artifact unreadable ({exc})")
            continue

        produced_any = False
        with engine.begin() as connection:
            for claim_id, responsibility in target.responsibilities:
                if not extractors_for({responsibility}):
                    continue
                candidates = openai_extract.extract(document, responsibility=responsibility)
                if not candidates:
                    continue
                produced_any = True
                for candidate in candidates:
                    created = _insert(
                        connection,
                        target=target,
                        candidate=candidate,
                        pilot_claim_id=claim_id,
                        responsibility=responsibility,
                        extractor=openai_extract.EXTRACTOR,
                        version=openai_extract.VERSION,
                    )
                    if created:
                        report.claims_created += 1
                        report.by_field_kind[candidate.field_kind.value] = (
                            report.by_field_kind.get(candidate.field_kind.value, 0) + 1
                        )
                        report.by_confidence[candidate.confidence.value] = (
                            report.by_confidence.get(candidate.confidence.value, 0) + 1
                        )
                        report.by_responsibility[responsibility] = (
                            report.by_responsibility.get(responsibility, 0) + 1
                        )
                    else:
                        report.claims_already_present += 1

        if produced_any:
            report.documents_with_claims += 1
        else:
            report.documents_without_claims += 1

    logger.info(
        "openai_claim_pass_complete",
        created=report.claims_created,
        existing=report.claims_already_present,
        documents=report.documents_eligible,
    )
    return report


__all__ = ["run_openai_claims"]
