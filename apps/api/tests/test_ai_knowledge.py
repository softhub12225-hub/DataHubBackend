"""Static checks for ChatGPT-style OpenAI knowledge collection."""

from __future__ import annotations

from app.db.enums import SourceCategory
from app.domains.pilot import ai_knowledge as ak
from app.domains.pilot.models import PILOT_FACT_TYPES


def test_ask_for_fact_types_are_accepted_by_the_check() -> None:
    ak.assert_ask_for_is_valid()
    for asks in ak.ASK_FOR.values():
        for fact_type, _field, _q in asks:
            assert fact_type in PILOT_FACT_TYPES


def test_ask_for_keys_are_real_source_categories() -> None:
    categories = {c.value for c in SourceCategory}
    assert set(ak.ASK_FOR) <= categories


def test_sheet_name_marks_model_knowledge_rows() -> None:
    assert ak.SHEET_NAME == "ai_knowledge"
    assert len(ak.SHEET_NAME) <= 64


def test_template_version_fits_column() -> None:
    version = f"{ak.EXTRACTOR_NAME}-{ak.EXTRACTOR_VERSION}"
    assert len(version) <= 32
