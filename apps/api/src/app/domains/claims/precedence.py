"""U1 scope precedence and U2 explicit-override semantics.

These two rules decide which of several overlapping applicant scopes governs a fact. They
are defined here, before any canonical publication, because the alternative is discovering
the rule at publication time and encoding whatever the first conflicting pair happened to
need.

U1 -- SCOPE PRECEDENCE: THE MORE SPECIFIC SCOPE WINS
====================================================
A fact stated for a narrower audience governs over one stated for a wider audience that
contains it. `applicant_scope.precedence` is the stored expression of this, and
`UNIVERSAL` sits at 0 with the note *"Zero criteria: matches every applicant. Lowest
precedence, so any more specific scope wins (U1)."*

Specificity is measured by **criteria count within the dimensions that apply**, not by
some ranking of the dimensions themselves:

* a scope with criteria on two dimensions is more specific than one with criteria on one;
* a scope with one criterion is more specific than `UNIVERSAL`, which has none.

Why not rank the dimensions? Because `applicant_country = CN` and
`qualification_type = A_LEVEL` are not comparable claims. One says where an applicant is
from; the other says what they studied. A Chinese applicant holding A-levels satisfies
both, and declaring one dimension inherently "more specific" would silently pick a winner
between two facts that are both about them. When two scopes are equally specific and
neither contains the other, U1 yields **no winner** -- see `Precedence.INCOMPARABLE`. That
is a reviewer's decision, not an ordering.

U1 IS CONTAINMENT, NOT COUNTING ALONE
=====================================
Counting criteria is only valid when one scope's criteria are a superset of the other's.
`{country=CN}` and `{country=US, qualification=IB}` do not overlap at all: they describe
disjoint audiences, so neither governs the other and both may coexist. Precedence applies
between scopes that *compete for the same applicant*, which means one must contain the
other. `compare()` checks containment first and only then compares specificity.

U2 -- EXPLICIT OVERRIDE: A STATED EXCEPTION BEATS A DERIVED RULE
================================================================
A scope a human explicitly resolved governs over one a mapper proposed, at equal
specificity. This exists because U1 alone cannot separate two scopes of the same shape,
and because the whole review workflow rests on human judgement outranking inference.

Two constraints keep U2 from becoming a loophole:

1. **It never beats U1.** A human-resolved `UNIVERSAL_EXPLICIT` does *not* override a
   machine-proposed `{country=CN}`. Specificity is decided first, and only ties fall
   through to provenance. Otherwise a reviewer confirming "this applies to everyone" would
   wipe out a narrower fact the page actually stated.
2. **`UNSCOPED` never wins anything.** It is not a scope; it is the absence of one. An
   unscoped candidate cannot govern, cannot be overridden, and cannot be published --
   which is the point of keeping it distinct from `UNIVERSAL`.

NOTHING HERE PUBLISHES
======================
This module compares scopes and returns a verdict. It writes nothing, reads no candidate,
and creates no `field_claim`. It exists so that the rule is testable in isolation before
anything depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: The five dimensions `scope_dimension` defines. Named here so a typo is a NameError
#: rather than a criterion that silently never matches.
#:
#: Step 5C.9 asks for two dimensions -- applicant jurisdiction and qualification system.
#: The existing vocabulary is finer-grained and is used as-is: `applicant_country` is the
#: jurisdiction dimension, and "qualification system" is spread across
#: `qualification_type`, `qualification_group` and `qualification_country` because those
#: answer genuinely different questions. Collapsing them to satisfy a two-way split would
#: throw away distinctions the schema already draws.
DIM_APPLICANT_COUNTRY = "applicant_country"
DIM_QUALIFICATION_COUNTRY = "qualification_country"
DIM_QUALIFICATION_TYPE = "qualification_type"
DIM_QUALIFICATION_GROUP = "qualification_group"
DIM_RESIDENCY_STATUS = "residency_status"

#: Which human scope state each dimension belongs to. The state is what a reviewer picks;
#: the dimensions are how it is recorded.
JURISDICTION_DIMENSIONS = frozenset({DIM_APPLICANT_COUNTRY, DIM_RESIDENCY_STATUS})
QUALIFICATION_DIMENSIONS = frozenset(
    {DIM_QUALIFICATION_COUNTRY, DIM_QUALIFICATION_TYPE, DIM_QUALIFICATION_GROUP}
)
ALL_DIMENSIONS = JURISDICTION_DIMENSIONS | QUALIFICATION_DIMENSIONS


class ScopeState(StrEnum):
    """The human scope states. Mirrors the migration's `SCOPE_STATES` exactly."""

    UNSCOPED = "UNSCOPED"
    """Nobody has said who this applies to. **Not** universal -- unknown.

    The distinction is the whole reason this enum exists rather than a nullable scope id.
    A requirement written for A-level applicants must not become everyone's requirement
    because the page failed to say so."""

    APPLICANT_JURISDICTION = "APPLICANT_JURISDICTION"
    """Scoped by where the applicant is from, or their residency status."""

    QUALIFICATION_SYSTEM = "QUALIFICATION_SYSTEM"
    """Scoped by what the applicant holds. Says nothing about nationality."""

    JURISDICTION_AND_QUALIFICATION = "JURISDICTION_AND_QUALIFICATION"
    """Both dimensions stated. The most specific shape the taxonomy can express."""

    UNIVERSAL_EXPLICIT = "UNIVERSAL_EXPLICIT"
    """A human read the page and confirmed it applies to every applicant.

    Named for the reason it is permitted: somebody said so. There is deliberately no
    `UNIVERSAL_INFERRED`."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """This field kind carries no applicant scope at all -- a programme's name has none."""

    @property
    def is_resolved(self) -> bool:
        """Whether a reviewer has actually settled the question."""
        return self is not ScopeState.UNSCOPED

    @property
    def can_publish(self) -> bool:
        """Whether a fact in this state may become publication-ready.

        `UNSCOPED` cannot: publishing a fact whose audience is unknown is the failure this
        entire workflow exists to prevent.
        """
        return self is not ScopeState.UNSCOPED


