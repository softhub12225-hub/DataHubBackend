"""Setting an operator's local password: once by them, or by an authenticated admin.

TWO DEFECTS, CLOSED IN ORDER
============================
The first version of this command was::

    UPDATE app_user SET password_hash = :h
     WHERE lower(email) = lower(:e) AND is_active

Nothing checked whether a credential already existed, so anyone who could run it could
overwrite any reviewer's password knowing only their email, authenticate as them, and
record verification decisions in their name. Step 5C.7C split the command in three and
added `WHERE password_hash IS NULL`, which stopped the overwrite.

It did not stop the **first claim**. An account with no password could still be claimed
by whoever ran the command first, and the only thing they needed to know was an email
address -- which is printed on business cards and appears in this repository's own
documentation. Step 5C.7D therefore made the first password require a one-time challenge
issued for that account and delivered out of band: see `enrollment_tokens`.

THREE OPERATIONS, NOT ONE
=========================
A first enrolment and a reset need different authority, and the command that conflated
them could take over any account. Each now states what it requires:

* **initial enrolment** -- a valid, unexpired, unused challenge for an account that has
  no credential. Unauthenticated, because somebody with no password has nothing to prove
  anything with, and safe because possession of the challenge is the proof;
* **change** -- proves the current password before accepting a new one;
* **administrative reset** -- proves an administrator, and is audited.

THE GUARDS ARE IN THE DATABASE
==============================
Enrolment's checks are all inside `app_claim_enrollment`: the challenge is spent by a
conditional `UPDATE`, and the password is written with `WHERE password_hash IS NULL`.
Two concurrent enrolments cannot both succeed, and neither can an enrolment that reaches
the write by some other route -- `app_api` holds no `UPDATE` on `app_user` at all, only
`EXECUTE` on that function.

WHAT THIS CANNOT PROTECT AGAINST, STATED PLAINLY
================================================
Anyone with the **owning** database credential can `UPDATE app_user` directly, and no
application check prevents that. That is why issuance is the owner-only half and claiming
is not: an operator holding only the application credential can spend a challenge and
cannot mint one. Direct owner access remains a trusted position, as it is for every table
in this system.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.domains.identity.accounts import (
    AccountRefusedError,
    CredentialAlreadyEnrolledError,
    read_account,
    read_account_by_id,
    require_enrollable,
)
from app.domains.identity.enrollment_tokens import claim
from app.domains.identity.passwords import hash_password, verify_password

#: The permission that authorises resetting somebody else's credential.
RESET_PERMISSION = "admin:roles"


class EnrolmentRefusedError(AccountRefusedError):
    """The credential cannot be set as asked. The message says which check failed."""


@dataclass(frozen=True, slots=True)
class EnrolmentResult:
    """Which account was changed, and how. Never carries credential material."""

    user_id: uuid.UUID
    email: str
    operation: str


def enrol_initial_password(
    connection: Connection,
    *,
    token: str,
    password: str,
) -> EnrolmentResult:
    """Set the first password on an account, using the challenge issued for it.

    Unauthenticated by necessity -- somebody with no password has nothing to authenticate
    with -- and therefore gated on the one thing they can have instead: a token that was
    issued for their account and handed to them out of band.

    Note what this function does **not** take. There is no `email`: the token names the
    account, so the caller cannot direct an enrolment at an account of their choosing,
    and there is nothing to be gained by knowing somebody's address. There is no
    `allow_test_identity` either -- whether a fixture identity may be enrolled was
    settled when the challenge was issued, and re-asking here would let a caller answer
    it differently.
    """
    # Hash before claiming. Argon2id is deliberately slow, and running it inside the
    # window where the challenge row is locked would hold that lock for no reason.
    digest = hash_password(password)
    user_id = claim(connection, token=token, password_hash=digest)

    account = read_account_by_id(connection, user_id)
    from app.domains.verification.audit import append as append_audit

    append_audit(
        connection,
        actor_id=user_id,
        action="CREDENTIAL_ENROLLED",
        object_type="app_user",
        object_id=user_id,
        reason=f"{account.email} claimed their account with an enrolment challenge",
        after={"email": account.email},
    )
    return EnrolmentResult(
        user_id=user_id,
        email=account.email,
        operation="INITIAL_ENROLMENT",
    )


def change_own_password(
    connection: Connection,
    *,
    email: str,
    current_password: str,
    new_password: str,
    allow_test_identity: bool = False,
) -> EnrolmentResult:
    """Replace a password, proving the current one first.

    Email alone is never enough. That is the whole point: the previous command accepted
    an email address as sufficient authority over an account.
    """
    row = read_account(connection, email)
    require_enrollable(row, allow_test_identity=allow_test_identity)

    stored = connection.execute(
        text("SELECT password_hash FROM app_user WHERE id = :i"), {"i": row.id}
    ).scalar()
    if not stored:
        raise EnrolmentRefusedError(f"{row.email} has no password to change. Enrol one first.")
    if not verify_password(stored, current_password).matched:
        raise EnrolmentRefusedError("the current password is not correct")
    if current_password == new_password:
        raise EnrolmentRefusedError("the new password is the same as the current one")

    connection.execute(
        text("UPDATE app_user SET password_hash = :h, updated_at = now() WHERE id = :i"),
        {"h": hash_password(new_password), "i": row.id},
    )
    return EnrolmentResult(
        user_id=row.id,
        email=row.email,
        operation="PASSWORD_CHANGED",
    )


def reset_password_as_administrator(
    connection: Connection,
    *,
    administrator_id: uuid.UUID,
    target_email: str,
    new_password: str,
    reason: str,
    allow_test_identity: bool = False,
) -> EnrolmentResult:
    """Replace somebody else's password, as an authenticated administrator.

    The caller authenticates the administrator; this checks their permission, performs
    the reset and audits it. `administrator_id` therefore comes from a verified session,
    never from an argument -- the same rule as every verification decision.

    Unusable today, and deliberately so: no identity holds `admin:roles`, and inventing
    one to make a reset path work would be inventing the authority it exists to check.
    """
    if not reason.strip():
        raise EnrolmentRefusedError("an administrative reset must record why")

    held = {
        entry.permission_code
        for entry in connection.execute(
            text(
                "SELECT rp.permission_code FROM user_role ur "
                "  JOIN role_permission rp ON rp.role_code = ur.role_code "
                " WHERE ur.user_id = :i"
            ),
            {"i": administrator_id},
        )
    }
    if RESET_PERMISSION not in held:
        raise EnrolmentRefusedError(
            f"{administrator_id} does not hold {RESET_PERMISSION!r} and may not reset "
            "another account's credential"
        )

    row = read_account(connection, target_email)
    require_enrollable(row, allow_test_identity=allow_test_identity)
    connection.execute(
        text("UPDATE app_user SET password_hash = :h, updated_at = now() WHERE id = :i"),
        {"h": hash_password(new_password), "i": row.id},
    )

    from app.domains.verification.audit import append as append_audit

    append_audit(
        connection,
        actor_id=administrator_id,
        action="CREDENTIAL_RESET",
        object_type="app_user",
        object_id=row.id,
        reason=reason,
        after={"email": row.email},
    )
    return EnrolmentResult(
        user_id=row.id,
        email=row.email,
        operation="ADMIN_RESET",
    )


__all__ = [
    "RESET_PERMISSION",
    "CredentialAlreadyEnrolledError",
    "EnrolmentRefusedError",
    "EnrolmentResult",
    "change_own_password",
    "enrol_initial_password",
    "reset_password_as_administrator",
]
