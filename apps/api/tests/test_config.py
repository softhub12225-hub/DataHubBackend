"""Configuration loading and its guardrails."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    CelerySettings,
    DatabaseRole,
    DatabaseSettings,
    Environment,
    RedisSettings,
    Settings,
    get_settings,
)


def _real_db_secrets() -> DatabaseSettings:
    """DatabaseSettings whose every role password passes the placeholder check."""
    return DatabaseSettings(
        migration_password=SecretStr("Bq7!tR2wZx9Lp"),
        api_password=SecretStr("Kd3!vN8pQr2Wz"),
        worker_password=SecretStr("Ty6!mB4jXs9Lc"),
        publisher_password=SecretStr("Hn2!wF7kZd5Rv"),
    )


def test_defaults_are_local_and_safe(settings: Settings) -> None:
    assert settings.environment is Environment.CI
    assert settings.debug is False
    # D8: rankings must default off until an authorised source is licensed.
    assert settings.rankings_enabled is False
    assert settings.api_prefix == "/api/v1"


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "other")
    db = DatabaseSettings()
    assert db.host == "db.internal"
    assert db.port == 6543
    assert db.db == "other"


def test_async_and_sync_dsns_use_the_right_drivers() -> None:
    db = DatabaseSettings(
        host="localhost",
        port=5432,
        db="datahub",
        migration_user="owner",
        migration_password=SecretStr("owner-pw"),
    )
    assert db.async_dsn(DatabaseRole.API).startswith("postgresql+asyncpg://")
    assert db.sync_dsn(DatabaseRole.MIGRATION).startswith("postgresql+psycopg://")
    assert db.async_dsn(DatabaseRole.API).endswith("/datahub")


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def _target(**overrides: Any) -> DatabaseSettings:
    fields: dict[str, Any] = {
        "host": "ep-example.aws.neon.tech",
        "port": 5432,
        "db": "neondb",
        "migration_user": "owner",
        "migration_password": SecretStr("owner-pw"),
    }
    fields.update(overrides)
    return DatabaseSettings(**fields)


def test_each_driver_gets_the_tls_parameter_it_actually_understands() -> None:
    """One setting, two spellings -- and the difference is not cosmetic.

    SQLAlchemy passes an unrecognised query parameter straight through to the driver,
    so a DSN carrying `sslmode` reaches asyncpg's `connect()` as an unexpected keyword
    argument. The result is a TypeError on the first connection, in the deployed
    environment, from configuration that read correctly. psycopg wants libpq's
    `sslmode`; asyncpg wants `ssl`.
    """
    db = _target(sslmode="require")
    assert "sslmode=require" in db.sync_dsn(DatabaseRole.MIGRATION)
    assert "ssl=require" in db.async_dsn(DatabaseRole.API)
    # Neither DSN may carry the other driver's spelling.
    assert "ssl=require" not in db.sync_dsn(DatabaseRole.MIGRATION).replace("sslmode=require", "")
    assert "sslmode" not in db.async_dsn(DatabaseRole.API)


def test_tls_is_unset_by_default_and_the_dsn_says_nothing() -> None:
    """Silence, not a default.

    Defaulting to `require` would make the local Compose stack fail on a loopback
    connection, and it would fail in a way that reads as a credential problem. Every
    managed provider needs it set; a container on a private network does not.
    """
    db = _target()
    assert db.sslmode is None
    assert "ssl" not in db.async_dsn(DatabaseRole.API)
    assert "sslmode" not in db.sync_dsn(DatabaseRole.MIGRATION)


def test_every_libpq_mode_is_accepted() -> None:
    for mode in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
        assert _target(sslmode=mode).sslmode == mode


def test_a_mistyped_mode_is_refused_at_startup_not_at_connect_time() -> None:
    """The typo must fail next to the typo.

    An unvalidated value travels into the DSN and surfaces as a driver error on the
    first query, which is a long way from the environment variable that caused it.
    """
    with pytest.raises(ValidationError) as raised:
        _target(sslmode="requre")
    assert "not a libpq TLS mode" in str(raised.value)


def test_an_empty_value_means_unset_rather_than_invalid() -> None:
    """Compose and Kubernetes can blank an inherited variable but cannot remove it."""
    assert _target(sslmode="").sslmode is None
    assert _target(sslmode="   ").sslmode is None


def test_the_mode_is_normalised_so_one_spelling_reaches_the_driver() -> None:
    assert _target(sslmode="REQUIRE").sslmode == "require"
    assert "ssl=require" in _target(sslmode=" Require ").async_dsn(DatabaseRole.API)


def test_tls_applies_to_every_role_not_just_the_api() -> None:
    """A worker or the publisher connecting without TLS would be refused too."""
    db = _target(sslmode="require")
    for role in (DatabaseRole.API, DatabaseRole.WORKER, DatabaseRole.PUBLISHER):
        assert "ssl=require" in db.async_dsn(role)
    assert "sslmode=require" in db.sync_dsn(DatabaseRole.MIGRATION)


# ---------------------------------------------------------------------------
# Database identity separation
# ---------------------------------------------------------------------------


def test_each_role_gets_its_own_credentials() -> None:
    db = DatabaseSettings(
        migration_user="owner",
        migration_password=SecretStr("owner-pw"),
        api_user="app_api",
        api_password=SecretStr("api-pw"),
        worker_user="app_worker",
        worker_password=SecretStr("worker-pw"),
        publisher_user="app_publisher",
        publisher_password=SecretStr("publisher-pw"),
    )
    assert db.credentials(DatabaseRole.MIGRATION)[0] == "owner"
    assert db.credentials(DatabaseRole.API)[0] == "app_api"
    assert db.credentials(DatabaseRole.WORKER)[0] == "app_worker"
    assert db.credentials(DatabaseRole.PUBLISHER)[0] == "app_publisher"

    # Each DSN carries its own identity, so a misconfigured service cannot borrow
    # another's privileges.
    assert "app_api:api-pw@" in db.async_dsn(DatabaseRole.API)
    assert "app_publisher:publisher-pw@" in db.async_dsn(DatabaseRole.PUBLISHER)
    assert "owner:owner-pw@" in db.sync_dsn(DatabaseRole.MIGRATION)


def test_every_service_role_is_covered_by_credentials() -> None:
    """A new service role must not silently fall through to a default."""
    db = DatabaseSettings()
    for role in (DatabaseRole.API, DatabaseRole.WORKER, DatabaseRole.PUBLISHER):
        user, password = db.credentials(role)
        assert user
        assert password.get_secret_value()


def test_migration_identity_is_absent_unless_configured() -> None:
    """A service container should not be holding the owner's password at all."""
    db = DatabaseSettings()
    assert db.migration_user is None
    with pytest.raises(ValueError, match="migration role is not configured"):
        db.credentials(DatabaseRole.MIGRATION)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_migration_credentials_are_treated_as_absent(blank: str) -> None:
    """Compose can override an inherited variable to empty but cannot remove it."""
    db = DatabaseSettings(migration_user=blank, migration_password=SecretStr(blank))
    assert db.migration_user is None
    assert db.migration_password is None


