"""The real pilot database must be unreachable from test mutation.

THE INCIDENT
============
Step 5C.7E: a live verification script assembled its DSN from the project settings.
`.env` sets `POSTGRES_DB=datahub`, so it connected to the **real pilot database**, created
eight `[TEST ONLY]` identities, issued each an enrolment challenge, and deleted them. The
identities and challenges went with the cascade. The nine `CREDENTIAL_ENROLLMENT_ISSUED`
rows did not, because `audit_log` is append-only by design -- correctly, and they are
still there.

Nothing was corrupted and no trust state moved. It was still a test-shaped write reaching
production data because of a default nobody mentioned.

WHY THESE TESTS ASK THE SERVER
==============================
The tempting guard is to read `POSTGRES_DB` and compare, or to require `DATAHUB_ENV=test`.
That is the check that already failed: the environment said `datahub` and was *correct* --
it is the right value for operating the real system. The intent was wrong, not the
variable, so re-reading the variable more carefully catches nothing.

Every test here therefore goes through `SELECT current_database()`. That answer cannot be
stale, inherited or mistyped.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from app.db.safety import (
    GATES_DATABASE,
    REAL_DATABASE,
    REFUSAL,
    TEST_DATABASES,
    RealDatabaseRefusedError,
    database_of,
    forbid_real_database,
    require_test_database,
    require_test_database_dsn,
)
from app.domains.identity.enrollment_tokens import issue
from app.domains.verification.identity import provision_reviewer

pytestmark = pytest.mark.integration


def _dsn_for(dsn: str, database: str) -> str:
    """The same server and credentials, a different database."""
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path=f"/{database}"))


@pytest.fixture
def real_database(postgres_dsn: str) -> object:
    """A **read-only** connection to the real pilot database.

    Connecting is how the refusal gets proven: the guard's job is to stop the write, and
    a test that could not reach the database could not show that it does. Nothing here
    commits, and every write attempt is expected to raise.
    """
    engine = create_engine(_dsn_for(postgres_dsn, REAL_DATABASE), future=True)
    try:
        with engine.connect() as connection:
            if database_of(connection) != REAL_DATABASE:
                pytest.skip(f"{REAL_DATABASE} is not reachable on this server")
            try:
                # SQLAlchemy 2.0 autobegins on first execute, so there is already a
                # transaction; rolling it back is what guarantees nothing here lands.
                yield connection
            finally:
                connection.rollback()
    finally:
        engine.dispose()


# ===========================================================================
# 1. The permitted database still works
# ===========================================================================


def test_the_test_database_is_permitted_and_mutable(conn: Connection) -> None:
    """The guard must not be so strict that it stops the suite working.

    A guard that refused everything would pass its own negative tests and be useless,
    so this asserts the positive case with a real write on the fixture connection every
    other integration test uses.
    """
    assert database_of(conn) in TEST_DATABASES
    assert require_test_database(conn) == database_of(conn)

    created = provision_reviewer(
        conn,
        email=f"isolation-{uuid.uuid4().hex[:8]}@example.test",
        display_name="Isolation fixture",
        test_only=True,
    )
    assert created.id is not None, "the permitted database must still accept a fixture write"


def test_every_db_backed_test_passes_through_the_guarded_fixture(
    postgres_dsn: str,
) -> None:
    """`postgres_dsn` is the chokepoint, so the guard cannot be forgotten.

    `owner_engine`, `conn`, `role_engines` and `runtime_role_passwords` all descend from
    it. Reaching this assertion at all means the guard already ran and passed -- which is
    the design: no test author has to remember to call it.
    """
    assert urlparse(postgres_dsn).path.lstrip("/") in TEST_DATABASES


# ===========================================================================
# 2. The real database is refused, and refused BEFORE anything is written
# ===========================================================================


def test_the_real_database_is_refused_by_the_guard(real_database: Connection) -> None:
    assert database_of(real_database) == REAL_DATABASE
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        require_test_database(real_database, context="isolation test")


def test_the_real_dsn_is_refused_before_an_engine_is_built(postgres_dsn: str) -> None:
    """The DSN form, which is what the pytest fixture uses."""
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        require_test_database_dsn(_dsn_for(postgres_dsn, REAL_DATABASE))


def test_a_fixture_identity_is_refused_in_the_real_database(
    real_database: Connection,
) -> None:
    """Section 7, and the layer that would actually have caught the incident.

    The script in 5C.7E called exactly this, with exactly these arguments, against
    exactly this database.
    """
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        provision_reviewer(
            real_database,
            email=f"isolation-{uuid.uuid4().hex[:8]}@example.test",
            display_name="Should never exist",
            test_only=True,
        )


def test_fixture_token_issuance_is_refused_in_the_real_database(
    real_database: Connection,
) -> None:
    """The second half of the incident: nine audit rows came from issuing challenges."""
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        issue(
            real_database,
            email="softhub12225@gmail.com",
            issued_by=None,
            allow_test_identity=True,
        )


def test_the_rejection_writes_nothing_to_the_real_database(
    real_database: Connection,
) -> None:
    """Section 8's critical assertion: refusing must not itself leave a trace.

    The audit chain is append-only, so a guard that appended anything -- even a record of
    its own refusal -- would be doing the thing it exists to prevent, permanently.
    """
    before = {
        table: real_database.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in ("audit_log", "app_user", "user_role", "credential_enrollment")
    }

    for attempt in (
        lambda: require_test_database(real_database),
        lambda: provision_reviewer(
            real_database,
            email=f"isolation-{uuid.uuid4().hex[:8]}@example.test",
            display_name="Should never exist",
            test_only=True,
        ),
        lambda: issue(
            real_database,
            email="softhub12225@gmail.com",
            issued_by=None,
            allow_test_identity=True,
        ),
    ):
        with pytest.raises(RealDatabaseRefusedError):
            attempt()

    after = {
        table: real_database.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in ("audit_log", "app_user", "user_role", "credential_enrollment")
    }
    assert before == after, f"a refused attempt changed the real database: {before} -> {after}"


def test_dejan_is_untouched_by_the_refused_attempts(real_database: Connection) -> None:
    """The specific thing being protected, asserted by name: *unchanged*, not *empty*.

    The first version of this test asserted absolute values -- `password_hash IS NULL`
    and zero challenges -- and broke the moment the operator legitimately issued the real
    enrolment token. That was the test's fault, not the system's: it had encoded a
    transient pre-handoff state as an invariant, so it would have failed again as soon as
    Dejan set his password, which is the entire point of the exercise.

    What must hold forever is that a **refused attempt changes nothing**. So this reads
    his state, runs the attempts, and reads it again. It stays true before the handoff,
    between issuance and enrolment, and after.
    """

    def snapshot() -> tuple[object, ...] | None:
        row = real_database.execute(
            text(
                "SELECT is_active, (password_hash IS NULL) AS pw_null, display_name, "
                "  (SELECT count(*) FROM credential_enrollment ce "
                "     WHERE ce.user_id = u.id) AS tokens, "
                "  (SELECT count(*) FROM credential_enrollment ce "
                "     WHERE ce.user_id = u.id AND ce.used_at IS NOT NULL) AS spent "
                "  FROM app_user u WHERE email = 'softhub12225@gmail.com'"
            )
        ).one_or_none()
        return tuple(row) if row is not None else None

    before = snapshot()
    if before is None:
        pytest.skip("the real reviewer is not provisioned on this server")

    for attempt in (
        lambda: require_test_database(real_database),
        lambda: provision_reviewer(
            real_database,
            email="softhub12225@gmail.com",
            display_name="Should never overwrite the real reviewer",
            test_only=True,
        ),
        lambda: issue(
            real_database,
            email="softhub12225@gmail.com",
            issued_by=None,
            allow_test_identity=True,
        ),
    ):
        with pytest.raises(RealDatabaseRefusedError):
            attempt()

    assert snapshot() == before, "a refused attempt changed the real reviewer's state"


# ===========================================================================
# 3. Fail closed: an unrecognised database is refused like the real one
# ===========================================================================


def test_a_database_that_is_not_on_the_allow_list_is_refused(conn: Connection) -> None:
    """Section 3. The database the guard was never told about is the one it exists for."""
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        require_test_database(conn, allow={"some-database-nobody-configured"})


def test_the_gates_database_is_not_a_default_test_target(postgres_dsn: str) -> None:
    """Section 26. `datahub_gates` exists, is six migrations behind, and no code selects it.

    It is deliberately absent from `TEST_DATABASES`: a database nothing references must
    not become a silent default just because it happens to exist. A workflow that really
    wants it has to name it, and naming it is the documentation.
    """
    assert GATES_DATABASE not in TEST_DATABASES

    gates = _dsn_for(postgres_dsn, GATES_DATABASE)
    engine = create_engine(gates, future=True)
    try:
        with engine.connect() as probe:
            if database_of(probe) != GATES_DATABASE:
                pytest.skip(f"{GATES_DATABASE} is not present on this server")
    except Exception:
        pytest.skip(f"{GATES_DATABASE} is not present on this server")
    finally:
        engine.dispose()

    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        require_test_database_dsn(gates)

    # ...and it is reachable when a workflow names it explicitly, which is the only way.
    assert require_test_database_dsn(gates, allow={GATES_DATABASE}) == GATES_DATABASE


def test_forbid_real_database_permits_everything_except_the_real_one(
    conn: Connection, real_database: Connection
) -> None:
    """The weaker sibling, used where an allow-list would be wrong.

    `provision_reviewer` must keep working for real reviewers in every database including
    the real one -- that is how Dejan exists. Only the `test_only` path is restricted.
    """
    assert forbid_real_database(conn) in TEST_DATABASES
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        forbid_real_database(real_database)


def test_a_real_reviewer_can_still_be_provisioned_in_the_test_database(
    conn: Connection,
) -> None:
    """Section 7's caveat: legitimate real-identity creation is not prohibited.

    A guard that also blocked real reviewers would have made Dejan unprovisionable.
    """
    created = provision_reviewer(
        conn,
        email=f"real-{uuid.uuid4().hex[:8]}@example.org",
        display_name="A Real Reviewer",
        test_only=False,
    )
    assert created.is_test is False


# ===========================================================================
# 4. No escape hatch (section 9)
# ===========================================================================


@pytest.mark.parametrize(
    "variable",
    ["ALLOW_REAL_DB_TESTS", "DATAHUB_ALLOW_REAL_DB", "PYTEST_ALLOW_REAL_DATABASE"],
)
def test_no_environment_variable_unlocks_the_real_database(
    real_database: Connection, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Section 9. An env flag is one inherited export away from being always-on.

    Widening the allow-list must be a code change, because a code change is reviewable
    and an exported shell variable is not.
    """
    monkeypatch.setenv(variable, "1")
    with pytest.raises(RealDatabaseRefusedError, match=REFUSAL):
        require_test_database(real_database)


