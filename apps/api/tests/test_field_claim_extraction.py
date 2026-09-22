"""Field extraction on fixtures (Step 5C.2 section 39).

WHY FIXTURES AND NOT THE REAL FLEET
===================================
The 175 real documents are the subject of a manual claim pass, not of CI. A test that
asserted "Imperial's fee page yields £41,000" would fail the day Imperial redesigns,
and the failure would say nothing about whether our rules are right.

So these are small documents written to hold the *shapes* the real fleet turned out to
have -- a fee table with a labelled column, a range written with an en dash, a deadline
with a day and a month and no year, "no component below 6.5", a requirement with no
country wording at all.

WHAT MOST OF THESE TESTS ASSERT IS A REFUSAL
============================================
The dangerous failure mode here is not missing a fact. It is inventing one: a currency
guessed from the university's country, a year guessed from today's date, a midpoint
computed from a range, a `UNIVERSAL` scope asserted from silence, a 6.5 conjured from
the words "English proficiency required". Every one of those has a test below whose
whole purpose is that nothing is produced.

Parsing goes through the real 5C.1 parser rather than hand-built blocks, so a change
that broke the block structure would surface here too.
"""

from __future__ import annotations

import json

import pytest

from app.domains.claims import admission, deadline, language, tuition
from app.domains.claims.locator import resolve
from app.domains.claims.model import Candidate, Confidence, FieldKind, extractors_for
from app.domains.extraction.document import NormalizedDocument
from app.domains.extraction.html_document import parse_html_document


def resolved(candidate: Candidate) -> dict[str, object]:
    """The candidate's structured value, asserting that the rule produced one.

    `Candidate.value` is legitimately `None` -- an unresolved candidate carries an
    `unresolved_reason` instead, and several tests below check exactly that. Every use of
    this helper is a case where the rule is supposed to have resolved, so a `None` here
    is the test's own failure and should say so rather than raising `TypeError` three
    lines later.
    """
    assert (
        candidate.value is not None
    ), f"{candidate.field_kind} did not resolve: {candidate.unresolved_reason}"
    return candidate.value


def document(body: str, *, title: str = "Test page") -> NormalizedDocument:
    """A normalised document from a body fragment, via the real parser."""
    html = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body><main>{body}</main></body></html>"
    )
    return parse_html_document(html.encode("utf-8"), content_type="text/html; charset=utf-8")


def kinds(candidates: list[Candidate], kind: FieldKind) -> list[Candidate]:
    return [candidate for candidate in candidates if candidate.field_kind is kind]


def only(candidates: list[Candidate], kind: FieldKind) -> Candidate:
    matching = kinds(candidates, kind)
    assert len(matching) == 1, f"expected exactly one {kind}, got {len(matching)}"
    return matching[0]


# ===========================================================================
# 4. Responsibility routing
# ===========================================================================


def test_a_homepage_authorises_no_business_extractor() -> None:
    """Section 4. A homepage saying "from £28,000" is marketing, not a fee claim."""
    assert extractors_for({"UNIVERSITY_HOME"}) == frozenset()
    assert extractors_for({"UNCLASSIFIED"}) == frozenset()


def test_responsibilities_union_rather_than_conflict() -> None:
    """A page claimed for two things answers for both."""
    assert extractors_for({"TUITION_FEES"}) == frozenset({"tuition"})
    assert extractors_for({"POSTGRADUATE_ADMISSIONS", "APPLICATION_DEADLINES"}) == frozenset(
        {"admission", "language", "deadline"}
    )


def test_an_unknown_responsibility_authorises_nothing() -> None:
    """Not a crash and not a default. An unrecognised label grants no permission."""
    assert extractors_for({"SOMETHING_NOBODY_DEFINED"}) == frozenset()


# ===========================================================================
# 6-7. Programme name and degree level
# ===========================================================================


def test_a_programme_heading_yields_a_name_and_a_level() -> None:
    doc = document("<h1>Our courses</h1><h2>MSc Computer Science</h2>")
    out = admission.extract_program(doc, responsibility="PROGRAM_CATALOG")

    name = only(out, FieldKind.PROGRAM_NAME)
    assert name.value == {"program_name": "MSc Computer Science"}
    level = only(out, FieldKind.DEGREE_LEVEL)
    assert level.value is not None
    assert level.value["degree_level"] == "MASTER"
    assert level.unresolved_reason is None


def test_a_heading_without_an_award_is_not_a_programme() -> None:
    """Section 6. "Our courses" is a heading; "MSc Computer Science" is a programme."""
    doc = document("<h1>Our courses</h1><h2>Why study here</h2><h2>Student life</h2>")
    assert admission.extract_program(doc, responsibility="PROGRAM_CATALOG") == []


def test_graduate_is_not_a_degree_level() -> None:
    """Section 7. "Graduate" does not mean PhD, and the claim says so.

    The level is null, the wording is kept, and `DEGREE_LEVEL_AMBIGUOUS` is recorded --
    rather than a plausible-looking MASTER that nobody published.
    """
    level, raw, reason = admission.degree_level_of("Graduate programme in Economics")
    assert level is None
    assert raw == "Graduate"
    assert "not a doctorate" in reason

    doc = document("<h2>Graduate Programme in Economics</h2>")
    out = admission.extract_program(doc, responsibility="PROGRAM_CATALOG")
    degree = only(out, FieldKind.DEGREE_LEVEL)
    assert degree.value is None
    assert degree.unresolved_reason == "DEGREE_LEVEL_AMBIGUOUS"
    assert degree.confidence is Confidence.LOW


def test_advanced_is_not_a_level_either() -> None:
    level, raw, _ = admission.degree_level_of("Advanced Diploma in Management")
    assert level is None
    assert raw == "Advanced"


def test_a_phd_page_mentioning_a_masters_is_read_as_doctorate() -> None:
    """Ordered doctorate -> master -> bachelor: the page's own award is the specific one."""
    level, _, _ = admission.degree_level_of("PhD in Engineering (master's degree required)")
    assert level == "DOCTORATE"


def test_a_duration_inside_a_programme_name_is_recorded_as_stated() -> None:
    doc = document("<h2>MSc Data Science (2 years, part-time)</h2>")
    out = admission.extract_program(doc, responsibility="PROGRAM_CATALOG")
    duration = only(out, FieldKind.DURATION)
    assert duration.value == {"duration_text": "2 years"}
    mode = only(out, FieldKind.STUDY_MODE)
    assert mode.value == {"study_mode_raw": "part-time"}
    assert duration.confidence is Confidence.LOW


def test_a_discipline_from_a_name_is_a_hint_and_says_so() -> None:
    doc = document("<h2>MSc Artificial Intelligence</h2>")
    hint = only(
        admission.extract_program(doc, responsibility="PROGRAM_CATALOG"),
        FieldKind.DISCIPLINE_HINT,
    )
    assert hint.value is not None
    assert hint.value["discipline"] == "COMPUTER_AND_DATA"
    assert hint.confidence is Confidence.LOW
    assert "not a classification" in hint.confidence_reason


def test_a_catalogue_link_naming_an_award_is_a_low_confidence_programme() -> None:
    """How most catalogues actually list programmes."""
    html = (
        "<!DOCTYPE html><html><head><title>Courses</title></head><body><main>"
        '<ul><li><a href="/msc-finance">MSc Finance</a></li>'
        '<li><a href="/about">About us</a></li></ul></main></body></html>'
    )
    doc = parse_html_document(html.encode("utf-8"))
    out = admission.extract_program(doc, responsibility="PROGRAM_CATALOG")
    names = [candidate.value_raw_text for candidate in kinds(out, FieldKind.PROGRAM_NAME)]
    assert names == ["MSc Finance"], "a non-programme link produced a programme"
    claim = only(out, FieldKind.PROGRAM_NAME)
    assert claim.confidence is Confidence.LOW
    # The locator has to be followable, or it is decoration. Version 1 recorded
    # `json_ld_path="links.N"`, which `resolve` had no branch for: 37 of every 200
    # locators sampled from the real fleet pointed nowhere.
    assert claim.locator.kind == "link"
    assert claim.locator.link_index is not None
    assert resolve(doc, claim.locator.as_json()) == "MSc Finance"
    assert claim.locator.json_ld_path is None, "a link is not JSON-LD"


def test_the_program_rule_versions_independently_of_the_requirement_rule() -> None:
    """Two rules in one module, two version constants.

    Not that the numbers differ -- identity is the extractor *name* plus its version,
    so two extractors sitting at "3" is harmless. What matters is that correcting one
    cannot relabel the other's claims, which a shared constant would guarantee.
    """
    assert admission.PROGRAM_EXTRACTOR != admission.EXTRACTOR
    assert "PROGRAM_VERSION" in admission.__all__
    assert "VERSION" in admission.__all__


# ===========================================================================
# 8-10. Admission requirements and scope
# ===========================================================================


