"""Triage of collected source candidates (U15).

Every function here records a **human decision**. There is no automatic path out of
`PENDING`: no function in this module, or anywhere else, moves a candidate to
`VERIFIED` without an actor and a reason, and the database agrees --
`decision_records_actor_time_and_reason` refuses any non-`PENDING` row that lacks
all three.

WHAT `VERIFIED` MEANS HERE, AND WHAT IT DOES NOT
================================================
It means: *a reviewer looked at this URL and thinks it is worth registering as a
source for this institution.* It does not mean the URL is an official source, and
nothing downstream may treat it as one. The promotion path is unchanged:

    collected candidate  ->  official_domain verified  ->  source_mapping promoted
                         ->  source earns its eligibility class

`register_verified_candidate` performs only the third arrow, and refuses unless the
second has already happened. The fourth is C27's `source_eligibility_is_earned`
trigger, which will not let a `source` be labelled `OFFICIAL_VERIFIED` unless a
promoted mapping already points at it. **A source is never born verified**, and this
module has no way to make one.

THE HOSTNAME IS EVIDENCE
========================
`candidate_detail` returns whether the host falls under a domain already verified for
*this* institution. That is the most useful single fact to put in front of a
reviewer, and the most tempting to automate on. It is not automated on, because a
matching hostname says where a page lives, not what it is: a news article, a student
society page and a personal staff page all match every hostname test there is.

So `verify_candidate` requires a reason whatever the hostname says, and there is no
`verify_all_matching_hosts` function. Its absence is the feature.

LOCK ORDER
==========
Unchanged from `onboarding/verification.py`, and for the same reason::

    pilot_collected_source  ->  official_domain  ->  source_mapping
                            ->  audit_chain_head

The audit append is last in every operation, because `audit_chain_head` serialises
every consequential write in the system (C18) and its lock is held to end of
transaction. Taking it first would put every verification behind every other write.
The order must be *consistent* across modules or two transactions form a cycle; this
module is not an exception to that rule.

NO FRONTEND
===========
Service functions and a CLI report. A review console against an unreviewed API
surface is a later step.
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
    UNCLASSIFIED_SOURCE_TYPE,
    ActorType,
    OfficialVerificationStatus,
    PilotCandidateAction,
    SourceCandidateState,
    SourceCategory,
)
from app.domains.onboarding.models import OfficialDomain, SourceMapping
from app.domains.pilot.models import PilotCollectedSource
from app.domains.pilot.queue import QUEUE_VIEW as _QUEUE
from app.domains.pilot.queue import CandidateDetail, DomainEvidence, row_to_detail
from app.domains.versioning.models import AuditLog

logger = get_logger(__name__)


class CandidateDecisionRefusedError(RuntimeError):
    """The decision cannot be recorded as asked. The message says why."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is deciding. A verification with no actor is not a verification."""

    user_id: uuid.UUID
    actor_type: ActorType = ActorType.USER


def verify_candidate(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    actor: Actor,
    reason: str,
    decided_at: datetime | None = None,
) -> None:
    """Record that a reviewer accepts this URL as worth registering.

    Deliberately does **not** register it. Registration is
    `register_verified_candidate`, which needs a verified `official_domain` to hang
    the mapping off. Splitting them keeps "I have read this page and it is the right
    one" separate from "this institution's domain has been confirmed", which are two
    different claims by two possibly different people.
    """
    _record(
        connection,
        candidate_id=candidate_id,
        actor=actor,
        reason=reason,
        state=SourceCandidateState.VERIFIED,
        action=PilotCandidateAction.VERIFY,
        decided_at=decided_at,
    )


def reject_candidate(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    actor: Actor,
    reason: str,
    decided_at: datetime | None = None,
) -> None:
    """Record that this URL will not be used. The row stays, and so does the reason.

    Kept rather than deleted, for the same reason a rejected `official_domain` is
    kept: the rejection is what stops the same wrong page being proposed again next
    time, and a deleted row teaches nobody anything.
    """
    _record(
        connection,
        candidate_id=candidate_id,
        actor=actor,
        reason=reason,
        state=SourceCandidateState.REJECTED,
        action=PilotCandidateAction.REJECT,
        decided_at=decided_at,
    )


