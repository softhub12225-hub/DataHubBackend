"""Human scope and conflict resolution: the workflow between extraction and publication.

WHAT THIS ADDS TO WHAT ALREADY EXISTED
======================================
`claims/scope.py` *proposes* a scope and refuses to resolve one. `claims/grouping.py`
computes agreement and conflict groups and refuses to pick a winner. Both were built that
way on purpose, and both left the same gap: there was nowhere for a human to record the
answer. This module is that place.

It adds no new vocabulary. The scope dimensions are the five `scope_dimension` rows the
schema already defines; the conflict classes are `grouping.Agreement`; the precedence rules
are `claims/precedence.py`. What is new is persistence, a preview/confirm cycle, and an
audit trail.

TWO DIMENSIONS, AS THE STEP ASKS -- RECORDED ON FIVE, AS THE SCHEMA DEFINES
===========================================================================
Step 5C.9 asks for applicant jurisdiction and qualification system kept apart. They are.
But `scope_dimension` already splits them further -- `applicant_country`,
`residency_status`, `qualification_country`, `qualification_type`, `qualification_group` --
and those distinctions are real: where an applicant is from, their fee status, where their
award was issued, what kind of award it is, and whether they are on a named list are five
different questions. `precedence.JURISDICTION_DIMENSIONS` and `QUALIFICATION_DIMENSIONS`
map the step's two states onto them, so the reviewer picks two things and the record keeps
five. Collapsing the schema to match the request would have discarded information.

UNSCOPED IS NEVER UNIVERSAL
===========================
Stated in three places because it is the rule most expensive to get wrong: the migration's
CHECK, `ScopeState.can_publish`, and `blockers`. A candidate whose audience nobody stated
cannot be published, and no code path turns silence into "everyone".

NO WINNER IS EVER CHOSEN
========================
`conflict_groups` reports every competing candidate and every source. A reviewer selects
one, rejects one, resolves a duplicate, or leaves it unresolved -- and `LEFT_UNRESOLVED` is
a first-class recorded outcome rather than the absence of a decision, so "somebody looked
and could not tell" is distinguishable from "nobody has looked".

NOTHING HERE CREATES A field_claim
==================================
Every preview reports `creates_field_claim: False`, and it is a constant rather than a
computation because this module contains no code that could create one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.domains.claims import review as review_rules
from app.domains.claims.grouping import Agreement, CandidateRow, group_candidates
from app.domains.claims.precedence import (
    ALL_DIMENSIONS,
    Criterion,
    ScopeState,
    state_for,
)
from app.domains.claims.scope import ScopeResolution, propose_scope
from app.domains.verification.audit import append as append_audit
from app.domains.verification.decisions import (
    DecisionRefusedError,
    PreviewForgedError,
    PreviewStaleError,
    Reviewer,
    issue_token,
    open_token,
)

logger = get_logger(__name__)

#: What a reviewer may do with a conflict or duplicate group. Mirrors the migration.
CONFLICT_ACTIONS: tuple[str, ...] = (
    "RESOLVED_DUPLICATE",
    "SELECTED_SUPPORTED_CLAIM",
    "REJECTED_CONFLICTING",
    "LEFT_UNRESOLVED",
)

#: Audit actions. Distinct names, because a scope resolution and a conflict resolution are
#: different acts and Step 5C.7L's lesson was that sharing an action name makes an audit
#: reader count one thing as another.
AUDIT_SCOPE = "CANDIDATE_SCOPE_RESOLVED"
AUDIT_CONFLICT = "CANDIDATE_CONFLICT_RESOLVED"

#: Group verdicts that need a human before anything downstream may rely on the group.
#:
#: `SINGLE` and `AGREES` are absent because nothing is in dispute. `NOT_COMPARABLE` is
#: absent for a different reason: agreement does not apply to that field kind at all, so
#: there is no question for a human to answer -- see `grouping.Agreement.NOT_COMPARABLE`.
UNRESOLVED_VERDICTS = frozenset(
    {
        Agreement.CONFLICTS.value,
        Agreement.POSSIBLE_DUPLICATE.value,
        Agreement.INSUFFICIENT_CONTEXT.value,
    }
)


class BlockerCode(StrEnum):
    """Why a resolution may not be applied."""

    UNKNOWN_CANDIDATE = "UNKNOWN_CANDIDATE"
    SUPERSEDED_CANDIDATE = "SUPERSEDED_CANDIDATE"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    REASON_REQUIRED = "REASON_REQUIRED"
    FIXTURE_IDENTITY = "FIXTURE_IDENTITY"
    STATE_CRITERIA_MISMATCH = "STATE_CRITERIA_MISMATCH"
    UNKNOWN_DIMENSION = "UNKNOWN_DIMENSION"
    NOT_SCOPEABLE = "NOT_SCOPEABLE"
    SELECTION_NOT_IN_GROUP = "SELECTION_NOT_IN_GROUP"
    GROUP_NOT_FOUND = "GROUP_NOT_FOUND"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True, slots=True)
class Blocker:
    code: BlockerCode
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


# ===========================================================================
# reading the queue
# ===========================================================================

#: Everything a reviewer needs to judge one candidate's scope. Selected explicitly, and
#: restricted to current candidates on both D55 axes by `review_rules.current_only`.
#:
#: S608: the only interpolation is `review_rules.current_only("c")`, whose sole input is
#: the literal alias `"c"`. Both version lists travel as bound array parameters
#: (`CURRENT_PARAMS`), which is what makes the suppression true rather than convenient --
#: the same reasoning, and the same suppression, as `review.current_only` itself.
_QUEUE_SQL = f"""
    SELECT c.id                       AS candidate_id,
           c.field_kind,
           c.source_responsibility    AS responsibility,
           c.value_normalized,
           c.value_raw_text,
           c.evidence_text,
           c.locator,
           c.unresolved_reason,
           c.confidence_band,
           c.confidence_reason,
           c.extractor_name,
           c.extractor_version,
           c.extraction_id,
           ti.match_key               AS institution,
           ti.id                       AS institution_id,
           pcs.source_ref,
           pcs.official_url            AS requested_url,
           pcs.host                    AS requested_host,
           pcs.id                      AS pilot_source_id,
           pcs.acquisition_source_id   AS source_id,
           sm.id                       AS mapping_id,
           sm.verification_status::text AS mapping_status,
           src.publication_eligibility::text AS source_eligibility,
           de.extractor_version        AS document_artifact_version,
           snap.effective_url,
           rev.decision                AS review_decision,
           scope_res.scope_state       AS human_scope_state,
           scope_res.selected_criteria AS human_scope_criteria,
           scope_res.reason            AS human_scope_reason,
           scope_who.display_name      AS human_scope_actor
      FROM field_claim_candidate c
      JOIN pilot_collected_source pcs ON pcs.id = c.pilot_collected_source_id
      JOIN target_institution ti ON ti.id = pcs.target_institution_id
      JOIN extraction de ON de.id = c.extraction_id
      LEFT JOIN source_mapping sm ON sm.id = pcs.promoted_source_mapping_id
      LEFT JOIN source src ON src.id = pcs.acquisition_source_id
      LEFT JOIN LATERAL (
            SELECT s.effective_url FROM snapshot s
             WHERE s.source_id = pcs.acquisition_source_id
             ORDER BY s.observed_at DESC LIMIT 1
      ) snap ON TRUE
      LEFT JOIN LATERAL (
            SELECT r.decision FROM field_claim_candidate_review r
             WHERE r.candidate_id = c.id ORDER BY r.decided_at DESC LIMIT 1
      ) rev ON TRUE
      LEFT JOIN LATERAL (
            SELECT sr.scope_state, sr.selected_criteria, sr.reason, sr.actor_id
              FROM candidate_scope_resolution sr
             WHERE sr.candidate_id = c.id ORDER BY sr.decided_at DESC LIMIT 1
      ) scope_res ON TRUE
      LEFT JOIN app_user scope_who ON scope_who.id = scope_res.actor_id
     WHERE ti.match_key = :institution
       AND {review_rules.current_only("c")}
     ORDER BY pcs.source_ref, c.field_kind, c.id
