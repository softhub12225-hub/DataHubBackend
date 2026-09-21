"""Step 4: the fifteen required onboarding guarantees, against a live database.

These tests speak SQL and call the service functions directly, in a transaction that
is always rolled back. What is under test is mostly *what the database refuses*, so
going through an ORM that validated first would risk a green test that proved
nothing.

The client's real workbook is used where the assertion is about the client's real
scope (the 181 count, the regional distribution). Those tests skip with an explicit
message when the file is not present, rather than asserting against a copy of the
numbers that could drift from the file.

Every other fixture institution is invented. No real university appears in one: a
seeded real fact would be a published claim with no provenance, which is precisely
what this platform exists to prevent.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import (
    ActorType,
    DegreeScope,
    DomainVerificationMethod,
    OfficialVerificationStatus,
    OnboardingStatus,
    SourceCategory,
)
from app.domains.onboarding import verification
from app.domains.onboarding.coverage import (
    REQUIRED_COVERAGE,
    CoverageStatus,
    coverage_report,
    coverage_totals,
)
from app.domains.onboarding.importer import (
    AUDIT_ACTION_IMPORT,
    TargetListConflictError,
    import_target_list,
)
from app.domains.onboarding.naming import normalize_institution_name
from app.domains.onboarding.urls import validate_source_url
from tests.integration.conftest import (
    CLIENT_DISTRIBUTION,
    CLIENT_TOTAL,
    Graph,
    expect_violation,
)
from tests.test_onboarding_parsing import SAMPLE_ROWS, write_workbook

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(conn: Connection) -> verification.Actor:
    """An operator to attribute decisions to. A verification needs a named human."""
    user_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO app_user (id, email, display_name) "
            "VALUES (:id, :email, 'Test Reviewer')"
        ),
        {"id": user_id, "email": f"reviewer-{user_id.hex[:8]}@example.test"},
    )
    return verification.Actor(user_id=user_id)


@pytest.fixture
def imported(conn: Connection, client_workbook: Path) -> Any:
    """The client's list, imported into the test transaction."""
    return import_target_list(conn, client_workbook)


def make_target(
    conn: Connection,
    *,
    name: str,
    list_id: uuid.UUID | None = None,
    destination: str = "GB",
) -> uuid.UUID:
    """A minimal target institution, with the list row it needs."""
    if list_id is None:
        list_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO target_list (id, list_name, list_version, file_name, "
                "file_sha256, file_byte_size, sheet_name, imported_row_count) "
                "VALUES (:id, 'Invented List', :ver, 'invented.xlsx', :sha, 1024, "
                "'targets', 1)"
            ),
            {"id": list_id, "ver": list_id.hex[:8], "sha": f"{list_id.hex}{list_id.hex}"},
        )
    target_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
            "latest_list_id, destination_code) "
            "VALUES (:id, :key, :list, :list, :dest)"
        ),
        {
            "id": target_id,
            "key": normalize_institution_name(name),
            "list": list_id,
            "dest": destination,
        },
    )
    return target_id


def make_domain(
    conn: Connection,
    *,
    target_id: uuid.UUID,
    host: str,
    university_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A CANDIDATE host. Nothing here is verified; that takes a human decision."""
    domain_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, university_id, host) "
            "VALUES (:id, :target, :uni, :host)"
        ),
        {"id": domain_id, "target": target_id, "uni": university_id, "host": host},
    )
    return domain_id


def make_mapping(
    conn: Connection,
    *,
    target_id: uuid.UUID,
    url: str,
    category: SourceCategory,
    domain_id: uuid.UUID | None = None,
    degree_scopes: tuple[DegreeScope, ...] = (),
    university_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A CANDIDATE source mapping. Never fetched."""
    parsed = validate_source_url(url)
    mapping_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source_mapping (id, target_institution_id, university_id, "
            "source_category, url, normalized_url, url_sha256, host, official_domain_id) "
            "VALUES (:id, :target, :uni, :cat, :url, :norm, :sha, :host, :domain)"
        ),
        {
            "id": mapping_id,
            "target": target_id,
            "uni": university_id,
            "cat": category.value,
            "url": parsed.original,
            "norm": parsed.normalized,
            "sha": parsed.sha256,
            "host": parsed.host,
            "domain": domain_id,
        },
    )
    for scope in degree_scopes:
        conn.execute(
            text(
                "INSERT INTO source_degree_scope (source_mapping_id, degree_scope) "
                "VALUES (:id, :scope)"
            ),
            {"id": mapping_id, "scope": scope.value},
        )
    return mapping_id


def verify_host(
    conn: Connection, domain_id: uuid.UUID, actor: verification.Actor, *, subdomains: bool = False
) -> None:
    verification.verify_domain(
        conn,
        domain_id=domain_id,
        actor=actor,
        method=DomainVerificationMethod.GOVERNMENT_REGISTRY,
        evidence="Listed in the invented national register of institutions",
        reason="onboarding",
        covers_subdomains=subdomains,
    )


def scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


# ===========================================================================
# 1. Exactly 181 target institutions are imported
# ===========================================================================


def test_the_client_list_imports_exactly_its_stated_number_of_institutions(
    conn: Connection, imported: Any
) -> None:
    """Requirement 16.1. The count is not hardcoded in the importer: it comes from
    the file, and the file's own preamble and summary block are cross-checked
    against the rows actually read."""
    assert imported.rows_read == CLIENT_TOTAL
    assert imported.entries_written == CLIENT_TOTAL
    assert imported.institutions_created == CLIENT_TOTAL

    assert scalar(conn, "SELECT count(*) FROM target_institution") == CLIENT_TOTAL
    assert scalar(conn, "SELECT count(*) FROM target_list_entry") == CLIENT_TOTAL

    # The file declared this about itself, and the importer stored both numbers so a
    # later reader can see they agreed.
    declared, actual = conn.execute(
        text("SELECT declared_row_count, imported_row_count FROM target_list")
    ).one()
    assert declared == actual == CLIENT_TOTAL


# ===========================================================================
# 2. The regional distribution matches exactly
# ===========================================================================


def test_the_regional_distribution_matches_the_client_specification(
    conn: Connection, imported: Any
) -> None:
    """Requirement 16.2."""
    rows = conn.execute(
        text(
            "SELECT destination_code, count(*) FROM target_institution " "GROUP BY destination_code"
        )
    ).all()
    assert {row[0]: row[1] for row in rows} == CLIENT_DISTRIBUTION
    assert sum(CLIENT_DISTRIBUTION.values()) == CLIENT_TOTAL

    # Nothing was left unplaced: every region in the file mapped to a destination.
    assert (
        scalar(conn, "SELECT count(*) FROM target_institution WHERE destination_code IS NULL") == 0
    )


