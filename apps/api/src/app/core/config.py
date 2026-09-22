"""Typed, environment-driven application settings.

Settings are read once at import of :func:`get_settings` and cached. Nothing in this
module connects to anything: importing the application must stay side-effect free so
that ``scripts/export_openapi.py`` and unit tests can build the app without Postgres,
Redis or MinIO running.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        """True for environments where placeholder secrets must be rejected."""
        return self in {Environment.STAGING, Environment.PRODUCTION}


# Values that must never reach a deployed environment. Fail fast at startup rather
# than discover them in a breach report.
PLACEHOLDER_SECRETS = frozenset(
    {"", "change-me", "changeme", "postgres", "password", "secret", "minioadmin"}
)


class DatabaseRole(StrEnum):
    """A database identity the platform connects as.

    One identity per privilege level, never one shared superuser. The privileges
    themselves are installed with the Step 3 domain migrations (see
    ``infra/postgres/README.md``); this enum and the credentials below exist first so
    that no code is ever written against the owning role by default.
    """

    MIGRATION = "migration"
    """Owns and creates schema objects. Used by Alembic only -- never by a service."""

    API = "api"
    """Request handling. Reads canonical projections; owns nothing."""

    WORKER = "worker"
    """Celery workers: acquisition, extraction, detection, SLA sweeps. Owns nothing."""

    PUBLISHER = "publisher"
    """The publication transaction. The only identity granted canonical writes."""


#: Identities that must never be the schema owner. Enforced in every environment.
NON_OWNER_ROLES: tuple[DatabaseRole, ...] = (
    DatabaseRole.API,
    DatabaseRole.WORKER,
    DatabaseRole.PUBLISHER,
)


class DatabaseSettings(BaseSettings):
    """Connection target plus one credential pair per database role.

    There is deliberately no single ``POSTGRES_USER``/``POSTGRES_PASSWORD`` pair for
    the application to fall back on. A service that cannot find its own credentials
    fails at startup rather than quietly connecting as the schema owner, which would
    make the Step 3 privilege separation (invariants I1 and I2) decorative.
    """

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    db: str = "datahub"
    #: Set to ``require`` for managed Postgres (e.g. Neon). Appended to every DSN.
    sslmode: str | None = None

    # One credential pair per role. Usernames default to the role names created by
    # infra/postgres/init/10-runtime-roles.sh; passwords default to an obvious
    # placeholder that deployed environments reject.
    # The migration identity is optional, and absent by design in service
    # containers: a process that cannot migrate the schema should not be holding the
    # owner's password at all. Requesting it when unset raises rather than producing
    # a DSN with an empty username.
    migration_user: str | None = None
    migration_password: SecretStr | None = None
    api_user: str = "app_api"
    api_password: SecretStr = SecretStr("change-me")
    worker_user: str = "app_worker"
    worker_password: SecretStr = SecretStr("change-me")
    publisher_user: str = "app_publisher"
    publisher_password: SecretStr = SecretStr("change-me")

    # Connection pooling. Deliberately small defaults: the API runs several replicas
    # and Postgres connections are the scarcer resource.
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=5, ge=0, le=50)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, gt=0)
    statement_timeout_ms: int = Field(default=15_000, gt=0)

    @model_validator(mode="after")
    def _blank_migration_credentials_mean_absent(self) -> Self:
        """Treat an empty string as "not configured".

        Compose and Kubernetes can override an inherited variable to empty but
        cannot remove it, so ``POSTGRES_MIGRATION_USER: ""`` is how a service
        container declares it has no migration identity.
        """
        if self.migration_user is not None and not self.migration_user.strip():
            object.__setattr__(self, "migration_user", None)
        if (
            self.migration_password is not None
            and not self.migration_password.get_secret_value().strip()
        ):
            object.__setattr__(self, "migration_password", None)
        return self

    def credentials(self, role: DatabaseRole) -> tuple[str, SecretStr]:
        """Username and password for ``role``.

        Raises for the migration role when it is not configured, which is the
        normal state inside an API or worker container.
        """
        match role:
            case DatabaseRole.MIGRATION:
                if self.migration_user is None or self.migration_password is None:
                    raise ValueError(
                        "the migration role is not configured in this process; set "
                        "POSTGRES_MIGRATION_USER and POSTGRES_MIGRATION_PASSWORD "
                        "(only the migration runner should have them)"
                    )
                return self.migration_user, self.migration_password
            case DatabaseRole.API:
                return self.api_user, self.api_password
            case DatabaseRole.WORKER:
                return self.worker_user, self.worker_password
            case DatabaseRole.PUBLISHER:
                return self.publisher_user, self.publisher_password

    def _dsn(self, role: DatabaseRole, driver: str) -> str:
        user, password = self.credentials(role)
        query = f"sslmode={self.sslmode}" if self.sslmode else None
        return str(
            PostgresDsn.build(
                scheme=driver,
                username=user,
                password=password.get_secret_value(),
                host=self.host,
                port=self.port,
                path=self.db,
                query=query,
            )
        )

    # Plain methods, deliberately NOT pydantic computed fields: a computed field is
    # part of the model, so it appears in repr() and model_dump() -- which would
    # print the password in clear text and defeat SecretStr entirely.
    def async_dsn(self, role: DatabaseRole) -> str:
        """DSN for an async engine bound to ``role``. Contains the password."""
        return self._dsn(role, "postgresql+asyncpg")

    def sync_dsn(self, role: DatabaseRole) -> str:
        """DSN for a synchronous connection as ``role``. Contains the password.

        Alembic uses this with :data:`DatabaseRole.MIGRATION`.
        """
        return self._dsn(role, "postgresql+psycopg")

    @model_validator(mode="after")
    def _services_must_not_use_the_owning_role(self) -> Self:
        """Reject any configuration where a service connects as the schema owner.

        Enforced in local and CI too, not just deployed environments: if it is
        allowed to work locally, that is the configuration that gets copied.
        """
        if self.migration_user is None:
            # No migration identity in this process, so no owner to collide with.
            return self
        offenders = [
            role.value
            for role in NON_OWNER_ROLES
            if self.credentials(role)[0] == self.migration_user
        ]
        if offenders:
            raise ValueError(
                "these roles are configured with the schema-owning user "
                f"{self.migration_user!r}: {', '.join(offenders)}. "
                "Set POSTGRES_<ROLE>_USER to a dedicated role; services must never "
                "own or migrate the schema."
            )
        return self

    @model_validator(mode="after")
    def _role_users_must_be_distinct(self) -> Self:
        """Each role needs its own identity, or per-role grants cannot be applied."""
        seen: dict[str, str] = {}
        for role in NON_OWNER_ROLES:
            user = self.credentials(role)[0]
            if user in seen:
                raise ValueError(
                    f"roles {seen[user]!r} and {role.value!r} share the database user "
                    f"{user!r}; per-role privileges require distinct identities"
                )
            seen[user] = role.value
        return self


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    host: str = "localhost"
    port: int = 6379
    db: int = Field(default=0, ge=0, le=15)
    password: SecretStr | None = None
    socket_timeout_seconds: float = Field(default=3.0, gt=0)

    @property
    def dsn(self) -> str:
        """Connection URL. May contain a password; see DatabaseSettings for why this
        is a property rather than a computed field."""
        return str(
            RedisDsn.build(
                scheme="redis",
                host=self.host,
                port=self.port,
                path=str(self.db),
                password=self.password.get_secret_value() if self.password else None,
            )
        )


class CelerySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CELERY_", extra="ignore")

    broker_url: str | None = None
    result_backend: str | None = None
    # The PRD schedules high-risk source comparison at 06:00/12:00/18:00 Beijing time,
    # so Beat must reason in this zone even though every stored timestamp is UTC.
    scheduler_timezone: str = "Asia/Shanghai"
    task_time_limit_seconds: int = Field(default=900, gt=0)
    task_soft_time_limit_seconds: int = Field(default=840, gt=0)

    @model_validator(mode="after")
    def _soft_limit_below_hard_limit(self) -> Self:
        if self.task_soft_time_limit_seconds >= self.task_time_limit_seconds:
            raise ValueError(
                "CELERY_TASK_SOFT_TIME_LIMIT_SECONDS must be below "
                "CELERY_TASK_TIME_LIMIT_SECONDS so tasks can clean up before being killed"
            )
        return self


class OpenAISettings(BaseSettings):
    """Optional OpenAI-assisted claim extraction (stored documents only)."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_", extra="ignore")

    api_key: SecretStr | None = None
    model: str = "gpt-4o-mini"


