"""Credential enrolment: claiming an account, changing it, resetting it.

TWO VULNERABILITIES, CLOSED IN ORDER
====================================
**5C.7C -- overwrite.** The command shipped in 5C.7B was::

    UPDATE app_user SET password_hash = :h
     WHERE lower(email) = lower(:e) AND is_active

Nothing checked whether a credential already existed, so anyone able to run the CLI could
overwrite any reviewer's password knowing only their email address, authenticate as them,
and record verification decisions in their name.
`test_email_alone_cannot_replace_an_existing_credential` is the one that would have
caught it.

**5C.7D -- first claim.** Adding `WHERE password_hash IS NULL` stopped the overwrite and
left the *first* claim wide open: an account with no password could be claimed by whoever
ran the command first, and all they needed to know was an email address. An operator
could have enrolled before Dejan and then been him.
`test_a_password_cannot_be_set_without_a_token` and
`test_an_operator_who_knows_the_email_still_cannot_claim_the_account` are the pair that
would have caught that one.

WHAT MAKES THE SECOND FIX REAL
==============================
Not the Python. The attacker here runs our own code, so a check they could skip by
calling a different function is not a check. What closes it is the grant matrix:
`app_api` holds no INSERT on `credential_enrollment` and no UPDATE on `app_user`, so it
can spend a challenge and cannot mint one. Section 4 tests that boundary as `app_api`
over a real connection, not by reading the catalogue -- the interesting failures are the
ones where the catalogue looks correct.
"""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from app.domains.identity.accounts import (
    AccountRefusedError,
    CredentialAlreadyEnrolledError,
)
from app.domains.identity.enrollment import (
    EnrolmentRefusedError,
    change_own_password,
    enrol_initial_password,
    reset_password_as_administrator,
)
from app.domains.identity.enrollment_tokens import (
    DEFAULT_LIFETIME,
    TOKEN_BYTES,
    TOKEN_LENGTH,
    EnrollmentTokenError,
    IssuedChallenge,
    TokenExpiredError,
    claim,
    hash_token,
    issue,
    looks_like_token,
    revoke,
)
from app.domains.identity.passwords import hash_password, verify_password
from app.domains.verification.audit import SYSTEM_ACTIONS, append_system
from app.domains.verification.authentication import (
    ActorMismatchError,
    AuthenticationFailedError,
    authenticate,
)
from app.domains.verification.identity import (
    BootstrapClosedError,
    NotAuthorizedError,
    provision_reviewer,
)

pytestmark = pytest.mark.integration

#: The dedicated, non-superuser owner of `app_claim_enrollment` (Step 5C.7F).
DEFINER_ROLE = "app_credential_definer"

FIRST = "the-first-fixture-password"
SECOND = "a-second-fixture-password"