def test_a_requirement_paragraph_is_preserved_not_interpreted() -> None:
    doc = document(
        "<h1>Entry requirements</h1>"
        "<p>Applicants must hold a bachelor's degree with a 2:1 or equivalent.</p>"
    )
    claim = only(
        admission.extract(doc, responsibility="ENTRY_REQUIREMENTS"),
        FieldKind.ADMISSION_REQUIREMENT,
    )
    assert claim.value is not None
    assert claim.value["requirement_text"] == (
        "Applicants must hold a bachelor's degree with a 2:1 or equivalent."
    )
    assert claim.value["qualification_hint"] == "bachelor's degree"
    assert claim.confidence is Confidence.MEDIUM
    assert claim.locator.heading_path == ["Entry requirements"]


def test_a_requirement_with_no_country_wording_stays_unresolved() -> None:
    """Section 10. The dangerous move is reading silence as UNIVERSAL.

    A rule written for A-level applicants, asserted as universal, becomes a Chinese
    applicant's requirement. So silence is `SCOPE_MAPPING_REQUIRED`, not everybody.
    """
    doc = document("<h1>Entry requirements</h1><p>Applicants require a good first degree.</p>")
    claim = only(
        admission.extract(doc, responsibility="ENTRY_REQUIREMENTS"),
        FieldKind.ADMISSION_REQUIREMENT,
    )
    assert claim.unresolved_reason == "SCOPE_MAPPING_REQUIRED"
    assert claim.value is not None
    assert claim.value["applicant_scopes"] is None
    assert "UNRESOLVED rather than universal" in claim.confidence_reason


def test_an_explicit_country_scope_is_preserved_with_its_wording() -> None:
    doc = document(
        "<h1>Entry requirements</h1>"
        "<p>Applicants from mainland China require a Gaokao score above the first tier.</p>"
    )
    claim = only(
        admission.extract(doc, responsibility="ENTRY_REQUIREMENTS"),
        FieldKind.ADMISSION_REQUIREMENT,
    )
    assert claim.unresolved_reason is None
    assert claim.value is not None
    scopes = claim.value["applicant_scopes"]
    assert isinstance(scopes, list)
    labels = {scope["scope_label"] for scope in scopes}
    assert {"China", "Gaokao"} <= labels
    china = next(scope for scope in scopes if scope["scope_label"] == "China")
    assert china["applicant_country_code"] == "CN"
    # Step 5C.5: the raw wording is the CONSTRUCTION that asserted the scope, not the
    # bare country token. "mainland China" alone is what a postal address and a campus
    # list also contain; "Applicants from mainland China" is why this row has a
    # jurisdiction, and it is what a reviewer needs to see.
    assert china["scope_raw"] == "Applicants from mainland China"
    assert china["match_kind"] == "APPLICANT_ORIGIN"


def test_a_qualification_marker_maps_no_country() -> None:
    """A-level is a qualification system, not a nationality. Mapping it to GB would be
    an inference about the applicant that the page did not make."""
    scopes = admission.scopes_in("Applicants offering A-levels need AAB.")
    assert scopes == [
        {
            "scope_raw": "A-levels",
            "scope_label": "A-level",
            "applicant_country_code": None,
            # Step 5C.5: the dimension is stated rather than implied by the country
            # code being null, which was an accident of the data rather than a claim
            # about it.
            "dimension": "QUALIFICATION_SYSTEM",
            "match_kind": "QUALIFICATION_SYSTEM",
        }
    ]


def test_prose_without_requirement_wording_produces_nothing() -> None:
    """Section 18. A page about campus life is not a requirement claim."""
    doc = document("<h1>Student life</h1><p>Our campus has three libraries and a lake.</p>")
    assert admission.extract(doc, responsibility="ENTRY_REQUIREMENTS") == []


def test_a_requirement_in_a_list_item_is_located_to_the_item() -> None:
    doc = document(
        "<h1>Admission requirements</h1>"
        "<ul><li>Applicants must hold a recognised bachelor's degree.</li>"
        "<li>Applicants must demonstrate English proficiency.</li></ul>"
    )
    claims = admission.extract(doc, responsibility="ENTRY_REQUIREMENTS")
    assert len(claims) == 2
    assert [claim.locator.list_item_index for claim in claims] == [0, 1]
    assert all(claim.locator.kind == "list_item" for claim in claims)
    # The locator resolves to the wording the claim quotes.
    for claim in claims:
        assert resolve(doc, claim.locator.as_json()) == claim.evidence_text


# ===========================================================================
# 11-12. Language requirements
# ===========================================================================


def test_english_proficiency_required_produces_no_score() -> None:
    """Section 11. The single most damaging thing this extractor could do.

    A fabricated 6.5 is indistinguishable downstream from a real one, so the absence
    of a number has to produce the absence of a score.
    """
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>All applicants must demonstrate English language proficiency.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE) == []
    assert kinds(out, FieldKind.LANGUAGE_COMPONENT_SCORE) == []
    assert out == [], "wording with no test name and no number produced a claim"


def test_a_test_name_without_a_number_is_a_test_claim_only() -> None:
    """ "We accept IELTS" is a fact about the page. It is not a score."""
    doc = document("<h1>English language</h1><p>We accept IELTS and TOEFL results.</p>")
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert {candidate.field_kind for candidate in out} == {FieldKind.LANGUAGE_TEST}
    assert {candidate.value_raw_text for candidate in out} == {"IELTS", "TOEFL"}


def test_an_ielts_overall_score_records_the_operator() -> None:
    doc = document(
        "<h1>English language requirements</h1><p>IELTS overall of at least 7.0 is required.</p>"
    )
    overall = only(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert overall.value is not None
    assert overall.value["score"] == 7.0
    assert overall.value["operator"] == "AT_LEAST"
    assert overall.value["explicitly_overall"] is True
    assert overall.confidence is Confidence.HIGH


def test_above_is_a_strict_inequality_and_at_least_is_not() -> None:
    """Section 12. Flattening both to 7.0 loses whether exactly 7.0 is admissible."""
    strict = language._operator_for("above")
    inclusive = language._operator_for("at least")
    assert (strict.code, inclusive.code) == ("ABOVE", "AT_LEAST")
    assert "strict" in strict.reason

    doc = document("<h1>English</h1><p>IELTS overall above 6.5.</p>")
    overall = only(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert overall.value is not None
    assert overall.value["operator"] == "ABOVE"


def test_no_component_below_is_a_component_floor_not_a_component_score() -> None:
    """Section 12. It states a floor for every component, not a score for one."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 7.0 overall with no component below 6.5.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    overall = only(out, FieldKind.LANGUAGE_OVERALL_SCORE)
    assert overall.value is not None
    assert overall.value["score"] == 7.0, "the component floor was read as the overall score"

    component = only(out, FieldKind.LANGUAGE_COMPONENT_SCORE)
    assert component.value is not None
    assert component.value["score"] == 6.5
    assert component.value["applies_to"] == "ALL_COMPONENTS"
    assert component.value["operator"] == "AT_LEAST"


def test_a_named_component_keeps_its_own_component_name() -> None:
    doc = document("<h1>English</h1><p>IELTS 7.0 overall, Writing 6.5, Speaking 6.0.</p>")
    components = kinds(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_COMPONENT_SCORE,
    )
    named = {
        candidate.value["applies_to"]: candidate.value["score"]
        for candidate in components
        if candidate.value is not None
    }
    assert named == {"WRITING": 6.5, "SPEAKING": 6.0}


def test_a_toefl_score_on_its_own_scale_is_accepted() -> None:
    doc = document("<h1>English language</h1><p>TOEFL iBT minimum of 100 overall.</p>")
    overall = only(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert overall.value is not None
    assert overall.value == {
        "test": "TOEFL",
        "score": 100.0,
        "operator": "AT_LEAST",
        "explicitly_overall": True,
    }


def test_an_implausible_score_for_a_known_test_is_refused() -> None:
    """A "IELTS 100" is a page we misread, not a requirement. 100 is off the 0-9 scale."""
    doc = document("<h1>English</h1><p>IELTS 100 required for entry.</p>")
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE) == []
    assert [candidate.field_kind for candidate in out] == [FieldKind.LANGUAGE_TEST]


def test_an_institution_code_near_a_test_name_is_not_an_overall_score() -> None:
    """UCL's page carries "TOEFL ... institution code 0246", and version 2 recorded an
    overall TOEFL requirement of 3.0 from a fragment of it.

    Every such number was inside the test's scale. `KNOWN_TESTS` bounds the scale;
    `OVERALL_REQUIREMENT_FLOORS` bounds what a university actually publishes.
    """
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>Please send TOEFL results to UCL. Our institution code is 3.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE) == []
    assert [candidate.field_kind for candidate in out] == [FieldKind.LANGUAGE_TEST]


def test_a_component_score_is_not_floored_like_an_overall_one() -> None:
    """A TOEFL component requirement of 21 is ordinary; a TOEFL *overall* of 21 is not.

    Flooring both would throw away real component requirements.
    """
    doc = document(
        "<h1>English language requirements</h1>"
        "<table><thead><tr><th>Test</th><th>Overall</th><th>Each component</th></tr></thead>"
        "<tbody><tr><td>TOEFL iBT</td><td>100</td><td>21</td></tr></tbody></table>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    cells = [c for c in out if c.locator.kind == "table_cell"]
    assert {c.field_kind: c.value["score"] for c in cells if c.value is not None} == {
        FieldKind.LANGUAGE_OVERALL_SCORE: 100.0,
        FieldKind.LANGUAGE_COMPONENT_SCORE: 21.0,
    }


def test_an_unknown_test_name_is_preserved_rather_than_dropped() -> None:
    """Section 11. There is no list of every English test a university may accept."""
    assert language._plausible("SOMETHING_NEW", 4242.0) is True


def test_two_tests_in_one_paragraph_each_keep_their_own_score() -> None:
    """The defect the plane test found: reading the whole paragraph once per test gave
    TOEFL IELTS's 7.0. A number belongs to the test named nearest to it."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 7.0 overall is required. We also accept TOEFL iBT with a minimum "
        "of 100 overall.</p>"
    )
    scores = {
        candidate.value["test"]: candidate.value["score"]
        for candidate in kinds(
            language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
            FieldKind.LANGUAGE_OVERALL_SCORE,
        )
        if candidate.value is not None
    }
    assert scores == {"IELTS": 7.0, "TOEFL": 100.0}


def test_a_coordinated_pairing_is_read_the_way_it_is_written() -> None:
    """The case raw distance gets wrong.

    In "7.0 in IELTS or 100 in TOEFL", the 100 sits 4 characters after "IELTS" and 7
    before "TOEFL". Distance alone hands TOEFL's score to IELTS; what a reader uses is
    that "or" coordinates alternatives while "in" binds a value to a name.
    """
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>An overall score of 7.0 in IELTS or 100 in TOEFL is required.</p>"
    )
    overall = kinds(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert {
        candidate.value["test"]: candidate.value["score"]
        for candidate in overall
        if candidate.value is not None
    } == {"IELTS": 7.0, "TOEFL": 100.0}
    assert all(candidate.context["test_attribution"] == "binding" for candidate in overall)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<p>We accept IELTS 7.0, TOEFL 100 or PTE 76.</p>",
            {"IELTS": 7.0, "TOEFL": 100.0, "PTE": 76.0},
        ),
        (
            "<p>IELTS 6.5 overall with 6.0 in each component.</p>",
            {"IELTS": 6.5},
        ),
        (
            "<p>TOEFL 100 overall with a minimum of 22 in writing.</p>",
            {"TOEFL": 100.0},
        ),
        (
            "<p>Applicants need IELTS 7.0 OR TOEFL 100.</p>",
            {"IELTS": 7.0, "TOEFL": 100.0},
        ),
        (
            "<p>An overall score of 7.0 in IELTS, 100 in TOEFL, or 76 in PTE.</p>",
            {"IELTS": 7.0, "TOEFL": 100.0, "PTE": 76.0},
        ),
        (
            "<p>IELTS: 7.5 (no band below 7.0); TOEFL iBT: 110 (no section below 25).</p>",
            {"IELTS": 7.5, "TOEFL": 110.0},
        ),
    ],
)
def test_every_number_stays_bound_to_its_own_test(body: str, expected: dict[str, float]) -> None:
    """Section 17. The C44 regression, widened.

    Each of these puts two or three tests in one sentence, in the shapes real pages
    use: comma-separated, coordinated with OR, value-before-name, and semicolon-
    separated with component floors. A score attributed to the wrong test is a wrong
    entry requirement, which sends a student to apply for something they cannot get.
    """
    doc = document(f"<h1>English language requirements</h1>{body}")
    overall = {
        candidate.value["test"]: candidate.value["score"]
        for candidate in kinds(
            language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
            FieldKind.LANGUAGE_OVERALL_SCORE,
        )
        if candidate.value is not None
    }
    assert overall == expected


