"""Migration and schema-shape tests.

The drift test is the one that earns its keep over time: models and migrations are
two descriptions of the same schema, and without a check they diverge quietly until
someone's local database disagrees with production.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import Connection, create_engine, text

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "b9c0d1e2f3a4"


def _run_alembic(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    import os

    parsed = urlparse(dsn)
    assert parsed.hostname and parsed.username and parsed.password
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_HOST": parsed.hostname,
            "POSTGRES_PORT": str(parsed.port or 5432),
            "POSTGRES_DB": parsed.path.lstrip("/"),
            "POSTGRES_MIGRATION_USER": parsed.username,
            "POSTGRES_MIGRATION_PASSWORD": parsed.password,
            "ENVIRONMENT": "ci",
            "LOG_FORMAT": "console",
            "PYTHONPATH": str(API_ROOT / "src"),
            # Migration comments contain Chinese (字段归责, destination names) and
            # Windows defaults stdout to cp1252, which cannot encode them. Without
            # this, `alembic upgrade --sql` fails with UnicodeEncodeError.
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return subprocess.run(  # noqa: S603 -- fixed executable, no shell
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


@pytest.fixture
def scratch_database(postgres_dsn: str) -> Iterator[str]:
    """A throwaway database, so a developer's working data is never dropped."""
    name = f"datahub_schema_{uuid.uuid4().hex[:10]}"
    parsed = urlparse(postgres_dsn)
    admin_dsn = urlunparse(parsed._replace(path="/postgres"))

    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    try:
        yield urlunparse(parsed._replace(path=f"/{name}"))
    finally:
        admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_full_schema_applies_to_an_empty_database(scratch_database: str) -> None:
    result = _run_alembic("upgrade", "head", dsn=scratch_database)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    engine = create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == HEAD_REVISION

            tables = set(
                connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'alembic_version'"
                    )
                ).scalars()
            )
            from app.db.classification import ALL_CLASSIFIED_TABLES

            assert tables == set(ALL_CLASSIFIED_TABLES), (
                f"unexpected: {sorted(tables - set(ALL_CLASSIFIED_TABLES))}, "
                f"missing: {sorted(set(ALL_CLASSIFIED_TABLES) - tables)}"
            )
    finally:
        engine.dispose()


def test_migrations_downgrade_to_base_and_reapply(scratch_database: str) -> None:
    """Every revision is reversible, and reversible twice over."""
    assert _run_alembic("upgrade", "head", dsn=scratch_database).returncode == 0

    down = _run_alembic("downgrade", "base", dsn=scratch_database)
    assert down.returncode == 0, f"stdout:\n{down.stdout}\nstderr:\n{down.stderr}"

    engine = create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            remaining = set(
                connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'alembic_version'"
                    )
                ).scalars()
            )
            assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"

            types = set(
                connection.execute(
                    text(
                        "SELECT t.typname FROM pg_type t JOIN pg_namespace n "
                        "ON n.oid = t.typnamespace "
                        "WHERE t.typtype = 'e' AND n.nspname = 'public'"
                    )
                ).scalars()
            )
            assert types == set(), f"downgrade left enum types behind: {sorted(types)}"
    finally:
        engine.dispose()

    assert _run_alembic("upgrade", "head", dsn=scratch_database).returncode == 0


