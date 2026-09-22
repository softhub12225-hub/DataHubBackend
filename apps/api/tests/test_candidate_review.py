"""Candidate review, grouping and quality on fixtures (Step 5C.3).

WHAT THESE ASSERT
=================
Mostly refusals, again. The dangerous moves in this step are all of one shape: treating
two things as the same because nothing distinguished them. Two candidates whose scope
nobody stated are not thereby about the same applicants; a calendar date is not an
application deadline; a catalogue link whose container the parser never recorded is not
confirmed body content; a `HIGH` band is not a decision.

Every one of those has a test below whose purpose is that the merge does not happen.

The grouping tests build `CandidateRow` values directly rather than going through the
extractors, because what is under test is the grouping rule and not the rules that feed
it. The quality tests go through the real classifier on real shapes.
"""

from __future__ import annotations

import uuid

import pytest

from app.domains.claims.grouping import (
    NEVER_GROUPED,
    Agreement,
    CandidateRow,
    context_conflicts,
    context_key_for,
    corroborated_ids,
    group_candidates,
    has_sentinel,
    language_groups,
    program_groups,
    value_key_for,
)
from app.domains.claims.loader import PROVEN_RELATIONSHIPS, evidence_relationship
from app.domains.claims.model import FieldKind
from app.domains.claims.quality import (
    AdmissionQuality,
    FinancialContext,
    classify_admission,
    classify_financial_context,
    looks_like_chrome,
)
from app.domains.claims.review import (
    CURRENT_RULES,
    BodyConfirmation,
    Decision,
    Queue,
    blockers_for,
    body_confirmation,
    priority_for,
    queue_for,
)
from app.domains.claims.runner import EXTRACTORS
from app.domains.claims.scope import ScopeResolution, propose_scope


def row(
    *,
    institution: str = "test university a",
    field_kind: str = FieldKind.LANGUAGE_OVERALL_SCORE.value,
    value: dict[str, object] | None = None,
    source: uuid.UUID | None = None,
    extraction: uuid.UUID | None = None,
    locator: dict[str, object] | None = None,
    confidence: str = "MEDIUM",
    url: str = "https://a.example.ac.uk/x",
    responsibility: str = "LANGUAGE_REQUIREMENTS",
    evidence: str = "IELTS 7.0 overall is required.",
    raw: str = "7.0",
) -> CandidateRow:
    return CandidateRow(
        candidate_id=uuid.uuid4(),
        institution=institution,
        field_kind=field_kind,
        value=value if value is not None else {"test": "IELTS", "score": 7.0},
        unresolved_reason=None,
        confidence_band=confidence,
        source_id=source or uuid.uuid4(),
        url=url,
        extraction_id=extraction or uuid.uuid4(),
        locator=locator if locator is not None else {"kind": "block", "block_index": 1},
        responsibility=responsibility,
        extractor_name="language-rule-extractor",
        extractor_version="5",
        evidence_text=evidence,
        value_raw_text=raw,
    )


# ===========================================================================
# 22. What "current" means
# ===========================================================================


def test_current_versions_come_from_the_registry_not_a_literal() -> None:
    """Section 22. A frozen copy of something that changes was wrong within four
    corrections, and it reported every current candidate as superseded."""
    assert (
        tuple(sorted((name, version) for _, name, version in EXTRACTORS.values())) == CURRENT_RULES
    )
    assert len(CURRENT_RULES) == len(EXTRACTORS)
    assert len({name for name, _ in CURRENT_RULES}) == len(CURRENT_RULES)


# ===========================================================================
# 4. Confidence is not review state
# ===========================================================================


@pytest.mark.parametrize("band", ["HIGH", "MEDIUM", "LOW"])
def test_no_band_implies_a_decision(band: str) -> None:
    """Section 4. HIGH describes how unambiguous the extraction was, not whether the
    fact is true. A labelled table cell can hold a number the university has changed."""
    queue = queue_for(
        confidence_band=band,
        is_superseded=False,
        decision_state="UNREVIEWED",
        is_only_candidate=False,
    )
    assert queue is not Queue.NOT_QUEUED
    blockers = blockers_for(
        decision_state="UNREVIEWED",
        is_superseded=False,
        source_eligibility="OFFICIAL_VERIFIED",
        scope_unresolved=False,
        in_conflict=False,
        responsibility="LANGUAGE_REQUIREMENTS",
        field_kind=FieldKind.LANGUAGE_OVERALL_SCORE.value,
    )
    assert "REVIEW_STATE_IS_UNREVIEWED" in blockers, "a band alone satisfied review"