"""  # noqa: S608


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One current candidate, with everything Step 5C.9 requires on screen."""

    candidate_id: uuid.UUID
    field_kind: str
    responsibility: str
    institution: str
    source_ref: str
    pilot_source_id: uuid.UUID
    mapping_id: uuid.UUID | None
    mapping_status: str | None
    source_eligibility: str
    requested_url: str
    effective_url: str | None
    requested_host: str
    value_normalized: dict[str, Any] | None
    value_raw_text: str
    evidence_text: str
    locator: dict[str, Any]
    heading_path: tuple[str, ...]
    confidence_band: str
    confidence_reason: str | None
    unresolved_reason: str | None
    extractor_name: str
    extractor_version: str
    document_artifact_version: str
    review_decision: str
    # --- scope -----------------------------------------------------------
    machine_resolution: str
    machine_raw_scope_text: str | None
    machine_country_hints: tuple[str, ...]
    machine_qualification_hints: tuple[str, ...]
    machine_category_hints: tuple[str, ...]
    machine_evidence_source: str | None
    suggested_criteria: tuple[dict[str, str], ...]
    human_scope_state: str
    human_scope_criteria: tuple[dict[str, Any], ...]
    human_scope_reason: str | None
    human_scope_actor: str | None
    scopeable: bool
    # --- conflict --------------------------------------------------------
    group_verdict: str
    context_fingerprint: str | None
    group_member_count: int
    conflict_action: str | None
    conflict_actor: str | None

    @property
    def scope_resolved(self) -> bool:
        return ScopeState(self.human_scope_state).is_resolved

    @property
    def conflict_resolved(self) -> bool:
        """Whether a human has settled this group, if it needed settling."""
        if self.group_verdict not in UNRESOLVED_VERDICTS:
            return True
        return self.conflict_action is not None and self.conflict_action != "LEFT_UNRESOLVED"

    @property
    def blockers(self) -> tuple[str, ...]:
        """Distance from a `field_claim`, using the HUMAN scope state.

        Delegates to `review.blockers_for` rather than restating the rule, and feeds it
        the human resolution instead of the machine proposal -- which is the whole point
        of this step: a reviewer clearing scope must actually clear the blocker.
        """
        return review_rules.blockers_for(
            decision_state=self.review_decision,
            is_superseded=False,  # the queue selects current candidates only
            source_eligibility=self.source_eligibility,
            scope_unresolved=not self.scope_resolved,
            in_conflict=not self.conflict_resolved,
            responsibility=self.responsibility,
            field_kind=self.field_kind,
        )


