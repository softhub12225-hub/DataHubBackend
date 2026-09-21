"""Acquisition reporting and source-verification assistance (Step 5B sections 22-23).

VERIFICATION ASSISTANCE, NOT VERIFICATION
=========================================
A reviewer deciding whether a URL is the page the collector meant currently has only
the address. After a fetch we can also show them: what HTTP status it returned, what
content type, what the page's `<title>` says, whether it redirected, and whether the
host that finally served it is the host we asked.

Every one of those makes the human decision better informed. **None of them makes it.**
Nothing in this module verifies, rejects, classifies or promotes anything -- it has no
write path at all. A 200 response is not evidence that a page is the tuition page, and
a redirect to an external portal is a question for a reviewer, not a promotion for that
host.

The temptation this refuses is real and specific: with a title and a status code it
would be easy to write "if the title contains 'Tuition' then classify it as
TUITION_FEES". That is guessing from a string, and the whole system exists because
guessing from strings is how one university's fees get published under another's name.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

#: The host of a stored URL, in SQL. Must agree with `_host_of` below, which is the
#: Python side of the same question -- `test_sql_and_python_agree_on_the_host_of_a_url`
#: asserts they do, because two implementations of one definition drift silently.
#:
#: Safe for the URLs this system stores and not in general: `onboarding/urls.py` refuses
#: credentials and IP literals at registration, so there is no userinfo to skip and no
#: bracketed IPv6 authority to mis-parse.
_SQL_HOST_OF = "lower(split_part(regexp_replace({0}, '^[^:]+://([^/?#]*).*$', '\\1'), ':', 1))"

#: "The page ended up on a different host than we asked for."
#:
#: NOT `effective_url IS NOT NULL`, which was the bug (C36). `effective_url` is written
#: whenever the final URL differs from the requested one **as a string**, so http->https,
#: a trailing slash or `/index.html` all set it -- and counting those as "off host" told
#: an operator that a university had redirected them elsewhere when it had done nothing
#: of the kind.
_MOVED_OFF_HOST = (
    "{0}.effective_url IS NOT NULL AND "
    + _SQL_HOST_OF.format("{0}.effective_url")
    + " <> "
    + _SQL_HOST_OF.format("{0}.requested_url")
)


@dataclass(frozen=True, slots=True)
class AcquisitionAssistance:
    """What a fetch learned about a URL, for the person who has to judge it."""

    source_id: uuid.UUID
    url: str
    match_key: str
    physical_source_ref: str
    claimed_categories: tuple[str, ...]
    requested_host: str
    effective_host: str | None
    effective_url: str | None
    host_differs: bool
    redirect_chain: list[dict[str, object]] | None
    http_status: int | None
    content_type: str | None
    page_title: str | None
    observed_at: object
    health: str
    #: Unchanged by anything here, and shown so the reviewer can see that fetching
    #: earned the source nothing.
    publication_eligibility: str


@dataclass(slots=True)
class AcquisitionSummary:
    """Counts an operator asks for after a cycle."""

    physical_pages: int = 0
    responsibility_claims: int = 0
    institutions: int = 0
    hosts: int = 0
    fetchable: int = 0
    blocked: int = 0
    disabled: int = 0
    needs_manual_review: int = 0
    never_fetched: int = 0
    by_health: dict[str, int] = field(default_factory=dict)
    total_runs: int = 0
    snapshots: int = 0
    #: Distinct bodies reachable from this population's snapshots -- **not** the row
    #: count of `content_blob`, which is global because a blob is shared by every page
    #: that served those exact bytes.
    blobs: int = 0
    unchanged_runs: int = 0
    publication_eligible: int = 0
    #: Named for its grain on purpose (C35): **pages**, not snapshots. One page that
    #: redirects on all three cycles moved once, not three times.
    pages_redirecting_off_host: int = 0

    def summary(self) -> str:
        return (
            f"{self.physical_pages} physical page(s) over {self.responsibility_claims} "
            f"responsibility claim(s), {self.institutions} institution(s), "
            f"{self.hosts} host(s): {self.fetchable} fetchable, {self.blocked} blocked, "
            f"{self.never_fetched} never fetched. {self.snapshots} snapshot(s) over "
            f"{self.blobs} distinct blob(s). "
            f"{self.publication_eligible} publication-eligible."
        )


def acquisition_summary(connection: Connection, *, pilot_only: bool = True) -> AcquisitionSummary:
    """The state of acquisition, read-only."""
    report = AcquisitionSummary()

    # `acquisition_target` is one row per physical page **per institution**, not one
    # row per page: uniqueness of a URL is enforced per institution (Step 5A, D31),
    # while `source` is unique on `url_hash` globally. So two institutions citing one
    # shared page -- a government regulator, say -- give that single page two rows,
    # and `count(*)` would report two pages while `sum(claim_count)` counted each of
    # its claims twice. The client's current file happens to be 1:1 (319 sources, 319
    # non-duplicate physical rows), so this was latent rather than wrong; it is fixed
    # here because "latent" means "wrong as soon as the data changes" (C37).
    row = connection.execute(
        text(
            """
            WITH page AS (
                SELECT DISTINCT t.source_id, t.claim_count, t.host,
                       t.fetch_eligibility, t.publication_eligibility
                  FROM acquisition_target t
                 WHERE NOT :pilot_only OR t.pilot_wave IS NOT NULL
            )
            SELECT count(*)                                              AS pages,
                   coalesce(sum(p.claim_count), 0)                       AS claims,
                   (SELECT count(DISTINCT t.target_institution_id)
                      FROM acquisition_target t
                     WHERE NOT :pilot_only OR t.pilot_wave IS NOT NULL)  AS institutions,
                   count(DISTINCT p.host)                                AS hosts,
                   count(*) FILTER (WHERE p.fetch_eligibility = 'FETCHABLE')   AS fetchable,
                   count(*) FILTER (WHERE p.fetch_eligibility = 'BLOCKED')     AS blocked,
                   count(*) FILTER (WHERE p.fetch_eligibility = 'DISABLED')    AS disabled,
                   count(*) FILTER (WHERE p.fetch_eligibility = 'NEEDS_MANUAL_REVIEW')
                                                                              AS needs_review,
                   count(*) FILTER (WHERE p.publication_eligibility <> 'NOT_ELIGIBLE')
                                                                              AS eligible
              FROM page p
            """
        ),
        {"pilot_only": pilot_only},
    ).one()
    report.physical_pages = row.pages
    report.responsibility_claims = int(row.claims)
    report.institutions = row.institutions
    report.hosts = row.hosts
    report.fetchable = row.fetchable
    report.blocked = row.blocked
    report.disabled = row.disabled
    report.needs_manual_review = row.needs_review
    report.publication_eligible = row.eligible

    report.by_health = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            text(
                # DISTINCT on the source, for the same reason as above: a page two
                # institutions share must not be two entries in the fleet.
                "SELECT h.health, count(DISTINCT h.source_id) FROM source_health h "
                "  JOIN acquisition_target t ON t.source_id = h.source_id "
                " WHERE NOT :pilot_only OR t.pilot_wave IS NOT NULL "
                " GROUP BY h.health ORDER BY h.health"
            ),
            {"pilot_only": pilot_only},
        )
    }
    report.never_fetched = report.by_health.get("NEVER_FETCHED", 0)

    # These were global while every other field above honoured `pilot_only`, so one
    # sentence reported a pilot numerator against whole-database totals (C37). They
    # are scoped to the same population now.
    #
    # `content_blob` is reached through `snapshot`, because it has no source column:
    # it is the global dedup boundary and two pages serving identical bytes share one
    # row. `count(DISTINCT content_hash)` is therefore the honest blob count for a
    # population, and it is a *different* number from `count(*) FROM content_blob`.
    #
    # S608 is suppressed where `_MOVED_OFF_HOST` is interpolated: it is a module
    # constant built from literals, never from input, and writing the predicate out
    # by hand instead is the duplication that caused C36.
    counts = connection.execute(
        text(
            "WITH scoped AS ("  # noqa: S608
            "  SELECT DISTINCT t.source_id FROM acquisition_target t "
            "   WHERE NOT :pilot_only OR t.pilot_wave IS NOT NULL) "
            "SELECT (SELECT count(*) FROM fetch_run r "
            "         WHERE r.source_id IN (SELECT source_id FROM scoped)) AS runs, "
            "       (SELECT count(*) FROM snapshot s "
            "         WHERE s.source_id IN (SELECT source_id FROM scoped)) AS snapshots, "
            "       (SELECT count(DISTINCT s.content_hash) FROM snapshot s "
            "         WHERE s.source_id IN (SELECT source_id FROM scoped)) AS blobs, "
            "       (SELECT count(*) FROM fetch_run r WHERE r.status = 'UNCHANGED' "
            "         AND r.source_id IN (SELECT source_id FROM scoped)) AS unchanged, "
            "       (SELECT count(DISTINCT s.source_id) FROM snapshot s "
            "         WHERE s.source_id IN (SELECT source_id FROM scoped) "
            f"          AND {_MOVED_OFF_HOST.format('s')}) AS moved"
        ),
        {"pilot_only": pilot_only},
    ).one()
    report.total_runs = counts.runs
    report.snapshots = counts.snapshots
    report.blobs = counts.blobs
    report.unchanged_runs = counts.unchanged
    report.pages_redirecting_off_host = counts.moved
    return report


#: The assist query, with one hole for the off-host predicate. Kept at module level and
#: free of interpolation so the single `.format` call below is a plain code line, which
#: is where the S608 suppression has to live: a lint directive placed after a triple
#: quote would be inside the string and would be sent to Postgres as SQL.
_ASSIST_SQL = """
            SELECT t.source_id, t.url, t.match_key, t.physical_source_ref,
                   t.categories, t.host AS requested_host,
                   t.publication_eligibility,
                   h.health,
                   s.effective_url, s.redirect_chain, s.http_status, s.content_type,
                   s.technical_metadata, s.observed_at
              FROM acquisition_target t
              LEFT JOIN source_health h ON h.source_id = t.source_id
              LEFT JOIN LATERAL (
                    SELECT sn.effective_url, sn.requested_url, sn.redirect_chain,
                           sn.http_status, sn.content_type, sn.technical_metadata,
                           sn.observed_at
                      FROM snapshot sn
                     WHERE sn.source_id = t.source_id
                     ORDER BY sn.observed_at DESC, sn.id DESC
                     LIMIT 1
              ) s ON TRUE
             WHERE (cast(:institution AS uuid) IS NULL
                    OR t.target_institution_id = :institution)
               AND (NOT :only_moved OR ({moved}))
             ORDER BY t.match_key, t.physical_source_ref
             LIMIT :limit
