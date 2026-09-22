"""Step 5C.7D: claiming a provisioned account requires a one-time challenge

THE FIRST-CLAIM ATTACK
======================
Step 5C.7C stopped an existing credential being overwritten. It did not stop the account
being claimed in the first place: `enrol_initial_password` required an email address and
the absence of a password, so whoever ran it first won. An operator who knew Dejan's
email could enrol before he did, choose the password, authenticate as him and record
verification decisions in his name -- the same takeover as before, one step earlier.

An email address is a routing label. It is on business cards, in `git log`, and in this
repository's own documentation. It cannot also be the thing that proves who you are.

WHAT THIS ADDS
==============
One row per outstanding claim. The reviewer receives a 256-bit random token out of band;
the database stores only its SHA-256, exactly as `user_session.token_hash` does and for
the same reason. Enrolment then requires possession of the token, which nobody can
derive from the email.

WHY SHA-256 AND NOT ARGON2
==========================
`app_user.password_hash` is Argon2id because a human-chosen password has little entropy
and must be expensive to guess. A `secrets.token_urlsafe(32)` token has 256 bits and is
not guessable at any cost, so a slow KDF buys nothing -- and it would make the token
unfindable, because Argon2's per-row random salt means you cannot look a value up by its
hash. The threat models are different and so are the primitives.

ONE LIVE CHALLENGE PER ACCOUNT
==============================
`ux_credential_enrollment_live` is UNIQUE on `user_id` WHERE the row is neither used nor
revoked. Issuing a second challenge while one is outstanding is refused by the database,
so reissue has to revoke first -- which is what makes "reissue invalidates the previous
token" an invariant rather than a convention.

ISSUING AND CLAIMING ARE DIFFERENT PRIVILEGES
=============================================
This is the part that actually closes the attack, and it has to be in the grants rather
than in Python, because the attacker in this threat model is an operator running our own
code.

* **Issuing** writes a row, and no runtime role may: `app_api`, `app_worker` and
  `app_publisher` hold `SELECT` on `credential_enrollment` and nothing else. Issuance
  therefore requires the owning connection, which is the documented bootstrap boundary.
* **Claiming** is `app_claim_enrollment`, a `SECURITY DEFINER` function that `app_api`
  may execute. It is the *only* way `app_api` can write `app_user.password_hash` -- the
  role holds no `UPDATE` on `app_user` at all -- and it refuses unless a live, unexpired,
  unused token is presented.

So possession of the application credential lets somebody *use* a token and never *mint*
one. A plain `GRANT UPDATE (password_hash) ON app_user TO app_api` would have been the
easy way to separate the two commands and a bad one: it would let a compromised API
process set anybody's password, which is a larger hole than the one being closed.

The function takes the token's SHA-256 and an already-Argon2id-hashed password, so it
never sees either plaintext, and `SET search_path` is pinned because a `SECURITY DEFINER`
function without one can be hijacked by a caller-controlled schema.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "credential_enrollment"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of a 256-bit random token. The plaintext is shown once to the "
                "issuing operator and never stored, logged or audited."
            ),
        ),
        sa.Column(
            "issued_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column(
            "issued_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "The administrator who issued it. NULL when issued under the bootstrap "
                "authority, because no authenticated administrator existed -- see "
                "bootstrap_mode. Never back-filled with an invented actor."
            ),
        ),
        sa.Column("bootstrap_mode", sa.Boolean(), nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_enrollment")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_credential_enrollment_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"],
            ["app_user.id"],
            name=op.f("fk_credential_enrollment_issued_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_credential_enrollment_token_hash")),
        sa.CheckConstraint(
            "expires_at > issued_at", name=op.f("ck_credential_enrollment_expiry_after_issue")
        ),
        # A challenge is used, or revoked, or outstanding. Never two of those at once,
        # because "which happened?" must have one answer.
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_credential_enrollment_used_or_revoked_not_both"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name=op.f("ck_credential_enrollment_revocation_has_a_reason"),
        ),
        # Bootstrap issuance has no issuer by definition; an administrative one must
        # name theirs. Without this the two are indistinguishable after the fact.
        sa.CheckConstraint(
            "(bootstrap_mode AND issued_by IS NULL) OR "
            "(NOT bootstrap_mode AND issued_by IS NOT NULL)",
            name=op.f("ck_credential_enrollment_issuer_matches_mode"),
        ),
        comment=(
            "One outstanding claim on a provisioned account. Knowing an email address "
            "must not be enough to claim an identity, so the first password requires a "
            "one-time challenge delivered out of band. Only the token's SHA-256 is "
            "stored."
        ),
    )
    op.create_index(op.f("ix_credential_enrollment_user_id"), TABLE, ["user_id"], unique=False)
    # THE invariant behind "reissue invalidates the previous token": at most one live
    # challenge per account, enforced by the database rather than by remembering to.
    op.create_index(
        "ux_credential_enrollment_live",
        TABLE,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )

    # Claiming a challenge: the whole operation, as one statement, owned by the schema
    # owner and callable by the application role. Everything that must be true is in the
    # two WHERE clauses, so no caller can skip a check by calling it differently.
    op.execute(
        """
        CREATE FUNCTION app_claim_enrollment(p_token_hash text, p_password_hash text)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            v_claim uuid;
            v_user  uuid;
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'ENROLLMENT_TOKEN_INVALID' USING ERRCODE = '28000';
            END IF;
            -- Refuse a plaintext password outright. The caller hashes with Argon2id and
            -- this function never holds anything a leak could replay.
            IF p_password_hash IS NULL OR p_password_hash NOT LIKE '$argon2id$%' THEN
                RAISE EXCEPTION 'ENROLLMENT_PASSWORD_NOT_HASHED' USING ERRCODE = '22023';
            END IF;

            -- THE RACE LIVES HERE. Claiming is a conditional UPDATE, not a SELECT
            -- followed by one: two concurrent enrolments serialise on this row and the
            -- second finds used_at already set, matches nothing, and loses.
            UPDATE credential_enrollment SET used_at = now()
             WHERE token_hash = p_token_hash
               AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
             RETURNING id, user_id INTO v_claim, v_user;

            IF v_claim IS NULL THEN
                IF EXISTS (
                    SELECT 1 FROM credential_enrollment
                     WHERE token_hash = p_token_hash AND used_at IS NULL
                       AND revoked_at IS NULL AND expires_at <= now()
                ) THEN
                    RAISE EXCEPTION 'ENROLLMENT_TOKEN_EXPIRED' USING ERRCODE = '28000';
                END IF;
                RAISE EXCEPTION 'ENROLLMENT_TOKEN_INVALID' USING ERRCODE = '28000';
            END IF;

            -- And again here: `password_hash IS NULL` is what makes enrolment happen at
            -- most once per account, whoever calls this and however often.
            UPDATE app_user SET password_hash = p_password_hash, updated_at = now()
             WHERE id = v_user AND password_hash IS NULL AND is_active;
            IF NOT FOUND THEN
                -- Two different problems, and the operator needs to know which: an
                -- account somebody else already claimed is a security event, and a
                -- disabled account is an administrative one. The token is spent only if
                -- this transaction commits, so raising here leaves it usable.
                IF EXISTS (
                    SELECT 1 FROM app_user
                     WHERE id = v_user AND password_hash IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'ENROLLMENT_ALREADY_CLAIMED' USING ERRCODE = '28000';
                END IF;
                RAISE EXCEPTION 'ENROLLMENT_ACCOUNT_DISABLED' USING ERRCODE = '28000';
            END IF;
            RETURN v_user;
        END
        $fn$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app_claim_enrollment(text, text) FROM PUBLIC")

    # Runtime roles may see that a challenge exists and never write one: issuance runs
    # on the owning connection, like every other identity operation. `app_api` may
    # additionally claim one, which is the only write it can reach on `app_user`.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                EXECUTE 'GRANT SELECT ON credential_enrollment TO app_api';
                EXECUTE 'GRANT EXECUTE ON FUNCTION app_claim_enrollment(text, text) '
                        'TO app_api';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                EXECUTE 'GRANT SELECT ON credential_enrollment TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
                EXECUTE 'GRANT SELECT ON credential_enrollment TO app_publisher';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_claim_enrollment(text, text)")
    op.drop_index("ux_credential_enrollment_live", table_name=TABLE)
    op.drop_index(op.f("ix_credential_enrollment_user_id"), table_name=TABLE)
    op.drop_table(TABLE)
