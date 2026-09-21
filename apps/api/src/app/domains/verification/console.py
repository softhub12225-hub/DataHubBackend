"""Everything the reviewer console displays, computed on the server. Read-only.

WHY A READ MODEL AND NOT A SET OF AD-HOC QUERIES
================================================
The console's whole justification is that a reviewer can *see* the state of the system
before acting on it. That makes every number on the screen a claim, and a claim assembled
differently in two places will eventually disagree with itself. So the counts, the
per-institution progress and the per-mapping cards are computed here, once, from the
database, and the frontend renders what it is given.

Nothing in this module writes. It is safe to point at the real `datahub`, which is the
point: the console must be able to show the live pilot without being able to alter it.

THE MANIFEST STATUS PANEL
=========================
`manifest_status` compares each frozen package on disk against the digest that was
approved. Green means *this is the package the reviewer signed off*. It is not decorative:
a decision is only bindable to a manifest row while the manifest still hashes to the
approved value, so a red panel is an early warning that every decision path is about to
refuse.

The approved digests are constants here rather than configuration. A digest that can be
changed by an environment variable is a digest that can be made to agree with whatever is
on disk, which defeats the entire mechanism.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.domains.verification import redirect_authority
from app.domains.verification.domain_binding import MANIFEST_DIR, manifest_digest

#: The digests of the three frozen packages as approved. See the module docstring for why
#: these are code and not configuration.
APPROVED_MANIFESTS: dict[str, str] = {
    "DOMAINS": "3de3166952df9a6313139027c788e05595d9f7bd3d28d253c2bbfa522ccc7b3a",
    "SOURCES": "7bc83909e394049bc258a9cecd117ff2e4de4fc30422dd307d83a227eaeea316",
    "RESPONSIBILITIES": "435e5c7dee1a91e27e04107a9c0a88b77c57b6dd906fb780e3b38e69783ee32c",
}

_MANIFEST_FILES = {
    "DOMAINS": "domain_decisions_proposed",
    "SOURCES": "source_decisions_proposed",
    "RESPONSIBILITIES": "responsibility_decisions_proposed",
}

#: Audit actions the institution audit tab groups under human-readable headings.
AUDIT_GROUPS: dict[str, str] = {
    "ONBOARDING_VERIFY": "Domain verification",
    "ONBOARDING_REJECT": "Domain verification",
    "PILOT_SOURCE_VERIFY": "Pilot-source decision",
    "PILOT_SOURCE_REJECT": "Pilot-source decision",
    "PILOT_SOURCE_NEEDS_REVIEW": "Pilot-source decision",
    "PILOT_SOURCE_CLASSIFY": "Pilot-source decision",
    "PILOT_SOURCE_REGISTERED": "Pilot-source registration",
    "RESPONSIBILITY_VERIFIED": "Responsibility decision",
    "RESPONSIBILITY_REJECTED": "Responsibility decision",
    "RESPONSIBILITY_NEEDS_REVIEW": "Responsibility decision",
    "SOURCE_MAPPING_PROMOTED": "Promotion",
}


#: The audit selection. Columns are named explicitly rather than `SELECT *` so a future
#: column cannot start appearing in a browser because nobody revisited this.
#:
#: Two complete literals rather than one with an interpolated WHERE clause: an assembled
#: SQL string is the shape a reader has to stop and verify, and writing the scoped variant
#: out is shorter than the argument for why the interpolation was safe.
_AUDIT_SELECT = """
            SELECT al.seq, al.occurred_at, al.action, al.actor_type::text AS actor_type,
                   al.object_type, al.object_id, al.before_state, al.after_state,
                   al.reason, who.display_name AS actor_name,
                   COALESCE(pcs.source_ref, pcs2.source_ref) AS source_ref
              FROM audit_log al
              LEFT JOIN app_user who ON who.id = al.actor_id
              LEFT JOIN pilot_collected_source pcs ON pcs.id = al.object_id
              LEFT JOIN pilot_collected_source pcs2
                     ON pcs2.promoted_source_mapping_id = al.object_id
"""

_AUDIT_TAIL = """
             ORDER BY al.seq DESC
             LIMIT :limit