def test_the_pilot_scope_is_not_invented(conn: Connection, imported: Any) -> None:
    """Requirement 4. The list has 57 institutions across the pilot destinations
    while the PRD speaks of at least 36. Which 36 is the client's decision, so
    `pilot_wave` is NULL for all of them rather than guessed."""
    pilot_destinations = ("GB", "HK", "MO")
    in_pilot_markets = scalar(
        conn,
        "SELECT count(*) FROM target_institution WHERE destination_code = ANY(:codes)",
        codes=list(pilot_destinations),
    )
    assert in_pilot_markets == sum(CLIENT_DISTRIBUTION[code] for code in pilot_destinations)
    assert in_pilot_markets == 57

    assert scalar(conn, "SELECT count(*) FROM target_institution WHERE pilot_wave IS NOT NULL") == 0


# ===========================================================================
# 3. Re-running the import does not duplicate anything
# ===========================================================================


def test_reimporting_the_same_file_writes_nothing(
    conn: Connection, client_workbook: Path, imported: Any
) -> None:
    """Requirement 16.3. "Run it again" is a normal operational reflex."""
    before = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("target_list", "target_institution", "target_list_entry", "target_list_diff")
    }

    again = import_target_list(conn, client_workbook)

    assert again.already_imported is True
    assert again.target_list_id == imported.target_list_id
    assert again.rows_read == 0

    after = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("target_list", "target_institution", "target_list_entry", "target_list_diff")
    }
    assert after == before


def test_the_same_version_from_different_content_is_refused_not_overwritten(
    conn: Connection, tmp_path: Path
) -> None:
    """Overwriting would destroy the history the difference report is computed from."""
    first = write_workbook(tmp_path / "v1.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, first)

    # Same declared name and version, one row fewer: a different file claiming to be
    # the same version.
    second = write_workbook(
        tmp_path / "v1b.xlsx",
        rows=SAMPLE_ROWS[:-1],
        preamble=["A List", "数据来源：x v1.0"],
    )
    with pytest.raises(TargetListConflictError, match="already imported"):
        import_target_list(conn, second)

    assert scalar(conn, "SELECT count(*) FROM target_list") == 1


# ===========================================================================
# 4. No duplicate universities are created
# ===========================================================================


def test_no_duplicate_target_institutions_and_no_universities_at_all(
    conn: Connection, imported: Any
) -> None:
    """Requirement 16.4."""
    distinct_keys = scalar(conn, "SELECT count(DISTINCT match_key) FROM target_institution")
    assert distinct_keys == CLIENT_TOTAL

    # One entry per institution per list version, enforced by a unique constraint as
    # well as by the importer.
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM (SELECT target_list_id, target_institution_id "
            "FROM target_list_entry GROUP BY 1, 2 HAVING count(*) > 1) d",
        )
        == 0
    )


def test_the_database_refuses_a_second_entry_for_one_institution_in_one_list(
    conn: Connection, tmp_path: Path
) -> None:
    path = write_workbook(tmp_path / "dupe.xlsx", preamble=["A List", "数据来源：x v1.0"])
    report = import_target_list(conn, path)
    institution_id, list_id = conn.execute(
        text("SELECT target_institution_id, target_list_id FROM target_list_entry LIMIT 1")
    ).one()

    with expect_violation(conn, "uq_target_list_entry"):
        conn.execute(
            text(
                "INSERT INTO target_list_entry (target_list_id, target_institution_id, "
                "source_row, qs_name, qs_name_normalized, region_label) "
                "VALUES (:list, :inst, 9999, 'Some Name', 'some name', '英国')"
            ),
            {"list": list_id, "inst": institution_id},
        )
    assert report.entries_written == len(SAMPLE_ROWS)


# ===========================================================================
# 5. No automatically verified canonical facts
# ===========================================================================


def test_the_import_creates_no_canonical_or_governance_rows(
    conn: Connection, imported: Any
) -> None:
    """Requirement 16.5 and 16.14, and the central rule of the whole platform.

    The client's spreadsheet establishes scope. It must not become a university, a
    programme, a claim, a proposal or a ranking.
    """
    must_stay_empty = (
        "university",
        "campus",
        "faculty",
        "program",
        "program_offering",
        "intake",
        "application_round",
        "application_deadline",
        "admission_requirement",
        "language_requirement",
        "tuition",
        "ranking_entry",
        "ranking_edition",
        "entity_alias",
        "fact_absence",
        "entity_head",
        "field_current",
        "entity_version",
        "field_provenance",
        "change_event",
        "field_claim",
        "claim_resolution",
        "change_proposal",
        "change_proposal_item",
        "review_task",
        "snapshot",
        "extraction",
        "content_blob",
        "fetch_run",
        "fetch_attempt",
        "source",
    )
    populated = {table: scalar(conn, f"SELECT count(*) FROM {table}") for table in must_stay_empty}
    assert {table: count for table, count in populated.items() if count} == {}


def test_no_target_is_matched_to_a_university_by_the_import(
    conn: Connection, imported: Any
) -> None:
    """Identity resolution is a human decision, recorded with an actor and a time."""
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM target_institution WHERE matched_university_id IS NOT NULL",
        )
        == 0
    )
    # And every target is at the start of the funnel.
    statuses = {
        row[0]: row[1]
        for row in conn.execute(
            text("SELECT onboarding_status, count(*) FROM target_institution GROUP BY 1")
        ).all()
    }
    assert statuses == {OnboardingStatus.NOT_STARTED.value: CLIENT_TOTAL}


