"""The privilege gate is executable, and provisioning it leaves nothing behind.

WHY THIS FILE EXISTS
====================
Step 5C.4 section 13 requires the fifteen runtime-role privilege tests to actually run
before any candidate is promoted, and forbids documenting the skip again. The mechanism
that makes them runnable -- `tests.runtime_credentials` -- is itself a thing that can
break, and it breaks in a way nobody would notice: if the restore silently failed, the
roles would be left carrying a password this suite generated, and whoever holds the
real one would find out at the worst possible moment.

So the restore is tested, not assumed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, create_engine, text

from tests.runtime_credentials import (
    RUNTIME_ROLES,
    ProvisioningUnavailableError,
    ephemeral_role_passwords,
)

pytestmark = pytest.mark.integration


def _verifiers(engine: Engine) -> dict[str, str | None]:
    with engine.connect() as connection:
        return {
            row.rolname: row.rolpassword
            for row in connection.execute(
                text(
                    "SELECT rolname, rolpassword FROM pg_authid "
                    " WHERE rolname = ANY(:roles) ORDER BY rolname"
                ),
                {"roles": list(RUNTIME_ROLES)},
            )
        }


def test_the_runtime_roles_can_actually_be_authenticated_as(
    postgres_dsn: str, runtime_role_passwords: dict[str, str]
) -> None:
    """Not `SET ROLE`, not a member role, not a catalog query: a real login.

    Section 13 forbids replacing an authentication test with a catalog assertion, and
    the reason is visible here -- a role whose grants are perfect but which cannot log
    in passes every `information_schema` check ever written.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(postgres_dsn)
    for role, password in runtime_role_passwords.items():
        netloc = f"{role}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        engine = create_engine(urlunparse(parsed._replace(netloc=netloc)), future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT current_user AS who, session_user AS session")
                ).one()
                assert row.who == role
                assert row.session == role, "session_user must be the role, not an owner using it"
                assert (
                    connection.execute(text("SELECT current_setting('is_superuser')")).scalar()
                    == "off"
                )
        finally:
            engine.dispose()


def test_provisioning_restores_every_verifier_byte_for_byte(postgres_dsn: str) -> None:
    """The rotation is reversible, which is the only reason it is acceptable.

    `pg_authid.rolpassword` holds a complete SCRAM verifier, and feeding that string
    back to `ALTER ROLE ... PASSWORD` stores it unchanged. Without that property this
    mechanism would destroy a credential it cannot recreate.
    """
    engine = create_engine(postgres_dsn, future=True)
    try:
        before = _verifiers(engine)
        assert set(before) == set(RUNTIME_ROLES), "the runtime roles must exist to test this"

        with ephemeral_role_passwords(postgres_dsn) as generated:
            during = _verifiers(engine)
            assert set(generated) == set(RUNTIME_ROLES)
            assert len(set(generated.values())) == len(RUNTIME_ROLES), "one secret per role"
            for role in RUNTIME_ROLES:
                assert during[role] != before[role], f"{role} was not actually rotated"

        assert _verifiers(engine) == before, "a verifier was not restored exactly"
    finally:
        engine.dispose()


def test_a_role_that_was_not_named_is_left_completely_alone(postgres_dsn: str) -> None:
    """Rotating a credential that something else is using breaks that something else.

    This rotated all three roles unconditionally, which was defensible while none of the
    real passwords existed in any recoverable form. Step 5C.7E provisioned a real
    `app_api` password into `.env`, and the first consequence was a test run rotating it
    out from under a live `reviewer-enrol-password`: the operator pasted a valid token
    and got *"password authentication failed for user app_api"* from a suite running in
    another window.

    So the caller names only the roles that need a generated password, and an unnamed
    role is not read, not rotated and not restored -- the safest handling of a working
    credential being not to touch it.
    """
    engine = create_engine(postgres_dsn, future=True)
    try:
        before = _verifiers(engine)
        assert set(before) == set(RUNTIME_ROLES), "the runtime roles must exist to test this"

        rotated = ("app_worker",)
        untouched = tuple(role for role in RUNTIME_ROLES if role not in rotated)

        with ephemeral_role_passwords(postgres_dsn, roles=rotated) as generated:
            assert set(generated) == set(rotated), "only the named role gets a secret"
            during = _verifiers(engine)
            for role in rotated:
                assert during[role] != before[role], f"{role} should have been rotated"
            for role in untouched:
                assert during[role] == before[role], (
                    f"{role} was rotated although it was not named -- this is the defect "
                    "that broke a live credential handoff"
                )

        assert _verifiers(engine) == before, "a verifier was not restored exactly"
    finally:
        engine.dispose()


def test_naming_no_roles_rotates_nothing(postgres_dsn: str) -> None:
    """The every-password-supplied case, which must still be a usable context manager."""
    engine = create_engine(postgres_dsn, future=True)
    try:
        before = _verifiers(engine)
        with ephemeral_role_passwords(postgres_dsn, roles=()) as generated:
            assert generated == {}
            assert _verifiers(engine) == before
        assert _verifiers(engine) == before
    finally:
        engine.dispose()


def test_provisioning_refuses_a_role_it_does_not_manage(postgres_dsn: str) -> None:
    """`app_credential_definer` owns the claim function and must never be rotated here.

    It is NOLOGIN with no password by design (Step 5C.7F). Giving it one would make it an
    account, and this mechanism exists for roles that authenticate.
    """
    with (
        pytest.raises(ProvisioningUnavailableError, match="not runtime roles"),
        ephemeral_role_passwords(postgres_dsn, roles=("app_credential_definer",)),
    ):
        pass


def test_provisioning_refuses_a_non_loopback_database(postgres_dsn: str) -> None:
    """Rotating a shared credential is somebody else's outage.

    The refusal names the host, because "provisioning failed" is not something a
    reviewer can act on.
    """
    remote = postgres_dsn.replace("127.0.0.1", "db.example.test").replace(
        "localhost", "db.example.test"
    )
    with (
        pytest.raises(ProvisioningUnavailableError, match="not a loopback address"),
        ephemeral_role_passwords(remote),
    ):
        pass


def test_provisioning_is_available_so_no_privilege_test_will_skip(
    postgres_dsn: str, runtime_role_passwords: dict[str, str]
) -> None:
    """Section 13's actual requirement, asserted rather than eyeballed in a summary.

    The fifteen privilege tests skip when they cannot get a credential. Requesting the
    same fixture they request proves they will not: if it could only skip, this test
    would skip too, and a skip here is as visible as a failure.

    Deliberately not a nested `pytest` subprocess. That was tried and it deadlocks --
    the child asks for the provisioning lock this session is holding -- and a test that
    hangs is worse than no test, because a hung run is a run whose restore never
    happens.
    """
    assert sorted(runtime_role_passwords) == sorted(RUNTIME_ROLES)
    assert all(runtime_role_passwords.values()), "a role got an empty credential"