def _account(
    conn: Connection,
    *,
    role: str = "reviewer",
    test_only: bool = True,
    active: bool = True,
    with_role: bool = True,
    granted_by: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """An account with no credential, as a freshly provisioned reviewer has."""
    unique = uuid.uuid4().hex[:8]
    email = f"enrol-{unique}@example.test"
    if with_role:
        created = provision_reviewer(
            conn,
            email=email,
            display_name=f"Enrolment fixture {unique}",
            role=role,
            test_only=test_only,
            granted_by=granted_by,
        )
        user_id = created.id
    else:
        user_id = uuid.uuid4()
        conn.execute(
            text("INSERT INTO app_user (id, email, display_name) VALUES (:i, :e, :n)"),
            {"i": user_id, "e": email, "n": f"[TEST ONLY] Roleless {unique}"},
        )
    if not active:
        conn.execute(
            text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
            {"i": user_id},
        )
    return user_id, email


def _challenge(conn: Connection, email: str, **kwargs: Any) -> IssuedChallenge:
    """Issue a challenge for a fixture account, under the bootstrap authority."""
    kwargs.setdefault("issued_by", None)
    kwargs.setdefault("allow_test_identity", True)
    return issue(conn, email=email, **kwargs)


def _enrol(conn: Connection, password: str = FIRST, **kwargs: Any) -> tuple[uuid.UUID, str]:
    """A fixture account taken through the whole real path: provision, issue, claim."""
    user_id, email = _account(conn, **kwargs)
    challenge = _challenge(conn, email)
    enrol_initial_password(conn, token=challenge.token, password=password)
    return user_id, email


def _stored_hash(conn: Connection, user_id: uuid.UUID) -> str | None:
    return conn.execute(
        text("SELECT password_hash FROM app_user WHERE id = :i"), {"i": user_id}
    ).scalar()


def _rows(conn: Connection, user_id: uuid.UUID) -> list[Any]:
    return list(
        conn.execute(
            text("SELECT * FROM credential_enrollment WHERE user_id = :i ORDER BY issued_at"),
            {"i": user_id},
        )
    )


# ===========================================================================
# 1. The first-claim attack itself
# ===========================================================================


def test_a_password_cannot_be_set_without_a_token() -> None:
    """The defect, stated as an API shape: there is no email+password enrolment.

    Deliberately a signature test rather than a behavioural one. The attack was possible
    because a function existed that took an address and a password and set a credential;
    the fix is that no such function exists, and a signature test is what notices if one
    comes back.
    """
    parameters = inspect.signature(enrol_initial_password).parameters
    assert "token" in parameters, "enrolment must require the challenge"
    assert "email" not in parameters, (
        "enrolment must not take an email address: the token names the account, and an "
        "email parameter is how an operator points an enrolment at somebody else"
    )
    assert "allow_test_identity" not in parameters, (
        "whether a fixture identity may enrol is settled at issuance; re-asking here "
        "would let the claimant answer it differently"
    )


def test_an_operator_who_knows_the_email_still_cannot_claim_the_account(
    conn: Connection,
) -> None:
    """Knowing the address confers nothing. That is the whole of 5C.7D."""
    user_id, email = _account(conn)

    # Everything an attacker has: the address, its parts, the account id, and a guess.
    for guess in (email, email.split("@")[0], str(user_id), uuid.uuid4().hex):
        with pytest.raises(EnrollmentTokenError):
            enrol_initial_password(conn, token=guess, password="attacker-chosen-pw")

    assert _stored_hash(conn, user_id) is None, "the account was claimed by a guess"


def test_a_token_issued_for_one_account_cannot_claim_another(conn: Connection) -> None:
    """The token names the account, so a leaked one is not a skeleton key."""
    victim, _ = _account(conn)
    _, other_email = _account(conn)
    challenge = _challenge(conn, other_email)
    assert challenge.user_id != victim

    enrol_initial_password(conn, token=challenge.token, password=FIRST)

    assert _stored_hash(conn, victim) is None
    assert _stored_hash(conn, challenge.user_id) is not None


# ===========================================================================
# 2. What is stored, and what never is
# ===========================================================================


def test_only_a_hash_of_the_token_is_stored(conn: Connection) -> None:
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    (row,) = _rows(conn, challenge.user_id)
    assert row.token_hash == hash_token(challenge.token)
    assert row.token_hash != challenge.token

    stored = {str(value) for value in row._mapping.values()}
    assert challenge.token not in stored, "the plaintext token reached the database"


def test_the_token_carries_at_least_256_bits_and_is_not_derivable(
    conn: Connection,
) -> None:
    """Section 3: not a UUID, not the email, not a timestamp, not a counter."""
    assert TOKEN_BYTES * 8 >= 256

    _, email = _account(conn)
    tokens = set()
    for _ in range(5):
        challenge = _challenge(conn, email, rotate=True)
        tokens.add(challenge.token)
        # 32 random bytes encode to 43 urlsafe-base64 characters.
        assert len(challenge.token) >= 43
        assert email.split("@")[0] not in challenge.token
        with pytest.raises(ValueError):
            uuid.UUID(challenge.token)
    assert len(tokens) == 5, "tokens repeated; they are not random"


def test_the_token_is_absent_from_the_audit_trail(conn: Connection) -> None:
    """Section 8. An audit row records that a challenge was issued, never its value."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    entries = conn.execute(
        text(
            "SELECT action, reason, after_state::text AS after FROM audit_log "
            " WHERE object_id = :i ORDER BY seq"
        ),
        {"i": challenge.user_id},
    ).all()
    assert entries, "issuance was not recorded at all"
    for entry in entries:
        blob = f"{entry.action} {entry.reason} {entry.after}"
        assert challenge.token not in blob
        assert hash_token(challenge.token) not in blob, (
            "even the hash is absent: it is the verifier, and an audit log is read by "
            "more people than the credential store is"
        )


def test_a_truncated_paste_is_reported_as_a_shape_problem(conn: Connection) -> None:
    """A partial paste and a wrong token are different mistakes.

    The first real handoff failed because `cmd.exe` does not paste on Ctrl+V and the
    prompt echoed nothing, so there was no way to tell an empty entry from a good one.
    Reporting the shape separately means "you pasted half of it" does not arrive
    disguised as "that token is not valid" -- the remedies differ.

    Not a security check: the shape is public, and the authority is still whether the
    hash matches a live row.
    """
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    assert looks_like_token(challenge.token)
    assert len(challenge.token) == TOKEN_LENGTH

    for bad in (
        "",
        "   ",
        challenge.token[:20],
        challenge.token[:-1],
        challenge.token + "x",
        challenge.token.replace(challenge.token[0], "!", 1),
    ):
        assert not looks_like_token(bad), f"{bad!r} should not look like a token"

    # Surrounding whitespace is what a paste actually adds, and must be tolerated.
    assert looks_like_token("  " + challenge.token + "  ")
    assert looks_like_token(challenge.token + chr(10))
    assert looks_like_token(challenge.token + chr(13) + chr(10))


def test_the_token_is_absent_from_repr_and_debug_output(conn: Connection) -> None:
    """Section 8. A dataclass that prints its own secret ends up in a traceback."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    assert challenge.token not in repr(challenge)
    assert "<redacted>" in repr(challenge)
    assert challenge.email in repr(challenge), "the useful fields are still there"


# ===========================================================================
# 3. One-time use, expiry, revocation, reissue
# ===========================================================================


def test_a_token_works_once(conn: Connection) -> None:
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    result = enrol_initial_password(conn, token=challenge.token, password=FIRST)
    assert result.operation == "INITIAL_ENROLMENT"
    assert verify_password(_stored_hash(conn, challenge.user_id) or "", FIRST).matched

    with pytest.raises(EnrollmentTokenError):
        enrol_initial_password(conn, token=challenge.token, password=SECOND)
    assert verify_password(
        _stored_hash(conn, challenge.user_id) or "", FIRST
    ).matched, "the second attempt changed the password"


def test_a_spent_token_is_marked_used_not_deleted(conn: Connection) -> None:
    """The row survives so "this account was claimed, and when" stays answerable."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    enrol_initial_password(conn, token=challenge.token, password=FIRST)

    (row,) = _rows(conn, challenge.user_id)
    assert row.used_at is not None
    assert row.revoked_at is None


def test_an_expired_token_is_refused_and_says_so(conn: Connection) -> None:
    """Section 11. Expiry gets its own error because the remedy is a reissue."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    conn.execute(
        text(
            "UPDATE credential_enrollment SET issued_at = now() - interval '2 days', "
            "       expires_at = now() - interval '1 minute' WHERE id = :i"
        ),
        {"i": challenge.id},
    )

    with pytest.raises(TokenExpiredError, match="ENROLLMENT_TOKEN_EXPIRED"):
        enrol_initial_password(conn, token=challenge.token, password=FIRST)
    assert _stored_hash(conn, challenge.user_id) is None


def test_the_default_lifetime_is_operator_scale_not_a_bearer_credential(
    conn: Connection,
) -> None:
    """Section 11: minutes, not days. A long-lived token is a password with a nicer name."""
    assert timedelta(minutes=15) <= DEFAULT_LIFETIME <= timedelta(minutes=60)

    _, email = _account(conn)
    challenge = _challenge(conn, email)
    (row,) = _rows(conn, challenge.user_id)
    assert row.expires_at - row.issued_at == DEFAULT_LIFETIME


def test_a_revoked_token_stops_working(conn: Connection) -> None:
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    assert revoke(conn, email=email, reason="delivered to the wrong address") == 1
    with pytest.raises(EnrollmentTokenError):
        enrol_initial_password(conn, token=challenge.token, password=FIRST)
    assert _stored_hash(conn, challenge.user_id) is None


def test_a_revocation_must_record_why(conn: Connection) -> None:
    _, email = _account(conn)
    _challenge(conn, email)
    with pytest.raises(EnrollmentTokenError, match="record why"):
        revoke(conn, email=email, reason="   ")


def test_reissuing_invalidates_the_previous_token(conn: Connection) -> None:
    """Section 12. Two live tokens would double the surface for no benefit."""
    _, email = _account(conn)
    first = _challenge(conn, email)
    second = _challenge(conn, email, rotate=True, reason="the first was lost")

    assert first.token != second.token
    with pytest.raises(EnrollmentTokenError):
        enrol_initial_password(conn, token=first.token, password=FIRST)

    enrol_initial_password(conn, token=second.token, password=SECOND)
    assert verify_password(_stored_hash(conn, second.user_id) or "", SECOND).matched


