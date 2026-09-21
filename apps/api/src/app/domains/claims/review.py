"""Candidate review: current-version semantics, decisions, queues and readiness.

WHAT "CURRENT" MEANS, AND WHY EVERY QUERY HAS TO SAY IT
=======================================================
The candidate table is append-only and rules are versioned, so a corrected rule leaves
its earlier output in place as history (D48). Today that is 4,114 superseded rows beside
1,941 current ones -- more than twice as many. A review total that silently included
them would be wrong by a factor of three, and it would look plausible.

So there is exactly one definition of "current", `CURRENT_RULES`, derived from the
runner's own registry rather than written out again. Everything in this module filters
on it. The database view `candidate_review_state` carries a frozen copy for SQL
consumers, and `test_the_view_and_the_registry_agree_on_current_versions` asserts the
two have not drifted -- a frozen copy nobody checks is how a report starts counting
history as though it were live.

A DECISION IS NOT A DERIVED STATE
=================================
The instruction suggested one state set: `UNREVIEWED | ACCEPTED | REJECTED |
NEEDS_CONTEXT | NEEDS_SCOPE_MAPPING | SOURCE_NOT_VERIFIED | SUPERSEDED`. Three of those
are not decisions. Whether a candidate is superseded, whether its source has been
verified and whether its scope resolved are all things a machine knows at any moment.

Collapsing them into the human's decision would make one column answer two questions --
the mistake D39 corrected for `fetch_eligibility` -- and would hide a reviewer's
judgement behind a fact about the source. Section 30 asks for the opposite: "Keep
candidate review and source eligibility separate." A reviewer who accepts a candidate
from an unverified source has made a real judgement about the extraction, and it must
survive the source being unverified.

CONFIDENCE IS NOT REVIEW STATE EITHER
=====================================
Section 4. `HIGH` describes how unambiguous the *extraction* was, not whether the fact
is true: a labelled table cell can hold a number the university has since changed, and a
`LOW` sentence can be exactly right. Nothing here maps a band to a decision, and
`test_no_band_implies_a_decision` asserts it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Connection, text

from app.domains.claims.model import ROUTING, FieldKind
from app.domains.claims.runner import CURRENT_DOCUMENT_VERSIONS, EXTRACTORS

#: (extractor, version) pairs the current rules produce, derived from the registry.
CURRENT_RULES: tuple[tuple[str, str], ...] = tuple(
    sorted((name, version) for _, name, version in EXTRACTORS.values())
)

#: The same pairs as a bound parameter, so a query interpolates nothing.
CURRENT_RULE_KEYS: list[str] = [f"{name}@{version}" for name, version in CURRENT_RULES]


#: Document artifact versions that count as current, re-exported so a caller binds one
#: module's constants rather than reaching into two.
CURRENT_DOCUMENTS: list[str] = list(CURRENT_DOCUMENT_VERSIONS)

#: Everything `current_only()` needs bound, as one mapping. Call sites spread this
#: rather than naming the parameters, so adding an axis to the predicate cannot leave a
#: query silently binding only half of it.
CURRENT_PARAMS: dict[str, list[str]] = {
    "current_rules": CURRENT_RULE_KEYS,
    "current_documents": CURRENT_DOCUMENTS,
}


#: The predicate, for an aliased table. The alias is a literal at every call site.
def current_only(alias: str = "c") -> str:
    """SQL selecting only current candidates: current rule **and** current document.

    Two axes, because they answer different questions. A rule version answers "is this
    what the rule says now?"; the document artifact version answers "is this what the
    page says now?". Section 8 forbids bumping a rule version merely because the
    document changed, and four of the six rules were measured to produce byte-identical
    statements across the v1 -> v2 change -- so without the second axis their v1 output
    would stay current forever and every count would double.
    """
    # S608: the only thing interpolated is `alias`, a literal at every call site. Both
    # version lists travel as bound array parameters, which is what makes the
    # suppression true rather than convenient.
    return (
        f"({alias}.extractor_name || '@' || {alias}.extractor_version) = ANY(:current_rules)"  # noqa: S608
        f" AND EXISTS (SELECT 1 FROM extraction de WHERE de.id = {alias}.extraction_id"
        f" AND de.extractor_version = ANY(:current_documents))"
    )


class Decision(StrEnum):
    """What a human may decide about a candidate.

    `SUPERSEDED` and `SOURCE_NOT_VERIFIED` are deliberately absent -- see the module
    docstring. They are derived, and `candidate_review_state` exposes them separately.
    """

    ACCEPTED = "ACCEPTED"
    """The extraction is right about what this page says. **Not** permission to publish."""

    REJECTED = "REJECTED"
    """The extraction is wrong, or this is not a fact of this kind."""

    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    """Might be right; the evidence does not carry enough to tell."""

    NEEDS_SCOPE_MAPPING = "NEEDS_SCOPE_MAPPING"
    """Right as far as it goes, but who it applies to is unresolved (section 12)."""


#: Why. The first six are `review_decision.reason_code`'s existing vocabulary, reused so
#: that rejections of candidates and returns of proposals can be reported together.
REASON_CODES: frozenset[str] = frozenset(
    {
        "EVIDENCE_INSUFFICIENT",
        "WRONG_SOURCE",
        "PARSE_ERROR",
        "NOT_OFFICIAL",
        "NEEDS_RECOLLECTION",
        "OTHER",
        "NOT_A_FACT_OF_THIS_KIND",
        "SITE_CHROME",
        "MARKETING_PROSE",
        "SCOPE_AMBIGUOUS",
        "PROGRAM_CONTEXT_MISSING",
        "SUPERSEDED_BY_NEWER_RULE",
        "CONFLICTS_WITH_ANOTHER_SOURCE",
        "CORRECT_AS_EXTRACTED",
    }
)

#: Fields where a wrong value costs a student money or an application (section 27).
HIGH_RISK_FIELDS: frozenset[str] = frozenset(
    {
        FieldKind.TUITION.value,
        FieldKind.APPLICATION_DEADLINE.value,
        FieldKind.LANGUAGE_OVERALL_SCORE.value,
        FieldKind.LANGUAGE_COMPONENT_SCORE.value,
        FieldKind.ADMISSION_REQUIREMENT.value,
    }
)

#: Which extractor key produces which field kind. Declared rather than inferred from the
#: data, because it is the contract routing enforces: inferring it from what the rules
#: happened to emit would make the validator agree with any bug it was meant to catch.
FIELD_KIND_EXTRACTOR: dict[str, str] = {
    FieldKind.TUITION.value: "tuition",
    FieldKind.LANGUAGE_TEST.value: "language",
    FieldKind.LANGUAGE_OVERALL_SCORE.value: "language",
    FieldKind.LANGUAGE_COMPONENT_SCORE.value: "language",
    FieldKind.APPLICATION_DEADLINE.value: "deadline",
    FieldKind.ACADEMIC_CALENDAR_EVENT.value: "calendar",
    FieldKind.ADMISSION_REQUIREMENT.value: "admission",
    FieldKind.PROGRAM_NAME.value: "program",
    FieldKind.DEGREE_LEVEL.value: "program",
    FieldKind.DISCIPLINE_HINT.value: "program",
    FieldKind.DURATION.value: "program",
    FieldKind.STUDY_MODE.value: "program",
    FieldKind.CAMPUS.value: "program",
    FieldKind.FACULTY_OR_SCHOOL.value: "program",
}


class BodyConfirmation(StrEnum):
    """Did the parser explicitly label this candidate's text as body content?

    Section 9 asks program grouping to exclude navigation "unless the document parser
    explicitly labelled the content as real body content". `Block.container` answers
    that for block-derived candidates. For link-derived ones it cannot: `Link` records
    no container at all, so a programme name taken from anchor text is *unconfirmed* by
    construction -- which is a gap in the document schema, not a property of the page.
    """

    CONFIRMED = "CONFIRMED"
    """`main`, `article`, or a PDF page."""

    UNCONFIRMED = "UNCONFIRMED"
    """A block with no semantic wrapper. Could be body text; could be a `<div>` menu."""

    PERIPHERAL = "PERIPHERAL"
    """`aside` or `form`: kept as content (D45) but not the page's main answer."""

    NOT_RECORDED = "NOT_RECORDED"
    """Link-derived. The document schema carries no container for a `Link`."""


