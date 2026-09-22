"""Manual recovery of an acquisition source (Step 5B.2 sections 4, 17, 18).

WHY THIS MODULE HAD TO EXIST
============================
Before Step 5B.2 there was **no way back**. A worker could set
`fetch_eligibility = 'BLOCKED'`, and nothing in the system could set it to anything
else: registration is `ON CONFLICT DO NOTHING` by design, so re-importing the workbook
could not clear it either. Recovering a page meant hand-written SQL against
production, which is not a recovery procedure, it is an invitation to one.

WHAT RE-ENABLING DOES AND DOES NOT MEAN
=======================================
`reenable` means exactly one thing: *"a worker may send an HTTP request to this URL
again."* It does **not** mean the source is trusted, and it cannot be made to mean
that -- `publication_eligibility` is never written here, so C27's earned-eligibility
rule is untouched and a re-enabled source is as publication-ineligible as it was a
moment earlier. That separation is the reason acquisition and publication have
different columns at all, and collapsing them to make an operator's life easier would
undo the whole design.

Re-enabling also does not bypass any safety check. The next fetch resolves DNS,
classifies every answer, pins the connection and revalidates every redirect exactly as
before; if the URL still resolves to loopback, it will be refused again within
seconds. There is deliberately no command that says "fetch this without checking".

WHAT IS AUDITED, AND WHAT IS NOT
================================
Every function here appends to the hash chain, because every one of them is a person
overriding what the system concluded. Automatic cooldowns are **not** audited: a
cooldown expiring is the clock, not a decision, and recording thousands of them would
bury the handful of rows that are decisions (section 18).

The audit append is the **last** write of each operation. `audit_chain_head`
serialises every consequential action in the system, so taking that lock first would
hold it across the domain writes and turn one busy table into a global bottleneck
(C18).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.db.enums import AcquisitionRecoveryAction, ActorType

logger = get_logger(__name__)

#: The shortest reason we will accept. Not a formality: the audit row is what a later
#: reviewer reads to decide whether the override still holds, and "fixed" tells them
#: nothing at all.
MIN_REASON_LENGTH = 8


class RecoveryRefusedError(RuntimeError):
    """The transition was not allowed, and the message says why."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is deciding. A manual transition with no actor is not a decision."""

    user_id: uuid.UUID
    actor_type: ActorType = ActorType.USER


@dataclass(frozen=True, slots=True)
class SourceState:
    """Everything an operator needs to decide what to do about one page."""

    source_id: uuid.UUID
    url: str
    is_active: bool
    fetch_eligibility: str
    fetch_eligibility_reason: str | None
    publication_eligibility: str
    access_state: str
    cooldown_until: datetime | None
    cooldown_reason: str | None
    rate_limit_strikes: int
    host: str
    host_cooldown_until: datetime | None
    health: str
    schedule_state: str
    total_runs: int
    last_status: str | None
    last_http_status: int | None
    last_error_class: str | None

    @property
    def in_cooldown(self) -> bool:
        return self.schedule_state == "COOLDOWN"


def source_state(connection: Connection, source_id: uuid.UUID) -> SourceState:
    """Read-only. What the source's operational state is right now."""
    row = connection.execute(
        text(
            "SELECT h.source_id, h.url, h.is_active, h.fetch_eligibility, "
            "       s.fetch_eligibility_reason, h.publication_eligibility, h.access_state, "
            "       h.cooldown_until, h.cooldown_reason, h.rate_limit_strikes, "
            "       h.host_cooldown_until, h.health, h.schedule_state, h.total_runs, "
            "       h.last_status, h.last_http_status, h.last_error_class, "
            "       lower(split_part(split_part(h.url, '://', 2), '/', 1)) AS host "
            "  FROM source_health h JOIN source s ON s.id = h.source_id "
            " WHERE h.source_id = :id"
        ),
        {"id": source_id},
    ).one_or_none()
    if row is None:
        raise RecoveryRefusedError(f"no such acquisition source: {source_id}")
    return SourceState(
        source_id=row.source_id,
        url=row.url,
        is_active=row.is_active,
        fetch_eligibility=row.fetch_eligibility,
        fetch_eligibility_reason=row.fetch_eligibility_reason,
        publication_eligibility=row.publication_eligibility,
        access_state=row.access_state,
        cooldown_until=row.cooldown_until,
        cooldown_reason=row.cooldown_reason,
        rate_limit_strikes=row.rate_limit_strikes,
        host=row.host,
        host_cooldown_until=row.host_cooldown_until,
        health=row.health,
        schedule_state=row.schedule_state,
        total_runs=row.total_runs,
        last_status=row.last_status,
        last_http_status=row.last_http_status,
        last_error_class=row.last_error_class,
    )


