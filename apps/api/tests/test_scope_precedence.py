"""U1 scope precedence and U2 explicit-override semantics.

These are pure rules over in-memory scopes, so they need no database and run in the unit
suite. That is deliberate: publication precedence must be decidable and testable before
anything depends on it, and a rule that could only be exercised through a populated
database would not get tested until it was already load-bearing.

Every case below is one way a fact could reach the wrong applicant.
"""

from __future__ import annotations

import pytest

from app.domains.claims.precedence import (
    ALL_DIMENSIONS,
    DIM_APPLICANT_COUNTRY,
    DIM_QUALIFICATION_GROUP,
    DIM_QUALIFICATION_TYPE,
    DIM_RESIDENCY_STATUS,
    Criterion,
    Precedence,
    ResolvedScope,
    ScopeState,
    compare,
    governing,
    state_for,
)


def crit(dimension: str, value: str) -> Criterion:
    return Criterion(dimension_code=dimension, operator="EQUALS", value=value)


def scope(state: ScopeState, *criteria: Criterion, human: bool = False) -> ResolvedScope:
    return ResolvedScope(state=state, criteria=frozenset(criteria), human_resolved=human)


CN = scope(ScopeState.APPLICANT_JURISDICTION, crit(DIM_APPLICANT_COUNTRY, "CN"))
US = scope(ScopeState.APPLICANT_JURISDICTION, crit(DIM_APPLICANT_COUNTRY, "US"))
HK = scope(ScopeState.APPLICANT_JURISDICTION, crit(DIM_APPLICANT_COUNTRY, "HK"))
A_LEVEL = scope(ScopeState.QUALIFICATION_SYSTEM, crit(DIM_QUALIFICATION_TYPE, "A_LEVEL"))
IB = scope(ScopeState.QUALIFICATION_SYSTEM, crit(DIM_QUALIFICATION_TYPE, "IB"))
CN_IB = scope(
    ScopeState.JURISDICTION_AND_QUALIFICATION,
    crit(DIM_APPLICANT_COUNTRY, "CN"),
    crit(DIM_QUALIFICATION_TYPE, "IB"),
)
UNIVERSAL_MACHINE = scope(ScopeState.UNIVERSAL_EXPLICIT)
UNIVERSAL_HUMAN = scope(ScopeState.UNIVERSAL_EXPLICIT, human=True)
UNSCOPED = scope(ScopeState.UNSCOPED)


# ===========================================================================
# U1 -- the more specific scope wins
# ===========================================================================


def test_universal_loses_to_any_stated_scope() -> None:
    """The core of U1, and the reason `UNIVERSAL.precedence` is 0 in the seed data."""
    assert compare(UNIVERSAL_MACHINE, CN) is Precedence.RIGHT_WINS
    assert compare(CN, UNIVERSAL_MACHINE) is Precedence.LEFT_WINS
    assert compare(UNIVERSAL_MACHINE, A_LEVEL) is Precedence.RIGHT_WINS


def test_two_criteria_beat_one_when_one_contains_the_other() -> None:
    assert compare(CN, CN_IB) is Precedence.RIGHT_WINS
    assert compare(CN_IB, CN) is Precedence.LEFT_WINS


def test_universal_loses_to_a_two_dimension_scope() -> None:
    assert compare(UNIVERSAL_MACHINE, CN_IB) is Precedence.RIGHT_WINS


def test_disjoint_jurisdictions_do_not_compete() -> None:
    """CN and US describe different audiences. Neither governs; both may coexist."""
    assert compare(CN, US) is Precedence.INCOMPARABLE
    assert compare(US, HK) is Precedence.INCOMPARABLE


def test_a_jurisdiction_and_a_qualification_are_incomparable() -> None:
    """The reason dimensions are never ranked against each other.

    A Chinese applicant holding A-levels satisfies both. Declaring either dimension
    inherently more specific would silently pick a winner between two facts that are both
    about the same person.
    """
    assert compare(CN, A_LEVEL) is Precedence.INCOMPARABLE
    assert compare(A_LEVEL, CN) is Precedence.INCOMPARABLE