def test_a_component_floor_is_not_read_as_another_tests_overall() -> None:
    """ "IELTS 6.5 overall with 6.0 in each" must not give some other test a 6.0."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 6.5 overall with 6.0 in each component. We also accept TOEFL 90.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    overall = {
        c.value["test"]: c.value["score"]
        for c in kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE)
        if c.value is not None
    }
    assert overall == {"IELTS": 6.5, "TOEFL": 90.0}
    components = kinds(out, FieldKind.LANGUAGE_COMPONENT_SCORE)
    assert all(c.value is not None and c.value["test"] == "IELTS" for c in components)


def test_a_named_component_stays_with_its_own_test() -> None:
    """ "TOEFL 100 with a minimum of 22 in writing" gives TOEFL the 22, not IELTS."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 7.0 overall is required. TOEFL iBT 100 with a minimum of 22 in "
        "writing is also accepted.</p>"
    )
    components = kinds(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_COMPONENT_SCORE,
    )
    for candidate in components:
        assert candidate.value is not None
        assert candidate.value["test"] == "TOEFL", candidate.value


def test_a_positional_attribution_is_never_high_confidence() -> None:
    """ "The page said this" and "we inferred it from position" are different
    statements, and a reviewer needs to know which one they are checking."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS is required. The overall requirement for the programme is 7.0.</p>"
    )
    overall = kinds(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert overall, "the fixture produced no score to check"
    for candidate in overall:
        assert candidate.context["test_attribution"] == "positional"
        assert candidate.confidence is not Confidence.HIGH
        assert "by position" in candidate.confidence_reason


def test_a_number_far_from_any_test_name_is_not_a_score() -> None:
    """ "IELTS is required" at the start and "2" in "within 2 years" at the end is not a
    requirement of 2.0."""
    filler = "We assess each application on its own merits and consider the whole file. "
    doc = document(
        "<h1>English language requirements</h1>"
        f"<p>An IELTS qualification is required. {filler * 2}Tests must be taken "
        "within 2 years of the start date.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE) == []
    assert [candidate.field_kind for candidate in out] == [FieldKind.LANGUAGE_TEST]


def test_every_candidate_from_one_block_has_its_own_locator() -> None:
    """The fingerprint is built from the locator, so identical locators mean claims
    silently dropped on insert. This is the regression test for that."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 7.0 overall with no component below 6.5, Writing 6.5, Speaking 6.0. "
        "We also accept TOEFL iBT with a minimum of 100 overall.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    identities = [
        (candidate.field_kind, json.dumps(candidate.locator.as_json(), sort_keys=True))
        for candidate in out
    ]
    assert len(identities) == len(set(identities)), "two claims share one locator"
    fingerprints = {
        candidate.fingerprint(extraction_id="e", extractor="lang", version="1") for candidate in out
    }
    assert len(fingerprints) == len(out)


def test_a_component_floor_is_not_also_read_as_an_overall_score() -> None:
    """The component span is excluded from the search for an overall score, so a page
    stating only a floor does not gain an overall requirement it never published."""
    doc = document(
        "<h1>English language requirements</h1><p>IELTS with no component below 6.5.</p>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    assert kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE) == []
    component = only(out, FieldKind.LANGUAGE_COMPONENT_SCORE)
    assert component.value is not None
    assert component.value["score"] == 6.5


def test_a_locator_span_points_at_the_matched_wording() -> None:
    doc = document("<h1>English language</h1><p>IELTS 7.0 overall required.</p>")
    overall = only(
        language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS"),
        FieldKind.LANGUAGE_OVERALL_SCORE,
    )
    assert resolve(doc, overall.locator.as_json()) == overall.value_raw_text


def test_a_labelled_score_table_is_high_confidence() -> None:
    """Section 20. A table is strong evidence *because* the publisher labelled it."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<table><thead><tr><th>Test</th><th>Overall</th><th>Each component</th></tr></thead>"
        "<tbody><tr><td>IELTS</td><td>7.0</td><td>6.5</td></tr></tbody></table>"
    )
    out = language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
    overall = [
        candidate
        for candidate in kinds(out, FieldKind.LANGUAGE_OVERALL_SCORE)
        if candidate.locator.kind == "table_cell"
    ]
    components = [
        candidate
        for candidate in kinds(out, FieldKind.LANGUAGE_COMPONENT_SCORE)
        if candidate.locator.kind == "table_cell"
    ]
    assert len(overall) == 1 and len(components) == 1
    assert overall[0].value is not None and overall[0].value["score"] == 7.0
    assert components[0].value is not None and components[0].value["score"] == 6.5
    assert overall[0].confidence is Confidence.HIGH
    assert overall[0].locator.column_index == 1
    assert resolve(doc, overall[0].locator.as_json()) == "7.0"


def test_an_overall_band_column_is_not_filed_as_a_component() -> None:
    """ "Overall band score" contains the word "band". Reading it as a component floor
    would turn one 7.0 requirement into a floor on all four skills."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<table><thead><tr><th>Test</th><th>Overall band score</th></tr></thead>"
        "<tbody><tr><td>IELTS</td><td>7.0</td></tr></tbody></table>"
    )
    cells = [
        candidate
        for candidate in language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
        if candidate.locator.kind == "table_cell"
    ]
    assert [candidate.field_kind for candidate in cells] == [FieldKind.LANGUAGE_OVERALL_SCORE]