def _check_reason(reason: str) -> str:
    cleaned = " ".join(reason.split())
    if len(cleaned) < MIN_REASON_LENGTH:
        raise RecoveryRefusedError(
            f"a reason of at least {MIN_REASON_LENGTH} characters is required; "
            "the audit row is what a later reviewer reads"
        )
    return cleaned[:400]


def _append_audit(
    connection: Connection,
    *,
    actor: Actor,
    action: AcquisitionRecoveryAction,
    source_id: uuid.UUID,
    before: dict[str, object],
    after: dict[str, object],
    reason: str,
) -> None:
    """Append to the hash chain. Always the last write of the operation (C18)."""
    connection.execute(
        text(
            "INSERT INTO audit_log (actor_type, actor_id, action, object_type, object_id, "
            "before_state, after_state, reason) "
            "VALUES (:actor_type, :actor_id, :action, 'source', :object_id, "
            "        cast(:before AS jsonb), cast(:after AS jsonb), :reason)"
        ),
        {
            "actor_type": actor.actor_type.value,
            "actor_id": actor.user_id,
            "action": action.value,
            "object_id": source_id,
            "before": _json(before),
            "after": _json(after),
            "reason": reason,
        },
    )


def _json(value: dict[str, object]) -> str:
    import json

    return json.dumps(value, default=str)