def flag_candidate_for_review(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    actor: Actor,
    reason: str,
    decided_at: datetime | None = None,
) -> None:
    """Record that this one needs a second opinion.

    Exists so "I am not sure" has somewhere to go. Without it the honest reviewer's
    only options are to guess or to leave the row `PENDING`, and a `PENDING` row that
    has actually been looked at twice is indistinguishable from one nobody has opened.
    """
    _record(
        connection,
        candidate_id=candidate_id,
        actor=actor,
        reason=reason,
        state=SourceCandidateState.NEEDS_REVIEW,
        action=PilotCandidateAction.NEEDS_REVIEW,
        decided_at=decided_at,
    )


def classify_candidate(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    source_category: str,
    actor: Actor,
    reason: str,
) -> None:
    """Say what an `UNCLASSIFIED` page is. A recorded decision, like any other.

    The official-source list supplies an "Important Additional Source" column with no
    category, so a page can arrive that nobody has described. Guessing from the column
    it came in would be inventing a claim -- and one of the three genuinely new pages
    in the client's file is a fee-rates table, not the PDF the column name suggests.

    Classification is deliberately separate from verification: `is this a real page of
    the university's?` and `is this the authority for tuition?` are different
    questions, and a CHECK refuses to let an unclassified row reach `VERIFIED` so the
    second cannot be answered before the first has been asked.
    """
    if not reason.strip():
        raise CandidateDecisionRefusedError("a reason is required")
    if source_category == UNCLASSIFIED_SOURCE_TYPE:
        raise CandidateDecisionRefusedError(
            "classifying a page as UNCLASSIFIED is not a classification; "
            "reject it instead if it is not a page we want"
        )
    if source_category not in {category.value for category in SourceCategory}:
        raise CandidateDecisionRefusedError(
            f"{source_category!r} is not a source category; "
            f"expected one of {', '.join(sorted(c.value for c in SourceCategory))}"
        )

    row = _lock_candidate(connection, candidate_id)
    before = {"source_type": row.source_type}
    connection.execute(
        update(PilotCollectedSource)
        .where(PilotCollectedSource.id == candidate_id)
        .values(source_type=source_category)
    )
    _append_audit(
        connection,
        actor=actor,
        action=PilotCandidateAction.CLASSIFY,
        object_type="pilot_collected_source",
        object_id=candidate_id,
        before=before,
        after={"source_type": source_category},
        reason=reason,
    )


