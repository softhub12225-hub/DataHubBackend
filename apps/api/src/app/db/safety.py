"""Refusing to let test and fixture code mutate the real pilot database.

THE INCIDENT THIS CLOSES
========================
In Step 5C.7E a live verification script built its DSN from the project settings. `.env`
sets ``POSTGRES_DB=datahub``, so it connected to the **real pilot database**, created
eight ``[TEST ONLY]`` identities, issued them enrolment challenges and deleted them
again. The identities and their challenges went away with the cascade; the nine
``CREDENTIAL_ENROLLMENT_ISSUED`` rows did not, because ``audit_log`` is append-only by
design. They are still there, and they should be -- see ``SOURCE_VERIFICATION`` §20.

Nothing was corrupted and no trust state moved. It was still a script writing to the
production-equivalent database because a default it never mentioned happened to point
there.

WHY THE CHECK IS A QUERY, NOT A STRING COMPARISON
=================================================
The obvious guard -- read ``POSTGRES_DB`` and compare, or require ``DATAHUB_ENV=test`` --
is the thing that already failed. The environment *said* ``datahub`` and that was exactly
the problem: the variable was correct, the intent was not, and no amount of reading the
same variable more carefully would have caught it.

So the guard asks the **server** what it is connected to::

    SELECT current_database()

That answer cannot be wrong. It survives a DSN assembled from parts, an inherited
environment, a pooled connection, a ``PGDATABASE`` nobody remembered, and a helpfully
"fixed" default. Environment configuration may be checked as well, but database identity
is the authority.

FAIL CLOSED, AND NO WAY ROUND IT
================================
An unknown database is refused exactly like the real one. A guard that allowed anything
it did not recognise would pass on the very case it exists for -- a database created
later, by someone who never read this file.

There is deliberately **no environment variable that relaxes this**. A flag such as
``ALLOW_REAL_DB_TESTS=1`` is one inherited shell export away from being always-on, which
is the same class of accident as the one above. The allow-list is a code-level argument:
widening it is a diff, and a diff is reviewable.

An extraordinary maintenance operation that genuinely must write to ``datahub`` belongs
in a separately named administrative command that never calls this, not in the test
runner.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, create_engine, text

#: The real pilot database. Holds the 175 snapshots, the 385 collected responsibilities,
#: Dejan's identity and the append-only audit chain. Never a test target.
REAL_DATABASE = "datahub"

#: Where DB-backed tests are allowed to write. One entry on purpose.
TEST_DATABASES: frozenset[str] = frozenset({"datahub_test"})

#: `datahub_gates` is a Step 5C.3 leftover, six migrations behind, that nothing in the
#: repository references. It is NOT in `TEST_DATABASES`: a database no code selects must
#: not become a silent default. A workflow that genuinely wants it passes it explicitly
#: and says why, which is the documentation.
GATES_DATABASE = "datahub_gates"

#: The message a refusal carries. Greppable on purpose: when this fires in CI, the first
#: thing anyone does is search for the string.
REFUSAL = "REFUSING_REAL_DATABASE_TEST_MUTATION"


class RealDatabaseRefusedError(RuntimeError):
    """Test or fixture code tried to act on a database it may not mutate.

    Not a warning. The caller is about to write, and by the time a warning is read the
    write has happened.
    """


def database_of(connection: Connection) -> str:
    """Ask the server which database this connection is actually attached to."""
    return str(connection.execute(text("SELECT current_database()")).scalar_one())


def require_test_database(
    connection: Connection, *, allow: Iterable[str] = TEST_DATABASES, context: str = ""
) -> str:
    """Refuse unless the live connection is attached to a permitted test database.

    Returns the database name so a caller can log or assert on it. Raises before the
    caller does anything else, which is the whole point: this must run ahead of the
    first INSERT, UPDATE, DELETE, DDL, audit append, fixture identity or issued token.
    """
    permitted = frozenset(allow)
    actual = database_of(connection)
    if actual in permitted:
        return actual

    where = f" ({context})" if context else ""
    if actual == REAL_DATABASE:
        raise RealDatabaseRefusedError(
            f"{REFUSAL}: connected to the real pilot database {actual!r}{where}. "
            f"Permitted here: {sorted(permitted)}. Point DATAHUB_TEST_POSTGRES_DSN at a "
            "test database. Note that POSTGRES_DB defaults to the real one, so a DSN "
            "assembled from the project settings reaches production data."
        )
    raise RealDatabaseRefusedError(
        f"{REFUSAL}: connected to {actual!r}{where}, which is not a permitted test "
        f"database. Permitted here: {sorted(permitted)}. An unrecognised database is "
        "refused exactly like the real one -- fail closed, because the database this "
        "guard was not told about is the one it exists for."
    )


def require_test_database_dsn(
    dsn: str, *, allow: Iterable[str] = TEST_DATABASES, context: str = ""
) -> str:
    """The same check, for a caller that holds a DSN and no connection yet.

    Opens its own short-lived connection and disposes of it, so the check happens before
    the caller builds the engine it intends to write through. Parsing the database out
    of the DSN string would be cheaper and would reproduce the original defect: the
    string is not the authority, the server is.
    """
    engine = create_engine(dsn, future=True)
    try:
        with engine.connect() as connection:
            return require_test_database(connection, allow=allow, context=context)
    finally:
        engine.dispose()


def forbid_real_database(connection: Connection, *, context: str = "") -> str:
    """Refuse only the real pilot database, permitting anything else.

    The weaker sibling of `require_test_database`, for the one case where an allow-list
    is wrong: production code paths that are legitimate in any database except that
    creating a `[TEST ONLY]` identity in the real one is never legitimate. Used by
    `provision_reviewer`, which must keep working for real reviewers everywhere.
    """
    actual = database_of(connection)
    if actual == REAL_DATABASE:
        where = f" ({context})" if context else ""
        raise RealDatabaseRefusedError(
            f"{REFUSAL}: refusing to create a fixture identity in the real pilot "
            f"database {actual!r}{where}. A [TEST ONLY] identity in the real database is "
            "how a fixture decision ends up in a real audit trail -- which is exactly "
            "what happened in Step 5C.7E."
        )
    return actual


__all__ = [
    "GATES_DATABASE",
    "REAL_DATABASE",
    "REFUSAL",
    "TEST_DATABASES",
    "RealDatabaseRefusedError",
    "database_of",
    "forbid_real_database",
    "require_test_database",
    "require_test_database_dsn",
]