def test_a_second_live_challenge_is_refused_rather_than_issued_quietly(
    conn: Connection,
) -> None:
    """Reissue is explicit. An operator who did not mean to revoke gets told."""
    _, email = _account(conn)
    _challenge(conn, email)
    with pytest.raises(EnrollmentTokenError, match="already has an outstanding"):
        _challenge(conn, email)


def test_the_database_itself_permits_only_one_live_challenge(conn: Connection) -> None:
    """The partial unique index, not the Python check. Sections 10 and 12.

    If this were enforced only in `issue`, any other writer -- a fixture, a repair
    script, a future endpoint -- could leave two live tokens on one account.
    """
    user_id, email = _account(conn)
    _challenge(conn, email)

    savepoint = conn.begin_nested()
    try:
        with pytest.raises(Exception, match="ux_credential_enrollment_live"):
            conn.execute(
                text(
                    "INSERT INTO credential_enrollment "
                    "  (user_id, token_hash, expires_at, bootstrap_mode) "
                    "VALUES (:u, :h, now() + interval '30 minutes', true)"
                ),
                {"u": user_id, "h": "f" * 64},
            )
    finally:
        savepoint.rollback()


# ===========================================================================
# 4. Who may issue: the privilege boundary that actually closes the attack
# ===========================================================================


def test_the_application_role_cannot_mint_a_challenge(conn: Connection) -> None:
    """Sections 4 and 5: the reviewer's own CLI must not issue its own token."""
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not conn.execute(
            text("SELECT has_table_privilege('app_api', 'credential_enrollment', :p)"),
            {"p": privilege},
        ).scalar(), f"app_api holds {privilege} on credential_enrollment"

    assert conn.execute(
        text("SELECT has_table_privilege('app_api', 'credential_enrollment', 'SELECT')")
    ).scalar(), "app_api must still be able to see that a challenge exists"


def test_the_application_role_cannot_write_a_password_except_by_claiming(
    conn: Connection,
) -> None:
    """Why claiming is a function and not a grant.

    `GRANT UPDATE (password_hash) ON app_user TO app_api` would have separated the two
    commands just as well, and let a compromised API process set anybody's password.
    """
    assert not conn.execute(
        text("SELECT has_table_privilege('app_api', 'app_user', 'UPDATE')")
    ).scalar()
    assert not conn.execute(
        text("SELECT has_column_privilege('app_api', 'app_user', 'password_hash', 'UPDATE')")
    ).scalar()
    assert conn.execute(
        text(
            "SELECT has_function_privilege("
            "  'app_api', 'app_claim_enrollment(text, text)', 'EXECUTE')"
        )
    ).scalar(), "app_api must be able to claim; only issuing is the owner's"


def test_no_runtime_role_may_issue(conn: Connection) -> None:
    for role in ("app_api", "app_worker", "app_publisher"):
        assert not conn.execute(
            text("SELECT has_table_privilege(:r, 'credential_enrollment', 'INSERT')"),
            {"r": role},
        ).scalar(), f"{role} can mint enrolment tokens"


def test_only_the_application_role_may_claim(conn: Connection) -> None:
    """The worker and the publisher have no business setting anybody's password."""
    for role in ("app_worker", "app_publisher"):
        assert not conn.execute(
            text(
                "SELECT has_function_privilege("
                "  :r, 'app_claim_enrollment(text, text)', 'EXECUTE')"
            ),
            {"r": role},
        ).scalar(), f"{role} can claim an account"


def test_the_application_role_really_cannot_issue_over_a_live_connection(
    owner_engine: Engine, role_engines: dict[str, Engine]
) -> None:
    """The same claim, exercised rather than read out of the catalogue.

    `runtime_credentials` exists so these tests authenticate as the real runtime roles
    over the real path. A catalogue assertion proves a grant was written; this proves the
    server enforces it, which is the failure worth catching.
    """
    unique = uuid.uuid4().hex[:8]
    with owner_engine.begin() as setup:
        created = provision_reviewer(
            setup,
            email=f"enrol-priv-{unique}@example.test",
            display_name=f"Privilege fixture {unique}",
            test_only=True,
        )
    try:
        with role_engines["app_api"].connect() as api:
            with pytest.raises(Exception, match="permission denied"):
                api.execute(
                    text(
                        "INSERT INTO credential_enrollment (user_id, token_hash, expires_at) "
                        "VALUES (:u, :h, now() + interval '30 minutes')"
                    ),
                    {"u": created.id, "h": "b" * 64},
                )
            api.rollback()
            with pytest.raises(Exception, match="permission denied"):
                api.execute(
                    text("UPDATE app_user SET password_hash = :h WHERE id = :i"),
                    {"h": hash_password(FIRST), "i": created.id},
                )
            api.rollback()
    finally:
        with owner_engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM app_user WHERE id = :i"), {"i": created.id})


def test_the_application_role_can_claim_a_challenge_it_could_not_issue(
    owner_engine: Engine, role_engines: dict[str, Engine]
) -> None:
    """The other half: claiming does not need the owner's credential, and issuing does.

    If enrolment also required the owner connection the separation would be decorative --
    whoever could run one command could run the other.
    """
    unique = uuid.uuid4().hex[:8]
    email = f"enrol-claim-{unique}@example.test"
    with owner_engine.begin() as setup:
        created = provision_reviewer(
            setup, email=email, display_name=f"Claim fixture {unique}", test_only=True
        )
        challenge = issue(setup, email=email, issued_by=None, allow_test_identity=True)
    try:
        with role_engines["app_api"].begin() as api:
            result = enrol_initial_password(api, token=challenge.token, password=FIRST)
            assert result.user_id == created.id

        with owner_engine.connect() as check:
            stored = check.execute(
                text("SELECT password_hash FROM app_user WHERE id = :i"),
                {"i": created.id},
            ).scalar_one()
            assert verify_password(stored, FIRST).matched
    finally:
        with owner_engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM app_user WHERE id = :i"), {"i": created.id})


def test_bootstrap_issuance_closes_once_an_administrator_exists(
    conn: Connection,
) -> None:
    """Sections 6 and 15. The exception is not a permanent bypass.

    Reuses `authorize_grant`, so there is one bootstrap rule rather than two that drift.
    """
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    assert challenge.bootstrap_mode is True

    # An administrator now exists, so somebody *could* have authorised it.
    admin, _ = _account(conn, role="admin")

    _, later = _account(conn, granted_by=admin)
    with pytest.raises(BootstrapClosedError, match="bootstrap path closed"):
        issue(conn, email=later, issued_by=None, allow_test_identity=True)

    # ...and the authenticated administrator can, which is the point of closing it.
    authorised = issue(conn, email=later, issued_by=admin, allow_test_identity=True)
    assert authorised.bootstrap_mode is False