#: Containers the parser labels as the page's own body.
BODY_CONTAINERS: frozenset[str] = frozenset({"main", "article", "pdf"})
PERIPHERAL_CONTAINERS: frozenset[str] = frozenset({"aside", "form"})


def body_confirmation(locator: dict[str, object], container: str | None) -> BodyConfirmation:
    """Classify where a candidate's text sat, for section 9."""
    if locator.get("kind") == "link":
        return BodyConfirmation.NOT_RECORDED
    if container in BODY_CONTAINERS:
        return BodyConfirmation.CONFIRMED
    if container in PERIPHERAL_CONTAINERS:
        return BodyConfirmation.PERIPHERAL
    return BodyConfirmation.UNCONFIRMED


# ===========================================================================
# Recording a decision (section 29)
# ===========================================================================


class ReviewError(Exception):
    """A review decision that the service refuses to record."""


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What recording one decision did."""

    candidate_id: uuid.UUID
    decision: Decision
    reason_code: str
    previous: str
    review_count: int


def record_decision(
    connection: Connection,
    *,
    candidate_id: uuid.UUID,
    actor_id: uuid.UUID,
    decision: Decision,
    reason_code: str,
    reason_text: str | None = None,
    decided_at: datetime | None = None,
) -> ReviewOutcome:
    """Append one human decision. Never updates a previous one.

    Refuses rather than guesses on every input it cannot verify: an unknown candidate,
    an unknown actor, an unknown reason code. A review decision that names a candidate
    nobody can find is not auditable, which is the only reason the row exists.

    A **superseded** candidate may still be decided on, deliberately: a reviewer looking
    at last week's queue should be able to reject something, and refusing would leave
    them unable to record what they saw. The state view flags it, and the active queues
    exclude it.
    """
    if reason_code not in REASON_CODES:
        raise ReviewError(
            f"unknown reason code {reason_code!r}; one of {sorted(REASON_CODES)} is required"
        )
    if reason_code == "OTHER" and not (reason_text or "").strip():
        raise ReviewError("reason code OTHER explains nothing on its own; give reason_text")

    exists = connection.execute(
        text("SELECT 1 FROM field_claim_candidate WHERE id = :id"), {"id": candidate_id}
    ).first()
    if exists is None:
        raise ReviewError(f"no candidate {candidate_id}")
    actor = connection.execute(
        text("SELECT 1 FROM app_user WHERE id = :id"), {"id": actor_id}
    ).first()
    if actor is None:
        raise ReviewError(f"no actor {actor_id}; a decision must name someone answerable")

    previous = connection.execute(
        text("SELECT decision_state FROM candidate_review_state WHERE candidate_id = :id"),
        {"id": candidate_id},
    ).first()

    connection.execute(
        text(
            "INSERT INTO field_claim_candidate_review "
            "  (id, candidate_id, actor_id, decision, reason_code, reason_text, decided_at) "
            "VALUES (:id, :candidate, :actor, :decision, :code, :note, "
            "        coalesce(:decided_at, now()))"
        ),
        {
            "id": uuid.uuid4(),
            "candidate": candidate_id,
            "actor": actor_id,
            "decision": decision.value,
            "code": reason_code,
            "note": reason_text,
            "decided_at": decided_at,
        },
    )
    count = connection.execute(
        text("SELECT count(*) FROM field_claim_candidate_review WHERE candidate_id = :id"),
        {"id": candidate_id},
    ).scalar_one()
    return ReviewOutcome(
        candidate_id=candidate_id,
        decision=decision,
        reason_code=reason_code,
        previous=str(previous[0]) if previous else "UNREVIEWED",
        review_count=int(count),
    )


# ===========================================================================
# Review priority (section 27)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Priority:
    """A priority with its reasons. Never a bare number.

    Section 27 forbids a black-box score, so the score is a sum of named, signed
    contributions and every one of them is returned beside it. A reviewer disagreeing
    with the ordering can see exactly which factor put a row where it is.
    """

    score: int
    reasons: tuple[str, ...]

    def explain(self) -> str:
        return "; ".join(self.reasons)


#: Each factor is (points, why). Positive raises the queue position, negative lowers it.
#: Written out rather than tuned, because a weight nobody can justify is a black box
#: with extra steps.
PRIORITY_FACTORS: tuple[tuple[str, int, str], ...] = (
    ("high_risk_field", 40, "a wrong value here costs a student money or an application"),
    ("confidence_high", 25, "an unambiguous labelled extraction"),
    ("confidence_medium", 15, "wording that is itself about the field"),
    ("corroborated", 20, "another official source states the same value"),
    ("body_confirmed", 10, "the parser labelled this as page body, not chrome"),
    ("has_evidence", 5, "the source has stored evidence to review against"),
    ("scope_unresolved", -20, "who it applies to is unknown, so it cannot be acted on yet"),
    ("in_conflict", -15, "another source disagrees; a reviewer needs both at once"),
    ("thin_source", -25, "the page carried too little static text to judge from"),
    ("body_unconfirmed", -10, "the parser could not confirm this was page body"),
    ("confidence_low", -20, "the right shape of value with thin surrounding context"),
)

_POINTS: dict[str, tuple[int, str]] = {
    name: (points, why) for name, points, why in PRIORITY_FACTORS
}


def priority_for(
    *,
    field_kind: str,
    confidence_band: str,
    scope_unresolved: bool,
    in_conflict: bool,
    corroborated: bool,
    confirmation: BodyConfirmation,
    thin_source: bool,
    has_evidence: bool = True,
) -> Priority:
    """Deterministic review priority, with the reasons that produced it."""
    applied: list[str] = []
    score = 0

    def add(name: str) -> None:
        nonlocal score
        points, why = _POINTS[name]
        score += points
        applied.append(f"{'+' if points >= 0 else ''}{points} {name}: {why}")

    if field_kind in HIGH_RISK_FIELDS:
        add("high_risk_field")
    if confidence_band == "HIGH":
        add("confidence_high")
    elif confidence_band == "MEDIUM":
        add("confidence_medium")
    else:
        add("confidence_low")
    if corroborated:
        add("corroborated")
    if confirmation is BodyConfirmation.CONFIRMED:
        add("body_confirmed")
    elif confirmation is BodyConfirmation.UNCONFIRMED:
        add("body_unconfirmed")
    if has_evidence:
        add("has_evidence")
    if scope_unresolved:
        add("scope_unresolved")
    if in_conflict:
        add("in_conflict")
    if thin_source:
        add("thin_source")

    return Priority(score=score, reasons=tuple(applied))


class Queue(StrEnum):
    """Which review queue a candidate belongs in (section 28)."""

    PRIMARY = "PRIMARY"
    """HIGH and MEDIUM confidence, current rule version, not superseded."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    """LOW confidence. Stored, kept, and not in front of a reviewer by default."""

    ONLY_CANDIDATE = "ONLY_CANDIDATE"
    """LOW, but nothing better exists for this field on this page. Section 28's
    exception: a weak answer is the only answer, so it is worth a reviewer's time."""

    NOT_QUEUED = "NOT_QUEUED"
    """Superseded, or already decided."""