def _fingerprint_context(key: tuple[str, ...]) -> str:
    """SHA-256 of a deterministic context key. See the migration's docstring."""
    blob = json.dumps(list(key), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _suggest(proposal: Any) -> tuple[dict[str, str], ...]:
    """Turn machine hints into the criteria a reviewer would most likely select.

    A *suggestion*, shown alongside the hints it came from and never applied on its own.

    Country hints become `applicant_country`. Qualification hints become
    `qualification_type`. Fee-status categories become `residency_status` -- which is what
    that dimension is for: *"Residency or fee status as the institution defines it."*
    Recording "international" as a residency status is not the same as expanding it into a
    country list, which `claims/scope.py` rightly refuses to do; it records the category as
    the category it is, in the dimension the schema already provides. Without this, every
    ANU candidate would arrive with a DOMESTIC or INTERNATIONAL hint and no suggestion at
    all, which was measured on the real queue.

    `EU`, `TRANSFER` and `MATURE` are deliberately left unsuggested. EU is a supranational
    grouping rather than a residency status, and transfer/mature describe an applicant's
    history rather than their fee status. A reviewer sees the hint and decides.
    """
    from app.domains.claims.precedence import (
        DIM_APPLICANT_COUNTRY,
        DIM_QUALIFICATION_TYPE,
        DIM_RESIDENCY_STATUS,
    )

    #: Categories that are genuinely a residency/fee status.
    residency_like = {"INTERNATIONAL", "DOMESTIC", "HOME", "OVERSEAS", "LOCAL", "NON_LOCAL"}

    suggested: list[dict[str, str]] = []
    for code in sorted(proposal.country_hints):
        suggested.append(
            {"dimension_code": DIM_APPLICANT_COUNTRY, "operator": "EQUALS", "value": code}
        )
    for code in sorted(proposal.qualification_hints):
        suggested.append(
            {"dimension_code": DIM_QUALIFICATION_TYPE, "operator": "EQUALS", "value": code}
        )
    for code in sorted(proposal.category_hints & residency_like):
        suggested.append(
            {"dimension_code": DIM_RESIDENCY_STATUS, "operator": "EQUALS", "value": code}
        )
    return tuple(suggested)


def queue(connection: Connection, *, institution: str) -> list[QueueItem]:
    """The review queue for one institution: current candidates only.

    Superseded candidates are not returned at all, on either axis. Step 5C.9 requires them
    not to be *reviewable*, and the safest way to honour that is for the queue never to
    mention them -- a filter the client could toggle off would eventually be toggled off.
    """
    rows = connection.execute(
        text(_QUEUE_SQL), {"institution": institution, **review_rules.CURRENT_PARAMS}
    ).all()

    # Grouping needs the whole set at once, so build the CandidateRows first.
    candidate_rows: dict[uuid.UUID, CandidateRow] = {}
    for row in rows:
        candidate_rows[uuid.UUID(str(row.candidate_id))] = CandidateRow(
            candidate_id=uuid.UUID(str(row.candidate_id)),
            institution=str(row.institution),
            field_kind=str(row.field_kind),
            value=row.value_normalized,
            unresolved_reason=row.unresolved_reason,
            confidence_band=str(row.confidence_band),
            source_id=uuid.UUID(str(row.source_id)) if row.source_id else uuid.uuid4(),
            url=str(row.requested_url),
            extraction_id=uuid.UUID(str(row.extraction_id)),
            locator=row.locator or {},
            responsibility=str(row.responsibility),
            extractor_name=str(row.extractor_name),
            extractor_version=str(row.extractor_version),
            evidence_text=str(row.evidence_text or ""),
            value_raw_text=str(row.value_raw_text or ""),
        )

    groups = group_candidates(list(candidate_rows.values()))
    verdict_of: dict[uuid.UUID, tuple[str, str, int]] = {}
    for group in groups:
        # EVERY group is fingerprinted, including the singletons `group_candidates` makes
        # for rows it could not key. Nulling those was a dead end: `conflict_groups` drops
        # a group with no fingerprint, so four of ANU's eight candidates reported
        # UNRESOLVED_CONFLICT while the console offered no group to resolve and the
        # resolution table -- keyed on a NOT NULL fingerprint -- had nowhere to put an
        # answer. `AgreementGroup.context_key` is deterministic for those rows too
        # (`candidate=<id>`), so there was never a reason to withhold it.
        fingerprint = _fingerprint_context(group.context_key)
        for member in group.members:
            verdict_of[member.candidate_id] = (group.verdict.value, fingerprint, len(group.members))

    resolutions = _conflict_resolutions(connection, institution=institution)

    items: list[QueueItem] = []
    for row in rows:
        candidate_id = uuid.UUID(str(row.candidate_id))
        candidate = candidate_rows[candidate_id]
        proposal = propose_scope(candidate)
        # Indexed, not `.get` with a fallback. `group_candidates` returns a group for
        # every row it was given -- an unkeyable one becomes its own singleton -- so a
        # missing candidate here would be a bug in grouping, and a default verdict would
        # hide it behind a plausible-looking INSUFFICIENT_CONTEXT.
        verdict, fingerprint, member_count = verdict_of[candidate_id]
        resolved = resolutions.get(fingerprint)
        stored_criteria = row.human_scope_criteria or []
        items.append(
            QueueItem(
                candidate_id=candidate_id,
                field_kind=str(row.field_kind),
                responsibility=str(row.responsibility),
                institution=str(row.institution),
                source_ref=str(row.source_ref),
                pilot_source_id=uuid.UUID(str(row.pilot_source_id)),
                mapping_id=uuid.UUID(str(row.mapping_id)) if row.mapping_id else None,
                mapping_status=str(row.mapping_status) if row.mapping_status else None,
                source_eligibility=str(row.source_eligibility or "NOT_ELIGIBLE"),
                requested_url=str(row.requested_url),
                effective_url=str(row.effective_url) if row.effective_url else None,
                requested_host=str(row.requested_host),
                value_normalized=row.value_normalized,
                value_raw_text=str(row.value_raw_text or ""),
                evidence_text=str(row.evidence_text or ""),
                locator=row.locator or {},
                heading_path=tuple(str(p) for p in (row.locator or {}).get("heading_path") or []),
                confidence_band=str(row.confidence_band),
                confidence_reason=(str(row.confidence_reason) if row.confidence_reason else None),
                unresolved_reason=(str(row.unresolved_reason) if row.unresolved_reason else None),
                extractor_name=str(row.extractor_name),
                extractor_version=str(row.extractor_version),
                document_artifact_version=str(row.document_artifact_version),
                review_decision=str(row.review_decision or "UNREVIEWED"),
                machine_resolution=proposal.resolution.value,
                machine_raw_scope_text=proposal.raw_scope_text,
                machine_country_hints=tuple(sorted(proposal.country_hints)),
                machine_qualification_hints=tuple(sorted(proposal.qualification_hints)),
                machine_category_hints=tuple(sorted(proposal.category_hints)),
                machine_evidence_source=proposal.evidence_source,
                suggested_criteria=_suggest(proposal),
                human_scope_state=str(
                    row.human_scope_state
                    or (
                        ScopeState.NOT_APPLICABLE.value
                        if proposal.resolution is ScopeResolution.NOT_APPLICABLE
                        else ScopeState.UNSCOPED.value
                    )
                ),
                human_scope_criteria=tuple(stored_criteria),
                human_scope_reason=(
                    str(row.human_scope_reason) if row.human_scope_reason else None
                ),
                human_scope_actor=(str(row.human_scope_actor) if row.human_scope_actor else None),
                scopeable=proposal.resolution is not ScopeResolution.NOT_APPLICABLE,
                group_verdict=verdict,
                context_fingerprint=fingerprint,
                group_member_count=member_count,
                conflict_action=resolved[0] if resolved else None,
                conflict_actor=resolved[1] if resolved else None,
            )
        )
    return items


def _conflict_resolutions(
    connection: Connection, *, institution: str
) -> dict[str, tuple[str, str | None]]:
    """The latest conflict resolution per context fingerprint, for one institution."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (cr.context_fingerprint)
                   cr.context_fingerprint, cr.action, who.display_name AS actor
              FROM candidate_conflict_resolution cr
              LEFT JOIN app_user who ON who.id = cr.actor_id
             WHERE cr.institution = :institution
             ORDER BY cr.context_fingerprint, cr.decided_at DESC
            """
        ),
        {"institution": institution},
    ).all()
    return {str(row.context_fingerprint): (str(row.action), row.actor) for row in rows}