class Precedence(StrEnum):
    """The verdict of comparing two scopes."""

    LEFT_WINS = "LEFT_WINS"
    RIGHT_WINS = "RIGHT_WINS"
    EQUIVALENT = "EQUIVALENT"
    """Same criteria, same provenance. Genuinely interchangeable."""

    INCOMPARABLE = "INCOMPARABLE"
    """Neither contains the other, or they tie with nothing to break it.

    Not an error and not a draw to be resolved by a tiebreak: it means U1 and U2 do not
    decide, and a human must. Returning a winner here would be the silent choice the
    workflow forbids."""

    NOT_PUBLISHABLE = "NOT_PUBLISHABLE"
    """At least one side is `UNSCOPED`, so there is nothing to order."""


@dataclass(frozen=True, slots=True)
class Criterion:
    """One (dimension, operator, value) triple, as `applicant_scope_criterion` stores it."""

    dimension_code: str
    operator: str
    value: str | None = None
    value_ref: str | None = None

    def __post_init__(self) -> None:
        if self.dimension_code not in ALL_DIMENSIONS:
            raise ValueError(
                f"unknown scope dimension {self.dimension_code!r}; "
                f"scope_dimension defines {sorted(ALL_DIMENSIONS)}"
            )
        if (self.value is None) == (self.value_ref is None):
            # Mirrors ck_applicant_scope_criterion_exactly_one_of_value_or_ref, so an
            # invalid criterion fails here rather than at INSERT time.
            raise ValueError("a criterion carries exactly one of value or value_ref")

    @property
    def identity(self) -> tuple[str, str, str | None, str | None]:
        return (self.dimension_code, self.operator, self.value, self.value_ref)


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """A scope as a human settled it: a state, its criteria, and who decided."""

    state: ScopeState
    criteria: frozenset[Criterion] = field(default_factory=frozenset)
    #: True when a human resolved it, False when a mapper proposed it. U2's input.
    human_resolved: bool = False

    @property
    def dimensions(self) -> frozenset[str]:
        return frozenset(criterion.dimension_code for criterion in self.criteria)

    @property
    def specificity(self) -> int:
        """How many criteria narrow this scope. `UNIVERSAL` is 0 by construction."""
        return len(self.criteria)

    @property
    def identities(self) -> frozenset[tuple[str, str, str | None, str | None]]:
        return frozenset(criterion.identity for criterion in self.criteria)

    def contains(self, other: ResolvedScope) -> bool:
        """Whether every applicant matching `other` also matches this scope.

        Conjunctive criteria, so containment is a subset test the other way round: a scope
        with *fewer* criteria admits more applicants. `UNIVERSAL` (no criteria) contains
        everything.
        """
        return self.identities <= other.identities