def register_verified_candidate(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    source_category: str,
    actor: Actor,
    reason: str,
    collection_priority: int = 3,
) -> uuid.UUID:
    """Create the `source_mapping` for a verified candidate. Returns its id.

    Three refusals, each protecting a different rule:

    * an unverified candidate cannot be registered -- the human decision comes first;
    * a candidate whose host has no verified `official_domain` for **this**
      institution cannot be registered. `source_mapping_requires_trusted_host` would
      refuse it anyway; failing here says why, in a sentence a reviewer can act on;
    * a candidate already registered cannot be registered twice, because the second
      mapping would make the same page look like two independent sources.

    `collection_priority` is 1 (highest) to 5; the default matches
    `source_mapping`'s own, so registering a page does not quietly reprioritise it.

    The mapping is created `CANDIDATE`, never verified. Promotion to a `source` with
    an eligibility class is a further, separate step -- C27 will not accept
    `OFFICIALLY_VERIFIED` eligibility on a source no promoted mapping points at.
    """
    if not reason.strip():
        raise CandidateDecisionRefusedError("a reason is required")

    row = _lock_candidate(connection, candidate_id)
    if row.verification_state != SourceCandidateState.VERIFIED.value:
        raise CandidateDecisionRefusedError(
            f"candidate is {row.verification_state}; verify it before registering it"
        )
    if row.promoted_source_mapping_id is not None:
        raise CandidateDecisionRefusedError(
            f"already registered as source_mapping {row.promoted_source_mapping_id}"
        )

    domain = _domain_for(connection, row.target_institution_id, row.host)
    if domain.matched_domain_host is None or not (
        domain.matches_verified_domain or domain.matches_authorized_domain
    ):
        raise CandidateDecisionRefusedError(
            f"no verified official domain covers {row.host!r} for this institution. "
            "Verify the host first -- a mapped source must hang off a trusted domain."
        )

    domain_id = connection.execute(
        select(OfficialDomain.id).where(
            OfficialDomain.target_institution_id == row.target_institution_id,
            OfficialDomain.host == domain.matched_domain_host,
            OfficialDomain.is_active.is_(True),
        )
    ).scalar_one()

    mapping_id = uuid.uuid4()
    connection.execute(
        insert(SourceMapping).values(
            id=mapping_id,
            target_institution_id=row.target_institution_id,
            source_category=source_category,
            url=row.official_url,
            normalized_url=row.normalized_url,
            url_sha256=row.url_sha256,
            host=row.host,
            official_domain_id=domain_id,
            # CANDIDATE, not verified. Mapping a page is not the same act as
            # confirming it is the page it claims to be.
            verification_status=OfficialVerificationStatus.CANDIDATE.value,
            collection_priority=collection_priority,
            # The person registering it, not the workbook: `discovered_by` names an
            # `app_user`, and a mapping that cannot say who put it there is the kind
            # of row nobody will later be willing to act on.
            discovered_by=actor.user_id,
            notes=(
                f"Registered from pilot submission {row.submission_id} "
                f"{row.source_ref}. {reason.strip()}"
            ),
        )
    )
    connection.execute(
        update(PilotCollectedSource)
        .where(PilotCollectedSource.id == candidate_id)
        .values(promoted_source_mapping_id=mapping_id)
    )

    _append_audit(
        connection,
        actor=actor,
        # REGISTERED, not VERIFY. Creating the mapping is a consequence of a decision
        # already recorded, not a second decision, and naming it `PILOT_SOURCE_VERIFY`
        # made an audit reader count twelve judgements where six were made. The six
        # historical rows that used VERIFY are left exactly as they are -- see
        # `PilotCandidateAction`.
        action=PilotCandidateAction.REGISTERED,
        object_type="pilot_collected_source",
        object_id=candidate_id,
        before={"promoted_source_mapping_id": None},
        after={"promoted_source_mapping_id": str(mapping_id), "source_category": source_category},
        reason=reason,
    )
    return mapping_id


def candidate_detail(connection: Connection, candidate_id: uuid.UUID) -> CandidateDetail:
    """One candidate as the reviewer should see it, hostname evidence included."""
    row = connection.execute(
        select(_QUEUE).where(_QUEUE.c.candidate_id == candidate_id)
    ).one_or_none()
    if row is None:
        raise CandidateDecisionRefusedError(f"no such candidate: {candidate_id}")
    return row_to_detail(row)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _record(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    actor: Actor,
    reason: str,
    state: SourceCandidateState,
    action: PilotCandidateAction,
    decided_at: datetime | None,
) -> None:
    if not reason.strip():
        raise CandidateDecisionRefusedError(
            "a reason is required -- a decision nobody can explain later is not a decision"
        )

    row = _lock_candidate(connection, candidate_id)
    if state is SourceCandidateState.VERIFIED and row.source_type == UNCLASSIFIED_SOURCE_TYPE:
        raise CandidateDecisionRefusedError(
            "this page has no category yet. Classify it first (classify_candidate), "
            "or reject it -- verifying asserts it is authoritative for something, and "
            "there is nothing yet for it to be authoritative for."
        )
    before = {
        "verification_state": row.verification_state,
        "verified_by": str(row.verified_by) if row.verified_by else None,
        "verification_reason": row.verification_reason,
    }

    if row.promoted_source_mapping_id is not None and state is not SourceCandidateState.VERIFIED:
        raise CandidateDecisionRefusedError(
            "this candidate is already registered as a source mapping; deactivate the "
            "mapping rather than un-verifying the row it came from"
        )

    when = decided_at or _now(connection)
    connection.execute(
        update(PilotCollectedSource)
        .where(PilotCollectedSource.id == candidate_id)
        .values(
            verification_state=state.value,
            verified_at=when,
            verified_by=actor.user_id,
            verification_reason=reason.strip(),
        )
    )

    _append_audit(
        connection,
        actor=actor,
        action=action,
        object_type="pilot_collected_source",
        object_id=candidate_id,
        before=before,
        after={
            "verification_state": state.value,
            "verified_by": str(actor.user_id),
            "verification_reason": reason.strip(),
        },
        reason=reason,
    )


