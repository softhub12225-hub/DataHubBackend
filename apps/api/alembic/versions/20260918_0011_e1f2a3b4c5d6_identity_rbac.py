"""Domain group 9: identity and RBAC

Schema only -- no authentication flow, no session issuance.

`app_user` and `user_session` carry prefixes because `user` and `session` collide
with SQL reserved words; an unquoted `SELECT ... FROM user` returns the current role
rather than the table.

Only hashes of credentials are stored, for both users and API clients: a leaked
database dump must not yield working credentials.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=True,
            comment="Argon2id. NULL when the user authenticates via OIDC",
        ),
        sa.Column(
            "external_subject", sa.String(length=256), nullable=True, comment="OIDC subject claim"
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("mfa_enrolled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position('@' in email) > 1", name=op.f("ck_app_user_email_has_a_domain")
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_app_user_email_is_lowercase")),
        sa.CheckConstraint(
            "is_active = true OR deactivated_at IS NOT NULL",
            name=op.f("ck_app_user_deactivation_has_a_timestamp"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
        sa.UniqueConstraint("external_subject", name="uq_app_user_external_subject"),
        comment="Internal operators. Named app_user: `user` is reserved in SQL.",
    )
    op.create_table(
        "role",
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name_en", sa.String(length=96), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_reviewer_role",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="Marks roles the segregation-of-duties rules apply to",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_role")),
        comment="Roles: consultant_readonly, data_editor, reviewer, ops, ...",
    )
    op.create_table(
        "permission",
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code ~ '^[a-z_]+:[a-z_]+$'", name=op.f("ck_permission_permission_code_shape")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_permission")),
        comment="Permission strings in `object:action` form.",
    )
    op.create_table(
        "role_permission",
        sa.Column("role_code", sa.String(length=48), nullable=False),
        sa.Column("permission_code", sa.String(length=96), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["permission_code"],
            ["permission.code"],
            name=op.f("fk_role_permission_permission_code_permission"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_code"],
            ["role.code"],
            name=op.f("fk_role_permission_role_code_role"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_code", "permission_code", name=op.f("pk_role_permission")),
        comment="Which permissions a role holds.",
    )
    op.create_table(
        "user_role",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_code", sa.String(length=48), nullable=False),
        sa.Column(
            "destination_code",
            sa.String(length=8),
            nullable=True,
            comment="NULL = all destinations",
        ),
        sa.Column("granted_by", sa.UUID(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["destination_code"],
            ["destination.code"],
            name=op.f("fk_user_role_destination_code_destination"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_code"],
            ["role.code"],
            name=op.f("fk_user_role_role_code_role"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_role_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_role")),
        sa.UniqueConstraint(
            "user_id",
            "role_code",
            "destination_code",
            name="uq_user_role_user_id_role_code_destination_code",
            postgresql_nulls_not_distinct=True,
        ),
        comment="Role assignments, optionally destination-scoped.",
    )
    op.create_index("ix_user_role_user_id", "user_role", ["user_id"], unique=False)
    op.create_table(
        "api_client",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "key_hash",
            sa.Text(),
            nullable=False,
            comment="Hash only; the key itself is never stored",
        ),
        sa.Column(
            "key_prefix",
            sa.String(length=16),
            nullable=False,
            comment="Non-secret prefix, for identifying a key in logs",
        ),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), server_default="60", nullable=False),
        sa.Column("ip_allowlist", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "array_length(scopes, 1) >= 1", name=op.f("ck_api_client_client_has_at_least_one_scope")
        ),
        sa.CheckConstraint(
            "rate_limit_per_minute > 0", name=op.f("ck_api_client_rate_limit_is_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_client")),
        sa.UniqueConstraint("key_prefix", name="uq_api_client_key_prefix"),
        sa.UniqueConstraint("name", name=op.f("uq_api_client_name")),
        comment="Internal API consumers. Hashed keys, explicit scopes.",
    )
    op.create_table(
        "user_session",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expires_at > issued_at", name=op.f("ck_user_session_expiry_after_issue")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_session_token_hash")),
        comment="Server-side sessions. Named user_session: `session` is reserved-ish.",
    )
    op.create_index(
        "ix_user_session_expires_at",
        "user_session",
        ["expires_at"],
        unique=False,
        postgresql_where="revoked_at IS NULL",
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("user_session")
    op.drop_table("api_client")
    op.drop_table("user_role")
    op.drop_table("role_permission")
    op.drop_table("permission")
    op.drop_table("role")
    op.drop_table("app_user")
