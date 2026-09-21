"""Privilege tests: invariants I1 and I2, verified against the live catalogs.

These matter more than any application test in the schema. The architecture's claim
is that the API and the workers *cannot* bypass publication and *cannot* rewrite
history — not that they are coded not to. That claim is only true if PostgreSQL
refuses, so this module asks PostgreSQL.

Two styles of check, both present on purpose:

* **Catalog assertions** (`information_schema.table_privileges`) prove the grant
  matrix is what the migration intended, table by table.
* **Behavioural assertions** connect as the role and attempt the write, proving the
  grants actually bite. A grant matrix can look right and still be defeated by an
  inherited role or a stray `PUBLIC` privilege.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.db.classification import ALL_CLASSIFIED_TABLES, CANONICAL_TABLES, IMMUTABLE_TABLES

pytestmark = pytest.mark.integration


def _privileges(conn: object, table: str, grantee: str) -> set[str]:
    rows = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_name = :t AND grantee = :g"
        ),
        {"t": table, "g": grantee},
    ).scalars()
    return set(rows)


# ---------------------------------------------------------------------------
# Catalog assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", CANONICAL_TABLES)
def test_only_the_publisher_may_write_canonical_projections(conn: object, table: str) -> None:
    """Invariant I1, table by table."""
    assert _privileges(conn, table, "app_api") == {
        "SELECT"
    }, f"app_api must be read-only on {table}"
    assert _privileges(conn, table, "app_worker") == {
        "SELECT"
    }, f"app_worker must be read-only on {table}"
    publisher = _privileges(conn, table, "app_publisher")
    assert {"SELECT", "INSERT", "UPDATE"} <= publisher
    assert "DELETE" not in publisher, "published state is corrected, never deleted"


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_no_role_may_update_or_delete_immutable_history(conn: object, table: str) -> None:
    """Invariant I2, table by table. Includes app_publisher: nobody is exempt."""
    for role in ("app_api", "app_worker", "app_publisher"):
        privileges = _privileges(conn, table, role)
        assert "UPDATE" not in privileges, f"{role} must not UPDATE {table}"
        assert "DELETE" not in privileges, f"{role} must not DELETE {table}"


def test_runtime_roles_own_no_tables(conn: object) -> None:
    """Ownership would let a role bypass every grant by altering the table."""
    owned = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT tablename, tableowner FROM pg_tables "
            "WHERE schemaname = 'public' "
            "AND tableowner IN ('app_api', 'app_worker', 'app_publisher')"
        )
    ).all()
    assert owned == [], f"runtime roles must own nothing, but own: {owned}"


def test_public_holds_no_privileges_on_domain_tables(conn: object) -> None:
    """A stray PUBLIC grant would silently defeat the whole matrix."""
    leaked = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT DISTINCT table_name, privilege_type "
            "FROM information_schema.table_privileges "
            "WHERE grantee = 'PUBLIC' AND table_schema = 'public'"
        )
    ).all()
    assert leaked == [], f"PUBLIC must hold nothing, but holds: {leaked}"


#: Every DELETE grant in the system, with the reason each one exists.
#:
#: Nothing that records a fact, an observation or a decision appears here, and that
#: is the invariant this test defends. A new entry needs a justification of the same
#: kind: deleting the row must lose no fact.
DELETE_GRANTS: dict[tuple[str, str], str] = {
    ("outbox_message", "app_worker"): (
        "delivery infrastructure, not history: pruning a delivered message loses no " "fact (C7)"
    ),
    ("user_session", "app_api"): "revocation is the point of a session record",
    # Step 4. These two are membership sets, not history: which applicant audiences
    # and which disciplines a mapped page serves is current configuration, and an
    # operator who ticked the wrong audience must be able to un-tick it. The
    # alternative -- rejecting the whole source mapping and re-creating it -- would
    # discard its verification for a corrected checkbox. The change is still
    # recorded, in `audit_log`.
    ("source_degree_scope", "app_api"): "audience membership set; corrections un-tick a box",
    ("source_discipline_scope", "app_api"): (
        "discipline membership set; corrections un-tick a box"
    ),
    # Step 5B.2. A host cooldown is an obligation we took on -- "do not ask this
    # server for anything until 18:20" -- and once that instant passes the row
    # records nothing. It is not an observation, not a decision and not history: the
    # 429 that caused it is permanent history in `fetch_run`, and that is the row
    # nobody may delete. The worker prunes expired pauses, and `clear_cooldown`
    # removes one deliberately, which is audited.
    ("host_cooldown", "app_worker"): (
        "expired timing obligation; the 429 that caused it stays in fetch_run"
    ),
}


def test_delete_is_granted_only_where_deleting_loses_no_fact(conn: object) -> None:
    """The deliberate DELETE grants, and nothing else (C7).

    Anything that records what a source said, what a reviewer decided or what was
    published is append-only and appears nowhere in `DELETE_GRANTS`.
    """
    deletable = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT DISTINCT table_name, grantee FROM information_schema.table_privileges "
            "WHERE privilege_type = 'DELETE' AND grantee LIKE 'app%' "
            "ORDER BY table_name, grantee"
        )
    ).all()
    granted = {(row.table_name, row.grantee) for row in deletable}
    unexpected = granted - set(DELETE_GRANTS)
    assert unexpected == set(), (
        "DELETE was granted without a recorded justification in DELETE_GRANTS; "
        f"each of these must lose no fact when deleted: {sorted(unexpected)}"
    )
    assert granted == set(
        DELETE_GRANTS
    ), f"missing expected DELETE grants: {sorted(set(DELETE_GRANTS) - granted)}"


def test_no_history_or_evidence_table_is_deletable(conn: object) -> None:
    """The class-level statement of the same rule, independent of the list above."""
    from app.db.classification import CANONICAL_TABLES, IMMUTABLE_TABLES

    deletable = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT DISTINCT table_name, grantee FROM information_schema.table_privileges "
            "WHERE privilege_type IN ('DELETE', 'TRUNCATE') AND grantee LIKE 'app%' "
            "  AND table_name = ANY(:tables)"
        ),
        {"tables": list(IMMUTABLE_TABLES + CANONICAL_TABLES)},
    ).all()
    assert deletable == [], f"history or canonical data is deletable: {deletable}"


# ---------------------------------------------------------------------------
# Behavioural assertions — connect as the role and try it
# ---------------------------------------------------------------------------


def test_app_api_cannot_create_alter_or_drop_domain_tables(
    role_engines: dict[str, Engine],
) -> None:
    engine = role_engines["app_api"]
    with engine.connect() as connection:
        for statement in (
            "CREATE TABLE api_role_should_not_do_this (id int)",
            "ALTER TABLE university ADD COLUMN injected text",
            "DROP TABLE tuition",
        ):
            transaction = connection.begin()
            with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
                connection.execute(text(statement))
            transaction.rollback()


@pytest.mark.parametrize("role", ["app_api", "app_worker"])
def test_service_roles_cannot_mutate_canonical_projections(
    role_engines: dict[str, Engine], role: str
) -> None:
    """The behavioural form of I1: the write is refused, not merely ungranted."""
    with role_engines[role].connect() as connection:
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(
                text(
                    "INSERT INTO university (id, canonical_id, destination_code, name_en) "
                    "VALUES (:id, 'bypass-attempt', 'GB', 'Bypass University')"
                ),
                {"id": uuid.uuid4()},
            )
        transaction.rollback()

        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(text("UPDATE university SET name_en = 'Renamed'"))
        transaction.rollback()


def test_app_publisher_may_write_canonical_but_not_rewrite_history(
    role_engines: dict[str, Engine],
) -> None:
    """The publisher's privileges are exactly its job, and no more."""
    with role_engines["app_publisher"].connect() as connection:
        transaction = connection.begin()
        university_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO university (id, canonical_id, destination_code, name_en) "
                "VALUES (:id, :cid, 'GB', 'Publisher Test University')"
            ),
            {"id": university_id, "cid": f"pub-test-{university_id.hex[:8]}"},
        )
        connection.execute(
            text("UPDATE university SET city = 'London' WHERE id = :id"),
            {"id": university_id},
        )
        transaction.rollback()

        # ...but it may not delete published state.
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(text("DELETE FROM university"))
        transaction.rollback()

        # ...nor rewrite history it appended.
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(text("UPDATE field_provenance SET field_status = 'PUBLISHED'"))
        transaction.rollback()