def test_a_high_band_is_not_promotable_and_a_low_one_is_not_rejected() -> None:
    high = blockers_for(
        decision_state="UNREVIEWED",
        is_superseded=False,
        source_eligibility="OFFICIAL_VERIFIED",
        scope_unresolved=False,
        in_conflict=False,
        responsibility="TUITION_FEES",
        field_kind=FieldKind.TUITION.value,
    )
    low_but_accepted = blockers_for(
        decision_state=Decision.ACCEPTED.value,
        is_superseded=False,
        source_eligibility="OFFICIAL_VERIFIED",
        scope_unresolved=False,
        in_conflict=False,
        responsibility="TUITION_FEES",
        field_kind=FieldKind.TUITION.value,
    )
    assert high, "an unreviewed HIGH candidate was promotable"
    assert not low_but_accepted, "an accepted candidate was blocked by its band"


# ===========================================================================
# 5-6. Agreement grouping
# ===========================================================================


def test_two_sources_stating_one_value_agree() -> None:
    """Section 6's first worked example."""
    shared = {"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2027}}
    groups = group_candidates(
        [
            row(value=dict(shared), source=uuid.uuid4()),
            row(value=dict(shared), source=uuid.uuid4()),
        ]
    )
    assert len(groups) == 1
    assert groups[0].verdict is Agreement.AGREES
    assert len(groups[0].sources) == 2
    assert corroborated_ids(groups) == {member.candidate_id for member in groups[0].members}


def test_one_source_repeating_itself_is_not_corroboration() -> None:
    source = uuid.uuid4()
    shared = {"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2027}}
    groups = group_candidates([row(value=dict(shared), source=source) for _ in range(2)])
    assert groups[0].verdict is Agreement.POSSIBLE_DUPLICATE
    assert corroborated_ids(groups) == frozenset()


def test_agreement_under_an_unknown_context_says_so() -> None:
    """Two pages agreeing about a score neither of them scopes is still corroboration a
    reviewer can use. What they must not be told is that the scope was established.

    The gate stays on CONFLICTS, where it is load-bearing: a contradiction asserts that
    two sources answered the same question differently, and a key of unknowns has not
    established that the question was the same.
    """
    shared = {"test": "IELTS", "score": 7.0}  # no scope, no period
    groups = group_candidates([row(value=dict(shared), source=uuid.uuid4()) for _ in range(2)])
    assert has_sentinel(groups[0].context_key)
    assert groups[0].verdict is Agreement.AGREES
    assert groups[0].context_is_thin, "the unknowns were not reported"


def test_the_same_value_in_different_years_stays_separate() -> None:
    """Section 6's third worked example."""
    groups = group_candidates(
        [
            row(value={"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2026}}),
            row(value={"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2027}}),
        ]
    )
    assert len(groups) == 2
    assert {group.verdict for group in groups} == {Agreement.SINGLE}


def test_an_unknown_programme_never_merges_with_a_named_one() -> None:
    """Section 6's second worked example. The sentinel is PROGRAM_UNKNOWN, not
    INSTITUTION_WIDE: a candidate that names no programme has not asserted that it
    applies to the whole institution."""
    groups = group_candidates(
        [
            row(
                field_kind=FieldKind.DEGREE_LEVEL.value,
                value={"degree_level": "MASTER", "_context": {"program_name": "MSc Finance"}},
            ),
            row(field_kind=FieldKind.DEGREE_LEVEL.value, value={"degree_level": "MASTER"}),
        ]
    )
    assert len(groups) == 2
    keys = {key for group in groups for key in group.context_key if key.startswith("program=")}
    assert keys == {"program=msc finance", "program=PROGRAM_UNKNOWN"}


def test_grouping_never_crosses_institutions() -> None:
    """Two universities publishing one fee is a coincidence, not corroboration."""
    shared = {"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2027}}
    groups = group_candidates(
        [
            row(institution="test university a", value=dict(shared)),
            row(institution="test university b", value=dict(shared)),
        ]
    )
    assert len(groups) == 2
    assert all(group.verdict is Agreement.SINGLE for group in groups)


def test_a_value_alone_is_never_the_group_key() -> None:
    """Section 5's prohibition, made structural: the key names the institution, the
    field, the scope, the programme and the period before it ever reaches a value."""
    key = context_key_for(row(value={"test": "IELTS", "score": 7.0}))
    assert key is not None
    assert key[0].startswith("institution=")
    assert any(part.startswith("scope=") for part in key)
    assert any(part.startswith("program=") for part in key)
    assert value_key_for(row()) != key


def test_a_test_is_part_of_what_a_language_claim_is_about() -> None:
    """Section 16. IELTS 7.0 and TOEFL 7.0 are not the same claim disagreeing."""
    groups = group_candidates(
        [
            row(value={"test": "IELTS", "score": 7.0, "_context": {"entry_year": 2027}}),
            row(value={"test": "TOEFL", "score": 7.0, "_context": {"entry_year": 2027}}),
        ]
    )
    assert len(groups) == 2


# ===========================================================================
# 7, 21. Conflicts
# ===========================================================================


def test_one_context_with_two_values_is_a_conflict_and_no_winner_is_chosen() -> None:
    """A conflict needs the context fully stated, so the fixture states it: the same
    applicant scope, the same entry year, the same test."""
    scope = [{"scope_label": "China", "applicant_country_code": "CN", "scope_raw": "China"}]

    def scored(score: float, confidence: str) -> CandidateRow:
        return row(
            value={
                "test": "IELTS",
                "score": score,
                "applicant_scopes": scope,
                "_context": {"entry_year": 2027, "program_name": "MSc Finance"},
            },
            source=uuid.uuid4(),
            confidence=confidence,
        )

    groups = group_candidates([scored(7.0, "HIGH"), scored(6.5, "LOW")])
    assert len(groups) == 1
    group = groups[0]
    assert group.verdict is Agreement.CONFLICTS
    assert len(group.values) == 2
    # Both values survive, and the HIGH one is not preferred: confidence describes
    # extraction quality, not truth (section 4).
    scores = {
        candidate.value["score"]
        for candidates in group.values.values()
        for candidate in candidates
        if candidate.value is not None
    }
    assert scores == {7.0, 6.5}


def test_a_thin_context_cannot_establish_a_conflict() -> None:
    """Caltech's admit-reply date and its application deadline are not rival answers to
    one question, and a key made of unknowns has not established that the question is
    the same. Reported as an unconfirmed disagreement, never as nothing."""
    groups = group_candidates(
        [
            row(
                field_kind=FieldKind.APPLICATION_DEADLINE.value,
                value={"deadline_kind": "FIXED_DATE", "month": 11, "day": 1},
                responsibility="APPLICATION_DEADLINES",
            ),
            row(
                field_kind=FieldKind.APPLICATION_DEADLINE.value,
                value={"deadline_kind": "FIXED_DATE", "month": 5, "day": 1},
                responsibility="APPLICATION_DEADLINES",
            ),
        ]
    )
    assert len(groups) == 1
    assert groups[0].verdict is Agreement.INSUFFICIENT_CONTEXT
    assert groups[0].unconfirmed_disagreement, "the disagreement was dropped, not reported"


def test_one_value_with_contradictory_context_is_its_own_conflict() -> None:
    """Harvard's two pages give January 1 and disagree about which round it is. Keyed by
    context those are two unrelated groups and the contradiction disappears."""
    value = {"deadline_kind": "FIXED_DATE", "year": 2027, "month": 1, "day": 1}
    rows = [
        row(
            field_kind=FieldKind.APPLICATION_DEADLINE.value,
            responsibility="APPLICATION_DEADLINES",
            value={
                **value,
                "_context": {"round_label": "Early Action", "round_label_source": "text"},
            },
        ),
        row(
            field_kind=FieldKind.APPLICATION_DEADLINE.value,
            responsibility="APPLICATION_DEADLINES",
            value={
                **value,
                "_context": {"round_label": "Regular Decision", "round_label_source": "text"},
            },
        ),
    ]
    assert all(group.verdict is Agreement.SINGLE for group in group_candidates(rows))
    conflicts = context_conflicts(rows)
    assert len(conflicts) == 1
    assert len(conflicts[0].contexts) == 2


def test_a_round_the_page_only_implied_does_not_separate_a_group() -> None:
    """Caltech's 'Early Action' heading stamped that label on a date whose own wording
    reads 'January 4, 2027 for Regular Decision'."""
    implied = row(
        field_kind=FieldKind.APPLICATION_DEADLINE.value,
        responsibility="APPLICATION_DEADLINES",
        value={
            "deadline_kind": "FIXED_DATE",
            "year": 2027,
            "month": 1,
            "day": 4,
            "_context": {"round_label": "Early Action", "round_label_source": "heading"},
        },
    )
    key = context_key_for(implied)
    assert key is not None
    assert "round=ROUND_NOT_STATED" in key


# ===========================================================================
# 8-9. Program grouping
# ===========================================================================


def test_program_candidates_group_on_one_locator() -> None:
    extraction = uuid.uuid4()
    locator = {"kind": "block", "block_index": 33, "heading_path": ["Courses"]}
    rows = [
        row(
            field_kind=kind,
            value={"program_name": "MSc Finance"},
            extraction=extraction,
            locator=dict(locator),
            responsibility="PROGRAM_CATALOG",
        )
        for kind in (
            FieldKind.PROGRAM_NAME.value,
            FieldKind.DEGREE_LEVEL.value,
            FieldKind.DISCIPLINE_HINT.value,
        )
    ]
    groups = program_groups(rows)
    assert len(groups) == 1
    assert len(groups[0].members) == 3


def test_two_programmes_on_one_page_stay_two_groups() -> None:
    """Section 8. Two parts of a page that are about related things are not one
    statement (section 28)."""
    extraction = uuid.uuid4()
    rows = [
        row(
            field_kind=FieldKind.PROGRAM_NAME.value,
            value={"program_name": name},
            extraction=extraction,
            locator={"kind": "block", "block_index": index},
            responsibility="PROGRAM_CATALOG",
        )
        for index, name in ((33, "MSc Finance"), (35, "MSc Computing"))
    ]
    assert len(program_groups(rows)) == 2


def test_a_link_derived_programme_is_never_confirmed_body_content() -> None:
    """Section 9. `Link` records no container, so a catalogue anchor and a site-wide
    course picker are indistinguishable on the row. It is unconfirmed by construction,
    which is a gap in the document schema rather than a property of the page."""
    assert body_confirmation({"kind": "link", "link_index": 7}, None) is (
        BodyConfirmation.NOT_RECORDED
    )
    assert body_confirmation({"kind": "link", "link_index": 7}, "main") is (
        BodyConfirmation.NOT_RECORDED
    )


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        ("main", BodyConfirmation.CONFIRMED),
        ("article", BodyConfirmation.CONFIRMED),
        ("pdf", BodyConfirmation.CONFIRMED),
        ("aside", BodyConfirmation.PERIPHERAL),
        ("form", BodyConfirmation.PERIPHERAL),
        (None, BodyConfirmation.UNCONFIRMED),
        ("nav", BodyConfirmation.UNCONFIRMED),
    ],
)
def test_body_confirmation_reads_the_container_the_parser_recorded(
    container: str | None, expected: BodyConfirmation
) -> None:
    assert body_confirmation({"kind": "block", "block_index": 1}, container) is expected


# ===========================================================================
# 16. Language grouping
# ===========================================================================


def test_one_block_naming_two_tests_makes_two_groups() -> None:
    """Section 16. UNSW's block 21 list item 0 is ~13,200 characters naming five tests,
    so the block alone would merge five separate statements."""
    extraction = uuid.uuid4()
    locator = {"kind": "list_item", "block_index": 21, "list_item_index": 0}
    rows = [
        row(
            field_kind=FieldKind.LANGUAGE_TEST.value,
            value={"test": test},
            extraction=extraction,
            locator=dict(locator),
        )
        for test in ("IELTS", "TOEFL")
    ]
    groups = language_groups(rows)
    assert len(groups) == 2
    assert {(group.members[0].value or {}).get("test") for group in groups} == {
        "IELTS",
        "TOEFL",
    }


def test_a_test_keeps_its_overall_and_component_scores_together() -> None:
    """Section 16's requested grouping: one logical language requirement candidate group
    that retains its atomic candidates."""
    extraction = uuid.uuid4()
    locator = {"kind": "list_item", "block_index": 21, "list_item_index": 0}
    rows = [
        row(
            field_kind=kind,
            value={"test": "IELTS", "score": score},
            extraction=extraction,
            locator=dict(locator),
        )
        for kind, score in (
            (FieldKind.LANGUAGE_TEST.value, None),
            (FieldKind.LANGUAGE_OVERALL_SCORE.value, 7.0),
            (FieldKind.LANGUAGE_COMPONENT_SCORE.value, 6.5),
        )
    ]
    groups = language_groups(rows)
    assert len(groups) == 1
    assert len(groups[0].members) == 3
    assert len(groups[0].kinds) == 3


def test_two_list_items_in_one_block_are_two_evidence_units() -> None:
    extraction = uuid.uuid4()
    rows = [
        row(
            field_kind=FieldKind.LANGUAGE_TEST.value,
            value={"test": "IELTS"},
            extraction=extraction,
            locator={"kind": "list_item", "block_index": 21, "list_item_index": item},
        )
        for item in (0, 3)
    ]
    assert len(language_groups(rows)) == 2


# ===========================================================================
# 10. Admission quality
# ===========================================================================


def test_the_admission_quality_classes_partition() -> None:
    """Every candidate lands in exactly one class, because the cascade returns on the
    first match and its last branch has no condition."""
    shapes = [
        ({"kind": "block", "heading_path": ["Entry requirements"]}, "Applicants must hold one."),
        ({"kind": "list_item", "heading_path": ["Entry requirements"]}, "Applicants require AAB."),
        ({"kind": "block", "heading_path": ["Entry requirements", "A-levels"]}, "Physics 101"),
        ({"kind": "block", "heading_path": ["Entry requirements"]}, "We welcome applications."),
        ({"kind": "block", "heading_path": []}, "Explore Support Resources"),
        ({"kind": "block", "heading_path": ["Apply"]}, 'var x = {"a": 1}; document.ready'),
    ]
    seen = set()
    for locator, evidence in shapes:
        quality, reason = classify_admission(locator=locator, evidence_text=evidence, value=None)
        assert isinstance(quality, AdmissionQuality)
        assert reason.strip()
        seen.add(quality)
    assert len(seen) >= 4, seen


def test_a_link_label_rendered_as_a_paragraph_is_chrome() -> None:
    """146 of 985 admission candidates are character-identical to a link label in their
    own document, inside `main` or `article` where the container test cannot see them."""
    is_chrome, why = looks_like_chrome(
        "Explore Support Resources", link_texts=frozenset({"Explore Support Resources"})
    )
    assert is_chrome
    assert "link label" in why


def test_an_inline_script_payload_is_chrome() -> None:
    is_chrome, why = looks_like_chrome('var CCWCAGExtLinks = {"internalDomains":["x"]}')
    assert is_chrome
    assert "script" in why


def test_prose_that_merely_mentions_a_word_is_not_chrome() -> None:
    """Adding bare `function`, `const` or `let` to the script pattern pulled in real
    requirement sentences, so it names only unambiguous tokens."""
    for text in (
        "If you are taking a qualification not listed, contact the team who will let you know.",
        "Applicants must hold a bachelor's degree with a 2:1 or equivalent.",
        "We consider applications holistically and let the context inform our decision.",
    ):
        is_chrome, _ = looks_like_chrome(text)
        assert not is_chrome, text


def test_a_requirement_sentence_is_not_classified_from_its_heading() -> None:
    quality, _ = classify_admission(
        locator={"kind": "block", "heading_path": ["Entry requirements", "A-levels"]},
        evidence_text="Applicants must hold A-levels at grade AAB.",
        value=None,
    )
    assert quality is AdmissionQuality.EXPLICIT_REQUIREMENT_SENTENCE


def test_a_table_row_class_exists_and_is_empty_by_construction() -> None:
    """`admission.extract` builds units only from list items and paragraph/quote blocks,
    so no requirement candidate has ever come from a table. "We looked and there are
    none" is a different statement from "we never looked"."""
    quality, _ = classify_admission(
        locator={"kind": "table_cell", "heading_path": ["Entry requirements"]},
        evidence_text="A-level: AAB including Mathematics at grade A.",
        value=None,
    )
    assert quality is AdmissionQuality.REQUIREMENT_TABLE_ROW


# ===========================================================================
# 12-13. Scope
# ===========================================================================


def test_silence_is_never_universal() -> None:
    """Section 12. The single most important refusal in this module."""
    proposal = propose_scope(
        row(
            field_kind=FieldKind.ADMISSION_REQUIREMENT.value,
            responsibility="ENTRY_REQUIREMENTS",
            value={"requirement_text": "Applicants require a good first degree."},
            evidence="Applicants require a good first degree.",
        )
    )
    assert proposal.resolution is ScopeResolution.UNRESOLVED
    assert proposal.applicant_scope_id is None
    assert proposal.country_hints == frozenset()


@pytest.mark.parametrize(
    ("text", "countries", "qualifications"),
    [
        ("Applicants from China need a Gaokao score.", {"CN"}, {"GAOKAO"}),
        ("A Chinese bachelor's degree is required.", {"CN"}, set()),
        ("Applicants offering the International Baccalaureate need 38 points.", set(), {"IB"}),
        ("Applicants offering A levels need AAB.", set(), {"A_LEVEL"}),
        ("Students from Hong Kong should offer the HKDSE.", {"HK"}, {"HKDSE"}),
    ],
)
def test_explicit_forms_are_proposed(
    text: str, countries: set[str], qualifications: set[str]
) -> None:
    """Section 12's own examples. A country hint and a qualification hint are different
    claims: an IB candidate may hold any passport."""
    proposal = propose_scope(
        row(
            field_kind=FieldKind.ADMISSION_REQUIREMENT.value,
            responsibility="ENTRY_REQUIREMENTS",
            value={"requirement_text": text},
            evidence=text,
        )
    )
    assert proposal.resolution is ScopeResolution.PARTIALLY_RESOLVED
    assert proposal.country_hints == frozenset(countries)
    assert proposal.qualification_hints == frozenset(qualifications)
    assert proposal.applicant_scope_id is None, "a hint was resolved to an id"


def test_a_scope_from_a_heading_records_that_it_came_from_a_heading() -> None:
    """ "The sentence said so" and "the section it sits under said so" are different
    strengths of evidence, and a reviewer needs to know which they are checking."""
    proposal = propose_scope(
        row(
            field_kind=FieldKind.ADMISSION_REQUIREMENT.value,
            responsibility="ENTRY_REQUIREMENTS",
            value={"requirement_text": "A minimum of 38 points is required."},
            evidence="A minimum of 38 points is required.",
            locator={
                "kind": "block",
                "block_index": 4,
                "heading_path": ["Entry requirements", "International Baccalaureate"],
            },
        )
    )
    assert proposal.resolution is ScopeResolution.PARTIALLY_RESOLVED
    assert proposal.evidence_source == "heading"
    assert proposal.qualification_hints == frozenset({"IB"})


def test_a_programme_name_has_no_applicant_scope() -> None:
    """Reporting one as unresolved would inflate the unresolved count with rows that
    were never going to have a scope."""
    proposal = propose_scope(
        row(field_kind=FieldKind.PROGRAM_NAME.value, value={"program_name": "MSc Finance"})
    )
    assert proposal.resolution is ScopeResolution.NOT_APPLICABLE


# ===========================================================================
# 14. Financial context
# ===========================================================================


@pytest.mark.parametrize(
    ("heading", "evidence", "raw", "expected"),
    [
        (
            ["Student Budgets (Cost of Attendance)"],
            "Housing and Utilities: Expenses are for nine months (~$1,609/month).",
            "$1,609",
            FinancialContext.HOUSING,
        ),
        (
            ["Cost of Attendance"],
            "Tuition$56,550Fees$5,126Housing$12,922Food$8,268",
            "$56,550",
            FinancialContext.TUITION_FEE,
        ),
        (
            ["Cost of Attendance"],
            "Tuition$56,550Fees$5,126Housing$12,922Food$8,268",
            "$12,922",
            FinancialContext.HOUSING,
        ),
        (
            ["Fees"],
            "The estimated total cost of attendance is $90,574 for the year.",
            "$90,574",
            FinancialContext.ESTIMATED_TOTAL_COST,
        ),
        (
            ["Fees"],
            "All students must pay a compulsory Student Services and Amenities Fee of A$351.",
            "A$351",
            FinancialContext.OTHER_MANDATORY_FEE,
        ),
        (
            ["Overview"],
            "The figure is 38,000 for the year.",
            "38,000",
            FinancialContext.UNKNOWN_FINANCIAL_AMOUNT,
        ),
    ],
)
def test_an_amount_is_classified_by_the_label_nearest_to_it(
    heading: list[str], evidence: str, raw: str, expected: FinancialContext
) -> None:
    """Section 14. A page heading describes the page; the sentence describes the amount.

    Reading the heading first put Berkeley's housing line under "Cost of Attendance",
    and taking the first pattern match rather than the nearest read
    "Tuition$56,550Fees...Food$8,268" as a meal plan.
    """
    kind, why = classify_financial_context(
        locator={"kind": "block", "heading_path": heading},
        evidence_text=evidence,
        value_raw_text=raw,
        value=None,
    )
    assert kind is expected, why


def test_a_labelled_column_outranks_the_wording() -> None:
    """The publisher labelled the cell; that is more specific than the sentence."""
    kind, why = classify_financial_context(
        locator={"kind": "table_cell", "heading_path": ["Cost of Attendance"]},
        evidence_text="8 units | $12,040",
        value_raw_text="$12,040",
        value={"_context": {"column_label": "Quarterly Tuition"}},
    )
    assert kind is FinancialContext.TUITION_FEE
    assert "column label" in why


# ===========================================================================
# 19. Calendar separation
# ===========================================================================


def test_a_calendar_event_is_never_grouped_for_agreement() -> None:
    """Section 19. What distinguishes "registration opens" from "midterm grades due" on
    one date is the event's own label, and the stored evidence is a 160-character window
    that spans two or three adjacent entries. There is nothing to key on."""
    assert FieldKind.ACADEMIC_CALENDAR_EVENT.value in NEVER_GROUPED
    assert FieldKind.ADMISSION_REQUIREMENT.value in NEVER_GROUPED
    groups = group_candidates(
        [
            row(
                field_kind=FieldKind.ACADEMIC_CALENDAR_EVENT.value,
                responsibility="ACADEMIC_CALENDAR",
                value={"year": 2027, "month": 1, "day": 5, "event_text": "Instruction begins"},
            )
        ]
    )
    assert groups[0].verdict is Agreement.INSUFFICIENT_CONTEXT


def test_only_the_calendar_rule_produces_calendar_events() -> None:
    """Section 19. Nothing converts a calendar event into an application deadline, and
    the two field kinds are produced by two different extractors."""
    from app.domains.claims.review import FIELD_KIND_EXTRACTOR

    assert FIELD_KIND_EXTRACTOR[FieldKind.ACADEMIC_CALENDAR_EVENT.value] == "calendar"
    assert FIELD_KIND_EXTRACTOR[FieldKind.APPLICATION_DEADLINE.value] == "deadline"


# ===========================================================================
# 27-28. Priority and queues
# ===========================================================================


def test_priority_returns_its_reasons() -> None:
    """Section 27 forbids a black-box score, so the score is a sum of named factors and
    every one that applied comes back with it."""
    priority = priority_for(
        field_kind=FieldKind.TUITION.value,
        confidence_band="HIGH",
        scope_unresolved=False,
        in_conflict=False,
        corroborated=True,
        confirmation=BodyConfirmation.CONFIRMED,
        thin_source=False,
    )
    assert priority.score > 0
    assert priority.reasons
    assert all(reason[0] in "+-" for reason in priority.reasons)
    assert "high_risk_field" in priority.explain()


def test_an_unresolved_scope_and_a_conflict_lower_priority() -> None:
    def score(**overrides: object) -> int:
        base: dict[str, object] = {
            "field_kind": FieldKind.TUITION.value,
            "confidence_band": "MEDIUM",
            "scope_unresolved": False,
            "in_conflict": False,
            "corroborated": False,
            "confirmation": BodyConfirmation.CONFIRMED,
            "thin_source": False,
        }
        base.update(overrides)
        return priority_for(**base).score  # type: ignore[arg-type]

    assert score(scope_unresolved=True) < score()
    assert score(in_conflict=True) < score()
    assert score(thin_source=True) < score()
    assert score(corroborated=True) > score()


def test_low_candidates_stay_out_of_the_primary_queue() -> None:
    """Section 28. Stored, kept, never deleted, and not in front of a reviewer by
    default."""
    assert (
        queue_for(
            confidence_band="LOW",
            is_superseded=False,
            decision_state="UNREVIEWED",
            is_only_candidate=False,
        )
        is Queue.LOW_CONFIDENCE
    )
    for band in ("HIGH", "MEDIUM"):
        assert (
            queue_for(
                confidence_band=band,
                is_superseded=False,
                decision_state="UNREVIEWED",
                is_only_candidate=False,
            )
            is Queue.PRIMARY
        )


def test_a_low_candidate_with_no_better_alternative_is_worth_reviewing() -> None:
    """Section 28's exception: a weak answer is the only answer there is."""
    assert (
        queue_for(
            confidence_band="LOW",
            is_superseded=False,
            decision_state="UNREVIEWED",
            is_only_candidate=True,
        )
        is Queue.ONLY_CANDIDATE
    )


def test_a_superseded_or_decided_candidate_is_not_queued() -> None:
    assert (
        queue_for(
            confidence_band="HIGH",
            is_superseded=True,
            decision_state="UNREVIEWED",
            is_only_candidate=False,
        )
        is Queue.NOT_QUEUED
    )
    assert (
        queue_for(
            confidence_band="HIGH",
            is_superseded=False,
            decision_state=Decision.ACCEPTED.value,
            is_only_candidate=False,
        )
        is Queue.NOT_QUEUED
    )


# ===========================================================================
# 30-31. Promotion readiness
# ===========================================================================


def test_acceptance_alone_does_not_make_a_candidate_promotable() -> None:
    """Section 30. The source may still be NOT_ELIGIBLE, and no reviewer of candidates
    can change that."""
    blockers = blockers_for(
        decision_state=Decision.ACCEPTED.value,
        is_superseded=False,
        source_eligibility="NOT_ELIGIBLE",
        scope_unresolved=False,
        in_conflict=False,
        responsibility="TUITION_FEES",
        field_kind=FieldKind.TUITION.value,
    )
    assert blockers == ("SOURCE_NOT_ELIGIBLE",)


def test_every_blocker_is_reported_not_just_the_first() -> None:
    """A reviewer clearing one wants to know whether it was the only one."""
    blockers = blockers_for(
        decision_state="UNREVIEWED",
        is_superseded=True,
        source_eligibility="NOT_ELIGIBLE",
        scope_unresolved=True,
        in_conflict=True,
        responsibility="UNIVERSITY_HOME",
        field_kind=FieldKind.TUITION.value,
    )
    assert set(blockers) == {
        "SUPERSEDED_RULE_VERSION",
        "REVIEW_STATE_IS_UNREVIEWED",
        "SOURCE_NOT_ELIGIBLE",
        "SCOPE_UNRESOLVED",
        "UNRESOLVED_CONFLICT",
        "RESPONSIBILITY_DOES_NOT_AUTHORISE_FIELD",
    }


def test_a_fully_cleared_candidate_has_no_blockers() -> None:
    """Non-vacuity: the blocker list must be clearable, or the readiness view would
    always be empty for reasons that have nothing to do with the data."""
    assert (
        blockers_for(
            decision_state=Decision.ACCEPTED.value,
            is_superseded=False,
            source_eligibility="OFFICIAL_VERIFIED",
            scope_unresolved=False,
            in_conflict=False,
            responsibility="TUITION_FEES",
            field_kind=FieldKind.TUITION.value,
        )
        == ()
    )


# ===========================================================================
# 24. Evidence relationship
# ===========================================================================


@pytest.mark.parametrize(
    ("resolved", "raw", "evidence", "expected"),
    [
        ("7.0", "7.0", "IELTS 7.0", "EXACT_RAW"),
        ("IELTS 7.0 overall", "7.0", "IELTS 7.0 overall", "CONTAINS_RAW"),
        (
            "the deadline is 13 January 2027 at 6pm",
            "13 January 2027 6pm",
            "the deadline is 13 January 2027 at 6pm",
            "ALL_RAW_TOKENS_PRESENT",
        ),
        ("a sentence", "elsewhere", "a sentence", "EQUALS_EVIDENCE"),
        ("part", "elsewhere", "a part of it", "WITHIN_EVIDENCE"),
        ("nothing alike", "elsewhere", "completely different", "MISMATCH"),
        (None, "7.0", "IELTS 7.0", "UNRESOLVED"),
    ],
)
def test_the_relationship_between_pointer_and_quote_is_named_not_guessed(
    resolved: str | None, raw: str, evidence: str, expected: str
) -> None:
    """Section 24. Byte equality is not required -- a deadline's raw text joins a date
    match to a time match, so it is not a substring of its own evidence -- but the
    relationship has to be provable rather than hopeful."""
    assert evidence_relationship(resolved, raw, evidence) == expected


def test_a_mismatch_is_not_treated_as_proof() -> None:
    assert "MISMATCH" not in PROVEN_RELATIONSHIPS
    assert "UNRESOLVED" not in PROVEN_RELATIONSHIPS
    assert "ALL_RAW_TOKENS_PRESENT" in PROVEN_RELATIONSHIPS
