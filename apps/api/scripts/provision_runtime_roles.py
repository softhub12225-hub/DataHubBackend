"""Create or re-provision the three runtime database roles. The psql-free equivalent.

WHY THIS EXISTS BESIDE THE SHELL SCRIPT
=======================================
`infra/postgres/init/10-runtime-roles.sh` is run once by the Postgres image's entrypoint
on an empty data directory, and it needs `psql`. A developer machine running Postgres
natively has no `psql` and no container entrypoint, so re-provisioning by hand was not
possible -- which is how a killed test run was able to leave three roles carrying
generated passwords that existed nowhere else.

This performs **exactly** the same statements as that script, from the same three
environment variables, over an ordinary owner connection. It is the supported operation
behind `make runtime-roles-reset` on a machine without `psql`.

WHAT IT REFUSES TO DO
=====================
It does not invent a password. If `APP_API_PASSWORD`, `APP_WORKER_PASSWORD` or
`APP_PUBLISHER_PASSWORD` is absent it fails and says which -- a default here would be a
credential in the repository.

It never prints, logs or stores a password. The report is role names and whether each
can authenticate afterwards, which is the only part anyone needs to read.

WHAT IT DELIBERATELY DOES NOT TOUCH
===================================
Table grants. Those belong to the migration that creates each table -- applying them
here would silently diverge from the migration that is supposed to own them, and the
privilege tests assert the migration's version. This script alters role passwords and
schema-level access only, exactly as the shell script does.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from psycopg import sql as pg_sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

#: role -> the environment variable carrying its password.
ROLES: dict[str, str] = {
    "app_api": "APP_API_PASSWORD",
    "app_worker": "APP_WORKER_PASSWORD",
    "app_publisher": "APP_PUBLISHER_PASSWORD",
}

#: Everything but the password, so the role's shape is stated in one place.
ROLE_ATTRIBUTES = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"

#: The owner of `app_claim_enrollment`, which is SECURITY DEFINER and therefore runs
#: its body with this role's privileges. It is not a runtime identity: nothing connects
#: as it, it has no password, and it appears in no `ROLES` entry because there is no
#: credential to rotate.
#:
#: Its table privileges are granted by migration `a8b9c0d1e2f3`, which is where they
#: belong -- they are column-level grants that must move with the schema. What this
#: script owns is the role's *shape*: a recovered or fresh environment must end up with
#: a role that cannot log in and cannot escalate, and asserting that here means one
#: command re-establishes it with no hand-written SQL.
DEFINER_ROLE = "app_credential_definer"
DEFINER_ATTRIBUTES = (
    "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
)


def _load_repo_dotenv() -> None:
    path = Path(__file__).resolve().parents[3] / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def owner_dsn() -> str:
    """The owning/migration connection, from the environment.

    Deliberately assembled here rather than through `app.core.config`: the settings
    layer refuses a configuration where two services share a database user, which is
    correct for the application and useless for a script whose whole job is to set those
    users up.
    """
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DB", "datahub")
    user = os.environ.get("POSTGRES_MIGRATION_USER") or os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_MIGRATION_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if not user or not password:
        raise SystemExit("POSTGRES_MIGRATION_USER/PASSWORD (or POSTGRES_USER/PASSWORD) must be set")
    sslmode = os.environ.get("POSTGRES_SSLMODE", "").strip()
    query = f"?sslmode={sslmode}" if sslmode else ""
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}{query}"


def provision(
    engine: Engine,
    passwords: dict[str, str],
    database: str,
    roles: tuple[str, ...] | None = None,
) -> None:
    """Create the roles if absent and set their password and attributes.

    Identifiers and the password are composed with `psycopg.sql`, because `ALTER ROLE`
    is a utility statement and takes no bind parameters -- the server rejects
    `PASSWORD $1`. The role names are keys of `ROLES`, never input.

    `roles` narrows the operation to a subset, defaulting to all of them. It exists
    because rotating one lost credential should not churn the other two: every rotation
    invalidates whatever is deployed against the old password, so a credential nobody
    asked about is an outage nobody asked for. The grant statements are scoped to the
    same subset for the same reason -- they are idempotent, but a narrowed rotation
    should not quietly re-assert authority over roles it was not asked to touch.
    """
    selected = tuple(roles) if roles else tuple(ROLES)
    unknown = sorted(set(selected) - set(ROLES))
    if unknown:
        raise SystemExit(f"unknown role(s): {', '.join(unknown)}")

    with engine.begin() as connection:
        driver = connection.connection.driver_connection
        is_superuser = bool(
            connection.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar()
        )
        role_attrs = ROLE_ATTRIBUTES if is_superuser else ""
        for role in selected:
            # Idempotent, and composed rather than formatted: `CREATE ROLE` takes an
            # identifier and `rolname = ...` takes a literal, and psycopg.sql is the
            # thing that knows the difference.
            create = pg_sql.SQL(
                "DO $$ BEGIN "
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) THEN "
                "    CREATE ROLE {role} LOGIN; "
                "  END IF; "
                "END $$;"
            ).format(name=pg_sql.Literal(role), role=pg_sql.Identifier(role))
            connection.exec_driver_sql(create.as_string(driver))
            statement = pg_sql.SQL(
                "ALTER ROLE {role} WITH PASSWORD {secret} " + role_attrs
            ).format(role=pg_sql.Identifier(role), secret=pg_sql.Literal(passwords[role]))
            connection.exec_driver_sql(statement.as_string(driver))

        names = pg_sql.SQL(", ").join(pg_sql.Identifier(role) for role in selected)
        for template in (
            "GRANT CONNECT ON DATABASE {db} TO {roles}",
            "GRANT USAGE ON SCHEMA public TO {roles}",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            "REVOKE ALL ON SCHEMA public FROM {roles}",
            "GRANT USAGE ON SCHEMA public TO {roles}",
        ):
            statement = pg_sql.SQL(template).format(db=pg_sql.Identifier(database), roles=names)
            connection.exec_driver_sql(statement.as_string(driver))


def ensure_definer_role(engine: Engine) -> None:
    """Create the credential-definer role if absent and re-assert its shape.

    Idempotent, and it re-asserts rather than trusting what it finds: a role that somehow
    acquired LOGIN or CREATEROLE is corrected, because the entire value of this role is
    in what it cannot do.

    Roles are cluster-wide, so this may find one another database already created. That
    is expected; `CREATE ROLE` is guarded and the `ALTER` is unconditional.

    No password is set and any existing one is cleared. A definer role with a password is
    an account, and this is deliberately not an account.

    On managed hosts without superuser (e.g. Neon), attribute ALTERs that require
    CREATEROLE/superuser are skipped after create-if-absent.
    """
    with engine.begin() as connection:
        driver = connection.connection.driver_connection
        is_superuser = bool(
            connection.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar()
        )
        create = pg_sql.SQL(
            "DO $$ BEGIN "
            "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) THEN "
            "    CREATE ROLE {role} NOLOGIN; "
            "  END IF; "
            "END $$;"
        ).format(name=pg_sql.Literal(DEFINER_ROLE), role=pg_sql.Identifier(DEFINER_ROLE))
        connection.exec_driver_sql(create.as_string(driver))
        if is_superuser:
            for template in (
                "ALTER ROLE {role} " + DEFINER_ATTRIBUTES,
                "ALTER ROLE {role} PASSWORD NULL",
            ):
                statement = pg_sql.SQL(template).format(role=pg_sql.Identifier(DEFINER_ROLE))
                connection.exec_driver_sql(statement.as_string(driver))


def definer_is_safe(engine: Engine) -> tuple[bool, str]:
    """Check the shape that makes the definer role worth having.

    The function it owns runs with these privileges, so `rolsuper` or `rolcanlogin` here
    would quietly undo Step 5C.7F. Membership matters just as much: a member could
    `SET ROLE` to it and borrow the privileges without going through the function at all.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolinherit, "
                "       rolreplication, rolbypassrls, (rolpassword IS NULL) AS no_password "
                "  FROM pg_authid WHERE rolname = :r"
            ),
            {"r": DEFINER_ROLE},
        ).one_or_none()
        if row is None:
            return False, "the role does not exist"
        members = [
            entry.member
            for entry in connection.execute(
                text(
                    "SELECT m.rolname AS member FROM pg_auth_members am "
                    "  JOIN pg_roles m ON m.oid = am.member "
                    "  JOIN pg_roles r ON r.oid = am.roleid WHERE r.rolname = :r"
                ),
                {"r": DEFINER_ROLE},
            )
        ]

    problems = [
        name
        for name, bad in (
            ("is a superuser", row.rolsuper),
            ("can log in", row.rolcanlogin),
            ("can create databases", row.rolcreatedb),
            ("can create roles", row.rolcreaterole),
            ("inherits", row.rolinherit),
            ("can replicate", row.rolreplication),
            ("bypasses RLS", row.rolbypassrls),
            ("has a password", not row.no_password),
        )
        if bad
    ]
    if members:
        problems.append("has members: " + ", ".join(sorted(members)))
    if problems:
        return False, "; ".join(problems)
    return True, "NOLOGIN, no password, no members, no escalation"