def test_the_guard_reads_no_environment_variable_at_all(
    monkeypatch: pytest.MonkeyPatch, conn: Connection
) -> None:
    """Stated structurally: the module must not consult the environment.

    Reading the environment is how the original defect happened. Asserting the source
    contains no such lookup is cheap and catches a future "just add a flag" edit.
    """
    import inspect

    from app.db import safety

    source = inspect.getsource(safety)
    for forbidden in ("os.environ", "os.getenv", "getenv("):
        assert forbidden not in source, (
            f"app.db.safety consults the environment ({forbidden}); database identity "
            "must be the sole authority"
        )
    # And behaviourally: a hostile environment changes nothing.
    monkeypatch.setenv("POSTGRES_DB", REAL_DATABASE)
    assert require_test_database(conn) in TEST_DATABASES
    assert os.environ["POSTGRES_DB"] == REAL_DATABASE


# ===========================================================================
# 5. The suite's own configuration
# ===========================================================================


def test_the_runtime_role_engines_are_also_pointed_at_the_test_database(
    role_engines: dict[str, Engine],
) -> None:
    """The privilege tests authenticate as real runtime roles; they must not do it here.

    `role_engines` derives from `postgres_dsn`, so it inherits the guard -- this asserts
    the derivation was not lost.
    """
    for role, engine in role_engines.items():
        with engine.connect() as connection:
            assert (
                database_of(connection) in TEST_DATABASES
            ), f"{role} is connected to {database_of(connection)}"