def test_a_reviewer_cannot_issue_a_challenge_for_anybody(conn: Connection) -> None:
    """Section 13: `admin:roles` is required, and a reviewer does not hold it."""
    reviewer, _ = _enrol(conn)
    _, target = _account(conn)

    with pytest.raises(NotAuthorizedError, match="admin:roles"):
        issue(conn, email=target, issued_by=reviewer, allow_test_identity=True)


def test_a_reviewer_cannot_reissue_their_own_challenge_before_authenticating(
    conn: Connection,
) -> None:
    """Section 13, stated as the attack would be run: self-service reissue.

    An unauthenticated account holds no permission, so it cannot authorise anything --
    least of all a fresh challenge for itself, which would make the token pointless.
    """
    _, email = _account(conn)
    first = _challenge(conn, email)

    with pytest.raises(NotAuthorizedError, match="admin:roles"):
        issue(
            conn,
            email=email,
            issued_by=first.user_id,
            rotate=True,
            allow_test_identity=True,
        )

    (row,) = _rows(conn, first.user_id)
    assert row.revoked_at is None, "a refused reissue disturbed the original challenge"


def test_a_challenge_is_refused_for_an_account_that_already_has_a_password(
    conn: Connection,
) -> None:
    """Enrolment is permanently closed once claimed; a reset is a different operation."""
    _, email = _enrol(conn)
    with pytest.raises(CredentialAlreadyEnrolledError, match="CREDENTIAL_ALREADY_ENROLLED"):
        _challenge(conn, email)


def test_a_challenge_is_refused_for_a_roleless_or_disabled_account(
    conn: Connection,
) -> None:
    _, roleless = _account(conn, with_role=False)
    with pytest.raises(EnrollmentTokenError, match="holds no role"):
        _challenge(conn, roleless)

    _, disabled = _account(conn, active=False)
    with pytest.raises(AccountRefusedError, match="deactivated"):
        _challenge(conn, disabled)


def test_a_challenge_is_refused_for_an_unknown_account(conn: Connection) -> None:
    with pytest.raises(AccountRefusedError, match="no account for"):
        _challenge(conn, "nobody-at-all@example.test")


def test_a_fixture_identity_has_no_operator_issuance_path(conn: Connection) -> None:
    """A fixture identity that looked real is how a fixture decision reaches a real trail."""
    _, email = _account(conn)
    with pytest.raises(AccountRefusedError, match="TEST ONLY"):
        issue(conn, email=email, issued_by=None)


# ===========================================================================
# 4b. The SECURITY DEFINER function itself
# ===========================================================================


def test_the_claim_function_is_owned_by_a_dedicated_non_superuser(
    conn: Connection,
) -> None:
    """A definer function runs its body as its owner, so the owner IS the blast radius.

    Step 5C.7D made the owner the schema owner, which on this cluster is a superuser. The
    body was static and tested, so nothing was exploitable -- but "not exploitable" is a
    property of today's code, and the owner is a property of the object. A future defect
    would have had the whole cluster to work with.

    The owner is now `app_credential_definer`: a role that can update two columns in two
    tables and do nothing else at all.
    """
    row = conn.execute(
        text(
            "SELECT pg_get_userbyid(p.proowner) AS owner, p.prosecdef, n.nspname AS schema "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE p.proname = 'app_claim_enrollment'"
        )
    ).one()
    assert row.prosecdef is True, "the function must be SECURITY DEFINER to work at all"
    assert row.schema == "public"
    assert row.owner == DEFINER_ROLE

    attributes = conn.execute(
        text(
            "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolinherit, "
            "       rolbypassrls, (rolpassword IS NULL) AS no_password "
            "  FROM pg_authid WHERE rolname = :r"
        ),
        {"r": DEFINER_ROLE},
    ).one()
    assert not attributes.rolsuper, "the definer must not be a superuser -- the whole point"
    assert not attributes.rolcanlogin, "nobody may authenticate as the definer"
    assert not attributes.rolcreatedb and not attributes.rolcreaterole
    assert not attributes.rolinherit and not attributes.rolbypassrls
    assert attributes.no_password, "a definer role with a password is an account"

    owner_of_app_user = conn.execute(
        text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'app_user'")
    ).scalar_one()
    assert row.owner != owner_of_app_user, "the schema owner is a superuser on this cluster"


def test_the_definer_holds_only_the_columns_the_function_writes(
    conn: Connection,
) -> None:
    """Section 13: exactly what the body needs, read from the body, nothing more.

    Whole-table UPDATE on `app_user` would let a defect in the function change an email,
    a display name or `is_active`. Enrolment has no business with any of those.
    """
    held: dict[tuple[str, str], set[str]] = {}
    for row in conn.execute(
        text(
            "SELECT table_name, privilege_type, column_name "
            "  FROM information_schema.column_privileges WHERE grantee = :r"
        ),
        {"r": DEFINER_ROLE},
    ):
        held.setdefault((row.table_name, row.privilege_type), set()).add(row.column_name)

    assert held.get(("app_user", "UPDATE")) == {"password_hash", "updated_at"}
    assert held.get(("credential_enrollment", "UPDATE")) == {"used_at"}
    assert held.get(("app_user", "SELECT")) == {"id", "password_hash", "is_active"}
    assert held.get(("credential_enrollment", "SELECT")) == {
        "id",
        "user_id",
        "token_hash",
        "used_at",
        "revoked_at",
        "expires_at",
    }

    # No table-level grant anywhere: one would silently cover every column, including
    # columns added by a later migration.
    table_level = [
        (row.table_name, row.privilege_type)
        for row in conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                " WHERE grantee = :r"
            ),
            {"r": DEFINER_ROLE},
        )
    ]
    assert table_level == [], f"the definer holds table-level privileges: {table_level}"


def test_the_definer_cannot_mint_a_challenge_or_delete_anything(
    conn: Connection,
) -> None:
    """Section 21. Claiming never creates a challenge, so the definer never may.

    If it could, the function that consumes tokens could also manufacture them, and the
    separation between issuing and claiming would exist only in Python.
    """
    for privilege in ("INSERT", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        for table in ("credential_enrollment", "app_user"):
            assert not conn.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": DEFINER_ROLE, "t": table, "p": privilege},
            ).scalar(), f"the definer holds {privilege} on {table}"

    for table in ("university", "tuition", "source", "source_mapping", "official_domain"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not conn.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": DEFINER_ROLE, "t": table, "p": privilege},
            ).scalar(), f"the definer holds {privilege} on {table}"