"""

_AUDIT_SQL_ALL = _AUDIT_SELECT + _AUDIT_TAIL

# Suppression justified: every operand below is a module-level string literal in this
# file. No caller value reaches the SQL text -- the institution arrives as the bound
# parameter `:inst`. The rule cannot distinguish literal composition from
# interpolation, and rewriting the query to satisfy it would mean duplicating the
# whole SELECT, which is the thing most likely to drift.
_AUDIT_SQL_FOR_INSTITUTION = (
    _AUDIT_SELECT  # noqa: S608
    + """
             WHERE al.object_id IN (SELECT id FROM official_domain
                                     WHERE target_institution_id = :inst)
                OR al.object_id IN (SELECT id FROM source_mapping
                                     WHERE target_institution_id = :inst)
                OR al.object_id IN (SELECT id FROM pilot_collected_source
                                     WHERE target_institution_id = :inst)
"""
    + _AUDIT_TAIL
)


@dataclass(frozen=True, slots=True)
class ManifestStatus:
    """One frozen package, and whether the file on disk is still the approved one."""

    name: str
    approved_sha256: str
    actual_sha256: str | None
    rows: int
    present: bool

    @property
    def matches(self) -> bool:
        return self.present and self.actual_sha256 == self.approved_sha256


@dataclass(frozen=True, slots=True)
class TrustCounts:
    """The numbers the dashboard leads with. Every one is a live count."""

    pilot_institutions: int
    verified_domains: int
    pilot_decisions: int
    source_mappings: int
    responsibility_decisions: int
    promoted_mappings: int
    eligible_sources: int


@dataclass(frozen=True, slots=True)
class SafetyCounts:
    """What must still be zero. Displayed prominently so a change is impossible to miss."""

    field_claim: int
    change_proposal: int
    change_event: int
    canonical_rows: int

    @property
    def all_zero(self) -> bool:
        return not (
            self.field_claim or self.change_proposal or self.change_event or self.canonical_rows
        )


@dataclass(frozen=True, slots=True)
class InstitutionRow:
    """One institution's compact status for the list page."""

    institution_id: uuid.UUID
    name: str
    """`target_institution.match_key`. There is no separate display name column, and the
    match key is exactly the label the frozen manifests use, so the console, the manifest
    and the database all name an institution the same way."""

    match_key: str
    verified_domains: int
    total_domains: int
    pilot_rows: int
    pilot_decided: int
    registered_mappings: int
    responsibility_decided: int
    promoted: int
    unresolved_sources: int


@dataclass(frozen=True, slots=True)
class DomainRow:
    """One verified (or not) host belonging to an institution."""

    domain_id: uuid.UUID
    host: str
    verification_status: str
    is_active: bool
    verified_by_name: str | None
    verified_at: Any


@dataclass(frozen=True, slots=True)
class PilotSourceRow:
    """One workbook row as the Sources tab shows it."""

    pilot_source_id: uuid.UUID
    source_ref: str
    responsibility: str
    degree_scope: str | None
    url: str
    host: str
    verification_state: str
    verification_reason: str | None
    mapping_id: uuid.UUID | None
    is_duplicate: bool
    duplicate_of: str | None
    body_available: bool
    effective_url: str | None
    redirected: bool
    http_status: int | None


@dataclass(frozen=True, slots=True)
class ResponsibilityCard:
    """One registered mapping, with every fact section H requires on its card."""

    mapping_id: uuid.UUID
    source_ref: str
    responsibility: str
    requested_url: str
    effective_url: str | None
    requested_host: str
    requested_host_authority: str
    effective_host: str
    effective_host_authority: str
    redirected: bool
    body_available: bool
    verification_status: str
    publication_eligibility: str
    verified_by_name: str | None
    promoted_source_id: uuid.UUID | None
    manifest_bound: bool
    manifest_blocker: str | None


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One audit entry, with no credential material of any kind."""

    seq: int
    occurred_at: Any
    action: str
    group: str
    actor_name: str | None
    actor_type: str
    object_type: str
    object_id: uuid.UUID | None
    source_ref: str | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    reason: str | None
    is_historical_registration: bool


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Section D, assembled in one place."""

    trust: TrustCounts
    safety: SafetyCounts
    manifests: list[ManifestStatus]
    physical_sources: int
    responsibility_rows: int
    reviewed_domain_rows: int
    audit_rows: int
    audit_chain_ok: bool
    blockers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------


