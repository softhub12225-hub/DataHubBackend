"""Whether a candidate may be promoted to a published claim, and if not, exactly why.

WHY THIS IS NOT A BOOLEAN
=========================
Section 24. `ready = false` tells a reviewer to go and look at eight possible causes.
Every blocker here names one thing to do, and a candidate can carry several at once
because it usually does -- an accepted candidate on an unverified host is blocked by the
host *and* by the responsibility, and fixing one does not make it ready.

WHY IT DOES NOT ASK `source.publication_eligibility` ALONE
==========================================================
Section 22. That column answers "may this page support a published fact at all", and
until Step 5C.6 it was the only thing C27 asked. It cannot answer "may this page support
*this* fact", because one URL carries several responsibilities and a reviewer decides
them separately. So readiness resolves the candidate's own responsibility claim, checks
it was verified, and checks the policy table says that responsibility may publish that
field kind.

The order of checks is deliberate: the cheap, local facts first, the joins after, and
the locator -- which reads an artifact off disk -- last and only when asked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.domains.claims.review import CURRENT_PARAMS, current_only
from app.domains.verification.policy import Blocker, is_compatible, responsibilities_for


@dataclass(frozen=True, slots=True)
class Readiness:
    """One candidate's answer, with its reasons."""

    candidate_id: uuid.UUID
    field_kind: str
    responsibility: str | None
    blockers: tuple[Blocker, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return not self.blockers


#: The one query readiness needs. Everything it selects is a fact somebody recorded:
#: no heuristics, and no reading of candidate content.
READINESS_SQL = f"""
    SELECT c.id                                     AS candidate_id,
           c.field_kind,
           c.source_responsibility                  AS responsibility,
           v.decision_state,
           v.is_superseded,
           v.scope_unresolved,
           s.id                                     AS source_id,
           s.publication_eligibility::text          AS eligibility,
           s.superseded_by_source_id,
           od.verification_status::text             AS domain_status,
           od.is_active                             AS domain_active,
           sm.verification_status::text             AS mapping_status,
           sm.is_active                             AS mapping_active,
           sm.promoted_source_id,
           sm.source_category::text                 AS mapping_responsibility
      FROM field_claim_candidate c
      JOIN candidate_review_state v ON v.candidate_id = c.id
      JOIN extraction e ON e.id = c.extraction_id
      JOIN snapshot sn ON sn.id = e.snapshot_id
      JOIN source s ON s.id = sn.source_id
      JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
      LEFT JOIN source_mapping sm
             ON sm.url_sha256 = s.url_hash
            AND sm.source_category::text = c.source_responsibility
      LEFT JOIN official_domain od
             ON od.id = sm.official_domain_id
     WHERE {current_only("c")}
"""


def assess(connection: Connection, *, accepted_only: bool = False) -> list[Readiness]:
    """Readiness for every current candidate, with blockers.

    `accepted_only` narrows to the ones a reviewer has already accepted, which is the
    list an operator actually works from: an unreviewed candidate is blocked by the
    absence of a decision and nothing else is interesting about it yet.
    """
    rows = connection.execute(text(READINESS_SQL), CURRENT_PARAMS).all()
    out: list[Readiness] = []
    for row in rows:
        if accepted_only and row.decision_state != "ACCEPTED":
            continue
        out.append(
            Readiness(
                candidate_id=row.candidate_id,
                field_kind=str(row.field_kind),
                responsibility=row.responsibility,
                blockers=tuple(blockers_for(row)),
            )
        )
    return out


def blockers_for(row: object) -> list[Blocker]:
    """Every reason this candidate is not promotable, in the order to fix them.

    Deliberately not short-circuiting. A reviewer verifying the host only to discover
    the responsibility is also unverified has been told half the answer, and the second
    half was knowable the first time.
    """
    found: list[Blocker] = []

    if getattr(row, "is_superseded", False):
        found.append(Blocker.NOT_CURRENT)
    if getattr(row, "decision_state", None) != "ACCEPTED":
        found.append(Blocker.NOT_ACCEPTED)

    # The host. A mapping with no domain row is a page on an unverified host.
    domain_status = getattr(row, "domain_status", None)
    if domain_status not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL") or not getattr(
        row, "domain_active", False
    ):
        found.append(Blocker.DOMAIN_NOT_VERIFIED)

    # This page's claim to carry THIS responsibility.
    mapping_status = getattr(row, "mapping_status", None)
    if mapping_status not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL") or not getattr(
        row, "mapping_active", False
    ):
        found.append(Blocker.RESPONSIBILITY_NOT_VERIFIED)
    elif getattr(row, "promoted_source_id", None) != getattr(row, "source_id", None):
        found.append(Blocker.MAPPING_NOT_PROMOTED)

    # Does that responsibility authorise this field at all? Checked whatever the
    # decision was: an incompatible pairing is worth saying even while unverified,
    # because verifying it would not help.
    responsibility = getattr(row, "responsibility", None)
    if not is_compatible(str(getattr(row, "field_kind", "")), responsibility):
        found.append(Blocker.RESPONSIBILITY_INCOMPATIBLE)

    if getattr(row, "eligibility", None) == "NOT_ELIGIBLE":
        found.append(Blocker.SOURCE_NOT_ELIGIBLE)
    if getattr(row, "superseded_by_source_id", None) is not None:
        found.append(Blocker.SOURCE_SUPERSEDED)
    if getattr(row, "scope_unresolved", False):
        found.append(Blocker.SCOPE_UNRESOLVED)

    return found


def summarise(results: list[Readiness]) -> dict[str, int]:
    """How many candidates each blocker is holding up. Order is by weight, descending."""
    counts: dict[str, int] = {}
    for result in results:
        for blocker in result.blockers:
            counts[blocker.value] = counts.get(blocker.value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def explain(field_kind: str) -> str:
    """Which responsibilities could ever publish this field kind. For a report."""
    allowed = sorted(responsibilities_for(field_kind))
    return ", ".join(allowed) if allowed else "(none: this field kind publishes nowhere)"


__all__ = ["READINESS_SQL", "Readiness", "assess", "blockers_for", "explain", "summarise"]
