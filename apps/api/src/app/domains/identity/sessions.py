"""Browser sessions for the reviewer console. The BFF path `authentication.py` refused.

WHY THIS EXISTS NOW AND NOT BEFORE
==================================
`verification/authentication.py` deliberately does not touch `user_session`::

    "It is not a session service, issues no token, and deliberately does not touch
     `user_session` -- those rows are for the Next.js BFF and giving the CLI a
     persistent token would create a second, weaker way in."

Step 5C.7L/M builds that BFF. So this is the intended path finally being implemented, not
a second authentication system: it reuses `app_user.password_hash`, the same Argon2
verification, the same role/permission resolution, and the same refusals in the same
order. What it adds is the one thing a browser needs and a CLI does not -- a credential
that survives between requests.

WHAT THE BROWSER GETS, AND WHAT IT DOES NOT
===========================================
The browser receives a **random 256-bit token**, in an HttpOnly cookie. The database
stores only its SHA-256. A reader of `user_session` therefore cannot mint a cookie, which
is the same reasoning the enrolment tokens use: high-entropy random secrets get SHA-256,
passwords get Argon2. Argon2 here would buy nothing (there is no low-entropy guess to slow
down) and would cost a KDF on every single request.

The browser never receives, and never sends: an actor UUID used as authority, any database
password, or the session signing secret. The actor is resolved from the session row on the
server, every request.

REVOCATION IS A DATABASE FACT
=============================
`revoked_at` is checked on every resolve, and so is `app_user.is_active`. A deactivated
reviewer stops being able to write on their very next request rather than whenever their
cookie happens to expire -- the difference between those two is the whole reason the
session is a row and not a self-contained signed blob.

PERMISSION IS RE-READ, NOT REMEMBERED
=====================================
`resolve` recomputes the held permissions on each request instead of storing them at login.
A role revoked at 10:00 must not keep working until 18:00 because that is when the session
was issued.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.core.logging import get_logger
from app.domains.identity.passwords import verify_password
from app.domains.verification.identity import TEST_MARKER, VERIFY_PERMISSION, permissions_of

logger = get_logger(__name__)

#: Bytes of entropy in a session token. 32 bytes is 256 bits: not guessable, and short
#: enough to sit in a cookie without comment.
TOKEN_BYTES = 32

#: How the CSRF token relates to the session. Double-submit: the same value is placed in a
#: readable cookie and must be echoed in a header on every mutating request. An attacker
#: on another origin can cause the browser to *send* the session cookie but cannot read it
#: to copy the value into the header, which is the property the whole scheme rests on.
CSRF_HEADER = "X-DataHub-CSRF"


class SessionRefusedError(RuntimeError):
    """The credential did not establish a usable session.

    One error for "no such account", "wrong password" and "no local credential", exactly
    as `authentication.AuthenticationFailedError` does and for the same reason: a caller
    that can tell them apart is an account-enumeration oracle.
    """


class SessionExpiredError(RuntimeError):
    """There was a session and it is no longer usable. Log in again."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What login produces. `token` is the only copy -- it is never stored or logged."""

    session_id: uuid.UUID
    token: str
    csrf_token: str
    expires_at: datetime
    user_id: uuid.UUID
    email: str
    display_name: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    """Who this request is, resolved from the cookie on the server. Never from a body."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    expires_at: datetime
    is_test: bool

    @property
    def may_verify(self) -> bool:
        return VERIFY_PERMISSION in self.permissions

    def require(self, permission: str) -> None:
        """Refuse unless this session holds the permission. Raises, never returns False."""
        if permission not in self.permissions:
            raise PermissionError(
                f"{self.email} holds {sorted(self.roles) or 'no roles'}, none of which "
                f"carries {permission!r}"
            )


def hash_token(token: str) -> str:
    """SHA-256 of a session token. The database stores this and never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _try_upgrade_hash(connection: Connection, *, user_id: uuid.UUID, replacement: str) -> bool:
    """Opportunistically restore a password hash to current Argon2 parameters.

    WHY THIS IS BEST-EFFORT AND NOT REQUIRED
    ========================================
    A successful login is the only moment the plaintext exists to rehash with. But the
    application role holds **SELECT only** on `app_user` -- deliberately: the API can read
    an identity and must not be able to rewrite one. So the UPDATE is expected to fail
    here, and failing the *login* because a housekeeping write was refused would deny a
    reviewer with a correct password for a reason that has nothing to do with them.

    Run in a savepoint, because in PostgreSQL a failed statement aborts the surrounding
    transaction and would take the session INSERT down with it. Returns whether the
    upgrade happened, so a caller running as a more privileged role can tell.
    """
    try:
        with connection.begin_nested():
            connection.execute(
                text("UPDATE app_user SET password_hash = :h, updated_at = now() WHERE id = :i"),
                {"h": replacement, "i": user_id},
            )
        return True
    except DBAPIError:
        logger.info(
            "password_hash_upgrade_skipped",
            user_id=str(user_id),
            reason="the application role may not write app_user",
        )
        return False