# ===========================================================================
# scope resolution: preview and apply
# ===========================================================================


@dataclass(slots=True)
class ScopePreview:
    """What recording this scope decision would do. Computed server-side."""

    candidate_id: uuid.UUID
    state: str
    criteria: tuple[dict[str, Any], ...]
    reason: str
    reviewer: Reviewer
    before_state: str
    before_criteria: tuple[dict[str, Any], ...]
    blockers_before: tuple[str, ...]
    blockers_after: tuple[str, ...]
    blockers: list[Blocker] = field(default_factory=list)
    token: str | None = None
    issued_at: datetime | None = None

    @property
    def valid(self) -> bool:
        return not self.blockers

    @property
    def would_append(self) -> str | None:
        return AUDIT_SCOPE if self.valid else None

    creates_field_claim: bool = False
    modifies_canonical: bool = False


def _scope_fingerprint(
    *,
    candidate_id: uuid.UUID,
    state: str,
    criteria: tuple[dict[str, Any], ...],
    reason: str,
    reviewer: Reviewer,
    before_state: str,
    group_verdict: str,
    source_eligibility: str,
) -> str:
    payload = {
        "kind": "candidate_scope",
        "candidate": str(candidate_id),
        "state": state,
        "criteria": sorted(json.dumps(c, sort_keys=True) for c in criteria),
        "reason": reason.strip(),
        "reviewer": str(reviewer.id),
        "before_state": before_state,
        "group_verdict": group_verdict,
        "source_eligibility": source_eligibility,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _find(items: list[QueueItem], candidate_id: uuid.UUID) -> QueueItem | None:
    return next((item for item in items if item.candidate_id == candidate_id), None)


def _validate_criteria(criteria: tuple[dict[str, Any], ...]) -> list[Blocker]:
    problems: list[Blocker] = []
    for entry in criteria:
        dimension = str(entry.get("dimension_code", ""))
        if dimension not in ALL_DIMENSIONS:
            problems.append(
                Blocker(
                    BlockerCode.UNKNOWN_DIMENSION,
                    f"{dimension!r} is not a scope_dimension; "
                    f"expected one of {sorted(ALL_DIMENSIONS)}",
                )
            )
    return problems


def preview_scope(
    connection: Connection,
    *,
    institution: str,
    candidate_id: uuid.UUID,
    state: str,
    criteria: tuple[dict[str, Any], ...],
    reason: str,
    reviewer: Reviewer,
    secret: str,
    now: datetime | None = None,
) -> ScopePreview:
    """Every check, then a token bound to what was checked. Writes nothing."""
    items = queue(connection, institution=institution)
    item = _find(items, candidate_id)
    blockers: list[Blocker] = []

    if item is None:
        # Either it does not exist or it is superseded; the queue cannot tell them apart
        # and neither answer makes it reviewable.
        return ScopePreview(
            candidate_id=candidate_id,
            state=state,
            criteria=criteria,
            reason=reason,
            reviewer=reviewer,
            before_state=ScopeState.UNSCOPED.value,
            before_criteria=(),
            blockers_before=(),
            blockers_after=(),
            blockers=[
                Blocker(
                    BlockerCode.UNKNOWN_CANDIDATE,
                    f"{candidate_id} is not a current candidate for {institution}. "
                    "Superseded candidates are deliberately not reviewable.",
                )
            ],
        )

    if reviewer.is_test:
        blockers.append(
            Blocker(
                BlockerCode.FIXTURE_IDENTITY,
                f"{reviewer.email} is a [TEST ONLY] identity and may not decide real data",
            )
        )
    try:
        chosen = ScopeState(state)
    except ValueError:
        blockers.append(Blocker(BlockerCode.UNKNOWN_STATE, f"{state!r} is not a scope state"))
        chosen = ScopeState.UNSCOPED
    if not reason.strip():
        blockers.append(Blocker(BlockerCode.REASON_REQUIRED, "a scope decision must say why"))
    blockers.extend(_validate_criteria(criteria))
    if not item.scopeable and chosen is not ScopeState.NOT_APPLICABLE:
        blockers.append(
            Blocker(
                BlockerCode.NOT_SCOPEABLE,
                f"{item.field_kind} carries no applicant scope, so only NOT_APPLICABLE fits",
            )
        )

    # The state must match what the criteria amount to, or the record would claim a
    # dimension it does not carry.
    if not blockers and chosen not in (
        ScopeState.UNSCOPED,
        ScopeState.UNIVERSAL_EXPLICIT,
        ScopeState.NOT_APPLICABLE,
    ):
        built = {
            Criterion(
                dimension_code=str(entry["dimension_code"]),
                operator=str(entry.get("operator", "EQUALS")),
                value=entry.get("value"),
                value_ref=entry.get("value_ref"),
            )
            for entry in criteria
        }
        derived = state_for(frozenset(built))
        if derived is not chosen:
            blockers.append(
                Blocker(
                    BlockerCode.STATE_CRITERIA_MISMATCH,
                    f"the selected criteria amount to {derived.value}, not {chosen.value}",
                )
            )
    if chosen in (ScopeState.UNSCOPED, ScopeState.UNIVERSAL_EXPLICIT) and criteria:
        blockers.append(
            Blocker(
                BlockerCode.STATE_CRITERIA_MISMATCH,
                f"{chosen.value} carries no criteria, and {len(criteria)} were selected",
            )
        )

    after = review_rules.blockers_for(
        decision_state=item.review_decision,
        is_superseded=False,
        source_eligibility=item.source_eligibility,
        scope_unresolved=not chosen.is_resolved,
        in_conflict=not item.conflict_resolved,
        responsibility=item.responsibility,
        field_kind=item.field_kind,
    )

    result = ScopePreview(
        candidate_id=candidate_id,
        state=state,
        criteria=criteria,
        reason=reason,
        reviewer=reviewer,
        before_state=item.human_scope_state,
        before_criteria=item.human_scope_criteria,
        blockers_before=item.blockers,
        blockers_after=after,
        blockers=blockers,
    )
    if result.valid:
        issued = now or datetime.now(UTC)
        result.issued_at = issued
        result.token = issue_token(
            secret=secret,
            fingerprint=_scope_fingerprint(
                candidate_id=candidate_id,
                state=state,
                criteria=criteria,
                reason=reason,
                reviewer=reviewer,
                before_state=item.human_scope_state,
                group_verdict=item.group_verdict,
                source_eligibility=item.source_eligibility,
            ),
            mapping_id=candidate_id,
            issued_at=issued,
        )
    return result


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """What was written, read back from the database."""

    candidate_id: uuid.UUID | None
    context_fingerprint: str | None
    action: str
    audit_action: str
    audit_seq: int
    audit_chain_ok: bool
    reviewer: Reviewer
    before_state: str
    after_state: str
    blockers_now: tuple[str, ...]
    field_claim: int
    canonical_rows: int

    @property
    def canonical_unchanged(self) -> bool:
        return self.canonical_rows == 0


def apply_scope(
    connection: Connection,
    *,
    token: str,
    institution: str,
    candidate_id: uuid.UUID,
    state: str,
    criteria: tuple[dict[str, Any], ...],
    reason: str,
    reviewer: Reviewer,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> ResolutionResult:
    """Record a scope decision, refusing unless the facts are still the previewed ones."""
    moment = now or datetime.now(UTC)
    payload = open_token(secret, token)
    if payload.get("m") != str(candidate_id):
        raise PreviewStaleError("PREVIEW_STALE: the preview names a different candidate")
    try:
        issued_at = datetime.fromisoformat(str(payload.get("t")))
    except ValueError as exc:
        raise PreviewForgedError("PREVIEW_INVALID: unreadable issue time") from exc
    if (moment - issued_at).total_seconds() > ttl_seconds:
        raise PreviewStaleError(
            f"PREVIEW_STALE: the preview is older than {ttl_seconds}s. Preview again."
        )

    fresh = preview_scope(
        connection,
        institution=institution,
        candidate_id=candidate_id,
        state=state,
        criteria=criteria,
        reason=reason,
        reviewer=reviewer,
        secret=secret,
        now=issued_at,
    )
    if not fresh.valid:
        raise DecisionRefusedError(
            "; ".join(f"{b.code.value}: {b.message}" for b in fresh.blockers)
        )
    if fresh.token is None or open_token(secret, fresh.token)["f"] != str(payload.get("f")):
        raise PreviewStaleError(
            "PREVIEW_STALE: the candidate, its group or its source changed after the "
            "preview. Preview again and re-read it before confirming."
        )

    connection.execute(
        text(
            """
            INSERT INTO candidate_scope_resolution
                   (candidate_id, actor_id, scope_state, selected_criteria, reason,
                    decided_at)
            VALUES (:candidate, :actor, :state, CAST(:criteria AS jsonb), :reason, now())
            """
        ),
        {
            "candidate": candidate_id,
            "actor": reviewer.id,
            "state": state,
            "criteria": json.dumps(list(criteria), sort_keys=True),
            "reason": reason,
        },
    )
    append_audit(
        connection,
        actor_id=reviewer.id,
        action=AUDIT_SCOPE,
        object_type="field_claim_candidate",
        object_id=candidate_id,
        reason=reason,
        before={"scope_state": fresh.before_state, "blockers": list(fresh.blockers_before)},
        after={
            "scope_state": state,
            "criteria": list(criteria),
            "blockers": list(fresh.blockers_after),
        },
    )
    return _read_back(
        connection,
        candidate_id=candidate_id,
        context_fingerprint=None,
        action=state,
        audit_action=AUDIT_SCOPE,
        reviewer=reviewer,
        before_state=fresh.before_state,
        after_state=state,
        institution=institution,
    )


def _read_back(
    connection: Connection,
    *,
    candidate_id: uuid.UUID | None,
    context_fingerprint: str | None,
    action: str,
    audit_action: str,
    reviewer: Reviewer,
    before_state: str,
    after_state: str,
    institution: str,
) -> ResolutionResult:
    """Section N of Step 5C.7L, applied here: report what the database says, not what we sent."""
    seq = connection.execute(
        text(
            "SELECT seq FROM audit_log WHERE action = :a AND actor_id = :actor "
            " ORDER BY seq DESC LIMIT 1"
        ),
        {"a": audit_action, "actor": reviewer.id},
    ).scalar_one()
    chain_ok = not connection.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all()
    counts = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM field_claim) AS field_claim,"
            "       (SELECT count(*) FROM university) + (SELECT count(*) FROM program)"
            "     + (SELECT count(*) FROM tuition) AS canonical_rows"
        )
    ).one()
    blockers_now: tuple[str, ...] = ()
    if candidate_id is not None:
        item = _find(queue(connection, institution=institution), candidate_id)
        blockers_now = item.blockers if item else ()
    return ResolutionResult(
        candidate_id=candidate_id,
        context_fingerprint=context_fingerprint,
        action=action,
        audit_action=audit_action,
        audit_seq=int(seq),
        audit_chain_ok=bool(chain_ok),
        reviewer=reviewer,
        before_state=before_state,
        after_state=after_state,
        blockers_now=blockers_now,
        field_claim=int(counts.field_claim),
        canonical_rows=int(counts.canonical_rows),
    )