def test_app_worker_may_append_evidence_but_not_publish(
    role_engines: dict[str, Engine],
) -> None:
    with role_engines["app_worker"].connect() as connection:
        transaction = connection.begin()
        source_id = uuid.uuid4()
        # The worker cannot register a source (that is an editor action through the
        # API), so this must fail.
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(
                text(
                    "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
                    "fetch_strategy) VALUES (:id, 'https://example.test/x', :h, "
                    "'university_site', 'DAILY', 'STATIC')"
                ),
                {"id": source_id, "h": "0" * 64},
            )
        transaction.rollback()

        # It may not write provenance: publication is not its job.
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="permission denied"):
            connection.execute(
                text(
                    "INSERT INTO field_provenance (id, entity_type, entity_id, field_path, "
                    "root_type, root_id, root_version_no, field_status, risk_level, "
                    "published_at) VALUES (:id, 'program', :e, 'name_en', 'program', :e, 1, "
                    "'OFFICIALLY_NOT_PUBLISHED', 'LOW', now())"
                ),
                {"id": uuid.uuid4(), "e": uuid.uuid4()},
            )
        transaction.rollback()


def test_every_domain_table_is_classified(conn: object) -> None:
    """A new table must be assigned a privilege class, or this fails.

    Without this, adding a table and forgetting to grant on it produces a table only
    the owner can read — which looks like a permissions bug in production rather
    than the missing migration step it is.
    """
    live = set(
        conn.execute(  # type: ignore[attr-defined]
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        ).scalars()
    )
    unclassified = sorted(live - set(ALL_CLASSIFIED_TABLES))
    assert unclassified == [], (
        f"these tables have no privilege classification: {unclassified}. "
        "Add them to the appropriate list in the privileges migration."
    )


def test_every_classified_table_actually_received_grants(conn: object) -> None:
    """Closes the gap left by freezing the migration's table lists.

    The privileges migration holds its lists frozen as of its own revision, because a
    migration must describe the schema at its point in history. That means the shared
    classification can no longer guarantee coverage on its own -- so this test does:
    every table the current classification knows about must have SELECT granted by
    *some* revision, which fails loudly if a new table arrives without privileges.
    """
    granted = set(
        conn.execute(  # type: ignore[attr-defined]
            text(
                "SELECT DISTINCT table_name FROM information_schema.table_privileges "
                "WHERE grantee LIKE 'app%' AND privilege_type = 'SELECT'"
            )
        ).scalars()
    )
    # audit_chain_head is deliberately ungranted: its trigger is SECURITY DEFINER.
    expected = set(ALL_CLASSIFIED_TABLES) - {"audit_chain_head"}
    missing = sorted(expected - granted)
    assert missing == [], (
        f"these tables have no grants from any revision: {missing}. "
        "Add them to the latest privileges migration."
    )