def test_a_target_cannot_reach_source_mapping_without_a_matched_university(
    conn: Connection,
) -> None:
    """The structural reason a QS row cannot become a collectable institution."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    for status in (
        OnboardingStatus.SOURCE_MAPPING,
        OnboardingStatus.READY_FOR_COLLECTION,
        OnboardingStatus.ACTIVE,
    ):
        with expect_violation(conn, "advanced_status_requires_a_match"):
            conn.execute(
                text("UPDATE target_institution SET onboarding_status = :status WHERE id = :id"),
                {"status": status.value, "id": target_id},
            )


def test_a_match_must_name_who_made_it_and_when(conn: Connection, graph: Graph) -> None:
    target_id = make_target(conn, name="Rivermouth University")
    with expect_violation(conn, "match_records_who_and_when"):
        conn.execute(
            text("UPDATE target_institution SET matched_university_id = :uni WHERE id = :id"),
            {"uni": graph["university_a"], "id": target_id},
        )


# ===========================================================================
# 6. QS rank and name stay traceable to a list version
# ===========================================================================


def test_every_qs_value_is_traceable_to_a_file_hash_and_a_row(
    conn: Connection, imported: Any
) -> None:
    """Requirement 16.6. "Where did this rank come from?" is answerable in SQL."""
    row = conn.execute(
        text(
            "SELECT e.qs_name, e.qs_rank, e.qs_score, e.source_row, "
            "       l.list_name, l.list_version, l.file_sha256, l.published_at "
            "  FROM target_list_entry e "
            "  JOIN target_list l ON l.id = e.target_list_id "
            " ORDER BY e.qs_rank, e.source_row LIMIT 1"
        )
    ).one()
    assert row.qs_rank is not None
    assert row.source_row >= 1
    assert row.list_version
    assert len(row.file_sha256) == 64
    # The workbook stated its own publication date; it was transcribed, not invented.
    assert str(row.published_at) == "2026-06-18"

    # No QS attribute lives on the durable institution row.
    columns = {
        name
        for (name,) in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'target_institution'"
            )
        ).all()
    }
    assert not {column for column in columns if column.startswith("qs_")}


def test_tied_ranks_are_preserved_because_rank_is_not_an_identifier(
    conn: Connection, imported: Any
) -> None:
    """The client's list contains ties; a unique constraint on rank would reject it."""
    tied = scalar(
        conn,
        "SELECT count(*) FROM (SELECT qs_rank FROM target_list_entry "
        "GROUP BY qs_rank HAVING count(*) > 1) t",
    )
    assert tied > 0


# ===========================================================================
# 7. An institution in several list versions
# ===========================================================================


def test_an_institution_in_two_list_versions_keeps_one_record_and_two_entries(
    conn: Connection, tmp_path: Path, actor: verification.Actor, graph: Graph
) -> None:
    """Requirement 16.7. Onboarding progress must not fork across list versions."""
    first = write_workbook(tmp_path / "y1.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, first)

    target_id = scalar(
        conn,
        "SELECT id FROM target_institution WHERE match_key = :key",
        key=normalize_institution_name("Northgate Institute of Technology"),
    )
    # Do some onboarding work, so the re-import has something to preserve.
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, "
            "matched_at = now(), matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_a"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor)

    # A second version: same institutions, one rank moved.
    rows = [list(row) for row in SAMPLE_ROWS]
    rows[0][1] = 7
    second = write_workbook(
        tmp_path / "y2.xlsx", rows=rows, preamble=["A List", "数据来源：x v2.0"]
    )
    report = import_target_list(conn, second)

    assert report.institutions_created == 0
    assert scalar(conn, "SELECT count(*) FROM target_institution") == len(SAMPLE_ROWS)
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM target_list_entry WHERE target_institution_id = :id",
            id=target_id,
        )
        == 2
    )

    # The accumulated work survived.
    kept = conn.execute(
        text(
            "SELECT matched_university_id, onboarding_status, is_in_current_list "
            "FROM target_institution WHERE id = :id"
        ),
        {"id": target_id},
    ).one()
    assert kept.matched_university_id == graph["university_a"]
    assert kept.onboarding_status == OnboardingStatus.DOMAIN_VERIFIED.value
    assert kept.is_in_current_list is True
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM official_domain WHERE target_institution_id = :id "
            "AND verification_status = 'VERIFIED_OFFICIAL'",
            id=target_id,
        )
        == 1
    )

    # Both versions' values remain readable, side by side.
    history = conn.execute(
        text(
            "SELECT l.list_version, e.qs_rank FROM target_list_entry e "
            "JOIN target_list l ON l.id = e.target_list_id "
            "WHERE e.target_institution_id = :id ORDER BY l.list_version"
        ),
        {"id": target_id},
    ).all()
    assert [(row.list_version, row.qs_rank) for row in history] == [("1.0", 10), ("2.0", 7)]

    assert report.diffs.get("RANK_CHANGED") == 1