def test_a_numeric_table_with_no_header_yields_nothing() -> None:
    """A table is only strong evidence when a header labels the column."""
    doc = document(
        "<h1>English</h1><table><tbody><tr><td>IELTS</td><td>7.0</td></tr></tbody></table>"
    )
    out = language._from_tables(doc)
    assert out == []


# ===========================================================================
# 13-15. Tuition
# ===========================================================================


def test_an_exact_fee_records_currency_and_billing_unit_when_stated() -> None:
    doc = document(
        "<h1>Tuition fees</h1>"
        "<p>The tuition fee for international students is £38,000 per year.</p>"
    )
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_kind"] == "EXACT"
    assert claim.value["amount_min"] == claim.value["amount_max"] == 38000.0
    assert claim.value["currency"] == "GBP"
    assert claim.value["billing_unit"] == "PER_YEAR"
    assert claim.value["student_category"] == ["INTERNATIONAL"]
    assert claim.unresolved_reason is None


def test_a_range_keeps_both_endpoints_and_never_a_midpoint() -> None:
    """Section 13, rule 1. Averaging invents a number no university published."""
    doc = document("<h1>Fees</h1><p>Tuition fees range from £28,000 – £32,000 per year.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_kind"] == "RANGE"
    assert claim.value["amount_min"] == 28000.0
    assert claim.value["amount_max"] == 32000.0
    assert 30000.0 not in claim.value.values(), "a midpoint was derived"


def test_a_from_price_is_a_floor_not_an_exact_fee() -> None:
    doc = document("<h1>Fees</h1><p>Tuition starts from £24,000 per year.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_kind"] == "FROM"
    assert claim.value["amount_min"] == 24000.0
    assert claim.value["amount_max"] is None


def test_an_up_to_price_is_a_ceiling() -> None:
    doc = document("<h1>Fees</h1><p>Course fees of up to £12,500 may apply.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_kind"] == "UP_TO"
    assert claim.value["amount_min"] is None
    assert claim.value["amount_max"] == 12500.0