def test_no_runtime_identity_is_a_member_of_the_definer(conn: Connection) -> None:
    """Section 18. Membership would hand these privileges to something that can log in."""
    members = {
        row.member
        for row in conn.execute(
            text(
                "SELECT m.rolname AS member FROM pg_auth_members am "
                "  JOIN pg_roles m ON m.oid = am.member "
                "  JOIN pg_roles r ON r.oid = am.roleid WHERE r.rolname = :r"
            ),
            {"r": DEFINER_ROLE},
        )
    }
    assert members == set(), f"the definer has members: {members}"

    # pg_has_role covers inherited and SET ROLE paths, not only direct membership.
    for role in ("app_api", "app_worker", "app_publisher"):
        assert not conn.execute(
            text("SELECT pg_has_role(:r, :d, 'USAGE')"), {"r": role, "d": DEFINER_ROLE}
        ).scalar(), f"{role} can use the definer role"
        assert not conn.execute(
            text("SELECT pg_has_role(:r, :d, 'MEMBER')"), {"r": role, "d": DEFINER_ROLE}
        ).scalar(), f"{role} can SET ROLE to the definer"


def test_the_claim_function_pins_its_search_path(conn: Connection) -> None:
    """Without a pinned `search_path` the caller chooses which tables it touches.

    `app_api` can execute this function and cannot write `app_user`. If the function
    resolved its table names through a caller-controlled `search_path`, the caller could
    create `my_schema.app_user`, prepend `my_schema`, and have the definer's privileges
    write to a table of their choosing. `"$user"` is caller-controlled for the same
    reason, and `pg_temp` must come last so a temporary table cannot shadow a real one.
    """
    config = conn.execute(
        text("SELECT proconfig FROM pg_proc WHERE proname = 'app_claim_enrollment'")
    ).scalar_one()
    pinned = [entry for entry in (config or []) if entry.startswith("search_path=")]
    assert pinned, "a SECURITY DEFINER function with no pinned search_path is hijackable"

    entries = [part.strip() for part in pinned[0].split("=", 1)[1].split(",")]
    assert "public" in entries
    assert not any(
        part.strip('"') == "$user" for part in entries
    ), 'search_path contains "$user", which the caller controls'
    assert entries[-1] == "pg_temp", "pg_temp must be last or it shadows real tables"


def test_the_claim_function_builds_no_dynamic_sql(conn: Connection) -> None:
    """The reason the pinned search_path is sufficient.

    Its owner is a superuser, so every statement in the body runs with superuser rights.
    That is safe only while the body is static: `EXECUTE format(...)` over anything a
    caller supplied would be object-name injection at superuser level. This test exists
    so that introducing one is a test failure rather than a quiet escalation.
    """
    body = conn.execute(
        text("SELECT prosrc FROM pg_proc WHERE proname = 'app_claim_enrollment'")
    ).scalar_one()
    for construct in ("EXECUTE format(", "EXECUTE '", 'EXECUTE "', "quote_ident("):
        assert construct not in body, f"dynamic SQL in a definer function: {construct}"


def test_only_the_application_role_and_the_owner_may_execute_the_claim_function(
    conn: Connection,
) -> None:
    """PUBLIC must not execute it, and no role beyond the two intended ones.

    `EXECUTE` here is the whole of `app_api`'s write authority over credentials. Any
    extra grantee is another identity that can set a first password.
    """
    assert not conn.execute(
        text(
            "SELECT has_function_privilege('public', 'app_claim_enrollment(text,text)', 'EXECUTE')"
        )
    ).scalar()
    granted = {
        row.rolname
        for row in conn.execute(
            text(
                "SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg_%' "
                "  AND has_function_privilege("
                "        rolname, 'app_claim_enrollment(text,text)', 'EXECUTE')"
            )
        )
    }
    # A superuser passes `has_function_privilege` for everything, so its presence is not
    # a grant. The assertion that matters is that nothing else appears.
    superusers = {
        row.rolname for row in conn.execute(text("SELECT rolname FROM pg_roles WHERE rolsuper"))
    }
    expected = {"app_api", DEFINER_ROLE} | superusers
    assert granted <= expected, f"unexpected grantees: {granted - expected}"
    assert "app_api" in granted, "the application role must still be able to claim"
    assert "app_worker" not in granted and "app_publisher" not in granted


def test_the_full_grant_matrix_over_real_authenticated_connections(
    owner_engine: Engine, role_engines: dict[str, Engine]
) -> None:
    """Section 24. Every runtime role, every forbidden write, actually attempted.

    `has_table_privilege` proves a grant was written. This proves the server enforces it,
    as each real role, over an authenticated connection -- which is the failure mode that
    matters, because the catalogue looks correct in exactly the cases that bite.

    The definer is absent from this loop on purpose: it is `NOLOGIN`, so there is no
    connection to attempt anything over. That is its proof, not a gap in this one.
    """
    unique = uuid.uuid4().hex[:8]
    with owner_engine.begin() as setup:
        target = provision_reviewer(
            setup,
            email=f"matrix-{unique}@example.test",
            display_name=f"Matrix fixture {unique}",
            test_only=True,
        )
        challenge = issue(setup, email=target.email, issued_by=None, allow_test_identity=True)

    digest = hash_password("a-long-enough-matrix-password")
    token_hash = "e" * 64

    # (label, statement, parameters, the roles allowed to succeed -- empty means none)
    attempts: tuple[tuple[str, str, dict[str, object], frozenset[str]], ...] = (
        (
            "UPDATE app_user password_hash",
            "UPDATE app_user SET password_hash = :h WHERE id = :u",
            {"h": digest, "u": target.id},
            frozenset(),
        ),
        (
            "UPDATE credential_enrollment",
            "UPDATE credential_enrollment SET used_at = now() WHERE user_id = :u",
            {"u": target.id},
            frozenset(),
        ),
        (
            "INSERT credential_enrollment",
            "INSERT INTO credential_enrollment "
            "  (user_id, token_hash, expires_at, bootstrap_mode) "
            "VALUES (:u, :h, now() + interval '30 minutes', true)",
            {"u": target.id, "h": token_hash},
            frozenset(),
        ),
        (
            "DELETE credential_enrollment",
            "DELETE FROM credential_enrollment WHERE user_id = :u",
            {"u": target.id},
            frozenset(),
        ),
    )

    try:
        for role, engine in sorted(role_engines.items()):
            with engine.connect() as connection:
                assert connection.execute(text("SELECT session_user")).scalar_one() == role
                for label, sql, params, allowed in attempts:
                    savepoint = connection.begin_nested()
                    try:
                        connection.execute(text(sql), params)
                        savepoint.rollback()
                        assert role in allowed, f"{role} was ALLOWED to {label}"
                    except Exception as error:
                        savepoint.rollback()
                        if role in allowed:
                            raise
                        message = str(getattr(error, "orig", error)).splitlines()[0]
                        assert (
                            "permission denied" in message
                        ), f"{role} was refused {label} for the wrong reason: {message}"
                connection.rollback()

        # EXECUTE: only app_api, and it must genuinely work rather than merely be granted.
        for role, engine in sorted(role_engines.items()):
            with engine.connect() as connection:
                savepoint = connection.begin_nested()
                try:
                    connection.execute(
                        text("SELECT app_claim_enrollment(:h, :p)"),
                        {"h": hash_token(challenge.token), "p": digest},
                    )
                    savepoint.rollback()
                    assert role == "app_api", f"{role} could EXECUTE the claim function"
                except Exception as error:
                    savepoint.rollback()
                    message = str(getattr(error, "orig", error)).splitlines()[0]
                    assert (
                        role != "app_api"
                    ), f"app_api could not claim through the function: {message}"
                    assert (
                        "permission denied" in message
                    ), f"{role} was refused EXECUTE for the wrong reason: {message}"
                connection.rollback()
    finally:
        with owner_engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM app_user WHERE id = :i"), {"i": target.id})