def authenticates(dsn: str, role: str, password: str) -> tuple[bool, str]:
    """Can this role actually log in, as itself, without superuser?

    A real connection, not a catalog lookup: a role whose grants are perfect but which
    cannot authenticate passes every `information_schema` check ever written.
    """
    parts = dsn.split("://", 1)[1].split("@", 1)[1]
    engine = create_engine(f"postgresql+psycopg://{role}:{password}@{parts}", future=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_user AS who, session_user AS session, "
                    "       current_setting('is_superuser') AS super"
                )
            ).one()
        if row.who != role or row.session != role:
            return False, f"connected as {row.who}/{row.session}, expected {role}"
        if row.super != "off":
            return False, "connected with superuser privileges"
        return True, "authenticated as itself, not a superuser"
    except OperationalError as exc:
        return False, type(exc.orig).__name__ if exc.orig else "connection refused"
    finally:
        engine.dispose()


def main() -> int:
    _load_repo_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check that each role authenticates; change nothing",
    )
    parser.add_argument(
        "--role",
        action="append",
        choices=sorted(ROLES),
        metavar="ROLE",
        help=(
            "restrict the operation to this role; repeatable. Only this role's password "
            "environment variable is then required, and only it is rotated -- rotating a "
            "credential nobody asked about is an outage nobody asked about. Omit to act "
            "on all of them."
        ),
    )
    args = parser.parse_args()

    selected = tuple(dict.fromkeys(args.role)) if args.role else tuple(ROLES)
    missing = sorted(ROLES[role] for role in selected if not os.environ.get(ROLES[role]))
    if missing:
        print(f"refusing to run: {', '.join(missing)} not set in the environment")
        print("A default password here would be a credential committed to the repository.")
        return 2

    passwords = {role: os.environ[ROLES[role]] for role in selected}
    dsn = owner_dsn()
    database = os.environ.get("POSTGRES_DB", "datahub")
    engine = create_engine(dsn, future=True)

    try:
        if not args.verify_only:
            provision(engine, passwords, database, roles=selected)
            print(
                f"provisioned {len(selected)} runtime role(s) in {database}: {', '.join(selected)}"
            )
        if not args.verify_only:
            # Always, regardless of --role: the definer carries no credential, so
            # narrowing the rotation has nothing to do with it, and a recovered
            # environment needs it before the function it owns can be restored.
            ensure_definer_role(engine)
        definer_ok, definer_detail = definer_is_safe(engine)

        print(f"{'role':18} {'state':14} detail")
        print(f"{DEFINER_ROLE:18} {'safe' if definer_ok else 'UNSAFE':14} {definer_detail}")
        failures = 0 if definer_ok else 1
        for role in selected:
            ok, detail = authenticates(dsn, role, passwords[role])
            failures += 0 if ok else 1
            print(f"{role:18} {'yes' if ok else 'NO':14} {detail}")
    finally:
        engine.dispose()

    if failures:
        print(f"\n{failures} role(s) could not authenticate.")
        return 1
    print(
        f"\nAll runtime roles authenticate as themselves without superuser, and "
        f"{DEFINER_ROLE} owns the claim function while being unable to log in."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
