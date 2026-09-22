"""Workbook reading, name normalisation and target-list validation.

Workbooks are synthesised with openpyxl rather than committed as fixtures, so each
test states the shape it is about in readable Python. The client's real file is
exercised separately, in the integration suite, when it is present.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from app.domains.onboarding.naming import (
    names_differ_only_by_presentation,
    normalize_institution_name,
)
from app.domains.onboarding.regions import resolve_destination
from app.domains.onboarding.target_list import (
    TargetListValidationError,
    parse_target_list,
)
from app.domains.onboarding.workbook import WorkbookRejectedError, load_workbook

HEADERS = [
    "序号",
    "QS 2027 世界排名",
    "院校名称（QS 官方英文）",
    "地区",
    "官方 Country/Territory",
    "综合得分",
]

#: A handful of invented institutions. No real university appears in a fixture: a
#: seeded real fact would be a published claim with no provenance, which is what this
#: platform exists to prevent.
SAMPLE_ROWS: list[list[object]] = [
    [1, 10, "Northgate Institute of Technology", "英国", "United Kingdom", 92.4],
    [2, 10, "Rivermouth University", "英国", "United Kingdom", 92.4],
    [3, 24, "Fairhaven College", "中国香港", "Hong Kong SAR, China", 81.0],
    [4, 37, "Eastvale Polytechnic University", "中国澳门", "Macao SAR, China", 70.5],
]


#: Default preamble: just a title. A real client file carries more, but a list with
#: no name at all is refused by the parser -- deliberately, since two unnamed imports
#: could not be told apart afterwards -- so the shared helper supplies one.
DEFAULT_PREAMBLE = ("Invented Targets List 2027",)


def write_workbook(
    path: Path,
    *,
    rows: Sequence[Sequence[object]] = tuple(SAMPLE_ROWS),
    headers: Sequence[object] = tuple(HEADERS),
    preamble: Sequence[str] = DEFAULT_PREAMBLE,
    summary: Sequence[tuple[str, int]] = (),
    sheet_title: str = "targets",
    extra_sheet: str | None = None,
) -> Path:
    """Build a workbook shaped like a client target list."""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = sheet_title

    for line in preamble:
        sheet.append([line])
    # The summary block sits to the right of the data columns, as the client's does.
    summary_start = len(preamble) or 1
    for offset, (label, count) in enumerate(summary):
        sheet.cell(row=summary_start + offset, column=len(headers) + 2, value=label)
        sheet.cell(row=summary_start + offset, column=len(headers) + 3, value=count)

    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))

    if extra_sheet is not None:
        second = book.create_sheet(extra_sheet)
        second.append(list(headers))
        second.append([1, 5, "Somewhere Else University", "英国", "United Kingdom", 50.0])

    book.save(path)
    return path


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


class TestNormalisation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Case.
            ("Northgate University", "NORTHGATE UNIVERSITY"),
            # Whitespace, including a non-breaking and an ideographic space.
            ("Northgate  University", "Northgate University"),
            ("Northgate University", "Northgate University"),
            (" Northgate University ", "Northgate University"),
            # Curly vs straight apostrophe -- the same institution, two typists.
            ("King’s College Northgate", "King's College Northgate"),
            # The six Unicode dashes.
            ("Northgate–Rivermouth University", "Northgate-Rivermouth University"),
            ("Northgate—Rivermouth University", "Northgate-Rivermouth University"),
            # Invisible characters.
            ("Northgate​University", "NorthgateUniversity"),
            # Full-width Latin, common in Chinese-authored spreadsheets.
            ("Ｎorthgate University", "Northgate University"),
            # Trailing punctuation.
            ("Northgate University.", "Northgate University"),
        ],
    )
    def test_presentation_differences_fold_together(self, left: str, right: str) -> None:
        assert normalize_institution_name(left) == normalize_institution_name(right)
        assert names_differ_only_by_presentation(left, right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Deliberately NOT merged: these are the cases where aggressive
            # normalisation would silently corrupt scope.
            ("University of Northgate", "Northgate University"),
            ("The University of Northgate", "University of Northgate"),
            ("Northgate University", "Northgate Christ Church University"),
            ("University of Eastvale, Fairhaven", "University of Eastvale, Rivermouth"),
            ("NIT", "Northgate Institute of Technology"),
            ("Northgate University (NU)", "Northgate University"),
        ],
    )
    def test_substantively_different_names_stay_different(self, left: str, right: str) -> None:
        assert normalize_institution_name(left) != normalize_institution_name(right)

    @pytest.mark.parametrize("raw", ["", "   ", "​", "."])
    def test_an_empty_key_is_refused_rather_than_shared(self, raw: str) -> None:
        with pytest.raises(ValueError, match="empty after normalisation"):
            normalize_institution_name(raw)


class TestRegionResolution:
    @pytest.mark.parametrize(
        ("region", "territory", "expected"),
        [
            ("英国", "United Kingdom", "GB"),
            ("美国", "United States of America", "US"),
            ("中国香港", "Hong Kong SAR, China", "HK"),
            ("中国澳门", "Macao SAR, China", "MO"),
            ("澳大利亚", "Australia", "AU"),
            ("加拿大", "Canada", "CA"),
            ("新加坡", "Singapore", "SG"),
            ("新西兰", "New Zealand", "NZ"),
            # Either column alone is enough.
            ("英国", None, "GB"),
            (None, "United Kingdom", "GB"),
            ("", "Singapore", "SG"),
        ],
    )
    def test_known_labels_resolve(
        self, region: str | None, territory: str | None, expected: str
    ) -> None:
        code, problem = resolve_destination(region, territory)
        assert code == expected
        assert problem is None

    def test_a_disagreement_is_reported_rather_than_resolved(self) -> None:
        """If a row says 英国 and Australia, one is wrong and we must not pick."""
        code, problem = resolve_destination("英国", "Australia")
        assert code is None
        assert problem is not None
        assert "GB" in problem and "AU" in problem

    @pytest.mark.parametrize(
        ("region", "territory"),
        [("火星", "Mars"), ("德国", None), (None, "Japan"), (None, None)],
    )
    def test_unknown_labels_are_reported_not_approximated(
        self, region: str | None, territory: str | None
    ) -> None:
        code, problem = resolve_destination(region, territory)
        assert code is None
        assert problem is not None


# ---------------------------------------------------------------------------
# Workbook safety
# ---------------------------------------------------------------------------


class TestWorkbookRejection:
    def test_a_missing_file_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(WorkbookRejectedError, match="no such file"):
            load_workbook(tmp_path / "absent.xlsx")

    def test_an_excel_lock_file_is_not_mistaken_for_a_workbook(self, tmp_path: Path) -> None:
        """The client's real file arrived beside exactly such a lock file."""
        lock = tmp_path / "~$targets.xlsx"
        lock.write_bytes(b"not a workbook")
        with pytest.raises(WorkbookRejectedError, match="lock file"):
            load_workbook(lock)

    def test_an_empty_file_is_rejected(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.xlsx"
        empty.write_bytes(b"")
        with pytest.raises(WorkbookRejectedError, match="empty"):
            load_workbook(empty)

    def test_a_non_zip_file_is_rejected(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.xlsx"
        bogus.write_bytes(b"PK-not-really" + b"x" * 100)
        with pytest.raises(WorkbookRejectedError, match="not a valid"):
            load_workbook(bogus)

    def test_an_archive_bomb_is_refused_before_extraction(self, tmp_path: Path) -> None:
        """A small file that inflates enormously is refused on its ratio alone."""
        bomb = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # 8 MB of zeros compresses to a few kilobytes: a ~1000:1 ratio.
            archive.writestr("xl/worksheets/sheet1.xml", b"\0" * (8 * 1024 * 1024))
        with pytest.raises(WorkbookRejectedError, match="compression ratio"):
            load_workbook(bomb)

    def test_an_archive_with_too_many_entries_is_refused(self, tmp_path: Path) -> None:
        many = tmp_path / "many.xlsx"
        with zipfile.ZipFile(many, "w") as archive:
            for index in range(600):
                archive.writestr(f"part{index}.xml", b"<x/>")
        with pytest.raises(WorkbookRejectedError, match="archive entries"):
            load_workbook(many)

    def test_a_traversing_entry_name_is_refused(self, tmp_path: Path) -> None:
        nasty = tmp_path / "nasty.xlsx"
        with zipfile.ZipFile(nasty, "w") as archive:
            archive.writestr("../../escape.xml", b"<x/>")
        with pytest.raises(WorkbookRejectedError, match="unsafe name"):
            load_workbook(nasty)

    def test_a_valid_workbook_is_hashed_over_its_bytes_as_supplied(self, tmp_path: Path) -> None:
        import hashlib

        path = write_workbook(tmp_path / "ok.xlsx")
        loaded = load_workbook(path)
        assert loaded.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert loaded.file_byte_size == path.stat().st_size


# ---------------------------------------------------------------------------
# Target-list validation
# ---------------------------------------------------------------------------


class TestHeaderDiscovery:
    def test_the_header_row_is_found_below_any_amount_of_preamble(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "preamble.xlsx",
            preamble=["Title", "Scope: something", "数据来源：somewhere", "note", "another"],
        )
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.header_row == 6
        assert len(parsed.rows) == len(SAMPLE_ROWS)

    def test_a_different_year_in_the_rank_header_still_resolves(self, tmp_path: Path) -> None:
        """A QS 2028 file must not need a code change."""
        headers = list(HEADERS)
        headers[1] = "QS 2028 世界排名"
        path = write_workbook(tmp_path / "y2028.xlsx", headers=headers)
        parsed = parse_target_list(load_workbook(path), list_version="2028.1")
        assert len(parsed.rows) == len(SAMPLE_ROWS)
        assert parsed.rows[0].qs_rank == 10

    def test_english_headers_are_recognised(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "english.xlsx",
            headers=["No.", "QS Rank", "Institution", "Region", "Country/Territory", "Score"],
        )
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert len(parsed.rows) == len(SAMPLE_ROWS)

    def test_a_file_without_a_rank_column_is_refused(self, tmp_path: Path) -> None:
        headers = list(HEADERS)
        headers[1] = "irrelevant"
        path = write_workbook(tmp_path / "norank.xlsx", headers=headers)
        with pytest.raises(TargetListValidationError, match="no header row found"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_a_named_sheet_is_honoured(self, tmp_path: Path) -> None:
        path = write_workbook(tmp_path / "two.xlsx", sheet_title="real", extra_sheet="draft")
        parsed = parse_target_list(load_workbook(path), sheet_name="real", list_version="1.0")
        assert parsed.sheet_name == "real"
        assert len(parsed.rows) == len(SAMPLE_ROWS)

    def test_a_missing_named_sheet_is_refused(self, tmp_path: Path) -> None:
        path = write_workbook(tmp_path / "one.xlsx", sheet_title="real")
        with pytest.raises(TargetListValidationError, match="expected sheet"):
            parse_target_list(load_workbook(path), sheet_name="absent", list_version="1.0")

    def test_several_populated_sheets_require_the_operator_to_choose(self, tmp_path: Path) -> None:
        """Choosing for them risks importing a different scope than intended."""
        path = write_workbook(tmp_path / "ambiguous.xlsx", extra_sheet="draft")
        with pytest.raises(TargetListValidationError, match="more than one populated sheet"):
            parse_target_list(load_workbook(path), list_version="1.0")


class TestRowValidation:
    def test_tied_ranks_are_accepted(self, tmp_path: Path) -> None:
        """QS ties are normal: the client's 2027 list has 29 of them."""
        path = write_workbook(tmp_path / "ties.xlsx")
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        ranks = [row.qs_rank for row in parsed.rows]
        assert ranks.count(10) == 2

    def test_a_banded_rank_is_refused(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][1] = "501+"
        path = write_workbook(tmp_path / "banded.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError, match="not a plain number"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_a_duplicate_institution_is_refused(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows.append([5, 99, "  northgate institute of TECHNOLOGY ", "英国", "United Kingdom", 50.0])
        path = write_workbook(tmp_path / "dupe.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError, match="duplicate institution"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_an_absent_score_is_allowed(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][5] = None
        path = write_workbook(tmp_path / "noscore.xlsx", rows=rows)
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.rows[0].qs_score is None

    def test_a_non_numeric_score_is_refused(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][5] = "n/a"
        path = write_workbook(tmp_path / "badscore.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError, match="not numeric"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_a_score_out_of_range_is_refused(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][5] = 140.0
        path = write_workbook(tmp_path / "bigscore.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError, match="outside 0-100"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_score_precision_survives_the_float_round_trip(self, tmp_path: Path) -> None:
        """Excel stores every number as a float; 32.5 must not become 32.499999."""
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][5] = 32.5
        path = write_workbook(tmp_path / "precise.xlsx", rows=rows)
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.rows[0].qs_score == Decimal("32.5")

    def test_a_missing_region_is_refused(self, tmp_path: Path) -> None:
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][3] = None
        path = write_workbook(tmp_path / "noregion.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError, match="region is missing"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_an_unknown_region_warns_rather_than_refusing(self, tmp_path: Path) -> None:
        """The institution is still in scope; it just cannot be placed yet."""
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][3] = "火星"
        rows[0][4] = "Mars"
        path = write_workbook(tmp_path / "mars.xlsx", rows=rows)
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.rows[0].destination_code is None
        assert any("unrecognised region" in warning for warning in parsed.warnings)

    def test_a_blank_row_inside_the_table_does_not_end_it(self, tmp_path: Path) -> None:
        rows: list[list[object]] = [list(SAMPLE_ROWS[0]), [None] * 6, list(SAMPLE_ROWS[1])]
        path = write_workbook(tmp_path / "gap.xlsx", rows=rows)
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert len(parsed.rows) == 2

    def test_a_header_with_no_data_is_refused(self, tmp_path: Path) -> None:
        path = write_workbook(tmp_path / "headeronly.xlsx", rows=[])
        with pytest.raises(TargetListValidationError, match="no data rows"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_every_problem_is_reported_at_once(self, tmp_path: Path) -> None:
        """An operator fixing a file wants the whole list, not the first failure."""
        rows = [list(row) for row in SAMPLE_ROWS]
        rows[0][1] = "501+"
        rows[1][5] = "n/a"
        rows[2][3] = None
        path = write_workbook(tmp_path / "messy.xlsx", rows=rows)
        with pytest.raises(TargetListValidationError) as excinfo:
            parse_target_list(load_workbook(path), list_version="1.0")
        assert len(excinfo.value.problems) == 3


class TestDeclaredCounts:
    def test_a_declared_total_that_matches_is_accepted(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "counted.xlsx",
            preamble=["Title", f"数据来源：somewhere；共筛得 {len(SAMPLE_ROWS)} 所院校。"],
        )
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.declared_row_count == len(SAMPLE_ROWS)

    def test_a_truncated_file_is_caught_by_its_own_declared_total(self, tmp_path: Path) -> None:
        """This is the check that stops a partial paste importing as a smaller scope."""
        path = write_workbook(
            tmp_path / "truncated.xlsx",
            preamble=["Title", "数据来源：somewhere；共筛得 181 所院校。"],
        )
        with pytest.raises(TargetListValidationError, match="may be truncated"):
            parse_target_list(load_workbook(path), list_version="1.0")

    def test_the_region_summary_block_is_cross_checked(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "summary.xlsx",
            preamble=["Title", "scope"],
            summary=[("英国", 2), ("中国香港", 1), ("中国澳门", 1), ("合计", 4)],
        )
        parsed = parse_target_list(load_workbook(path), list_version="1.0")
        assert parsed.declared_region_counts["__total__"] == 4
        assert parsed.declared_region_counts["英国"] == 2

    def test_a_summary_disagreeing_with_the_rows_is_refused(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "badsummary.xlsx",
            preamble=["Title", "scope"],
            summary=[("英国", 48), ("合计", 48)],
        )
        with pytest.raises(TargetListValidationError, match="summary"):
            parse_target_list(load_workbook(path), list_version="1.0")


class TestMetadata:
    def test_version_and_date_are_read_from_the_files_own_preamble(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "meta.xlsx",
            preamble=[
                "Some Rankings List 2027",
                "筛选范围：rank ≤ 500",
                "数据来源：Some Rankings 2027 官方 Excel v3.2"
                "（2026-06-18 发布）；共筛得 4 所院校。",
                "https://example.org/list.xlsx",
                "口径说明：ties share a rank.",
            ],
        )
        parsed = parse_target_list(load_workbook(path))
        assert parsed.list_name == "Some Rankings List 2027"
        assert parsed.list_version == "3.2"
        assert str(parsed.published_at) == "2026-06-18"
        assert parsed.source_url == "https://example.org/list.xlsx"
        assert parsed.source_description is not None
        assert "官方 Excel v3.2" in parsed.source_description
        assert parsed.scope_note is not None
        assert parsed.method_note is not None

    def test_a_file_stating_no_version_requires_one_to_be_supplied(self, tmp_path: Path) -> None:
        """Two imports that cannot be told apart would make history unanswerable."""
        path = write_workbook(tmp_path / "noversion.xlsx", preamble=["A List"])
        with pytest.raises(TargetListValidationError, match="does not state a list version"):
            parse_target_list(load_workbook(path))

    def test_a_supplied_version_overrides_the_file(self, tmp_path: Path) -> None:
        path = write_workbook(tmp_path / "override.xlsx", preamble=["A List", "数据来源：x v1.1"])
        parsed = parse_target_list(load_workbook(path), list_version="1.1-corrected")
        assert parsed.list_version == "1.1-corrected"

    def test_no_publication_date_is_left_absent_rather_than_guessed(self, tmp_path: Path) -> None:
        """A date we invented would later read as one the source published (D17)."""
        path = write_workbook(tmp_path / "nodate.xlsx", preamble=["A List", "数据来源：x v1.1"])
        parsed = parse_target_list(load_workbook(path))
        assert parsed.published_at is None

    def test_a_malformed_date_is_left_absent(self, tmp_path: Path) -> None:
        path = write_workbook(
            tmp_path / "baddate.xlsx",
            preamble=["A List", "数据来源：x v1.1（2026-02-30 发布）"],
        )
        parsed = parse_target_list(load_workbook(path))
        assert parsed.published_at is None
