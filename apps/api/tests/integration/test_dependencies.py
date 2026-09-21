"""Connectivity against live Postgres and Redis.

Skipped unless ``DATAHUB_TEST_POSTGRES_DSN`` and ``DATAHUB_TEST_REDIS_URL`` point at
running services (docker compose up, or the local dev stack). CI sets both.
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_postgres_accepts_connections_and_is_version_16_or_newer(
    postgres_dsn: str,
) -> None:
    engine = create_async_engine(postgres_dsn.replace("postgresql+psycopg", "postgresql+asyncpg"))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
            major = (await conn.execute(text("SHOW server_version_num"))).scalar_one()
            assert int(major) >= 160000, f"PostgreSQL 16+ required, got {major}"
    finally:
        await engine.dispose()


async def test_required_extensions_are_installed(postgres_dsn: str) -> None:
    """The bootstrap migration must have created these (ARCHITECTURE.md section 8)."""
    engine = create_async_engine(postgres_dsn.replace("postgresql+psycopg", "postgresql+asyncpg"))
    try:
        async with engine.connect() as conn:
            installed = set(
                (await conn.execute(text("SELECT extname FROM pg_extension"))).scalars()
            )
        assert {"pg_trgm", "btree_gist"} <= installed
    finally:
        await engine.dispose()


async def test_naming_convention_is_applied_to_metadata() -> None:
    """Constraint names must be deterministic before the first domain migration."""
    from app.core.db import Base

    convention = Base.metadata.naming_convention
    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"


async def test_redis_responds_to_ping_and_round_trips_a_value(redis_url: str) -> None:
    client: Redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    try:
        assert await client.ping() is True
        await client.set("datahub:bootstrap:probe", "ok", ex=30)
        assert await client.get("datahub:bootstrap:probe") == "ok"
        await client.delete("datahub:bootstrap:probe")
    finally:
        await client.aclose()


async def test_runtime_roles_exist_and_cannot_create_schema_objects(
    postgres_dsn: str, runtime_role_passwords: dict[str, str]
) -> None:
    """The separation must hold in the database, not only in the settings layer."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import ProgrammingError

    owner = create_engine(postgres_dsn)
    try:
        with owner.connect() as conn:
            roles = set(
                conn.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'app\\_%'")
                ).scalars()
            )
        assert {"app_api", "app_worker", "app_publisher"} <= roles

        with owner.connect() as conn:
            privileged = conn.execute(
                text(
                    "SELECT rolname FROM pg_roles "
                    "WHERE rolname = ANY (ARRAY['app_api','app_worker','app_publisher']) "
                    "AND (rolsuper OR rolcreatedb OR rolcreaterole)"
                )
            ).scalars()
            assert not list(privileged), "runtime roles must hold no cluster privileges"
    finally:
        owner.dispose()

    # And prove it: the API role cannot create a table.
    api_dsn = _dsn_as(postgres_dsn, "app_api", runtime_role_passwords["app_api"])
    api_engine = create_engine(api_dsn, isolation_level="AUTOCOMMIT")
    try:
        with (
            api_engine.connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(text("CREATE TABLE api_role_should_not_be_able_to_do_this (id int)"))
    finally:
        api_engine.dispose()


async def test_readiness_reports_ready_using_the_api_role(
    postgres_dsn: str,
    redis_url: str,
    runtime_role_passwords: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the real probes, the real endpoint, the real services.

    The readiness probe connects as ``app_api``, not as the owner, so this also
    proves the bootstrap migration granted that role SELECT on ``alembic_version``.
    """
    from urllib.parse import urlparse

    from httpx import ASGITransport, AsyncClient

    import app.core.db as db_module
    import app.core.redis as redis_module
    from app.core.config import DatabaseRole, Environment, Settings
    from app.main import create_app

    parsed_pg = urlparse(postgres_dsn)
    parsed_redis = urlparse(redis_url)
    assert parsed_pg.hostname and parsed_pg.username and parsed_pg.password

    settings = Settings(
        environment=Environment.CI,
        log_format="console",
        log_level="WARNING",
        database={  # type: ignore[arg-type]
            "host": parsed_pg.hostname,
            "port": parsed_pg.port or 5432,
            "db": parsed_pg.path.lstrip("/"),
            "migration_user": parsed_pg.username,
            "migration_password": parsed_pg.password,
            "api_user": "app_api",
            "api_password": runtime_role_passwords["app_api"],
            "worker_user": "app_worker",
            "worker_password": runtime_role_passwords["app_worker"],
            "publisher_user": "app_publisher",
            "publisher_password": runtime_role_passwords["app_publisher"],
        },
        redis={  # type: ignore[arg-type]
            "host": parsed_redis.hostname or "localhost",
            "port": parsed_redis.port or 6379,
        },
        _env_file=None,
    )

    # The probe helpers read module-level engines built from get_settings(); point the
    # API-role engine at this test's settings instead.
    monkeypatch.setitem(
        db_module._engines,
        DatabaseRole.API,
        db_module.create_engine_from_settings(settings, DatabaseRole.API),
    )
    monkeypatch.setattr(redis_module, "_client", redis_module.create_client_from_settings(settings))

    try:
        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health/ready")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert all(check["status"] == "ok" for check in body["checks"])
    finally:
        await db_module.dispose_engines()
        await redis_module.close_redis()


def _dsn_as(dsn: str, user: str, password: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(dsn)
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))