def test_a_fee_with_no_currency_says_the_currency_is_absent() -> None:
    """Section 13, rule 2. A UK university publishing "30,000" has not said pounds.

    Guessing GBP from the institution's country is how a fee in RMB reaches a Chinese
    student's screen as a fee in pounds.
    """
    doc = document("<h1>Tuition fees</h1><p>The annual tuition fee is 30,000.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["currency"] is None
    assert claim.unresolved_reason is not None
    assert "CURRENCY_ABSENT" in claim.unresolved_reason


def test_a_fee_with_no_billing_unit_is_not_assumed_annual() -> None:
    """Section 13, rule 3. A displayed fee is not annual because annual is common."""
    doc = document("<h1>Tuition fees</h1><p>The tuition fee is £9,250.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["billing_unit"] is None
    assert claim.unresolved_reason is not None
    assert "BILLING_UNIT_UNRESOLVED" in claim.unresolved_reason


def test_a_student_category_is_never_inferred_from_the_currency() -> None:
    """Section 14. "£38,000" does not say who pays it."""
    doc = document("<h1>Tuition fees</h1><p>Tuition is £38,000 per year.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["student_category"] is None
    assert claim.unresolved_reason is not None
    assert "STUDENT_CATEGORY_UNRESOLVED" in claim.unresolved_reason


def test_variable_fees_are_a_statement_and_absence_is_not() -> None:
    """Section 15. A page addressing fees without a figure has said something.

    A page that never mentions fees has not, and produces nothing (section 18).
    """
    varies = document("<h1>Tuition fees</h1><p>Tuition fees vary by programme.</p>")
    claim = only(tuition.extract(varies, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value == {"amount_kind": "VARIABLE"}

    silent = document("<h1>Our campus</h1><p>We have three libraries and a lake.</p>")
    assert tuition.extract(silent, responsibility="TUITION_FEES") == []


def test_a_year_is_not_read_as_a_fee() -> None:
    """ "2027 entry" has the shape of money only if you ignore separators and symbols."""
    doc = document("<h1>Tuition fees</h1><p>Fees for 2027 entry are confirmed in March.</p>")
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []


def test_a_credit_count_is_not_read_as_a_fee() -> None:
    doc = document("<h1>Tuition fees</h1><p>The fee covers 180 credits of study.</p>")
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []


def test_a_labelled_fee_table_takes_currency_from_its_own_header() -> None:
    """Section 13. The header is context the publisher supplied, in the same table --
    not cross-page inference."""
    doc = document(
        "<h1>Tuition fees</h1>"
        "<table><thead><tr><th>Programme</th><th>Annual fee (GBP)</th></tr></thead>"
        "<tbody><tr><td>MSc Computer Science (international)</td><td>38,000</td></tr>"
        "<tr><td>MSc Finance (international)</td><td>41,500</td></tr></tbody></table>"
    )
    cells = [
        candidate
        for candidate in tuition.extract(doc, responsibility="TUITION_FEES")
        if candidate.locator.kind == "table_cell"
    ]
    assert len(cells) == 2
    first = cells[0]
    assert first.value is not None
    assert first.value["amount_min"] == 38000.0
    assert first.value["currency"] == "GBP"
    assert first.value["billing_unit"] == "PER_YEAR"
    assert first.value["student_category"] == ["INTERNATIONAL"]
    assert first.confidence is Confidence.HIGH
    assert resolve(doc, first.locator.as_json()) == "38,000"


def test_a_fee_heading_alone_admits_the_claim_but_only_at_low_confidence() -> None:
    """Section 21. The heading is context a human uses, so the rule uses it -- but an
    ancestor heading is weaker evidence than fee wording, and the band says so.

    Princeton's page is why: "Cost & Aid > New & Noteworthy > University Raises Funds
    for United Way" put a $121,467.08 donation total under a heading containing "Cost",
    and version 1 recorded it at MEDIUM on that basis alone.
    """
    doc = document("<h1>Tuition fees</h1><h2>International</h2><p>£38,000 per year.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.confidence is Confidence.LOW
    assert "the section heading is the only thing" in claim.confidence_reason
    assert claim.locator.heading_path == ["Tuition fees", "International"]

    # Fee wording plus a fee heading is the MEDIUM case.
    stated = document(
        "<h1>Tuition fees</h1><h2>International</h2>" "<p>The tuition fee is £38,000 per year.</p>"
    )
    claim = only(tuition.extract(stated, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.confidence is Confidence.MEDIUM


def test_a_postcode_is_not_a_fee() -> None:
    """Northwestern's "Evanston, IL 60208" became a fee of 60,208 under version 1.

    The old guard only excluded bare numbers below 10,000, which is the one range
    postcodes are not in.
    """
    doc = document(
        "<h1>Tuition and aid</h1>"
        "<p>Undergraduate Financial Aid, 633 Clark Street, Suite 1603, Evanston, IL 60208</p>"
    )
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []


def test_an_id_inside_a_settings_payload_is_not_a_fee() -> None:
    """A number from a Drupal settings blob on Harvard's page became a fee of 597,778."""
    doc = document(
        "<h1>Cost of attendance</h1>"
        '<p>{"path":{"baseUrl":"/","currentPath":"node/21"},"ajaxTrustedUrl":597778}</p>'
    )
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []


def test_a_descending_amount_pair_is_ambiguous_not_exact() -> None:
    """Princeton's cell reads "$90,574 -$83,000".

    Version 1 fell through to EXACT and recorded 90,574, discarding the other number
    and asserting a shape the page did not have. Range-written-backwards and
    net-after-deduction are both readings, and section 26 says not to choose.
    """
    doc = document("<h1>Fees</h1><p>Tuition &amp; fees: $90,574 -$83,000 for the year.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_kind"] == "AMOUNT_SHAPE_AMBIGUOUS"
    assert claim.value["amount_min"] == 83000.0
    assert claim.value["amount_max"] == 90574.0
    assert claim.unresolved_reason is not None
    assert "AMOUNT_SHAPE_AMBIGUOUS" in claim.unresolved_reason


def test_a_css_colour_triplet_is_not_a_fee() -> None:
    """UBC's page carries `--wp-block-synced-color--rgb:122,0,223`, and "0,223" matched
    as a thousands-separated 223. A fragment of a longer digit run is not an amount."""
    doc = document(
        "<h1>Tuition fees</h1>"
        "<p>:root{--wp-block-synced-color:#7a00df;--wp-block-synced-color--rgb:122,0,223}</p>"
    )
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []


def test_an_ambiguous_shape_in_a_table_cell_is_reported_too() -> None:
    """A cell holding "$90,574 -$83,000" is as ambiguous as the same wording in a
    sentence, and a labelled header does not make the shape certain."""
    doc = document(
        "<h1>Tuition fees</h1>"
        "<table><thead><tr><th>Average net cost</th></tr></thead>"
        "<tbody><tr><td>$90,574 -$83,000</td></tr></tbody></table>"
    )
    cell = next(
        candidate
        for candidate in tuition.extract(doc, responsibility="TUITION_FEES")
        if candidate.locator.kind == "table_cell"
    )
    assert cell.value is not None
    assert cell.value["amount_kind"] == "AMOUNT_SHAPE_AMBIGUOUS"
    assert cell.unresolved_reason is not None
    assert "AMOUNT_SHAPE_AMBIGUOUS" in cell.unresolved_reason
    assert cell.confidence is Confidence.MEDIUM, "an ambiguous shape was recorded as HIGH"


# ===========================================================================
# Site chrome is not page content
# ===========================================================================

NAV_PAGE = (
    # A neutral heading: "Postgraduate taught" would itself be an ambiguous award name
    # in the page's own content, which is a different claim and a different test.
    "<h1>Study with us</h1>"
    "<nav><ul><li>Apply Entry requirements Accepted qualifications Deadlines "
    "Fees and funding English language requirements IELTS 7.0 Tuition fees "
    "£38,000 Dates and deadlines 15 January 2027</li></ul></nav>"
    "<footer><p>Applicants must hold a degree. IELTS 7.0. Fees are £41,000 "
    "per year. The deadline is 1 March 2027.</p></footer>"
)


@pytest.mark.parametrize(
    ("extractor", "responsibility"),
    [
        (admission.extract, "ENTRY_REQUIREMENTS"),
        (admission.extract_program, "PROGRAM_CATALOG"),
        (language.extract, "LANGUAGE_REQUIREMENTS"),
        (tuition.extract, "TUITION_FEES"),
        (deadline.extract, "APPLICATION_DEADLINES"),
        (deadline.extract_calendar, "ACADEMIC_CALENDAR"),
    ],
)
def test_no_rule_reads_navigation_or_a_footer_as_page_content(
    extractor: object, responsibility: str
) -> None:
    """What the lineage proof on Imperial's page showed.

    Its `ADMISSION_REQUIREMENT` claim quoted 2,000 characters of the site menu -- "Apply
    Undergraduate Application process Choose a course Entry requirements Accepted
    qualifications Deadlines ..." -- because that menu is one list item containing the
    words every rule was looking for. It resolved correctly and was attributed
    correctly, and it was still not a requirement.

    Step 5C.1 recorded `Block.container` instead of deleting this text, so that this
    decision could be made here. This is that decision.
    """
    doc = document(NAV_PAGE)
    assert extractor(doc, responsibility=responsibility) == []  # type: ignore[operator]


def test_content_beside_navigation_still_produces_claims() -> None:
    """Non-vacuity: the guard must exclude the menu, not the page."""
    doc = document(
        "<nav><ul><li>Fees and funding Entry requirements</li></ul></nav>"
        "<main><h1>Tuition fees</h1>"
        "<p>The tuition fee for international students is £38,000 per year.</p></main>"
    )
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.value is not None
    assert claim.value["amount_min"] == 38000.0


def test_an_aside_is_content_and_a_form_is_too() -> None:
    """`aside` and `form` are deliberately not chrome: a fee table in an aside is real,
    and a deadline printed beside an application form is too."""
    doc = document(
        "<aside><h2>Tuition fees</h2>"
        "<p>The tuition fee is £38,000 per year for international students.</p></aside>"
    )
    assert kinds(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)


def test_each_extractor_has_its_own_version_constant() -> None:
    """A shared constant means correcting one rule relabels every claim from the other
    as a new version, which is the opposite of what versioning is for."""
    from app.domains.claims.runner import EXTRACTORS

    names = [name for _, name, _ in EXTRACTORS.values()]
    assert len(names) == len(set(names)), "two registry entries share an extractor name"


# ===========================================================================
# 16-17. Deadlines
# ===========================================================================


def test_a_date_without_a_year_records_no_year() -> None:
    """Section 16. The year is not inferred from today, nor from a nearby "2027 entry".

    C9/C13 exist because a date-only fact converted to midnight UTC is a fabricated
    instant, and a deadline is the field where an hour matters.
    """
    doc = document("<h1>Application deadlines</h1><p>The application deadline is 15 January.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert claim.value["year"] is None
    assert (claim.value["month"], claim.value["day"]) == (1, 15)
    assert claim.value["hour"] is None
    assert claim.value["timezone"] is None
    assert claim.unresolved_reason is not None
    assert "YEAR_NOT_STATED" in claim.unresolved_reason
    assert "TIME_NOT_STATED" in claim.unresolved_reason


def test_a_full_date_with_a_time_and_a_zone_records_all_of_it() -> None:
    doc = document("<h1>Deadlines</h1><p>Applications close on 15 January 2027 at 23:59 GMT.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert claim.value["year"] == 2027
    assert claim.value["hour"] == 23
    assert claim.value["minute"] == 59
    assert claim.value["timezone"] == "GMT"
    assert claim.unresolved_reason is None


def test_a_missing_timezone_is_recorded_as_missing() -> None:
    """Section 16. An instant without a zone is not an instant."""
    doc = document("<h1>Deadlines</h1><p>Applications close on 15 January 2027 at 5pm.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert claim.value["hour"] == 17
    assert claim.value["timezone"] is None
    assert claim.unresolved_reason == "TIMEZONE_NOT_STATED"


def test_a_round_number_is_only_recorded_when_stated() -> None:
    """ "Round 1" has a number. "Priority" does not, and inventing one would publish an
    ordering the university never stated."""
    numbered = document("<h1>Key dates</h1><p>Round 1 deadline: 15 October 2026.</p>")
    claim = only(
        deadline.extract(numbered, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert str(claim.context["round_label"]).lower().startswith("round")
    assert claim.context["round_number"] == 1

    unnumbered = document("<h1>Key dates</h1><p>Priority deadline: 15 October 2026.</p>")
    claim = only(
        deadline.extract(unnumbered, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert str(claim.context["round_label"]).lower() == "priority"
    assert "round_number" not in claim.context


def test_round_one_does_not_become_one_in_the_morning() -> None:
    """A bare number is not a time."""
    doc = document("<h1>Key dates</h1><p>Round 1 deadline: 15 October 2026.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert claim.value["hour"] is None


def test_rolling_admission_is_its_own_kind() -> None:
    doc = document("<h1>Deadlines</h1><p>Applications are reviewed on a rolling basis.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value == {"deadline_kind": "ROLLING"}


def test_an_explicit_kind_wins_over_a_date_in_the_same_sentence() -> None:
    """A page saying both is describing two things; the explicit statement is stronger."""
    doc = document(
        "<h1>Deadlines</h1>"
        "<p>There is no fixed deadline, though applications opened on 1 September 2026.</p>"
    )
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value == {"deadline_kind": "NO_FIXED_DEADLINE"}


def test_a_closed_intake_is_recorded_as_closed() -> None:
    doc = document("<h1>Deadlines</h1><p>We are not currently accepting applications.</p>")
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value == {"deadline_kind": "NOT_CURRENTLY_ACCEPTING"}


def test_a_time_belongs_to_its_own_date_not_the_blocks_first_date() -> None:
    """UCL's sentence, which version 2 read as "15 October 18:00".

    "Applications for all 2027 entry courses, except those with a 15 October deadline,
    should arrive at UCAS by 18:00 (UK time) on 13 January 2027." The first date is 15
    October; the 18:00 is 35 characters away and belongs to 13 January. A deadline is
    the one field where being an hour wrong matters.
    """
    doc = document(
        "<h1>Application deadlines</h1>"
        "<p>Applications for all 2027 entry undergraduate courses, except those with "
        "a 15 October deadline, should arrive at UCAS by 18:00 (UK time) on "
        "13 January 2027.</p>"
    )
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert (claim.value["month"], claim.value["day"]) == (10, 15)
    assert claim.value["hour"] is None, "a time 35 characters away was bound to this date"
    assert claim.unresolved_reason is not None
    assert "TIME_NOT_STATED" in claim.unresolved_reason


def test_a_time_far_from_any_date_is_not_a_deadline_time() -> None:
    """Harvard's timeline block: a date in one sentence and a time in another, 700
    characters apart, became "November 1 11:59pm"."""
    filler = "Consider taking the following tests and review the guidance carefully. "
    doc = document(
        "<h1>Application deadlines</h1>"
        f"<p>The Early Action deadline is November 1. {filler * 10}"
        "Materials are due by 11:59pm.</p>"
    )
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert claim.value["hour"] is None


def test_an_adjacent_time_is_still_bound_to_its_date() -> None:
    """Non-vacuity: the guard must exclude distant times, not all times."""
    doc = document(
        "<h1>Application deadlines</h1>"
        "<p>The closing date for applications is 13 January 2027 at 6pm (GMT).</p>"
    )
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert (claim.value["hour"], claim.value["minute"]) == (18, 0)
    # "6pm (GMT)" states a timezone; version 2 recorded TIMEZONE_NOT_STATED because the
    # zone pattern could not cross the opening bracket.
    assert claim.value["timezone"] == "GMT"
    assert claim.unresolved_reason is None


def test_a_time_stated_before_its_date_is_bound_to_it() -> None:
    """ "by 18:00 (UK time) on 13 January 2027" puts the time first, and it is still
    this date's time."""
    doc = document(
        "<h1>Application deadlines</h1>"
        "<p>Applications should arrive by 18:00 GMT on 13 January 2027.</p>"
    )
    claim = only(
        deadline.extract(doc, responsibility="APPLICATION_DEADLINES"),
        FieldKind.APPLICATION_DEADLINE,
    )
    assert claim.value is not None
    assert (claim.value["hour"], claim.value["timezone"]) == (18, "GMT")


def test_a_page_with_no_deadline_wording_produces_nothing() -> None:
    """Section 18. Absence is not a fact: no claim, and certainly not
    OFFICIALLY_NOT_PUBLISHED."""
    doc = document("<h1>Fees</h1><p>Our fees are £38,000 per year.</p>")
    assert deadline.extract(doc, responsibility="APPLICATION_DEADLINES") == []


def test_an_academic_calendar_date_is_not_an_application_deadline() -> None:
    """Section 22. "Beginning of instruction" is not a deadline, and must not be filed
    as one -- so it gets its own field kind and its own extractor."""
    doc = document(
        "<h1>Academic calendar 2026-27</h1>"
        "<p>Beginning of instruction: 28 September 2026. Registration ends: 2 October 2026.</p>"
    )
    out = deadline.extract_calendar(doc, responsibility="ACADEMIC_CALENDAR")
    assert out, "a calendar page with dates produced nothing"
    assert {candidate.field_kind for candidate in out} == {FieldKind.ACADEMIC_CALENDAR_EVENT}
    assert all("NOT an application deadline" in c.confidence_reason for c in out)
    assert kinds(out, FieldKind.APPLICATION_DEADLINE) == []


def test_a_calendar_locator_narrows_to_the_line_not_the_page() -> None:
    """A per-block locator on a PDF calendar page would point at 2,000 characters."""
    doc = document(
        "<h1>Academic calendar</h1>"
        "<p>" + ("Filler sentence. " * 40) + "Beginning of instruction: 28 September 2026.</p>"
    )
    out = deadline.extract_calendar(doc, responsibility="ACADEMIC_CALENDAR")
    assert len(out) == 1
    claim = out[0]
    assert claim.locator.char_start is not None and claim.locator.char_end is not None
    span = claim.locator.char_end - claim.locator.char_start
    assert span < 200, f"the locator spans {span} characters"
    resolved = resolve(doc, claim.locator.as_json())
    assert resolved is not None and "28 September 2026" in resolved


def test_two_dates_in_one_block_are_two_distinct_claims() -> None:
    """The defect the real pass found, on Cornell's calendar.

    Version 1 located an entry by a window clamped to the block, so two dates near the
    start of one short block both got `char_start=0`, the same fingerprint, and one of
    them was discarded on insert. 34 of 2,156 candidates were lost that way.
    """
    doc = document("<h1>Academic calendar</h1><p>Oct 10 and Oct 15 are holidays.</p>")
    out = deadline.extract_calendar(doc, responsibility="ACADEMIC_CALENDAR")
    assert len(out) == 2, [candidate.value_raw_text for candidate in out]
    spans = {(candidate.locator.char_start, candidate.locator.char_end) for candidate in out}
    assert len(spans) == 2, "two entries share one locator"
    fingerprints = {
        candidate.fingerprint(extraction_id="e", extractor="cal", version="2") for candidate in out
    }
    assert len(fingerprints) == 2
    # The locator points at the date; the surrounding line is the evidence.
    for candidate in out:
        assert resolve(doc, candidate.locator.as_json()) == candidate.value_raw_text
        assert "holidays" in candidate.evidence_text


def test_the_calendar_rule_versions_independently_of_the_deadline_rule() -> None:
    """Sharing one constant with the deadline rule meant correcting the calendar would
    have relabelled 81 unchanged deadline claims as a new version."""
    assert deadline.CALENDAR_EXTRACTOR != deadline.EXTRACTOR
    assert "CALENDAR_VERSION" in deadline.__all__
    assert "VERSION" in deadline.__all__


@pytest.mark.parametrize(
    ("body", "extractor", "responsibility"),
    [
        (
            "<h1>English language requirements</h1>"
            "<p>IELTS 7.0 overall with no component below 6.5, Writing 6.5. We also "
            "accept TOEFL iBT with a minimum of 100 overall.</p>",
            language.extract,
            "LANGUAGE_REQUIREMENTS",
        ),
        (
            "<h1>Academic calendar</h1><p>Oct 10, Oct 15 and 3 November 2026.</p>",
            deadline.extract_calendar,
            "ACADEMIC_CALENDAR",
        ),
        (
            "<h1>Tuition fees</h1>"
            "<table><thead><tr><th>Programme</th><th>Annual fee (GBP)</th>"
            "<th>Total fee (GBP)</th></tr></thead>"
            "<tbody><tr><td>MSc Finance</td><td>41,500</td><td>41,500</td></tr>"
            "<tr><td>MSc Computing</td><td>38,000</td><td>38,000</td></tr>"
            "</tbody></table>",
            tuition.extract,
            "TUITION_FEES",
        ),
        (
            "<h1>Courses</h1><h2>MSc Finance</h2><h2>MSc Computer Science</h2>"
            "<h2>PhD Engineering</h2>",
            admission.extract_program,
            "PROGRAM_CATALOG",
        ),
    ],
)
def test_no_extractor_produces_two_claims_with_one_fingerprint(
    body: str, extractor: object, responsibility: str
) -> None:
    """The property both defects violated, asserted directly.

    A duplicate fingerprint is not a visible failure: `ON CONFLICT DO NOTHING` drops
    the second claim and the pass reports success. So the shapes most likely to
    collide -- repeated values in one block, repeated cells in one table, several
    awards in one catalogue -- are each checked.
    """
    doc = document(body)
    candidates = extractor(doc, responsibility=responsibility)  # type: ignore[operator]
    assert candidates, "the fixture produced no claim to check"
    fingerprints = {
        candidate.fingerprint(extraction_id="e", extractor="x", version="1")
        for candidate in candidates
    }
    assert len(fingerprints) == len(candidates), (
        f"{len(candidates) - len(fingerprints)} of {len(candidates)} candidates "
        "would be silently dropped on insert"
    )


# ===========================================================================
# 18, 26, 28. Absence, conflict, and evidence locality
# ===========================================================================


def test_a_page_that_mentions_nothing_produces_no_claim_of_any_kind() -> None:
    """Section 18. The whole battery, on a page about a lake."""
    doc = document("<h1>Visit us</h1><p>The campus has three libraries and a lake.</p>")
    assert tuition.extract(doc, responsibility="TUITION_FEES") == []
    assert language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS") == []
    assert deadline.extract(doc, responsibility="APPLICATION_DEADLINES") == []
    assert admission.extract(doc, responsibility="ENTRY_REQUIREMENTS") == []
    assert admission.extract_program(doc, responsibility="PROGRAM_CATALOG") == []


def test_two_conflicting_statements_stay_two_claims() -> None:
    """Section 26. No winner is chosen here.

    Two fee figures under one heading is a conflict a reviewer resolves. Picking one
    now would hide the disagreement, and picking by confidence would hide it behind a
    number.
    """
    doc = document(
        "<h1>Tuition fees</h1>"
        "<p>The annual tuition fee is £38,000 for international students.</p>"
        "<p>The annual tuition fee is £41,000 for international students.</p>"
    )
    claims = kinds(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    amounts = sorted(
        float(claim.value["amount_min"])  # type: ignore[arg-type]
        for claim in claims
        if claim.value is not None
    )
    assert amounts == [38000.0, 41000.0], "a conflict was resolved instead of recorded"
    assert len({claim.locator.block_index for claim in claims}) == 2


def test_the_same_value_twice_in_one_document_is_two_located_claims() -> None:
    """Identity is the region, not the value: two statements are two pieces of
    evidence even when they agree."""
    doc = document(
        "<h1>Tuition fees</h1>"
        "<p>The annual tuition fee is £38,000.</p>"
        "<p>The annual tuition fee is £38,000.</p>"
    )
    claims = kinds(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert len(claims) == 2
    fingerprints = {
        claim.fingerprint(extraction_id="e", extractor="t", version="1") for claim in claims
    }
    assert len(fingerprints) == 2, "two regions collapsed to one fingerprint"


def test_a_fingerprint_ignores_the_value_and_depends_on_the_region() -> None:
    """Section 5. A rule change that alters a number must produce a second *version*
    of the same claim, not a brand-new one."""
    doc = document("<h1>Tuition fees</h1><p>The annual fee is £38,000.</p>")
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    baseline = claim.fingerprint(extraction_id="e1", extractor="t", version="1")

    from dataclasses import replace

    changed_value = replace(claim, value={"amount_kind": "EXACT", "amount_min": 99.0})
    assert changed_value.fingerprint(extraction_id="e1", extractor="t", version="1") == baseline

    for changed in (
        claim.fingerprint(extraction_id="e2", extractor="t", version="1"),
        claim.fingerprint(extraction_id="e1", extractor="other", version="1"),
        claim.fingerprint(extraction_id="e1", extractor="t", version="2"),
    ):
        assert changed != baseline


def test_extraction_is_deterministic_across_repeated_runs() -> None:
    """Section 5. Re-running must produce the same fingerprints, or idempotency is a
    coincidence rather than a property."""
    doc = document(
        "<h1>English language requirements</h1>"
        "<p>IELTS 7.0 overall with no component below 6.5.</p>"
    )
    first, second = (
        [
            candidate.fingerprint(extraction_id="e", extractor="lang", version="1")
            for candidate in language.extract(doc, responsibility="LANGUAGE_REQUIREMENTS")
        ]
        for _ in range(2)
    )
    assert first == second
    assert len(set(first)) == len(first), "one document produced a duplicate fingerprint"


# ===========================================================================
# 3, 34. Locators resolve
# ===========================================================================


@pytest.mark.parametrize(
    ("body", "extractor", "responsibility"),
    [
        (
            "<h1>Tuition fees</h1><p>The annual tuition fee is £38,000.</p>",
            tuition.extract,
            "TUITION_FEES",
        ),
        (
            "<h1>English language</h1><p>IELTS 7.0 overall required.</p>",
            language.extract,
            "LANGUAGE_REQUIREMENTS",
        ),
        (
            "<h1>Deadlines</h1><p>The deadline is 15 January 2027.</p>",
            deadline.extract,
            "APPLICATION_DEADLINES",
        ),
        (
            "<h1>Entry requirements</h1><p>Applicants must hold a bachelor's degree.</p>",
            admission.extract,
            "ENTRY_REQUIREMENTS",
        ),
        (
            "<h1>Courses</h1><h2>MSc Finance</h2>"
            '<ul><li><a href="/phd-eng">PhD Engineering</a></li></ul>',
            admission.extract_program,
            "PROGRAM_CATALOG",
        ),
        (
            "<h1>Academic calendar</h1><p>Instruction begins 28 September 2026.</p>",
            deadline.extract_calendar,
            "ACADEMIC_CALENDAR",
        ),
    ],
)
def test_every_locator_resolves_to_the_evidence_it_quotes(
    body: str, extractor: object, responsibility: str
) -> None:
    """Section 34. A locator nobody can follow is decoration.

    Resolving it must land on the wording the claim carries -- otherwise the pointer
    and the quote could drift apart and nobody would notice.
    """
    doc = document(body)
    candidates = extractor(doc, responsibility=responsibility)  # type: ignore[operator]
    assert candidates, "the fixture produced no claim to check"
    for candidate in candidates:
        resolved = resolve(doc, candidate.locator.as_json())
        assert resolved is not None, f"{candidate.field_kind} has an unresolvable locator"
        assert candidate.value_raw_text in resolved or resolved == candidate.evidence_text


def test_a_heading_path_is_a_path_and_not_every_preceding_heading() -> None:
    """`h1 Fees` -> `h2 International` -> `h3 2027` is a path; the `h3 2026` before it
    is not part of it."""
    doc = document(
        "<h1>Tuition fees</h1><h2>International</h2><h3>2026 entry</h3>"
        "<p>Old fees.</p><h3>2027 entry</h3><p>The annual tuition fee is £38,000.</p>"
    )
    claim = only(tuition.extract(doc, responsibility="TUITION_FEES"), FieldKind.TUITION)
    assert claim.locator.heading_path == ["Tuition fees", "International", "2027 entry"]


# ===========================================================================
# Step 5C.4: hygiene at the rule level (sections 3, 5, 6, 7, 8)
# ===========================================================================

_CATALOGUE = (
    b"<html><body><main><section><h2>Our courses</h2><ul>"
    b'<li><a href="/a">MSc Computer Science</a></li>'
    b'<li><a href="/b">BA Anglo-Saxon, Norse, and Celtic</a></li>'
    b"</ul></section></main></body></html>"
)

_REQUIREMENTS = (
    b"<html><body><main><section><h1>Entry requirements</h1>"
    b'<p><a href="/intl">International entry requirements</a></p>'
    b"<p>Applicants must hold a recognised bachelor degree with a 2:1 classification.</p>"
    b'<ul><li><a href="/pg">Postgraduate entry requirements</a></li>'
    b"<li>Applicants must supply two academic references before the deadline.</li></ul>"
    b"</section></main></body></html>"
)


def test_a_link_label_is_not_an_admission_requirement() -> None:
    """Section 7. "International entry requirements" is a link *to* the requirements.

    144 LOW rows and 2 MEDIUM ones were exactly this shape.
    """
    doc = parse_html_document(_REQUIREMENTS)
    found = admission.extract(doc, responsibility="ENTRY_REQUIREMENTS")
    texts = [candidate.evidence_text for candidate in found]

    assert "International entry requirements" not in texts
    assert "Postgraduate entry requirements" not in texts
    assert any("recognised bachelor degree" in text for text in texts)
    assert any("two academic references" in text for text in texts)


def test_the_link_only_rule_is_field_specific_so_a_catalogue_still_reads_its_links() -> None:
    """Section 3 forbids solving this by deleting link-only content.

    The same structural fact -- the block is exactly one anchor -- means "navigation"
    on a requirements page and "a programme" in a catalogue. Only the field rule knows
    which.
    """
    doc = parse_html_document(_CATALOGUE)
    programmes = admission.extract_program(doc, responsibility="PROGRAM_CATALOG")
    names = {
        resolved(candidate).get("program_name")
        for candidate in programmes
        if candidate.field_kind is FieldKind.PROGRAM_NAME
    }
    assert "MSc Computer Science" in names
    assert "BA Anglo-Saxon, Norse, and Celtic" in names


def test_site_chrome_produces_no_admission_candidate_even_when_nested() -> None:
    """Section 5: semantic structure, not a CSS class called `nav`."""
    page = (
        b"<html><body><nav><section><h2>Entry requirements</h2>"
        b"<p>Applicants must hold a recognised bachelor degree to apply.</p>"
        b"</section></nav></body></html>"
    )
    doc = parse_html_document(page)
    assert admission.extract(doc, responsibility="ENTRY_REQUIREMENTS") == []


def test_a_programme_name_is_only_read_from_body_content() -> None:
    """Section 6. 60% of the fleet's links are inside navigation, and v4 read them all.

    Cambridge's "Courses for 2027 entry >> Vertical menu >> A" picker is the shape:
    real award names, in a site-wide index, describing no page in particular.
    """
    page = (
        b'<html><body><nav><ul><li><a href="/a">MSc Computer Science</a></li></ul></nav>'
        b'<main><section><ul><li><a href="/b">MSc Data Science</a></li></ul>'
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    names = {
        resolved(candidate).get("program_name")
        for candidate in admission.extract_program(doc, responsibility="PROGRAM_CATALOG")
        if candidate.field_kind is FieldKind.PROGRAM_NAME
    }
    assert names == {"MSc Data Science"}


def test_a_programme_link_needs_a_responsibility_that_authorises_a_catalogue() -> None:
    """Section 6, asserted on the rule rather than left implicit in the routing table."""
    doc = parse_html_document(_CATALOGUE)
    assert admission.extract_program(doc, responsibility="PROGRAM_CATALOG") != []
    assert admission.extract_program(doc, responsibility="UNIVERSITY_HOME") == []


def test_the_degree_level_comes_from_the_wording_not_the_page_title() -> None:
    """Section 7. A page titled "Graduate Admissions" stamped every paragraph on it.

    Including the testimonials -- which is a wrong value on the row, not a recall
    trade-off, so it is fixed rather than reported.
    """
    page = (
        b"<html><body><main><section><h1>Graduate Admissions</h1>"
        b"<h2>Student voices</h2>"
        b"<p>Students from all walks of life come together, and I felt at home here.</p>"
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    found = admission.extract(doc, responsibility="POSTGRADUATE_ADMISSIONS")
    testimonial = next(c for c in found if "all walks of life" in c.evidence_text)

    assert resolved(testimonial)["degree_level_raw"] is None
    assert resolved(testimonial)["degree_level_hint"] is None
    # The page title is still recorded on the locator, where it is provenance rather
    # than an assertion about the level.
    assert testimonial.locator.heading_path[0] == "Graduate Admissions"


def test_requirement_prose_and_qualification_lists_survive_the_hygiene_pass() -> None:
    """Section 7: remove structurally false candidates, not difficult real prose.

    A-level subject lists read like navigation -- short, title-case, no full stop --
    and they are exactly what an entry requirement is made of.
    """
    page = (
        b"<html><body><main><section><h1>Entry requirements</h1>"
        b"<p>We accept the following A-level subjects for entry to this programme:</p>"
        b"<ul><li>English Language and Literature</li>"
        b"<li>Art and Design: Graphic Design</li></ul>"
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    texts = [c.evidence_text for c in admission.extract(doc, responsibility="ENTRY_REQUIREMENTS")]
    assert "English Language and Literature" in texts
    assert "Art and Design: Graphic Design" in texts


def test_the_rules_that_did_not_change_kept_their_version() -> None:
    """Section 8: a version bump says the rule changed, so it must not say anything else.

    Measured over all 175 dual-artifact snapshots, the calendar, language, tuition and
    deadline rules produced byte-identical statements from document 1.0.0 and 2.0.0.
    Bumping them would have marked 472 correct claims superseded in order to record a
    fact about the parser -- which is what `document_artifact_version` records instead.
    """
    assert admission.VERSION == "6", "the requirement rule changed again in Step 5C.5"
    assert admission.PROGRAM_VERSION == "5", "the programme rule changed in Step 5C.4"
    assert deadline.VERSION == "4", "the deadline rule did not change"
    assert deadline.CALENDAR_VERSION == "4", "the calendar rule did not change"
    assert tuition.VERSION == "4", "the tuition rule did not change"
    assert language.VERSION == "6", (
        "the language rule did not change in Step 5C.4; it changed in 5C.5, when "
        "_COORDINATOR was found to contain backspaces where the word boundaries "
        "belonged, so its word half had never run"
    )


# ===========================================================================
# Step 5C.5: a country name is not an applicant jurisdiction (sections 1, 2)
# ===========================================================================

#: The wording that must yield a jurisdiction, and the construction that carries it.
#: The last three are the only true positives the real 175-page corpus contained.
JURISDICTION_ACCEPTS: tuple[tuple[str, str, str], ...] = (
    ("Applicants from China must hold a recognised qualification.", "CN", "APPLICANT_ORIGIN"),
    ("Applicants educated in China are considered on the same basis.", "CN", "APPLICANT_ORIGIN"),
    ("Chinese applicants should submit certified translations.", "CN", "DEMONYM"),
    ("Qualifications obtained in China are assessed individually.", "CN", "QUALIFICATION_ORIGIN"),
    ("If you are from China you will need a transcript.", "CN", "CONDITIONAL_ORIGIN"),
    ("US nationals do not require a visa for this programme.", "US", "DEMONYM"),
    ("Indian students holding the CBSE certificate are eligible.", "IN", "DEMONYM"),
    (
        "Hong Kong Certificate of Education (HKCEE) at grade C or higher",
        "HK",
        "NATIONAL_QUALIFICATION",
    ),
    ("Singapore/Cambridge GCE Ordinary level at grades 1 to 6.", "SG", "NATIONAL_QUALIFICATION"),
    ("Malaysia Sijil Pelejaran (SPM) at grades 1 to 6", "MY", "NATIONAL_QUALIFICATION"),
)

#: Wording that contains a country token and states no applicant jurisdiction. The
#: first four are section 1's examples; the rest are the real candidates the previous
#: rule got wrong, quoted from the corpus.
JURISDICTION_REJECTS: tuple[str, ...] = (
    "This four-year degree programme focuses on Chinese medicine and basic Western "
    "medicine knowledge, with the intention of producing practitioners.",
    "Our campus in Beijing, China welcomes visitors throughout the year.",
    "Please contact the China office for further details.",
    "A Chinese language course is offered to all first-year students.",
    "Four students and four recent alumni will pursue graduate degrees at Schwarzman "
    "College in Beijing, China.",
    "NTU Singapore is embarking on an ambitious effort to transform its undergraduate "
    "education with artificial intelligence.",
    "Send your entire application to:Harvard College Admissions86 Brattle "
    "StreetCambridge, MA 02138 USA",
    "Chicago's urban landscape\u2014with additional locations in Beijing, Delhi, London, "
    "Paris, and Hong Kong\u2014UChicago has helped launch and advance many fields.",
    "Applicants offering DP courses combined with other qualifications (such as "
    "A-levels or USA Aps) are considered on a case-by-case basis.",
    "Please contact us for more information, or study with us next year.",
)


@pytest.mark.parametrize(("sentence", "country", "kind"), JURISDICTION_ACCEPTS)
def test_applicant_semantics_yield_a_jurisdiction(sentence: str, country: str, kind: str) -> None:
    """Section 1. The construction is what asserts the scope, and it is named on the row."""
    found = admission.jurisdictions_in(sentence)
    assert found, f"no jurisdiction found in {sentence!r}"
    assert found[0]["applicant_country_code"] == country
    assert found[0]["match_kind"] == kind
    assert found[0]["dimension"] == "JURISDICTION"
    assert str(found[0]["scope_raw"]) in sentence, "the raw wording must be quotable"


@pytest.mark.parametrize("sentence", JURISDICTION_REJECTS)
def test_a_bare_country_token_asserts_no_jurisdiction(sentence: str) -> None:
    """Section 1, and the reason it is an allowlist rather than a blocklist.

    Nine of the twelve jurisdiction scopes on the real corpus were these: a subject, a
    campus location, a list of campus cities, an institution's own name, a postal
    address and navigation text. Excluding them one string at a time would have left the
    rule still asserting a jurisdiction from a bare token, so the next address walks
    through. A country token with no applicant or qualification semantics asserts
    nothing.
    """
    assert admission.jurisdictions_in(sentence) == []


def test_the_two_scope_dimensions_are_not_collapsed() -> None:
    """Section 2. Where an applicant is from and what they hold are different questions.

    They were previously distinguishable only by `applicant_country_code` being null,
    which is an accident of the data rather than a statement about it.
    """
    sentence = "Chinese applicants offering A-levels are considered for direct entry."
    jurisdictions = admission.jurisdictions_in(sentence)
    qualifications = admission.qualification_systems_in(sentence)

    assert [entry["applicant_country_code"] for entry in jurisdictions] == ["CN"]
    assert [entry["scope_label"] for entry in qualifications] == ["A-level"]
    assert all(entry["dimension"] == "JURISDICTION" for entry in jurisdictions)
    assert all(entry["dimension"] == "QUALIFICATION_SYSTEM" for entry in qualifications)
    # And the combined list is both, in order, so the grouping key and the review-time
    # proposal keep reading one field.
    assert admission.scopes_in(sentence) == jurisdictions + qualifications


def test_a_qualification_system_never_carries_a_country() -> None:
    """A gaokao strongly suggests a Chinese applicant. Suggesting is not extracting."""
    for sentence in (
        "The gaokao is accepted for direct entry.",
        "Applicants offering A-levels need AAB.",
        "We accept the full International Baccalaureate Diploma.",
        "Advanced Placement results are considered.",
    ):
        found = admission.qualification_systems_in(sentence)
        assert found, sentence
        assert all(entry["applicant_country_code"] is None for entry in found)


def test_scope_dimensions_reach_the_candidate_separately() -> None:
    """Section 2, on the stored value rather than on the helper."""
    page = (
        b"<html><body><main><section><h1>Entry requirements</h1>"
        b"<p>Chinese applicants offering A-levels must achieve grades AAB overall.</p>"
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    claim = next(
        c
        for c in admission.extract(doc, responsibility="ENTRY_REQUIREMENTS")
        if "Chinese applicants" in c.evidence_text
    )
    jurisdictions = resolved(claim)["applicant_jurisdictions"]
    qualifications = resolved(claim)["qualification_systems"]
    assert isinstance(jurisdictions, list) and len(jurisdictions) == 1
    assert isinstance(qualifications, list) and len(qualifications) == 1
    assert jurisdictions[0]["applicant_country_code"] == "CN"
    assert qualifications[0]["scope_label"] == "A-level"
    assert claim.unresolved_reason is None, "a stated jurisdiction resolves the scope"


def test_a_qualification_hint_alone_leaves_the_jurisdiction_unresolved() -> None:
    """Section 1: never UNIVERSAL, and never silently 'resolved' either.

    Knowing an applicant holds A-levels does not say where they are from. One flag
    covering both would hide which of the two is missing.
    """
    page = (
        b"<html><body><main><section><h1>Entry requirements</h1>"
        b"<p>Applicants offering A-levels must achieve grades AAB overall to apply.</p>"
        b"<p>Applicants must hold a recognised degree awarded by a university.</p>"
        b"</section></main></body></html>"
    )
    doc = parse_html_document(page)
    found = admission.extract(doc, responsibility="ENTRY_REQUIREMENTS")

    a_level = next(c for c in found if "A-levels" in c.evidence_text)
    assert a_level.unresolved_reason == "APPLICANT_JURISDICTION_UNRESOLVED"
    assert resolved(a_level)["applicant_jurisdictions"] is None

    nothing = next(c for c in found if "recognised degree" in c.evidence_text)
    assert nothing.unresolved_reason == "SCOPE_MAPPING_REQUIRED"
    assert resolved(nothing)["applicant_scopes"] is None


def test_only_the_admission_rule_changed_for_the_scope_fix() -> None:
    """Section 1 says to version only the affected rule.

    The programme rule reads no scopes, and neither do language, tuition, deadline or
    calendar. Bumping them would mark their correct output superseded to record a fact
    about a rule they do not use.
    """
    assert admission.VERSION == "6", "the requirement rule changed"
    assert admission.PROGRAM_VERSION == "5", "the programme rule reads no scopes"
    assert language.VERSION == "6", "corrected in 5C.5; see C61"
    assert tuition.VERSION == "4"
    assert deadline.VERSION == "4"
    assert deadline.CALENDAR_VERSION == "4"