def test_the_difference_report_names_every_kind_of_change(conn: Connection, tmp_path: Path) -> None:
    """Requirement 13. Six change kinds, answered from stored data."""
    first = write_workbook(tmp_path / "d1.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, first)

    rows = [list(row) for row in SAMPLE_ROWS]
    rows[0][1] = 7  # RANK_CHANGED
    rows[1][5] = 91.0  # SCORE_CHANGED
    rows[2][2] = "Fairhaven College of Arts"  # a new institution + a removal
    rows[3][3] = "中国香港"  # REGION_CHANGED
    rows[3][4] = "Hong Kong SAR, China"
    rows.append([5, 60, "Southbank University", "英国", "United Kingdom", 60.0])  # ADDED_TARGET

    second = write_workbook(
        tmp_path / "d2.xlsx", rows=rows, preamble=["A List", "数据来源：x v2.0"]
    )
    report = import_target_list(conn, second)

    assert report.diffs.get("RANK_CHANGED") == 1
    assert report.diffs.get("SCORE_CHANGED") == 1
    assert report.diffs.get("REGION_CHANGED") == 1
    # The renamed college is a new target plus a removal, not a silent merge: a
    # conservative match key will not equate two different strings, and a wrong
    # merge is worse than a visible pair for a human to resolve.
    assert report.diffs.get("ADDED_TARGET") == 2
    assert report.diffs.get("REMOVED_FROM_NEW_LIST") == 1

    stored = {
        row[0]: row[1]
        for row in conn.execute(
            text(
                "SELECT change_kind, count(*) FROM target_list_diff "
                "WHERE target_list_id = :id GROUP BY 1"
            ),
            {"id": report.target_list_id},
        ).all()
    }
    assert stored == report.diffs

    # A rank change records both values.
    change = conn.execute(
        text(
            "SELECT before_value, after_value FROM target_list_diff "
            "WHERE target_list_id = :id AND change_kind = 'RANK_CHANGED'"
        ),
        {"id": report.target_list_id},
    ).one()
    assert change.before_value == {"qs_rank": 10}
    assert change.after_value == {"qs_rank": 7}


def test_the_difference_report_is_append_only(conn: Connection, tmp_path: Path) -> None:
    path = write_workbook(tmp_path / "ap.xlsx", preamble=["A List", "数据来源：x v1.0"])
    report = import_target_list(conn, path)
    diff_id = scalar(conn, "SELECT id FROM target_list_diff LIMIT 1")

    with expect_violation(conn, "append-only"):
        conn.execute(
            text("UPDATE target_list_diff SET change_kind = 'NAME_CHANGED' WHERE id = :id"),
            {"id": diff_id},
        )
    with expect_violation(conn, "append-only"):
        conn.execute(text("DELETE FROM target_list_diff WHERE id = :id"), {"id": diff_id})
    assert report.diffs


def test_a_list_entry_is_append_only(conn: Connection, tmp_path: Path) -> None:
    """What a supplied file said is history: revising it destroys the only record."""
    path = write_workbook(tmp_path / "ae.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, path)
    entry_id = scalar(conn, "SELECT id FROM target_list_entry LIMIT 1")

    with expect_violation(conn, "append-only"):
        conn.execute(
            text("UPDATE target_list_entry SET qs_rank = 1 WHERE id = :id"), {"id": entry_id}
        )
    with expect_violation(conn, "append-only"):
        conn.execute(text("DELETE FROM target_list_entry WHERE id = :id"), {"id": entry_id})


# ===========================================================================
# 8. Removal from a new list destroys nothing
# ===========================================================================


def test_removal_from_a_new_list_deletes_no_canonical_data(
    conn: Connection, tmp_path: Path, actor: verification.Actor, graph: Graph
) -> None:
    """Requirement 16.8. Scope shrinking is a statement about scope, not a licence
    to destroy governed data."""
    first = write_workbook(tmp_path / "r1.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, first)

    dropped_name = SAMPLE_ROWS[-1][2]
    assert isinstance(dropped_name, str)
    target_id = scalar(
        conn,
        "SELECT id FROM target_institution WHERE match_key = :key",
        key=normalize_institution_name(dropped_name),
    )

    # Give it a matched university, a verified host and a mapped source.
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, "
            "matched_at = now(), matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_b"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="eastvale.edu.mo")
    verify_host(conn, domain_id, actor)
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://eastvale.edu.mo/",
        category=SourceCategory.UNIVERSITY_HOME,
        domain_id=domain_id,
    )

    # A later list simply does not mention it.
    second = write_workbook(
        tmp_path / "r2.xlsx", rows=SAMPLE_ROWS[:-1], preamble=["A List", "数据来源：x v2.0"]
    )
    report = import_target_list(conn, second)

    assert report.diffs.get("REMOVED_FROM_NEW_LIST") == 1

    state = conn.execute(
        text(
            "SELECT is_in_current_list, removed_from_list_id, matched_university_id "
            "FROM target_institution WHERE id = :id"
        ),
        {"id": target_id},
    ).one()
    assert state.is_in_current_list is False
    assert state.removed_from_list_id == report.target_list_id
    # The point of the test: the canonical link is intact.
    assert state.matched_university_id == graph["university_b"]

    assert (
        scalar(conn, "SELECT count(*) FROM university WHERE id = :id", id=graph["university_b"])
        == 1
    )
    assert scalar(conn, "SELECT count(*) FROM official_domain WHERE id = :id", id=domain_id) == 1
    assert scalar(conn, "SELECT count(*) FROM source_mapping WHERE id = :id", id=mapping_id) == 1
    # Its first list version still records what that file said.
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM target_list_entry WHERE target_institution_id = :id",
            id=target_id,
        )
        == 1
    )


def test_a_reappearing_institution_keeps_its_accumulated_work(
    conn: Connection, tmp_path: Path
) -> None:
    first = write_workbook(tmp_path / "b1.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, first)
    second = write_workbook(
        tmp_path / "b2.xlsx", rows=SAMPLE_ROWS[:-1], preamble=["A List", "数据来源：x v2.0"]
    )
    import_target_list(conn, second)
    third = write_workbook(tmp_path / "b3.xlsx", preamble=["A List", "数据来源：x v3.0"])
    report = import_target_list(conn, third)

    assert report.institutions_created == 0
    back = conn.execute(
        text(
            "SELECT is_in_current_list, removed_from_list_id FROM target_institution "
            "WHERE match_key = :key"
        ),
        {"key": normalize_institution_name(str(SAMPLE_ROWS[-1][2]))},
    ).one()
    assert back.is_in_current_list is True
    assert back.removed_from_list_id is None
    assert scalar(conn, "SELECT count(*) FROM target_institution") == len(SAMPLE_ROWS)


# ===========================================================================
# 9. Multiple domains per university
# ===========================================================================


def test_one_institution_may_own_many_hosts_and_subdomains(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    """Requirement 16.9. Universities routinely publish across several hosts."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    hosts = [
        "northgate.ac.uk",
        "www.northgate.ac.uk",
        "pg.northgate.ac.uk",
        "business.northgate.ac.uk",
        "northgate-alumni.org.uk",
    ]
    domain_ids = [make_domain(conn, target_id=target_id, host=host) for host in hosts]
    for domain_id in domain_ids:
        verify_host(conn, domain_id, actor)

    assert scalar(
        conn,
        "SELECT count(*) FROM official_domain WHERE target_institution_id = :id "
        "AND verification_status = 'VERIFIED_OFFICIAL'",
        id=target_id,
    ) == len(hosts)


def test_two_institutions_cannot_both_own_one_host(
    conn: Connection, actor: verification.Actor
) -> None:
    first = make_target(conn, name="Northgate Institute of Technology")
    second = make_target(conn, name="Rivermouth University")
    verify_host(conn, make_domain(conn, target_id=first, host="shared.ac.uk"), actor)

    other = make_domain(conn, target_id=second, host="shared.ac.uk")
    with expect_violation(conn, "ix_official_domain_one_trusted_owner_per_host"):
        verify_host(conn, other, actor)


def test_a_rejected_host_may_be_re_registered_for_another_institution(
    conn: Connection, actor: verification.Actor
) -> None:
    """The unique index is partial, so rejections accumulate without blocking."""
    first = make_target(conn, name="Northgate Institute of Technology")
    second = make_target(conn, name="Rivermouth University")
    rejected = make_domain(conn, target_id=first, host="contested.ac.uk")
    verification.reject_domain(
        conn, domain_id=rejected, actor=actor, reason="belongs to a different institution"
    )
    verify_host(conn, make_domain(conn, target_id=second, host="contested.ac.uk"), actor)
    assert scalar(conn, "SELECT count(*) FROM official_domain WHERE host = 'contested.ac.uk'") == 2


# ===========================================================================
# 10. An unverified host is never treated as verified
# ===========================================================================


def test_a_candidate_host_is_not_trusted_anywhere(conn: Connection) -> None:
    """Requirement 16.10 and requirement 5: a similarly named domain is a guess."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate-university.com")

    status = scalar(
        conn, "SELECT verification_status FROM official_domain WHERE id = :id", id=domain_id
    )
    assert status == OfficialVerificationStatus.CANDIDATE.value

    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate-university.com/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=domain_id,
    )
    with expect_violation(conn, "is CANDIDATE"):
        conn.execute(
            text(
                "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
                "verified_at = now(), verified_by = NULL WHERE id = :id"
            ),
            {"id": mapping_id},
        )