# ===========================================================================
# conflict resolution: preview and apply
# ===========================================================================


@dataclass(slots=True)
class ConflictPreview:
    """What resolving this group would record."""

    context_fingerprint: str
    institution: str
    field_kind: str
    verdict: str
    action: str
    selected_candidate_id: uuid.UUID | None
    member_candidate_ids: tuple[uuid.UUID, ...]
    reason: str
    reviewer: Reviewer
    before_action: str | None
    blockers: list[Blocker] = field(default_factory=list)
    token: str | None = None
    issued_at: datetime | None = None

    @property
    def valid(self) -> bool:
        return not self.blockers

    @property
    def would_append(self) -> str | None:
        return AUDIT_CONFLICT if self.valid else None

    creates_field_claim: bool = False
    modifies_canonical: bool = False


def _conflict_fingerprint(
    *,
    context_fingerprint: str,
    action: str,
    selected: uuid.UUID | None,
    members: tuple[uuid.UUID, ...],
    reason: str,
    reviewer: Reviewer,
    verdict: str,
    before_action: str | None,
) -> str:
    payload = {
        "kind": "candidate_conflict",
        "context": context_fingerprint,
        "action": action,
        "selected": str(selected) if selected else None,
        "members": sorted(str(m) for m in members),
        "reason": reason.strip(),
        "reviewer": str(reviewer.id),
        "verdict": verdict,
        "before_action": before_action,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def conflict_groups(connection: Connection, *, institution: str) -> list[dict[str, Any]]:
    """Every agreement/conflict group over this institution's current candidates."""
    items = queue(connection, institution=institution)
    by_fingerprint: dict[str, list[QueueItem]] = {}
    for item in items:
        if item.context_fingerprint is None:
            continue
        by_fingerprint.setdefault(item.context_fingerprint, []).append(item)

    groups: list[dict[str, Any]] = []
    for fingerprint, members in sorted(by_fingerprint.items()):
        first = members[0]
        groups.append(
            {
                "context_fingerprint": fingerprint,
                "institution": first.institution,
                "field_kind": first.field_kind,
                "verdict": first.group_verdict,
                "needs_resolution": first.group_verdict in UNRESOLVED_VERDICTS,
                "resolved": first.conflict_resolved,
                "action": first.conflict_action,
                "actor": first.conflict_actor,
                "members": [
                    {
                        "candidate_id": str(m.candidate_id),
                        "source_ref": m.source_ref,
                        "value_raw_text": m.value_raw_text,
                        "value_normalized": m.value_normalized,
                        "confidence_band": m.confidence_band,
                        "requested_url": m.requested_url,
                        "review_decision": m.review_decision,
                        "human_scope_state": m.human_scope_state,
                    }
                    for m in members
                ],
            }
        )
    return groups


def preview_conflict(
    connection: Connection,
    *,
    institution: str,
    context_fingerprint: str,
    action: str,
    selected_candidate_id: uuid.UUID | None,
    reason: str,
    reviewer: Reviewer,
    secret: str,
    now: datetime | None = None,
) -> ConflictPreview:
    """Every check, then a token. Writes nothing. Never picks a winner itself."""
    groups = {
        g["context_fingerprint"]: g for g in conflict_groups(connection, institution=institution)
    }
    group = groups.get(context_fingerprint)
    blockers: list[Blocker] = []

    if group is None:
        return ConflictPreview(
            context_fingerprint=context_fingerprint,
            institution=institution,
            field_kind="",
            verdict="",
            action=action,
            selected_candidate_id=selected_candidate_id,
            member_candidate_ids=(),
            reason=reason,
            reviewer=reviewer,
            before_action=None,
            blockers=[
                Blocker(
                    BlockerCode.GROUP_NOT_FOUND,
                    f"no current group with fingerprint {context_fingerprint[:12]}... for "
                    f"{institution}. The context may have changed since it was listed.",
                )
            ],
        )

    members = tuple(uuid.UUID(m["candidate_id"]) for m in group["members"])
    if reviewer.is_test:
        blockers.append(
            Blocker(
                BlockerCode.FIXTURE_IDENTITY,
                f"{reviewer.email} is a [TEST ONLY] identity and may not decide real data",
            )
        )
    if action not in CONFLICT_ACTIONS:
        blockers.append(Blocker(BlockerCode.UNKNOWN_ACTION, f"{action!r} is not a conflict action"))
    if not reason.strip():
        blockers.append(Blocker(BlockerCode.REASON_REQUIRED, "a resolution must say why"))
    if action == "SELECTED_SUPPORTED_CLAIM" and selected_candidate_id not in members:
        blockers.append(
            Blocker(
                BlockerCode.SELECTION_NOT_IN_GROUP,
                "the selected candidate is not a member of this group",
            )
        )
    if action != "SELECTED_SUPPORTED_CLAIM" and selected_candidate_id is not None:
        blockers.append(
            Blocker(
                BlockerCode.UNKNOWN_ACTION,
                f"{action} names a candidate, and only SELECTED_SUPPORTED_CLAIM may",
            )
        )

    result = ConflictPreview(
        context_fingerprint=context_fingerprint,
        institution=institution,
        field_kind=str(group["field_kind"]),
        verdict=str(group["verdict"]),
        action=action,
        selected_candidate_id=selected_candidate_id,
        member_candidate_ids=members,
        reason=reason,
        reviewer=reviewer,
        before_action=group["action"],
        blockers=blockers,
    )
    if result.valid:
        issued = now or datetime.now(UTC)
        result.issued_at = issued
        result.token = issue_token(
            secret=secret,
            fingerprint=_conflict_fingerprint(
                context_fingerprint=context_fingerprint,
                action=action,
                selected=selected_candidate_id,
                members=members,
                reason=reason,
                reviewer=reviewer,
                verdict=str(group["verdict"]),
                before_action=group["action"],
            ),
            # The token's `m` slot carries the group identity here. Namespaced by `kind`
            # in the fingerprint, so it cannot be spent as a candidate or mapping token.
            mapping_id=uuid.UUID(context_fingerprint[:32]),
            issued_at=issued,
        )
    return result


def apply_conflict(
    connection: Connection,
    *,
    token: str,
    institution: str,
    context_fingerprint: str,
    action: str,
    selected_candidate_id: uuid.UUID | None,
    reason: str,
    reviewer: Reviewer,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> ResolutionResult:
    """Record a conflict resolution, refusing unless the group is still the previewed one."""
    moment = now or datetime.now(UTC)
    payload = open_token(secret, token)
    if payload.get("m") != str(uuid.UUID(context_fingerprint[:32])):
        raise PreviewStaleError("PREVIEW_STALE: the preview names a different group")
    try:
        issued_at = datetime.fromisoformat(str(payload.get("t")))
    except ValueError as exc:
        raise PreviewForgedError("PREVIEW_INVALID: unreadable issue time") from exc
    if (moment - issued_at).total_seconds() > ttl_seconds:
        raise PreviewStaleError(
            f"PREVIEW_STALE: the preview is older than {ttl_seconds}s. Preview again."
        )

    fresh = preview_conflict(
        connection,
        institution=institution,
        context_fingerprint=context_fingerprint,
        action=action,
        selected_candidate_id=selected_candidate_id,
        reason=reason,
        reviewer=reviewer,
        secret=secret,
        now=issued_at,
    )
    if not fresh.valid:
        raise DecisionRefusedError(
            "; ".join(f"{b.code.value}: {b.message}" for b in fresh.blockers)
        )
    if fresh.token is None or open_token(secret, fresh.token)["f"] != str(payload.get("f")):
        raise PreviewStaleError(
            "PREVIEW_STALE: the group's members, verdict or prior resolution changed "
            "after the preview. Preview again and re-read it before confirming."
        )

    connection.execute(
        text(
            """
            INSERT INTO candidate_conflict_resolution
                   (context_fingerprint, institution, field_kind, actor_id, action,
                    selected_candidate_id, member_candidate_ids, verdict_at_decision,
                    reason, decided_at)
            VALUES (:fingerprint, :institution, :field_kind, :actor, :action,
                    :selected, CAST(:members AS jsonb), :verdict, :reason, now())
            """
        ),
        {
            "fingerprint": context_fingerprint,
            "institution": institution,
            "field_kind": fresh.field_kind,
            "actor": reviewer.id,
            "action": action,
            "selected": selected_candidate_id,
            "members": json.dumps(sorted(str(m) for m in fresh.member_candidate_ids)),
            "verdict": fresh.verdict,
            "reason": reason,
        },
    )
    append_audit(
        connection,
        actor_id=reviewer.id,
        action=AUDIT_CONFLICT,
        object_type="field_claim_candidate_group",
        object_id=selected_candidate_id,
        reason=reason,
        before={"action": fresh.before_action, "verdict": fresh.verdict},
        after={
            "action": action,
            "context_fingerprint": context_fingerprint,
            "members": sorted(str(m) for m in fresh.member_candidate_ids),
            "selected": str(selected_candidate_id) if selected_candidate_id else None,
        },
    )
    return _read_back(
        connection,
        candidate_id=selected_candidate_id,
        context_fingerprint=context_fingerprint,
        action=action,
        audit_action=AUDIT_CONFLICT,
        reviewer=reviewer,
        before_state=str(fresh.before_action or "UNRESOLVED"),
        after_state=action,
        institution=institution,
    )


# ===========================================================================
# the blocker matrix
# ===========================================================================


def blocker_matrix(connection: Connection, *, institution: str) -> dict[str, Any]:
    """Promotion-readiness for one institution: every blocker, counted.

    Reports every blocker on every current candidate rather than the first, because a
    reviewer clearing one wants to know whether it was the only one.
    """
    items = queue(connection, institution=institution)
    counts: dict[str, int] = {}
    for item in items:
        for blocker in item.blockers:
            counts[blocker] = counts.get(blocker, 0) + 1
    by_field: dict[str, dict[str, int]] = {}
    for item in items:
        bucket = by_field.setdefault(item.field_kind, {"current": 0, "ready": 0})
        bucket["current"] += 1
        if not item.blockers:
            bucket["ready"] += 1
    return {
        "institution": institution,
        "current_candidates": len(items),
        "ready": sum(1 for item in items if not item.blockers),
        "scope_unresolved": sum(1 for item in items if not item.scope_resolved),
        "conflict_unresolved": sum(1 for item in items if not item.conflict_resolved),
        "blocker_counts": dict(sorted(counts.items())),
        "by_field_kind": dict(sorted(by_field.items())),
    }


__all__ = [
    "AUDIT_CONFLICT",
    "AUDIT_SCOPE",
    "CONFLICT_ACTIONS",
    "UNRESOLVED_VERDICTS",
    "Blocker",
    "BlockerCode",
    "ConflictPreview",
    "QueueItem",
    "ResolutionResult",
    "ScopePreview",
    "apply_conflict",
    "apply_scope",
    "blocker_matrix",
    "conflict_groups",
    "preview_conflict",
    "preview_scope",
    "queue",
]
