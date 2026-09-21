"""Fixtures for the domain-schema integration tests.

These tests talk to PostgreSQL directly with SQL rather than through the ORM. That is
deliberate: what is under test is what the *database* refuses, and going through
SQLAlchemy would risk a passing test that only proves the ORM validated something
first.

Every test runs inside a transaction that is rolled back, so the suite leaves no
rows behind. Expected violations use savepoints, because a failed statement aborts
the surrounding transaction.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DatabaseError


@pytest.fixture(scope="session")
def owner_engine(postgres_dsn: str) -> Iterator[Engine]:
    """Engine connected as the schema-owning role."""
    engine = create_engine(postgres_dsn, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def conn(owner_engine: Engine) -> Iterator[Connection]:
    """A connection in a transaction that is always rolled back."""
    with owner_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture(scope="session")
def role_engines(
    postgres_dsn: str, runtime_role_passwords: dict[str, str]
) -> Iterator[dict[str, Engine]]:
    """One engine per runtime role, for privilege tests."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(postgres_dsn)
    engines: dict[str, Engine] = {}
    for role, password in runtime_role_passwords.items():
        netloc = f"{role}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        engines[role] = create_engine(urlunparse(parsed._replace(netloc=netloc)), future=True)
    yield engines
    for engine in engines.values():
        engine.dispose()


@contextmanager
def expect_violation(connection: Connection, match: str) -> Iterator[None]:
    """Assert the enclosed statement is refused by the database.

    Runs inside a savepoint so the outer transaction survives, and matches the
    message so a test cannot pass because of an unrelated error (a typo in a column
    name would otherwise look like success).
    """
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DatabaseError) as excinfo:
            yield
        assert re.search(
            match, str(excinfo.value), re.IGNORECASE
        ), f"expected a violation matching {match!r}, got: {excinfo.value}"
    finally:
        if savepoint.is_active:
            savepoint.rollback()


# ---------------------------------------------------------------------------
# Fictional catalog graph
#
# Wholly invented institutions, created inside the test transaction. No real
# university ever appears in a fixture: a seeded real fact would be a published
# claim with no provenance, which is what this platform exists to prevent.
# ---------------------------------------------------------------------------


class Graph(dict[str, uuid.UUID]):
    """Ids of the fictional entities a test created."""


@pytest.fixture
def graph(conn: Connection) -> Graph:
    """A minimal valid catalog graph: university -> ... -> application round.

    Two universities, so cross-parent tests have something to cross into.
    """
    ids = Graph()

    def new(key: str) -> uuid.UUID:
        ids[key] = uuid.uuid4()
        return ids[key]

    for suffix in ("a", "b"):
        uni = new(f"university_{suffix}")
        conn.execute(
            text(
                "INSERT INTO university (id, canonical_id, destination_code, name_en) "
                "VALUES (:id, :cid, 'GB', :name)"
            ),
            {
                "id": uni,
                "cid": f"test-uni-{suffix}-{uni.hex[:8]}",
                "name": f"Test University {suffix.upper()}",
            },
        )
        campus = new(f"campus_{suffix}")
        conn.execute(
            text(
                "INSERT INTO campus (id, university_id, code, name_en, is_primary) "
                "VALUES (:id, :uni, 'MAIN', 'Main Campus', true)"
            ),
            {"id": campus, "uni": uni},
        )
        faculty = new(f"faculty_{suffix}")
        conn.execute(
            text(
                "INSERT INTO faculty (id, university_id, code, name_en) "
                "VALUES (:id, :uni, 'ENG', 'Faculty of Engineering')"
            ),
            {"id": faculty, "uni": uni},
        )
        program = new(f"program_{suffix}")
        conn.execute(
            text(
                "INSERT INTO program (id, canonical_id, university_id, primary_faculty_id, "
                "name_en, degree_level_code, lifecycle_status, lifecycle_field_status) "
                "VALUES (:id, :cid, :uni, :fac, :name, 'MASTER', 'ACTIVE', 'PUBLISHED')"
            ),
            {
                "id": program,
                "cid": f"test-prog-{suffix}-{program.hex[:8]}",
                "uni": uni,
                "fac": faculty,
                "name": f"MSc Test Programme {suffix.upper()}",
            },
        )
        offering = new(f"offering_{suffix}")
        conn.execute(
            text(
                "INSERT INTO program_offering (id, canonical_id, program_id, university_id, "
                "study_mode, delivery_mode, duration_value, duration_unit, campus_id, "
                "lifecycle_status, lifecycle_field_status) "
                "VALUES (:id, :cid, :prog, :uni, 'FULL_TIME', 'ON_CAMPUS', 1, 'YEAR', "
                ":campus, 'ACTIVE', 'PUBLISHED')"
            ),
            {
                "id": offering,
                "cid": f"test-off-{suffix}-{offering.hex[:8]}",
                "prog": program,
                "uni": uni,
                "campus": campus,
            },
        )
        intake = new(f"intake_{suffix}")
        conn.execute(
            text(
                "INSERT INTO intake (id, offering_id, academic_year, intake_season_code) "
                "VALUES (:id, :off, '2027/28', 'SEPTEMBER')"
            ),
            {"id": intake, "off": offering},
        )
        round_id = new(f"round_{suffix}")
        conn.execute(
            text(
                "INSERT INTO application_round (id, intake_id, round_code, round_label) "
                "VALUES (:id, :intake, 'ROUND_1', 'Round 1')"
            ),
            {"id": round_id, "intake": intake},
        )

    ids["universal_scope"] = conn.execute(
        text("SELECT id FROM applicant_scope WHERE code = 'UNIVERSAL'")
    ).scalar_one()
    ids["student_category_intl"] = conn.execute(
        text(
            "SELECT id FROM student_category "
            "WHERE code = 'INTERNATIONAL' AND destination_code = 'GB'"
        )
    ).scalar_one()
    return ids