class ObjectStorageSettings(BaseSettings):
    """S3-compatible evidence store (MinIO locally, S3 in deployed environments)."""

    model_config = SettingsConfigDict(env_prefix="S3_", extra="ignore")

    endpoint_url: str | None = "http://localhost:9000"
    region: str = "us-east-1"
    access_key_id: SecretStr = SecretStr("change-me")
    secret_access_key: SecretStr = SecretStr("change-me")
    evidence_bucket: str = "datahub-evidence"
    use_path_style: bool = True  # required by MinIO; harmless on S3
    connect_timeout_seconds: float = Field(default=3.0, gt=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    debug: bool = False

    app_name: str = "Overseas University DataHub API"
    app_version: str = "0.1.0"
    # Mounted under a version prefix from the outset so the internal API contract can
    # evolve without breaking the console.
    api_prefix: str = "/api/v1"

    log_level: Annotated[str, Field(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")] = "INFO"
    log_format: Literal["json", "console"] = "json"

    cors_allowed_origins: tuple[str, ...] = ("http://localhost:3000",)

    # Readiness gates. Postgres and Redis are always required; object storage is
    # checked only where it is actually provisioned.
    readiness_check_object_storage: bool = False
    readiness_timeout_seconds: float = Field(default=5.0, gt=0)

    # Feature gate for ranking ingestion/display. Defaults off: architecture decision
    # D8 requires rankings to stay dark until an authorised source is licensed.
    rankings_enabled: bool = False

    # --- Reviewer console -------------------------------------------------------
    # The console authenticates a human in a browser, which the CLI never did. Two
    # server-held secrets back that, and neither ever reaches the client bundle.
    #
    # `session_secret` signs preview tokens (see domains/verification/decisions.py).
    # It is a signing key, not a session store: sessions themselves live in
    # `user_session`, hashed, so revocation is a database fact rather than a matter of
    # waiting for a token to expire.
    session_secret: SecretStr = SecretStr("change-me")
    session_cookie_name: str = "datahub_review_session"
    session_ttl_minutes: int = Field(default=480, gt=0)
    #: How long a preview stays applicable. Short on purpose: a preview is a statement
    #: about the database as it was, and the longer it is honoured the less true it is.
    preview_ttl_seconds: int = Field(default=900, gt=0)

    #: Where the normalised document artifacts live. The database holds each document's
    #: hash; the bytes live here. The evidence viewer reads from it and never refetches,
    #: so what a reviewer sees is the artifact the extraction actually produced.
    artifact_root: str = ".artifacts-full"

    openai: OpenAISettings = Field(default_factory=OpenAISettings)

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    object_storage: ObjectStorageSettings = Field(default_factory=ObjectStorageSettings)

    @property
    def session_cookie_secure(self) -> bool:
        """`Secure` on the session cookie everywhere but local development.

        Derived rather than configured: a deployment that could turn this off by
        setting a variable is a deployment that eventually will.
        """
        return self.environment.is_deployed

    @property
    def celery_broker_url(self) -> str:
        return self.celery.broker_url or self.redis.dsn

    @property
    def celery_result_backend(self) -> str:
        return self.celery.result_backend or self.redis.dsn

    @model_validator(mode="after")
    def _reject_placeholder_secrets_when_deployed(self) -> Self:
        if not self.environment.is_deployed:
            return self
        candidates: list[tuple[str, SecretStr]] = [
            (f"POSTGRES_{role.value.upper()}_PASSWORD", self.database.credentials(role)[1])
            for role in NON_OWNER_ROLES
        ]
        # The migration password is checked only where the process actually has one.
        if self.database.migration_password is not None:
            candidates.append(("POSTGRES_MIGRATION_PASSWORD", self.database.migration_password))
        candidates += [
            ("S3_ACCESS_KEY_ID", self.object_storage.access_key_id),
            ("S3_SECRET_ACCESS_KEY", self.object_storage.secret_access_key),
            # A placeholder here would make every preview token forgeable by anyone who
            # has read this file, which is everyone.
            ("SESSION_SECRET", self.session_secret),
        ]
        offenders = [
            name
            for name, secret in candidates
            if secret.get_secret_value().strip().lower() in PLACEHOLDER_SECRETS
        ]
        if offenders:
            raise ValueError(
                f"environment={self.environment} requires real secrets; "
                f"placeholder values found for: {', '.join(offenders)}"
            )
        return self

    @model_validator(mode="after")
    def _reject_debug_in_production(self) -> Self:
        if self.environment is Environment.PRODUCTION and self.debug:
            raise ValueError("DEBUG must be false in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read from the environment exactly once."""
    return Settings()