def manifest_status(directory: Path = MANIFEST_DIR) -> list[ManifestStatus]:
    """Each frozen package, hashed from disk and compared with the approved digest."""
    out: list[ManifestStatus] = []
    for name, basename in _MANIFEST_FILES.items():
        path = directory / f"{basename}.json"
        if not path.exists():
            out.append(
                ManifestStatus(
                    name=name,
                    approved_sha256=APPROVED_MANIFESTS[name],
                    actual_sha256=None,
                    rows=0,
                    present=False,
                )
            )
            continue
        rows = list(json.loads(path.read_text(encoding="utf-8")).get("rows") or [])
        out.append(
            ManifestStatus(
                name=name,
                approved_sha256=APPROVED_MANIFESTS[name],
                actual_sha256=manifest_digest(rows),
                rows=len(rows),
                present=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


def _scalar(connection: Connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def dashboard(connection: Connection, directory: Path = MANIFEST_DIR) -> Dashboard:
    """Section D. Live counts, never cached: a stale safety count is worse than none."""
    trust = TrustCounts(
        pilot_institutions=_scalar(
            connection, "SELECT count(DISTINCT target_institution_id) FROM pilot_collected_source"
        ),
        verified_domains=_scalar(
            connection,
            "SELECT count(*) FROM official_domain "
            " WHERE verification_status = 'VERIFIED_OFFICIAL' AND is_active",
        ),
        pilot_decisions=_scalar(
            connection,
            "SELECT count(*) FROM pilot_collected_source WHERE verification_state <> 'PENDING'",
        ),
        source_mappings=_scalar(connection, "SELECT count(*) FROM source_mapping"),
        responsibility_decisions=_scalar(
            connection, "SELECT count(*) FROM source_mapping WHERE verified_by IS NOT NULL"
        ),
        promoted_mappings=_scalar(
            connection, "SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL"
        ),
        eligible_sources=_scalar(
            connection,
            "SELECT count(*) FROM source WHERE publication_eligibility <> 'NOT_ELIGIBLE'",
        ),
    )
    safety = SafetyCounts(
        field_claim=_scalar(connection, "SELECT count(*) FROM field_claim"),
        change_proposal=_scalar(connection, "SELECT count(*) FROM change_proposal"),
        change_event=_scalar(connection, "SELECT count(*) FROM change_event"),
        canonical_rows=_scalar(
            connection,
            "SELECT (SELECT count(*) FROM university) + (SELECT count(*) FROM program)"
            "     + (SELECT count(*) FROM tuition)",
        ),
    )
    manifests = manifest_status(directory)
    chain_ok = not connection.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all()

    blockers: list[str] = []
    blockers += [
        f"{m.name} manifest does not match the approved package" for m in manifests if not m.matches
    ]
    if not chain_ok:
        blockers.append("the audit hash chain does not verify")
    if not safety.all_zero:
        blockers.append("a downstream plane is no longer empty")

    return Dashboard(
        trust=trust,
        safety=safety,
        manifests=manifests,
        physical_sources=_scalar(connection, "SELECT count(*) FROM source"),
        responsibility_rows=_scalar(connection, "SELECT count(*) FROM pilot_collected_source"),
        reviewed_domain_rows=next((m.rows for m in manifests if m.name == "DOMAINS"), 0),
        audit_rows=_scalar(connection, "SELECT count(*) FROM audit_log"),
        audit_chain_ok=bool(chain_ok),
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# institutions
# ---------------------------------------------------------------------------


def institutions(connection: Connection) -> list[InstitutionRow]:
    """Section E. Every institution that has pilot rows, with compact progress."""
    rows = connection.execute(
        text(
            """
            SELECT ti.id, ti.match_key AS name, ti.match_key,
                   count(DISTINCT pcs.id)                                     AS pilot_rows,
                   count(DISTINCT pcs.id) FILTER (
                       WHERE pcs.verification_state <> 'PENDING')             AS pilot_decided,
                   count(DISTINCT pcs.promoted_source_mapping_id)             AS registered,
                   count(DISTINCT pcs.id) FILTER (
                       WHERE pcs.verification_state IN ('NEEDS_REVIEW', 'REJECTED')
                   )                                                          AS unresolved,
                   (SELECT count(*) FROM official_domain od
                     WHERE od.target_institution_id = ti.id)                  AS total_domains,
                   (SELECT count(*) FROM official_domain od
                     WHERE od.target_institution_id = ti.id
                       AND od.verification_status = 'VERIFIED_OFFICIAL'
                       AND od.is_active)                                      AS verified_domains,
                   (SELECT count(*) FROM source_mapping sm
                     WHERE sm.target_institution_id = ti.id
                       AND sm.verified_by IS NOT NULL)                        AS resp_decided,
                   (SELECT count(*) FROM source_mapping sm
                     WHERE sm.target_institution_id = ti.id
                       AND sm.promoted_source_id IS NOT NULL)                 AS promoted
              FROM target_institution ti
              JOIN pilot_collected_source pcs ON pcs.target_institution_id = ti.id
             GROUP BY ti.id, ti.match_key
             ORDER BY ti.match_key
            """
        )
    ).all()
    return [
        InstitutionRow(
            institution_id=uuid.UUID(str(r.id)),
            name=str(r.name),
            match_key=str(r.match_key),
            verified_domains=int(r.verified_domains),
            total_domains=int(r.total_domains),
            pilot_rows=int(r.pilot_rows),
            pilot_decided=int(r.pilot_decided),
            registered_mappings=int(r.registered),
            responsibility_decided=int(r.resp_decided),
            promoted=int(r.promoted),
            unresolved_sources=int(r.unresolved),
        )
        for r in rows
    ]


def domains_of(connection: Connection, institution_id: uuid.UUID) -> list[DomainRow]:
    """Section F, Domains tab."""
    rows = connection.execute(
        text(
            """
            SELECT od.id, od.host, od.verification_status::text AS status, od.is_active,
                   od.verified_at, who.display_name AS verified_by_name
              FROM official_domain od
              LEFT JOIN app_user who ON who.id = od.verified_by
             WHERE od.target_institution_id = :inst
             ORDER BY od.host
            """
        ),
        {"inst": institution_id},
    ).all()
    return [
        DomainRow(
            domain_id=uuid.UUID(str(r.id)),
            host=str(r.host),
            verification_status=str(r.status),
            is_active=bool(r.is_active),
            verified_by_name=(str(r.verified_by_name) if r.verified_by_name else None),
            verified_at=r.verified_at,
        )
        for r in rows
    ]


def pilot_sources_of(connection: Connection, institution_id: uuid.UUID) -> list[PilotSourceRow]:
    """Section F, Sources tab. All rows, decided or not, with evidence status."""
    rows = connection.execute(
        text(
            """
            SELECT pcs.id, pcs.source_ref, pcs.source_type::text AS responsibility,
                   pcs.degree_scope::text AS degree_scope, pcs.official_url, pcs.host,
                   pcs.verification_state::text AS state, pcs.verification_reason,
                   pcs.promoted_source_mapping_id, pcs.duplicate_of_source_ref,
                   snap.effective_url, snap.http_status,
                   snap.id IS NOT NULL AS has_snapshot
              FROM pilot_collected_source pcs
              LEFT JOIN LATERAL (
                   SELECT s.id, s.effective_url, s.http_status
                     FROM snapshot s
                    WHERE s.source_id = pcs.acquisition_source_id
                    ORDER BY s.observed_at DESC
                    LIMIT 1
              ) snap ON TRUE
             WHERE pcs.target_institution_id = :inst
             ORDER BY pcs.source_ref
            """
        ),
        {"inst": institution_id},
    ).all()
    out: list[PilotSourceRow] = []
    for r in rows:
        effective = str(r.effective_url) if r.effective_url else None
        redirected = bool(
            effective and _host_of(effective) and _host_of(effective) != str(r.host).lower()
        )
        out.append(
            PilotSourceRow(
                pilot_source_id=uuid.UUID(str(r.id)),
                source_ref=str(r.source_ref),
                responsibility=str(r.responsibility),
                degree_scope=(str(r.degree_scope) if r.degree_scope else None),
                url=str(r.official_url),
                host=str(r.host),
                verification_state=str(r.state),
                verification_reason=(str(r.verification_reason) if r.verification_reason else None),
                mapping_id=(
                    uuid.UUID(str(r.promoted_source_mapping_id))
                    if r.promoted_source_mapping_id
                    else None
                ),
                is_duplicate=bool(r.duplicate_of_source_ref),
                duplicate_of=(
                    str(r.duplicate_of_source_ref) if r.duplicate_of_source_ref else None
                ),
                body_available=bool(r.has_snapshot),
                effective_url=effective,
                redirected=redirected,
                http_status=(int(r.http_status) if r.http_status is not None else None),
            )
        )
    return out


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def responsibility_cards(
    connection: Connection,
    institution_id: uuid.UUID,
    *,
    expect_sha256: str | None = None,
    directory: Path = MANIFEST_DIR,
) -> list[ResponsibilityCard]:
    """Section H. One card per registered mapping, with its manifest binding checked.

    The binding is evaluated here rather than described, so a card can say *bound* only
    when the same function the write path uses agreed that it is.
    """
    from app.domains.verification.responsibility_binding import (
        ManifestChangedError,
        ResponsibilityBindingError,
    )
    from app.domains.verification.responsibility_binding import (
        require_binding as require_responsibility_binding,
    )

    rows = connection.execute(
        text(
            """
            SELECT sm.id, sm.source_category::text AS responsibility, sm.url, sm.host,
                   sm.verification_status::text AS status,
                   sm.publication_eligibility::text AS eligibility,
                   sm.promoted_source_id, who.display_name AS verified_by_name,
                   pcs.source_ref, pcs.acquisition_source_id
              FROM source_mapping sm
              LEFT JOIN app_user who ON who.id = sm.verified_by
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
             WHERE sm.target_institution_id = :inst
             ORDER BY pcs.source_ref
            """
        ),
        {"inst": institution_id},
    ).all()

    cards: list[ResponsibilityCard] = []
    for r in rows:
        mapping_id = uuid.UUID(str(r.id))
        authority = redirect_authority.for_mapping(connection, mapping_id)
        bound, blocker = False, None
        if expect_sha256:
            try:
                require_responsibility_binding(
                    connection,
                    mapping_id=mapping_id,
                    expect_sha256=expect_sha256,
                    directory=directory,
                )
                bound = True
            except (ResponsibilityBindingError, ManifestChangedError) as exc:
                blocker = str(exc).splitlines()[0]
        else:
            blocker = "no manifest digest supplied, so the binding was not checked"
        has_snapshot = authority.verdict is not redirect_authority.AuthorityVerdict.NO_EVIDENCE
        cards.append(
            ResponsibilityCard(
                mapping_id=mapping_id,
                source_ref=str(r.source_ref) if r.source_ref else "",
                responsibility=str(r.responsibility),
                requested_url=str(r.url),
                effective_url=authority.effective_url,
                requested_host=authority.requested.host,
                requested_host_authority=authority.requested.display,
                effective_host=authority.effective.host,
                effective_host_authority=authority.effective.display,
                redirected=authority.redirected,
                body_available=has_snapshot,
                verification_status=str(r.status),
                publication_eligibility=str(r.eligibility),
                verified_by_name=(str(r.verified_by_name) if r.verified_by_name else None),
                promoted_source_id=(
                    uuid.UUID(str(r.promoted_source_id)) if r.promoted_source_id else None
                ),
                manifest_bound=bound,
                manifest_blocker=blocker,
            )
        )
    return cards


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def audit_trail(
    connection: Connection, institution_id: uuid.UUID | None = None, *, limit: int = 200
) -> list[AuditRow]:
    """Section P. The trail, grouped and labelled, with no credential material.

    `audit_log` carries no secrets by construction -- `before_state`/`after_state` hold
    state names and ids -- but the selection is explicit rather than `SELECT *` so that a
    future column cannot start appearing in a browser because nobody revisited this.
    """
    params: dict[str, Any] = {"limit": limit}
    if institution_id is not None:
        params["inst"] = institution_id
    rows = connection.execute(
        text(_AUDIT_SQL_FOR_INSTITUTION if institution_id is not None else _AUDIT_SQL_ALL),
        params,
    ).all()
    return [
        AuditRow(
            seq=int(r.seq),
            occurred_at=r.occurred_at,
            action=str(r.action),
            group=AUDIT_GROUPS.get(str(r.action), "Other"),
            actor_name=(str(r.actor_name) if r.actor_name else None),
            actor_type=str(r.actor_type),
            object_type=str(r.object_type),
            object_id=(uuid.UUID(str(r.object_id)) if r.object_id else None),
            source_ref=(str(r.source_ref) if r.source_ref else None),
            before_state=r.before_state,
            after_state=r.after_state,
            reason=(str(r.reason) if r.reason else None),
            # The six historical rows: registration wrongly filed under the decision
            # action. Detectable exactly as documented -- by what the after-state carries.
            is_historical_registration=(
                str(r.action) == "PILOT_SOURCE_VERIFY"
                and isinstance(r.after_state, dict)
                and "promoted_source_mapping_id" in r.after_state
            ),
        )
        for r in rows
    ]


__all__ = [
    "APPROVED_MANIFESTS",
    "AUDIT_GROUPS",
    "AuditRow",
    "Dashboard",
    "DomainRow",
    "InstitutionRow",
    "ManifestStatus",
    "PilotSourceRow",
    "ResponsibilityCard",
    "SafetyCounts",
    "TrustCounts",
    "audit_trail",
    "dashboard",
    "domains_of",
    "institutions",
    "manifest_status",
    "pilot_sources_of",
    "responsibility_cards",
]