# ===========================================================================
# 5. The race is in the write predicate (section 10)
# ===========================================================================


def test_the_claim_is_a_conditional_update_not_a_read_then_write(
    conn: Connection,
) -> None:
    """The checks live in `app_claim_enrollment`, where no caller can route around them."""
    body = conn.execute(
        text("SELECT prosrc FROM pg_proc WHERE proname = 'app_claim_enrollment'")
    ).scalar_one()
    assert "UPDATE credential_enrollment SET used_at = now()" in body
    assert "used_at IS NULL AND revoked_at IS NULL" in body
    assert "expires_at > now()" in body
    assert "password_hash IS NULL AND is_active" in body


def test_two_concurrent_enrolments_cannot_both_succeed(
    owner_engine: Engine, postgres_dsn: str
) -> None:
    """Section 10, over two real connections. A `SELECT` then `UPDATE` would pass both.

    Needs committed data, so this manages its own fixture account and removes it
    afterwards. The audit rows it writes stay: `audit_log` is append-only by design and
    carries no foreign key to `app_user`, precisely so that removing an account cannot
    erase what it did.
    """
    unique = uuid.uuid4().hex[:8]
    email = f"enrol-race-{unique}@example.test"
    with owner_engine.begin() as setup:
        created = provision_reviewer(
            setup, email=email, display_name=f"Race fixture {unique}", test_only=True
        )
        challenge = issue(setup, email=email, issued_by=None, allow_test_identity=True)

    # Hash outside the contended window: Argon2id is deliberately slow and would
    # otherwise dominate the timing this test depends on.
    first_hash, second_hash = hash_password(FIRST), hash_password(SECOND)
    outcome: dict[str, str] = {}

    def second_enrolment() -> None:
        rival = create_engine(postgres_dsn, future=True)
        try:
            with rival.begin() as other:
                claim(other, token=challenge.token, password_hash=second_hash)
            outcome["result"] = "succeeded"
        except Exception as error:
            outcome["result"] = type(error).__name__
        finally:
            rival.dispose()

    runner = threading.Thread(target=second_enrolment)
    try:
        with owner_engine.begin() as winner:
            claim(winner, token=challenge.token, password_hash=first_hash)
            runner.start()
            # Let the rival reach its UPDATE and block on the row this one holds.
            time.sleep(0.5)
        runner.join(timeout=30)
        assert not runner.is_alive(), "the second enrolment never resolved"
        assert outcome["result"] in {
            "EnrollmentTokenError",
            "CredentialAlreadyEnrolledError",
        }, f"both enrolments succeeded: {outcome}"

        with owner_engine.connect() as check:
            stored = check.execute(
                text("SELECT password_hash FROM app_user WHERE id = :i"),
                {"i": created.id},
            ).scalar_one()
            assert verify_password(stored, FIRST).matched, "the loser overwrote the winner"
            assert not verify_password(stored, SECOND).matched
    finally:
        with owner_engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM app_user WHERE id = :i"), {"i": created.id})


def test_the_token_is_not_spent_when_the_password_cannot_be_set(
    conn: Connection,
) -> None:
    """Both writes are one unit, so a failed enrolment costs the reviewer nothing."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    savepoint = conn.begin_nested()
    try:
        with pytest.raises(EnrollmentTokenError):
            claim(conn, token=challenge.token, password_hash="not-an-argon2-hash")
    finally:
        savepoint.rollback()

    # The token survived the refusal and still works.
    enrol_initial_password(conn, token=challenge.token, password=FIRST)
    assert verify_password(_stored_hash(conn, challenge.user_id) or "", FIRST).matched


def test_a_plaintext_password_is_refused_by_the_database(conn: Connection) -> None:
    """Defence against a caller that forgets to hash. The function checks; not only we do."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    with pytest.raises(EnrollmentTokenError, match="unhashed"):
        claim(conn, token=challenge.token, password_hash=FIRST)


# ===========================================================================
# 6. What the audit trail says, and what it must never claim
# ===========================================================================


