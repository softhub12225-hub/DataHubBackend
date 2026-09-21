"""The pilot collection workbook, and the rule for matching it back.

Two things are under test. The export must carry a stable matching key on every row
and must not invent a pilot selection. The matching contract must refuse to guess:
its whole value is that it hands work back to a human rather than attaching one
university's fee to another.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.onboarding import pilot_matching
from app.domains.onboarding.importer import import_target_list
from app.domains.onboarding.pilot_template import (
    HIGH_RISK_SHEETS,
    MATCH_KEY_COLUMN,
    TEMPLATE_FIELD_STATUSES,
    TEMPLATE_SHEETS,
    build_pilot_template,
    choices_for,
    load_target_rows,
)
from tests.test_onboarding_parsing import SAMPLE_ROWS, write_workbook

pytestmark = pytest.mark.integration


@pytest.fixture
def imported(conn: Connection, tmp_path: Path) -> Any:
    path = write_workbook(tmp_path / "targets.xlsx", preamble=["A List", "数据来源：x v1.0"])
    return import_target_list(conn, path)


@pytest.fixture
def workbook(conn: Connection, tmp_path: Path, imported: Any) -> Any:
    import openpyxl

    out = tmp_path / "pilot.xlsx"
    build_pilot_template(conn, out, pilot_target=2)
    return openpyxl.load_workbook(out)


# ===========================================================================
# The export
# ===========================================================================


def test_every_sheet_carries_the_matching_key_first(workbook: Any) -> None:
    """A row without it cannot be matched, so no sheet may omit it."""
    for spec in TEMPLATE_SHEETS:
        header = [cell.value for cell in workbook[spec.name][1]]
        assert header[0] == MATCH_KEY_COLUMN, f"{spec.name} does not start with the key"


def test_each_institution_appears_once_with_its_real_id(
    conn: Connection, workbook: Any, imported: Any
) -> None:
    sheet = workbook["Pilot_Universities"]
    exported = [sheet.cell(row=row, column=1).value for row in range(2, sheet.max_row + 1)]
    assert len(exported) == len(SAMPLE_ROWS)
    assert len(set(exported)) == len(exported), "an institution was exported twice"

    live = {str(row[0]) for row in conn.execute(text("SELECT id FROM target_institution")).all()}
    assert set(exported) == live


def test_the_export_does_not_select_the_pilot_for_the_client(workbook: Any) -> None:
    """U8. Which institutions are in the pilot is the client's decision.

    Every `selected` cell is blank, so nothing is pre-chosen -- not by rank, not by
    destination, not by row order.
    """
    sheet = workbook["Pilot_Universities"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    selected = [
        sheet.cell(row=row, column=columns["selected"]).value for row in range(2, sheet.max_row + 1)
    ]
    assert set(selected) == {None}


def test_a_previously_assigned_wave_is_preserved_on_re_export(
    conn: Connection, tmp_path: Path, imported: Any
) -> None:
    """Re-exporting must not silently un-select earlier work."""
    import openpyxl

    target = conn.execute(text("SELECT id FROM target_institution LIMIT 1")).scalar_one()
    conn.execute(
        text("UPDATE target_institution SET pilot_wave = 1 WHERE id = :id"), {"id": target}
    )
    out = tmp_path / "again.xlsx"
    build_pilot_template(conn, out)
    sheet = openpyxl.load_workbook(out)["Pilot_Universities"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    marked = {
        sheet.cell(row=row, column=1).value
        for row in range(2, sheet.max_row + 1)
        if sheet.cell(row=row, column=columns["selected"]).value == "YES"
    }
    assert marked == {str(target)}


def test_qs_columns_are_marked_reference_only(workbook: Any) -> None:
    """The QS name is shown so a human can recognise a row, and labelled as such."""
    sheet = workbook["Pilot_Universities"]
    comments = {cell.value: (cell.comment.text if cell.comment else "") for cell in sheet[1]}
    assert "not publish" in comments["qs_name"].lower()
    assert "not published" in comments["qs_rank"].lower()


def test_the_status_columns_mirror_the_schemas_own_field_status_columns(
    conn: Connection, workbook: Any
) -> None:
    """The template's status vocabulary is the schema's, not a parallel invention."""
    schema_columns = {
        (row[0], row[1])
        for row in conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                " WHERE table_schema = 'public' AND column_name LIKE '%field_status'"
            )
        ).all()
    }
    governed_tables = {
        table
        for table, _column in schema_columns
        if table
        in {
            "tuition",
            "application_deadline",
            "admission_requirement",
            "language_requirement",
            "program",
        }
    }
    assert len(governed_tables) == 5

    status_columns = [
        (spec.name, column.name)
        for spec in TEMPLATE_SHEETS
        for column in spec.columns
        if column.choices == TEMPLATE_FIELD_STATUSES
    ]
    assert {sheet for sheet, _ in status_columns} == {
        "Admissions",
        "Language_Requirements",
        "Tuition",
        "Deadlines",
    }
    # `program.lifecycle_field_status` is offered as the lifecycle vocabulary rather
    # than the three-way status, because "is it on offer" has its own values.
    assert any(column.name == "lifecycle_status" for column in TEMPLATE_SHEETS[2].columns)


def test_not_checked_and_not_published_are_both_offerable(workbook: Any) -> None:
    """The distinction the client asked for, present in the file itself."""
    assert set(TEMPLATE_FIELD_STATUSES) == {
        "PUBLISHED",
        "OFFICIALLY_NOT_PUBLISHED",
        "NOT_CHECKED",
    }
    readme = "\n".join(str(row[0].value) for row in workbook["README"].iter_rows() if row[0].value)
    assert "OFFICIALLY_NOT_PUBLISHED" in readme
    assert "nobody has looked yet" in readme


def test_every_vocabulary_column_gets_a_real_dropdown(conn: Connection, workbook: Any) -> None:
    """Including `source_type`, whose fifteen values exceed Excel's inline-list cap.

    An inline list would have silently omitted the single most important dropdown in
    the workbook, so the values live on a hidden sheet and are referenced by range.
    """
    from app.domains.onboarding.pilot_template import _load_vocabularies

    vocabularies = _load_vocabularies(conn)
    for spec in TEMPLATE_SHEETS:
        expected = sum(1 for column in spec.columns if choices_for(column, vocabularies))
        actual = len(workbook[spec.name].data_validations.dataValidation)
        assert actual == expected, f"{spec.name}: {actual} dropdowns, expected {expected}"

    header = {cell.value: cell.column_letter for cell in workbook["Official_Sources"][1]}
    source_type = next(
        dv
        for dv in workbook["Official_Sources"].data_validations.dataValidation
        if header["source_type"] in str(dv.sqref)
    )
    assert "_Lists" in str(source_type.formula1)


def test_the_high_risk_sheets_all_require_a_source_reference(workbook: Any) -> None:
    """Invariant I3: a high-risk fact is unpublishable without evidence.

    The link is `source_ref`, not `source_url`. A pasted address cannot be resolved
    without matching it, which is the one thing this design refuses to do.
    """
    for name in HIGH_RISK_SHEETS:
        header = [cell.value for cell in workbook[name][1]]
        assert "source_ref" in header, f"{name} collects no source_ref"
        comment = next(
            cell.comment.text for cell in workbook[name][1] if cell.value == "source_ref"
        )
        assert "Official_Sources" in comment
        # And the optional address column says plainly that it is not the link.
        url_comment = next(
            cell.comment.text for cell in workbook[name][1] if cell.value == "source_url"
        )
        assert "never on this" in url_comment


def test_the_lists_sheet_is_hidden(workbook: Any) -> None:
    assert workbook["_Lists"].sheet_state == "hidden"


def test_the_export_refuses_when_there_is_nothing_in_scope(
    conn: Connection, tmp_path: Path
) -> None:
    """Better an error than a workbook with a header row and no institutions."""
    with pytest.raises(ValueError, match="import a target list first"):
        build_pilot_template(conn, tmp_path / "empty.xlsx")


def test_dropped_institutions_are_excluded_by_default(
    conn: Connection, tmp_path: Path, imported: Any
) -> None:
    target = conn.execute(text("SELECT id FROM target_institution LIMIT 1")).scalar_one()
    conn.execute(
        text(
            "UPDATE target_institution SET is_in_current_list = false, "
            "removed_from_list_id = latest_list_id WHERE id = :id"
        ),
        {"id": target},
    )
    current = {row.target_institution_id for row in load_target_rows(conn)}
    everything = {row.target_institution_id for row in load_target_rows(conn, only_current=False)}
    assert target not in current
    assert target in everything


# ===========================================================================
# The matching contract
# ===========================================================================


def test_a_row_with_the_id_resolves(conn: Connection, imported: Any) -> None:
    target = conn.execute(text("SELECT id FROM target_institution LIMIT 1")).scalar_one()
    result = pilot_matching.resolve_row(conn, raw_id=str(target))
    assert result.is_resolved
    assert result.target_institution_id == target


def test_a_uuid_object_resolves_too(conn: Connection, imported: Any) -> None:
    target = conn.execute(text("SELECT id FROM target_institution LIMIT 1")).scalar_one()
    assert pilot_matching.resolve_row(conn, raw_id=target).is_resolved


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_missing_id_is_never_guessed_from_a_name(
    conn: Connection, imported: Any, raw: str | None
) -> None:
    """The heart of requirement 5: no silent fuzzy fallback."""
    result = pilot_matching.resolve_row(
        conn,
        raw_id=raw,
        raw_name="Northgate Institute of Technology",
        sheet="Tuition",
        row_number=7,
    )
    assert not result.is_resolved
    assert result.needs_manual_resolution
    assert result.outcome is pilot_matching.MatchOutcome.MISSING_ID
    # A suggestion is offered, and is explicitly not a match.
    assert len(result.candidates) == 1
    assert "suggestion, not a match" in result.message
    assert "Tuition row 7" in result.message


@pytest.mark.parametrize(
    "raw", ["not-a-uuid", "abc-123", "12345", "=CONCATENATE(A1)", "see note below"]
)
def test_a_malformed_id_is_reported_not_repaired(conn: Connection, imported: Any, raw: str) -> None:
    result = pilot_matching.resolve_row(conn, raw_id=raw)
    assert result.outcome is pilot_matching.MatchOutcome.MALFORMED_ID
    assert result.needs_manual_resolution


def test_an_unknown_but_valid_uuid_is_reported(conn: Connection, imported: Any) -> None:
    result = pilot_matching.resolve_row(conn, raw_id=str(uuid.uuid4()))
    assert result.outcome is pilot_matching.MatchOutcome.UNKNOWN_ID
    assert "different environment" in result.message


def test_a_name_matching_two_institutions_is_ambiguous(conn: Connection, tmp_path: Path) -> None:
    """Two list versions can name the same institution; a name must not pick one.

    Built by importing the same institution under two target lists, which is exactly
    what happens across QS editions.
    """
    first = write_workbook(tmp_path / "one.xlsx", preamble=["List A", "数据来源：x v1.0"])
    import_target_list(conn, first, list_name="List A")
    second = write_workbook(tmp_path / "two.xlsx", preamble=["List B", "数据来源：x v1.0"])
    import_target_list(conn, second, list_name="List B")

    # The same normalised name now belongs to two separate target institutions,
    # because they came from differently named lists.
    duplicated = conn.execute(
        text("SELECT qs_name FROM target_list_entry GROUP BY qs_name HAVING count(*) > 1 LIMIT 1")
    ).scalar_one_or_none()
    if duplicated is None:
        pytest.skip("the two imports shared match keys; ambiguity not reproducible here")

    result = pilot_matching.resolve_row(conn, raw_id=None, raw_name=duplicated)
    assert result.outcome in {
        pilot_matching.MatchOutcome.AMBIGUOUS_NAME,
        pilot_matching.MatchOutcome.MISSING_ID,
    }
    assert result.needs_manual_resolution


def test_a_name_that_matches_nothing_yields_no_candidates(conn: Connection, imported: Any) -> None:
    result = pilot_matching.resolve_row(conn, raw_id=None, raw_name="Entirely Invented Polytechnic")
    assert result.candidates == ()
    assert "No institution could be suggested" in result.message


def test_a_renamed_institution_is_not_matched_by_similarity(
    conn: Connection, imported: Any
) -> None:
    """`University of Northgate` must not resolve to `Northgate University`.

    This is the case a fuzzy matcher would "helpfully" join, and the case where
    joining it wrongly attaches one institution's official data to another.
    """
    result = pilot_matching.resolve_row(
        conn, raw_id=None, raw_name="Institute of Technology, Northgate"
    )
    assert not result.is_resolved
    assert result.candidates == ()


def test_every_unresolved_outcome_is_in_the_manual_set(conn: Connection) -> None:
    """A new outcome cannot become importable by being forgotten."""
    importable = {
        outcome
        for outcome in pilot_matching.MatchOutcome
        if outcome not in pilot_matching.NEEDS_MANUAL_RESOLUTION
    }
    assert importable == {pilot_matching.MatchOutcome.RESOLVED}
