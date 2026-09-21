"""Registering the pilot's physical pages as acquisition targets (Step 5B sections 1-4).

WHAT REGISTRATION MEANS HERE, AND WHAT IT DOES NOT
==================================================
Creating a `source` row means "this URL is a known acquisition target". It is **not** a
trust signal, and reading it as one is the mistake this step exists to correct. Every
source created here gets:

* `publication_eligibility = NOT_ELIGIBLE` -- the C27 default, untouched. Nothing from
  this source may support a published fact until a human earns it the class, and the
  triggers on `field_claim` and `field_provenance` enforce that regardless of what any
  code here does.
* `fetch_eligibility = FETCHABLE`, when and only when the URL passes shape and scheme
  validation. That is a technical judgement a machine can make.

The two together are the normal state: we may look, and nothing we see may be published.

WHY NOT `source_mapping`
========================
`source_mapping` is the responsibility record -- *this page is the authority for
tuition at this institution* -- and `source_mapping_requires_trusted_host` rightly
refuses one until the host has a verified `official_domain`. Zero domains are verified,
so routing acquisition through mappings would mean fetching nothing until the review
pass finishes, which is backwards (see the migration header).

Lineage therefore runs through `pilot_collected_source.acquisition_source_id`, which
records where the URL came from without asserting anything about what it is. When
domains are verified later, mappings are created then, and they are what confers
eligibility.

ONE SOURCE PER PHYSICAL PAGE
============================
The 385 responsibility claims cover 319 distinct URLs. A `source` is created per
distinct URL, and **every** claim on that URL -- the physical row and its duplicates --
is pointed at it. So a page claimed by three categories is fetched once, and all three
claims remain reachable from the source in one query.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.db.enums import (
    UNCLASSIFIED_SOURCE_TYPE,
    FetchEligibility,
    PublicationEligibility,
    SourceCategory,
)
from app.domains.acquisition.netsafety import UnsafeTargetError, validate_stored_url_for_fetch

logger = get_logger(__name__)

#: `source_category` (what content we need) -> `source.source_type` (what kind of site).
#: Two vocabularies that predate each other; this is the translation, written out so a
#: reader can see it is a mapping rather than a coincidence.
#:
#: The value is derived from the **physical** claim -- the column that first named the
#: URL -- and is a coarse acquisition label. It is explicitly **not** a responsibility
#: claim: that a page was first listed under "Tuition/Fees URL" does not make it the
#: authority for fees, and `source_mapping` remains the only place that says so.
SOURCE_TYPE_BY_CATEGORY: dict[str, str] = {
    SourceCategory.UNIVERSITY_HOME.value: "university_site",
    SourceCategory.UNDERGRADUATE_ADMISSIONS.value: "admissions_page",
    SourceCategory.POSTGRADUATE_ADMISSIONS.value: "admissions_page",
    SourceCategory.PHD_ADMISSIONS.value: "admissions_page",
    SourceCategory.ENTRY_REQUIREMENTS.value: "admissions_page",
    SourceCategory.LANGUAGE_REQUIREMENTS.value: "admissions_page",
    SourceCategory.APPLICATION_DEADLINES.value: "admissions_page",
    SourceCategory.PROGRAM_CATALOG.value: "university_site",
    SourceCategory.PROGRAM_PAGE.value: "university_site",
    SourceCategory.ACADEMIC_CALENDAR.value: "university_site",
    SourceCategory.FACULTY_OR_SCHOOL.value: "faculty_site",
    SourceCategory.TUITION_FEES.value: "fee_page",
    SourceCategory.OFFICIAL_PDF.value: "official_pdf",
    SourceCategory.GOVERNMENT.value: "government_regulator",
    SourceCategory.AUTHORIZED_RANKING.value: "authorized_ranking",
    # A page nobody has categorised stays uncategorised here too (D32). Guessing from
    # the URL is the inference Step 5A refused, and it would be no better here.
    UNCLASSIFIED_SOURCE_TYPE: "unclassified",
}

#: How a page is fetched. `.pdf` gets the DOCUMENT strategy so a browser worker never
#: picks it up; everything else starts STATIC and is only promoted to BROWSER on
#: evidence that static HTTP is insufficient (section 21).
DEFAULT_CRAWL_FREQUENCY = "WEEKLY"


@dataclass(slots=True)
class RegistrationReport:
    """What registration created, in acquisition terms."""

    physical_pages: int = 0
    sources_created: int = 0
    sources_existing: int = 0
    claims_linked: int = 0
    fetchable: int = 0
    needs_manual_review: int = 0
    unclassified: int = 0
    document_strategy: int = 0
    hosts: int = 0
    refused: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.physical_pages} physical page(s) -> {self.sources_created} source(s) "
            f"created, {self.sources_existing} already present; {self.claims_linked} "
            f"responsibility claim(s) linked across {self.hosts} host(s). "
            f"{self.fetchable} FETCHABLE, {self.needs_manual_review} need review. "
            "All NOT_ELIGIBLE for publication."
        )


def register_acquisition_targets(
    connection: Connection,
    *,
    submission_id: uuid.UUID | None = None,
    pilot_only: bool = True,
    registered_by: uuid.UUID | None = None,
) -> RegistrationReport:
    """Create a `source` per distinct URL and link every claim to it.

    Idempotent: `source.url_hash` is unique, so re-running adopts the existing row
    rather than creating a second acquisition target for the same page.
    """
    report = RegistrationReport()

    rows = connection.execute(
        text(
            """
            SELECT p.id, p.submission_id, p.source_ref, p.target_institution_id,
                   p.normalized_url, p.official_url, p.url_sha256, p.host, p.source_type
              FROM pilot_collected_source p
              JOIN pilot_submission s ON s.id = p.submission_id
              JOIN target_institution ti ON ti.id = p.target_institution_id
             WHERE p.duplicate_of_source_ref IS NULL
               AND (cast(:submission AS uuid) IS NULL OR p.submission_id = :submission)
               AND (NOT :pilot_only OR ti.pilot_wave IS NOT NULL)
               AND s.import_status = 'VALIDATED'
             ORDER BY p.host, p.source_ref
            """
        ),
        {"submission": submission_id, "pilot_only": pilot_only},
    ).all()

    report.physical_pages = len(rows)
    report.hosts = len({row.host for row in rows})

    for row in rows:
        # Eligibility is computed, never asserted. A URL that cannot pass shape
        # validation is parked for a human rather than silently dropped -- it is in
        # the client's file, and they should hear about it.
        try:
            validate_stored_url_for_fetch(row.normalized_url)
            eligibility = FetchEligibility.FETCHABLE
            reason = "URL shape, scheme and port validated at registration"
        except UnsafeTargetError as exc:
            eligibility = FetchEligibility.NEEDS_MANUAL_REVIEW
            reason = f"refused by fetch-time URL validation: {exc}"
            report.refused.append(f"{row.source_ref} {row.normalized_url}: {exc}")

        source_type = SOURCE_TYPE_BY_CATEGORY.get(row.source_type, "unclassified")
        if source_type == "unclassified":
            report.unclassified += 1
        strategy = "DOCUMENT" if row.normalized_url.lower().endswith(".pdf") else "STATIC"
        if strategy == "DOCUMENT":
            report.document_strategy += 1

        source_id = connection.execute(
            text(
                """
                INSERT INTO source (id, url, url_hash, source_type, owner_entity_type,
                                    owner_entity_id, crawl_frequency, fetch_strategy,
                                    fetch_eligibility, fetch_eligibility_reason,
                                    fetch_eligibility_set_at, registered_by)
                VALUES (:id, :url, :hash, :type, 'target_institution', :owner,
                        :freq, :strategy, :eligibility, :reason, now(), :by)
                ON CONFLICT (url_hash) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": uuid.uuid4(),
                "url": row.normalized_url,
                "hash": row.url_sha256,
                "type": source_type,
                "owner": row.target_institution_id,
                "freq": DEFAULT_CRAWL_FREQUENCY,
                "strategy": strategy,
                "eligibility": eligibility.value,
                "reason": reason,
                "by": registered_by,
            },
        ).scalar_one_or_none()

        if source_id is None:
            source_id = connection.execute(
                text("SELECT id FROM source WHERE url_hash = :hash"), {"hash": row.url_sha256}
            ).scalar_one()
            report.sources_existing += 1
        else:
            report.sources_created += 1
            if eligibility is FetchEligibility.FETCHABLE:
                report.fetchable += 1
            else:
                report.needs_manual_review += 1

        # Every claim on this URL, physical and duplicate alike, points at the one
        # source. That is what makes "fetched once, claimed three times" queryable
        # from either end.
        linked = connection.execute(
            text(
                """
                UPDATE pilot_collected_source
                   SET acquisition_source_id = :source
                 WHERE submission_id = :submission
                   AND target_institution_id = :institution
                   AND url_sha256 = :hash
                   AND acquisition_source_id IS DISTINCT FROM :source
                RETURNING id
                """
            ),
            {
                "source": source_id,
                "submission": row.submission_id,
                "institution": row.target_institution_id,
                "hash": row.url_sha256,
            },
        ).scalars()
        report.claims_linked += len(list(linked))

    logger.info(
        "acquisition_targets_registered",
        pages=report.physical_pages,
        created=report.sources_created,
        existing=report.sources_existing,
        claims=report.claims_linked,
    )
    return report


def assert_nothing_became_publishable(connection: Connection) -> None:
    """Belt and braces: no source registered for acquisition earned a class.

    C27 already refuses this, and `register_acquisition_targets` never sets the
    column. This exists so the claim can be *checked* after a real run rather than
    argued from the code, and so a future edit that starts passing an eligibility
    fails loudly here.
    """
    leaked = connection.execute(
        text(
            "SELECT count(*) FROM source s "
            " JOIN pilot_collected_source p ON p.acquisition_source_id = s.id "
            " WHERE s.publication_eligibility <> :not_eligible"
        ),
        {"not_eligible": PublicationEligibility.NOT_ELIGIBLE.value},
    ).scalar_one()
    if leaked:
        raise AssertionError(
            f"{leaked} acquisition source(s) carry a publication eligibility class. "
            "Registration must never grant one; only a promoted, verified mapping may."
        )


__all__ = [
    "DEFAULT_CRAWL_FREQUENCY",
    "SOURCE_TYPE_BY_CATEGORY",
    "RegistrationReport",
    "assert_nothing_became_publishable",
    "register_acquisition_targets",
]