def test_models_and_migrations_do_not_drift(scratch_database: str) -> None:
    """`alembic check` on a freshly migrated database must find nothing to do.

    This is what keeps the ORM models and the migration history describing the same
    schema. If it fails, one of them was edited without the other.
    """
    assert _run_alembic("upgrade", "head", dsn=scratch_database).returncode == 0
    result = _run_alembic("check", dsn=scratch_database)
    assert result.returncode == 0, (
        "models and migrations have drifted:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "No new upgrade operations detected" in (result.stdout + result.stderr)


def test_required_extensions_and_enum_types_exist(conn: Connection) -> None:
    extensions = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert {"pg_trgm", "btree_gist"} <= extensions

    from app.db.enums import enum_ddl_specs

    live = set(
        conn.execute(
            text(
                "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public'"
            )
        ).scalars()
    )
    expected = {name for name, _ in enum_ddl_specs()}
    assert (
        expected == live
    ), f"unexpected types: {sorted(live - expected)}, missing: {sorted(expected - live)}"


def test_enum_members_match_the_python_enums(conn: Connection) -> None:
    """Members, not just type names.

    Comparing only names let `fetch_status` drift: revision 0015 added `ABANDONED`
    to the PostgreSQL type and the Python `FetchStatus` never gained it, so reading
    back an abandoned fetch would have failed to coerce. A member present in one and
    absent from the other is a bug in whichever direction it points.
    """
    from app.db.enums import enum_ddl_specs

    live: dict[str, set[str]] = {}
    for type_name, member in conn.execute(
        text(
            "SELECT t.typname, e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public'"
        )
    ).all():
        live.setdefault(type_name, set()).add(member)

    mismatched: dict[str, tuple[list[str], list[str]]] = {}
    for type_name, members in enum_ddl_specs():
        in_python = set(members)
        in_database = live.get(type_name, set())
        if in_python != in_database:
            mismatched[type_name] = (
                sorted(in_database - in_python),
                sorted(in_python - in_database),
            )
    assert mismatched == {}, (
        "enum members differ between PostgreSQL and app.db.enums "
        f"(type: only-in-database, only-in-python): {mismatched}"
    )


def test_generated_columns_are_generated_not_stored_by_the_application(
    conn: Connection,
) -> None:
    """Derived columns must be `GENERATED ALWAYS`, or they can be set wrongly."""
    generated = {
        (row[0], row[1])
        for row in conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE is_generated = 'ALWAYS' AND table_schema = 'public'"
            )
        ).all()
    }
    expected = {
        ("application_deadline", "deadline_precision"),
        ("application_deadline", "deadline_cal_range"),
        ("application_round", "opens_precision"),
        ("application_round", "opens_cal_range"),
        ("application_round", "round_label_norm"),
        ("admission_requirement", "grain"),
        ("language_requirement", "grain"),
    }
    assert expected <= generated, f"missing generated columns: {sorted(expected - generated)}"


def test_no_history_table_has_an_updated_at_column(conn: Connection) -> None:
    """An `updated_at` on append-only history would advertise a mutation that cannot
    happen, and would invite code to attempt one."""
    from app.db.classification import IMMUTABLE_TABLES

    offenders = conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'updated_at' "
            "AND table_name = ANY(:tables)"
        ),
        {"tables": list(IMMUTABLE_TABLES)},
    ).all()
    assert offenders == [], f"immutable tables must not have updated_at: {offenders}"


def test_no_table_carries_a_superseded_at_column(conn: Connection) -> None:
    """Correction C1: supersession is version chronology, never a mutated flag."""
    offenders = conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND column_name IN ('superseded_at', 'is_current')"
        )
    ).all()
    assert offenders == [], (
        "supersession must come from version chronology and entity_head, "
        f"not from a mutable column: {offenders}"
    )


def test_expected_search_indexes_exist(conn: Connection) -> None:
    indexes = set(
        conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")).scalars()
    )
    for expected in (
        "ix_university_name_en_trgm",
        "ix_program_name_en_trgm",
        "ix_entity_alias_value_trgm",
        "ix_application_deadline_cal_range",
        "ix_change_proposal_pending_sla",
        "ix_fetch_run_failures",
        "ix_field_provenance_root_lookup",
        "ix_change_event_feed",
        "uq_application_round_intake_code",
        "uq_application_round_intake_label",
    ):
        assert expected in indexes, f"missing index {expected}"