def test_a_scope_that_overlaps_partially_is_incomparable() -> None:
    """`{US}` and `{CN, IB}` share no applicant, so containment does not hold either way."""
    assert compare(US, CN_IB) is Precedence.INCOMPARABLE


def test_two_qualification_systems_are_incomparable() -> None:
    assert compare(A_LEVEL, IB) is Precedence.INCOMPARABLE


# ===========================================================================
# U2 -- explicit override, and its two limits
# ===========================================================================


def test_a_human_resolution_beats_a_machine_proposal_at_equal_specificity() -> None:
    assert compare(UNIVERSAL_HUMAN, UNIVERSAL_MACHINE) is Precedence.LEFT_WINS
    assert compare(UNIVERSAL_MACHINE, UNIVERSAL_HUMAN) is Precedence.RIGHT_WINS


def test_u2_never_beats_u1() -> None:
    """The limit that matters most.

    A reviewer confirming "this applies to everyone" must not wipe out a narrower fact the
    page actually stated. Specificity is settled first; provenance only breaks ties.
    """
    assert compare(UNIVERSAL_HUMAN, CN) is Precedence.RIGHT_WINS
    assert compare(CN, UNIVERSAL_HUMAN) is Precedence.LEFT_WINS


def test_two_scopes_with_identical_criteria_and_provenance_are_equivalent() -> None:
    other_cn = scope(ScopeState.APPLICANT_JURISDICTION, crit(DIM_APPLICANT_COUNTRY, "CN"))
    assert compare(CN, other_cn) is Precedence.EQUIVALENT


def test_a_human_resolved_narrow_scope_beats_a_machine_one_of_the_same_shape() -> None:
    human_cn = scope(
        ScopeState.APPLICANT_JURISDICTION, crit(DIM_APPLICANT_COUNTRY, "CN"), human=True
    )
    assert compare(human_cn, CN) is Precedence.LEFT_WINS


# ===========================================================================
# UNSCOPED is the absence of a scope, not a wide one
# ===========================================================================


@pytest.mark.parametrize("other", [CN, A_LEVEL, CN_IB, UNIVERSAL_HUMAN, UNIVERSAL_MACHINE])
def test_unscoped_never_orders_against_anything(other: ResolvedScope) -> None:
    assert compare(UNSCOPED, other) is Precedence.NOT_PUBLISHABLE
    assert compare(other, UNSCOPED) is Precedence.NOT_PUBLISHABLE


def test_unscoped_cannot_publish_and_is_not_resolved() -> None:
    assert ScopeState.UNSCOPED.can_publish is False
    assert ScopeState.UNSCOPED.is_resolved is False


@pytest.mark.parametrize(
    "state",
    [
        ScopeState.APPLICANT_JURISDICTION,
        ScopeState.QUALIFICATION_SYSTEM,
        ScopeState.JURISDICTION_AND_QUALIFICATION,
        ScopeState.UNIVERSAL_EXPLICIT,
        ScopeState.NOT_APPLICABLE,
    ],
)
def test_every_other_state_is_resolved_and_publishable(state: ScopeState) -> None:
    assert state.is_resolved is True
    assert state.can_publish is True


def test_there_is_no_inferred_universal_state() -> None:
    """Section 12 as a structural fact: silence cannot be spelled as universality."""
    names = {state.value for state in ScopeState}
    assert "UNIVERSAL_EXPLICIT" in names
    assert not any("INFERRED" in name for name in names)
    assert "UNIVERSAL" not in names, "a bare UNIVERSAL would be inferrable from silence"


# ===========================================================================
# governing() -- a winner over a set, or none at all
# ===========================================================================