def insert(connection: Connection, sql: str, **params: Any) -> Any:
    """Execute a statement with named parameters."""
    return connection.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Eligible evidence (C27)
#
# Since publication eligibility landed, `field_provenance` refuses any asserted
# status that does not resolve to an evidence-eligible source. Tests that are about
# something else -- the I3 reviewer rule, the value/status biconditional, claim
# navigation -- need a citation that passes, so they get the smallest honest one.
#
# "Smallest honest" matters: this builds a real verified domain, a real promoted
# mapping and a real fetch, because the promotion order is forced by the design. A
# fixture that shortcut it would be testing a schema we do not have.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibleEvidence:
    """An OFFICIAL_VERIFIED source with one snapshot, plus the actor who verified it."""

    actor: uuid.UUID
    source: uuid.UUID
    snapshot: uuid.UUID
    extraction: uuid.UUID


@pytest.fixture
def eligible_evidence(conn: Connection) -> EligibleEvidence:
    import hashlib

    def sha(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()

    ids = {
        name: uuid.uuid4()
        for name in (
            "actor",
            "list",
            "target",
            "domain",
            "mapping",
            "source",
            "attempt",
            "run",
            "snapshot",
            "extraction",
        )
    }
    url = f"https://evidence-{ids['source'].hex[:8]}.ac.uk/"
    url_hash = sha(url)

    insert(
        conn,
        "INSERT INTO app_user (id, email, display_name) VALUES (:id, :email, 'Evidence')",
        id=ids["actor"],
        email=f"evidence-{ids['actor'].hex[:8]}@example.test",
    )
    insert(
        conn,
        "INSERT INTO target_list (id, list_name, list_version, file_name, file_sha256, "
        "file_byte_size, sheet_name, imported_row_count) "
        "VALUES (:id, 'Evidence List', :ver, 'e.xlsx', :sha, 1, 's', 1)",
        id=ids["list"],
        ver=ids["list"].hex[:8],
        sha=sha(str(ids["list"])),
    )
    insert(
        conn,
        "INSERT INTO target_institution (id, match_key, first_seen_list_id, latest_list_id) "
        "VALUES (:id, :key, :list, :list)",
        id=ids["target"],
        key=f"evidence-{ids['target'].hex[:8]}",
        list=ids["list"],
    )
    insert(
        conn,
        "INSERT INTO official_domain (id, target_institution_id, host, verification_status, "
        "verification_method, verification_evidence, verified_at, verified_by, "
        "covers_subdomains) VALUES (:id, :target, :host, 'VERIFIED_OFFICIAL', "
        "'GOVERNMENT_REGISTRY', 'invented register', now(), :actor, true)",
        id=ids["domain"],
        target=ids["target"],
        host=f"evidence-{ids['source'].hex[:8]}.ac.uk",
        actor=ids["actor"],
    )
    # Source first (unclassified), then the mapping that vouches for it, then the
    # classification -- the only order the design permits.
    insert(
        conn,
        "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, fetch_strategy) "
        "VALUES (:id, :url, :hash, 'university_site', 'MONTHLY', 'STATIC')",
        id=ids["source"],
        url=url,
        hash=url_hash,
    )
    insert(
        conn,
        "INSERT INTO source_mapping (id, target_institution_id, source_category, url, "
        "normalized_url, url_sha256, host, official_domain_id, verification_status, "
        "verified_at, verified_by, promoted_source_id) "
        "VALUES (:id, :target, 'TUITION_FEES', :url, :url, :sha, :host, :domain, "
        "'VERIFIED_OFFICIAL', now(), :actor, :source)",
        id=ids["mapping"],
        target=ids["target"],
        url=url,
        sha=url_hash,
        host=f"evidence-{ids['source'].hex[:8]}.ac.uk",
        domain=ids["domain"],
        actor=ids["actor"],
        source=ids["source"],
    )
    insert(
        conn,
        "UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED', "
        "eligibility_set_by = :actor, eligibility_set_at = now(), "
        "eligibility_reason = 'test fixture' WHERE id = :id",
        actor=ids["actor"],
        id=ids["source"],
    )
    insert(
        conn,
        "INSERT INTO fetch_attempt (id, source_id, cycle_key) VALUES (:id, :source, :cycle)",
        id=ids["attempt"],
        source=ids["source"],
        cycle=ids["attempt"].hex[:12],
    )
    insert(
        conn,
        "INSERT INTO fetch_run (id, source_id, attempt_id, started_at, status, fetcher) "
        "VALUES (:id, :source, :attempt, now(), 'OK', 'STATIC')",
        id=ids["run"],
        source=ids["source"],
        attempt=ids["attempt"],
    )
    blob = sha(f"body-{ids['snapshot']}")
    insert(
        conn,
        "INSERT INTO content_blob (content_hash, storage_key, first_observed_at) "
        "VALUES (:hash, :key, now())",
        hash=blob,
        key=f"blobs/{blob}",
    )
    insert(
        conn,
        "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, observed_at, "
        "requested_url, fetcher) VALUES (:id, :run, :source, :hash, now(), :url, 'STATIC')",
        id=ids["snapshot"],
        run=ids["run"],
        source=ids["source"],
        hash=blob,
        url=url,
    )
    insert(
        conn,
        "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, status) "
        "VALUES (:id, :snap, 'fixture-extractor', '1.0', 'OK')",
        id=ids["extraction"],
        snap=ids["snapshot"],
    )
    # Step 5C.6: publication authority is scoped to a field, so an eligible source with
    # no bindings publishes nothing. Promotion writes these from the mapping's category;
    # the fixture does the same, from the same policy table. The category is
    # TUITION_FEES because the fixture's URL is a fee page and its claims are fees --
    # it read UNIVERSITY_HOME before, which was never true of it.
    from app.domains.verification.policy import bindings_for

    for entity_type, field_path in bindings_for("TUITION_FEES"):
        insert(
            conn,
            "INSERT INTO source_field_binding (source_id, entity_type, field_path, "
            "responsibility) VALUES (:source, :entity, :field, 'PRIMARY') "
            "ON CONFLICT DO NOTHING",
            source=ids["source"],
            entity=entity_type,
            field=field_path,
        )
    return EligibleEvidence(
        actor=ids["actor"],
        source=ids["source"],
        snapshot=ids["snapshot"],
        extraction=ids["extraction"],
    )


# ---------------------------------------------------------------------------
# The client's real target workbook
#
# Shared because three modules assert against the client's actual scope. Tests that
# use it skip when the file is absent, which is honest: the alternative is asserting
# against a copy of the numbers that could drift from the file itself.
# ---------------------------------------------------------------------------

import os  # noqa: E402 - kept beside the fixture it serves
from pathlib import Path  # noqa: E402

#: The client's workbook. Overridable so CI can point at a copy.
CLIENT_WORKBOOK_ENV = "DATAHUB_TEST_TARGET_LIST"
DEFAULT_CLIENT_WORKBOOK = Path(r"D:\My Project\Chinese\QS_2027_世界前500_指定地区院校.xlsx")

#: The distribution the client stated in writing. Asserted against the workbook's own
#: summary block, not used in place of it: if the file and this disagree, that is the
#: finding.
CLIENT_DISTRIBUTION = {
    "US": 68,
    "GB": 48,
    "AU": 27,
    "CA": 17,
    "NZ": 8,
    "HK": 7,
    "SG": 4,
    "MO": 2,
}
CLIENT_TOTAL = 181


#: Step 5A. The client's final official-source list: 35 institutions, 11 URL
#: columns. Like the target list it lives outside the repository -- it is the
#: client's file, not a fixture, and copying it in would make a stale copy
#: authoritative.
OFFICIAL_SOURCES_ENV = "DATAHUB_TEST_OFFICIAL_SOURCES"
DEFAULT_OFFICIAL_SOURCES = Path(r"D:\My Project\Chinese\university_official_sources.xlsx")

#: What the supplied file holds, asserted rather than assumed. If the file and
#: these disagree, that disagreement is the finding.
OFFICIAL_SOURCES_INSTITUTIONS = 35
OFFICIAL_SOURCES_CORE_CELLS = 350
OFFICIAL_SOURCES_ADDITIONAL_CELLS = 35
OFFICIAL_SOURCES_DISTINCT_PAGES = 319


@pytest.fixture(scope="session")
def official_sources_workbook() -> Path:
    override = os.environ.get(OFFICIAL_SOURCES_ENV)
    path = Path(override) if override else DEFAULT_OFFICIAL_SOURCES
    if not path.is_file():
        pytest.skip(
            f"official source list not found at {path}; " f"set {OFFICIAL_SOURCES_ENV} to its path"
        )
    return path


@pytest.fixture(scope="session")
def client_workbook() -> Path:
    override = os.environ.get(CLIENT_WORKBOOK_ENV)
    path = Path(override) if override else DEFAULT_CLIENT_WORKBOOK
    if not path.is_file():
        pytest.skip(
            f"client target list not found at {path}; set {CLIENT_WORKBOOK_ENV} to its path"
        )
    return path


# ---------------------------------------------------------------------------
# Manifest bindings for fixture promotions
# ---------------------------------------------------------------------------


def fixture_binding(connection: Connection, mapping_id: uuid.UUID) -> Any:
    """A `ReviewedResponsibility` for a fixture mapping, assembled from the database.

    WHY THIS IS NOT A MANIFEST
    ==========================
    `promote` requires the object `responsibility_binding.require_binding` returns, so
    that an operator cannot promote a mapping the approved package never described. The
    real path obtains it by validating a frozen manifest digest and comparing five fields
    of lineage.

    Fixture mappings appear in no manifest -- they are invented per test -- so building one
    here from the row itself is the only way these tests can exercise promotion at all.
    That is safe precisely because it is *not* reachable by an operator: the CLI and the
    console both construct bindings only through `require_binding`, and this helper lives
    in the test tree.

    It deliberately reads the real lineage rather than taking arguments, so a test cannot
    accidentally promote with a binding that describes some other mapping.
    """
    from app.domains.verification.responsibility_binding import ReviewedResponsibility

    row = connection.execute(
        text(
            """
            SELECT sm.id, sm.source_category::text AS responsibility, sm.url, sm.host,
                   sm.target_institution_id,
                   pcs.id AS pilot_id, pcs.source_ref
              FROM source_mapping sm
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
             WHERE sm.id = :mapping
            """
        ),
        {"mapping": mapping_id},
    ).one()
    return ReviewedResponsibility(
        mapping_id=uuid.UUID(str(row.id)),
        pilot_source_id=uuid.UUID(str(row.pilot_id)) if row.pilot_id else uuid.uuid4(),
        source_ref=str(row.source_ref or "S0000"),
        institution_label="fixture institution",
        institution_id=uuid.UUID(str(row.target_institution_id)),
        claimed_responsibility=str(row.responsibility),
        url=str(row.url),
        host=str(row.host),
        access_class="BODY_EVIDENCE",
        page_evidence_available=True,
        is_duplicate_row=False,
        duplicate_of=None,
        declared_degree_scope=None,
        manifest_sha256="0" * 64,
    )