@pytest.mark.parametrize(
    "field",
    ["api_user", "worker_user", "publisher_user"],
)
def test_services_may_not_connect_as_the_schema_owner(field: str) -> None:
    """The core of the separation: no service may use the migration identity."""
    kwargs: dict[str, Any] = {"migration_user": "datahub_owner", field: "datahub_owner"}
    with pytest.raises(ValidationError, match="schema-owning user"):
        DatabaseSettings(**kwargs)


def test_service_roles_must_be_distinct_from_each_other() -> None:
    with pytest.raises(ValidationError, match="share the database user"):
        DatabaseSettings(api_user="shared", worker_user="shared")


def test_defaults_do_not_use_the_owning_role() -> None:
    """Out-of-the-box configuration must already be separated, not merely able to be."""
    db = DatabaseSettings(migration_user="datahub", migration_password=SecretStr("owner-pw"))
    for role in (DatabaseRole.API, DatabaseRole.WORKER, DatabaseRole.PUBLISHER):
        assert db.credentials(role)[0] != "datahub"


def test_application_engine_refuses_the_migration_role() -> None:
    """Async application pools must never be handed the owning role."""
    from app.core.db import engine_for

    with pytest.raises(ValueError, match="Alembic only"):
        engine_for(DatabaseRole.MIGRATION)


def test_secrets_are_not_exposed_by_repr_or_serialisation() -> None:
    """Regression guard: DSNs must not be pydantic computed fields.

    As computed fields they were included in repr() and model_dump(), which printed
    the database password in clear text and defeated SecretStr.
    """
    db = DatabaseSettings(api_password=SecretStr("super-secret-value"))

    assert "super-secret-value" not in repr(db)
    assert "super-secret-value" not in str(db.model_dump())
    assert "super-secret-value" not in db.model_dump_json()
    assert "super-secret-value" not in str(db.api_password)

    # ...but remain retrievable where genuinely needed.
    assert db.api_password.get_secret_value() == "super-secret-value"
    assert "super-secret-value" in db.async_dsn(DatabaseRole.API)