def compare(left: ResolvedScope, right: ResolvedScope) -> Precedence:
    """Which of two overlapping scopes governs. U1 first, then U2, else no winner.

    The order is load-bearing. U2 is only consulted on a tie, so a human-confirmed wide
    scope can never displace a narrower one the page actually stated.
    """
    # U2's second constraint: UNSCOPED is the absence of a scope, not a wide one.
    if not left.state.can_publish or not right.state.can_publish:
        return Precedence.NOT_PUBLISHABLE

    if left.identities == right.identities:
        # Same audience. Only provenance can separate them -- U2.
        if left.human_resolved == right.human_resolved:
            return Precedence.EQUIVALENT
        return Precedence.LEFT_WINS if left.human_resolved else Precedence.RIGHT_WINS

    # U1 applies only between scopes competing for the same applicant, which requires
    # one to contain the other. Disjoint scopes describe different audiences and coexist.
    left_contains_right = left.contains(right)
    right_contains_left = right.contains(left)
    if not left_contains_right and not right_contains_left:
        return Precedence.INCOMPARABLE

    # U1: the narrower scope -- the contained one -- governs.
    if left_contains_right:
        return Precedence.RIGHT_WINS
    return Precedence.LEFT_WINS


def governing(scopes: list[ResolvedScope]) -> ResolvedScope | None:
    """The single scope that governs all of these, or None when they do not decide.

    Returns None rather than a best guess whenever any pair is `INCOMPARABLE` or
    `EQUIVALENT`, or when anything is `UNSCOPED`. A publication path must treat None as
    "a human has to look", never as "pick the first".
    """
    if not scopes:
        return None
    if any(not scope.state.can_publish for scope in scopes):
        return None

    winner = scopes[0]
    for challenger in scopes[1:]:
        verdict = compare(winner, challenger)
        if verdict is Precedence.RIGHT_WINS:
            winner = challenger
        elif verdict is Precedence.LEFT_WINS:
            continue
        else:
            # EQUIVALENT, INCOMPARABLE or NOT_PUBLISHABLE: no ordering exists.
            return None

    # A winner beating its neighbours pairwise is not yet a winner over the set: with
    # A > B and C incomparable to A, the loop above could still have settled on A.
    # Re-check the survivor against everything.
    for challenger in scopes:
        if challenger is winner:
            continue
        if compare(winner, challenger) is not Precedence.LEFT_WINS:
            return None
    return winner


def state_for(criteria: frozenset[Criterion] | set[Criterion]) -> ScopeState:
    """The human state a set of criteria amounts to.

    Used to check that what a reviewer selected matches the state they chose, so the
    record cannot say `APPLICANT_JURISDICTION` while carrying only a qualification
    criterion.
    """
    dimensions = {criterion.dimension_code for criterion in criteria}
    has_jurisdiction = bool(dimensions & JURISDICTION_DIMENSIONS)
    has_qualification = bool(dimensions & QUALIFICATION_DIMENSIONS)
    if has_jurisdiction and has_qualification:
        return ScopeState.JURISDICTION_AND_QUALIFICATION
    if has_jurisdiction:
        return ScopeState.APPLICANT_JURISDICTION
    if has_qualification:
        return ScopeState.QUALIFICATION_SYSTEM
    # No criteria. Which of UNSCOPED and UNIVERSAL_EXPLICIT this is cannot be derived --
    # that is exactly the distinction a human must draw, so the caller supplies it.
    return ScopeState.UNSCOPED


__all__ = [
    "ALL_DIMENSIONS",
    "DIM_APPLICANT_COUNTRY",
    "DIM_QUALIFICATION_COUNTRY",
    "DIM_QUALIFICATION_GROUP",
    "DIM_QUALIFICATION_TYPE",
    "DIM_RESIDENCY_STATUS",
    "JURISDICTION_DIMENSIONS",
    "QUALIFICATION_DIMENSIONS",
    "Criterion",
    "Precedence",
    "ResolvedScope",
    "ScopeState",
    "compare",
    "governing",
    "state_for",
]
