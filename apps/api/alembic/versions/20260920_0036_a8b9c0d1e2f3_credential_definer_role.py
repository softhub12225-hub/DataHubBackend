"""Step 5C.7F: the credential-claim function stops running as a superuser

THE BLAST RADIUS BEING REDUCED
==============================
`app_claim_enrollment` is `SECURITY DEFINER`, which means its body runs with its owner's
privileges. Step 5C.7D made the owner `datahub` -- the schema owner, which on this
cluster is also a **superuser**. The body was static, had no dynamic SQL and was tested,
so nothing was exploitable. But "not exploitable today" is a property of the current
code, and the owner is a property of the object: any future defect in that function would
have had the whole cluster available to it.

So the function gets an owner that can do exactly what the function does and nothing
else. A defect in it can then reach two tables and four columns.

WHAT THE DEFINER MAY DO
=======================
Read from the body, statement by statement, not guessed:

* `UPDATE credential_enrollment SET used_at = now() WHERE token_hash = ... RETURNING id,
  user_id` needs `UPDATE(used_at)` and `SELECT` on the columns in the predicate and the
  `RETURNING` list.
* `SELECT 1 FROM credential_enrollment WHERE ... expires_at <= now()` needs the same
  `SELECT` columns.
* `UPDATE app_user SET password_hash = ..., updated_at = now() WHERE id = ... AND
  password_hash IS NULL AND is_active` needs `UPDATE(password_hash, updated_at)` and
  `SELECT` on the predicate columns.
* `SELECT 1 FROM app_user WHERE id = ... AND password_hash IS NOT NULL` needs `SELECT` on
  those two.

Column-level throughout, because PostgreSQL supports it here and a whole-table `UPDATE`
on `app_user` would let a defect change an email, a display name or `is_active` -- none
of which enrolment has any business touching. Neither table carries a trigger, so there
is no hidden statement needing rights of its own; that was checked rather than assumed.

The definer gets **no** `INSERT` on `credential_enrollment`. Claiming a challenge never
creates one, and issuing remains the owner's or an authenticated administrator's work,
exactly as Step 5C.7D established. It gets no `DELETE`, no `TRUNCATE`, no `CREATE`, no
canonical writes and no trust-verification writes.

THE ROLE ITSELF
===============
`NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT`, with no password and no members, plus
`NOSUPERUSER NOREPLICATION NOBYPASSRLS` wherever this migration runs as a superuser --
see `upgrade` for why those three are conditional and why that costs nothing.
It is not an identity anybody authenticates as; it exists only to be the thing the
function runs as. `NOINHERIT` is belt-and-braces -- it holds no memberships to inherit
from -- and `NOLOGIN` is what stops it ever being a connection.

WHY THE DOWNGRADE DOES NOT DROP IT
==================================
Roles are **cluster-wide**, and this cluster carries `datahub`, `datahub_test` and
`datahub_gates`. Dropping the role while downgrading one database would break the
function in the others, which is a cross-database outage caused by a single-database
migration. So `downgrade()` returns ownership and revokes the privileges, and leaves an
inert, privilege-less, unusable role behind. `upgrade()` creates it idempotently for the
same reason: another database on the same cluster may have created it already.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The role that owns the credential-claim function. Named for what it is, not for a
#: service: nothing runs as it and nothing is a member of it.
DEFINER = "app_credential_definer"

FUNCTION = "app_claim_enrollment(text, text)"

#: Exactly the columns the function body reads, and exactly the ones it writes.
ENROLLMENT_SELECT = "id, user_id, token_hash, used_at, revoked_at, expires_at"
ENROLLMENT_UPDATE = "used_at"
USER_SELECT = "id, password_hash, is_active"
USER_UPDATE = "password_hash, updated_at"


def upgrade() -> None:
    # Idempotent: the role is cluster-wide and another database on this cluster may
    # already have created it. The attributes are re-asserted either way, so a role that
    # somehow acquired LOGIN or CREATEROLE is corrected rather than trusted.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEFINER}') THEN
                CREATE ROLE {DEFINER};
            END IF;
        END
        $$;
        """
    )
    # Split in two because three of these attributes are superuser-only to SET, even
    # to the value they already hold. A managed provider (Neon, Supabase, RDS) gives
    # you an owner with CREATEROLE but not SUPERUSER, and naming NOSUPERUSER there
    # fails the whole migration with "permission denied to alter role".
    #
    # Nothing is weakened by making them conditional. SUPERUSER, REPLICATION and
    # BYPASSRLS are off for any role CREATE ROLE produces, and only a superuser can
    # turn them on -- so on a cluster with no superuser available to this migration,
    # there is no path by which the role could have acquired them. The attributes a
    # CREATEROLE owner *can* grant are exactly the ones still asserted unconditionally.
    op.execute(f"ALTER ROLE {DEFINER} NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF (SELECT rolsuper FROM pg_roles WHERE rolname = CURRENT_USER) THEN
                EXECUTE 'ALTER ROLE {DEFINER} NOSUPERUSER NOREPLICATION NOBYPASSRLS';
            END IF;
        END
        $$;
        """
    )
    # No password, ever. A role with no password and NOLOGIN cannot be authenticated as,
    # whatever the host-based authentication file says.
    op.execute(f"ALTER ROLE {DEFINER} PASSWORD NULL")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {DEFINER}")
    op.execute(f"GRANT SELECT ({ENROLLMENT_SELECT}) ON credential_enrollment TO {DEFINER}")
    op.execute(f"GRANT UPDATE ({ENROLLMENT_UPDATE}) ON credential_enrollment TO {DEFINER}")
    op.execute(f"GRANT SELECT ({USER_SELECT}) ON app_user TO {DEFINER}")
    op.execute(f"GRANT UPDATE ({USER_UPDATE}) ON app_user TO {DEFINER}")

    # Handing the function over requires being able to SET ROLE to the new owner.
    #
    # A superuser always can, which is why this was never needed locally. A managed
    # provider's owner (Neon, Supabase, RDS) has CREATEROLE and no superuser, and since
    # PostgreSQL 16 the membership such a creator is auto-granted on a role it creates
    # carries ADMIN but neither SET nor INHERIT -- that pair is governed by the
    # `createrole_self_grant` GUC, which defaults to empty. The result is an owner that
    # may administer the role but not become it, and `ALTER FUNCTION ... OWNER TO` then
    # fails with "must be able to SET ROLE".
    #
    # SET TRUE, INHERIT FALSE is the whole requirement and the whole grant. The owner
    # needs to *become* the definer for one statement; it has no business passively
    # holding the definer's rights on `app_user` and `credential_enrollment` for the
    # rest of the session, which is what INHERIT would mean.
    #
    # Skipped for a superuser, who needs no grant, and below PostgreSQL 16, where
    # `WITH SET` is not syntax and the creator's auto-grant was full membership
    # already. Idempotent, so no probe of `pg_auth_members.set_option` -- that column
    # is itself 16-only and naming it in a statically parsed expression would break
    # the very clusters the version guard exists to protect.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = CURRENT_USER)
               AND current_setting('server_version_num')::int >= 160000 THEN
                EXECUTE format(
                    'GRANT {DEFINER} TO %I WITH INHERIT FALSE, SET TRUE', CURRENT_USER
                );
            END IF;
        END
        $$;
        """
    )

    # The role must exist and hold its privileges before it owns the function, or the
    # function is briefly owned by a role that cannot execute its own body.
    # CREATE on the schema, lent for exactly one statement.
    #
    # PostgreSQL requires the NEW owner -- not the caller -- to hold CREATE on the
    # object's schema, so that changing an owner can never achieve something the new
    # owner could not have achieved by creating the object itself. A superuser is
    # exempt from that check and from the SET ROLE one above, which is why running
    # these migrations as a local superuser never exercised either.
    #
    # The definer must not keep CREATE: a role whose entire purpose is to own one
    # function and write two column sets has no business creating schema objects, and
    # `10-runtime-roles.sh` and `neon-roles.sql` both revoke exactly this from the
    # service roles. So it is granted, used, and revoked in the same transaction --
    # ownership persists once set, the privilege does not need to.
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {DEFINER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {DEFINER}")

    # Ownership carries EXECUTE, so re-assert the intended grantees explicitly: changing
    # an owner rewrites the ACL, and "it still works" is not the same as "only app_api
    # can still work".
    #
    # These run AS the definer. Granting and revoking on a function is the owner's
    # right, and the statement above just made that somebody else -- so the role that
    # began this migration can no longer touch the ACL it is trying to assert. A
    # superuser could regardless, which is the third and last place that exemption was
    # hiding. `SET ROLE` is available here because of the membership granted earlier.
    op.execute(f"SET ROLE {DEFINER}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION {FUNCTION} TO app_api';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'REVOKE ALL ON FUNCTION {FUNCTION} FROM app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
                EXECUTE 'REVOKE ALL ON FUNCTION {FUNCTION} FROM app_publisher';
            END IF;
        END
        $$;
        """
    )
    # Back to the migration identity for whatever runs next. The transaction would
    # reset it anyway; being explicit means a later statement added to this function
    # cannot silently execute as the definer.
    op.execute("RESET ROLE")


def downgrade() -> None:
    # Ownership first: the function must never be left owned by a role whose privileges
    # have just been taken away, and never owned by a role that no longer exists.
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO CURRENT_USER")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION {FUNCTION} TO app_api';
            END IF;
        END
        $$;
        """
    )

    op.execute(f"REVOKE ALL ON credential_enrollment FROM {DEFINER}")
    op.execute(f"REVOKE ALL ON app_user FROM {DEFINER}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {DEFINER}")

    # The role is deliberately NOT dropped. Roles are cluster-wide and this cluster holds
    # more than one database using this function; dropping it here would break them. What
    # is left is inert: NOLOGIN, no password, no privileges, owning nothing.


__all__ = ["DEFINER", "downgrade", "upgrade"]