def test_migration_owned_indexes_exist_and_are_complete(conn: Connection) -> None:
    """`MIGRATION_OWNED_INDEXES` must name exactly the indexes migrations create.

    This replaces the equality assertion that used to live inside revision 0012.
    That assertion imported the live list, so a later revision adding an index made a
    *fresh* database fail to build while an already-upgraded one was fine -- the C21
    defect. Checking it here, against the built schema, is both safer and stronger:

    * every declared name must actually exist, so a typo in `env.py`'s exclusion list
      cannot silently switch off drift detection for a real index;
    * every index that exists but is *not* declared by a model must be declared here,
      so an index added in a migration cannot be left out of the exclusion list and
      then be proposed for deletion on every `alembic check`.
    """
    import app.db.all_models  # noqa: F401 - populates Base.metadata
    from app.core.db import Base
    from app.db.classification import MIGRATION_OWNED_INDEXES

    live = set(
        conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname NOT LIKE 'pk_%' "
                # Alembic owns its own bookkeeping table and its primary key index.
                "  AND tablename <> 'alembic_version'"
            )
        ).scalars()
    )

    missing = set(MIGRATION_OWNED_INDEXES) - live
    assert missing == set(), f"declared migration-owned indexes do not exist: {sorted(missing)}"

    # Everything the models declare: table indexes plus the constraint-backed ones.
    from_models: set[str] = set()
    for table in Base.metadata.tables.values():
        from_models.update(index.name for index in table.indexes if index.name)
        from_models.update(
            str(constraint.name) for constraint in table.constraints if constraint.name
        )

    undeclared = live - from_models - set(MIGRATION_OWNED_INDEXES)
    assert undeclared == set(), (
        "these indexes exist but are owned by neither a model nor "
        f"MIGRATION_OWNED_INDEXES, so `alembic check` will propose dropping them: "
        f"{sorted(undeclared)}"
    )


def test_the_calendar_range_index_is_gist(conn: Connection) -> None:
    """A B-tree cannot answer a range-overlap query, so the method matters."""
    method = conn.execute(
        text(
            "SELECT am.amname FROM pg_class c "
            "JOIN pg_am am ON am.oid = c.relam "
            "WHERE c.relname = 'ix_application_deadline_cal_range'"
        )
    ).scalar_one()
    assert method == "gist"


def test_reference_vocabulary_is_seeded_but_no_institution_facts_are(
    conn: Connection,
) -> None:
    """Seeds carry vocabulary only. A seeded university fact would be a published
    claim with no provenance, which is exactly what this platform exists to prevent.
    """
    for table, minimum in (
        ("destination", 3),
        ("degree_level", 2),
        ("intake_season", 2),
        ("currency", 3),
        ("billing_unit", 3),
        ("test_type", 3),
        ("student_category", 4),
        ("scope_dimension", 3),
        ("application_round_type", 5),
        ("role", 6),
        ("permission", 10),
        ("role_permission", 10),
    ):
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count >= minimum, f"{table} looks unseeded ({count} rows)"

    for table in ("university", "program", "program_offering", "tuition", "ranking_entry"):
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"{table} must not be seeded with facts, found {count} rows"


def test_the_universal_applicant_scope_exists_with_no_criteria(conn: Connection) -> None:
    scope_id, is_universal = conn.execute(
        text("SELECT id, is_universal FROM applicant_scope WHERE code = 'UNIVERSAL'")
    ).one()
    assert is_universal is True
    criteria = conn.execute(
        text("SELECT count(*) FROM applicant_scope_criterion WHERE scope_id = :s"),
        {"s": scope_id},
    ).scalar_one()
    assert criteria == 0, "the universal scope matches everyone, so it has no criteria"


def test_rankings_are_gated_closed_by_default(conn: Connection) -> None:
    """D8: nothing ranking-related may be displayable without an authorisation."""
    displayable = conn.execute(
        text("SELECT count(*) FROM ranking_edition WHERE display_allowed = true")
    ).scalar_one()
    assert displayable == 0, "no ranking edition may be displayable before a licence exists"


def test_upgrading_twice_is_a_no_op(scratch_database: str) -> None:
    """Re-running the migration set must not attempt work it has already done."""
    assert _run_alembic("upgrade", "head", dsn=scratch_database).returncode == 0
    second = _run_alembic("upgrade", "head", dsn=scratch_database)
    assert second.returncode == 0, second.stderr


def test_offline_mode_renders_the_whole_schema_as_sql(postgres_dsn: str) -> None:
    """``--sql`` lets the DDL be reviewed before it touches a database.

    Also a regression guard for console encoding: the migrations carry Chinese in
    their comments, and on Windows stdout defaults to a codec that cannot encode it,
    which made this command fail outright.
    """
    result = _run_alembic("upgrade", "head", "--sql", dsn=postgres_dsn)
    assert result.returncode == 0, result.stderr
    for expected in ("CREATE EXTENSION", "CREATE TABLE university", "CREATE TRIGGER"):
        assert expected in result.stdout, f"offline SQL is missing {expected!r}"