def _lock_candidate(connection: Connection, candidate_id: uuid.UUID) -> Any:
    row = connection.execute(
        select(PilotCollectedSource)
        .where(PilotCollectedSource.id == candidate_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise CandidateDecisionRefusedError(f"no such candidate: {candidate_id}")
    return row


def _domain_for(connection: Connection, institution_id: uuid.UUID, host: str) -> DomainEvidence:
    """The strongest active domain for this institution covering this host.

    Only this institution's domains are considered. No host is matched because it
    resembles an institution's name, and no domain belonging to another institution
    is ever consulted -- the same refusal `pilot_matching.py` makes for names.
    """
    row = connection.execute(
        select(
            OfficialDomain.host,
            OfficialDomain.verification_status,
            OfficialDomain.covers_subdomains,
        )
        .where(
            OfficialDomain.target_institution_id == institution_id,
            OfficialDomain.is_active.is_(True),
        )
        .order_by(OfficialDomain.host)
    ).all()

    best: tuple[int, int, Any] | None = None
    rank = {
        OfficialVerificationStatus.VERIFIED_OFFICIAL.value: 0,
        OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value: 1,
        OfficialVerificationStatus.CANDIDATE.value: 2,
        OfficialVerificationStatus.LEGACY.value: 3,
    }
    for candidate in row:
        domain_host = candidate.host
        status = _enum_value(candidate.verification_status)
        covered = domain_host == host or (
            bool(candidate.covers_subdomains) and host.endswith("." + domain_host)
        )
        if not covered:
            continue
        key = (rank.get(str(status), 4), -len(domain_host), candidate)
        if best is None or key[:2] < best[:2]:
            best = key

    if best is None:
        return DomainEvidence(
            host=host,
            matched_domain_host=None,
            domain_verification_status=None,
            covers_subdomains=None,
        )
    matched = best[2]
    return DomainEvidence(
        host=host,
        matched_domain_host=matched.host,
        domain_verification_status=str(_enum_value(matched.verification_status)),
        covers_subdomains=bool(matched.covers_subdomains),
    )


def _append_audit(
    connection: Connection,
    *,
    actor: Actor,
    action: PilotCandidateAction,
    object_type: str,
    object_id: uuid.UUID,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    reason: str,
) -> None:
    """Append to the hash chain. Always the last write of the operation (C18)."""
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
        "pilot_candidate_decision_recorded",
        action=action.value,
        candidate_id=str(object_id),
        actor_id=str(actor.user_id),
    )


def _now(connection: Connection) -> datetime:
    """Database time, so the decision timestamp matches the audit row's."""
    return connection.execute(select(func.now())).scalar_one()


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


__all__ = [
    "Actor",
    "CandidateDecisionRefusedError",
    "candidate_detail",
    "classify_candidate",
    "flag_candidate_for_review",
    "register_verified_candidate",
    "reject_candidate",
    "verify_candidate",
]
