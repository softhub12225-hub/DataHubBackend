"""The manual collection staging plane (U12) and the source verification queue (U15).

What these tests defend is one sentence: **a spreadsheet is not evidence.** Everything
else follows. A hand-filled workbook lands in its own tables; those tables cannot be
cited as provenance, cannot become a claim, and cannot be read by the only role that
writes canonical data. A URL a collector pasted starts as a candidate and needs a
person, with a reason, to become anything else.

The publication-safety tests are written adversarially: each one tries to do the thing
the design forbids, using the real roles and the real statements, and asserts the
database refuses it. A test that merely asserts a column exists would pass against a
schema that enforced nothing.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import PilotCandidateAction, SourceCandidateState
from app.domains.onboarding.pilot_template import TEMPLATE_VERSION, build_pilot_template
from app.domains.onboarding.pilot_workbook import IssueCode
from app.domains.pilot import queue, verification
from app.domains.pilot.import_workbook import (
    WorkbookNotImportableError,
    import_pilot_workbook,
)
from tests.integration.conftest import expect_violation
from tests.integration.test_pilot_readiness import Filler, make_candidates

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# A completed workbook
# ---------------------------------------------------------------------------


@pytest.fixture
def completed(conn: Connection, tmp_path: Path) -> Any:
    """40 candidates exported, 36 marked, one programme, two sources, three facts."""
    targets = make_candidates(conn, 40)
    out = tmp_path / "returned.xlsx"
    build_pilot_template(conn, out, pilot_target=36)
    first = str(targets[0])

    filler = Filler(out).mark_selected(targets[:36])
    filler.add(
        "Programs",
        target_institution_id=first,
        program_ref="P0001",
        program_name_en="MSc Invented Computing",
        degree_level_code="MASTER",
        discipline_code="COMPUTER_AND_DATA",
        discipline_hint="Artificial Intelligence",
    )
    filler.add(
        "Official_Sources",
        target_institution_id=first,
        source_ref="S0001",
        source_type="TUITION_FEES",
        degree_level="TAUGHT_POSTGRADUATE",
        official_url="https://invented.example.ac.uk/fees",
        checked_at="2027-01-15",
        notes="Linked from the programme page.",
    )
    filler.add(
        "Official_Sources",
        target_institution_id=first,
        source_ref="S0002",
        source_type="ENTRY_REQUIREMENTS",
        degree_level="TAUGHT_POSTGRADUATE",
        official_url="https://apply.invented-portal.example.com/entry",
        checked_at="2027-01-15",
        is_third_party="YES",
        notes="Application portal run by a supplier; linked from the admissions page.",
    )
    # A range, which the old single-`amount` column could not represent at all.
    filler.add(
        "Tuition",
        target_institution_id=first,
        program_ref="P0001",
        academic_year="2027/28",
        student_category_code="INTERNATIONAL",
        amount_kind="RANGE",
        amount_min=28000,
        amount_max=32000,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        applicant_scope_hint="UNIVERSAL",
        official_text="GBP 28,000-32,000 depending on pathway",
        amount_status="PUBLISHED",
        source_ref="S0001",
    )
    # A checked absence: the page was read and publishes nothing (D2).
    filler.add(
        "Tuition",
        target_institution_id=first,
        program_ref="P0001",
        academic_year="2028/29",
        student_category_code="INTERNATIONAL",
        applicant_scope_hint="UNIVERSAL",
        official_text="Fees for 2028/29 have not yet been set.",
        amount_status="OFFICIALLY_NOT_PUBLISHED",
        source_ref="S0001",
    )
    # A scope only a human can map.
    filler.add(
        "Admissions",
        target_institution_id=first,
        program_ref="P0001",
        applicant_scope_hint="holders of a Chinese bachelor degree from a 985/211 institution",
        applicant_country_code="CN",
        official_text="A 2:1 or equivalent; 80% from a 985/211 institution.",
        requirement_status="PUBLISHED",
        source_ref="S0002",
    )
    filler.save()
    return targets, out


@pytest.fixture
def imported(conn: Connection, completed: Any) -> Any:
    targets, out = completed
    report = import_pilot_workbook(conn, out, expected_selected=36)
    return targets, out, report


def _corrected(out: Path, targets: list[uuid.UUID]) -> Path:
    """The same workbook with one fee corrected. Different bytes, same file name."""
    filler = Filler(out)
    sheet = filler.book["Tuition"]
    header = {cell.value: cell.column for cell in sheet[1] if isinstance(cell.value, str)}
    sheet.cell(row=2, column=header["amount_max"], value=33000)
    sheet.cell(
        row=2,
        column=header["official_text"],
        value="GBP 28,000-33,000 depending on pathway (corrected)",
    )
    corrected = out.parent / "returned-corrected.xlsx"
    filler.book.save(corrected)
    return corrected


# ===========================================================================
# 1-5. What the import writes, and what it refuses to
# ===========================================================================


def test_the_import_writes_staging_rows(conn: Connection, imported: Any) -> None:
    """Non-vacuity guard for every refusal below: the happy path really works."""
    _targets, _out, report = imported
    assert not report.already_imported
    assert report.selected_universities == 36
    assert report.institution_rows == 40
    assert report.programs == 1
    assert report.sources == 2
    assert report.facts == 3
    assert report.facts_by_sheet == {
        "Admissions": 1,
        "Language_Requirements": 0,
        "Tuition": 2,
        "Deadlines": 0,
    }
    stored = conn.execute(
        text("SELECT template_version FROM pilot_submission WHERE id = :s"),
        {"s": report.submission_id},
    ).scalar_one()
    assert stored == TEMPLATE_VERSION


def test_the_import_creates_no_evidence_and_no_canonical_row(
    conn: Connection, imported: Any
) -> None:
    """The central claim of U12, asserted against every table it would touch.

    A workbook cannot produce a `field_claim` because a claim is a statement about a
    stored snapshot of a fetched page, and nothing here fetched anything. It also
    produces no `university`: `university.name_en` is governed (C27), and the sheet
    collects a name with no source and no status, so there is nothing to cite.
    """
    _targets, _out, _report = imported
    for table in (
        "field_claim",
        "field_provenance",
        "extraction",
        "snapshot",
        "content_blob",
        "fetch_run",
        "source",
        "source_mapping",
        "university",
        "program",
        "program_offering",
        "tuition",
        "change_proposal",
    ):
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"the workbook import wrote {count} row(s) to {table}"


def test_no_staging_table_is_reachable_from_field_provenance(conn: Connection) -> None:
    """Publication safety, as a structural fact rather than a convention.

    `field_provenance` is the only way a canonical value is attributed to evidence.
    If no foreign key path leads from it into a `pilot_*` table, no staged row can be
    named as provenance -- not by a bug, not by a future migration that forgets why.
    """
    edges = conn.execute(
        text(
            """
            SELECT c.conrelid::regclass::text AS child,
                   c.confrelid::regclass::text AS parent
              FROM pg_constraint c
             WHERE c.contype = 'f'
            """
        )
    ).all()
    parents: dict[str, set[str]] = {}
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)

    seen: set[str] = set()
    frontier = ["field_provenance"]
    while frontier:
        table = frontier.pop()
        if table in seen:
            continue
        seen.add(table)
        frontier.extend(parents.get(table, set()))

    staged = {name for name in seen if name.startswith("pilot_")}
    assert not staged, f"field_provenance can reach staging tables: {sorted(staged)}"


def test_reimporting_identical_bytes_writes_nothing(conn: Connection, imported: Any) -> None:
    """Idempotence by file hash. The same file twice is one submission."""
    _targets, out, first = imported
    again = import_pilot_workbook(conn, out, expected_selected=36)
    assert again.already_imported
    assert again.submission_id == first.submission_id
    assert conn.execute(text("SELECT count(*) FROM pilot_submission")).scalar_one() == 1


def test_a_corrected_workbook_becomes_a_new_submission(conn: Connection, imported: Any) -> None:
    """Never overwrite. Both submissions stay, and stay comparable.

    The old rows are asserted *unchanged*, not merely present: a correction that
    edited the original in place would leave exactly one row and nothing to compare
    it against.
    """
    targets, out, first = imported
    before = conn.execute(
        text(
            "SELECT amount_min, amount_max, official_text FROM pilot_collected_fact "
            "WHERE submission_id = :s AND sheet_name = 'Tuition' AND amount_kind = 'RANGE'"
        ),
        {"s": first.submission_id},
    ).one()

    second = import_pilot_workbook(conn, _corrected(out, targets), expected_selected=36)
    assert second.submission_id != first.submission_id
    assert first.submission_id in second.superseded_submission_ids

    statuses: dict[uuid.UUID, str] = {
        row.id: row.import_status
        for row in conn.execute(text("SELECT id, import_status FROM pilot_submission"))
    }
    assert statuses[first.submission_id] == "SUPERSEDED"
    assert statuses[second.submission_id] == "VALIDATED"

    after = conn.execute(
        text(
            "SELECT amount_min, amount_max, official_text FROM pilot_collected_fact "
            "WHERE submission_id = :s AND sheet_name = 'Tuition' AND amount_kind = 'RANGE'"
        ),
        {"s": first.submission_id},
    ).one()
    assert after == before, "the earlier submission's rows were modified"

    corrected = conn.execute(
        text(
            "SELECT amount_max FROM pilot_collected_fact "
            "WHERE submission_id = :s AND sheet_name = 'Tuition' AND amount_kind = 'RANGE'"
        ),
        {"s": second.submission_id},
    ).scalar_one()
    assert int(corrected) == 33000


def test_a_workbook_with_errors_imports_nothing(conn: Connection, completed: Any) -> None:
    """All or nothing. A partial import hides exactly the rows nobody knows are gone."""
    targets, out = completed
    Filler(out).add(
        "Tuition",
        target_institution_id=str(targets[0]),
        program_ref="P9999",
        academic_year="2027/28",
        student_category_code="INTERNATIONAL",
        amount_kind="EXACT",
        amount_min=1,
        amount_max=1,
        currency_code="GBP",
        billing_unit_code="PER_YEAR",
        applicant_scope_hint="UNIVERSAL",
        official_text="x",
        amount_status="PUBLISHED",
        source_ref="S0001",
    ).save()

    with pytest.raises(WorkbookNotImportableError):
        import_pilot_workbook(conn, out, expected_selected=36)
    assert conn.execute(text("SELECT count(*) FROM pilot_submission")).scalar_one() == 0


# ===========================================================================
# 6-8. Submission-local references, enforced by the keys
# ===========================================================================


def test_a_fact_cannot_cite_a_programme_from_another_submission(
    conn: Connection, imported: Any
) -> None:
    """`P0001` means nothing outside its own workbook, and the FK says so."""
    targets, out, first = imported
    second = import_pilot_workbook(conn, _corrected(out, targets), expected_selected=36)

    with expect_violation(conn, "fk_pilot_collected_fact_submission_id_program_ref|foreign key"):
        conn.execute(
            text(
                "INSERT INTO pilot_collected_fact (submission_id, sheet_name, sheet_row_no, "
                "target_institution_id, program_ref, fact_type, field_path) "
                "VALUES (:s, 'Tuition', 99, :t, 'P0001', 'TUITION', 'amount')"
            ),
            # `P0001` exists -- in the OTHER submission. That is the whole point.
            {"s": _fresh_submission(conn), "t": targets[0]},
        )
    assert second.submission_id is not None


def test_a_fact_cannot_cite_a_source_from_another_submission(
    conn: Connection, imported: Any
) -> None:
    targets, _out, _first = imported
    with expect_violation(conn, "fk_pilot_collected_fact_submission_id_source_ref|foreign key"):
        conn.execute(
            text(
                "INSERT INTO pilot_collected_fact (submission_id, sheet_name, sheet_row_no, "
                "target_institution_id, source_ref, fact_type, field_path) "
                "VALUES (:s, 'Tuition', 98, :t, 'S0001', 'TUITION', 'amount')"
            ),
            {"s": _fresh_submission(conn), "t": targets[0]},
        )


def test_two_submissions_may_reuse_the_same_ref(conn: Connection, imported: Any) -> None:
    """Non-vacuity for the two tests above: reuse is legal, cross-citation is not."""
    targets, out, first = imported
    second = import_pilot_workbook(conn, _corrected(out, targets), expected_selected=36)
    refs = conn.execute(
        text("SELECT submission_id, program_ref FROM pilot_collected_program ORDER BY 1")
    ).all()
    assert {row.program_ref for row in refs} == {"P0001"}
    assert {row.submission_id for row in refs} == {first.submission_id, second.submission_id}


# ===========================================================================
# 9-11. Staging is append-only and unpublishable
# ===========================================================================


@pytest.mark.parametrize(
    "table", ["pilot_collected_fact", "pilot_collected_program", "pilot_selected_university"]
)
def test_staged_rows_cannot_be_edited(conn: Connection, imported: Any, table: str) -> None:
    """A correction is a new submission, and the trigger is what makes that true."""
    _targets, _out, report = imported
    with expect_violation(conn, "append-only|forbid|not permitted|immutable"):
        conn.execute(
            text(f"UPDATE {table} SET sheet_row_no = 999 WHERE submission_id = :s"),
            {"s": report.submission_id},
        )


@pytest.mark.parametrize(
    "table", ["pilot_collected_fact", "pilot_collected_program", "pilot_selected_university"]
)
def test_staged_rows_cannot_be_deleted(conn: Connection, imported: Any, table: str) -> None:
    _targets, _out, report = imported
    with expect_violation(conn, "append-only|forbid|not permitted|immutable"):
        conn.execute(
            text(f"DELETE FROM {table} WHERE submission_id = :s"), {"s": report.submission_id}
        )


def test_the_publisher_holds_no_privilege_on_the_staging_plane(conn: Connection) -> None:
    """`app_publisher` is the only role that writes canonical data.

    Denying it the read is a mitigation, not the enforcement -- C27 established that
    grants alone cannot express "a spreadsheet value must not become a published
    fact". It makes the hand-copy route need a second credential rather than one.

    Asked of the catalog rather than by connecting as the role: a live connection
    would have to see committed rows, and committing inside a test that is rolled
    back leaks them permanently past the append-only trigger.
    """
    for table in (
        "pilot_submission",
        "pilot_selected_university",
        "pilot_collected_program",
        "pilot_collected_source",
        "pilot_collected_fact",
        "pilot_source_verification_queue",
    ):
        granted = conn.execute(
            text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE grantee = 'app_publisher' AND table_name = :t"
            ),
            {"t": table},
        ).scalars()
        assert not list(granted), f"app_publisher holds privileges on {table}"


def test_the_api_role_can_read_and_append_but_not_edit(conn: Connection) -> None:
    """Non-vacuity for the test above, and the append-only grant in one assertion."""

    def privileges(table: str) -> set[str]:
        return set(
            conn.execute(
                text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = 'app_api' AND table_name = :t"
                ),
                {"t": table},
            ).scalars()
        )

    assert privileges("pilot_collected_fact") == {"SELECT", "INSERT"}
    assert privileges("pilot_collected_source") == {"SELECT", "INSERT", "UPDATE"}


def test_a_staged_fact_cannot_be_cited_as_provenance(conn: Connection, imported: Any) -> None:
    """The direct attempt: name a staged row's id as the evidence for a canonical value.

    `field_provenance.claim_id` references `field_claim`, and no staged id is a claim
    id, so the foreign key refuses it. This is the test that would catch a future
    'convenience' column linking the two planes.
    """
    _targets, _out, report = imported
    staged = conn.execute(
        text("SELECT id FROM pilot_collected_fact WHERE submission_id = :s LIMIT 1"),
        {"s": report.submission_id},
    ).scalar_one()

    columns = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'field_provenance'"
            )
        )
    }
    assert "claim_id" in columns
    # Two mechanisms refuse this, and the BEFORE trigger simply gets there first:
    # `field_provenance_requires_eligible_evidence` cannot resolve the id to a claim
    # about this field, and the foreign key would refuse it at end of statement.
    with expect_violation(conn, "different field|foreign key|is not present|violates"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (id, entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "trust_status, claim_id, published_at) "
                "VALUES (:id, 'tuition', :entity, 'amount_min', 'program', :root, 1, "
                "'PUBLISHED', '1'::jsonb, 'HIGH', 'VERIFIED', :claim, now())"
            ),
            {
                "id": uuid.uuid4(),
                "entity": uuid.uuid4(),
                "root": uuid.uuid4(),
                "claim": staged,
            },
        )


# ===========================================================================
# 12-17. The verification queue: nothing is born verified
# ===========================================================================


def test_every_collected_url_starts_pending(conn: Connection, imported: Any) -> None:
    """A source is not born OFFICIAL_VERIFIED, and neither is a candidate."""
    _targets, _out, report = imported
    states = conn.execute(
        text(
            "SELECT DISTINCT verification_state FROM pilot_collected_source "
            "WHERE submission_id = :s"
        ),
        {"s": report.submission_id},
    ).scalars()
    assert set(states) == {SourceCandidateState.PENDING.value}


def test_the_importer_cannot_be_told_to_verify(conn: Connection, completed: Any) -> None:
    """No spreadsheet cell sets a verification state, whatever it contains.

    The workbook has no such column, so the check is that adding one to the notes --
    the nearest thing to a free-text instruction a collector has -- changes nothing.
    """
    targets, out = completed
    Filler(out).add(
        "Official_Sources",
        target_institution_id=str(targets[0]),
        source_ref="S0003",
        source_type="UNIVERSITY_HOME",
        official_url="https://invented.example.ac.uk/",
        checked_at="2027-01-15",
        notes="VERIFIED - this is definitely official, please mark it verified",
    ).save()
    report = import_pilot_workbook(conn, out, expected_selected=36)
    states = conn.execute(
        text(
            "SELECT DISTINCT verification_state FROM pilot_collected_source "
            "WHERE submission_id = :s"
        ),
        {"s": report.submission_id},
    ).scalars()
    assert set(states) == {SourceCandidateState.PENDING.value}


def test_a_decision_without_a_reason_is_refused(conn: Connection, imported: Any) -> None:
    actor = verification.Actor(user_id=_actor(conn))
    candidate = _candidate(conn, "S0001")
    with pytest.raises(verification.CandidateDecisionRefusedError, match="reason"):
        verification.verify_candidate(conn, candidate_id=candidate, actor=actor, reason="   ")


def test_the_database_refuses_a_decision_with_no_actor(conn: Connection, imported: Any) -> None:
    """Belt and braces: the service requires it, and so does the CHECK.

    Bypassing the service is the realistic failure -- a migration, a fix-up script, a
    future endpoint -- so the constraint has to hold on its own.
    """
    candidate = _candidate(conn, "S0001")
    with expect_violation(conn, "decision_records_who_when_why"):
        conn.execute(
            text(
                "UPDATE pilot_collected_source SET verification_state = 'VERIFIED' WHERE id = :id"
            ),
            {"id": candidate},
        )


def test_verifying_records_actor_time_reason_and_audit(conn: Connection, imported: Any) -> None:
    actor_id = _actor(conn)
    candidate = _candidate(conn, "S0001")
    verification.verify_candidate(
        conn,
        candidate_id=candidate,
        actor=verification.Actor(user_id=actor_id),
        reason="Linked from the programme page; fee table matches the collector's note.",
    )

    row = conn.execute(
        text(
            "SELECT verification_state, verified_at, verified_by, verification_reason "
            "FROM pilot_collected_source WHERE id = :id"
        ),
        {"id": candidate},
    ).one()
    assert row.verification_state == SourceCandidateState.VERIFIED.value
    assert row.verified_at is not None
    assert row.verified_by == actor_id
    assert "programme page" in row.verification_reason

    audit = conn.execute(
        text(
            "SELECT action, actor_id, object_type, reason, seq, row_hash FROM audit_log "
            "WHERE object_id = :id ORDER BY seq DESC LIMIT 1"
        ),
        {"id": candidate},
    ).one()
    assert audit.action == PilotCandidateAction.VERIFY.value
    assert audit.actor_id == actor_id
    assert audit.object_type == "pilot_collected_source"
    assert audit.row_hash, "the decision did not join the audit hash chain"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (verification.reject_candidate, SourceCandidateState.REJECTED),
        (verification.flag_candidate_for_review, SourceCandidateState.NEEDS_REVIEW),
    ],
)
def test_reject_and_needs_review_are_recorded_the_same_way(
    conn: Connection, imported: Any, action: Any, expected: SourceCandidateState
) -> None:
    candidate = _candidate(conn, "S0002")
    action(
        conn,
        candidate_id=candidate,
        actor=verification.Actor(user_id=_actor(conn)),
        reason="Third-party portal; no authorisation statement found on the official site.",
    )
    state = conn.execute(
        text("SELECT verification_state FROM pilot_collected_source WHERE id = :id"),
        {"id": candidate},
    ).scalar_one()
    assert state == expected.value
    assert (
        conn.execute(
            text("SELECT count(*) FROM audit_log WHERE object_id = :id"), {"id": candidate}
        ).scalar_one()
        == 1
    )


def test_a_matching_verified_host_is_evidence_and_not_a_decision(
    conn: Connection, imported: Any
) -> None:
    """The temptation this design refuses.

    A verified domain for the institution covers the candidate's host, which is the
    strongest automatic signal available -- and the candidate is still `PENDING`. A
    university's own domain also carries news articles, student societies and staff
    home pages; hostname is where a page lives, not what it is.
    """
    targets, _out, _report = imported
    _verified_domain(conn, targets[0], "invented.example.ac.uk")

    detail = verification.candidate_detail(conn, _candidate(conn, "S0001"))
    assert detail.domain.matches_verified_domain
    assert detail.domain.matched_domain_host == "invented.example.ac.uk"
    assert detail.verification_state == SourceCandidateState.PENDING.value

    queued = conn.execute(
        text(
            "SELECT host_matches_verified_domain, verification_state "
            "FROM pilot_source_verification_queue WHERE source_ref = 'S0001'"
        )
    ).one()
    assert queued.host_matches_verified_domain is True
    assert queued.verification_state == SourceCandidateState.PENDING.value


def test_no_domain_of_another_institution_is_ever_matched(conn: Connection, imported: Any) -> None:
    """No institutional identity is inferred, by hostname or by anything else."""
    targets, _out, _report = imported
    _verified_domain(conn, targets[1], "invented.example.ac.uk")

    detail = verification.candidate_detail(conn, _candidate(conn, "S0001"))
    assert detail.domain.matched_domain_host is None
    assert not detail.domain.matches_verified_domain


# ===========================================================================
# 18-20. Promotion: a source must earn its eligibility
# ===========================================================================


def test_an_unverified_candidate_cannot_be_registered(conn: Connection, imported: Any) -> None:
    with pytest.raises(verification.CandidateDecisionRefusedError, match="verify it"):
        verification.register_verified_candidate(
            conn,
            candidate_id=_candidate(conn, "S0001"),
            source_category="TUITION_FEES",
            actor=verification.Actor(user_id=_actor(conn)),
            reason="skipping the queue",
        )


def test_registration_requires_a_verified_domain_first(conn: Connection, imported: Any) -> None:
    """The middle arrow of the promotion chain cannot be skipped.

    Accepting a URL is not the same act as confirming the institution owns the host,
    and one person doing the first does not do the second.
    """
    candidate = _candidate(conn, "S0001")
    actor = verification.Actor(user_id=_actor(conn))
    verification.verify_candidate(
        conn, candidate_id=candidate, actor=actor, reason="Right page, right institution."
    )
    with pytest.raises(
        verification.CandidateDecisionRefusedError, match="verified official domain"
    ):
        verification.register_verified_candidate(
            conn,
            candidate_id=candidate,
            source_category="TUITION_FEES",
            actor=actor,
            reason="registering it",
        )


def test_a_registered_candidate_becomes_a_candidate_mapping_not_a_verified_source(
    conn: Connection, imported: Any
) -> None:
    """The end of the path this phase builds, and where it deliberately stops.

    Registration creates a `source_mapping` in `CANDIDATE` state. It creates no
    `source` at all, so nothing has become publication-eligible: C27's
    `source_eligibility_is_earned` still has to be satisfied by a promotion that
    happens later, deliberately, and not here.
    """
    targets, _out, _report = imported
    _verified_domain(conn, targets[0], "invented.example.ac.uk")
    candidate = _candidate(conn, "S0001")
    actor = verification.Actor(user_id=_actor(conn))
    verification.verify_candidate(
        conn, candidate_id=candidate, actor=actor, reason="Official fee table for this programme."
    )

    mapping_id = verification.register_verified_candidate(
        conn,
        candidate_id=candidate,
        source_category="TUITION_FEES",
        actor=actor,
        reason="Registering the fee page for collection.",
    )
    mapping = conn.execute(
        text(
            "SELECT verification_status, publication_eligibility, promoted_source_id "
            "FROM source_mapping WHERE id = :id"
        ),
        {"id": mapping_id},
    ).one()
    assert mapping.verification_status == "CANDIDATE"
    assert mapping.publication_eligibility == "NOT_ELIGIBLE"
    assert mapping.promoted_source_id is None
    assert conn.execute(text("SELECT count(*) FROM source")).scalar_one() == 0


def test_a_source_cannot_be_born_official_verified(conn: Connection, imported: Any) -> None:
    """C27, re-asserted from the staging side.

    Importing a workbook must not become a way around the promotion order, so the
    shortcut is tried directly: insert a `source` claiming the class the candidate's
    URL would eventually earn.
    """
    with expect_violation(conn, "source_eligibility_is_earned|eligib"):
        conn.execute(
            text(
                "INSERT INTO source (id, url, normalized_url, url_sha256, host, "
                "source_type, publication_eligibility) "
                "VALUES (:id, :url, :url, :sha, 'invented.example.ac.uk', "
                "'OFFICIAL_PAGE', 'OFFICIAL_VERIFIED')"
            ),
            {
                "id": uuid.uuid4(),
                "url": "https://invented.example.ac.uk/fees",
                "sha": hashlib.sha256(b"https://invented.example.ac.uk/fees").hexdigest(),
            },
        )


# ===========================================================================
# 21-22. The queue report
# ===========================================================================


def test_the_queue_counts_by_institution_source_type_and_degree_level(
    conn: Connection, imported: Any
) -> None:
    targets, _out, _report = imported
    actor = verification.Actor(user_id=_actor(conn))
    verification.reject_candidate(
        conn,
        candidate_id=_candidate(conn, "S0002"),
        actor=actor,
        reason="Third-party portal with no authorisation statement.",
    )

    summary = queue.queue_summary(conn)
    assert summary.overall.total == 2
    assert summary.overall.pending == 1
    assert summary.overall.rejected == 1
    assert summary.overall.open == 1

    assert set(summary.by_source_type) == {"TUITION_FEES", "ENTRY_REQUIREMENTS"}
    assert summary.by_source_type["TUITION_FEES"].pending == 1
    assert summary.by_source_type["ENTRY_REQUIREMENTS"].rejected == 1
    assert set(summary.by_degree_level) == {"TAUGHT_POSTGRADUATE"}
    assert len(summary.by_institution) == 1

    assert queue.open_count(conn) == 1
    open_rows = queue.open_candidates(conn)
    assert [row.source_ref for row in open_rows] == ["S0001"]
    assert open_rows[0].collector_checked_at is not None
    assert open_rows[0].collector_notes


def test_the_queue_excludes_unselected_institutions_by_default(
    conn: Connection, completed: Any
) -> None:
    """A candidate for an institution the client did not pick is not blocking anything."""
    targets, out = completed
    Filler(out).add(
        "Official_Sources",
        target_institution_id=str(targets[39]),  # not among the 36 marked
        source_ref="S0009",
        source_type="UNIVERSITY_HOME",
        official_url="https://unselected.example.ac.uk/",
        checked_at="2027-01-15",
    ).save()
    import_pilot_workbook(conn, out, expected_selected=36)

    assert queue.queue_summary(conn).overall.total == 2
    assert queue.queue_summary(conn, selected_only=False).overall.total == 3


# ===========================================================================
# 23-28. U14 through the workbook
#
# Every rule below is also a database CHECK. Catching it here is what stops a
# constraint violation surfacing on row 400 of a batch insert, weeks after the
# collector closed the page they would need to look at to fix it.
# ===========================================================================


def _tuition_row(targets: list[uuid.UUID], **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "target_institution_id": str(targets[0]),
        "program_ref": "P0001",
        "academic_year": "2029/30",
        "student_category_code": "INTERNATIONAL",
        "applicant_scope_hint": "UNIVERSAL",
        "amount_status": "PUBLISHED",
        "source_ref": "S0001",
        "currency_code": "GBP",
        "billing_unit_code": "PER_YEAR",
    }
    row.update(overrides)
    return row


def _validate_with(conn: Connection, completed: Any, **overrides: Any) -> Any:
    from app.domains.onboarding.pilot_workbook import validate_workbook

    targets, out = completed
    Filler(out).add("Tuition", **_tuition_row(targets, **overrides)).save()
    return validate_workbook(conn, out, expected_selected=36)


def test_a_range_imports_with_both_ends_and_no_midpoint(conn: Connection, imported: Any) -> None:
    """The case that motivated U14, carried end to end into staging.

    The staged row keeps 28000 and 32000. Nothing derives 30000 -- not the importer,
    not the schema -- because a fee of "28,000-32,000" is not 30,000 and a consultant
    quoting the average would be quoting a figure no university published.
    """
    _targets, _out, report = imported
    row = conn.execute(
        text(
            "SELECT amount_kind, amount_min, amount_max, official_text "
            "FROM pilot_collected_fact "
            "WHERE submission_id = :s AND sheet_name = 'Tuition' AND amount_kind IS NOT NULL"
        ),
        {"s": report.submission_id},
    ).one()
    assert row.amount_kind == "RANGE"
    assert (int(row.amount_min), int(row.amount_max)) == (28000, 32000)
    assert "28,000-32,000" in row.official_text
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM pilot_collected_fact "
                "WHERE amount_min = 30000 OR amount_max = 30000"
            )
        ).scalar_one()
        == 0
    )


def test_a_checked_absence_survives_the_import(conn: Connection, imported: Any) -> None:
    """Blank vs OFFICIALLY_NOT_PUBLISHED, the distinction D2 exists for.

    The second Tuition row was checked and the page publishes nothing. It must arrive
    with no kind, no figures, its wording, and its source -- not as `NOT_CHECKED`,
    which would throw away the work, and not as a zero.
    """
    _targets, _out, report = imported
    row = conn.execute(
        text(
            "SELECT field_status, amount_kind, amount_min, amount_max, source_ref, official_text "
            "FROM pilot_collected_fact "
            "WHERE submission_id = :s AND sheet_name = 'Tuition' AND amount_kind IS NULL"
        ),
        {"s": report.submission_id},
    ).one()
    assert row.field_status == "OFFICIALLY_NOT_PUBLISHED"
    assert row.amount_min is None and row.amount_max is None
    assert row.source_ref == "S0001"
    assert "not yet been set" in row.official_text


def test_an_exact_fee_with_two_different_figures_is_caught(
    conn: Connection, completed: Any
) -> None:
    """And the message names the right fix rather than just the rule."""
    report = _validate_with(
        conn, completed, amount_kind="EXACT", amount_min=28000, amount_max=32000
    )
    issues = [i for i in report.issues if i.code is IssueCode.TUITION_AMOUNT_SHAPE_INCONSISTENT]
    assert len(issues) == 1
    assert "RANGE" in issues[0].message
    assert not report.is_importable


def test_a_variable_fee_must_quote_the_page(conn: Connection, completed: Any) -> None:
    report = _validate_with(conn, completed, amount_kind="VARIABLE")
    assert any(
        i.code is IssueCode.TUITION_AMOUNT_SHAPE_INCONSISTENT and "official_text" in i.message
        for i in report.issues
    )


def test_a_variable_fee_with_wording_is_accepted(conn: Connection, completed: Any) -> None:
    """Non-vacuity: VARIABLE is a legitimate published fee, not a rejected one."""
    report = _validate_with(
        conn,
        completed,
        amount_kind="VARIABLE",
        official_text="Fees vary by module selection; see the fee calculator.",
    )
    assert report.is_importable, [str(i) for i in report.errors]


def test_officially_not_published_is_not_an_amount_kind(conn: Connection, completed: Any) -> None:
    """The confusion U14 is most likely to invite, named in the error message.

    `OFFICIALLY_NOT_PUBLISHED` is a status -- "the page says nothing about fees".
    `VARIABLE` is a shape -- "the page addresses fees but gives no figure". Letting a
    collector write the status here would quietly merge two different facts.
    """
    report = _validate_with(conn, completed, amount_kind="OFFICIALLY_NOT_PUBLISHED")
    issues = [i for i in report.issues if i.code is IssueCode.TUITION_AMOUNT_KIND_UNKNOWN]
    assert len(issues) == 1
    assert "amount_status" in issues[0].message


def test_a_fee_written_as_text_is_caught(conn: Connection, completed: Any) -> None:
    """'GBP 28,000 per year' in the amount cell, which is what people actually type."""
    report = _validate_with(conn, completed, amount_kind="EXACT", amount_min="GBP 28,000 per year")
    assert any(i.code is IssueCode.TUITION_AMOUNT_NOT_A_NUMBER for i in report.issues)


def test_a_from_fee_may_not_carry_a_ceiling(conn: Connection, completed: Any) -> None:
    report = _validate_with(conn, completed, amount_kind="FROM", amount_min=24500, amount_max=30000)
    assert any(
        i.code is IssueCode.TUITION_AMOUNT_SHAPE_INCONSISTENT and "RANGE" in i.message
        for i in report.issues
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _actor(conn: Connection) -> uuid.UUID:
    existing: uuid.UUID | None = conn.execute(
        text("SELECT id FROM app_user WHERE email = 'pilot-reviewer@example.test'")
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    actor_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO app_user (id, email, display_name) "
            "VALUES (:id, 'pilot-reviewer@example.test', 'Pilot Reviewer')"
        ),
        {"id": actor_id},
    )
    return actor_id


def _candidate(conn: Connection, source_ref: str) -> uuid.UUID:
    candidate_id: uuid.UUID = conn.execute(
        text(
            "SELECT id FROM pilot_collected_source WHERE source_ref = :ref "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"ref": source_ref},
    ).scalar_one()
    return candidate_id


def _fresh_submission(conn: Connection) -> uuid.UUID:
    """An empty submission, so a cross-submission citation has somewhere to be tried."""
    submission_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "file_byte_size, template_version, selected_university_count) "
            "VALUES (:id, :sha, 'empty.xlsx', 1, :ver, 0)"
        ),
        {
            "id": submission_id,
            "sha": hashlib.sha256(submission_id.bytes).hexdigest(),
            "ver": TEMPLATE_VERSION,
        },
    )
    return submission_id


def _verified_domain(conn: Connection, target_institution_id: uuid.UUID, host: str) -> uuid.UUID:
    """A host already confirmed as officially this institution's.

    Built the long way -- method, evidence, actor and timestamp -- because
    `verified_domain_records_its_basis` refuses a verification that cannot say who
    made it on what grounds, and a fixture that shortcut that would be testing a
    schema we do not have.
    """
    domain_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, covers_subdomains, "
            "verification_status, verification_method, verification_evidence, verified_at, "
            "verified_by, is_active) VALUES (:id, :target, :host, true, 'VERIFIED_OFFICIAL', "
            "'GOVERNMENT_REGISTRY', 'Invented registry record', now(), :actor, true)"
        ),
        {
            "id": domain_id,
            "target": target_institution_id,
            "host": host,
            "actor": _actor(conn),
        },
    )
    return domain_id