def queue_for(
    *,
    confidence_band: str,
    is_superseded: bool,
    decision_state: str,
    is_only_candidate: bool,
) -> Queue:
    """Which queue, deterministically.

    LOW candidates are never deleted (section 28) and never silently promoted into the
    primary queue. The one exception is when no higher-confidence candidate exists for
    the same field on the same page: then the weak answer is the only answer there is.
    """
    if is_superseded or decision_state != "UNREVIEWED":
        return Queue.NOT_QUEUED
    if confidence_band in ("HIGH", "MEDIUM"):
        return Queue.PRIMARY
    return Queue.ONLY_CANDIDATE if is_only_candidate else Queue.LOW_CONFIDENCE


# ===========================================================================
# Promotion readiness (sections 30-31)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ReadinessRow:
    """One candidate's distance from being promotable. Read-only."""

    candidate_id: uuid.UUID
    field_kind: str
    institution: str
    url: str
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return not self.blockers


#: Which publication eligibilities may support which responsibilities. `field_claim`'s
#: C27 trigger is the real gate; this mirrors its intent so a readiness report can say
#: *why* a candidate is not ready without attempting the insert.
ELIGIBLE_FOR_PUBLICATION: frozenset[str] = frozenset(
    {"OFFICIAL_VERIFIED", "AUTHORIZED_EXTERNAL", "AUTHORIZED_RANKING"}
)


