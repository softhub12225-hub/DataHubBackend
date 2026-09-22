"""Issuing and consuming the one-time challenge that lets somebody claim an account.

THE ATTACK THIS CLOSES
======================
Step 5C.7C made it impossible to overwrite an existing credential. It left the account
claimable by whoever got there first: enrolment needed an email address and the absence
of a password, and an email address is a routing label, not a secret. An operator who
knew Dejan's address could enrol before him, pick the password, and then be him.

So the first password now requires possession of a token that nobody can derive from
anything public. Provisioning says *this account exists*; the challenge says *and you
are the person it was provisioned for*. Those are two claims and they need two pieces
of evidence.

WHO MAY ISSUE ONE
=================
Not the person claiming it -- that would restore the hole exactly. Either an
authenticated administrator holding `admin:roles`, or, while no administrator exists at
all, the bootstrap authority on the owning connection. The bootstrap path reuses
`verification.identity.authorize_grant`, so it closes permanently the moment an
administrator does exist, and there is no flag to reopen it.

WHAT IS STORED
==============
The SHA-256 of the token, and nothing else. The plaintext is returned once, to the
process that issued it, for the operator to hand over out of band. It is never written
to a log, an audit row, a repr or a test artifact.

THE RACE IS IN THE `UPDATE`, AND THE `UPDATE` IS IN THE DATABASE
================================================================
`claim` does not read the token and then decide. It calls `app_claim_enrollment`, which
claims the row with a conditional `UPDATE ... WHERE used_at IS NULL AND revoked_at IS
NULL AND expires_at > now()` and sets the password in the same statement sequence. Two
concurrent enrolments serialise on that row; the second matches nothing and is refused.

It lives in SQL rather than here because the privilege boundary needs it to: it is the
only route by which `app_api` can write `app_user.password_hash`, so a caller cannot
reach the write without passing the checks. See the migration's docstring.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.db.safety import forbid_real_database
from app.domains.identity.accounts import (
    Account,
    AccountRefusedError,
    CredentialAlreadyEnrolledError,
    has_role,
    read_account,
    require_enrollable,
)

#: Bytes of entropy in a token. 32 bytes is 256 bits; `token_urlsafe` encodes it to 43
#: characters. Section 3 forbids a UUID, an email, a timestamp or a numeric code --
#: each of which is guessable, derivable, or both.
TOKEN_BYTES = 32

#: How long a challenge stays live. Short because it is a bearer credential handed over
#: out of band: an operator issues it, passes it to the reviewer, and the reviewer uses
#: it within the hour. A multi-day token is a password with a shorter name.
DEFAULT_LIFETIME = timedelta(minutes=30)

# There is deliberately no ISSUE_PERMISSION constant here. `authorize_grant` owns the
# question of who may hand out authority, and a fourth copy of the string "admin:roles"
# is a fourth place for it to drift out of step with the other three.


class EnrollmentTokenError(AccountRefusedError):
    """The challenge cannot be issued or spent as asked.

    Subclasses `AccountRefusedError` so that a caller wrapping the whole enrolment path
    catches a refusal about the account and a refusal about the token alike -- they are
    the same answer to the operator: it did not happen, and here is why.
    """


class TokenExpiredError(EnrollmentTokenError):
    """The challenge has passed its expiry. Reported distinctly because the remedy is
    a reissue rather than a correction."""


@dataclass(frozen=True, slots=True)
class IssuedChallenge:
    """A freshly issued challenge.

    `token` is the only copy of the plaintext that will ever exist. It is returned so
    the issuing operator can hand it over, and deliberately excluded from `__repr__`:
    a dataclass that prints its own secret ends up in a traceback eventually.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    token: str
    expires_at: datetime
    bootstrap_mode: bool

    def __repr__(self) -> str:
        return (
            f"IssuedChallenge(id={self.id!r}, user_id={self.user_id!r}, "
            f"email={self.email!r}, token=<redacted>, "
            f"expires_at={self.expires_at!r}, bootstrap_mode={self.bootstrap_mode!r})"
        )


#: What `secrets.token_urlsafe(TOKEN_BYTES)` produces: 32 bytes of base64url without
#: padding, which is 43 characters from the urlsafe alphabet.
TOKEN_LENGTH = 43
_TOKEN_SHAPE = re.compile(rf"\A[A-Za-z0-9_-]{{{TOKEN_LENGTH}}}\Z")


