"""Manual verification of official domains and mapped sources.

Every function here is a *recorded human decision*. There is no automatic
verification path in this module or anywhere else: nothing promotes a host out of
`CANDIDATE` except a call made on a named person's behalf, with a reason.

LOCK ORDER -- LOAD-BEARING, NOT INCIDENTAL
==========================================
Every operation in this module takes locks in one direction only::

    official_domain  ->  target_institution  ->  source_mapping  ->  audit_chain_head

Concretely:

1. the host row(s) in `official_domain`, with ``SELECT ... FOR UPDATE``. When an
   operation touches two of them (a replacement), they are locked **ordered by
   primary key**, so two reviewers replacing hosts in opposite directions cannot
   deadlock against each other;
2. the target institution, when its onboarding status advances;
3. the affected `source_mapping` rows -- which is why `verify_source_mapping` locks
   the host *before* the mapping even though the mapping is the row being changed:
   `reject_domain` goes host-then-mappings, and a second caller going
   mapping-then-host would close the cycle;
4. the audit chain, last, by inserting into `audit_log` -- whose trigger locks the
   singleton `audit_chain_head` row.

The `source_mapping_requires_trusted_host` trigger takes a ``FOR SHARE`` lock on
`official_domain`, which fits inside this order rather than adding to it: by the time
a mapping is written, its host is already held.

The reason the audit append is last is that its lock is the most contended in the
system: `audit_chain_head` serialises every consequential action, and the lock is
held to end of transaction (C18). Taking it first would make every verification wait
behind every other write in the system. Taking it last bounds the hold time to the
remainder of this transaction.

The reason the order must be *consistent* is deadlock. If one code path locked the
audit head before a domain row and another locked them the other way round, two
concurrent transactions would form a cycle. Every writer in this codebase therefore
appends to the audit chain last, and this module is not an exception to that rule.

NO FRONTEND YET
===============
These are service functions. Wiring them to HTTP endpoints and a review console is
a later step; exposing them now would mean shipping a review UI against an
unreviewed API surface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Connection, func, insert, select, update

from app.core.logging import get_logger
from app.db.enums import (
    ActorType,
    DomainVerificationMethod,
    OfficialVerificationStatus,
    OnboardingStatus,
    OnboardingVerificationAction,
)
from app.domains.onboarding.models import (
    OfficialDomain,
    SourceMapping,
    TargetInstitution,
)
from app.domains.versioning.models import AuditLog

logger = get_logger(__name__)


class VerificationRefusedError(RuntimeError):
    """The decision cannot be recorded as asked. The message says why."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making the decision. A verification with no actor is not a verification."""

    user_id: uuid.UUID
    actor_type: ActorType = ActorType.USER


def verify_domain(
    connection: Connection,
    *,
    domain_id: uuid.UUID,
    actor: Actor,
    method: DomainVerificationMethod,
    evidence: str,
    reason: str,
    covers_subdomains: bool = False,
    verified_at: datetime | None = None,
) -> None:
    """Record that a host was confirmed to be officially the institution's.

    `method` and `evidence` are mandatory. "It looked right" is not a method, which
    is why `DomainVerificationMethod` has no member for it: a reviewer must name a
    registry, a certificate, a page or their own documented review.

    This function will not verify a host as official when the operator meant
    "authorised third party" -- that is `authorize_external_domain`, which demands an
    authorisation reference. Keeping them separate is what stops a SaaS application
    portal becoming `VERIFIED_OFFICIAL` because it was convenient.
    """
    if not evidence.strip():
        raise VerificationRefusedError("verification evidence is required")
    if not reason.strip():
        raise VerificationRefusedError("a reason is required")

    row = _lock_domain(connection, domain_id)
    before = _domain_state(row)

    if row.verification_status == OfficialVerificationStatus.REJECTED:
        raise VerificationRefusedError(
            "this host was rejected; register a fresh candidate rather than "
            "reversing a rejection in place, so the rejection stays on the record"
        )

    when = verified_at or _now(connection)
    connection.execute(
        update(OfficialDomain)
        .where(OfficialDomain.id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.VERIFIED_OFFICIAL.value,
            verification_method=method.value,
            verification_evidence=evidence.strip(),
            authorization_reference=None,
            verified_at=when,
            verified_by=actor.user_id,
            covers_subdomains=covers_subdomains,
            is_active=True,
            rejected_reason=None,
        )
    )
    _advance_institution(connection, row.target_institution_id)
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.VERIFY,
        object_type="official_domain",
        object_id=domain_id,
        before=before,
        after={
            "verification_status": OfficialVerificationStatus.VERIFIED_OFFICIAL.value,
            "verification_method": method.value,
            "verification_evidence": evidence.strip(),
            "covers_subdomains": covers_subdomains,
        },
        reason=reason,
    )