def blockers_for(
    *,
    decision_state: str,
    is_superseded: bool,
    source_eligibility: str,
    scope_unresolved: bool,
    in_conflict: bool,
    responsibility: str,
    field_kind: str,
) -> tuple[str, ...]:
    """Everything standing between this candidate and a `field_claim`.

    Returns them all rather than the first, because a reviewer clearing one blocker
    wants to know whether that was the only one. An empty tuple means promotable -- and
    section 31 expects that to be empty for every candidate today, because no source has
    been verified.
    """
    blocking: list[str] = []
    if is_superseded:
        blocking.append("SUPERSEDED_RULE_VERSION")
    if decision_state != Decision.ACCEPTED.value:
        blocking.append(f"REVIEW_STATE_IS_{decision_state}")
    if source_eligibility not in ELIGIBLE_FOR_PUBLICATION:
        blocking.append(f"SOURCE_{source_eligibility}")
    if scope_unresolved:
        blocking.append("SCOPE_UNRESOLVED")
    if in_conflict:
        blocking.append("UNRESOLVED_CONFLICT")
    expected = FIELD_KIND_EXTRACTOR.get(field_kind)
    if expected is not None and expected not in ROUTING.get(responsibility, frozenset()):
        blocking.append("RESPONSIBILITY_DOES_NOT_AUTHORISE_FIELD")
    return tuple(blocking)


__all__ = [
    "BODY_CONTAINERS",
    "CURRENT_DOCUMENTS",
    "CURRENT_PARAMS",
    "CURRENT_RULES",
    "CURRENT_RULE_KEYS",
    "ELIGIBLE_FOR_PUBLICATION",
    "FIELD_KIND_EXTRACTOR",
    "HIGH_RISK_FIELDS",
    "PERIPHERAL_CONTAINERS",
    "PRIORITY_FACTORS",
    "REASON_CODES",
    "BodyConfirmation",
    "Decision",
    "Priority",
    "Queue",
    "ReadinessRow",
    "ReviewError",
    "ReviewOutcome",
    "blockers_for",
    "body_confirmation",
    "current_only",
    "priority_for",
    "queue_for",
    "record_decision",
]
