"""What the AI collection run writes, checked against the real schema.

`test_ai_collect.py` covers the judgements the command makes in memory. This module
covers the other half: that the rows it builds are rows this database will actually
accept, and that the constraints which shaped the design are really there.

That second part matters more than it looks. The command copies the source rows it
reads into its own submission for one reason -- `asserted_row_cites_a_source_ref` --
and if that constraint were ever relaxed, the copy would look like ceremony and
someone would remove it. So the constraint is asserted adversarially here: the test
tries to write the fact the design forbids and requires the database to refuse it.

Runs against `datahub_test`, skipped when no test DSN is configured.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import FieldStatus, PilotFactValidationState
from tests.integration.conftest import expect_violation
from tests.integration.test_pilot_readiness import make_candidates

pytestmark = pytest.mark.integration

API = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    path = API / "scripts/ai_collect.py"
    spec = importlib.util.spec_from_file_location("_ai_collect_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(target: uuid.UUID, *, ref: str = "S0001", source_type: str = "TUITION_FEES") -> Any:
    """A registered responsibility, shaped exactly as the command reads one."""
    url = f"https://example.edu/{ref.lower()}/fees"
    return {
        "source_ref": ref,
        "target_institution_id": target,
        "sheet_row_no": 2,
        "source_type": source_type,
        "degree_scope": None,
        "workbook_column": "tuition_url",
        "official_url": url,
        "normalized_url": url,
        "url_sha256": f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
        "host": "example.edu",
        "is_third_party": False,
    }


@pytest.fixture
def staged(conn: Connection) -> Any:
    """One institution, one page, and the submission a run would open for it."""
    module = _module()
    target = make_candidates(conn, 1)[0]
    source = _source(target)
    submission_id = module._open_submission(conn, model="test-model", sources=[source])
    return module, submission_id, source


def test_a_run_opens_a_collection_workbook_that_does_not_define_scope(
    conn: Connection, staged: Any
) -> None:
    """The client's list says who is in the pilot. A machine's reading never does."""
    _, submission_id, _ = staged
    row = conn.execute(
        text(
            "SELECT submission_kind, defines_pilot_scope, template_version "
            "  FROM pilot_submission WHERE id = :id"
        ),
        {"id": submission_id},
    ).one()

    assert row.submission_kind == "COLLECTION_WORKBOOK"
    assert row.defines_pilot_scope is False
    assert row.template_version.startswith("llm-")


def test_the_pages_it_will_read_are_recorded_as_pending_candidates(
    conn: Connection, staged: Any
) -> None:
    """A machine reading a page is not a human deciding the page is worth registering."""
    _, submission_id, _ = staged
    state = conn.execute(
        text("SELECT verification_state FROM pilot_collected_source WHERE submission_id = :id"),
        {"id": submission_id},
    ).scalar_one()

    assert state == "PENDING"


def test_a_quoted_value_is_accepted_and_lands_needing_review(conn: Connection, staged: Any) -> None:
    module, submission_id, source = staged
    module._insert_fact(
        conn,
        submission_id=submission_id,
        row_no=2,
        source=source,
        fact_type="TUITION",
        field_path="tuition.amount",
        value_text="AUD 45,600 per annum",
        evidence="Tuition for 2026 is AUD 45,600 per annum.",
        effective_url=source["official_url"],
        not_stated=False,
        model="test-model",
        detail={"quote": "AUD 45,600 per annum", "quote_verified": True},
    )

    row = conn.execute(
        text(
            "SELECT field_status, validation_state, official_text, source_ref, "
            "       amount_min, collected_values "
            "  FROM pilot_collected_fact WHERE submission_id = :id"
        ),
        {"id": submission_id},
    ).one()

    assert row.field_status == FieldStatus.PUBLISHED.value
    assert row.validation_state == PilotFactValidationState.NEEDS_REVIEW.value
    assert row.source_ref == "S0001"
    assert "45,600" in row.official_text
    # Unparsed on purpose: reading "AUD 45,600 per annum" as a number is a decision a
    # reconciler makes, not a side effect of collecting it.
    assert row.amount_min is None
    assert row.collected_values["quote_verified"] is True


def test_an_abstention_is_stored_as_officially_not_published(conn: Connection, staged: Any) -> None:
    module, submission_id, source = staged
    module._insert_fact(
        conn,
        submission_id=submission_id,
        row_no=2,
        source=source,
        fact_type="TUITION",
        field_path="tuition.amount",
        value_text="",
        evidence="Fees are set per programme.",
        effective_url=source["official_url"],
        not_stated=True,
        model="test-model",
        detail={"unresolved_reason": "NOT_STATED_ON_PAGE"},
    )

    status = conn.execute(
        text("SELECT field_status FROM pilot_collected_fact WHERE submission_id = :id"),
        {"id": submission_id},
    ).scalar_one()

    assert status == FieldStatus.OFFICIALLY_NOT_PUBLISHED.value


def test_an_asserted_fact_cannot_be_written_without_naming_its_page(
    conn: Connection, staged: Any
) -> None:
    """The constraint the whole design bends around, asserted rather than assumed.

    Without it the run could skip copying its sources and write facts citing nothing,
    and a reviewer would have no page to open.
    """
    _, submission_id, source = staged
    with expect_violation(conn, "asserted_row_cites_a_source_ref"):
        conn.execute(
            text(
                "INSERT INTO pilot_collected_fact (submission_id, sheet_name, "
                "  sheet_row_no, target_institution_id, fact_type, field_path, "
                "  field_status, validation_state) "
                "VALUES (:id, 'ai_collect', 2, :target, 'TUITION', 'tuition.amount', "
                "  'PUBLISHED', 'NEEDS_REVIEW')"
            ),
            {"id": submission_id, "target": source["target_institution_id"]},
        )


def test_a_fact_cannot_cite_a_page_listed_by_another_submission(
    conn: Connection, staged: Any
) -> None:
    """`source_ref` is submission-local, which is why the run copies rather than points.

    S0001 exists in the original official-source list too. If the reference resolved
    globally, a run could quietly attribute its reading to somebody else's collection.
    """
    module, _, source = staged
    other = module._open_submission(
        conn, model="test-model", sources=[_source(source["target_institution_id"], ref="S0002")]
    )
    with expect_violation(conn, "fk_pilot_collected_fact_submission_id_source_ref"):
        conn.execute(
            text(
                "INSERT INTO pilot_collected_fact (submission_id, sheet_name, "
                "  sheet_row_no, target_institution_id, source_ref, fact_type, "
                "  field_path, field_status, validation_state) "
                "VALUES (:id, 'ai_collect', 2, :target, 'S0001', 'TUITION', "
                "  'tuition.amount', 'PUBLISHED', 'NEEDS_REVIEW')"
            ),
            {"id": other, "target": source["target_institution_id"]},
        )


def test_the_run_writes_nothing_a_publisher_could_ever_read(conn: Connection, staged: Any) -> None:
    """The plane's standing guarantee, restated for this producer.

    Whatever the model says, it lands where the publishing role holds no privilege at
    all -- so a wrong reading cannot become a published fact by any route.
    """
    module, submission_id, source = staged
    module._insert_fact(
        conn,
        submission_id=submission_id,
        row_no=2,
        source=source,
        fact_type="TUITION",
        field_path="tuition.amount",
        value_text="AUD 45,600 per annum",
        evidence="Tuition for 2026 is AUD 45,600 per annum.",
        effective_url=source["official_url"],
        not_stated=False,
        model="test-model",
        detail={},
    )

    states = (
        conn.execute(
            text(
                "SELECT DISTINCT validation_state FROM pilot_collected_fact WHERE sheet_name = :s"
            ),
            {"s": module.SHEET_NAME},
        )
        .scalars()
        .all()
    )

    assert states == [PilotFactValidationState.NEEDS_REVIEW.value]