def test_governing_picks_the_narrowest_of_a_nested_set() -> None:
    winner = governing([UNIVERSAL_MACHINE, CN, CN_IB])
    assert winner is not None
    assert winner.identities == CN_IB.identities


@pytest.mark.parametrize(
    "scopes",
    [
        [CN, US],
        [CN, A_LEVEL],
        [CN, UNSCOPED],
        [UNIVERSAL_MACHINE, CN, US],
        [A_LEVEL, IB],
    ],
)
def test_governing_returns_none_rather_than_guessing(scopes: list[ResolvedScope]) -> None:
    """None means "a human has to look". It must never mean "pick the first"."""
    assert governing(scopes) is None


def test_governing_of_an_empty_set_is_none() -> None:
    assert governing([]) is None


def test_governing_of_one_scope_is_that_scope() -> None:
    assert governing([CN]) is CN


def test_a_pairwise_winner_is_rechecked_against_the_whole_set() -> None:
    """Regression guard for the subtle case.

    With `[CN_IB, CN, US]`, CN_IB beats CN pairwise; if the loop stopped there it would
    report CN_IB as governing while US sits incomparable to both.
    """
    assert governing([CN_IB, CN, US]) is None


# ===========================================================================
# state_for() -- the state a set of criteria amounts to
# ===========================================================================


def test_state_for_maps_the_two_dimensions_the_step_asks_for() -> None:
    assert state_for({crit(DIM_APPLICANT_COUNTRY, "US")}) is ScopeState.APPLICANT_JURISDICTION
    assert state_for({crit(DIM_QUALIFICATION_TYPE, "IB")}) is ScopeState.QUALIFICATION_SYSTEM
    assert (
        state_for({crit(DIM_APPLICANT_COUNTRY, "HK"), crit(DIM_QUALIFICATION_TYPE, "A_LEVEL")})
        is ScopeState.JURISDICTION_AND_QUALIFICATION
    )


def test_residency_status_counts_as_jurisdiction() -> None:
    """DOMESTIC/INTERNATIONAL is a fee status, which is a statement about the applicant."""
    assert (
        state_for({crit(DIM_RESIDENCY_STATUS, "INTERNATIONAL")})
        is ScopeState.APPLICANT_JURISDICTION
    )


def test_qualification_group_counts_as_qualification() -> None:
    assert (
        state_for({Criterion(DIM_QUALIFICATION_GROUP, "IN_GROUP", value_ref="x")})
        is ScopeState.QUALIFICATION_SYSTEM
    )


def test_no_criteria_derives_unscoped_not_universal() -> None:
    """The one thing that cannot be derived, so the caller must say which it is."""
    assert state_for(frozenset()) is ScopeState.UNSCOPED


# ===========================================================================
# the dimension vocabulary
# ===========================================================================


def test_an_unknown_dimension_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown scope dimension"):
        Criterion(dimension_code="china_specific_thing", operator="EQUALS", value="x")


def test_a_criterion_carries_exactly_one_of_value_or_ref() -> None:
    """Mirrors the database CHECK, so an invalid criterion fails before the INSERT."""
    with pytest.raises(ValueError, match="exactly one"):
        Criterion(DIM_APPLICANT_COUNTRY, "EQUALS", value="CN", value_ref="also")
    with pytest.raises(ValueError, match="exactly one"):
        Criterion(DIM_APPLICANT_COUNTRY, "EQUALS")


def test_the_dimensions_are_the_five_the_schema_defines() -> None:
    """A guard against a sixth appearing in code without a `scope_dimension` row."""
    assert {
        "applicant_country",
        "residency_status",
        "qualification_country",
        "qualification_type",
        "qualification_group",
    } == ALL_DIMENSIONS


def test_no_dimension_is_china_specific() -> None:
    """Step 5C.9 forbids it, and the vocabulary is generic by construction."""
    assert not any("china" in dim.lower() or "cn_" in dim.lower() for dim in ALL_DIMENSIONS)
