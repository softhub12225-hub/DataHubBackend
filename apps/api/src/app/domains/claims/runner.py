"""Running the field extractors over stored documents (Step 5C.2 sections 4-5, 23, 30).

OFFLINE, AND EVIDENCE-LOCAL
===========================
Reads normalised documents from the artifact store. No network request, no LLM, no
cross-page inference: an extractor sees one document and the responsibilities that page
was claimed for, and nothing else. A fee on page A and the word "international" on
page B do not combine into anything (section 28).

WHAT AUTHORISES AN EXTRACTOR
============================
The workbook responsibilities on the physical page. A page claimed only as
`UNIVERSITY_HOME` runs nothing; one claimed for postgraduate admissions and deadlines
runs both sets. The authorising claim is stored on every candidate, so "why did a
tuition claim come off this page" is answerable from the row rather than from the code.

INSUFFICIENT STATIC CONTENT
===========================
A document with almost no visible text is not a failure and not an empty result -- it
is a coverage finding (section 23). It produces no business claim, and the pass records
`INSUFFICIENT_STATIC_CONTENT` so the five thin pages stay visible instead of looking
like pages where the rules simply found nothing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, text

from app.core.logging import get_logger
from app.domains.acquisition.storage import EvidenceStore
from app.domains.claims import admission, deadline, language, tuition
from app.domains.claims.model import Candidate, extractors_for
from app.domains.extraction.document import EXTRACTOR_VERSION, NormalizedDocument
from app.domains.extraction.runner import publish_document_versions

#: Document artifact versions whose candidates count as current. A list rather than a
#: scalar because a rebuild that supersedes one normaliser need not supersede the other,
#: and because it travels as a bound array parameter.
CURRENT_DOCUMENT_VERSIONS: list[str] = [EXTRACTOR_VERSION]

logger = get_logger(__name__)

#: Below this much visible text, static evidence is insufficient to extract anything
#: from. The same threshold Step 5C.1 used for "usable visible text", so the two
#: reports describe the same boundary.
MIN_USABLE_TEXT = 400

#: extractor key -> (callable, name, version)
EXTRACTORS: dict[str, tuple[object, str, str]] = {
    "tuition": (tuition.extract, tuition.EXTRACTOR, tuition.VERSION),
    "language": (language.extract, language.EXTRACTOR, language.VERSION),
    "deadline": (deadline.extract, deadline.EXTRACTOR, deadline.VERSION),
    "admission": (admission.extract, admission.EXTRACTOR, admission.VERSION),
    "program": (
        admission.extract_program,
        admission.PROGRAM_EXTRACTOR,
        admission.PROGRAM_VERSION,
    ),
    "calendar": (
        deadline.extract_calendar,
        deadline.CALENDAR_EXTRACTOR,
        deadline.CALENDAR_VERSION,
    ),
}


@dataclass(slots=True)
class ClaimReport:
    """What one claim pass did."""

    documents_considered: int = 0
    documents_eligible: int = 0
    documents_with_claims: int = 0
    documents_without_claims: int = 0
    documents_insufficient_text: int = 0
    documents_not_authorised: int = 0
    claims_created: int = 0
    claims_already_present: int = 0
    by_field_kind: dict[str, int] = field(default_factory=dict)
    by_confidence: dict[str, int] = field(default_factory=dict)
    by_responsibility: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.documents_considered} document(s) considered, "
            f"{self.documents_eligible} authorised for at least one extractor; "
            f"{self.documents_with_claims} produced claims, "
            f"{self.documents_without_claims} produced none, "
            f"{self.documents_insufficient_text} had insufficient static text, "
            f"{self.documents_not_authorised} were authorised for nothing; "
            f"{self.claims_created} claim(s) created, "
            f"{self.claims_already_present} already present"
        )


@dataclass(frozen=True, slots=True)
class ClaimTarget:
    """One extracted document, with the responsibilities that authorise extractors."""

    extraction_id: uuid.UUID
    document_hash: str
    source_id: uuid.UUID
    url: str
    institution: str
    #: (pilot_collected_source_id, responsibility) for every claim on this page.
    responsibilities: tuple[tuple[uuid.UUID, str], ...]


def targets_for_claims(connection: Connection) -> list[ClaimTarget]:
    """Every **currently extracted** document and what it was claimed for.

    `FAILED` extractions are excluded: there is no document to read. `PARTIAL` are
    included -- a charset fallback or a malformed JSON-LD block does not stop the text
    being usable, which is the whole point of the distinction.

    Superseded document artifacts are excluded too, and that predicate is load-bearing.
    Re-extraction keeps the old artifact (Step 5C.4 section 1), so after the document
    normaliser went to 2.0.0 there were two extractions per snapshot. Without this the
    pass would run every rule over both and create two candidates for one fact --
    indistinguishable in the reports, and impossible to collapse by fingerprint, because
    `extraction_id` is part of it.
    """
    rows = connection.execute(
        text(
            """
            SELECT e.id AS extraction_id, e.document_hash,
                   s.id AS source_id, s.url,
                   coalesce(ti.match_key, '?') AS institution,
                   pcs.id AS claim_id, pcs.source_type AS responsibility
              FROM extraction e
              JOIN snapshot sn ON sn.id = e.snapshot_id
              JOIN source s ON s.id = sn.source_id
              JOIN pilot_collected_source pcs ON pcs.acquisition_source_id = s.id
              LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
             WHERE e.status <> 'FAILED' AND e.document_hash IS NOT NULL
               AND e.extractor_version = ANY(:document_versions)
             ORDER BY e.id, pcs.source_ref
            """
        ),
        {"document_versions": CURRENT_DOCUMENT_VERSIONS},
    ).all()

    grouped: dict[uuid.UUID, dict[str, object]] = {}
    for row in rows:
        entry = grouped.setdefault(
            row.extraction_id,
            {
                "document_hash": row.document_hash,
                "source_id": row.source_id,
                "url": row.url,
                "institution": row.institution,
                "responsibilities": [],
            },
        )
        responsibilities = entry["responsibilities"]
        assert isinstance(responsibilities, list)
        responsibilities.append((row.claim_id, row.responsibility))

    return [
        ClaimTarget(
            extraction_id=extraction_id,
            document_hash=str(entry["document_hash"]),
            source_id=entry["source_id"],  # type: ignore[arg-type]
            url=str(entry["url"]),
            institution=str(entry["institution"]),
            responsibilities=tuple(entry["responsibilities"]),  # type: ignore[arg-type]
        )
        for extraction_id, entry in grouped.items()
    ]


def load_document(artifacts: EvidenceStore, document_hash: str) -> NormalizedDocument:
    """Rehydrate a stored artifact into the dataclasses the extractors expect."""
    from app.domains.extraction.document import (
        Block,
        BlockKind,
        DocumentStatistics,
        Embed,
        Link,
        LinkProfile,
        StructuredData,
        Table,
        TableCell,
    )

    payload = json.loads(artifacts.get(document_hash))

    blocks = [
        Block(
            kind=BlockKind(item["kind"]),
            text=item.get("text", ""),
            level=item.get("level"),
            items=list(item.get("items", [])),
            ordered=item.get("ordered"),
            table_index=item.get("table_index"),
            container=item.get("container"),
            # Absent on a v1 artifact and on any block with no anchor, which is what
            # `None` means here -- see `LinkProfile`.
            link_profile=(
                LinkProfile(**item["link_profile"]) if item.get("link_profile") else None
            ),
        )
        for item in payload.get("blocks", [])
    ]
    tables = [
        Table(
            caption=item.get("caption"),
            header_rows=[
                [
                    TableCell(
                        text=cell.get("text", ""),
                        header=cell.get("header", False),
                        rowspan=cell.get("rowspan", 1),
                        colspan=cell.get("colspan", 1),
                    )
                    for cell in row
                ]
                for row in item.get("header_rows", [])
            ],
            rows=[
                [
                    TableCell(
                        text=cell.get("text", ""),
                        header=cell.get("header", False),
                        rowspan=cell.get("rowspan", 1),
                        colspan=cell.get("colspan", 1),
                    )
                    for cell in row
                ]
                for row in item.get("rows", [])
            ],
        )
        for item in payload.get("tables", [])
    ]
    links = [
        Link(
            text=item.get("text", ""),
            href=item.get("href", ""),
            resolved=item.get("resolved"),
            rel=item.get("rel"),
            is_pdf=item.get("is_pdf", False),
            host=item.get("host"),
            # Structural provenance, absent on a v1 artifact. Defaulting rather than
            # failing is deliberate: an old artifact is still readable, and a consumer
            # that needs the provenance can tell it is missing because `container` is
            # None and `document.schema` says `/1`.
            container=item.get("container"),
            block_index=item.get("block_index"),
            ancestry=item.get("ancestry"),
            is_link_only_block=item.get("is_link_only_block", False),
            is_link_only_item=item.get("is_link_only_item", False),
        )
        for item in payload.get("links", [])
    ]
    structured = payload.get("structured_data", {})
    statistics = payload.get("statistics", {})

    return NormalizedDocument(
        schema=payload.get("schema", ""),
        extractor_name=payload.get("extractor_name", ""),
        extractor_version=payload.get("extractor_version", ""),
        media_type=payload.get("media_type", ""),
        title=payload.get("title"),
        language=payload.get("language"),
        canonical_url=payload.get("canonical_url"),
        meta_description=payload.get("meta_description"),
        metadata=payload.get("metadata", {}),
        blocks=blocks,
        tables=tables,
        links=links,
        embeds=[
            Embed(tag=item.get("tag", ""), src=item.get("src"), title=item.get("title"))
            for item in payload.get("embeds", [])
        ],
        structured_data=StructuredData(
            json_ld=list(structured.get("json_ld", [])),
            json_ld_types=list(structured.get("json_ld_types", [])),
            embedded_json=list(structured.get("embedded_json", [])),
            unparsed_payloads=list(structured.get("unparsed_payloads", [])),
        ),
        encoding=payload.get("encoding", {}),
        statistics=DocumentStatistics(**statistics) if statistics else DocumentStatistics(),
        warnings=list(payload.get("warnings", [])),
    )


def claims_for_document(
    document: NormalizedDocument, target: ClaimTarget
) -> list[tuple[Candidate, uuid.UUID, str, str, str]]:
    """Candidates plus the responsibility and extractor that produced each.

    One extractor may be authorised by several responsibilities on the same page. It
    runs **once**, attributed to the first responsibility that authorised it, because
    running it twice would create two identical claims for one piece of evidence.
    """
    authorising: dict[str, tuple[uuid.UUID, str]] = {}
    for claim_id, responsibility in target.responsibilities:
        for key in extractors_for({responsibility}):
            authorising.setdefault(key, (claim_id, responsibility))

    out: list[tuple[Candidate, uuid.UUID, str, str, str]] = []
    for key, (claim_id, responsibility) in sorted(authorising.items()):
        function, name, version = EXTRACTORS[key]
        candidates = function(document, responsibility=responsibility)  # type: ignore[operator]
        for candidate in candidates:
            out.append((candidate, claim_id, responsibility, name, version))
    return out


def publish_rule_versions(connection: Connection) -> None:
    """Tell SQL which rule version is live, from this registry.

    `candidate_review_state` needs to tell a current candidate from a superseded one,
    and it cannot work that out for itself. The first attempt froze the pairs into the
    view; four rule corrections later the view reported every current candidate as
    superseded, silently, with a total that happened to look right. So the code
    publishes what it knows instead, on every pass.
    """
    for _, name, version in EXTRACTORS.values():
        connection.execute(
            text(
                "INSERT INTO claim_rule_version (extractor_name, version) "
                "VALUES (:name, :version) "
                "ON CONFLICT (extractor_name) DO UPDATE "
                "  SET version = EXCLUDED.version, updated_at = now() "
                " WHERE claim_rule_version.version <> EXCLUDED.version"
            ),
            {"name": name, "version": version},
        )


def run_claims(
    engine: Engine, *, artifacts: EvidenceStore, limit: int | None = None
) -> ClaimReport:
    """Extract candidate claims from every stored document. Creates no canonical row."""
    report = ClaimReport()
    with engine.begin() as connection:
        publish_rule_versions(connection)
        # The other axis of "current". This pass is about to read only the live
        # document artifact, so the view must agree about which one that is before any
        # report counts a candidate as superseded or live.
        publish_document_versions(connection)
    with engine.connect() as connection:
        targets = targets_for_claims(connection)
    if limit is not None:
        targets = targets[:limit]

    for target in targets:
        report.documents_considered += 1
        responsibilities = {responsibility for _, responsibility in target.responsibilities}
        allowed = extractors_for(responsibilities)
        if not allowed:
            report.documents_not_authorised += 1
            continue
        report.documents_eligible += 1

        try:
            document = load_document(artifacts, target.document_hash)
        except (KeyError, OSError, ValueError) as exc:
            report.failures.append(f"{target.url}: artifact unreadable ({exc})")
            continue

        if document.statistics.text_characters < MIN_USABLE_TEXT:
            report.documents_insufficient_text += 1
            logger.info(
                "claims_insufficient_static_content",
                source_id=str(target.source_id),
                characters=document.statistics.text_characters,
            )
            continue

        produced = claims_for_document(document, target)
        if not produced:
            report.documents_without_claims += 1
            continue
        report.documents_with_claims += 1

        with engine.begin() as connection:
            for candidate, claim_id, responsibility, name, version in produced:
                created = _insert(
                    connection,
                    target=target,
                    candidate=candidate,
                    pilot_claim_id=claim_id,
                    responsibility=responsibility,
                    extractor=name,
                    version=version,
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

    logger.info(
        "claim_pass_complete",
        created=report.claims_created,
        existing=report.claims_already_present,
        documents=report.documents_eligible,
    )
    return report


def _insert(
    connection: Connection,
    *,
    target: ClaimTarget,
    candidate: Candidate,
    pilot_claim_id: uuid.UUID,
    responsibility: str,
    extractor: str,
    version: str,
) -> bool:
    """Insert one candidate, or do nothing if its fingerprint already exists.

    `ON CONFLICT DO NOTHING` rather than a read-then-write: the fingerprint is the
    identity, and letting the database decide keeps a concurrent second pass from
    inserting a duplicate between the check and the write.
    """
    fingerprint = candidate.fingerprint(
        extraction_id=str(target.extraction_id), extractor=extractor, version=version
    )
    result = connection.execute(
        text(
            "INSERT INTO field_claim_candidate ("
            "  id, extraction_id, pilot_collected_source_id, source_responsibility, "
            "  field_kind, value_normalized, unresolved_reason, value_raw_text, "
            "  evidence_text, locator, extractor_name, extractor_version, "
            "  confidence_band, confidence_reason, claim_fingerprint) "
            "VALUES (:id, :extraction, :pilot_claim, :responsibility, :kind, "
            "  cast(:value AS jsonb), :unresolved, :raw, :evidence, "
            "  cast(:locator AS jsonb), :extractor, :version, :band, :reason, :fingerprint) "
            "ON CONFLICT (claim_fingerprint) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "extraction": target.extraction_id,
            "pilot_claim": pilot_claim_id,
            "responsibility": responsibility,
            "kind": candidate.field_kind.value,
            "value": _json(_with_context(candidate)),
            "unresolved": candidate.unresolved_reason,
            "raw": candidate.value_raw_text[:2000] or "(no wording)",
            "evidence": candidate.evidence_text[:4000],
            "locator": _json(candidate.locator.as_json()),
            "extractor": extractor,
            "version": version,
            "band": candidate.confidence.value,
            "reason": candidate.confidence_reason,
            "fingerprint": fingerprint,
        },
    )
    return bool(result.rowcount)


def _with_context(candidate: Candidate) -> dict[str, object] | None:
    """The value plus the document-local context the rule used.

    Kept together so a reviewer sees the same thing the rule saw -- the column label,
    the heading, the scope phrase -- without a second query.
    """
    if candidate.value is None:
        return None
    payload = dict(candidate.value)
    if candidate.context:
        payload["_context"] = dict(candidate.context)
    return payload


def _json(value: dict[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


__all__ = [
    "CURRENT_DOCUMENT_VERSIONS",
    "EXTRACTORS",
    "MIN_USABLE_TEXT",
    "ClaimReport",
    "ClaimTarget",
    "claims_for_document",
    "load_document",
    "publish_rule_versions",
    "run_claims",
    "targets_for_claims",
]
