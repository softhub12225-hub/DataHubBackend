"""Hashing and verifying a local operator password. Argon2id, as the model says.

WHY ARGON2ID AND NOT SOMETHING ALREADY INSTALLED
================================================
`AppUser.password_hash` has carried the comment *"Argon2id. NULL when the user
authenticates via OIDC"* since the identity module was written. The algorithm was
chosen there; this module implements what was already decided rather than substituting
`hashlib.scrypt` because it happened to be in the standard library. A column whose
comment and contents disagree is worse than either.

WHERE THIS SITS IN THE INTENDED MODEL
=====================================
OIDC is the preferred path and `external_subject` is where it lands. A local password
is the documented **fallback**, and this is that fallback -- no more. It exists so that
an operator can authenticate at a terminal before any browser-facing identity provider
is wired up, and `authenticate()` says so in its docstring rather than leaving the next
reader to assume this is the production path.

WHAT IT REFUSES TO DO
=====================
It never logs, prints, returns or stores a plaintext password, and it has no function
that generates one. `verify` re-hashes on demand when Argon2's parameters move, which is
the only reason it returns a replacement hash at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: One hasher, with the library's current defaults. Deliberately not tuned down for
#: test speed: a test suite that exercises a weaker KDF than production is testing
#: something else.
_HASHER = PasswordHasher()

#: The shortest password this will accept. Not a policy engine -- a floor, so that an
#: empty string or a typo cannot become a credential.
MINIMUM_LENGTH = 12


class WeakPasswordError(ValueError):
    """The supplied password is too short to be accepted."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Whether the password matched, and a rehash when the parameters have moved."""

    matched: bool
    replacement_hash: str | None = None


def hash_password(password: str) -> str:
    """Hash a password for storage. The plaintext is never returned or logged."""
    if len(password) < MINIMUM_LENGTH:
        raise WeakPasswordError(
            f"a password must be at least {MINIMUM_LENGTH} characters; " f"{len(password)} supplied"
        )
    return _HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> VerificationResult:
    """Check a password against a stored hash.

    A missing hash is a failed verification rather than an error: an account with no
    local credential simply cannot authenticate this way, which is the correct answer
    for every OIDC-only user.
    """
    if not stored_hash:
        return VerificationResult(matched=False)
    try:
        _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return VerificationResult(matched=False)
    replacement = hash_password(password) if _HASHER.check_needs_rehash(stored_hash) else None
    return VerificationResult(matched=True, replacement_hash=replacement)


__all__ = [
    "MINIMUM_LENGTH",
    "VerificationResult",
    "WeakPasswordError",
    "hash_password",
    "verify_password",
]
