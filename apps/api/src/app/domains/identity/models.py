"""Identity and RBAC — schema only.

No authentication flow, no password verification, no session issuance. Those arrive
with the identity module; putting a stub here would invite code to be written against
it. What exists is the shape the architecture's six roles and segregation-of-duties
rules need.

`app_user` and `user_session` are named with prefixes because `user` and `session`
collide with PostgreSQL reserved words — an unquoted `SELECT ... FROM user` returns
the current role, not the table, which is the kind of bug that survives review.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.db.mixins import TimestampedMixin, uuid_pk


class AppUser(TimestampedMixin, Base):
    """An internal operator: consultant, editor, reviewer, ops or admin.

    `password_hash` is nullable because OIDC is the preferred path (no local
    credential at all); a local password is the fallback.
    """

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(
        Text, comment="Argon2id. NULL when the user authenticates via OIDC"
    )
    external_subject: Mapped[str | None] = mapped_column(String(256), comment="OIDC subject claim")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    mfa_enrolled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roles: Mapped[list[UserRole]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_is_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_has_a_domain"),
        CheckConstraint(
            "is_active = true OR deactivated_at IS NOT NULL",
            name="deactivation_has_a_timestamp",
        ),
        UniqueConstraint("external_subject", name="uq_app_user_external_subject"),
        {"comment": "Internal operators. Named app_user: `user` is reserved in SQL."},
    )


class Role(TimestampedMixin, Base):
    """A named role. The six the architecture defines, seeded in migration."""

    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(48), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_reviewer_role: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Marks roles the segregation-of-duties rules apply to",
    )

    permissions: Mapped[list[RolePermission]] = relationship(back_populates="role")

    __table_args__ = ({"comment": "Roles: consultant_readonly, data_editor, reviewer, ops, ..."},)


class Permission(TimestampedMixin, Base):
    """A permission string, e.g. `proposal:publish`.

    Reserved up front so no endpoint has to invent one later, and so the grant matrix
    is reviewable before any endpoint exists.
    """

    __tablename__ = "permission"

    code: Mapped[str] = mapped_column(String(96), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("code ~ '^[a-z_]+:[a-z_]+$'", name="permission_code_shape"),
        {"comment": "Permission strings in `object:action` form."},
    )


class RolePermission(Base):
    __tablename__ = "role_permission"

    role_code: Mapped[str] = mapped_column(
        String(48), ForeignKey("role.code", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        String(96), ForeignKey("permission.code", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    role: Mapped[Role] = relationship(back_populates="permissions")

    __table_args__ = ({"comment": "Which permissions a role holds."},)


class UserRole(Base):
    """A role assignment, optionally scoped to a destination.

    Destination scoping exists because reviewer assignment by destination is a
    phase-2 requirement, and retrofitting the column later would mean rewriting every
    authorisation check.
    """

    __tablename__ = "user_role"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(
        String(48), ForeignKey("role.code", ondelete="RESTRICT"), nullable=False
    )
    destination_code: Mapped[str | None] = mapped_column(
        String(8),
        ForeignKey("destination.code", ondelete="RESTRICT"),
        comment="NULL = all destinations",
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[AppUser] = relationship(back_populates="roles")

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "role_code",
            "destination_code",
            name="uq_user_role_user_id_role_code_destination_code",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_user_role_user_id", "user_id"),
        {"comment": "Role assignments, optionally destination-scoped."},
    )


class ApiClient(TimestampedMixin, Base):
    """A machine consumer of the internal API (CRM, matching service).

    Only a hash of the key is stored: a leaked database dump must not yield working
    credentials.
    """

    __tablename__ = "api_client"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Hash only; the key itself is never stored"
    )
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="Non-secret prefix, for identifying a key in logs"
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    ip_allowlist: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("array_length(scopes, 1) >= 1", name="client_has_at_least_one_scope"),
        CheckConstraint("rate_limit_per_minute > 0", name="rate_limit_is_positive"),
        UniqueConstraint("key_prefix", name="uq_api_client_key_prefix"),
        {"comment": "Internal API consumers. Hashed keys, explicit scopes."},
    )


class UserSession(Base):
    """A server-side session record.

    Held by the Next.js BFF; no token ever reaches browser JavaScript. Only a hash is
    stored, for the same reason as API keys.
    """

    __tablename__ = "user_session"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),
        Index("ix_user_session_user_id", "user_id"),
        Index(
            "ix_user_session_expires_at",
            "expires_at",
            postgresql_where="revoked_at IS NULL",
        ),
        {"comment": "Server-side sessions. Named user_session: `session` is reserved-ish."},
    )


__all__ = [
    "ApiClient",
    "AppUser",
    "Permission",
    "Role",
    "RolePermission",
    "UserRole",
    "UserSession",
]


class CredentialEnrollment(Base):
    """One outstanding claim on a provisioned account (Step 5C.7D).

    WHY AN ACCOUNT NEEDS CLAIMING AT ALL
    ====================================
    Provisioning creates the account; it does not prove who may use it. Without this
    table the first password went to whoever ran the enrolment command first, so an
    operator who knew a reviewer's email could take the identity before its owner
    arrived. An email address is a routing label -- it is on business cards and in
    `git log` -- and cannot also be the thing that proves who you are.

    ONLY A HASH
    ===========
    `token_hash` is the SHA-256 of a 256-bit random token, as `UserSession.token_hash`
    is for the same reason. SHA-256 rather than Argon2id deliberately: a password needs
    an expensive KDF because it has little entropy, a 256-bit token does not, and
    Argon2's per-row salt would make the value impossible to look up.

    ONE LIVE CHALLENGE
    ==================
    A partial unique index on `user_id` where the row is neither used nor revoked means
    a second challenge cannot be issued while one is outstanding. Reissue therefore has
    to revoke first, which makes "reissue invalidates the previous token" something the
    database enforces rather than something the code remembers.
    """

    __tablename__ = "credential_enrollment"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment=(
            "SHA-256 of a 256-bit random token. The plaintext is shown once to the "
            "issuing operator and never stored, logged or audited."
        ),
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    issued_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        comment=(
            "The administrator who issued it. NULL when issued under the bootstrap "
            "authority, because no authenticated administrator existed -- see "
            "bootstrap_mode. Never back-filled with an invented actor."
        ),
    )
    bootstrap_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),
        CheckConstraint("used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked_not_both"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name="revocation_has_a_reason",
        ),
        CheckConstraint(
            "(bootstrap_mode AND issued_by IS NULL) OR "
            "(NOT bootstrap_mode AND issued_by IS NOT NULL)",
            name="issuer_matches_mode",
        ),
        Index("ix_credential_enrollment_user_id", "user_id"),
        Index(
            "ux_credential_enrollment_live",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
        {
            "comment": (
                "One outstanding claim on a provisioned account. Knowing an email "
                "address must not be enough to claim an identity, so the first password "
                "requires a one-time challenge delivered out of band. Only the token's "
                "SHA-256 is stored."
            )
        },
    )
