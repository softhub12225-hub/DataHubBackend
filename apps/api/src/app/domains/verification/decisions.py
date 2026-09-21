"""Preview and apply a responsibility decision. One path, two callers, no second policy.

WHY THIS MODULE EXISTS
======================
Step 5C.7L/M moved the primary review workflow from a terminal into a browser. That
creates an obvious hazard: a console that re-implements the rules in TypeScript so it can
grey out a button, while the real guard lives in Python. The two drift, and the half the
reviewer sees is not the half that protects the write.

So the console does not decide anything. ``preview()`` runs the *entire* validation the
apply path runs, against the live database, and returns a structured answer: every check,
whether it passed, and what the write would do. The button is enabled because the server
said the decision is valid, not because the client re-derived it.

PREVIEW AND APPLY ARE THE SAME CODE
===================================
``apply()`` calls ``_evaluate()`` -- the same function ``preview()`` calls -- and refuses
if it is not valid. There is no path that writes without having just re-run every check.
A preview is not a permission slip that the apply trusts; it is a *freshness claim* the
apply re-verifies from scratch.

STALE PREVIEWS
==============
A preview describes the database at a moment. Between preview and confirmation another
reviewer may decide the same mapping, a domain may be deactivated, the manifest may be
replaced. Applying a decision the reviewer previewed against different facts would record
a judgement about something they did not see.

The token is an HMAC over a fingerprint of everything the reviewer was shown: the mapping,
its exact current state, the decision, the reason, the manifest digest, the authenticated
reviewer, and the trust states of both hosts. On apply the server recomputes the
fingerprint from the database as it is *now* and requires the token to match. Any change
in any of those facts produces a different fingerprint, and the apply refuses with
``PREVIEW_STALE``.

The client cannot forge one: the payload is signed with a server-held key
(``Settings.session_secret``) that never reaches the browser. The client's copy of the
token is opaque and echoing it back proves only that the server issued it.

WHAT THIS MODULE WILL NOT DO
============================
It sets ``source_mapping.verification_status`` and nothing else. It does not promote, does
not touch ``source``, writes no ``field_claim``, and creates no canonical row. Those are
separate acts with separate authority, and the preview says so explicitly so a reviewer
can see that confirming here does not publish anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.domains.verification import redirect_authority
from app.domains.verification.audit import append as append_audit
from app.domains.verification.domain_binding import MANIFEST_DIR
from app.domains.verification.redirect_authority import RedirectAuthority
from app.domains.verification.responsibility_binding import (
    ManifestChangedError,
    ResponsibilityBindingError,
    ReviewedResponsibility,
)
from app.domains.verification.responsibility_binding import (
    require_binding as require_responsibility_binding,
)

logger = get_logger(__name__)

#: Responsibility decisions, onto `source_mapping.verification_status`. Identical to the
#: CLI's table, which now imports this one rather than keeping its own copy.
RESPONSIBILITY_DECISIONS: dict[str, str] = {
    "VERIFIED": "VERIFIED_OFFICIAL",
    "AUTHORIZED_EXTERNAL": "AUTHORIZED_EXTERNAL",
    "REJECTED": "REJECTED",
    "NEEDS_REVIEW": "CANDIDATE",
    "REVOKED": "REJECTED",
}

#: Decisions that switch a row off. Explicit rather than derived from the resulting
#: status, because REVOKED and REJECTED land on the same status and mean different things
#: to the person reading the audit log.
DEACTIVATING = frozenset({"REJECTED", "REVOKED"})

#: The decisions the console offers. A deliberate subset: AUTHORIZED_EXTERNAL and REVOKED
#: exist for situations the pilot has not reached, and offering them as buttons invites a
#: reviewer to pick one without the context those decisions require.
CONSOLE_DECISIONS = ("VERIFIED", "REJECTED", "NEEDS_REVIEW")

#: Audit action recorded per decision.
AUDIT_ACTIONS: dict[str, str] = {
    decision: f"RESPONSIBILITY_{decision}" for decision in RESPONSIBILITY_DECISIONS
}


class DecisionRefusedError(RuntimeError):
    """The decision cannot be applied as asked. The message names the failed check."""


class PreviewStaleError(DecisionRefusedError):
    """The database changed after the preview. Re-preview and look again."""


class PreviewForgedError(DecisionRefusedError):
    """The token did not come from this server, or was altered in transit."""


class BlockerCode(StrEnum):
    """Why a decision may not be applied. One code per distinct remedy."""

    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    MISSING_PERMISSION = "MISSING_PERMISSION"
    FIXTURE_IDENTITY = "FIXTURE_IDENTITY"
    UNKNOWN_MAPPING = "UNKNOWN_MAPPING"
    UNKNOWN_DECISION = "UNKNOWN_DECISION"
    REASON_REQUIRED = "REASON_REQUIRED"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    REQUESTED_HOST_UNTRUSTED = "REQUESTED_HOST_UNTRUSTED"
    EFFECTIVE_HOST_UNTRUSTED = "EFFECTIVE_HOST_UNTRUSTED"
    HOST_INSTITUTION_MISMATCH = "HOST_INSTITUTION_MISMATCH"
    NO_STORED_EVIDENCE = "NO_STORED_EVIDENCE"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One named reason, phrased so a reviewer knows what to go and do."""

    code: BlockerCode
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class MappingState:
    """The fields a responsibility decision reads or writes. The before/after panel."""

    verification_status: str
    publication_eligibility: str
    verified_by: uuid.UUID | None
    verified_by_name: str | None
    is_active: bool
    promoted_source_id: uuid.UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification_status": self.verification_status,
            "publication_eligibility": self.publication_eligibility,
            "verified_by": str(self.verified_by) if self.verified_by else None,
            "verified_by_name": self.verified_by_name,
            "is_active": self.is_active,
            "promoted_source_id": (
                str(self.promoted_source_id) if self.promoted_source_id else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Reviewer:
    """Whoever the server authenticated. Never a value the client supplied."""

    id: uuid.UUID
    email: str
    display_name: str
    is_test: bool = False


@dataclass(slots=True)
class DecisionPreview:
    """Everything the confirmation screen must show, computed server-side."""

    mapping_id: uuid.UUID
    decision: str
    reason: str
    reviewer: Reviewer
    before: MappingState
    after: MappingState | None
    binding: ReviewedResponsibility | None
    authority: RedirectAuthority | None
    blockers: list[Blocker] = field(default_factory=list)
    token: str | None = None
    issued_at: datetime | None = None

    @property
    def valid(self) -> bool:
        return not self.blockers

    @property
    def would_append(self) -> str | None:
        return AUDIT_ACTIONS.get(self.decision) if self.valid else None

    # These three are constants, and they are returned rather than assumed because the
    # whole point of the confirmation screen is that the reviewer does not have to take
    # anybody's word for what a decision does.
    creates_field_claim: bool = False
    modifies_canonical: bool = False
    promotes: bool = False


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """What actually happened, read back from the database after the write."""

    mapping_id: uuid.UUID
    decision: str
    action: str
    before: MappingState
    after: MappingState
    audit_seq: int
    audit_chain_ok: bool
    reviewer: Reviewer
    source_ref: str
    responsibility: str
    field_claim_count: int
    canonical_unchanged: bool


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


#: The mapping as a decision reads it. Written out twice, locked and unlocked, rather
#: than concatenated from a flag: an assembled SQL string is the shape a reviewer of this
#: file has to stop and check, and the two literals are shorter than the argument for why
#: the concatenation was safe.
_STATE_SQL = """
            SELECT sm.id, sm.source_category::text        AS responsibility,
                   sm.verification_status::text           AS status,
                   sm.publication_eligibility::text       AS eligibility,
                   sm.verified_by, sm.is_active, sm.promoted_source_id,
                   sm.target_institution_id,
                   who.display_name                       AS verified_by_name,
                   pcs.source_ref                         AS source_ref
              FROM source_mapping sm
              LEFT JOIN app_user who ON who.id = sm.verified_by
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
             WHERE sm.id = :mapping
"""

_STATE_SQL_LOCKED = _STATE_SQL + "             FOR UPDATE OF sm"


def _load_state(connection: Connection, mapping_id: uuid.UUID, *, lock: bool) -> Any:
    """The mapping as it is now. `lock` takes FOR UPDATE for the apply path."""
    return connection.execute(
        text(_STATE_SQL_LOCKED if lock else _STATE_SQL),
        {"mapping": mapping_id},
    ).one_or_none()


def _state_of(row: Any) -> MappingState:
    return MappingState(
        verification_status=str(row.status),
        publication_eligibility=str(row.eligibility),
        verified_by=(uuid.UUID(str(row.verified_by)) if row.verified_by else None),
        verified_by_name=(str(row.verified_by_name) if row.verified_by_name else None),
        is_active=bool(row.is_active),
        promoted_source_id=(
            uuid.UUID(str(row.promoted_source_id)) if row.promoted_source_id else None
        ),
    )


def _projected(current: MappingState, *, decision: str, reviewer: Reviewer) -> MappingState:
    """What the row becomes. Derived, so the panel cannot promise a different write."""
    status = RESPONSIBILITY_DECISIONS[decision]
    eligible = {
        "VERIFIED_OFFICIAL": "OFFICIAL_VERIFIED",
        "AUTHORIZED_EXTERNAL": "AUTHORIZED_EXTERNAL",
    }.get(status, "NOT_ELIGIBLE")
    return MappingState(
        verification_status=status,
        # `source_mapping.publication_eligibility` is GENERATED ALWAYS from the status.
        # Mirrored here so the panel shows what the database will derive -- and note this
        # is the *mapping's* eligibility, not `source.publication_eligibility`, which is
        # what C27 reads and which only promotion changes.
        publication_eligibility=eligible,
        verified_by=reviewer.id,
        verified_by_name=reviewer.display_name,
        is_active=decision not in DEACTIVATING,
        promoted_source_id=current.promoted_source_id,
    )


# ---------------------------------------------------------------------------
# preview tokens
# ---------------------------------------------------------------------------


def _fingerprint(
    *,
    mapping_id: uuid.UUID,
    decision: str,
    reason: str,
    reviewer: Reviewer,
    before: MappingState,
    binding: ReviewedResponsibility,
    authority: RedirectAuthority,
) -> str:
    """A digest of every fact the reviewer was shown.

    Anything that changes the meaning of the decision must appear here, or a preview
    would survive a change it should not have survived.
    """
    payload = {
        # Namespaced. A responsibility token and a promotion token for the same mapping
        # must never be interchangeable, and relying on "their contents happen to differ"
        # is the kind of assumption that stops being true when a field is added.
        "kind": "responsibility",
        "mapping": str(mapping_id),
        "decision": decision,
        "reason": reason.strip(),
        "reviewer": str(reviewer.id),
        "manifest": binding.manifest_sha256,
        "source_ref": binding.source_ref,
        "institution": str(binding.institution_id),
        "responsibility": binding.claimed_responsibility,
        "url": binding.url,
        "before": before.as_dict(),
        "authority": {
            "verdict": authority.verdict.value,
            "requested": [authority.requested.host, authority.requested.display],
            "effective": [authority.effective.host, authority.effective.display],
            "redirected": authority.redirected,
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def issue_token(
    *,
    secret: str,
    fingerprint: str,
    mapping_id: uuid.UUID,
    issued_at: datetime,
) -> str:
    """A signed, opaque handle to one preview. Carries its own payload; forgery-proof.

    Shaped `<base64url(payload)>.<hmac>` so the server needs no preview store: the token
    is self-describing and the signature is what makes it trustworthy. A database table
    of pending previews would be a second source of truth that can outlive the facts it
    describes.
    """
    payload = {
        "m": str(mapping_id),
        "f": fingerprint,
        "t": issued_at.isoformat(),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(secret, body)}"


def open_token(secret: str, token: str) -> dict[str, str]:
    """Public alias for `_open_token`.

    The candidate-resolution service (Step 5C.9) needs the same verify-then-parse, and a
    second implementation of signature checking is the last thing this codebase should
    have two of.
    """
    return _open_token(secret, token)


def _open_token(secret: str, token: str) -> dict[str, str]:
    """Verify the signature and return the payload, or refuse.

    Checks the shape before touching it. An API client that omits the field, or a caller
    that passes the `None` an invalid preview returns, must get a named refusal rather
    than an `AttributeError` from somewhere deep in the parser.
    """
    if not isinstance(token, str) or not token.strip():
        raise PreviewForgedError(
            "PREVIEW_INVALID: no preview token was supplied. Apply is only reachable "
            "through a preview."
        )
    try:
        encoded, signature = token.strip().split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        body = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError) as exc:
        raise PreviewForgedError("PREVIEW_INVALID: the preview token is malformed") from exc
    if not hmac.compare_digest(signature, _sign(secret, body)):
        raise PreviewForgedError("PREVIEW_INVALID: the preview token was not issued by this server")
    parsed: dict[str, str] = json.loads(body)
    return parsed


# ---------------------------------------------------------------------------
# evaluation -- the single shared path
# ---------------------------------------------------------------------------


def _evaluate(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    decision: str,
    reason: str,
    reviewer: Reviewer,
    expect_sha256: str,
    directory: Path,
    lock: bool,
) -> DecisionPreview:
    """Every check, in the order a reviewer would ask them. Writes nothing."""
    blockers: list[Blocker] = []

    row = _load_state(connection, mapping_id, lock=lock)
    if row is None:
        # Nothing further can be said about a mapping that does not exist.
        empty = MappingState("UNKNOWN", "UNKNOWN", None, None, False, None)
        return DecisionPreview(
            mapping_id=mapping_id,
            decision=decision,
            reason=reason,
            reviewer=reviewer,
            before=empty,
            after=None,
            binding=None,
            authority=None,
            blockers=[Blocker(BlockerCode.UNKNOWN_MAPPING, f"no source_mapping {mapping_id}")],
        )

    before = _state_of(row)

    if reviewer.is_test:
        blockers.append(
            Blocker(
                BlockerCode.FIXTURE_IDENTITY,
                f"{reviewer.email} is a [TEST ONLY] identity and may not decide real sources",
            )
        )
    if decision not in RESPONSIBILITY_DECISIONS:
        blockers.append(
            Blocker(
                BlockerCode.UNKNOWN_DECISION,
                f"{decision!r} is not a responsibility decision",
            )
        )
    if not reason.strip():
        blockers.append(
            Blocker(BlockerCode.REASON_REQUIRED, "a decision must record why it was made")
        )

    binding: ReviewedResponsibility | None = None
    try:
        binding = require_responsibility_binding(
            connection,
            mapping_id=mapping_id,
            expect_sha256=expect_sha256,
            directory=directory,
        )
    except ManifestChangedError as exc:
        blockers.append(Blocker(BlockerCode.MANIFEST_MISMATCH, str(exc)))
    except ResponsibilityBindingError as exc:
        blockers.append(Blocker(BlockerCode.BINDING_MISMATCH, str(exc)))

    authority = redirect_authority.for_mapping(connection, mapping_id)
    verdict = authority.verdict
    if verdict is redirect_authority.AuthorityVerdict.REQUESTED_UNTRUSTED:
        blockers.append(
            Blocker(BlockerCode.REQUESTED_HOST_UNTRUSTED, authority.blocker or verdict.value)
        )
    elif verdict is redirect_authority.AuthorityVerdict.EFFECTIVE_UNTRUSTED:
        blockers.append(
            Blocker(BlockerCode.EFFECTIVE_HOST_UNTRUSTED, authority.blocker or verdict.value)
        )
    elif verdict is redirect_authority.AuthorityVerdict.INSTITUTION_MISMATCH:
        blockers.append(
            Blocker(BlockerCode.HOST_INSTITUTION_MISMATCH, authority.blocker or verdict.value)
        )
    # NO_EVIDENCE is intentionally NOT a blocker for a responsibility decision. A reviewer
    # may legitimately mark a dead or unfetchable page REJECTED or NEEDS_REVIEW, and
    # requiring stored evidence to do so would trap exactly those rows. Promotion is where
    # missing evidence must refuse, because that is the act that relies on the content.

    after = (
        _projected(before, decision=decision, reviewer=reviewer)
        if decision in RESPONSIBILITY_DECISIONS
        else None
    )
    if (
        after is not None
        and before.verification_status == after.verification_status
        and before.is_active == after.is_active
        and before.verified_by is not None
    ):
        blockers.append(
            Blocker(
                BlockerCode.NO_CHANGE,
                f"the mapping is already {before.verification_status} and this decision "
                "would change nothing",
            )
        )

    return DecisionPreview(
        mapping_id=mapping_id,
        decision=decision,
        reason=reason,
        reviewer=reviewer,
        before=before,
        after=after,
        binding=binding,
        authority=authority,
        blockers=blockers,
    )


def preview(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    decision: str,
    reason: str,
    reviewer: Reviewer,
    expect_sha256: str,
    secret: str,
    now: datetime | None = None,
    directory: Path = MANIFEST_DIR,
) -> DecisionPreview:
    """Run every check and, when they all pass, issue a token bound to what was checked.

    Writes nothing. Safe to call against the real database, which is why the console can
    show a reviewer exactly what would happen without asking them to commit to it.
    """
    result = _evaluate(
        connection,
        mapping_id=mapping_id,
        decision=decision,
        reason=reason,
        reviewer=reviewer,
        expect_sha256=expect_sha256,
        directory=directory,
        lock=False,
    )
    if result.valid and result.binding is not None and result.authority is not None:
        issued = now or datetime.now(UTC)
        result.issued_at = issued
        result.token = issue_token(
            secret=secret,
            fingerprint=_fingerprint(
                mapping_id=mapping_id,
                decision=decision,
                reason=reason,
                reviewer=reviewer,
                before=result.before,
                binding=result.binding,
                authority=result.authority,
            ),
            mapping_id=mapping_id,
            issued_at=issued,
        )
    return result


def apply_decision(
    connection: Connection,
    *,
    token: str,
    mapping_id: uuid.UUID,
    decision: str,
    reason: str,
    reviewer: Reviewer,
    expect_sha256: str,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
    directory: Path = MANIFEST_DIR,
) -> DecisionResult:
    """Apply a previewed decision, refusing unless the facts are still the previewed ones.

    Re-runs the whole evaluation under `FOR UPDATE`. The token is not authority to write;
    it is a claim that nothing has moved, and this checks the claim against the database
    rather than believing it.
    """
    moment = now or datetime.now(UTC)
    payload = _open_token(secret, token)
    if payload.get("m") != str(mapping_id):
        raise PreviewStaleError("PREVIEW_STALE: the preview was issued for a different mapping")
    try:
        issued_at = datetime.fromisoformat(str(payload.get("t")))
    except ValueError as exc:
        raise PreviewForgedError("PREVIEW_INVALID: unreadable issue time") from exc
    if (moment - issued_at).total_seconds() > ttl_seconds:
        raise PreviewStaleError(
            f"PREVIEW_STALE: the preview is older than {ttl_seconds}s. Preview again so "
            "you are deciding against the database as it is now."
        )

    fresh = _evaluate(
        connection,
        mapping_id=mapping_id,
        decision=decision,
        reason=reason,
        reviewer=reviewer,
        expect_sha256=expect_sha256,
        directory=directory,
        lock=True,
    )
    if not fresh.valid:
        raise DecisionRefusedError(
            "; ".join(f"{b.code.value}: {b.message}" for b in fresh.blockers)
        )
    assert fresh.binding is not None and fresh.authority is not None and fresh.after is not None

    current = _fingerprint(
        mapping_id=mapping_id,
        decision=decision,
        reason=reason,
        reviewer=reviewer,
        before=fresh.before,
        binding=fresh.binding,
        authority=fresh.authority,
    )
    if not hmac.compare_digest(current, str(payload.get("f"))):
        raise PreviewStaleError(
            "PREVIEW_STALE: the mapping, its hosts, the manifest or the decision text "
            "changed after the preview was taken. Preview again and re-read it before "
            "confirming."
        )

    status = RESPONSIBILITY_DECISIONS[decision]
    active = decision not in DEACTIVATING
    connection.execute(
        text(
            """
            UPDATE source_mapping
               SET verification_status = CAST(:status AS official_verification_status),
                   verified_at = now(), verified_by = :actor,
                   rejected_reason = :rejected, is_active = :active,
                   deactivated_reason = :deactivated, updated_at = now()
             WHERE id = :mapping
            """
        ),
        {
            "mapping": mapping_id,
            "status": status,
            "actor": reviewer.id,
            "rejected": reason if decision in DEACTIVATING else None,
            "deactivated": reason if not active else None,
            "active": active,
        },
    )
    append_audit(
        connection,
        actor_id=reviewer.id,
        action=AUDIT_ACTIONS[decision],
        object_type="source_mapping",
        object_id=mapping_id,
        reason=reason,
        before=fresh.before.as_dict(),
        after={
            "responsibility": fresh.binding.claimed_responsibility,
            "status": status,
            "is_active": active,
            "source_ref": fresh.binding.source_ref,
            "manifest_sha256": fresh.binding.manifest_sha256,
        },
    )

    # Read back rather than assume. Section N: every real operation ends with a result
    # taken from the database, not from what the code believed it wrote.
    written = _load_state(connection, mapping_id, lock=False)
    assert written is not None
    seq = connection.execute(
        text(
            "SELECT seq FROM audit_log WHERE object_id = :m AND action = :a "
            " ORDER BY seq DESC LIMIT 1"
        ),
        {"m": mapping_id, "a": AUDIT_ACTIONS[decision]},
    ).scalar_one()
    chain_ok = not connection.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all()
    claims = connection.execute(text("SELECT count(*) FROM field_claim")).scalar_one()
    canonical = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM university) + (SELECT count(*) FROM program) "
            "     + (SELECT count(*) FROM tuition)"
        )
    ).scalar_one()

    logger.info(
        "responsibility_decision_applied",
        mapping_id=str(mapping_id),
        decision=decision,
        source_ref=fresh.binding.source_ref,
        actor=str(reviewer.id),
        audit_seq=int(seq),
    )
    return DecisionResult(
        mapping_id=mapping_id,
        decision=decision,
        action=AUDIT_ACTIONS[decision],
        before=fresh.before,
        after=_state_of(written),
        audit_seq=int(seq),
        audit_chain_ok=bool(chain_ok),
        reviewer=reviewer,
        source_ref=fresh.binding.source_ref,
        responsibility=fresh.binding.claimed_responsibility,
        field_claim_count=int(claims),
        canonical_unchanged=int(canonical) == 0,
    )