def test_bootstrap_issuance_is_recorded_as_system_and_claims_no_approver(
    conn: Connection,
) -> None:
    """Section 15. It must not say a human approved an identity when none did."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)

    entry = conn.execute(
        text(
            "SELECT actor_type::text AS actor_type, actor_id, after_state FROM audit_log "
            " WHERE object_id = :i AND action = :a ORDER BY seq DESC LIMIT 1"
        ),
        {"i": challenge.user_id, "a": "CREDENTIAL_ENROLLMENT_ISSUED"},
    ).one()
    assert entry.actor_type == "SYSTEM"
    assert entry.actor_id is None, "a bootstrap issuance had no human approver"
    assert entry.after_state["bootstrap_mode"] is True

    (row,) = _rows(conn, challenge.user_id)
    assert row.bootstrap_mode is True
    assert row.issued_by is None


def test_an_authorised_issuance_names_the_administrator(conn: Connection) -> None:
    admin, _ = _account(conn, role="admin")
    _, target = _account(conn, granted_by=admin)
    challenge = issue(conn, email=target, issued_by=admin, allow_test_identity=True)

    entry = conn.execute(
        text(
            "SELECT actor_type::text AS actor_type, actor_id FROM audit_log "
            " WHERE object_id = :i AND action = 'CREDENTIAL_ENROLLMENT_ISSUED' "
            " ORDER BY seq DESC LIMIT 1"
        ),
        {"i": challenge.user_id},
    ).one()
    assert entry.actor_type == "USER"
    assert entry.actor_id == admin

    (row,) = _rows(conn, challenge.user_id)
    assert row.issued_by == admin
    assert row.bootstrap_mode is False


def test_the_database_refuses_an_issuer_that_disagrees_with_the_mode(
    conn: Connection,
) -> None:
    """`bootstrap_mode` and `issued_by` must agree, or the two are indistinguishable later."""
    user_id, _ = _account(conn)
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(Exception, match="issuer_matches_mode"):
            conn.execute(
                text(
                    "INSERT INTO credential_enrollment "
                    "  (user_id, token_hash, expires_at, bootstrap_mode, issued_by) "
                    "VALUES (:u, :h, now() + interval '30 minutes', true, :u)"
                ),
                {"u": user_id, "h": "a" * 64},
            )
    finally:
        savepoint.rollback()


def test_claiming_an_account_is_audited_against_the_person_who_claimed_it(
    conn: Connection,
) -> None:
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    enrol_initial_password(conn, token=challenge.token, password=FIRST)

    entry = conn.execute(
        text(
            "SELECT actor_type::text AS actor_type, actor_id FROM audit_log "
            " WHERE object_id = :i AND action = 'CREDENTIAL_ENROLLED'"
        ),
        {"i": challenge.user_id},
    ).one()
    assert entry.actor_type == "USER"
    assert entry.actor_id == challenge.user_id


def test_the_system_appender_refuses_anything_that_is_a_judgement(
    conn: Connection,
) -> None:
    """Why a SYSTEM path is safe to have at all.

    A general one would make "a machine verified it" a one-word change. Adding an action
    to the allow-list is an edit to `audit.py`, which is reviewable; passing a different
    string at a call site is not.
    """
    assert {"CREDENTIAL_ENROLLMENT_ISSUED"} == SYSTEM_ACTIONS
    for forbidden in (
        "SOURCE_VERIFIED",
        "SOURCE_PROMOTED",
        "ROLE_GRANTED",
        "CREDENTIAL_RESET",
        "PROPOSAL_PUBLISHED",
    ):
        with pytest.raises(ValueError, match="may not be recorded without a human actor"):
            append_system(
                conn,
                action=forbidden,
                object_type="app_user",
                object_id=uuid.uuid4(),
                reason="attempting to file a decision under nobody",
            )


# ===========================================================================
# 7. 5C.7C's guarantees, unchanged
# ===========================================================================


def test_email_alone_cannot_replace_an_existing_credential(conn: Connection) -> None:
    """The 5C.7C regression test. It still passes, and now it has company."""
    user_id, email = _enrol(conn)
    original = _stored_hash(conn, user_id)

    with pytest.raises(CredentialAlreadyEnrolledError):
        _challenge(conn, email)
    with pytest.raises(EnrolmentRefusedError, match="current password is not correct"):
        change_own_password(
            conn,
            email=email,
            current_password="a-guess-at-the-password",
            new_password="attacker-chosen-password",
            allow_test_identity=True,
        )

    assert _stored_hash(conn, user_id) == original
    assert authenticate(conn, email=email, password=FIRST).id == user_id
    with pytest.raises(AuthenticationFailedError):
        authenticate(conn, email=email, password="attacker-chosen-password")


def test_the_current_password_permits_a_change(conn: Connection) -> None:
    user_id, email = _enrol(conn)
    result = change_own_password(
        conn,
        email=email,
        current_password=FIRST,
        new_password=SECOND,
        allow_test_identity=True,
    )
    assert result.operation == "PASSWORD_CHANGED"
    assert verify_password(_stored_hash(conn, user_id) or "", SECOND).matched


def test_the_old_password_stops_working_after_a_change(conn: Connection) -> None:
    _, email = _enrol(conn)
    change_own_password(
        conn,
        email=email,
        current_password=FIRST,
        new_password=SECOND,
        allow_test_identity=True,
    )
    assert authenticate(conn, email=email, password=SECOND).email == email
    with pytest.raises(AuthenticationFailedError):
        authenticate(conn, email=email, password=FIRST)


def test_a_wrong_current_password_cannot_change_anything(conn: Connection) -> None:
    user_id, email = _enrol(conn)
    original = _stored_hash(conn, user_id)
    with pytest.raises(EnrolmentRefusedError, match="not correct"):
        change_own_password(
            conn,
            email=email,
            current_password="not-the-password",
            new_password=SECOND,
            allow_test_identity=True,
        )
    assert _stored_hash(conn, user_id) == original


def test_changing_to_the_same_password_is_refused(conn: Connection) -> None:
    _, email = _enrol(conn)
    with pytest.raises(EnrolmentRefusedError, match="same as the current"):
        change_own_password(
            conn,
            email=email,
            current_password=FIRST,
            new_password=FIRST,
            allow_test_identity=True,
        )


def test_changing_a_password_that_was_never_enrolled_is_refused(
    conn: Connection,
) -> None:
    _, email = _account(conn)
    with pytest.raises(EnrolmentRefusedError, match="no password to change"):
        change_own_password(
            conn,
            email=email,
            current_password=FIRST,
            new_password=SECOND,
            allow_test_identity=True,
        )


def test_an_administrator_may_reset_and_it_is_audited(conn: Connection) -> None:
    admin, _ = _account(conn, role="admin")
    target, target_email = _account(conn, granted_by=admin)
    # The bootstrap path closed when the administrator was created, so the challenge is
    # issued by them. That is section 6 working, not an inconvenience.
    challenge = _challenge(conn, target_email, issued_by=admin)
    enrol_initial_password(conn, token=challenge.token, password=FIRST)

    result = reset_password_as_administrator(
        conn,
        administrator_id=admin,
        target_email=target_email,
        new_password=SECOND,
        reason="the reviewer lost access to their password manager",
        allow_test_identity=True,
    )
    assert result.operation == "ADMIN_RESET"
    assert authenticate(conn, email=target_email, password=SECOND).id == target

    row = conn.execute(
        text(
            "SELECT actor_id, object_id, reason FROM audit_log "
            " WHERE action = 'CREDENTIAL_RESET' ORDER BY seq DESC LIMIT 1"
        )
    ).one()
    assert row.actor_id == admin
    assert row.object_id == target
    assert "password manager" in row.reason


def test_a_reviewer_cannot_reset_somebody_elses_credential(conn: Connection) -> None:
    """Holding `source:verify` is not holding `admin:roles`."""
    reviewer, _ = _account(conn)
    target, target_email = _enrol(conn)
    original = _stored_hash(conn, target)

    with pytest.raises(EnrolmentRefusedError, match="admin:roles"):
        reset_password_as_administrator(
            conn,
            administrator_id=reviewer,
            target_email=target_email,
            new_password=SECOND,
            reason="trying to reset without the permission",
            allow_test_identity=True,
        )
    assert _stored_hash(conn, target) == original


def test_an_administrative_reset_must_record_why(conn: Connection) -> None:
    admin, _ = _account(conn, role="admin")
    _, target_email = _account(conn, granted_by=admin)
    with pytest.raises(EnrolmentRefusedError, match="must record why"):
        reset_password_as_administrator(
            conn,
            administrator_id=admin,
            target_email=target_email,
            new_password=SECOND,
            reason="   ",
            allow_test_identity=True,
        )


def test_a_disabled_account_can_neither_claim_nor_change_nor_authenticate(
    conn: Connection,
) -> None:
    """Section 4. All three doors, not just the one that happens to be tested."""
    user_id, email = _enrol(conn)
    conn.execute(
        text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
        {"i": user_id},
    )

    with pytest.raises(AuthenticationFailedError, match="deactivated"):
        authenticate(conn, email=email, password=FIRST)
    with pytest.raises(AccountRefusedError, match="deactivated"):
        change_own_password(
            conn,
            email=email,
            current_password=FIRST,
            new_password=SECOND,
            allow_test_identity=True,
        )

    fresh_id, fresh_email = _account(conn, active=False)
    with pytest.raises(AccountRefusedError, match="deactivated"):
        _challenge(conn, fresh_email)
    assert _stored_hash(conn, fresh_id) is None


def test_a_deactivated_account_cannot_be_claimed_even_with_a_valid_token(
    conn: Connection,
) -> None:
    """The account check is inside the function, so it is re-evaluated at claim time.

    An account can be disabled between issuance and use, and the token must not still
    work: revoking access has to mean revoking access.
    """
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    conn.execute(
        text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
        {"i": challenge.user_id},
    )
    with pytest.raises(AccountRefusedError, match="deactivated"):
        enrol_initial_password(conn, token=challenge.token, password=FIRST)
    assert _stored_hash(conn, challenge.user_id) is None


# ===========================================================================
# 8. The hash itself
# ===========================================================================


def test_each_hash_uses_a_fresh_random_salt() -> None:
    """The same password twice must not produce the same hash."""
    first, second = hash_password(FIRST), hash_password(FIRST)
    assert first != second, "identical hashes mean a missing or constant salt"
    assert verify_password(first, FIRST).matched
    assert verify_password(second, FIRST).matched


def test_the_hash_records_argon2id_parameters() -> None:
    """`m=`, `t=` and `p=` are in the encoded hash, so a rehash can detect a change."""
    digest = hash_password(FIRST)
    fields = digest.split("$")
    assert fields[1] == "argon2id"
    parameters = fields[3]
    for key in ("m=", "t=", "p="):
        assert key in parameters, f"{key} missing from {parameters}"


def test_no_operation_returns_or_stores_plaintext(conn: Connection) -> None:
    """Section 8.10. Nothing that comes back carries the secret."""
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    result = enrol_initial_password(conn, token=challenge.token, password=FIRST)

    assert FIRST not in repr(result)
    assert challenge.token not in repr(result)
    stored = _stored_hash(conn, challenge.user_id) or ""
    assert FIRST not in stored

    session = authenticate(conn, email=email, password=FIRST)
    assert FIRST not in repr(session)
    assert FIRST not in repr(session.reviewer)


def test_unknown_user_and_wrong_password_remain_indistinguishable(
    conn: Connection,
) -> None:
    """Section 6, restated here because enrolment changed the surrounding code."""
    _, email = _enrol(conn)

    with pytest.raises(AuthenticationFailedError) as unknown:
        authenticate(conn, email="absent@example.test", password=FIRST)
    with pytest.raises(AuthenticationFailedError) as wrong:
        authenticate(conn, email=email, password=SECOND)
    assert str(unknown.value) == str(wrong.value)


def test_a_wrong_token_and_a_spent_token_remain_indistinguishable(
    conn: Connection,
) -> None:
    """The same rule one layer down: a refusal must not be an oracle.

    If "already used" and "never existed" read differently, the message tells an attacker
    whether a token they hold was ever real, and which account it named.
    """
    _, email = _account(conn)
    challenge = _challenge(conn, email)
    enrol_initial_password(conn, token=challenge.token, password=FIRST)

    with pytest.raises(EnrollmentTokenError) as spent:
        enrol_initial_password(conn, token=challenge.token, password=SECOND)
    with pytest.raises(EnrollmentTokenError) as nonsense:
        enrol_initial_password(conn, token="not-a-token-at-all", password=SECOND)
    assert str(spent.value) == str(nonsense.value)
    assert email not in str(spent.value)


# ===========================================================================
# 9. Actor binding still holds end to end
# ===========================================================================


def test_the_claimed_credential_binds_decisions_to_that_identity(
    conn: Connection,
) -> None:
    """A credential exists so a decision can name whoever made it. Still true."""
    user_id, email = _enrol(conn)
    session = authenticate(conn, email=email, password=FIRST)

    assert session.id == user_id
    assert session.bind(None) == user_id
    assert session.bind(user_id) == user_id

    other, _ = _account(conn)
    with pytest.raises(ActorMismatchError):
        session.bind(other)


def test_the_whole_path_from_provisioning_to_a_bound_decision(
    conn: Connection,
) -> None:
    """One test that walks every step, because each was added to serve the next.

    Provision (no credential) -> issue (operator) -> claim (reviewer) -> authenticate ->
    bind. If any link stops holding, a verification decision stops naming the person who
    made it, which is the only reason any of this exists.
    """
    user_id, email = _account(conn)
    assert _stored_hash(conn, user_id) is None, "provisioning must not set a credential"

    challenge = _challenge(conn, email)
    assert challenge.user_id == user_id
    assert _stored_hash(conn, user_id) is None, "issuing must not set a credential either"

    enrol_initial_password(conn, token=challenge.token, password=FIRST)
    reviewer = authenticate(conn, email=email, password=FIRST)
    assert reviewer.bind(user_id) == user_id

    actions = [
        row.action
        for row in conn.execute(
            text("SELECT action FROM audit_log WHERE object_id = :i ORDER BY seq"),
            {"i": user_id},
        )
    ]
    assert "CREDENTIAL_ENROLLMENT_ISSUED" in actions
    assert "CREDENTIAL_ENROLLED" in actions
    assert actions.index("CREDENTIAL_ENROLLMENT_ISSUED") < actions.index("CREDENTIAL_ENROLLED")