def authorize_external_domain(
    connection: Connection,
    *,
    domain_id: uuid.UUID,
    actor: Actor,
    authorization_reference: str,
    evidence: str,
    reason: str,
) -> None:
    """Record that a third-party host is authorised by the institution.

    Application portals, fee calculators and prospectus hosts legitimately carry
    official information while not being the university's own domain. They reach
    `AUTHORIZED_EXTERNAL` and no further.

    `authorization_reference` records *why* we concluded the institution sanctioned
    it -- the page that delegates to it, a contract, a written confirmation. Being
    linked from an official page is not sufficient on its own and the reviewer is
    expected to say what they actually relied on.
    """
    if not authorization_reference.strip():
        raise VerificationRefusedError(
            "an authorisation reference is required: name the delegation, contract "
            "or confirmation relied on. A link from an official page is not enough."
        )
    if not evidence.strip():
        raise VerificationRefusedError("verification evidence is required")

    row = _lock_domain(connection, domain_id)
    before = _domain_state(row)

    connection.execute(
        update(OfficialDomain)
        .where(OfficialDomain.id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value,
            verification_method=(DomainVerificationMethod.AUTHORIZED_PARTNER_AGREEMENT.value),
            verification_evidence=evidence.strip(),
            authorization_reference=authorization_reference.strip(),
            verified_at=_now(connection),
            verified_by=actor.user_id,
            is_active=True,
            rejected_reason=None,
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.VERIFY,
        object_type="official_domain",
        object_id=domain_id,
        before=before,
        after={
            "verification_status": OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value,
            "authorization_reference": authorization_reference.strip(),
        },
        reason=reason,
    )


def reject_domain(
    connection: Connection, *, domain_id: uuid.UUID, actor: Actor, reason: str
) -> None:
    """Record that a candidate host is not the institution's.

    The row is kept, inactive. Keeping rejections is what stops the same wrong
    domain being re-proposed every time someone searches for the institution.
    """
    if not reason.strip():
        raise VerificationRefusedError("a rejection reason is required")

    row = _lock_domain(connection, domain_id)
    before = _domain_state(row)

    connection.execute(
        update(OfficialDomain)
        .where(OfficialDomain.id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.REJECTED.value,
            rejected_reason=reason.strip(),
            authorization_reference=None,
            is_active=False,
        )
    )
    # Any mapping that relied on this host loses its basis and is deactivated with
    # it: a source vouched for by a rejected domain must not stay collectable.
    connection.execute(
        update(SourceMapping)
        .where(SourceMapping.official_domain_id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.REJECTED.value,
            rejected_reason=f"host rejected: {reason.strip()}",
            is_active=False,
            deactivated_reason="the host this source relied on was rejected",
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.REJECT,
        object_type="official_domain",
        object_id=domain_id,
        before=before,
        after={"verification_status": OfficialVerificationStatus.REJECTED.value},
        reason=reason,
    )


def replace_domain(
    connection: Connection,
    *,
    domain_id: uuid.UUID,
    replacement_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> None:
    """Record that one host supersedes another.

    Both rows are locked, **ordered by id**, so two reviewers replacing hosts in
    opposite directions cannot deadlock. The old host becomes `LEGACY` and points at
    its successor; nothing is deleted, so a snapshot captured from the old host
    keeps a registry entry that explains where it came from.
    """
    if domain_id == replacement_id:
        raise VerificationRefusedError("a host cannot replace itself")
    if not reason.strip():
        raise VerificationRefusedError("a reason is required")

    first, second = sorted([domain_id, replacement_id], key=str)
    locked = {row.id: row for row in _lock_domains(connection, [first, second])}
    if domain_id not in locked or replacement_id not in locked:
        missing = [str(i) for i in (domain_id, replacement_id) if i not in locked]
        raise VerificationRefusedError(f"no such official_domain: {', '.join(missing)}")

    old, new = locked[domain_id], locked[replacement_id]
    if new.verification_status not in (
        OfficialVerificationStatus.VERIFIED_OFFICIAL,
        OfficialVerificationStatus.AUTHORIZED_EXTERNAL,
    ):
        raise VerificationRefusedError(
            f"the replacement host {new.host!r} is {new.verification_status.value}; "
            "verify it before using it to supersede another host"
        )

    connection.execute(
        update(OfficialDomain)
        .where(OfficialDomain.id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.LEGACY.value,
            superseded_by_id=replacement_id,
            authorization_reference=None,
            is_active=False,
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.REPLACE,
        object_type="official_domain",
        object_id=domain_id,
        before=_domain_state(old),
        after={
            "verification_status": OfficialVerificationStatus.LEGACY.value,
            "superseded_by_id": str(replacement_id),
            "superseded_by_host": new.host,
        },
        reason=reason,
    )


def mark_domain_legacy(
    connection: Connection, *, domain_id: uuid.UUID, actor: Actor, reason: str
) -> None:
    """Record that a host is retired but historically real.

    Distinct from `REJECTED`: a legacy host *was* official. Evidence gathered from it
    stays valid for the period it was current, which is why the distinction is
    stored rather than collapsed into "not usable".
    """
    if not reason.strip():
        raise VerificationRefusedError("a reason is required")

    row = _lock_domain(connection, domain_id)
    before = _domain_state(row)

    connection.execute(
        update(OfficialDomain)
        .where(OfficialDomain.id == domain_id)
        .values(
            verification_status=OfficialVerificationStatus.LEGACY.value,
            authorization_reference=None,
            is_active=False,
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.MARK_LEGACY,
        object_type="official_domain",
        object_id=domain_id,
        before=before,
        after={"verification_status": OfficialVerificationStatus.LEGACY.value},
        reason=reason,
    )


def request_review(
    connection: Connection,
    *,
    target_institution_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> None:
    """Escalate a target whose official identity could not be settled.

    A deliberate dead end rather than a guess. An institution parked here stays out
    of collection until a human resolves it.
    """
    if not reason.strip():
        raise VerificationRefusedError("a reason is required")

    row = connection.execute(
        select(
            TargetInstitution.id,
            TargetInstitution.onboarding_status,
            TargetInstitution.blocked_reason,
        )
        .where(TargetInstitution.id == target_institution_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise VerificationRefusedError(f"no such target_institution: {target_institution_id}")

    connection.execute(
        update(TargetInstitution)
        .where(TargetInstitution.id == target_institution_id)
        .values(
            onboarding_status=OnboardingStatus.NEEDS_MANUAL_REVIEW.value,
            blocked_reason=reason.strip(),
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.REQUEST_REVIEW,
        object_type="target_institution",
        object_id=target_institution_id,
        before={
            "onboarding_status": _enum_value(row.onboarding_status),
            "blocked_reason": row.blocked_reason,
        },
        after={"onboarding_status": OnboardingStatus.NEEDS_MANUAL_REVIEW.value},
        reason=reason,
    )


def verify_source_mapping(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> None:
    """Record that a mapped URL is confirmed to carry official information.

    Refused unless the mapping's host is already a trusted registry entry. That
    check is also enforced by a database trigger; doing it here as well gives the
    operator a comprehensible message instead of a constraint violation.

    **Lock order: the host first, then the mapping.** This is the same direction
    `reject_domain` takes, and taking it in the other order here would close a
    deadlock cycle -- rejection locks a domain and then updates its mappings, so a
    concurrent verification holding the mapping and waiting for the domain would
    wedge both transactions.
    """
    if not reason.strip():
        raise VerificationRefusedError("a reason is required")

    # Unlocked read, purely to discover which host to lock. Whether it is still the
    # mapping's host is re-checked below, under the locks.
    domain_id = connection.execute(
        select(SourceMapping.official_domain_id).where(SourceMapping.id == mapping_id)
    ).one_or_none()
    if domain_id is None:
        raise VerificationRefusedError(f"no such source_mapping: {mapping_id}")
    if domain_id[0] is None:
        raise VerificationRefusedError(
            "this source's host is not registered in official_domain; register and "
            "verify the host before verifying a source on it"
        )

    domain = connection.execute(
        select(
            OfficialDomain.id,
            OfficialDomain.verification_status,
            OfficialDomain.host,
            OfficialDomain.is_active,
        )
        .where(OfficialDomain.id == domain_id[0])
        .with_for_update()
    ).one()

    row = connection.execute(
        select(
            SourceMapping.id,
            SourceMapping.host,
            SourceMapping.verification_status,
            SourceMapping.official_domain_id,
            SourceMapping.target_institution_id,
        )
        .where(SourceMapping.id == mapping_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise VerificationRefusedError(f"no such source_mapping: {mapping_id}")

    # The mapping was retargeted between the two reads. Rare, and the safe response
    # is to make the operator retry rather than to verify against a host we did not
    # lock or check.
    if row.official_domain_id != domain.id:
        raise VerificationRefusedError(
            "this source's host changed while the decision was being recorded; "
            "review it again and retry"
        )

    if domain.verification_status not in (
        OfficialVerificationStatus.VERIFIED_OFFICIAL,
        OfficialVerificationStatus.AUTHORIZED_EXTERNAL,
    ):
        raise VerificationRefusedError(
            f"the host {domain.host!r} is {domain.verification_status.value}; a source "
            "cannot be verified as official on an unverified host"
        )
    if not domain.is_active:
        raise VerificationRefusedError(
            f"the host {domain.host!r} is inactive; a source on a retired host cannot "
            "be verified"
        )

    status = (
        OfficialVerificationStatus.AUTHORIZED_EXTERNAL
        if domain.verification_status == OfficialVerificationStatus.AUTHORIZED_EXTERNAL
        else OfficialVerificationStatus.VERIFIED_OFFICIAL
    )
    connection.execute(
        update(SourceMapping)
        .where(SourceMapping.id == mapping_id)
        .values(
            verification_status=status.value,
            verified_at=_now(connection),
            verified_by=actor.user_id,
            rejected_reason=None,
            is_active=True,
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.VERIFY,
        object_type="source_mapping",
        object_id=mapping_id,
        before={"verification_status": _enum_value(row.verification_status)},
        after={"verification_status": status.value},
        reason=reason,
    )


def reject_source_mapping(
    connection: Connection, *, mapping_id: uuid.UUID, actor: Actor, reason: str
) -> None:
    """Record that a mapped URL does not carry the information it was mapped for."""
    if not reason.strip():
        raise VerificationRefusedError("a rejection reason is required")

    row = connection.execute(
        select(SourceMapping.id, SourceMapping.verification_status)
        .where(SourceMapping.id == mapping_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise VerificationRefusedError(f"no such source_mapping: {mapping_id}")

    connection.execute(
        update(SourceMapping)
        .where(SourceMapping.id == mapping_id)
        .values(
            verification_status=OfficialVerificationStatus.REJECTED.value,
            rejected_reason=reason.strip(),
            is_active=False,
            deactivated_reason=reason.strip(),
        )
    )
    _append_audit(
        connection,
        actor=actor,
        action=OnboardingVerificationAction.REJECT,
        object_type="source_mapping",
        object_id=mapping_id,
        before={"verification_status": _enum_value(row.verification_status)},
        after={"verification_status": OfficialVerificationStatus.REJECTED.value},
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _lock_domain(connection: Connection, domain_id: uuid.UUID) -> Any:
    rows = _lock_domains(connection, [domain_id])
    if not rows:
        raise VerificationRefusedError(f"no such official_domain: {domain_id}")
    return rows[0]


def _lock_domains(connection: Connection, ids: list[uuid.UUID]) -> list[Any]:
    """Lock domain rows, ordered by id so concurrent callers cannot deadlock."""
    return list(
        connection.execute(
            select(
                OfficialDomain.id,
                OfficialDomain.host,
                OfficialDomain.verification_status,
                OfficialDomain.verification_method,
                OfficialDomain.covers_subdomains,
                OfficialDomain.is_active,
                OfficialDomain.target_institution_id,
                OfficialDomain.university_id,
            )
            .where(OfficialDomain.id.in_(ids))
            .order_by(OfficialDomain.id)
            .with_for_update()
        ).all()
    )


def _domain_state(row: Any) -> dict[str, object]:
    return {
        "host": row.host,
        "verification_status": _enum_value(row.verification_status),
        "verification_method": _enum_value(row.verification_method),
        "covers_subdomains": row.covers_subdomains,
        "is_active": row.is_active,
    }


def _advance_institution(connection: Connection, target_institution_id: uuid.UUID | None) -> None:
    """Move a target to DOMAIN_VERIFIED once one of its hosts is verified.

    Only from the earlier states. A target already in source mapping or collection
    is not moved backwards, and a blocked one is not quietly unblocked: clearing a
    block is its own decision.
    """
    if target_institution_id is None:
        return
    connection.execute(
        update(TargetInstitution)
        .where(
            TargetInstitution.id == target_institution_id,
            TargetInstitution.onboarding_status.in_(
                [
                    OnboardingStatus.NOT_STARTED.value,
                    OnboardingStatus.IDENTITY_VERIFICATION.value,
                    OnboardingStatus.DOMAIN_CANDIDATE.value,
                ]
            ),
        )
        .values(onboarding_status=OnboardingStatus.DOMAIN_VERIFIED.value)
    )


def _append_audit(
    connection: Connection,
    *,
    actor: Actor,
    action: OnboardingVerificationAction,
    object_type: str,
    object_id: uuid.UUID,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    reason: str,
) -> None:
    """Append to the hash chain. Always the last write of the operation."""
    connection.execute(
        insert(AuditLog).values(
            actor_type=actor.actor_type.value,
            actor_id=actor.user_id,
            action=action.value,
            object_type=object_type,
            object_id=object_id,
            before_state=before,
            after_state=after,
            reason=reason.strip(),
        )
    )
    logger.info(
        "onboarding_verification_recorded",
        action=action.value,
        object_type=object_type,
        object_id=str(object_id),
        actor_id=str(actor.user_id),
    )


def _now(connection: Connection) -> datetime:
    """Database time, so a verification timestamp matches the audit row's.

    Using the application clock here would let a skewed worker record a verification
    as happening before the audit entry that describes it.
    """
    return connection.execute(select(func.now())).scalar_one()


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


__all__ = [
    "Actor",
    "VerificationRefusedError",
    "authorize_external_domain",
    "mark_domain_legacy",
    "reject_domain",
    "reject_source_mapping",
    "replace_domain",
    "request_review",
    "verify_domain",
    "verify_source_mapping",
]
