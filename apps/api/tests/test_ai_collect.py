"""The judgements the AI collection command makes before any row reaches the database.

Two of them are load-bearing and neither is checked by the schema:

* every field kind the model may return maps to a `fact_type` the CHECK accepts --
  otherwise a run dies partway through, having written some institutions and not
  others, which is worse than not running;
* an abstention is only stored where the page is the registered authority for that
  category -- otherwise "the admissions page did not mention fees" is recorded as
  "this university does not publish fees", which is a false published finding and the
  most expensive mistake this plane can make.

No database and no API key, so it runs everywhere.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from app.db.enums import SourceCategory
from app.domains.claims.llm import REQUESTED_KINDS
from app.domains.pilot.models import PILOT_FACT_TYPES

API = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    """Loaded by path: `scripts/` is not a package, as `test_pilot_scope` explains."""
    path = API / "scripts/ai_collect.py"
    spec = importlib.util.spec_from_file_location("_ai_collect_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_kind_the_model_may_return_has_somewhere_to_go() -> None:
    """A gap here is a crash mid-run, after part of the pilot has been written."""
    module = _module()
    assert set(REQUESTED_KINDS) <= set(module.FACT_FOR_KIND)


def test_every_mapped_fact_type_is_one_the_check_accepts() -> None:
    module = _module()
    for fact_type, _ in module.FACT_FOR_KIND.values():
        assert fact_type in PILOT_FACT_TYPES


def test_abstention_is_recorded_only_from_the_authority_for_that_category() -> None:
    """The distinction between a finding and a page about something else."""
    module = _module()
    assert module.abstention_is_a_finding(SourceCategory.TUITION_FEES.value, "TUITION")
    assert not module.abstention_is_a_finding(
        SourceCategory.UNDERGRADUATE_ADMISSIONS.value, "TUITION"
    )
    assert not module.abstention_is_a_finding(SourceCategory.UNIVERSITY_HOME.value, "TUITION")


def test_an_unclassified_page_can_never_report_that_nothing_is_published() -> None:
    """A page supplied without a category is authoritative for nothing at all."""
    module = _module()
    for fact_type in PILOT_FACT_TYPES:
        assert not module.abstention_is_a_finding("UNCLASSIFIED", fact_type)


def test_the_remit_map_names_real_categories_and_real_fact_types() -> None:
    module = _module()
    categories = {category.value for category in SourceCategory}
    for source_type, fact_type in module.ABSTENTION_REMIT.items():
        assert source_type in categories
        assert fact_type in PILOT_FACT_TYPES


def test_the_sheet_name_marks_these_rows_as_machine_collected() -> None:
    """A reviewer weighs an AI reading differently, and can only do that if it is
    distinguishable from a person's.
    """
    module = _module()
    assert module.SHEET_NAME == "ai_collect"
    assert len(module.SHEET_NAME) <= 64  # the column's width