__all__ = [
    "AUDIT_ACTIONS",
    "CONSOLE_DECISIONS",
    "DEACTIVATING",
    "RESPONSIBILITY_DECISIONS",
    "Blocker",
    "BlockerCode",
    "DecisionPreview",
    "DecisionRefusedError",
    "DecisionResult",
    "MappingState",
    "PreviewForgedError",
    "PreviewStaleError",
    "PromotionPreview",
    "PromotionResultView",
    "Reviewer",
    "apply_decision",
    "apply_promotion",
    "issue_token",
    "open_token",
    "preview",
    "preview_promotion",
]


# ---------------------------------------------------------------------------
# promotion preview (section O)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PromotionPreview:
    """What promoting this mapping would do. Section O. Computed, never guessed.

    Promotion is a *separate* operation from responsibility verification, and this exists
    so that separation is visible: a reviewer who has just verified a responsibility can
    see exactly what the next act would add, and that it has not happened yet.
    """

    mapping_id: uuid.UUID
    source_id: uuid.UUID | None
    source_ref: str
    responsibility: str
    manifest_bound: bool
    manifest_note: str | None
    authority: RedirectAuthority
    field_bindings: list[tuple[str, str]]
    source_eligibility_before: str | None
    source_eligibility_after: str | None
    promoted_source_id_before: uuid.UUID | None
    promoted_source_id_after: uuid.UUID | None
    blockers: list[Blocker]

    token: str | None = None
    """Issued only for a valid preview. A blocked preview yields nothing to confirm."""

    issued_at: datetime | None = None

    @property
    def valid(self) -> bool:
        return not self.blockers

    # Constants, returned rather than assumed, for the same reason as on DecisionPreview.
    field_claim_remains_zero: bool = True
    canonical_unchanged: bool = True