def _identity(
    connection: Connection, user_id: uuid.UUID
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    roles = tuple(
        row.role_code
        for row in connection.execute(
            text("SELECT role_code FROM user_role WHERE user_id = :i ORDER BY role_code"),
            {"i": user_id},
        )
    )
    return roles, tuple(sorted(permissions_of(connection, user_id)))


def login(
    connection: Connection,
    *,
    email: str,
    password: str,
    ttl_minutes: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> IssuedSession:
    """Verify a password and open a session. The password is never logged or returned.

    Refusals in the same order as the CLI path: unknown/credential-less account, wrong
    password, deactivated account, missing `source:verify`. The first two are reported
    identically on purpose.
    """
    moment = now or datetime.now(UTC)
    row = connection.execute(
        text(
            "SELECT id, email, display_name, password_hash, is_active "
            "  FROM app_user WHERE lower(email) = lower(:email)"
        ),
        {"email": email.strip()},
    ).one_or_none()
    if row is None:
        raise SessionRefusedError("no account with that email, or no password set")

    result = verify_password(row.password_hash, password)
    if not result.matched:
        raise SessionRefusedError("no account with that email, or no password set")
    if not row.is_active:
        raise SessionRefusedError(f"{row.email} is deactivated")

    if result.replacement_hash is not None:
        _try_upgrade_hash(connection, user_id=row.id, replacement=result.replacement_hash)

    roles, permissions = _identity(connection, row.id)
    if VERIFY_PERMISSION not in permissions:
        raise SessionRefusedError(
            f"{row.email} authenticated but holds {sorted(roles) or 'no roles'}, and "
            f"none of them carries {VERIFY_PERMISSION!r}"
        )

    token = secrets.token_urlsafe(TOKEN_BYTES)
    csrf = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = moment + timedelta(minutes=ttl_minutes)
    session_id = uuid.uuid4()
    connection.execute(
        text(
            """
            INSERT INTO user_session (id, user_id, token_hash, issued_at, expires_at,
                                      ip_address, user_agent)
            VALUES (:id, :user, :hash, :issued, :expires, CAST(:ip AS inet), :agent)
            """
        ),
        {
            "id": session_id,
            "user": row.id,
            "hash": hash_token(token),
            "issued": moment,
            "expires": expires_at,
            "ip": ip_address,
            "agent": (user_agent or "")[:512] or None,
        },
    )
    logger.info(
        "reviewer_session_opened",
        session_id=str(session_id),
        user_id=str(row.id),
        expires_at=expires_at.isoformat(),
    )
    return IssuedSession(
        session_id=session_id,
        token=token,
        csrf_token=csrf,
        expires_at=expires_at,
        user_id=uuid.UUID(str(row.id)),
        email=str(row.email),
        display_name=str(row.display_name),
        roles=roles,
        permissions=permissions,
    )


def resolve(connection: Connection, *, token: str, now: datetime | None = None) -> ResolvedSession:
    """Turn a cookie into an identity, re-reading every fact that could have changed.

    Refuses an unknown, revoked or expired session, and a session whose user has since
    been deactivated. Permissions are recomputed rather than remembered.
    """
    moment = now or datetime.now(UTC)
    if not token or not token.strip():
        raise SessionExpiredError("no session")
    row = connection.execute(
        text(
            """
            SELECT s.id, s.user_id, s.expires_at, s.revoked_at,
                   u.email, u.display_name, u.is_active
              FROM user_session s
              JOIN app_user u ON u.id = s.user_id
             WHERE s.token_hash = :hash
            """
        ),
        {"hash": hash_token(token)},
    ).one_or_none()
    if row is None:
        raise SessionExpiredError("no session")
    if row.revoked_at is not None:
        raise SessionExpiredError("this session was logged out")
    if row.expires_at <= moment:
        raise SessionExpiredError("this session has expired")
    if not row.is_active:
        # The reason `user_session` is a row rather than a signed blob.
        raise SessionExpiredError(f"{row.email} is deactivated")

    roles, permissions = _identity(connection, row.user_id)
    return ResolvedSession(
        session_id=uuid.UUID(str(row.id)),
        user_id=uuid.UUID(str(row.user_id)),
        email=str(row.email),
        display_name=str(row.display_name),
        roles=roles,
        permissions=permissions,
        expires_at=row.expires_at,
        is_test=str(row.display_name).startswith(TEST_MARKER),
    )


def logout(connection: Connection, *, token: str, now: datetime | None = None) -> bool:
    """Revoke a session. Idempotent; returns whether this call was the one that did it."""
    moment = now or datetime.now(UTC)
    result = connection.execute(
        text(
            "UPDATE user_session SET revoked_at = :now "
            " WHERE token_hash = :hash AND revoked_at IS NULL"
        ),
        {"hash": hash_token(token), "now": moment},
    )
    return bool(result.rowcount)


def revoke_all(connection: Connection, *, user_id: uuid.UUID, now: datetime | None = None) -> int:
    """Revoke every live session for one identity. Returns how many were closed."""
    moment = now or datetime.now(UTC)
    result = connection.execute(
        text(
            "UPDATE user_session SET revoked_at = :now "
            " WHERE user_id = :user AND revoked_at IS NULL AND expires_at > :now"
        ),
        {"user": user_id, "now": moment},
    )
    return int(result.rowcount or 0)


__all__ = [
    "CSRF_HEADER",
    "TOKEN_BYTES",
    "IssuedSession",
    "ResolvedSession",
    "SessionExpiredError",
    "SessionRefusedError",
    "hash_token",
    "login",
    "logout",
    "resolve",
    "revoke_all",
]