def reenable(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> SourceState:
    """Allow technical fetch attempts again. **Not** a statement of trust.

    Clears the cooldown and the strike count along with the eligibility, because
    leaving a stale cooldown behind would mean the operator's re-enable appeared to do
    nothing until it expired.

    `access_state` returns to `OK` as well: the operator is asserting that whatever
    the site was doing has been dealt with, and leaving it `BLOCKED` would make the
    re-enable invisible to the scheduler.

    `is_active` is deliberately *not* flipped here: a source deactivated at the
    catalogue level is a different decision, with its own reason, and
    `an_inactive_source_is_not_fetchable` would reject the combination anyway.
    """
    cleaned = _check_reason(reason)
    before = source_state(connection, source_id)
    if not before.is_active:
        raise RecoveryRefusedError(
            "the source is inactive at the catalogue level; reactivate it there first "
            "(an inactive source is never fetchable, by constraint)"
        )

    # `access_state` comes back to OK too. Without it, a source refused with 403 was
    # left `FETCHABLE` **and** `access_state = 'BLOCKED'`, which the health view still
    # reports as BLOCKED -- so the operator's re-enable appeared to do nothing. Found
    # by `test_reenable_restores_fetchability_and_is_audited`, and it is the whole
    # point of this module, so it is asserted rather than assumed.
    connection.execute(
        text(
            "UPDATE source SET fetch_eligibility = 'FETCHABLE', "
            "fetch_eligibility_reason = :reason, fetch_eligibility_set_at = now(), "
            "access_state = 'OK', "
            "cooldown_until = NULL, cooldown_reason = NULL, cooldown_set_at = NULL, "
            "rate_limit_strikes = 0 "
            " WHERE id = :id"
        ),
        {"reason": f"re-enabled by operator: {cleaned}"[:400], "id": source_id},
    )
    after = source_state(connection, source_id)
    _append_audit(
        connection,
        actor=actor,
        action=AcquisitionRecoveryAction.REENABLE,
        source_id=source_id,
        before=_snapshot(before),
        after=_snapshot(after),
        reason=cleaned,
    )
    logger.info(
        "acquisition_source_reenabled",
        source_id=str(source_id),
        actor_id=str(actor.user_id),
        was=before.fetch_eligibility,
    )
    return after


def disable(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> SourceState:
    """Stop fetching this page until a person says otherwise."""
    cleaned = _check_reason(reason)
    before = source_state(connection, source_id)
    connection.execute(
        text(
            "UPDATE source SET fetch_eligibility = 'DISABLED', "
            "fetch_eligibility_reason = :reason, fetch_eligibility_set_at = now() "
            " WHERE id = :id"
        ),
        {"reason": f"disabled by operator: {cleaned}"[:400], "id": source_id},
    )
    after = source_state(connection, source_id)
    _append_audit(
        connection,
        actor=actor,
        action=AcquisitionRecoveryAction.DISABLE,
        source_id=source_id,
        before=_snapshot(before),
        after=_snapshot(after),
        reason=cleaned,
    )
    logger.info(
        "acquisition_source_disabled", source_id=str(source_id), actor_id=str(actor.user_id)
    )
    return after


def mark_needs_review(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    actor: Actor,
    reason: str,
) -> SourceState:
    """Park the page for a human decision, without judging the site."""
    cleaned = _check_reason(reason)
    before = source_state(connection, source_id)
    connection.execute(
        text(
            "UPDATE source SET fetch_eligibility = 'NEEDS_MANUAL_REVIEW', "
            "fetch_eligibility_reason = :reason, fetch_eligibility_set_at = now() "
            " WHERE id = :id"
        ),
        {"reason": f"flagged by operator: {cleaned}"[:400], "id": source_id},
    )
    after = source_state(connection, source_id)
    _append_audit(
        connection,
        actor=actor,
        action=AcquisitionRecoveryAction.NEEDS_REVIEW,
        source_id=source_id,
        before=_snapshot(before),
        after=_snapshot(after),
        reason=cleaned,
    )
    return after


def clear_cooldown(
    connection: Connection,
    *,
    source_id: uuid.UUID,
    actor: Actor,
    reason: str,
    include_host: bool = False,
) -> SourceState:
    """Cut a cooldown short. Audited, because shortening a site's pause is a choice.

    A cooldown normally needs no operator at all -- it expires. This exists for the
    case where the cause is known to be resolved, and it is audited precisely because
    the alternative reading is "someone got impatient with a university's rate limit".

    `include_host` also clears the host-level pause. Off by default: clearing one page
    is a small decision and un-quieting a whole institution is not the same one.
    """
    cleaned = _check_reason(reason)
    before = source_state(connection, source_id)
    if before.schedule_state != "COOLDOWN":
        raise RecoveryRefusedError(
            f"this source is not in cooldown (schedule state {before.schedule_state}); "
            "nothing to clear"
        )
    connection.execute(
        text(
            "UPDATE source SET cooldown_until = NULL, cooldown_reason = NULL, "
            "cooldown_set_at = NULL WHERE id = :id"
        ),
        {"id": source_id},
    )
    if include_host:
        connection.execute(
            text("DELETE FROM host_cooldown WHERE host = :host"), {"host": before.host}
        )
    after = source_state(connection, source_id)
    _append_audit(
        connection,
        actor=actor,
        action=AcquisitionRecoveryAction.CLEAR_COOLDOWN,
        source_id=source_id,
        before=_snapshot(before),
        after=_snapshot(after),
        reason=cleaned + (" (host pause cleared too)" if include_host else ""),
    )
    return after


def _snapshot(state: SourceState) -> dict[str, object]:
    """The fields a reviewer of the audit row would want, and no others."""
    return {
        "fetch_eligibility": state.fetch_eligibility,
        "schedule_state": state.schedule_state,
        "health": state.health,
        "cooldown_until": state.cooldown_until,
        "rate_limit_strikes": state.rate_limit_strikes,
        # Included precisely so the trail shows it did not move.
        "publication_eligibility": state.publication_eligibility,
    }


__all__ = [
    "MIN_REASON_LENGTH",
    "Actor",
    "RecoveryRefusedError",
    "SourceState",
    "clear_cooldown",
    "disable",
    "mark_needs_review",
    "reenable",
    "source_state",
]