@dataclass(frozen=True, slots=True)
class PromotionResultView:
    """What promotion actually did, read back from the database after the write.

    Every field is queried after the commit rather than predicted from the arguments.
    A result panel that echoed its own inputs would say SUCCESS whatever happened.
    """

    mapping_id: uuid.UUID
    source_id: uuid.UUID
    source_ref: str
    responsibility: str
    reviewer: Reviewer
    promoted: bool
    promoted_source_id: uuid.UUID | None
    source_eligibility: str
    mapping_eligibility: str
    bindings_written: int
    already_promoted: bool
    audit_seq: int
    audit_action: str
    audit_chain_ok: bool
    field_claim: int
    change_proposal: int
    change_event: int
    canonical_rows: int

    @property
    def canonical_unchanged(self) -> bool:
        return self.canonical_rows == 0


def _promotion_fingerprint(
    *,
    mapping_id: uuid.UUID,
    source_id: uuid.UUID,
    reviewer: Reviewer,
    binding: ReviewedResponsibility,
    authority: RedirectAuthority,
    mapping_status: str,
    mapping_eligibility: str,
    source_eligibility: str | None,
    promoted_source_id: uuid.UUID | None,
    field_bindings: list[tuple[str, str]],
) -> str:
    """A digest of every fact the promotion screen showed.

    Wider than the responsibility fingerprint because promotion depends on more: the
    target source and its current eligibility, whether the mapping is already promoted,
    and the exact set of field bindings that would be created. If any of those moved
    between the preview and the confirmation, the reviewer approved a different act.
    """
    payload = {
        "kind": "promotion",
        "mapping": str(mapping_id),
        "source": str(source_id),
        "reviewer": str(reviewer.id),
        "manifest": binding.manifest_sha256,
        "source_ref": binding.source_ref,
        "institution": str(binding.institution_id),
        "responsibility": binding.claimed_responsibility,
        "url": binding.url,
        "mapping_status": mapping_status,
        "mapping_eligibility": mapping_eligibility,
        "source_eligibility": source_eligibility,
        "promoted_source_id": str(promoted_source_id) if promoted_source_id else None,
        "bindings": sorted(f"{entity}.{field}" for entity, field in field_bindings),
        "authority": {
            "verdict": authority.verdict.value,
            "requested": [authority.requested.host, authority.requested.display],
            "effective": [authority.effective.host, authority.effective.display],
            "redirected": authority.redirected,
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def preview_promotion(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    expect_sha256: str,
    reviewer: Reviewer | None = None,
    secret: str | None = None,
    now: datetime | None = None,
    directory: Path = MANIFEST_DIR,
) -> PromotionPreview:
    """Everything section O requires, without promoting anything.

    Runs the real guards -- manifest binding, redirect authority, and the trust checks
    `promote` itself applies -- and reports what each says.

    When `reviewer` and `secret` are supplied and every check passed, it issues a signed
    token bound to those facts. The token is namespaced to `promotion`, so a
    responsibility token can never be spent here and this one can never be spent there.
    Omitting them yields a read-only preview with no token, which is what a caller that
    only wants to display the state should ask for.
    """
    from app.domains.verification.policy import bindings_for

    blockers: list[Blocker] = []
    binding: ReviewedResponsibility | None = None
    note: str | None = None
    try:
        binding = require_responsibility_binding(
            connection, mapping_id=mapping_id, expect_sha256=expect_sha256, directory=directory
        )
    except (ManifestChangedError, ResponsibilityBindingError) as exc:
        note = str(exc).splitlines()[0]
        blockers.append(Blocker(BlockerCode.BINDING_MISMATCH, note))

    row = connection.execute(
        text(
            """
            SELECT sm.source_category::text AS responsibility, sm.url_sha256,
                   sm.verification_status::text AS status,
                   sm.publication_eligibility::text AS mapping_eligibility,
                   sm.promoted_source_id, sm.is_active,
                   pcs.source_ref,
                   src.id AS source_id,
                   src.publication_eligibility::text AS source_eligibility
              FROM source_mapping sm
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
              LEFT JOIN source src ON src.url_hash = sm.url_sha256
             WHERE sm.id = :mapping
            """
        ),
        {"mapping": mapping_id},
    ).one_or_none()
    if row is None:
        raise DecisionRefusedError(f"no source_mapping {mapping_id}")

    authority = redirect_authority.for_mapping(connection, mapping_id)
    if authority.verdict in (
        redirect_authority.AuthorityVerdict.REQUESTED_UNTRUSTED,
        redirect_authority.AuthorityVerdict.EFFECTIVE_UNTRUSTED,
        redirect_authority.AuthorityVerdict.INSTITUTION_MISMATCH,
    ):
        blockers.append(
            Blocker(BlockerCode.EFFECTIVE_HOST_UNTRUSTED, authority.blocker or "untrusted host")
        )

    if row.status not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL"):
        blockers.append(
            Blocker(
                BlockerCode.BINDING_MISMATCH,
                f"the responsibility is {row.status}: verify it before promoting it",
            )
        )
    if row.source_id is None:
        blockers.append(
            Blocker(
                BlockerCode.NO_STORED_EVIDENCE,
                "no acquisition source shares this mapping's URL hash, so there is "
                "nothing to promote onto",
            )
        )

    pairs = bindings_for(str(row.responsibility))
    if not pairs:
        blockers.append(
            Blocker(
                BlockerCode.BINDING_MISMATCH,
                f"responsibility {row.responsibility} authorises no publishable field",
            )
        )

    would_be = (
        str(row.mapping_eligibility)
        if row.mapping_eligibility in ("OFFICIAL_VERIFIED", "AUTHORIZED_EXTERNAL")
        else "NOT_ELIGIBLE"
    )
    result = PromotionPreview(
        mapping_id=mapping_id,
        source_id=(uuid.UUID(str(row.source_id)) if row.source_id else None),
        source_ref=str(row.source_ref or ""),
        responsibility=str(row.responsibility),
        manifest_bound=binding is not None,
        manifest_note=note,
        authority=authority,
        field_bindings=[(e, f) for e, f in pairs],
        source_eligibility_before=(str(row.source_eligibility) if row.source_eligibility else None),
        source_eligibility_after=(would_be if not blockers else None),
        promoted_source_id_before=(
            uuid.UUID(str(row.promoted_source_id)) if row.promoted_source_id else None
        ),
        promoted_source_id_after=(
            uuid.UUID(str(row.source_id)) if row.source_id and not blockers else None
        ),
        blockers=blockers,
    )
    # A token only for a preview that passed every check, and only when the caller
    # supplied an authenticated reviewer to bind it to. A blocked preview has nothing to
    # confirm, so issuing one would create a confirm button that must then be disabled by
    # the client -- which is the client deciding, and it does not get to.
    if (
        result.valid
        and binding is not None
        and result.source_id is not None
        and secret
        and reviewer
    ):
        issued = now or datetime.now(UTC)
        result.issued_at = issued
        result.token = issue_token(
            secret=secret,
            fingerprint=_promotion_fingerprint(
                mapping_id=mapping_id,
                source_id=result.source_id,
                reviewer=reviewer,
                binding=binding,
                authority=authority,
                mapping_status=str(row.status),
                mapping_eligibility=str(row.mapping_eligibility),
                source_eligibility=(
                    str(row.source_eligibility) if row.source_eligibility else None
                ),
                promoted_source_id=result.promoted_source_id_before,
                field_bindings=result.field_bindings,
            ),
            mapping_id=mapping_id,
            issued_at=issued,
        )
    return result


def apply_promotion(
    connection: Connection,
    *,
    token: str,
    mapping_id: uuid.UUID,
    reviewer: Reviewer,
    reason: str,
    expect_sha256: str,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
    directory: Path = MANIFEST_DIR,
) -> PromotionResultView:
    """Promote a verified mapping, refusing unless the facts are still the previewed ones.

    Re-runs the entire preview under the same rules and compares the fingerprint against
    the token. The token is not authority to write; it is a claim that nothing has moved,
    and this checks the claim against the database rather than believing it.

    The write itself goes through `promotion.promote`, which applies its own ten checks
    including the manifest binding and redirect authority. Nothing here re-implements
    them: this establishes freshness and identity, and the promotion service decides.
    """
    from app.domains.verification.promotion import Actor, promote

    moment = now or datetime.now(UTC)
    payload = _open_token(secret, token)
    if payload.get("m") != str(mapping_id):
        raise PreviewStaleError("PREVIEW_STALE: the preview was issued for a different mapping")
    try:
        issued_at = datetime.fromisoformat(str(payload.get("t")))
    except ValueError as exc:
        raise PreviewForgedError("PREVIEW_INVALID: unreadable issue time") from exc
    if (moment - issued_at).total_seconds() > ttl_seconds:
        raise PreviewStaleError(
            f"PREVIEW_STALE: the preview is older than {ttl_seconds}s. Preview again so "
            "you are promoting against the database as it is now."
        )
    if not reason.strip():
        raise DecisionRefusedError("a promotion must record why it was made")

    fresh = preview_promotion(
        connection,
        mapping_id=mapping_id,
        expect_sha256=expect_sha256,
        reviewer=reviewer,
        secret=secret,
        now=issued_at,
        directory=directory,
    )
    if not fresh.valid:
        raise DecisionRefusedError(
            "; ".join(f"{b.code.value}: {b.message}" for b in fresh.blockers)
        )
    if fresh.token is None or not hmac.compare_digest(
        _open_token(secret, fresh.token)["f"], str(payload.get("f"))
    ):
        raise PreviewStaleError(
            "PREVIEW_STALE: the mapping, its hosts, the target source or the manifest "
            "changed after the preview was taken. Preview again and re-read it before "
            "confirming."
        )
    assert fresh.source_id is not None

    binding = require_responsibility_binding(
        connection, mapping_id=mapping_id, expect_sha256=expect_sha256, directory=directory
    )
    outcome = promote(
        connection,
        mapping_id=mapping_id,
        source_id=fresh.source_id,
        actor=Actor(id=reviewer.id, display=reviewer.display_name),
        reason=reason,
        binding=binding,
    )

    # Read everything back. Section N: a real operation ends with what the database says.
    written = connection.execute(
        text(
            """
            SELECT sm.promoted_source_id,
                   sm.publication_eligibility::text AS mapping_eligibility,
                   src.publication_eligibility::text AS source_eligibility
              FROM source_mapping sm
              LEFT JOIN source src ON src.id = sm.promoted_source_id
             WHERE sm.id = :mapping
            """
        ),
        {"mapping": mapping_id},
    ).one()
    seq = connection.execute(
        text(
            "SELECT seq FROM audit_log WHERE object_id = :m AND action = :a "
            " ORDER BY seq DESC LIMIT 1"
        ),
        {"m": mapping_id, "a": "SOURCE_MAPPING_PROMOTED"},
    ).scalar_one()
    chain_ok = not connection.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all()
    counts = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM field_claim)     AS field_claim,"
            "       (SELECT count(*) FROM change_proposal) AS change_proposal,"
            "       (SELECT count(*) FROM change_event)    AS change_event,"
            "       (SELECT count(*) FROM university)"
            "     + (SELECT count(*) FROM program)"
            "     + (SELECT count(*) FROM tuition)         AS canonical_rows"
        )
    ).one()

    logger.info(
        "promotion_applied",
        mapping_id=str(mapping_id),
        source_id=str(fresh.source_id),
        source_ref=fresh.source_ref,
        actor=str(reviewer.id),
        audit_seq=int(seq),
        bindings=outcome.bindings_written,
    )
    return PromotionResultView(
        mapping_id=mapping_id,
        source_id=fresh.source_id,
        source_ref=fresh.source_ref,
        responsibility=fresh.responsibility,
        reviewer=reviewer,
        promoted=written.promoted_source_id is not None,
        promoted_source_id=(
            uuid.UUID(str(written.promoted_source_id)) if written.promoted_source_id else None
        ),
        source_eligibility=str(written.source_eligibility or "NOT_ELIGIBLE"),
        mapping_eligibility=str(written.mapping_eligibility),
        bindings_written=outcome.bindings_written,
        already_promoted=outcome.already_promoted,
        audit_seq=int(seq),
        audit_action="SOURCE_MAPPING_PROMOTED",
        audit_chain_ok=bool(chain_ok),
        field_claim=int(counts.field_claim),
        change_proposal=int(counts.change_proposal),
        change_event=int(counts.change_event),
        canonical_rows=int(counts.canonical_rows),
    )
