"""Test-only credentials for the real runtime roles (Step 5C.4 section 13).

WHY THIS EXISTS
===============
Fifteen privilege tests were skipping. They skip when `APP_API_PASSWORD`,
`APP_WORKER_PASSWORD` or `APP_PUBLISHER_PASSWORD` is unset, and on this machine all
three are unset and **unrecoverable**: there is no `.env` anywhere in the repository,
and `pg_authid` stores only a SCRAM-SHA-256 verifier, from which no plaintext can be
derived. The roles themselves exist, can log in, and carry the production grants.

A skipped privilege test is not a passing one. The grants those tests describe are the
mechanism that stops a worker writing canonical facts, and "we could not check" is the
same evidence as "it does not work".

WHAT IT DOES INSTEAD
====================
At session setup, using the owner connection the test suite already has, each runtime
role's password is rotated to a freshly generated value that exists only in this
process's memory. Tests then authenticate as `app_api`, `app_worker` and
`app_publisher` for real, over the same `scram-sha-256` path production uses, with the
same role, the same grants and the same `NOINHERIT`. Nothing about the privilege
semantics under test is simulated.

At teardown the **original verifier is restored verbatim**. `pg_authid.rolpassword`
holds a complete SCRAM verifier string, and feeding that string back to `ALTER ROLE ...
PASSWORD` stores it unchanged -- so the role ends the run byte-identical to how it
started, and whoever does hold the real password still holds it. Measured: the verifier
md5 goes `428acadf...` -> rotated -> `428acadf...`.

WHY NOT THE ALTERNATIVES
========================
*Member roles.* Creating a test role and granting it `app_api` authenticates as the
member, not as `app_api`. Section 13 requires the tests to exercise the actual runtime
roles, and a member's privileges are not the role's -- `NOINHERIT` alone changes the
answer.

*Catalog assertions.* Reading `information_schema.role_table_grants` proves a grant was
written, not that the server enforces it. Section 13 forbids this substitution for
tests whose subject is runtime behaviour, and it is right to: the interesting failures
are the ones where the catalog looks correct.

*`SET ROLE` from the owner.* Reproduces the privileges but not the authentication, and
a superuser's `SET ROLE` is reversible by the session itself. It would not detect a
role that cannot log in at all.

SAFETY
======
- **Loopback only.** Refuses to rotate anything unless the DSN's host is a loopback
  address. A remote host means the database is shared, and rotating a shared
  credential is somebody else's outage.
- **Generated, never stored.** `secrets.token_urlsafe(32)` per role per run. The value
  is never written to a file, never logged, and never put in an environment variable
  that outlives the process.
- **Restored, and verified.** Teardown re-applies the saved verifier and asserts the
  stored bytes match what was read at setup.
- **Serialised, and re-entrant.** An advisory lock, so two concurrent test runs cannot
  interleave a rotation with a restore. Re-entrant within one process, because a
  PostgreSQL advisory lock is held per connection: a nested call on a second connection
  would otherwise wait for the outer one forever, and a hung test run is how the
  restore gets skipped.
- **Opt-out.** If the three environment variables are set, this does nothing at all and
  the real credentials are used, unchanged -- which is what CI does.

WHAT A HARD KILL COSTS
======================
The restore runs in a `finally`, so an exception, a failed assertion or an ordinary
interrupt are all safe. `SIGKILL` is not: the process dies without running anything, and
the original verifiers are then **unrecoverable** -- they exist nowhere else, which is
the same reason this module has to generate a password in the first place.

The blast radius is bounded by the opt-out above: this path only runs when the three
environment variables are unset, meaning no configured consumer is using those
credentials. The remedy is to re-run `infra/postgres/init/10-runtime-roles.sh`, which
sets them from the environment again. They are not stored anywhere else on purpose: a
backup of a credential verifier, written to make a test tidier, is a worse thing to have
than an interrupted test run.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from psycopg import sql as pg_sql
from sqlalchemy import create_engine, text

#: The roles under test, in the order the grants were written.
RUNTIME_ROLES: tuple[str, ...] = ("app_api", "app_worker", "app_publisher")

#: Environment variable holding each role's real password, when there is one.
ENV_VARS: dict[str, str] = {
    "app_api": "APP_API_PASSWORD",
    "app_worker": "APP_WORKER_PASSWORD",
    "app_publisher": "APP_PUBLISHER_PASSWORD",
}

#: One arbitrary, fixed key so concurrent test runs on one server serialise. Chosen
#: once and never derived from anything, because two runs must pick the same number.
_ADVISORY_LOCK_KEY = 5_304_130_013

#: Hosts where rotating a credential affects only this machine.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})

#: How many times this process is inside `ephemeral_role_passwords`. A PostgreSQL
#: advisory lock is held per connection, so a nested call would take a second connection
#: and wait on the outer one for ever. Nesting is legitimate -- a test that checks the
#: restore has to rotate inside a session whose fixture already rotated -- and it is the
#: inner block's job to restore whatever it found, which it does.
_DEPTH = 0


class ProvisioningUnavailableError(RuntimeError):
    """Ephemeral credentials cannot be created here, with the specific reason why."""


@dataclass(frozen=True, slots=True)
class _Saved:
    """One role's stored verifier, as read before rotation.

    `verifier` is None when the role had no password at all, which must be restored as
    faithfully as a password: leaving a generated one behind would be a new credential
    on a role that had none.
    """

    role: str
    verifier: str | None


def _is_loopback(dsn: str) -> bool:
    host = (urlsplit(dsn).hostname or "").strip().lower()
    return host in _LOOPBACK


def _remote_refusal(dsn: str) -> str:
    """Why a non-loopback DSN is refused, naming the host so it is actionable."""
    host = urlsplit(dsn).hostname or "?"
    return (
        f"the test database is at {host!r}, not a loopback address. Rotating a "
        "runtime role's password on a shared server would break every other "
        "consumer of that credential, so it is refused."
    )


def _blocker(dsn: str, connection: object) -> str | None:
    """The precise reason provisioning cannot happen, or None when it can."""
    if not _is_loopback(dsn):
        return _remote_refusal(dsn)
    row = connection.execute(  # type: ignore[attr-defined]
        text(
            "SELECT current_user AS who, usesuper AS super "
            "  FROM pg_user WHERE usename = current_user"
        )
    ).one_or_none()
    if row is None or not row.super:
        who = row.who if row is not None else "unknown"
        return (
            f"the owner connection authenticates as {who!r}, which is not a superuser. "
            "Reading pg_authid.rolpassword to save the existing verifier, and writing "
            "it back afterwards, both require superuser; without it a rotation could "
            "not be undone."
        )
    missing = [
        role
        for role in RUNTIME_ROLES
        if connection.execute(  # type: ignore[attr-defined]
            text("SELECT 1 FROM pg_roles WHERE rolname = :r AND rolcanlogin"), {"r": role}
        ).one_or_none()
        is None
    ]
    if missing:
        return (
            f"runtime role(s) {', '.join(missing)} do not exist or cannot log in. "
            "Run infra/postgres/init/10-runtime-roles.sh to create them."
        )
    return None


def _read_verifier(connection: object, role: str) -> str | None:
    row = connection.execute(  # type: ignore[attr-defined]
        text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"), {"r": role}
    ).one_or_none()
    return None if row is None else row.rolpassword


def _set_password(connection: object, role: str, value: str | None) -> None:
    """Set or clear a role's password.

    `ALTER ROLE` is a utility statement and takes no bind parameters -- the server
    rejects `PASSWORD $1` outright -- so both the role name and the secret have to be
    literals in the statement text. They are composed by `psycopg.sql`, which quotes and
    escapes them properly, rather than by string formatting. The role is also checked
    against `RUNTIME_ROLES` first, so the identifier can only ever be one of three
    constants.

    One consequence worth stating plainly: the secret appears in the statement text, so
    a server with `log_statement = 'ddl'` or `'all'` will log it. That is acceptable
    only because the value is generated for this run, is restored at teardown, and is
    never a production credential -- see the module docstring.
    """
    if role not in RUNTIME_ROLES:
        raise ProvisioningUnavailableError(f"refusing to alter {role!r}: not a runtime role")
    driver = connection.connection.driver_connection  # type: ignore[attr-defined]
    if value is None:
        statement = pg_sql.SQL("ALTER ROLE {role} PASSWORD NULL").format(
            role=pg_sql.Identifier(role)
        )
    else:
        statement = pg_sql.SQL("ALTER ROLE {role} PASSWORD {secret}").format(
            role=pg_sql.Identifier(role), secret=pg_sql.Literal(value)
        )
    connection.exec_driver_sql(statement.as_string(driver))  # type: ignore[attr-defined]


@contextmanager
def ephemeral_role_passwords(
    owner_dsn: str, roles: tuple[str, ...] = RUNTIME_ROLES
) -> Iterator[dict[str, str]]:
    """Give the named runtime roles a fresh password for the duration of the block.

    Yields role -> password. Restores every original verifier on the way out, including
    when the body raises, and asserts the restoration actually took.

    Raises `ProvisioningUnavailableError` with a specific reason when it cannot be done, so
    the caller can skip with that reason rather than a generic one.

    ROTATE ONLY WHAT HAS TO BE ROTATED
    ==================================
    `roles` exists because rotating a credential that somebody is *using* breaks them.
    This originally rotated all three unconditionally, which was defensible while none of
    the real passwords existed in any recoverable form on this machine. It stopped being
    defensible the moment one did: Step 5C.7E provisioned a real `app_api` password into
    `.env`, and the first thing that happened was a test run rotating it out from under a
    live `reviewer-enrol-password`, which failed with *"password authentication failed for
    user app_api"* seconds after the operator pasted a valid token.

    So the caller passes only the roles for which no real password is available. A role
    whose password is supplied is left strictly alone -- not read, not rotated, not
    restored -- because the safest handling of a working credential is not to touch it.
    """
    global _DEPTH

    # Before anything is opened. The loopback guard is the one check that must not
    # depend on reaching the server: a DSN pointing somewhere shared should be refused
    # by inspection, not after a connection attempt that may itself hang or fail with a
    # DNS error that hides the real reason.
    if not _is_loopback(owner_dsn):
        raise ProvisioningUnavailableError(_remote_refusal(owner_dsn))

    selected = tuple(roles)
    unknown = sorted(set(selected) - set(RUNTIME_ROLES))
    if unknown:
        raise ProvisioningUnavailableError(f"not runtime roles: {', '.join(unknown)}")
    if not selected:
        # Nothing to do, and nothing to restore. Still a context manager, so the caller
        # does not need a second code path for "every password was supplied".
        yield {}
        return

    engine = create_engine(owner_dsn, future=True, poolclass=None)
    saved: list[_Saved] = []
    generated: dict[str, str] = {}
    outermost = _DEPTH == 0
    try:
        with engine.begin() as connection:
            reason = _blocker(owner_dsn, connection)
            if reason is not None:
                raise ProvisioningUnavailableError(reason)
            if outermost:
                # Taken once per process. `pg_try_advisory_lock` rather than the
                # blocking form: another run holding it is a condition to report, not
                # one to wait out, because waiting is indistinguishable from a hang and
                # a killed run is a run whose restore never happens.
                acquired = connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
                ).scalar()
                if not acquired:
                    raise ProvisioningUnavailableError(
                        "another test run already holds the credential-provisioning lock "
                        "on this server. Run them one at a time, or set "
                        "APP_API_PASSWORD / APP_WORKER_PASSWORD / APP_PUBLISHER_PASSWORD "
                        "so that neither run needs to provision."
                    )
        _DEPTH += 1
        with engine.begin() as connection:
            for role in selected:
                saved.append(_Saved(role=role, verifier=_read_verifier(connection, role)))
                secret = secrets.token_urlsafe(32)
                _set_password(connection, role, secret)
                generated[role] = secret
        yield generated
    finally:
        if saved:
            _DEPTH -= 1
        with engine.begin() as connection:
            for entry in saved:
                _set_password(connection, entry.role, entry.verifier)
                restored = _read_verifier(connection, entry.role)
                if restored != entry.verifier:
                    raise ProvisioningUnavailableError(
                        f"failed to restore {entry.role}'s original verifier; the role "
                        "has been left with a password this run generated. Re-run "
                        "infra/postgres/init/10-runtime-roles.sh to reset it."
                    )
            if outermost and _DEPTH == 0:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_LOCK_KEY}
                )
        engine.dispose()


__all__ = [
    "ENV_VARS",
    "RUNTIME_ROLES",
    "ProvisioningUnavailableError",
    "ephemeral_role_passwords",
]
