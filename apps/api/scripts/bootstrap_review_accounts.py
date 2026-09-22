"""Operator repair: ensure review-console accounts exist and can log in.

Uses the migration (owner) connection. Intended for fresh Neon databases or when
enrollment was never completed. Does not touch verification or publish state.

Usage::

    uv run python scripts/bootstrap_review_accounts.py \\
      --email softhub12225@gmail.com --display-name "Admin" --role admin \\
      --password 'YourPassword'

Run from ``apps/api``; reads ``datahub/.env`` via application settings.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.identity.enrollment import enrol_initial_password
from app.domains.identity.enrollment_tokens import issue as issue_enrollment_challenge
from app.domains.identity.passwords import hash_password
from app.domains.verification.identity import (
    BootstrapClosedError,
    provision_reviewer,
    permissions_of,
    VERIFY_PERMISSION,
)


def _has_password(connection, email: str) -> bool:
    row = connection.execute(
        text(
            "SELECT password_hash IS NOT NULL AS enrolled FROM app_user "
            " WHERE lower(email) = lower(:e)"
        ),
        {"e": email},
    ).one_or_none()
    return bool(row and row.enrolled)


def _set_password_owner(connection, email: str, password: str) -> None:
    """Owner-level repair when bootstrap enrollment cannot run."""
    connection.execute(
        text(
            "UPDATE app_user SET password_hash = :h, updated_at = now() "
            " WHERE lower(email) = lower(:e) AND is_active"
        ),
        {"h": hash_password(password), "e": email},
    )


def bootstrap_one(
    connection,
    *,
    email: str,
    display_name: str,
    role: str,
    password: str,
    force_password: bool,
) -> str:
    email = email.strip().lower()
    exists = connection.execute(
        text("SELECT 1 FROM app_user WHERE lower(email) = lower(:e)"),
        {"e": email},
    ).one_or_none()
    if not exists:
        provision_reviewer(
            connection,
            email=email,
            display_name=display_name,
            role=role,
        )
        action = "created"
    else:
        action = "exists"

    if _has_password(connection, email) and not force_password:
        return f"{email}: {action}, already enrolled (use --force-password to reset)"

    if not _has_password(connection, email):
        try:
            challenge = issue_enrollment_challenge(
                connection,
                email=email,
                issued_by=None,
                rotate=True,
                reason="bootstrap_review_accounts repair",
            )
            enrol_initial_password(connection, token=challenge.token, password=password)
            return f"{email}: {action}, enrolled via bootstrap token"
        except BootstrapClosedError:
            _set_password_owner(connection, email, password)
            return f"{email}: {action}, password set via owner repair (bootstrap closed)"

    _set_password_owner(connection, email, password)
    return f"{email}: password reset via owner repair"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", action="append", required=True, help="account email")
    parser.add_argument("--display-name", default="Review Operator")
    parser.add_argument("--role", default="admin", choices=("admin", "reviewer"))
    parser.add_argument("--password", required=True, help="login password to set")
    parser.add_argument(
        "--force-password",
        action="store_true",
        help="reset password even when already enrolled",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(
        settings.database.sync_dsn(DatabaseRole.MIGRATION),
        connect_args={"connect_timeout": 20},
    )
    try:
        with engine.begin() as connection:
            for email in args.email:
                print(
                    bootstrap_one(
                        connection,
                        email=email,
                        display_name=args.display_name,
                        role=args.role,
                        password=args.password,
                        force_password=args.force_password,
                    )
                )
            print()
            print("Login eligibility:")
            for email in args.email:
                row = connection.execute(
                    text(
                        "SELECT u.id, u.is_active FROM app_user u "
                        " WHERE lower(u.email) = lower(:e)"
                    ),
                    {"e": email},
                ).one()
                perms = permissions_of(connection, row.id)
                can = VERIFY_PERMISSION in perms
                print(f"  {email}: active={row.is_active} may_verify={can}")
        return 0
    except Exception as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