"""


def verification_assistance(
    connection: Connection,
    *,
    target_institution_id: uuid.UUID | None = None,
    only_host_differs: bool = False,
    limit: int = 50,
) -> list[AcquisitionAssistance]:
    """What acquisition learned, per page, for the source reviewer.

    Read-only by construction: there is no write path in this module, so "the report
    verified it" cannot happen by accident.
    """
    assist_sql = _ASSIST_SQL.format(moved=_MOVED_OFF_HOST.format("s"))
    rows = connection.execute(
        text(assist_sql),
        {
            "institution": target_institution_id,
            "only_moved": only_host_differs,
            "limit": limit,
        },
    ).all()

    assistance: list[AcquisitionAssistance] = []
    for row in rows:
        effective_host = _host_of(row.effective_url) if row.effective_url else None
        metadata = row.technical_metadata or {}
        assistance.append(
            AcquisitionAssistance(
                source_id=row.source_id,
                url=row.url,
                match_key=row.match_key,
                physical_source_ref=row.physical_source_ref,
                claimed_categories=tuple(row.categories or ()),
                requested_host=row.requested_host,
                effective_host=effective_host,
                effective_url=row.effective_url,
                host_differs=bool(effective_host and effective_host != row.requested_host),
                redirect_chain=row.redirect_chain,
                http_status=row.http_status,
                content_type=row.content_type,
                page_title=metadata.get("title") if isinstance(metadata, dict) else None,
                observed_at=row.observed_at,
                health=row.health or "NEVER_FETCHED",
                publication_eligibility=row.publication_eligibility,
            )
        )
    return assistance


def health_by_institution(connection: Connection) -> dict[str, Counter[str]]:
    """Health counts per institution, for the operational report."""
    grouped: dict[str, Counter[str]] = {}
    for row in connection.execute(
        text(
            "SELECT t.match_key, h.health, count(*) AS n "
            "  FROM acquisition_target t "
            "  JOIN source_health h ON h.source_id = t.source_id "
            " GROUP BY 1, 2 ORDER BY 1, 2"
        )
    ):
        grouped.setdefault(row.match_key, Counter())[row.health] = row.n
    return grouped


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


__all__ = [
    "AcquisitionAssistance",
    "AcquisitionSummary",
    "acquisition_summary",
    "health_by_institution",
    "verification_assistance",
]