def test_leaving_candidate_requires_a_method_an_actor_and_a_time(
    conn: Connection,
) -> None:
    """There is no member of DomainVerificationMethod for "the name matched"."""
    target_id = make_target(conn, name="Rivermouth University")
    domain_id = make_domain(conn, target_id=target_id, host="rivermouth.ac.uk")
    with expect_violation(conn, "verified_domain_records_its_basis"):
        conn.execute(
            text(
                "UPDATE official_domain SET verification_status = 'VERIFIED_OFFICIAL' "
                "WHERE id = :id"
            ),
            {"id": domain_id},
        )


def test_an_authorized_external_host_cannot_back_an_official_source(
    conn: Connection, actor: verification.Actor
) -> None:
    """Requirement 6, stated as a database rule.

    An application SaaS platform carries official information because a university
    authorised it. That is not the same as being the university's own domain, and no
    code path may promote "linked from the official site" into "is the official
    site".
    """
    target_id = make_target(conn, name="Northgate Institute of Technology")
    saas = make_domain(conn, target_id=target_id, host="apply.some-admissions-saas.com")
    verification.authorize_external_domain(
        conn,
        domain_id=saas,
        actor=actor,
        authorization_reference="Delegated from northgate.ac.uk/apply, confirmed by email",
        evidence="northgate.ac.uk/apply redirects here and names it as the portal",
        reason="onboarding the application portal",
    )
    assert (
        scalar(conn, "SELECT verification_status FROM official_domain WHERE id = :id", id=saas)
        == OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value
    )

    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://apply.some-admissions-saas.com/northgate/deadlines",
        category=SourceCategory.APPLICATION_DEADLINES,
        domain_id=saas,
    )
    with expect_violation(conn, "cannot be VERIFIED_OFFICIAL"):
        conn.execute(
            text(
                "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
                "verified_at = now(), verified_by = :actor WHERE id = :id"
            ),
            {"id": mapping_id, "actor": actor.user_id},
        )

    # It may, however, be AUTHORIZED_EXTERNAL -- which is what it is.
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="authorised portal"
    )
    assert (
        scalar(conn, "SELECT verification_status FROM source_mapping WHERE id = :id", id=mapping_id)
        == OfficialVerificationStatus.AUTHORIZED_EXTERNAL.value
    )


def test_an_authorized_external_host_must_name_its_authorization(
    conn: Connection, actor: verification.Actor
) -> None:
    target_id = make_target(conn, name="Rivermouth University")
    domain_id = make_domain(conn, target_id=target_id, host="portal.vendor.example.com")
    with expect_violation(conn, "authorized_external_names_its_authorization"):
        conn.execute(
            text(
                "UPDATE official_domain SET verification_status = 'AUTHORIZED_EXTERNAL', "
                "verification_method = 'AUTHORIZED_PARTNER_AGREEMENT', verified_at = now(), "
                "verified_by = :actor WHERE id = :id"
            ),
            {"id": domain_id, "actor": actor.user_id},
        )


