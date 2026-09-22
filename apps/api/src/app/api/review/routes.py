"""HTTP surface for the reviewer console.

WHAT THESE HANDLERS ARE ALLOWED TO CONTAIN
==========================================
Shape, and nothing else. Every rule about what may be decided, what a decision would do
and whether it may be applied lives in `domains/verification/{decisions,console,
responsibility_binding,redirect_authority}.py` -- the same modules the CLI calls. A handler
that made its own judgement would be a second policy, and the console's value depends on
the screen and the guard agreeing.

So: no handler computes a blocker, and no handler decides whether a button should be
enabled. `preview` returns the server's verdict, and the client renders it.

PREVIEW IS A POST THAT WRITES NOTHING
=====================================
It is a POST because it carries a decision and a reason in a body, not because it mutates.
The connection is in a transaction like every request; preview simply issues no statement
that changes a row. Section J requires it to run the *real* validation, which it does by
calling the same `_evaluate` the apply path calls.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from pydantic import BaseModel, SecretStr
from sqlalchemy import text

from app.api.review.deps import (
    ConnectionDep,
    CsrfDep,
    SessionDep,
    SettingsDep,
    VerifyingSessionDep,
    clear_session_cookies,
    run_sync,
    set_session_cookies,
)
from app.core.logging import get_logger
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.claims import precedence as candidate_precedence
from app.domains.claims import resolution as candidate_resolution
from app.domains.extraction.runner import DERIVED_PREFIX
from app.domains.identity import sessions
from app.domains.verification import console as console_reads
from app.domains.verification import decisions as decision_service
from app.domains.verification import source_body
from app.domains.verification.promotion import PromotionRefusedError

logger = get_logger(__name__)

router = APIRouter(prefix="/review", tags=["review"])


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: SecretStr
    """`SecretStr`, so an accidental log or traceback prints `**********`.

    An earlier version used `Field(repr=False)`, which pydantic warns is ineffective here
    -- `repr` is field metadata that only applies via `Annotated` or assignment, so the
    password would have appeared in full in any `repr()` of the model. The protection has
    to be in the type, not in a flag that silently does nothing.
    """


class SessionResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    roles: list[str]
    permissions: list[str]
    may_verify: bool
    expires_at: Any


class DecisionRequest(BaseModel):
    decision: str
    reason: str
    manifest_sha256: str


class ApplyRequest(DecisionRequest):
    preview_token: str


def _session_payload(
    *,
    user_id: uuid.UUID,
    email: str,
    display_name: str,
    roles: tuple[str, ...],
    permissions: tuple[str, ...],
    expires_at: Any,
) -> SessionResponse:
    return SessionResponse(
        user_id=user_id,
        email=email,
        display_name=display_name,
        roles=list(roles),
        permissions=list(permissions),
        may_verify="source:verify" in permissions,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=SessionResponse)
async def login(
    request: Request,
    response: Response,
    connection: ConnectionDep,
    config: SettingsDep,
    payload: Annotated[LoginRequest, Body()],
) -> SessionResponse:
    """Exchange an email and password for a session cookie.

    The password is never logged, never echoed and never stored beyond the Argon2 check.
    A failure is deliberately indistinguishable between "no such account" and "wrong
    password" -- see `sessions.SessionRefusedError`.
    """
    try:
        issued = await run_sync(
            connection,
            sessions.login,
            email=payload.email,
            password=payload.password.get_secret_value(),
            ttl_minutes=config.session_ttl_minutes,
            ip_address=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
        )
    except sessions.SessionRefusedError as exc:
        # One status, one shape, for every failure mode.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    set_session_cookies(
        response,
        config=config,
        token=issued.token,
        csrf_token=issued.csrf_token,
        max_age=config.session_ttl_minutes * 60,
    )
    return _session_payload(
        user_id=issued.user_id,
        email=issued.email,
        display_name=issued.display_name,
        roles=issued.roles,
        permissions=issued.permissions,
        expires_at=issued.expires_at,
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    connection: ConnectionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
) -> Response:
    """Revoke the session row and clear both cookies. Idempotent."""
    token = request.cookies.get(config.session_cookie_name, "")
    if token:
        await run_sync(connection, sessions.logout, token=token)
    # Clear the cookies on the response that is actually returned. Setting them on the
    # injected `response` and then returning a NEW one discarded the Set-Cookie headers,
    # so the session row was revoked while the browser kept its cookie -- a logout that
    # looked complete and left a cookie behind.
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(out, config=config)
    return out


@router.get("/auth/me", response_model=SessionResponse)
async def whoami(session: SessionDep) -> SessionResponse:
    """Who the server says this request is. Resolved from the cookie, never the body."""
    return _session_payload(
        user_id=session.user_id,
        email=session.email,
        display_name=session.display_name,
        roles=session.roles,
        permissions=session.permissions,
        expires_at=session.expires_at,
    )


# ---------------------------------------------------------------------------
# dashboard and institutions
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def dashboard(
    connection: ConnectionDep, session: SessionDep, config: SettingsDep
) -> dict[str, Any]:
    """Section D."""
    data = await run_sync(connection, console_reads.dashboard)
    return {
        "reviewer": {"display_name": session.display_name, "roles": list(session.roles)},
        "pilot": {
            "institutions": data.trust.pilot_institutions,
            "reviewed_domain_rows": data.reviewed_domain_rows,
            "physical_sources": data.physical_sources,
            "responsibility_rows": data.responsibility_rows,
        },
        "trust": {
            "verified_domains": data.trust.verified_domains,
            "pilot_decisions": data.trust.pilot_decisions,
            "source_mappings": data.trust.source_mappings,
            "responsibility_decisions": data.trust.responsibility_decisions,
            "promoted_mappings": data.trust.promoted_mappings,
            "eligible_sources": data.trust.eligible_sources,
        },
        "safety": {
            "field_claim": data.safety.field_claim,
            "change_proposal": data.safety.change_proposal,
            "change_event": data.safety.change_event,
            "canonical_rows": data.safety.canonical_rows,
            "all_zero": data.safety.all_zero,
        },
        "manifests": [
            {
                "name": m.name,
                "rows": m.rows,
                "present": m.present,
                "matches": m.matches,
                "approved_sha256": m.approved_sha256,
                "actual_sha256": m.actual_sha256,
            }
            for m in data.manifests
        ],
        "audit": {"rows": data.audit_rows, "chain_ok": data.audit_chain_ok},
        "blockers": data.blockers,
    }


@router.get("/institutions")
async def list_institutions(connection: ConnectionDep, session: SessionDep) -> list[dict[str, Any]]:
    """Section E."""
    rows = await run_sync(connection, console_reads.institutions)
    return [
        {
            "institution_id": str(r.institution_id),
            "name": r.name,
            "verified_domains": r.verified_domains,
            "total_domains": r.total_domains,
            "pilot_rows": r.pilot_rows,
            "pilot_decided": r.pilot_decided,
            "registered_mappings": r.registered_mappings,
            "responsibility_decided": r.responsibility_decided,
            "promoted": r.promoted,
            "unresolved_sources": r.unresolved_sources,
        }
        for r in rows
    ]


@router.get("/institutions/{institution_id}")
async def institution_detail(
    institution_id: uuid.UUID,
    connection: ConnectionDep,
    session: SessionDep,
    config: SettingsDep,
) -> dict[str, Any]:
    """Section F: overview, domains, sources and responsibility cards in one payload."""
    rows = await run_sync(connection, console_reads.institutions)
    summary = next((r for r in rows if r.institution_id == institution_id), None)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no pilot institution {institution_id}",
        )
    domains = await run_sync(connection, console_reads.domains_of, institution_id)
    sources = await run_sync(connection, console_reads.pilot_sources_of, institution_id)
    cards = await run_sync(
        connection,
        console_reads.responsibility_cards,
        institution_id,
        expect_sha256=console_reads.APPROVED_MANIFESTS["RESPONSIBILITIES"],
    )
    return {
        "institution_id": str(institution_id),
        "name": summary.name,
        "summary": {
            "verified_domains": summary.verified_domains,
            "total_domains": summary.total_domains,
            "pilot_rows": summary.pilot_rows,
            "pilot_decided": summary.pilot_decided,
            "registered_mappings": summary.registered_mappings,
            "responsibility_decided": summary.responsibility_decided,
            "promoted": summary.promoted,
            "unresolved_sources": summary.unresolved_sources,
        },
        "domains": [
            {
                "domain_id": str(d.domain_id),
                "host": d.host,
                "verification_status": d.verification_status,
                "is_active": d.is_active,
                "verified_by_name": d.verified_by_name,
                "verified_at": d.verified_at,
            }
            for d in domains
        ],
        "sources": [
            {
                "pilot_source_id": str(s.pilot_source_id),
                "source_ref": s.source_ref,
                "responsibility": s.responsibility,
                "degree_scope": s.degree_scope,
                "url": s.url,
                "host": s.host,
                "verification_state": s.verification_state,
                "verification_reason": s.verification_reason,
                "mapping_id": str(s.mapping_id) if s.mapping_id else None,
                "is_duplicate": s.is_duplicate,
                "duplicate_of": s.duplicate_of,
                "body_available": s.body_available,
                "effective_url": s.effective_url,
                "redirected": s.redirected,
                "http_status": s.http_status,
            }
            for s in sources
        ],
        "responsibilities": [
            {
                "mapping_id": str(c.mapping_id),
                "source_ref": c.source_ref,
                "responsibility": c.responsibility,
                "requested_url": c.requested_url,
                "effective_url": c.effective_url,
                "requested_host": c.requested_host,
                "requested_host_authority": c.requested_host_authority,
                "effective_host": c.effective_host,
                "effective_host_authority": c.effective_host_authority,
                "redirected": c.redirected,
                "body_available": c.body_available,
                "verification_status": c.verification_status,
                "publication_eligibility": c.publication_eligibility,
                "verified_by_name": c.verified_by_name,
                "promoted_source_id": str(c.promoted_source_id) if c.promoted_source_id else None,
                "manifest_bound": c.manifest_bound,
                "manifest_blocker": c.manifest_blocker,
            }
            for c in cards
        ],
        "manifest_sha256": console_reads.APPROVED_MANIFESTS["RESPONSIBILITIES"],
        "decisions_offered": list(decision_service.CONSOLE_DECISIONS),
    }


@router.get("/institutions/{institution_id}/audit")
async def institution_audit(
    institution_id: uuid.UUID,
    connection: ConnectionDep,
    session: SessionDep,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Section P. Never returns credential material -- the columns are named explicitly."""
    rows = await run_sync(
        connection, console_reads.audit_trail, institution_id, limit=min(limit, 500)
    )
    return [
        {
            "seq": r.seq,
            "occurred_at": r.occurred_at,
            "action": r.action,
            "group": r.group,
            "actor_name": r.actor_name,
            "actor_type": r.actor_type,
            "object_type": r.object_type,
            "object_id": str(r.object_id) if r.object_id else None,
            "source_ref": r.source_ref,
            "before_state": r.before_state,
            "after_state": r.after_state,
            "reason": r.reason,
            "is_historical_registration": r.is_historical_registration,
        }
        for r in rows
    ]


