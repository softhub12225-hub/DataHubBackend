"""Authenticating an operator at a terminal, and binding decisions to who authenticated.

THE CORRECTION THIS EXISTS FOR
==============================
Until now a write command took `--actor <uuid>`. That establishes *attribution* -- the
row says Dejan decided it -- and nothing else. Anyone with CLI access could type
somebody else's UUID, and the audit log would name a person who had not made the
decision. For a system whose entire purpose is that every published fact carries a
reviewer, an unauthenticated actor argument is the weakest possible link.

So a write now requires proof, and the actor it records comes **from the proof**, never
from an argument. `--actor` is gone from the authenticated write commands rather than
retained and validated, because an argument that must equal the session is an argument
that will eventually be trusted without the comparison.

WHAT THIS IS, AND WHAT IT IS NOT
================================
`AppUser.password_hash` is documented as the **fallback**: OIDC is the preferred path
and `external_subject` is where it lands. This is the fallback, for an operator at a
terminal before any identity provider is wired up. It is not a session service, issues
no token, and deliberately does not touch `user_session` -- those rows are for the
Next.js BFF and giving the CLI a persistent token would create a second, weaker way in.
Each command authenticates once and the process exits.

Credentials are read from a TTY or an environment variable, never from an argument.
Command-line arguments leak through shell history, process listings and logs.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.domains.identity.passwords import verify_password
from app.domains.verification.identity import (
    TEST_MARKER,
    VERIFY_PERMISSION,
    Reviewer,
    permissions_of,
)

#: Where a non-interactive run may supply the password. An environment variable is not
#: ideal, and it is materially better than an argument: it does not appear in `ps`, in
#: shell history, or in a command echoed into a log.
# S105: this is the NAME of an environment variable, not a password. The rule
# matches on the identifier containing "PASSWORD", which is the point of the name.
PASSWORD_ENV = "DATAHUB_REVIEWER_PASSWORD"  # noqa: S105


class AuthenticationFailedError(RuntimeError):
    """The credential did not establish who this is.

    Deliberately one error for "no such account", "wrong password" and "no local
    credential set". A caller that can tell them apart is an account-enumeration
    oracle, and the operator already knows which of the three applies.
    """


class ActorMismatchError(RuntimeError):
    """A decision was attributed to someone other than whoever authenticated."""


class FixtureIdentityRefusedError(RuntimeError):
    """A `[TEST ONLY]` identity tried to act on real data.

    Named `Fixture...` rather than `Test...` because pytest collects any class whose
    name begins with `Test`, and an exception it tried to instantiate as a test class
    broke collection for the whole module.
    """


@dataclass(frozen=True, slots=True)
class AuthenticatedReviewer:
    """Proof of who this process is. The only source of an actor for a write."""

    reviewer: Reviewer
    method: str
    """How they proved it. Recorded so an audit reader can weigh it."""

    @property
    def id(self) -> uuid.UUID:
        return self.reviewer.id

    @property
    def email(self) -> str:
        return self.reviewer.email

    @property
    def display_name(self) -> str:
        return self.reviewer.display_name

    @property
    def is_test(self) -> bool:
        return self.reviewer.is_test

    def bind(self, claimed_actor: uuid.UUID | None) -> uuid.UUID:
        """Return the actor to record, refusing any claim that is not this session.

        Present for the one caller that still accepts an actor argument for backward
        compatibility. It returns the authenticated id in every accepted case, so a
        caller that ignores the argument entirely behaves identically.
        """
        if claimed_actor is not None and claimed_actor != self.id:
            raise ActorMismatchError(
                f"authenticated as {self.email} ({self.id}) but the decision names "
                f"{claimed_actor}. A decision is attributed to whoever made it."
            )
        return self.id


def read_password(*, prompt: str = "Reviewer password: ") -> str:
    """A password from the environment, or from a TTY. Never from an argument."""
    supplied = os.environ.get(PASSWORD_ENV)
    if supplied:
        return supplied
    import getpass

    return getpass.getpass(prompt)


def authenticate(connection: Connection, *, email: str, password: str) -> AuthenticatedReviewer:
    """Prove an identity by local password, and confirm it may verify sources.

    Four refusals, in this order: unknown or credential-less account, wrong password,
    deactivated account, and an account whose roles do not carry `source:verify`. The
    first two are reported identically on purpose -- see `AuthenticationFailedError`.
    """
    row = connection.execute(
        text(
            "SELECT id, email, display_name, password_hash, is_active "
            "  FROM app_user WHERE lower(email) = lower(:email)"
        ),
        {"email": email.strip()},
    ).one_or_none()
    if row is None:
        raise AuthenticationFailedError("no account with that email, or no password set")

    result = verify_password(row.password_hash, password)
    if not result.matched:
        raise AuthenticationFailedError("no account with that email, or no password set")

    if not row.is_active:
        raise AuthenticationFailedError(f"{row.email} is deactivated")

    if result.replacement_hash is not None:
        # The stored parameters are behind the library's current defaults. Upgrading on
        # a successful login is the only moment the plaintext is available to do it.
        connection.execute(
            text("UPDATE app_user SET password_hash = :h, updated_at = now() WHERE id = :i"),
            {"h": result.replacement_hash, "i": row.id},
        )

    roles = tuple(
        entry.role_code
        for entry in connection.execute(
            text("SELECT role_code FROM user_role WHERE user_id = :i ORDER BY role_code"),
            {"i": row.id},
        )
    )
    held = permissions_of(connection, row.id)
    if VERIFY_PERMISSION not in held:
        raise AuthenticationFailedError(
            f"{row.email} authenticated but holds {sorted(roles) or 'no roles'}, and "
            f"none of them carries {VERIFY_PERMISSION!r}"
        )

    reviewer = Reviewer(
        id=row.id,
        email=str(row.email),
        display_name=str(row.display_name),
        roles=roles,
        is_test=str(row.display_name).startswith(TEST_MARKER),
    )
    return AuthenticatedReviewer(reviewer=reviewer, method="local-password")


def refuse_test_identity_on_real_data(session: AuthenticatedReviewer) -> None:
    """Stop a `[TEST ONLY]` identity making a decision about the real pilot.

    A fixture identity is allowed to authenticate -- the fixture workflow tests depend
    on it -- and is not allowed to reach the operator commands that write to the real
    database. Both halves matter: a test identity that could not authenticate would
    leave the workflow untested, and one that could decide real sources would put a
    fixture's judgement in the audit trail.
    """
    if session.is_test:
        raise FixtureIdentityRefusedError(
            f"{session.email} is a {TEST_MARKER} identity. It may authenticate, and it "
            "may not record decisions about real sources."
        )


__all__ = [
    "PASSWORD_ENV",
    "ActorMismatchError",
    "AuthenticatedReviewer",
    "AuthenticationFailedError",
    "FixtureIdentityRefusedError",
    "authenticate",
    "read_password",
    "refuse_test_identity_on_real_data",
]