def looks_like_token(candidate: str) -> bool:
    """Is this the right shape to be an enrolment token at all?

    Not a security check -- the shape is public knowledge and the authoritative test is
    whether the hash matches a live row. It exists so that a **truncated paste** can be
    reported as "that is 19 characters, not 43" instead of the same generic refusal a
    wrong token gets. Those are different mistakes with different remedies, and a
    terminal that pastes unreliably makes the first one common.
    """
    return bool(_TOKEN_SHAPE.match(candidate.strip()))


def hash_token(token: str) -> str:
    """SHA-256 of the token. See the module docstring for why not Argon2id."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _claimable_account(connection: Connection, email: str, *, allow_test_identity: bool) -> Account:
    """The account a challenge may be issued for, or a refusal saying why not.

    Every one of these is checked at issuance rather than at claim time, because a token
    is only ever issued for an account that passed them -- which is what lets the
    database function stay short enough to be read in one sitting.
    """
    if allow_test_identity:
        # The same narrow rule as `provision_reviewer`: the fixture-only path may not
        # run against the real pilot database. Issuing a challenge for a [TEST ONLY]
        # identity there is precisely what left nine audit rows in Step 5C.7E, and the
        # audit chain is append-only, so there is no undo.
        forbid_real_database(connection, context=f"issue(allow_test_identity, {email})")
    account = read_account(connection, email)
    require_enrollable(account, allow_test_identity=allow_test_identity)
    if account.has_credential:
        raise CredentialAlreadyEnrolledError(
            f"CREDENTIAL_ALREADY_ENROLLED: {account.email} already has a credential. "
            "Enrolment is permanently closed for this account; use a password change or "
            "an administrative reset."
        )
    if not has_role(connection, account.id):
        raise EnrollmentTokenError(
            f"{account.email} holds no role. Provision the account before issuing an "
            "enrolment challenge for it."
        )
    return account


def issue(
    connection: Connection,
    *,
    email: str,
    issued_by: uuid.UUID | None,
    lifetime: timedelta = DEFAULT_LIFETIME,
    allow_test_identity: bool = False,
    rotate: bool = False,
    reason: str = "",
) -> IssuedChallenge:
    """Create a challenge for a provisioned, credential-less account.

    `issued_by` is an administrator's id, or None for the bootstrap case -- which is
    permitted only while no administrator exists, checked by `authorize_grant`. The
    caller resolves the administrator from an authenticated session; this never takes an
    actor on trust from an argument that was not authenticated.

    `rotate` revokes an outstanding challenge first. Without it the partial unique index
    refuses a second live row, so reissue cannot silently leave two valid tokens.
    """
    from app.domains.verification.audit import append, append_system
    from app.domains.verification.identity import authorize_grant

    # Authority first: bootstrap only while nobody could have authorised it.
    administrator = authorize_grant(connection, issued_by)
    account = _claimable_account(connection, email, allow_test_identity=allow_test_identity)

    live = connection.execute(
        text(
            "SELECT id FROM credential_enrollment "
            " WHERE user_id = :i AND used_at IS NULL AND revoked_at IS NULL FOR UPDATE"
        ),
        {"i": account.id},
    ).one_or_none()
    if live is not None:
        if not rotate:
            raise EnrollmentTokenError(
                f"{account.email} already has an outstanding enrolment challenge. Reissue "
                "explicitly if it must be replaced; the previous one is then revoked."
            )
        # Revoking first is not politeness -- `ux_credential_enrollment_live` refuses a
        # second live row, so a reissue that skipped this would fail on the INSERT.
        connection.execute(
            text(
                "UPDATE credential_enrollment SET revoked_at = now(), "
                "       revoked_reason = :why WHERE id = :i"
            ),
            {"i": live.id, "why": reason or "superseded by a reissued challenge"},
        )

    token = secrets.token_urlsafe(TOKEN_BYTES)
    challenge_id = uuid.uuid4()
    expires_at = connection.execute(
        text(
            "INSERT INTO credential_enrollment (id, user_id, token_hash, expires_at, "
            "                                   issued_by, bootstrap_mode) "
            "VALUES (:i, :u, :h, now() + :life, :by, :boot) RETURNING expires_at"
        ),
        {
            "i": challenge_id,
            "u": account.id,
            "h": hash_token(token),
            "life": lifetime,
            "by": administrator,
            "boot": administrator is None,
        },
    ).scalar_one()

    # Section 15: record what actually happened. A bootstrap issuance had no human
    # approver, so it is filed as SYSTEM rather than attributed to one. Neither row
    # carries the token or its hash.
    if administrator is None:
        append_system(
            connection,
            action="CREDENTIAL_ENROLLMENT_ISSUED",
            object_type="app_user",
            object_id=account.id,
            reason=reason or "bootstrap issuance: no authenticated administrator exists",
            after={"bootstrap_mode": True, "expires_at": str(expires_at)},
        )
    else:
        append(
            connection,
            actor_id=administrator,
            action="CREDENTIAL_ENROLLMENT_ISSUED",
            object_type="app_user",
            object_id=account.id,
            reason=reason or f"issued an enrolment challenge for {account.email}",
            after={"bootstrap_mode": False, "expires_at": str(expires_at)},
        )

    return IssuedChallenge(
        id=challenge_id,
        user_id=account.id,
        email=account.email,
        token=token,
        expires_at=expires_at,
        bootstrap_mode=administrator is None,
    )


def revoke(connection: Connection, *, email: str, reason: str) -> int:
    """Revoke any outstanding challenge for an account. Returns how many were revoked.

    Takes no actor: revocation only ever destroys a capability, so a caller who can
    reach it can at worst force a reissue. The reason is required because a revoked
    challenge with no explanation is indistinguishable from an expired one later.
    """
    if not reason.strip():
        raise EnrollmentTokenError("a revocation must record why")
    return (
        connection.execute(
            text(
                "UPDATE credential_enrollment SET revoked_at = now(), revoked_reason = :why "
                " WHERE used_at IS NULL AND revoked_at IS NULL AND user_id = ("
                "       SELECT id FROM app_user WHERE lower(email) = lower(:e))"
            ),
            {"why": reason, "e": email.strip()},
        ).rowcount
        or 0
    )


#: What the database raises, and what the caller should be told. The SQLSTATE is the
#: same for all of them (28000, invalid authorization) so that a caller which only
#: catches broadly still refuses; the message distinguishes the two remedies.
_DB_FAILURES = {
    "ENROLLMENT_TOKEN_EXPIRED": (
        TokenExpiredError,
        "ENROLLMENT_TOKEN_EXPIRED: this challenge has expired. Ask for a new one; no "
        "password was changed.",
    ),
    "ENROLLMENT_TOKEN_INVALID": (
        EnrollmentTokenError,
        "the enrolment token is not valid. It may be wrong, already used, or revoked. "
        "No password was changed.",
    ),
    "ENROLLMENT_ALREADY_CLAIMED": (
        CredentialAlreadyEnrolledError,
        "CREDENTIAL_ALREADY_ENROLLED: this account was claimed while the token was "
        "outstanding. The first enrolment stands and the token was not spent.",
    ),
    "ENROLLMENT_ACCOUNT_DISABLED": (
        AccountRefusedError,
        "the account this token belongs to has been deactivated. The token was not "
        "spent; it will work again if the account is re-enabled before it expires.",
    ),
    "ENROLLMENT_PASSWORD_NOT_HASHED": (
        EnrollmentTokenError,
        "internal error: a password reached the database unhashed and was refused",
    ),
}


def claim(connection: Connection, *, token: str, password_hash: str) -> uuid.UUID:
    """Spend a challenge and set the account's first password. Returns the account.

    Both writes happen inside `app_claim_enrollment`, so the token is spent if and only
    if the password was set: a failure at either step rolls back both and the reviewer
    can try again with the same token.

    Distinguishes an expired token from an invalid one, because the remedies differ, and
    says nothing about which account a bad token belonged to -- that would make this an
    oracle for enumerating them.

    Runs inside a savepoint. A refused claim is an ordinary outcome -- a mistyped token
    is the common case -- and a raised `RAISE EXCEPTION` would otherwise poison the whole
    transaction, forcing every caller to abandon work that had nothing to do with it.
    The savepoint keeps the refusal local: nothing this function attempted survives it,
    and everything around it does.
    """
    if not token.strip():
        raise EnrollmentTokenError("no enrolment token supplied")

    savepoint = connection.begin_nested()
    try:
        claimed = connection.execute(
            text("SELECT app_claim_enrollment(:h, :p)"),
            {"h": hash_token(token), "p": password_hash},
        ).scalar_one()
    except DBAPIError as error:
        savepoint.rollback()
        detail = str(getattr(error.orig, "args", ("",))[0] if error.orig else error)
        for marker, (kind, message) in _DB_FAILURES.items():
            if marker in detail:
                raise kind(message) from error
        raise
    savepoint.commit()
    return uuid.UUID(str(claimed))


__all__ = [
    "DEFAULT_LIFETIME",
    "TOKEN_BYTES",
    "TOKEN_LENGTH",
    "EnrollmentTokenError",
    "IssuedChallenge",
    "TokenExpiredError",
    "claim",
    "hash_token",
    "issue",
    "looks_like_token",
    "revoke",
]