@router.get("/operations")
async def operations(
    connection: ConnectionDep, session: SessionDep, limit: int = 50
) -> list[dict[str, Any]]:
    """Section Q: recent real operations and how they landed.

    Derived from `audit_log`, which is the record of operations that actually happened.
    Previews are deliberately absent: a preview writes nothing, and persisting a row for
    one would put events in the operator's history that never occurred. The console shows
    the live preview beside the decision it belongs to instead.
    """
    rows = await run_sync(connection, console_reads.audit_trail, None, limit=min(limit, 200))
    return [
        {
            "seq": r.seq,
            "occurred_at": r.occurred_at,
            "operation": r.action,
            "group": r.group,
            "actor_name": r.actor_name,
            "source_ref": r.source_ref,
            "object_type": r.object_type,
            "object_id": str(r.object_id) if r.object_id else None,
            "applied": True,
            "audit_seq": r.seq,
            "is_historical_registration": r.is_historical_registration,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


@router.get("/sources/{pilot_source_id}/evidence")
async def evidence(
    pilot_source_id: uuid.UUID,
    connection: ConnectionDep,
    session: SessionDep,
    config: SettingsDep,
    skip_chrome: bool = False,
    max_blocks: int = 0,
) -> dict[str, Any]:
    """Section G. The stored body, never a fresh fetch.

    A dead row returns `BODY_EVIDENCE_NOT_AVAILABLE` and the acquisition reason rather
    than an error: "there is no evidence" is a finding a reviewer needs, not a failure.
    """
    try:
        found = await run_sync(connection, source_body.resolve, pilot_source_id)
    except source_body.SourceBodyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    header: dict[str, Any] = {
        "pilot_source_id": str(found.pilot_source_id),
        "source_ref": found.source_ref,
        "institution": found.institution,
        "responsibility": found.responsibility,
        "degree_scope": found.degree_scope,
        "duplicate_of": found.duplicate_of,
        "requested_url": found.requested_url,
        "effective_url": found.effective_url,
        "requested_host": found.host,
        "host_status": found.host_status,
        "verification_state": found.verification_state,
        "snapshot_id": str(found.snapshot_id) if found.snapshot_id else None,
        "extraction_id": str(found.extraction_id) if found.extraction_id else None,
        "document_artifact_version": found.document_artifact_version,
        "extraction_status": found.extraction_status,
        "media_type": found.media_type,
        "http_status": found.http_status,
        "fetch_status": found.fetch_status,
        "error_class": found.error_class,
        "access_class": found.access_class,
        "warnings": list(found.warnings),
        "available": found.available,
    }
    if found.duplicate_of:
        header["same_page_note"] = (
            f"SAME PHYSICAL PAGE as {found.duplicate_of}, DIFFERENT RESPONSIBILITY. One "
            "fetch, one stored body, two questions about it."
        )

    if not found.available:
        return {
            **header,
            "body": None,
            "body_status": source_body.NOT_AVAILABLE,
            "note": (
                "There is no stored body for this page, so nothing here supports the claim "
                "that it carries this responsibility. The host being VERIFIED_OFFICIAL is "
                "not evidence about this URL, and the URL path is not evidence either. No "
                "new fetch was attempted."
            ),
        }

    store = FilesystemEvidenceStore(Path(config.artifact_root), prefix=DERIVED_PREFIX)
    try:
        document = source_body.load(store, found)
    except (source_body.SourceBodyError, FileNotFoundError) as exc:
        return {
            **header,
            "body": None,
            "body_status": source_body.NOT_AVAILABLE,
            "note": (
                f"the database knows the document hash but the artifact is not in "
                f"{config.artifact_root}: {exc}"
            ),
        }
    return {
        **header,
        "body_status": "AVAILABLE",
        "blocks": len(document.blocks),
        "tables": len(document.tables),
        "body": source_body.render(document, max_blocks=max_blocks, skip_chrome=skip_chrome),
        "chrome_filter_note": (
            "'Hide obvious navigation/chrome' is a display filter only. It is "
            "conservative and under-filters: furniture it cannot recognise is still shown, "
            "and nothing is removed from the stored evidence."
        ),
    }


# ---------------------------------------------------------------------------
# responsibility decisions
# ---------------------------------------------------------------------------


def _preview_payload(result: decision_service.DecisionPreview) -> dict[str, Any]:
    """Section K's before/after panel, exactly as the server computed it."""
    binding = result.binding
    authority = result.authority
    return {
        "mapping_id": str(result.mapping_id),
        "decision": result.decision,
        "reason": result.reason,
        "valid": result.valid,
        "blockers": [b.as_dict() for b in result.blockers],
        "reviewer": {
            "display_name": result.reviewer.display_name,
            "email": result.reviewer.email,
        },
        "before": result.before.as_dict(),
        "after": result.after.as_dict() if result.after else None,
        "binding": (
            {
                "source_ref": binding.source_ref,
                "institution": binding.institution_label,
                "responsibility": binding.claimed_responsibility,
                "url": binding.url,
                "manifest_sha256": binding.manifest_sha256,
                "access_class": binding.access_class,
                "page_evidence_available": binding.page_evidence_available,
                "same_page_as": binding.same_page_as,
                "matches": True,
            }
            if binding
            else None
        ),
        "authority": (
            {
                "verdict": authority.verdict.value,
                "redirected": authority.redirected,
                "requested_host": authority.requested.host,
                "requested_authority": authority.requested.display,
                "effective_host": authority.effective.host,
                "effective_authority": authority.effective.display,
                "effective_url": authority.effective_url,
                "redirect_chain": authority.redirect_chain,
                "note": authority.blocker,
            }
            if authority
            else None
        ),
        "would_append": result.would_append,
        "creates_field_claim": result.creates_field_claim,
        "modifies_canonical": result.modifies_canonical,
        "promotes": result.promotes,
        "preview_token": result.token,
        "issued_at": result.issued_at,
    }


@router.post("/responsibilities/{mapping_id}/preview")
async def preview_responsibility(
    mapping_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[DecisionRequest, Body()],
) -> dict[str, Any]:
    """Section J. The same validation the apply path runs, writing nothing."""
    result = await run_sync(
        connection,
        decision_service.preview,
        mapping_id=mapping_id,
        decision=payload.decision,
        reason=payload.reason,
        reviewer=_reviewer_of(session),
        expect_sha256=payload.manifest_sha256,
        secret=config.session_secret.get_secret_value(),
    )
    return _preview_payload(result)


@router.post("/responsibilities/{mapping_id}/apply")
async def apply_responsibility(
    mapping_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[ApplyRequest, Body()],
) -> dict[str, Any]:
    """Section L and N. Requires a fresh preview; ends by reading the database back."""
    try:
        result = await run_sync(
            connection,
            decision_service.apply_decision,
            token=payload.preview_token,
            mapping_id=mapping_id,
            decision=payload.decision,
            reason=payload.reason,
            reviewer=_reviewer_of(session),
            expect_sha256=payload.manifest_sha256,
            secret=config.session_secret.get_secret_value(),
            ttl_seconds=config.preview_ttl_seconds,
        )
    except decision_service.PreviewStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except decision_service.PreviewForgedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except decision_service.DecisionRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return {
        "operation": result.action,
        "status": "SUCCESS",
        "actor": result.reviewer.display_name,
        "source_ref": result.source_ref,
        "responsibility": result.responsibility,
        "mapping_id": str(result.mapping_id),
        "previous_state": result.before.as_dict(),
        "current_state": result.after.as_dict(),
        "audit_seq": result.audit_seq,
        "audit_chain_ok": result.audit_chain_ok,
        "publication_eligibility": result.after.publication_eligibility,
        "promoted": result.after.promoted_source_id is not None,
        "field_claim_created": result.field_claim_count > 0,
        "field_claim_total": result.field_claim_count,
        "canonical_unchanged": result.canonical_unchanged,
    }


def _reviewer_of(session: Any) -> decision_service.Reviewer:
    """The authenticated session, as the decision service's actor. Never a client value."""
    return decision_service.Reviewer(
        id=session.user_id,
        email=session.email,
        display_name=session.display_name,
        is_test=session.is_test,
    )


class PromotionApplyRequest(BaseModel):
    reason: str
    preview_token: str


@router.post("/promotions/{mapping_id}/apply")
async def apply_promotion(
    mapping_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[PromotionApplyRequest, Body()],
) -> dict[str, Any]:
    """Promote a verified mapping. Requires a fresh promotion preview.

    Every guard runs again inside `apply_promotion`: the manifest binding, the requested
    and effective host authority, and the fingerprint the preview was issued against. The
    token proves the reviewer saw *these* facts; it does not by itself authorise the write.
    """
    try:
        result = await run_sync(
            connection,
            decision_service.apply_promotion,
            token=payload.preview_token,
            mapping_id=mapping_id,
            reviewer=_reviewer_of(session),
            reason=payload.reason,
            expect_sha256=console_reads.APPROVED_MANIFESTS["RESPONSIBILITIES"],
            secret=config.session_secret.get_secret_value(),
            ttl_seconds=config.preview_ttl_seconds,
        )
    except decision_service.PreviewStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except decision_service.PreviewForgedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (decision_service.DecisionRefusedError, PromotionRefusedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return {
        "operation": result.audit_action,
        "status": "SUCCESS",
        "actor": result.reviewer.display_name,
        "mapping_id": str(result.mapping_id),
        "source_id": str(result.source_id),
        "source_ref": result.source_ref,
        "responsibility": result.responsibility,
        "promoted": result.promoted,
        "promoted_source_id": (
            str(result.promoted_source_id) if result.promoted_source_id else None
        ),
        "publication_eligibility": result.source_eligibility,
        "mapping_eligibility": result.mapping_eligibility,
        "field_bindings_written": result.bindings_written,
        "already_promoted": result.already_promoted,
        "audit_seq": result.audit_seq,
        "audit_chain_ok": result.audit_chain_ok,
        "field_claim": result.field_claim,
        "change_proposal": result.change_proposal,
        "change_event": result.change_event,
        "canonical_rows": result.canonical_rows,
        "canonical_unchanged": result.canonical_unchanged,
    }


# ---------------------------------------------------------------------------
# Step 5C.9: candidate scope and conflict resolution
# ---------------------------------------------------------------------------


class ScopeDecisionRequest(BaseModel):
    """A reviewer's scope decision. No default state -- the client must choose one."""

    state: str
    criteria: list[dict[str, Any]] = []
    reason: str


class ScopeApplyRequest(ScopeDecisionRequest):
    preview_token: str


class ConflictDecisionRequest(BaseModel):
    action: str
    selected_candidate_id: uuid.UUID | None = None
    reason: str


class ConflictApplyRequest(ConflictDecisionRequest):
    preview_token: str


async def _match_key(connection: ConnectionDep, institution_id: uuid.UUID) -> str:
    """The institution's match_key, which is how the claims domain names an institution.

    Resolved server-side from the id in the URL rather than accepted as a string, so a
    client cannot ask for one institution's queue while naming another.
    """
    key = await run_sync(
        connection,
        lambda conn: conn.execute(
            text("SELECT match_key FROM target_institution WHERE id = :i"),
            {"i": institution_id},
        ).scalar_one_or_none(),
    )
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no institution {institution_id}"
        )
    return str(key)


def _queue_item_payload(item: Any) -> dict[str, Any]:
    """Everything Step 5C.9 requires on a candidate card."""
    return {
        "candidate_id": str(item.candidate_id),
        "field_kind": item.field_kind,
        "responsibility": item.responsibility,
        "institution": item.institution,
        "source_ref": item.source_ref,
        "pilot_source_id": str(item.pilot_source_id),
        "mapping_id": str(item.mapping_id) if item.mapping_id else None,
        "mapping_status": item.mapping_status,
        "source_authority": item.source_eligibility,
        "requested_url": item.requested_url,
        "effective_url": item.effective_url,
        "requested_host": item.requested_host,
        "extracted_value": item.value_raw_text,
        "normalized_value": item.value_normalized,
        "evidence_excerpt": item.evidence_text,
        "locator": item.locator,
        "heading_path": list(item.heading_path),
        "confidence": item.confidence_band,
        "confidence_reason": item.confidence_reason,
        "unresolved_reason": item.unresolved_reason,
        "rule_version": f"{item.extractor_name}@{item.extractor_version}",
        "artifact_version": item.document_artifact_version,
        "review_decision": item.review_decision,
        "scope": {
            "machine_resolution": item.machine_resolution,
            "machine_raw_text": item.machine_raw_scope_text,
            "jurisdiction_hints": list(item.machine_country_hints),
            "qualification_hints": list(item.machine_qualification_hints),
            "category_hints": list(item.machine_category_hints),
            "hint_source": item.machine_evidence_source,
            "suggested_criteria": [dict(c) for c in item.suggested_criteria],
            "human_state": item.human_scope_state,
            "human_criteria": [dict(c) for c in item.human_scope_criteria],
            "human_reason": item.human_scope_reason,
            "human_actor": item.human_scope_actor,
            "resolved": item.scope_resolved,
            "scopeable": item.scopeable,
        },
        "conflict": {
            "verdict": item.group_verdict,
            "context_fingerprint": item.context_fingerprint,
            "member_count": item.group_member_count,
            "action": item.conflict_action,
            "actor": item.conflict_actor,
            "resolved": item.conflict_resolved,
        },
        "blockers": list(item.blockers),
        "promotion_ready": not item.blockers,
    }


@router.get("/institutions/{institution_id}/candidates")
async def candidate_queue(
    institution_id: uuid.UUID,
    connection: ConnectionDep,
    session: SessionDep,
    source_ref: str | None = None,
    field_kind: str | None = None,
    confidence: str | None = None,
    scope_unresolved: bool | None = None,
    conflict_unresolved: bool | None = None,
    review_status: str | None = None,
    promotion_ready: bool | None = None,
) -> dict[str, Any]:
    """The candidate review queue. Current candidates only, on both D55 axes.

    Superseded candidates are not filtered out here -- they are never selected. Step 5C.9
    requires them not to be reviewable, and a filter the client could invert would
    eventually be inverted.

    Filtering happens server-side so that a count the reviewer sees is a count of what the
    server would act on.
    """
    key = await _match_key(connection, institution_id)
    items = await run_sync(connection, candidate_resolution.queue, institution=key)

    def keep(item: Any) -> bool:
        if source_ref and item.source_ref != source_ref:
            return False
        if field_kind and item.field_kind != field_kind:
            return False
        if confidence and item.confidence_band != confidence:
            return False
        if scope_unresolved is not None and item.scope_resolved is scope_unresolved:
            return False
        if conflict_unresolved is not None and item.conflict_resolved is conflict_unresolved:
            return False
        if review_status and item.review_decision != review_status:
            return False
        # Inverted rather than a final `if ... return False / return True`: the guards
        # above read as a filter chain, and ruff is right that the last one is just the
        # negation of the condition.
        return not (promotion_ready is not None and bool(item.blockers) is promotion_ready)

    selected = [item for item in items if keep(item)]
    return {
        "institution_id": str(institution_id),
        "institution": key,
        "total_current": len(items),
        "shown": len(selected),
        "scope_states": [state.value for state in candidate_precedence.ScopeState],
        "conflict_actions": list(candidate_resolution.CONFLICT_ACTIONS),
        "dimensions": sorted(candidate_precedence.ALL_DIMENSIONS),
        "jurisdiction_dimensions": sorted(candidate_precedence.JURISDICTION_DIMENSIONS),
        "qualification_dimensions": sorted(candidate_precedence.QUALIFICATION_DIMENSIONS),
        "candidates": [_queue_item_payload(item) for item in selected],
    }


@router.get("/institutions/{institution_id}/candidate-groups")
async def candidate_groups(
    institution_id: uuid.UUID, connection: ConnectionDep, session: SessionDep
) -> list[dict[str, Any]]:
    """Every agreement/conflict group. No winner is chosen or implied."""
    key = await _match_key(connection, institution_id)
    return await run_sync(connection, candidate_resolution.conflict_groups, institution=key)


@router.get("/institutions/{institution_id}/candidate-readiness")
async def candidate_readiness(
    institution_id: uuid.UUID, connection: ConnectionDep, session: SessionDep
) -> dict[str, Any]:
    """The promotion-readiness blocker matrix for this institution."""
    key = await _match_key(connection, institution_id)
    return await run_sync(connection, candidate_resolution.blocker_matrix, institution=key)


def _scope_preview_payload(result: Any) -> dict[str, Any]:
    return {
        "candidate_id": str(result.candidate_id),
        "state": result.state,
        "criteria": [dict(c) for c in result.criteria],
        "reason": result.reason,
        "valid": result.valid,
        "blockers": [b.as_dict() for b in result.blockers],
        "reviewer": {"display_name": result.reviewer.display_name},
        "before": {
            "scope_state": result.before_state,
            "criteria": [dict(c) for c in result.before_criteria],
            "promotion_blockers": list(result.blockers_before),
        },
        "after": {
            "scope_state": result.state,
            "criteria": [dict(c) for c in result.criteria],
            "promotion_blockers": list(result.blockers_after),
        },
        "would_append": result.would_append,
        "creates_field_claim": result.creates_field_claim,
        "modifies_canonical": result.modifies_canonical,
        "preview_token": result.token,
        "issued_at": result.issued_at,
    }


@router.post("/institutions/{institution_id}/candidates/{candidate_id}/scope/preview")
async def preview_candidate_scope(
    institution_id: uuid.UUID,
    candidate_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[ScopeDecisionRequest, Body()],
) -> dict[str, Any]:
    """The same validation the apply path runs, writing nothing."""
    key = await _match_key(connection, institution_id)
    result = await run_sync(
        connection,
        candidate_resolution.preview_scope,
        institution=key,
        candidate_id=candidate_id,
        state=payload.state,
        criteria=tuple(payload.criteria),
        reason=payload.reason,
        reviewer=_reviewer_of(session),
        secret=config.session_secret.get_secret_value(),
    )
    return _scope_preview_payload(result)


def _resolution_result_payload(result: Any) -> dict[str, Any]:
    return {
        "operation": result.audit_action,
        "status": "SUCCESS",
        "actor": result.reviewer.display_name,
        "candidate_id": str(result.candidate_id) if result.candidate_id else None,
        "context_fingerprint": result.context_fingerprint,
        "action": result.action,
        "previous_state": result.before_state,
        "current_state": result.after_state,
        "promotion_blockers_now": list(result.blockers_now),
        "audit_seq": result.audit_seq,
        "audit_chain_ok": result.audit_chain_ok,
        "field_claim": result.field_claim,
        "canonical_rows": result.canonical_rows,
        "canonical_unchanged": result.canonical_unchanged,
    }


@router.post("/institutions/{institution_id}/candidates/{candidate_id}/scope/apply")
async def apply_candidate_scope(
    institution_id: uuid.UUID,
    candidate_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[ScopeApplyRequest, Body()],
) -> dict[str, Any]:
    """Record a scope decision. Requires a fresh preview; creates no field_claim."""
    key = await _match_key(connection, institution_id)
    try:
        result = await run_sync(
            connection,
            candidate_resolution.apply_scope,
            token=payload.preview_token,
            institution=key,
            candidate_id=candidate_id,
            state=payload.state,
            criteria=tuple(payload.criteria),
            reason=payload.reason,
            reviewer=_reviewer_of(session),
            secret=config.session_secret.get_secret_value(),
            ttl_seconds=config.preview_ttl_seconds,
        )
    except decision_service.PreviewStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except decision_service.PreviewForgedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except decision_service.DecisionRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _resolution_result_payload(result)


@router.post("/institutions/{institution_id}/candidate-groups/{fingerprint}/preview")
async def preview_candidate_conflict(
    institution_id: uuid.UUID,
    fingerprint: str,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[ConflictDecisionRequest, Body()],
) -> dict[str, Any]:
    """Preview a conflict resolution. Never selects a winner on the reviewer's behalf."""
    key = await _match_key(connection, institution_id)
    result = await run_sync(
        connection,
        candidate_resolution.preview_conflict,
        institution=key,
        context_fingerprint=fingerprint,
        action=payload.action,
        selected_candidate_id=payload.selected_candidate_id,
        reason=payload.reason,
        reviewer=_reviewer_of(session),
        secret=config.session_secret.get_secret_value(),
    )
    return {
        "context_fingerprint": result.context_fingerprint,
        "field_kind": result.field_kind,
        "verdict": result.verdict,
        "action": result.action,
        "selected_candidate_id": (
            str(result.selected_candidate_id) if result.selected_candidate_id else None
        ),
        "member_candidate_ids": [str(m) for m in result.member_candidate_ids],
        "reason": result.reason,
        "valid": result.valid,
        "blockers": [b.as_dict() for b in result.blockers],
        "reviewer": {"display_name": result.reviewer.display_name},
        "before_action": result.before_action,
        "would_append": result.would_append,
        "creates_field_claim": result.creates_field_claim,
        "modifies_canonical": result.modifies_canonical,
        "preview_token": result.token,
        "issued_at": result.issued_at,
    }


@router.post("/institutions/{institution_id}/candidate-groups/{fingerprint}/apply")
async def apply_candidate_conflict(
    institution_id: uuid.UUID,
    fingerprint: str,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
    payload: Annotated[ConflictApplyRequest, Body()],
) -> dict[str, Any]:
    """Record a conflict resolution. Requires a fresh preview; creates no field_claim."""
    key = await _match_key(connection, institution_id)
    try:
        result = await run_sync(
            connection,
            candidate_resolution.apply_conflict,
            token=payload.preview_token,
            institution=key,
            context_fingerprint=fingerprint,
            action=payload.action,
            selected_candidate_id=payload.selected_candidate_id,
            reason=payload.reason,
            reviewer=_reviewer_of(session),
            secret=config.session_secret.get_secret_value(),
            ttl_seconds=config.preview_ttl_seconds,
        )
    except decision_service.PreviewStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except decision_service.PreviewForgedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except decision_service.DecisionRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _resolution_result_payload(result)


__all__ = ["router"]


@router.post("/promotions/{mapping_id}/preview")
async def preview_promotion(
    mapping_id: uuid.UUID,
    connection: ConnectionDep,
    session: VerifyingSessionDep,
    config: SettingsDep,
    _csrf: CsrfDep,
) -> dict[str, Any]:
    """Section O. Shows what promotion would do. Promotes nothing, and issues no token.

    Promotion is a separate operation from responsibility verification, and deliberately
    has no apply endpoint in this step: section O says prepare the controls, not use them.
    """
    try:
        result = await run_sync(
            connection,
            decision_service.preview_promotion,
            mapping_id=mapping_id,
            expect_sha256=console_reads.APPROVED_MANIFESTS["RESPONSIBILITIES"],
            reviewer=_reviewer_of(session),
            secret=config.session_secret.get_secret_value(),
        )
    except decision_service.DecisionRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    authority = result.authority
    return {
        "mapping_id": str(result.mapping_id),
        "source_ref": result.source_ref,
        "responsibility": result.responsibility,
        "valid": result.valid,
        "blockers": [b.as_dict() for b in result.blockers],
        "manifest_bound": result.manifest_bound,
        "manifest_note": result.manifest_note,
        "authority": {
            "verdict": authority.verdict.value,
            "redirected": authority.redirected,
            "requested_host": authority.requested.host,
            "requested_authority": authority.requested.display,
            "effective_host": authority.effective.host,
            "effective_authority": authority.effective.display,
            "note": authority.blocker,
        },
        "field_bindings_that_would_be_created": [
            {"entity_type": e, "field_path": f} for e, f in result.field_bindings
        ],
        "source_id": str(result.source_id) if result.source_id else None,
        "source_eligibility_before": result.source_eligibility_before,
        "source_eligibility_after": result.source_eligibility_after,
        "promoted_source_id_before": (
            str(result.promoted_source_id_before) if result.promoted_source_id_before else None
        ),
        "promoted_source_id_after": (
            str(result.promoted_source_id_after) if result.promoted_source_id_after else None
        ),
        "field_claim_remains_zero": result.field_claim_remains_zero,
        "canonical_unchanged": result.canonical_unchanged,
        "preview_token": result.token,
        "issued_at": result.issued_at,
        "note": (
            "Promotion is a separate operation and has not been performed. Confirming "
            "below re-checks every fact on this panel before writing anything."
        ),
    }
