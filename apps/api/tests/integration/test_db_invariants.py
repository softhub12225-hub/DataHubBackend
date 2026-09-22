"""Database-enforced invariants.

Each test asserts something the *database* refuses, not something the application
checks. That distinction is the whole point: a rule enforced only in Python is a rule
that a migration, a maintenance script or a future endpoint can walk straight past.

Organised by the invariant families in ARCHITECTURE.md section 8.1:

* hierarchy containment via composite foreign keys (C6)
* application-round identity (C10)
* temporal honesty (C9, C13, C14)
* value/status agreement on high-risk facts (D2, D14, D16)
* money completeness (B5)
* version uniqueness (C3)
* identity immutability (B6)
* immutability of history (C1) via trigger, tested here as the owner because
  privileges are covered separately in test_privileges.py
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Connection, text

from tests.integration.conftest import EligibleEvidence, Graph, expect_violation


def _range_text(value: object) -> str:
    """Normalise a psycopg range rendering, which inserts a space after the comma."""
    return str(value).replace(" ", "")


pytestmark = pytest.mark.integration


# ===========================================================================
# 1. Hierarchy containment (C6)
# ===========================================================================


def test_offering_cannot_belong_to_another_universitys_campus(
    conn: Connection, graph: Graph
) -> None:
    """University A's program cannot be taught at university B's campus."""
    with expect_violation(conn, "violates foreign key|program_offering_campus"):
        conn.execute(
            text(
                "INSERT INTO program_offering (id, canonical_id, program_id, university_id, "
                "study_mode, delivery_mode, duration_value, duration_unit, campus_id) "
                "VALUES (:id, 'cross-campus-attempt', :prog, :uni, 'PART_TIME', "
                "'ON_CAMPUS', 2, 'YEAR', :foreign_campus)"
            ),
            {
                "id": uuid.uuid4(),
                "prog": graph["program_a"],
                "uni": graph["university_a"],
                "foreign_campus": graph["campus_b"],
            },
        )


def test_faculty_cannot_be_nested_under_another_universitys_faculty(
    conn: Connection, graph: Graph
) -> None:
    with expect_violation(conn, "violates foreign key|faculty_parent"):
        conn.execute(
            text(
                "INSERT INTO faculty (id, university_id, parent_faculty_id, code, name_en) "
                "VALUES (:id, :uni, :foreign_parent, 'SUB', 'Department of Confusion')"
            ),
            {
                "id": uuid.uuid4(),
                "uni": graph["university_a"],
                "foreign_parent": graph["faculty_b"],
            },
        )


def test_faculty_cannot_be_its_own_parent(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "not_own_parent"):
        conn.execute(
            text("UPDATE faculty SET parent_faculty_id = id WHERE id = :id"),
            {"id": graph["faculty_a"]},
        )


def test_requirement_cannot_point_at_an_offering_of_another_program(
    conn: Connection, graph: Graph
) -> None:
    """The composite FK proves offering-belongs-to-program."""
    with expect_violation(conn, "violates foreign key"):
        conn.execute(
            text(
                "INSERT INTO admission_requirement (id, program_id, offering_id, "
                "applicant_scope_id, requirement_kind) "
                "VALUES (:id, :prog_a, :off_b, :scope, 'ACADEMIC')"
            ),
            {
                "id": uuid.uuid4(),
                "prog_a": graph["program_a"],
                "off_b": graph["offering_b"],
                "scope": graph["universal_scope"],
            },
        )


def test_requirement_cannot_point_at_an_intake_of_another_offering(
    conn: Connection, graph: Graph
) -> None:
    """The second composite FK proves intake-belongs-to-offering."""
    with expect_violation(conn, "violates foreign key"):
        conn.execute(
            text(
                "INSERT INTO admission_requirement (id, program_id, offering_id, intake_id, "
                "applicant_scope_id, requirement_kind) "
                "VALUES (:id, :prog_a, :off_a, :intake_b, :scope, 'ACADEMIC')"
            ),
            {
                "id": uuid.uuid4(),
                "prog_a": graph["program_a"],
                "off_a": graph["offering_a"],
                "intake_b": graph["intake_b"],
                "scope": graph["universal_scope"],
            },
        )


def test_requirement_cannot_name_an_intake_without_an_offering(
    conn: Connection, graph: Graph
) -> None:
    """The impossible row in the C6 grain table."""
    with expect_violation(conn, "intake_requires_offering|violates foreign key"):
        conn.execute(
            text(
                "INSERT INTO admission_requirement (id, program_id, intake_id, "
                "applicant_scope_id, requirement_kind) "
                "VALUES (:id, :prog, :intake, :scope, 'ACADEMIC')"
            ),
            {
                "id": uuid.uuid4(),
                "prog": graph["program_a"],
                "intake": graph["intake_a"],
                "scope": graph["universal_scope"],
            },
        )


@pytest.mark.parametrize(
    ("offering_key", "intake_key", "expected_grain"),
    [
        (None, None, "PROGRAM"),
        ("offering_a", None, "OFFERING"),
        ("offering_a", "intake_a", "INTAKE"),
    ],
)
def test_requirement_grain_is_derived_from_the_ownership_chain(
    conn: Connection,
    graph: Graph,
    offering_key: str | None,
    intake_key: str | None,
    expected_grain: str,
) -> None:
    """All three valid levels work, and `grain` is computed, never supplied."""
    requirement_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO admission_requirement (id, program_id, offering_id, intake_id, "
            "applicant_scope_id, requirement_kind) "
            "VALUES (:id, :prog, :off, :intake, :scope, 'ACADEMIC')"
        ),
        {
            "id": requirement_id,
            "prog": graph["program_a"],
            "off": graph[offering_key] if offering_key else None,
            "intake": graph[intake_key] if intake_key else None,
            "scope": graph["universal_scope"],
        },
    )
    grain = conn.execute(
        text("SELECT grain FROM admission_requirement WHERE id = :id"), {"id": requirement_id}
    ).scalar_one()
    assert grain == expected_grain


# ===========================================================================
# 2. Application-round identity (C10)
# ===========================================================================


def test_a_controlled_round_code_occurs_once_per_intake(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "uq_application_round_intake_code|duplicate key"):
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code, round_label) "
                "VALUES (:id, :intake, 'ROUND_1', 'Round One (again)')"
            ),
            {"id": uuid.uuid4(), "intake": graph["intake_a"]},
        )


def test_several_institution_defined_rounds_with_different_labels_are_allowed(
    conn: Connection, graph: Graph
) -> None:
    """The case the previous key forbade, and the reason C10 exists."""
    for label in ("Scholarship consideration round", "Late applications window", "Clearing top-up"):
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code, round_label) "
                "VALUES (:id, :intake, 'INSTITUTION_DEFINED', :label)"
            ),
            {"id": uuid.uuid4(), "intake": graph["intake_a"], "label": label},
        )
    count = conn.execute(
        text(
            "SELECT count(*) FROM application_round "
            "WHERE intake_id = :intake AND round_code = 'INSTITUTION_DEFINED'"
        ),
        {"intake": graph["intake_a"]},
    ).scalar_one()
    assert count == 3


def test_institution_defined_rounds_are_deduplicated_by_normalised_label(
    conn: Connection, graph: Graph
) -> None:
    """Whitespace and case must not create a second copy of the same round."""
    conn.execute(
        text(
            "INSERT INTO application_round (id, intake_id, round_code, round_label) "
            "VALUES (:id, :intake, 'INSTITUTION_DEFINED', 'Scholarship Round')"
        ),
        {"id": uuid.uuid4(), "intake": graph["intake_a"]},
    )
    with expect_violation(conn, "uq_application_round_intake_label|duplicate key"):
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code, round_label) "
                "VALUES (:id, :intake, 'INSTITUTION_DEFINED', '  scholarship   ROUND  ')"
            ),
            {"id": uuid.uuid4(), "intake": graph["intake_a"]},
        )


def test_an_institution_defined_round_must_carry_a_label(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "institution_defined_requires_label"):
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code) "
                "VALUES (:id, :intake, 'INSTITUTION_DEFINED')"
            ),
            {"id": uuid.uuid4(), "intake": graph["intake_a"]},
        )


def test_sequence_no_does_not_define_identity(conn: Connection, graph: Graph) -> None:
    """Two rounds may share a display position; that is not a collision (C10)."""
    for code, label in (("ROUND_2", "Round 2"), ("PRIORITY", "Priority")):
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code, round_label, "
                "sequence_no) VALUES (:id, :intake, :code, :label, 5)"
            ),
            {"id": uuid.uuid4(), "intake": graph["intake_a"], "code": code, "label": label},
        )
    count = conn.execute(
        text("SELECT count(*) FROM application_round WHERE intake_id = :i AND sequence_no = 5"),
        {"i": graph["intake_a"]},
    ).scalar_one()
    assert count == 2


# ===========================================================================
# 3. Temporal honesty (C9, C13, C14)
# ===========================================================================


def _insert_deadline(conn: Connection, graph: Graph, **columns: object) -> uuid.UUID:
    deadline_id = uuid.uuid4()
    base: dict[str, object] = {
        "id": deadline_id,
        "round_id": graph["round_a"],
        "applicant_scope_id": graph["universal_scope"],
    }
    base.update(columns)
    names = ", ".join(base)
    values = ", ".join(f":{name}" for name in base)
    conn.execute(text(f"INSERT INTO application_deadline ({names}) VALUES ({values})"), base)
    return deadline_id


def test_an_exact_instant_requires_a_stated_time_and_timezone(
    conn: Connection, graph: Graph
) -> None:
    """The constraint that makes "no invention" a database guarantee (C9)."""
    with expect_violation(conn, "deadline_instant_requires_time_and_zone"):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="PUBLISHED",
            deadline_kind="FIXED_DATE",
            deadline_year=2027,
            deadline_month=1,
            deadline_day=15,
            deadline_instant_utc="2027-01-15 23:59:00+00",
            deadline_text="15 January 2027",
        )


def test_a_full_datetime_fact_may_carry_an_exact_instant(conn: Connection, graph: Graph) -> None:
    """When the source really did state date + time + zone, the instant is allowed."""
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="FIXED_DATE",
        deadline_year=2027,
        deadline_month=1,
        deadline_day=15,
        deadline_time="23:59:00",
        deadline_timezone="Europe/London",
        deadline_instant_utc="2027-01-15 23:59:00+00",
        deadline_text="15 January 2027, 23:59 GMT",
    )
    row = conn.execute(
        text(
            "SELECT deadline_precision, deadline_instant_utc IS NOT NULL, deadline_cal_range "
            "FROM application_deadline WHERE id = :id"
        ),
        {"id": deadline_id},
    ).one()
    assert row[0] == "DATETIME"
    assert row[1] is True
    assert _range_text(row[2]) == "[2027-01-15,2027-01-16)"


def test_a_date_only_fact_gets_no_instant_and_a_one_day_range(
    conn: Connection, graph: Graph
) -> None:
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="FIXED_DATE",
        deadline_year=2027,
        deadline_month=1,
        deadline_day=15,
        deadline_text="15 January 2027",
    )
    precision, instant, cal_range = conn.execute(
        text(
            "SELECT deadline_precision, deadline_instant_utc, deadline_cal_range "
            "FROM application_deadline WHERE id = :id"
        ),
        {"id": deadline_id},
    ).one()
    assert precision == "DATE"
    assert instant is None, "a date-only fact must not acquire an invented instant"
    assert _range_text(cal_range) == "[2027-01-15,2027-01-16)"


def test_a_month_part_fact_covers_the_whole_month(conn: Connection, graph: Graph) -> None:
    """C14: early/mid/late must not narrow the range -- that is a product policy."""
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="FIXED_DATE",
        deadline_year=2027,
        deadline_month=1,
        deadline_month_part="MID",
        deadline_text="mid-January",
    )
    precision, cal_range, month_part = conn.execute(
        text(
            "SELECT deadline_precision, deadline_cal_range, deadline_month_part "
            "FROM application_deadline WHERE id = :id"
        ),
        {"id": deadline_id},
    ).one()
    assert precision == "MONTH_PART"
    assert _range_text(cal_range) == "[2027-01-01,2027-02-01)", "must not narrow to days 11-20"
    assert month_part == "MID", "month_part is preserved as structured metadata"


def test_a_december_month_fact_rolls_into_the_next_year(conn: Connection, graph: Graph) -> None:
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="FIXED_DATE",
        deadline_year=2027,
        deadline_month=12,
        deadline_text="December 2027",
    )
    cal_range = conn.execute(
        text("SELECT deadline_cal_range FROM application_deadline WHERE id = :id"),
        {"id": deadline_id},
    ).scalar_one()
    assert _range_text(cal_range) == "[2027-12-01,2028-01-01)"


@pytest.mark.parametrize("kind", ["ROLLING", "UNTIL_FILLED", "NO_FIXED_DEADLINE"])
def test_open_ended_deadlines_publish_without_any_date(
    conn: Connection, graph: Graph, kind: str
) -> None:
    """ "rolling" is a publishable answer, not missing data."""
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind=kind,
        deadline_text=f"Applications are considered on a {kind.lower()} basis",
    )
    precision, cal_range = conn.execute(
        text(
            "SELECT deadline_precision, deadline_cal_range "
            "FROM application_deadline WHERE id = :id"
        ),
        {"id": deadline_id},
    ).one()
    assert precision is None
    assert cal_range is None


def test_a_rolling_deadline_may_still_carry_a_final_cutoff(conn: Connection, graph: Graph) -> None:
    """Institutions publish "rolling, final cut-off 30 June" -- both must be storable."""
    deadline_id = _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="ROLLING",
        deadline_year=2027,
        deadline_month=6,
        deadline_day=30,
        deadline_text="Rolling, final cut-off 30 June 2027",
    )
    assert (
        conn.execute(
            text("SELECT deadline_cal_range FROM application_deadline WHERE id = :id"),
            {"id": deadline_id},
        ).scalar_one()
        is not None
    )


def test_no_fixed_deadline_cannot_carry_a_date(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "date_matches_kind"):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="PUBLISHED",
            deadline_kind="NO_FIXED_DEADLINE",
            deadline_year=2027,
            deadline_text="No fixed deadline",
        )


def test_a_fixed_date_deadline_must_carry_a_date(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "date_matches_kind"):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="PUBLISHED",
            deadline_kind="FIXED_DATE",
            deadline_text="There is a deadline but we did not record it",
        )


def test_a_published_deadline_must_retain_the_official_wording(
    conn: Connection, graph: Graph
) -> None:
    with expect_violation(conn, "published_requires_official_text"):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="PUBLISHED",
            deadline_kind="FIXED_DATE",
            deadline_year=2027,
            deadline_month=1,
            deadline_day=15,
        )


def test_an_unpublished_deadline_cannot_carry_a_date(conn: Connection, graph: Graph) -> None:
    """ "The page publishes no deadline" and "the deadline is January" are different."""
    with expect_violation(conn, "unpublished_has_no_date|kind_present_iff_published"):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="OFFICIALLY_NOT_PUBLISHED",
            deadline_year=2027,
            deadline_month=1,
        )


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ({"deadline_month": 1}, "month_requires_year"),
        ({"deadline_year": 2027, "deadline_day": 15}, "day_requires_month"),
        (
            {"deadline_year": 2027, "deadline_month": 1, "deadline_time": "23:59:00"},
            "time_requires_day",
        ),
        (
            {"deadline_year": 2027, "deadline_month": 1, "deadline_timezone": "Europe/London"},
            "timezone_requires_time",
        ),
        (
            {
                "deadline_year": 2027,
                "deadline_month": 1,
                "deadline_day": 15,
                "deadline_month_part": "MID",
            },
            "month_part_excludes_day",
        ),
        ({"deadline_year": 2027, "deadline_month": 13}, "month_range"),
        (
            {"deadline_year": 2027, "deadline_month": 2, "deadline_day": 30},
            # make_date inside the generated column raises before the named CHECK is
            # reached; the row is refused either way.
            "out of range|day_is_a_real_date",
        ),
    ],
)
def test_incoherent_calendar_parts_are_rejected(
    conn: Connection, graph: Graph, columns: dict[str, object], expected: str
) -> None:
    """Calendar parts degrade in one direction only, and must denote a real date."""
    # ROLLING rather than FIXED_DATE: FIXED_DATE requires a year, so its constraint
    # would fire first and mask the part-coherence rule under test.
    with expect_violation(conn, expected):
        _insert_deadline(
            conn,
            graph,
            deadline_field_status="PUBLISHED",
            deadline_kind="ROLLING",
            deadline_text="probe",
            **columns,
        )


def test_the_calendar_range_supports_an_overlap_filter(conn: Connection, graph: Graph) -> None:
    """The reason cal_range exists: filtering across mixed precision (C13)."""
    _insert_deadline(
        conn,
        graph,
        deadline_field_status="PUBLISHED",
        deadline_kind="FIXED_DATE",
        deadline_year=2027,
        deadline_month=1,
        deadline_text="January 2027",
    )
    matched = conn.execute(
        text(
            "SELECT count(*) FROM application_deadline "
            "WHERE round_id = :r AND deadline_cal_range && daterange('2027-01-10','2027-01-20')"
        ),
        {"r": graph["round_a"]},
    ).scalar_one()
    assert matched == 1, "an imprecise January fact must match a mid-January filter"


# ===========================================================================
# 4. Money completeness (B5)
# ===========================================================================


def _insert_tuition(conn: Connection, graph: Graph, **columns: object) -> None:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "offering_id": graph["offering_a"],
        "academic_year": "2027/28",
        "student_category_id": graph["student_category_intl"],
    }
    base.update(columns)
    names = ", ".join(base)
    values = ", ".join(f":{name}" for name in base)
    conn.execute(text(f"INSERT INTO tuition ({names}) VALUES ({values})"), base)


def test_a_fee_without_a_currency_is_rejected(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "amounts_require_currency_and_billing_unit"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="EXACT",
            amount_min=32000,
            amount_max=32000,
            billing_unit_code="PER_YEAR",
        )


def test_a_fee_without_a_billing_unit_is_rejected(conn: Connection, graph: Graph) -> None:
    """32000 GBP is not a fee until you know whether it is per year or in total."""
    with expect_violation(conn, "amounts_require_currency_and_billing_unit"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="EXACT",
            amount_min=32000,
            amount_max=32000,
            currency_code="GBP",
        )


def test_a_complete_fee_is_accepted(conn: Connection, graph: Graph) -> None:
    _insert_tuition(
        conn,
        graph,
        amount_field_status="PUBLISHED",
        amount_kind="EXACT",
        amount_min=32000,
        amount_max=32000,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        official_text="GBP 32,000 per year",
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM tuition WHERE offering_id = :o"),
            {"o": graph["offering_a"]},
        ).scalar_one()
        == 1
    )


def test_an_unpublished_fee_must_not_carry_an_amount(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "amount_kind_matches_field_status"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="OFFICIALLY_NOT_PUBLISHED",
            amount_kind="EXACT",
            amount_min=32000,
            amount_max=32000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_officially_unpublished_fee_is_representable(conn: Connection, graph: Graph) -> None:
    """The 官网未发布 case: a row that records the absence, with no amount (D2)."""
    _insert_tuition(
        conn,
        graph,
        amount_field_status="OFFICIALLY_NOT_PUBLISHED",
        official_url="https://example.test/fees",
        official_text="Fees for this programme are not yet published.",
    )
    status = conn.execute(
        text("SELECT amount_field_status FROM tuition WHERE offering_id = :o"),
        {"o": graph["offering_a"]},
    ).scalar_one()
    assert status == "OFFICIALLY_NOT_PUBLISHED"


# ===========================================================================
# 4b. Fee shape (U14)
#
# The rule underneath all of these: the database stores what the page published and
# nothing else. A range keeps both ends because a midpoint is a figure no university
# stated, and "fees vary" keeps its wording because the wording is the whole fact.
# ===========================================================================


def test_a_published_range_keeps_both_ends(conn: Connection, graph: Graph) -> None:
    """The case the single-`amount` column could not hold at all."""
    _insert_tuition(
        conn,
        graph,
        amount_field_status="PUBLISHED",
        amount_kind="RANGE",
        amount_min=28000,
        amount_max=32000,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        official_text="GBP 28,000-32,000 depending on pathway",
    )
    low, high = conn.execute(
        text("SELECT amount_min, amount_max FROM tuition WHERE offering_id = :o"),
        {"o": graph["offering_a"]},
    ).one()
    assert (low, high) == (Decimal("28000.00"), Decimal("32000.00"))
    # And nothing anywhere stored 30000: the midpoint is not a published fact.
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM tuition WHERE offering_id = :o "
                "AND (amount_min = 30000 OR amount_max = 30000)"
            ),
            {"o": graph["offering_a"]},
        ).scalar_one()
        == 0
    )


def test_an_exact_fee_must_have_equal_endpoints(conn: Connection, graph: Graph) -> None:
    """EXACT means the page gave one figure. Two figures is a RANGE."""
    with expect_violation(conn, "exact_has_equal_endpoints"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="EXACT",
            amount_min=28000,
            amount_max=32000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_a_range_must_be_ordered(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "range_is_ordered"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="RANGE",
            amount_min=32000,
            amount_max=28000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_a_range_needs_both_ends(conn: Connection, graph: Graph) -> None:
    """A RANGE with one end is a FROM or an UP_TO, and must say which."""
    with expect_violation(conn, "range_is_ordered"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="RANGE",
            amount_min=28000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_from_carries_a_floor_and_no_ceiling(conn: Connection, graph: Graph) -> None:
    """'from GBP 24,500' publishes a floor. Inventing a ceiling would publish fiction."""
    _insert_tuition(
        conn,
        graph,
        amount_field_status="PUBLISHED",
        amount_kind="FROM",
        amount_min=24500,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        official_text="Fees start from GBP 24,500",
    )
    with expect_violation(conn, "from_has_only_a_minimum"):
        _insert_tuition(
            conn,
            graph,
            academic_year="2028/29",
            amount_field_status="PUBLISHED",
            amount_kind="FROM",
            amount_min=24500,
            amount_max=30000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_up_to_carries_a_ceiling_and_no_floor(conn: Connection, graph: Graph) -> None:
    _insert_tuition(
        conn,
        graph,
        amount_field_status="PUBLISHED",
        amount_kind="UP_TO",
        amount_max=9250,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        official_text="Up to GBP 9,250",
    )
    with expect_violation(conn, "up_to_has_only_a_maximum"):
        _insert_tuition(
            conn,
            graph,
            academic_year="2028/29",
            amount_field_status="PUBLISHED",
            amount_kind="UP_TO",
            amount_min=5000,
            amount_max=9250,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_variable_fees_are_published_and_must_quote_the_page(
    conn: Connection, graph: Graph
) -> None:
    """VARIABLE is a published fee with no figure, so the wording IS the fact.

    This is the case that used to force a collector to choose between inventing a
    number and recording `OFFICIALLY_NOT_PUBLISHED` about a page that plainly does
    publish something.
    """
    _insert_tuition(
        conn,
        graph,
        amount_field_status="PUBLISHED",
        amount_kind="VARIABLE",
        official_text="Fees vary by module selection; see the fee calculator.",
    )
    with expect_violation(conn, "variable_states_its_wording"):
        _insert_tuition(
            conn,
            graph,
            academic_year="2028/29",
            amount_field_status="PUBLISHED",
            amount_kind="VARIABLE",
        )


def test_amount_kind_is_not_a_field_status(conn: Connection, graph: Graph) -> None:
    """`OFFICIALLY_NOT_PUBLISHED` is a status, and there is no kind for it.

    Confusing the two is the mistake that would let "the page says nothing about
    fees" and "the page says fees vary" collapse into one row shape.
    """
    with expect_violation(conn, "invalid input value for enum tuition_amount_kind"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="OFFICIALLY_NOT_PUBLISHED",
        )


def test_amounts_cannot_float_free_of_a_kind(conn: Connection, graph: Graph) -> None:
    """A figure with no stated shape is a number of unknown meaning."""
    with expect_violation(conn, "no_amounts_without_a_kind|amount_kind_matches_field_status"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="NOT_CHECKED",
            amount_min=32000,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


def test_a_negative_fee_is_rejected(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "amounts_non_negative"):
        _insert_tuition(
            conn,
            graph,
            amount_field_status="PUBLISHED",
            amount_kind="EXACT",
            amount_min=-1,
            amount_max=-1,
            currency_code="GBP",
            billing_unit_code="PER_YEAR",
        )


# ===========================================================================
# 5. Version uniqueness (C3)
# ===========================================================================


def test_a_root_version_number_cannot_repeat(conn: Connection, graph: Graph) -> None:
    for _ in range(1):
        conn.execute(
            text(
                "INSERT INTO entity_version (id, root_type, root_id, version_no, "
                "state_snapshot, published_at) "
                "VALUES (:id, 'program', :root, 1, '{}'::jsonb, now())"
            ),
            {"id": uuid.uuid4(), "root": graph["program_a"]},
        )
    with expect_violation(conn, "uq_entity_version_root_version_no|duplicate key"):
        conn.execute(
            text(
                "INSERT INTO entity_version (id, root_type, root_id, version_no, "
                "state_snapshot, published_at) "
                "VALUES (:id, 'program', :root, 1, '{}'::jsonb, now())"
            ),
            {"id": uuid.uuid4(), "root": graph["program_a"]},
        )


def test_version_numbers_may_have_gaps(conn: Connection, graph: Graph) -> None:
    """Gaplessness is explicitly NOT required (C3)."""
    for version_no in (1, 2, 7):
        conn.execute(
            text(
                "INSERT INTO entity_version (id, root_type, root_id, version_no, "
                "state_snapshot, published_at) "
                "VALUES (:id, 'program', :root, :v, '{}'::jsonb, now())"
            ),
            {"id": uuid.uuid4(), "root": graph["program_a"], "v": version_no},
        )
    assert (
        conn.execute(
            text("SELECT count(*) FROM entity_version WHERE root_id = :r"),
            {"r": graph["program_a"]},
        ).scalar_one()
        == 3
    )


def test_high_risk_provenance_with_no_evidence_at_all_is_refused(
    conn: Connection, graph: Graph
) -> None:
    """Invariant I3. Since C27 two mechanisms refuse this, and the trigger wins.

    A BEFORE INSERT trigger runs before CHECK constraints, so
    `field_provenance_requires_eligible_evidence` reports first. Both refuse the row;
    this asserts on the message an operator would actually see. The CHECK itself is
    exercised separately below, where evidence *is* cited but the reviewer is not.
    """
    with expect_violation(conn, "resolves to no source"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (id, entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "published_at) VALUES (:id, 'application_deadline', :e, 'deadline_at', "
                "'program', :root, 1, 'PUBLISHED', '\"2027-01-15\"'::jsonb, 'HIGH', now())"
            ),
            {"id": uuid.uuid4(), "e": uuid.uuid4(), "root": graph["program_a"]},
        )


def test_high_risk_provenance_with_evidence_still_requires_a_reviewer(
    conn: Connection, graph: Graph, eligible_evidence: EligibleEvidence
) -> None:
    """The I3 CHECK, reached by citing eligible evidence and omitting the reviewer.

    This is the case the trigger no longer masks, and it is the one that matters:
    evidence alone does not authorise a high-risk publication.
    """
    with expect_violation(conn, "high_risk_requires_evidence_and_review"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (id, entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "source_id, snapshot_id, published_at) "
                "VALUES (:id, 'application_deadline', :e, 'deadline_at', "
                "'program', :root, 1, 'PUBLISHED', '\"2027-01-15\"'::jsonb, 'HIGH', "
                ":source, :snap, now())"
            ),
            {
                "id": uuid.uuid4(),
                "e": uuid.uuid4(),
                "root": graph["program_a"],
                "source": eligible_evidence.source,
                "snap": eligible_evidence.snapshot,
            },
        )


def test_provenance_value_must_agree_with_its_status(
    conn: Connection, graph: Graph, eligible_evidence: EligibleEvidence
) -> None:
    """Evidence is cited, so the value/status biconditional is what refuses this."""
    with expect_violation(conn, "value_matches_field_status"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (id, entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "source_id, snapshot_id, published_at) "
                "VALUES (:id, 'program', :e, 'name_en', 'program', :root, 1, "
                "'OFFICIALLY_NOT_PUBLISHED', '\"a value\"'::jsonb, 'LOW', "
                ":source, :snap, now())"
            ),
            {
                "id": uuid.uuid4(),
                "e": uuid.uuid4(),
                "root": graph["program_a"],
                "source": eligible_evidence.source,
                "snap": eligible_evidence.snapshot,
            },
        )


# ===========================================================================
# 6. Identity immutability (B6)
# ===========================================================================


@pytest.mark.parametrize(
    ("table", "key"), [("university", "university_a"), ("program", "program_a")]
)
def test_canonical_id_cannot_change(conn: Connection, graph: Graph, table: str, key: str) -> None:
    """A rename belongs in entity_alias; the id is an external contract."""
    with expect_violation(conn, "canonical_id is immutable"):
        conn.execute(
            text(f"UPDATE {table} SET canonical_id = 'renamed-entity' WHERE id = :id"),
            {"id": graph[key]},
        )


def test_a_rename_is_recorded_as_an_alias_without_touching_the_id(
    conn: Connection, graph: Graph
) -> None:
    original = conn.execute(
        text("SELECT canonical_id FROM university WHERE id = :id"), {"id": graph["university_a"]}
    ).scalar_one()

    conn.execute(
        text("UPDATE university SET name_en = 'Test University A (renamed)' WHERE id = :id"),
        {"id": graph["university_a"]},
    )
    conn.execute(
        text(
            "INSERT INTO entity_alias (id, entity_type, entity_id, alias_kind, value) "
            "VALUES (:id, 'university', :e, 'FORMER_NAME', 'Test University A')"
        ),
        {"id": uuid.uuid4(), "e": graph["university_a"]},
    )

    after = conn.execute(
        text("SELECT canonical_id FROM university WHERE id = :id"), {"id": graph["university_a"]}
    ).scalar_one()
    assert after == original, "a rename must not change canonical_id"


def test_an_entity_cannot_supersede_itself(conn: Connection, graph: Graph) -> None:
    with expect_violation(conn, "no_self_relationship"):
        conn.execute(
            text(
                "INSERT INTO entity_relationship (id, entity_type, from_entity_id, "
                "to_entity_id, relationship_kind, effective_from) "
                "VALUES (:id, 'university', :e, :e, 'MERGED_INTO', '2027-01-01')"
            ),
            {"id": uuid.uuid4(), "e": graph["university_a"]},
        )


@pytest.mark.parametrize("kind", ["SUPERSEDED_BY", "MERGED_INTO", "SPLIT_INTO"])
def test_all_three_supersession_kinds_are_supported(
    conn: Connection, graph: Graph, kind: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO entity_relationship (id, entity_type, from_entity_id, "
            "to_entity_id, relationship_kind, effective_from) "
            "VALUES (:id, 'university', :a, :b, :kind, '2027-01-01')"
        ),
        {
            "id": uuid.uuid4(),
            "a": graph["university_a"],
            "b": graph["university_b"],
            "kind": kind,
        },
    )


# ===========================================================================
# 7. Immutability of history (C1) -- trigger layer
# ===========================================================================


def test_immutable_history_cannot_be_updated(conn: Connection, graph: Graph) -> None:
    """Tested as the OWNER: privileges are covered separately, this is the trigger."""
    conn.execute(
        text(
            "INSERT INTO entity_version (id, root_type, root_id, version_no, "
            "state_snapshot, published_at) "
            "VALUES (:id, 'program', :root, 1, '{}'::jsonb, now())"
        ),
        {"id": uuid.uuid4(), "root": graph["program_a"]},
    )
    with expect_violation(conn, "append-only history"):
        conn.execute(
            text("UPDATE entity_version SET version_no = 99 WHERE root_id = :r"),
            {"r": graph["program_a"]},
        )


def test_immutable_history_cannot_be_deleted(conn: Connection, graph: Graph) -> None:
    conn.execute(
        text(
            "INSERT INTO entity_version (id, root_type, root_id, version_no, "
            "state_snapshot, published_at) "
            "VALUES (:id, 'program', :root, 1, '{}'::jsonb, now())"
        ),
        {"id": uuid.uuid4(), "root": graph["program_a"]},
    )
    with expect_violation(conn, "append-only history"):
        conn.execute(
            text("DELETE FROM entity_version WHERE root_id = :r"), {"r": graph["program_a"]}
        )


def test_the_audit_log_cannot_be_rewritten(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (id, actor_type, action, object_type) "
            "VALUES (:id, 'SYSTEM', 'test.action', 'program')"
        ),
        {"id": uuid.uuid4()},
    )
    with expect_violation(conn, "append-only history"):
        conn.execute(text("UPDATE audit_log SET action = 'tampered'"))


def test_the_audit_log_hash_chain_is_computed_by_the_database(conn: Connection) -> None:
    """A caller cannot forge the chain, because it does not supply it."""
    first, second = uuid.uuid4(), uuid.uuid4()
    for entry_id in (first, second):
        conn.execute(
            text(
                "INSERT INTO audit_log (id, actor_type, action, object_type, row_hash) "
                "VALUES (:id, 'SYSTEM', 'test.action', 'program', 'forged-value')"
            ),
            {"id": entry_id},
        )
    rows = conn.execute(
        text("SELECT id, prev_hash, row_hash FROM audit_log ORDER BY occurred_at, id")
    ).all()
    hashes = {row[0]: (row[1], row[2]) for row in rows}
    assert hashes[first][1] != "forged-value", "the trigger must overwrite a supplied hash"
    assert len(hashes[first][1]) == 64
    later = hashes[second]
    assert later[0] is not None, "the second entry must link to the first"


def test_controlled_maintenance_can_bypass_the_trigger(conn: Connection, graph: Graph) -> None:
    """The documented escape hatch, so schema maintenance stays possible.

    Deliberately available only to a role that already holds UPDATE -- runtime roles
    do not, so this is the owner's tool, not an application back door.
    """
    conn.execute(
        text(
            "INSERT INTO entity_version (id, root_type, root_id, version_no, "
            "state_snapshot, published_at) "
            "VALUES (:id, 'program', :root, 1, '{}'::jsonb, now())"
        ),
        {"id": uuid.uuid4(), "root": graph["program_a"]},
    )
    conn.execute(text("SET LOCAL app.allow_history_maintenance = 'on'"))
    conn.execute(
        text("UPDATE entity_version SET diff_summary = '{}'::jsonb WHERE root_id = :r"),
        {"r": graph["program_a"]},
    )
    conn.execute(text("SET LOCAL app.allow_history_maintenance = 'off'"))
    with expect_violation(conn, "append-only history"):
        conn.execute(
            text("UPDATE entity_version SET diff_summary = NULL WHERE root_id = :r"),
            {"r": graph["program_a"]},
        )


# ===========================================================================
# 8. Applicant scopes (B1) -- extensible dimensions
# ===========================================================================


def test_a_new_scope_dimension_needs_no_migration(conn: Connection) -> None:
    """The extensibility B1 requires: adding a dimension is an INSERT."""
    conn.execute(
        text(
            "INSERT INTO scope_dimension (code, name_en, description) "
            "VALUES ('sponsor_status', 'Sponsorship status', "
            "'Whether the applicant is government-sponsored')"
        )
    )
    scope_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO applicant_scope (id, code, name_en, precedence) "
            "VALUES (:id, 'SPONSORED_APPLICANTS', 'Government-sponsored applicants', 10)"
        ),
        {"id": scope_id},
    )
    conn.execute(
        text(
            "INSERT INTO applicant_scope_criterion (id, scope_id, dimension_code, operator, "
            "value) VALUES (:id, :scope, 'sponsor_status', 'EQUALS', 'GOVERNMENT')"
        ),
        {"id": uuid.uuid4(), "scope": scope_id},
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM applicant_scope_criterion WHERE scope_id = :s"),
            {"s": scope_id},
        ).scalar_one()
        == 1
    )


def test_a_criterion_carries_a_value_or_a_group_but_not_both(conn: Connection) -> None:
    scope_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO applicant_scope (id, code, name_en) "
            "VALUES (:id, 'PROBE_SCOPE', 'Probe scope')"
        ),
        {"id": scope_id},
    )
    group_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO qualification_group (id, code, name_en) "
            "VALUES (:id, 'PROBE_GROUP', 'Probe institution list')"
        ),
        {"id": group_id},
    )
    with expect_violation(conn, "exactly_one_of_value_or_ref"):
        conn.execute(
            text(
                "INSERT INTO applicant_scope_criterion (id, scope_id, dimension_code, "
                "operator, value, value_ref) VALUES (:id, :scope, 'qualification_group', "
                "'IN_GROUP', 'CN', :group)"
            ),
            {"id": uuid.uuid4(), "scope": scope_id, "group": group_id},
        )
    with expect_violation(conn, "exactly_one_of_value_or_ref"):
        conn.execute(
            text(
                "INSERT INTO applicant_scope_criterion (id, scope_id, dimension_code, "
                "operator) VALUES (:id, :scope, 'qualification_group', 'IN_GROUP')"
            ),
            {"id": uuid.uuid4(), "scope": scope_id},
        )


def test_an_institution_tier_list_is_ordinary_data(conn: Connection, graph: Graph) -> None:
    """No jurisdiction's rules are in the schema: a tier list is source-published data."""
    group_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO qualification_group (id, code, name_en, "
            "published_by_university_id, description) VALUES (:id, 'TEST_UNI_A_TIER_1', "
            "'Test University A recognised institution list', :uni, "
            "'A list the institution itself publishes')"
        ),
        {"id": group_id, "uni": graph["university_a"]},
    )
    conn.execute(
        text(
            "INSERT INTO qualification_group_member (id, group_id, member_university_id) "
            "VALUES (:id, :group, :member)"
        ),
        {"id": uuid.uuid4(), "group": group_id, "member": graph["university_b"]},
    )
    conn.execute(
        text(
            "INSERT INTO qualification_group_member (id, group_id, member_label) "
            "VALUES (:id, :group, 'An institution not in our catalog')"
        ),
        {"id": uuid.uuid4(), "group": group_id},
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM qualification_group_member WHERE group_id = :g"),
            {"g": group_id},
        ).scalar_one()
        == 2
    )


def test_a_group_member_must_be_identified_somehow(conn: Connection) -> None:
    group_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO qualification_group (id, code, name_en) "
            "VALUES (:id, 'EMPTY_PROBE', 'Probe')"
        ),
        {"id": group_id},
    )
    with expect_violation(conn, "member_is_identified"):
        conn.execute(
            text("INSERT INTO qualification_group_member (id, group_id) VALUES (:id, :group)"),
            {"id": uuid.uuid4(), "group": group_id},
        )