def test_no_role_password_leaks_when_settings_are_serialised() -> None:
    from app.core.config import ObjectStorageSettings

    resolved = Settings(
        environment=Environment.LOCAL,
        database=DatabaseSettings(
            migration_password=SecretStr("owner-plaintext"),
            api_password=SecretStr("api-plaintext"),
            worker_password=SecretStr("worker-plaintext"),
            publisher_password=SecretStr("publisher-plaintext"),
        ),
        object_storage=ObjectStorageSettings(secret_access_key=SecretStr("s3-plaintext")),
        _env_file=None,
    )
    dumped = resolved.model_dump_json()
    for secret in (
        "owner-plaintext",
        "api-plaintext",
        "worker-plaintext",
        "publisher-plaintext",
        "s3-plaintext",
    ):
        assert secret not in dumped


def test_redis_dsn_includes_database_index() -> None:
    assert RedisSettings(host="cache", port=6379, db=3).dsn == "redis://cache:6379/3"


def test_celery_falls_back_to_redis_when_broker_unset(settings: Settings) -> None:
    assert settings.celery.broker_url is None
    assert settings.celery_broker_url == settings.redis.dsn
    assert settings.celery_result_backend == settings.redis.dsn


def test_celery_soft_time_limit_must_be_below_hard_limit() -> None:
    with pytest.raises(ValidationError, match="must be below"):
        CelerySettings(task_time_limit_seconds=100, task_soft_time_limit_seconds=100)


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_deployed_environments_reject_placeholder_secrets(environment: Environment) -> None:
    with pytest.raises(ValidationError, match="requires real secrets"):
        Settings(
            environment=environment,
            database=DatabaseSettings(api_password=SecretStr("change-me")),
            _env_file=None,
        )


def test_deployed_environment_accepts_real_secrets() -> None:
    from app.core.config import ObjectStorageSettings

    resolved = Settings(
        environment=Environment.STAGING,
        database=_real_db_secrets(),
        object_storage=ObjectStorageSettings(
            access_key_id=SecretStr("AKIAREAL0000EXAMPLE"),
            secret_access_key=SecretStr("Vn4!sQ8eLd2Kf9Mz"),
        ),
        session_secret=SecretStr("k7Qp2Xf9Lm4Rt8Wz"),
        _env_file=None,
    )
    assert resolved.environment.is_deployed is True


def test_debug_is_rejected_in_production() -> None:
    from app.core.config import ObjectStorageSettings

    # Real secrets throughout, so this asserts the debug rule rather than tripping
    # the placeholder-secret validator first.
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        Settings(
            environment=Environment.PRODUCTION,
            debug=True,
            database=_real_db_secrets(),
            object_storage=ObjectStorageSettings(
                access_key_id=SecretStr("AKIAREAL0000EXAMPLE"),
                secret_access_key=SecretStr("Vn4!sQ8eLd2Kf9Mz"),
            ),
            session_secret=SecretStr("k7Qp2Xf9Lm4Rt8Wz"),
            _env_file=None,
        )


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="CHATTY", _env_file=None)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_a_placeholder_session_secret_is_rejected_when_deployed() -> None:
    """The console signs preview tokens with it, so a known value forges confirmations.

    Deliberately asserts the *absence* of an override: the default is `change-me`, and a
    deployment that never sets SESSION_SECRET must fail to start rather than run with a
    signing key printed in the repository.
    """
    from app.core.config import ObjectStorageSettings

    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(
            environment=Environment.STAGING,
            database=_real_db_secrets(),
            object_storage=ObjectStorageSettings(
                access_key_id=SecretStr("AKIAREAL0000EXAMPLE"),
                secret_access_key=SecretStr("Vn4!sQ8eLd2Kf9Mz"),
            ),
            _env_file=None,
        )


def test_the_session_cookie_is_secure_exactly_when_deployed() -> None:
    """Derived from the environment, so no variable can turn it off in production."""
    local = Settings(environment=Environment.LOCAL, _env_file=None)
    assert local.session_cookie_secure is False
    assert Environment.STAGING.is_deployed and Environment.PRODUCTION.is_deployed
