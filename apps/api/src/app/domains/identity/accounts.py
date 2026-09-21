"""What an account is, and when it may hold a credential.

Both halves of enrolment ask the same questions -- issuing a challenge and spending one
must agree about which accounts are claimable, or the gate has a gap between them. They
ask here, so there is one answer rather than two that drift.

This module reads `app_user` and decides nothing about credentials themselves; the
password work is in `passwords`, the challenge lifecycle in `enrollment_tokens`, and the
operations in `enrollment`. Keeping it underneath all three is what stops them importing
each other in a circle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

#: Display-name prefix marking a fixture identity. Defined here rather than imported
#: from `verification.identity`, because identity is the layer underneath verification
#: and must not depend on it.
TEST_MARKER = "[TEST ONLY]"


class AccountRefusedError(RuntimeError):
    """This account may not do what was asked of it. The message says which check failed."""


class CredentialAlreadyEnrolledError(AccountRefusedError):
    """This account already has a password, so no enrolment path may touch it.

    Separate from the general refusal because it is the security-relevant one: it is what
    stops an email address, or a leaked challenge, being usable to take over an account
    that somebody has already claimed.
    """


@dataclass(frozen=True, slots=True)
class Account:
    """The account row, typed, so the checks read as what they are.

    Carries `has_credential` rather than the hash itself: almost every caller needs to
    know whether a password exists, and none of them needs the value.
    """

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    has_credential: bool

    @property
    def is_test(self) -> bool:
        return self.display_name.startswith(TEST_MARKER)


def read_account(connection: Connection, email: str) -> Account:
    """Find an account by email, locking the row.

    The lock is not optional, because every caller is about to decide something from what
    it just read and two of them deciding concurrently is the failure mode this whole
    area exists to prevent. Use `read_account_by_id` when the answer is only for display.
    """
    row = connection.execute(
        text(
            "SELECT id, email, display_name, is_active, "
            "       (password_hash IS NOT NULL) AS has_credential "
            "  FROM app_user WHERE lower(email) = lower(:email) FOR UPDATE"
        ),
        {"email": email.strip()},
    ).one_or_none()
    if row is None:
        raise AccountRefusedError(f"no account for {email}")
    return Account(
        id=row.id,
        email=str(row.email),
        display_name=str(row.display_name),
        is_active=bool(row.is_active),
        has_credential=bool(row.has_credential),
    )


def read_account_by_id(connection: Connection, user_id: uuid.UUID) -> Account:
    """Read an account back by id, without locking.

    Used after an operation has already identified the account -- for naming it in an
    audit entry or a result. Nothing decides anything from this, so there is no lock.
    """
    row = connection.execute(
        text(
            "SELECT id, email, display_name, is_active, "
            "       (password_hash IS NOT NULL) AS has_credential "
            "  FROM app_user WHERE id = :i"
        ),
        {"i": user_id},
    ).one_or_none()
    if row is None:
        raise AccountRefusedError(f"no app_user {user_id}")
    return Account(
        id=row.id,
        email=str(row.email),
        display_name=str(row.display_name),
        is_active=bool(row.is_active),
        has_credential=bool(row.has_credential),
    )


def has_role(connection: Connection, user_id: uuid.UUID) -> bool:
    """Does this account hold any role at all?"""
    return bool(
        connection.execute(
            text("SELECT 1 FROM user_role WHERE user_id = :i LIMIT 1"), {"i": user_id}
        ).one_or_none()
    )


def require_enrollable(account: Account, *, allow_test_identity: bool = False) -> None:
    """The conditions an account must meet before it may hold a credential at all.

    Deliberately not including "has no password yet": a change and a reset are also
    credential operations and both need these same checks on an account that does.
    """
    if not account.is_active:
        raise AccountRefusedError(
            f"{account.email} is deactivated. A disabled account may not enrol, "
            "authenticate or change a credential."
        )
    if account.is_test and not allow_test_identity:
        raise AccountRefusedError(
            f"{account.email} is a {TEST_MARKER} identity and may not be enrolled "
            "through the operator command. Fixtures enrol it directly."
        )


__all__ = [
    "TEST_MARKER",
    "Account",
    "AccountRefusedError",
    "CredentialAlreadyEnrolledError",
    "has_role",
    "read_account",
    "read_account_by_id",
    "require_enrollable",
]
