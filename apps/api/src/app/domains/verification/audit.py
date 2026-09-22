"""Appending to the hash chain. One implementation, used by everything here.

Section 19 forbids a second audit mechanism, and the reason is not tidiness: the chain
is only tamper-evident if every consequential write is in it, and a second appender is
how one of them quietly stops being.

ALWAYS LAST
===========
`audit_chain_head` serialises every consequential write in the system and its lock is
held to end of transaction (C18). Every writer in this codebase therefore appends last:
taking that lock first would put every verification behind every other write, and taking
it in a different order in different modules is how two transactions form a cycle.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Connection, text

#: The only actions that may be recorded without a human actor, and the reason each may:
#: it happened before any human authority existed, so naming one would be a fabrication.
#: Nothing on this list is a judgement about data. Verification, promotion, publication
#: and role grants are absent deliberately -- see `append_system`.
SYSTEM_ACTIONS = frozenset({"CREDENTIAL_ENROLLMENT_ISSUED"})


def append(
    connection: Connection,
    *,
    actor_id: uuid.UUID,
    action: str,
    object_type: str,
    object_id: uuid.UUID | None,
    reason: str,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
) -> None:
    """Record one human decision. `actor_type` is USER because a human made it.

    A machine actor may never stand in for a reviewer. `append_system` exists for the one
    narrow case that is not a decision at all, and it refuses every action that is.
    """
    connection.execute(
        text(
            """
            INSERT INTO audit_log (actor_type, actor_id, action, object_type, object_id,
                                   before_state, after_state, reason)
            VALUES ('USER', :actor, :action, :object_type, :object_id,
                    CAST(:before AS jsonb), CAST(:after AS jsonb), :reason)
            """
        ),
        {
            "actor": actor_id,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "before": json.dumps(before, sort_keys=True, default=str) if before else None,
            "after": json.dumps(after, sort_keys=True, default=str) if after else None,
            "reason": reason,
        },
    )


def append_system(
    connection: Connection,
    *,
    action: str,
    object_type: str,
    object_id: uuid.UUID | None,
    reason: str,
    after: dict[str, Any] | None = None,
) -> None:
    """Record an act that genuinely had no human author, naming none.

    WHY THIS EXISTS AT ALL
    ======================
    Bootstrap enrolment issuance happens while no administrator exists -- that is its
    entry condition. Filing it under a person would claim somebody approved an identity
    when nobody did, and leaving it unrecorded would hide the one issuance that had no
    approver. `actor_type = SYSTEM` with `actor_id` NULL is the honest third answer: it
    says *this happened, and no human authorised it*, which is exactly the fact a later
    auditor needs.

    WHY IT CANNOT BECOME THE BACK DOOR
    ==================================
    A general SYSTEM appender would make "a machine verified it" a one-word change, which
    is what the rest of this system is built to prevent. So the action must be on
    `SYSTEM_ACTIONS`, a short allow-list that contains no judgement about data. A reviewer
    decision, a promotion, a publication and a role grant are all refused here, loudly,
    and adding one to the list is a visible edit to this file rather than a call site.
    """
    if action not in SYSTEM_ACTIONS:
        raise ValueError(
            f"{action!r} may not be recorded without a human actor. SYSTEM entries are "
            f"limited to {sorted(SYSTEM_ACTIONS)}: anything that is a judgement about "
            "data must name the person who made it."
        )
    connection.execute(
        text(
            """
            INSERT INTO audit_log (actor_type, actor_id, action, object_type, object_id,
                                   before_state, after_state, reason)
            VALUES ('SYSTEM', NULL, :action, :object_type, :object_id,
                    NULL, CAST(:after AS jsonb), :reason)
            """
        ),
        {
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "after": json.dumps(after, sort_keys=True, default=str) if after else None,
            "reason": reason,
        },
    )


__all__ = ["SYSTEM_ACTIONS", "append", "append_system"]