def test_a_source_cannot_cite_a_host_that_does_not_cover_its_url(
    conn: Connection, actor: verification.Actor
) -> None:
    """Otherwise a mapping could point anywhere while citing a verified domain."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    verified = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, verified, actor)

    # A URL on a different host, citing the verified registry entry.
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://elsewhere.example.com/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=verified,
    )
    with expect_violation(conn, "not covered by official_domain"):
        conn.execute(
            text(
                "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
                "verified_at = now(), verified_by = :actor WHERE id = :id"
            ),
            {"id": mapping_id, "actor": actor.user_id},
        )


def test_subdomain_coverage_is_explicit(conn: Connection, actor: verification.Actor) -> None:
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)

    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://pg.northgate.ac.uk/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=domain_id,
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="covered by the verified parent domain"
    )
    assert (
        scalar(conn, "SELECT verification_status FROM source_mapping WHERE id = :id", id=mapping_id)
        == OfficialVerificationStatus.VERIFIED_OFFICIAL.value
    )


# ===========================================================================
# 11. Bachelor, Master and PhD sources are mapped separately
# ===========================================================================


def test_the_three_degree_audiences_are_mapped_independently(
    conn: Connection, actor: verification.Actor
) -> None:
    """Requirement 16.11 and requirement 8.

    A taught-Masters page is not evidence about doctoral admissions: different
    pages, different offices, at nearly every institution in the list.
    """
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)

    specs = [
        (
            "https://northgate.ac.uk/undergraduate/apply",
            SourceCategory.UNDERGRADUATE_ADMISSIONS,
            (DegreeScope.UNDERGRADUATE,),
        ),
        (
            "https://northgate.ac.uk/postgraduate-taught/apply",
            SourceCategory.POSTGRADUATE_ADMISSIONS,
            (DegreeScope.TAUGHT_POSTGRADUATE,),
        ),
        (
            "https://northgate.ac.uk/research-degrees/apply",
            SourceCategory.PHD_ADMISSIONS,
            (DegreeScope.RESEARCH_POSTGRADUATE,),
        ),
    ]
    mapping_ids = []
    for url, category, scopes in specs:
        mapping_id = make_mapping(
            conn,
            target_id=target_id,
            url=url,
            category=category,
            domain_id=domain_id,
            degree_scopes=scopes,
        )
        verification.verify_source_mapping(
            conn, mapping_id=mapping_id, actor=actor, reason="official admissions page"
        )
        mapping_ids.append(mapping_id)

    rows = conn.execute(
        text(
            "SELECT m.source_category, s.degree_scope FROM source_mapping m "
            "JOIN source_degree_scope s ON s.source_mapping_id = m.id "
            "WHERE m.target_institution_id = :id ORDER BY 1"
        ),
        {"id": target_id},
    ).all()
    assert {(row.source_category, row.degree_scope) for row in rows} == {
        ("UNDERGRADUATE_ADMISSIONS", "UNDERGRADUATE"),
        ("POSTGRADUATE_ADMISSIONS", "TAUGHT_POSTGRADUATE"),
        ("PHD_ADMISSIONS", "RESEARCH_POSTGRADUATE"),
    }
    assert len(set(mapping_ids)) == 3


def test_a_taught_masters_page_does_not_imply_doctoral_coverage(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    """The silence is meaningful, and the coverage report reflects it."""
    target_id = make_target(conn, name="Rivermouth University")
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, matched_at = now(), "
            "matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_a"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="rivermouth.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)

    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://rivermouth.ac.uk/pg/apply",
        category=SourceCategory.POSTGRADUATE_ADMISSIONS,
        domain_id=domain_id,
        degree_scopes=(DegreeScope.TAUGHT_POSTGRADUATE,),
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="official taught-PG page"
    )

    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.has_taught_postgraduate_admissions is True
    assert row.has_research_postgraduate_admissions is False


# ===========================================================================
# 12. Coverage names what is missing
# ===========================================================================


def test_coverage_reports_every_missing_category(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    """Requirement 16.12 and requirement 9."""
    target_id = make_target(conn, name="Northgate Institute of Technology")

    # Before identity resolution, the blocker is identity -- not sources.
    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.coverage_status == CoverageStatus.IDENTITY_NOT_VERIFIED.value

    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, matched_at = now(), "
            "matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_a"], "actor": actor.user_id, "id": target_id},
    )
    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.coverage_status == CoverageStatus.NO_SOURCES_MAPPED.value
    assert set(row.missing()) == set(REQUIRED_COVERAGE)

    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate.ac.uk/",
        category=SourceCategory.UNIVERSITY_HOME,
        domain_id=domain_id,
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="official homepage"
    )

    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.coverage_status == CoverageStatus.SOURCE_MAPPING_INCOMPLETE.value
    assert row.has_homepage is True
    assert "has_homepage" not in row.missing()
    assert "has_tuition" in row.missing()
    assert row.verified_source_count == 1
    assert row.verified_domain_count == 1


def test_coverage_becomes_complete_only_when_every_category_is_verified(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    target_id = make_target(conn, name="Rivermouth University")
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, matched_at = now(), "
            "matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_b"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="rivermouth.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)

    plan: list[tuple[str, SourceCategory, tuple[DegreeScope, ...]]] = [
        ("", SourceCategory.UNIVERSITY_HOME, ()),
        ("ug/apply", SourceCategory.UNDERGRADUATE_ADMISSIONS, (DegreeScope.UNDERGRADUATE,)),
        ("pgt/apply", SourceCategory.POSTGRADUATE_ADMISSIONS, (DegreeScope.TAUGHT_POSTGRADUATE,)),
        ("phd/apply", SourceCategory.PHD_ADMISSIONS, (DegreeScope.RESEARCH_POSTGRADUATE,)),
        ("courses", SourceCategory.PROGRAM_CATALOG, ()),
        ("entry-requirements", SourceCategory.ENTRY_REQUIREMENTS, ()),
        ("english-language", SourceCategory.LANGUAGE_REQUIREMENTS, ()),
        ("fees", SourceCategory.TUITION_FEES, ()),
        ("deadlines", SourceCategory.APPLICATION_DEADLINES, ()),
    ]
    for path, category, scopes in plan:
        mapping_id = make_mapping(
            conn,
            target_id=target_id,
            url=f"https://rivermouth.ac.uk/{path}",
            category=category,
            domain_id=domain_id,
            degree_scopes=scopes,
        )
        verification.verify_source_mapping(
            conn, mapping_id=mapping_id, actor=actor, reason="official page"
        )

    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.missing() == []
    assert row.coverage_status == CoverageStatus.SOURCE_MAPPING_COMPLETE.value


def test_a_candidate_source_does_not_count_toward_coverage(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    """A candidate URL is a guess; counting it would claim a readiness we lack."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, matched_at = now(), "
            "matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_a"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate.ac.uk/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=domain_id,
    )

    row = next(
        r for r in coverage_report(conn, only_current=False) if r.target_institution_id == target_id
    )
    assert row.has_tuition is False
    assert row.verified_source_count == 0
    assert row.candidate_source_count == 1


def test_the_whole_client_list_reports_as_identity_not_verified(
    conn: Connection, imported: Any
) -> None:
    """The honest state immediately after import: scope known, nothing verified."""
    totals = coverage_totals(conn)
    assert totals == {CoverageStatus.IDENTITY_NOT_VERIFIED.value: CLIENT_TOTAL}


# ===========================================================================
# 13. Verification actions enter the audit history
# ===========================================================================


def test_every_verification_action_appends_to_the_hash_chain(
    conn: Connection, actor: verification.Actor
) -> None:
    """Requirement 16.13 and requirement 10."""
    start = scalar(conn, "SELECT coalesce(max(seq), 0) FROM audit_log")

    target_id = make_target(conn, name="Northgate Institute of Technology")
    verified = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    rejected = make_domain(conn, target_id=target_id, host="northgate-uni.example.com")
    legacy = make_domain(conn, target_id=target_id, host="old.northgate.ac.uk")
    replaced = make_domain(conn, target_id=target_id, host="ancient.northgate.ac.uk")

    verify_host(conn, verified, actor)
    verification.reject_domain(
        conn, domain_id=rejected, actor=actor, reason="unaffiliated look-alike domain"
    )
    verification.mark_domain_legacy(
        conn, domain_id=legacy, actor=actor, reason="retired in favour of the main site"
    )
    verification.replace_domain(
        conn,
        domain_id=replaced,
        replacement_id=verified,
        actor=actor,
        reason="superseded by the current domain",
    )
    verification.request_review(
        conn,
        target_institution_id=target_id,
        actor=actor,
        reason="two plausible institutions share this name",
    )

    rows = conn.execute(
        text(
            "SELECT seq, action, object_type, object_id, actor_id, actor_type, reason, "
            "       before_state, after_state "
            "  FROM audit_log WHERE seq > :start ORDER BY seq"
        ),
        {"start": start},
    ).all()

    assert [row.action for row in rows] == [
        "ONBOARDING_VERIFY",
        "ONBOARDING_REJECT",
        "ONBOARDING_MARK_LEGACY",
        "ONBOARDING_REPLACE",
        "ONBOARDING_REQUEST_REVIEW",
    ]
    # Actor, reason and both states are recorded for every one of them.
    for row in rows:
        assert row.actor_id == actor.user_id
        assert row.actor_type == ActorType.USER.value
        assert row.reason
        assert row.before_state is not None
        assert row.after_state is not None

    # The chain is gapless and strictly increasing across them (C18).
    seqs = [row.seq for row in rows]
    assert seqs == list(range(start + 1, start + 1 + len(rows)))

    # And the chain still verifies.
    assert conn.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all() == []


def test_the_import_itself_is_audited(conn: Connection, tmp_path: Path) -> None:
    path = write_workbook(tmp_path / "audited.xlsx", preamble=["A List", "数据来源：x v1.0"])
    report = import_target_list(conn, path)

    row = conn.execute(
        text(
            "SELECT action, object_type, object_id, after_state, reason FROM audit_log "
            "WHERE action = :action"
        ),
        {"action": AUDIT_ACTION_IMPORT},
    ).one()
    assert row.object_type == "target_list"
    assert row.object_id == report.target_list_id
    assert row.after_state["file_sha256"] == report.file_sha256
    assert row.after_state["rows_read"] == len(SAMPLE_ROWS)


def test_the_audit_row_is_the_last_write_of_a_verification(
    conn: Connection, actor: verification.Actor
) -> None:
    """Lock order: work rows first, the audit chain's head row last (C18).

    Asserted through the chain's own sequence: the audit entry describing a change
    must carry a `seq` greater than any entry written before the work began, and the
    work row must already show its new state by then.
    """
    target_id = make_target(conn, name="Rivermouth University")
    domain_id = make_domain(conn, target_id=target_id, host="rivermouth.ac.uk")
    before = scalar(conn, "SELECT coalesce(max(seq), 0) FROM audit_log")

    verify_host(conn, domain_id, actor)

    status, verified_at = conn.execute(
        text("SELECT verification_status, verified_at FROM official_domain WHERE id = :id"),
        {"id": domain_id},
    ).one()
    audit_seq, occurred_at = conn.execute(
        text(
            "SELECT seq, occurred_at FROM audit_log WHERE object_id = :id "
            "ORDER BY seq DESC LIMIT 1"
        ),
        {"id": domain_id},
    ).one()

    assert status == OfficialVerificationStatus.VERIFIED_OFFICIAL.value
    assert audit_seq == before + 1
    # The verification timestamp comes from the database, so it cannot precede the
    # audit row that describes it by clock skew.
    assert verified_at <= occurred_at


def test_a_verification_without_a_reason_is_refused(
    conn: Connection, actor: verification.Actor
) -> None:
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    with pytest.raises(verification.VerificationRefusedError, match="reason is required"):
        verification.verify_domain(
            conn,
            domain_id=domain_id,
            actor=actor,
            method=DomainVerificationMethod.MANUAL_STAFF_REVIEW,
            evidence="checked the site",
            reason="   ",
        )


def test_a_rejection_is_not_reversed_in_place(conn: Connection, actor: verification.Actor) -> None:
    """Keeping the rejection is what stops the same wrong domain being re-proposed."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate-uni.example.com")
    verification.reject_domain(conn, domain_id=domain_id, actor=actor, reason="look-alike")
    with pytest.raises(verification.VerificationRefusedError, match="was rejected"):
        verify_host(conn, domain_id, actor)


def test_rejecting_a_host_deactivates_the_sources_that_relied_on_it(
    conn: Connection, actor: verification.Actor
) -> None:
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor)
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate.ac.uk/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=domain_id,
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="official fee page"
    )

    verification.reject_domain(
        conn, domain_id=domain_id, actor=actor, reason="this host is not the institution's"
    )
    row = conn.execute(
        text("SELECT verification_status, is_active FROM source_mapping WHERE id = :id"),
        {"id": mapping_id},
    ).one()
    assert row.verification_status == OfficialVerificationStatus.REJECTED.value
    assert row.is_active is False


# ===========================================================================
# 14. No claims or proposals are produced
# ===========================================================================


def test_onboarding_produces_no_claim_and_no_proposal(
    conn: Connection, actor: verification.Actor, graph: Graph, tmp_path: Path
) -> None:
    """Requirement 16.14. Verified *sources* are not extracted *facts*."""
    path = write_workbook(tmp_path / "noclaims.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, path)

    target_id = scalar(
        conn,
        "SELECT id FROM target_institution WHERE match_key = :key",
        key=normalize_institution_name("Northgate Institute of Technology"),
    )
    conn.execute(
        text(
            "UPDATE target_institution SET matched_university_id = :uni, matched_at = now(), "
            "matched_by = :actor WHERE id = :id"
        ),
        {"uni": graph["university_a"], "actor": actor.user_id, "id": target_id},
    )
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor, subdomains=True)
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate.ac.uk/fees",
        category=SourceCategory.TUITION_FEES,
        domain_id=domain_id,
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="official fee page"
    )

    for table in (
        "field_claim",
        "change_proposal",
        "change_proposal_item",
        "claim_resolution",
        "field_conflict",
        "review_task",
        "extraction",
        "snapshot",
        "content_blob",
        "fetch_run",
        "fetch_attempt",
    ):
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0, table


def test_nothing_is_registered_for_collection_by_step_four(
    conn: Connection, actor: verification.Actor, graph: Graph
) -> None:
    """`promoted_source_id` stays NULL: no acquisition is started."""
    target_id = make_target(conn, name="Northgate Institute of Technology")
    domain_id = make_domain(conn, target_id=target_id, host="northgate.ac.uk")
    verify_host(conn, domain_id, actor)
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://northgate.ac.uk/",
        category=SourceCategory.UNIVERSITY_HOME,
        domain_id=domain_id,
    )
    verification.verify_source_mapping(
        conn, mapping_id=mapping_id, actor=actor, reason="official homepage"
    )

    # Scoped to this mapping. A global `source` count said the same thing until Step
    # 5B, where registering acquisition targets creates sources deliberately -- and a
    # test asserting "no source exists anywhere" would then be asserting the opposite
    # of the design rather than what this test is about.
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM source WHERE url_hash = "
            "  (SELECT url_sha256 FROM source_mapping WHERE id = :m)",
            m=mapping_id,
        )
        == 0
    )
    assert (
        scalar(conn, "SELECT count(*) FROM source_mapping WHERE promoted_source_id IS NOT NULL")
        == 0
    )


def test_an_untrusted_mapping_cannot_be_promoted_for_collection(conn: Connection) -> None:
    target_id = make_target(conn, name="Rivermouth University")
    domain_id = make_domain(conn, target_id=target_id, host="rivermouth.ac.uk")
    mapping_id = make_mapping(
        conn,
        target_id=target_id,
        url="https://rivermouth.ac.uk/",
        category=SourceCategory.UNIVERSITY_HOME,
        domain_id=domain_id,
    )
    source_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:id, 'https://rivermouth.ac.uk/', :hash, "
            "'university_site', 'MONTHLY', 'STATIC')"
        ),
        {"id": source_id, "hash": "a" * 64},
    )
    # Two guards now answer this, and either is a correct refusal: the CHECK
    # `only_a_trusted_mapping_may_be_promoted` sees the mapping's own status, and
    # Step 5C.6's `app_promotion_requires_live_trust` additionally sees the host. The
    # trigger runs first, so it usually reports; the test asserts the refusal rather
    # than which layer produced it.
    with expect_violation(
        conn, "only_a_trusted_mapping_may_be_promoted|nothing may be promoted under it"
    ):
        conn.execute(
            text("UPDATE source_mapping SET promoted_source_id = :src WHERE id = :id"),
            {"src": source_id, "id": mapping_id},
        )


# ===========================================================================
# QS separation, stated as privileges
# ===========================================================================


def test_the_publisher_cannot_touch_the_onboarding_plane(
    role_engines: dict[str, Any],
) -> None:
    """Requirement 11, as far as grants can take it -- which is not all the way.

    **This test's original claim was wrong and the client caught it.** It said that
    denying `app_publisher` write access to the onboarding plane meant no single
    database identity could carry a spreadsheet value into published data. It did
    not: `app_publisher` could *read* the onboarding tables and *write* the canonical
    ones, so one identity was enough.

    What grants actually establish is narrower and still worth asserting: the
    publisher can neither write the onboarding plane nor, since C27, read it -- so it
    has no route to the client's list at all. The rule that a QS value may not become
    a fact is enforced separately, by eligibility classes and triggers, in
    `test_publication_eligibility.py`.
    """
    from sqlalchemy import text as sql

    onboarding_tables = (
        "target_list",
        "target_institution",
        "target_list_entry",
        "target_list_diff",
        "official_domain",
        "source_mapping",
        "source_degree_scope",
        "source_discipline_scope",
    )
    engine = role_engines["app_publisher"]
    with engine.connect() as connection:
        granted = {
            (row.table_name, row.privilege_type)
            for row in connection.execute(
                sql(
                    "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = 'app_publisher' AND table_name = ANY(:tables)"
                ),
                {"tables": list(onboarding_tables)},
            ).all()
        }
    # No privilege of any kind: C27 revoked the SELECT that made the read half of
    # the attack possible. The publisher publishes reviewed claims and has no
    # legitimate reason to see the client's scope list.
    assert granted == set(), f"app_publisher still holds: {sorted(granted)}"


def test_the_roles_that_write_onboarding_cannot_write_canonical_tables(
    role_engines: dict[str, Any],
) -> None:
    from sqlalchemy import text as sql

    for role in ("app_api", "app_worker"):
        with role_engines[role].connect() as connection:
            rows = connection.execute(
                sql(
                    "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = :role AND table_name IN "
                    "('university', 'program', 'tuition', 'ranking_entry') "
                    "AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')"
                ),
                {"role": role},
            ).all()
        assert rows == [], f"{role} may write canonical data"


def test_only_http_urls_can_be_stored_at_all(conn: Connection) -> None:
    """Requirement 15: importing or pasting a URL is not a way past the URL guard.

    The service layer validates first, but the database is the floor: even a direct
    INSERT cannot store a non-http scheme.
    """
    target_id = make_target(conn, name="Northgate Institute of Technology")
    for url in ("file:///etc/passwd", "ftp://example.ac.uk/x", "redis://127.0.0.1:6379"):
        with expect_violation(conn, "url_is_http_or_https"):
            conn.execute(
                text(
                    "INSERT INTO source_mapping (target_institution_id, source_category, "
                    "url, normalized_url, url_sha256, host) "
                    "VALUES (:target, 'UNIVERSITY_HOME', :url, :url, :sha, 'example.ac.uk')"
                ),
                {"target": target_id, "url": url, "sha": "b" * 64},
            )


def test_a_target_list_source_url_must_also_be_http(conn: Connection) -> None:
    with expect_violation(conn, "source_url_is_http"):
        conn.execute(
            text(
                "INSERT INTO target_list (list_name, list_version, source_url, file_name, "
                "file_sha256, file_byte_size, sheet_name, imported_row_count) "
                "VALUES ('L', '1', 'file:///etc/passwd', 'f.xlsx', :sha, 1, 's', 0)"
            ),
            {"sha": "c" * 64},
        )


def test_the_workbooks_own_qs_url_is_stored_but_never_fetched(
    conn: Connection, imported: Any
) -> None:
    """The QS download URL is provenance text. No fetch happens during import."""
    source_url, description = conn.execute(
        text("SELECT source_url, source_description FROM target_list")
    ).one()
    assert source_url is not None
    assert source_url.startswith("https://")
    assert description is not None and "QS World University Rankings 2027" in description
    # Nothing was retrieved: the evidence plane is untouched.
    assert scalar(conn, "SELECT count(*) FROM fetch_attempt") == 0
    assert scalar(conn, "SELECT count(*) FROM snapshot") == 0


# ===========================================================================
# 15. Step 3 / 3.5 invariants still hold
# ===========================================================================


def test_every_table_is_still_classified_and_granted(conn: Connection) -> None:
    """Requirement 16.15, the part that the new tables could have broken.

    The privileges migration froze its table list before these tables existed, so
    their grants are emitted by revision 0016. This asserts the two together cover
    every table that exists (C21).
    """
    from app.db.classification import ALL_CLASSIFIED_TABLES

    live = {
        name
        for (name,) in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version'"
            )
        ).all()
    }
    assert live == set(ALL_CLASSIFIED_TABLES)

    ungranted = {
        name
        for (name,) in conn.execute(
            text(
                "SELECT t.table_name FROM information_schema.tables t "
                "WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                "  AND t.table_name <> 'alembic_version' "
                "  AND t.table_name <> 'audit_chain_head' "
                "  AND NOT EXISTS (SELECT 1 FROM information_schema.table_privileges p "
                "                   WHERE p.table_name = t.table_name "
                "                     AND p.grantee = 'app_api' "
                "                     AND p.privilege_type = 'SELECT')"
            )
        ).all()
    }
    assert ungranted == set()


def test_the_audit_chain_verifies_after_onboarding_writes(
    conn: Connection, tmp_path: Path, actor: verification.Actor
) -> None:
    path = write_workbook(tmp_path / "chain.xlsx", preamble=["A List", "数据来源：x v1.0"])
    import_target_list(conn, path)
    target_id = make_target(conn, name="Southbank University")
    domain_id = make_domain(conn, target_id=target_id, host="southbank.ac.uk")
    verify_host(conn, domain_id, actor)

    assert conn.execute(text("SELECT * FROM app_audit_log_verify_chain()")).all() == []


def test_the_coverage_view_columns_match_the_python_contract(conn: Connection) -> None:
    """The view lives in the migration and the requirements live in Python; a test
    is what stops the two drifting."""
    columns = {
        name
        for (name,) in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'target_source_coverage'"
            )
        ).all()
    }
    assert set(REQUIRED_COVERAGE).issubset(columns)
    assert {"identity_verified", "coverage_status", "verified_source_count"}.issubset(columns)
