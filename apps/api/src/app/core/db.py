"""Database engine, session factory and the declarative base.

The engine is created lazily on first use so that importing the application does not
require a reachable database -- required by ``scripts/export_openapi.py`` and by the
unit test suite.

No domain models are defined here or anywhere else yet. ``Base`` exists so that the
metadata naming convention is fixed *before* the first domain migration is written:
retrofitting a convention later means renaming every constraint in a live database,
and ARCHITECTURE.md section 8 depends on constraints being addressable by name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import DatabaseRole, Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Deterministic constraint names. Without these, Alembic autogenerate emits
# database-assigned names and constraints cannot be referenced reliably in later
# migrations or in tests that assert an invariant exists.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base for all future ORM models."""

    metadata = metadata


# One engine per database role, created on first use. Keyed by role so a process
# that legitimately needs two identities (an API request handler and the publication
# transaction) does not share a connection pool between privilege levels.
_engines: dict[DatabaseRole, AsyncEngine] = {}
_sessionmakers: dict[DatabaseRole, async_sessionmaker[AsyncSession]] = {}


def create_engine_from_settings(settings: Settings, role: DatabaseRole) -> AsyncEngine:
    db = settings.database
    return create_async_engine(
        db.async_dsn(role),
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        pool_timeout=db.pool_timeout_seconds,
        pool_recycle=db.pool_recycle_seconds,
        pool_pre_ping=True,
        echo=False,
        connect_args={
            # A query that runs longer than this is a bug, not a slow query. Failing
            # fast keeps a single pathological statement from exhausting the pool.
            "server_settings": {
                "statement_timeout": str(db.statement_timeout_ms),
                # Role in the application_name so pg_stat_activity attributes a
                # session to a privilege level, not just to "the app".
                "application_name": f"datahub-{role.value}",
            }
        },
    )


def engine_for(role: DatabaseRole) -> AsyncEngine:
    """Engine bound to ``role``, created on first use.

    ``DatabaseRole.MIGRATION`` is rejected: migrations run synchronously through
    Alembic, and handing the owning role to the async application pool is exactly
    the fallback this design exists to prevent.
    """
    if role is DatabaseRole.MIGRATION:
        raise ValueError(
            "the migration role is for Alembic only; application code must connect "
            "as api, worker or publisher"
        )
    engine = _engines.get(role)
    if engine is None:
        engine = create_engine_from_settings(get_settings(), role)
        _engines[role] = engine
        logger.info("database_engine_created", role=role.value)
    return engine


def sessionmaker_for(role: DatabaseRole) -> async_sessionmaker[AsyncSession]:
    factory = _sessionmakers.get(role)
    if factory is None:
        factory = async_sessionmaker(
            bind=engine_for(role),
            expire_on_commit=False,
            autoflush=False,
        )
        _sessionmakers[role] = factory
    return factory


async def session_for(role: DatabaseRole) -> AsyncIterator[AsyncSession]:
    """Yield a session for ``role`` that rolls back on error.

    Commits are the caller's responsibility: the publication transaction in
    particular must control its own boundary (ARCHITECTURE.md section 7).
    """
    async with sessionmaker_for(role)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_api_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a read-path session with the API role's privileges."""
    async for session in session_for(DatabaseRole.API):
        yield session


async def get_publisher_session() -> AsyncIterator[AsyncSession]:
    """Session for the publication transaction, and for nothing else.

    Deliberately not wired to a FastAPI dependency: canonical writes go through
    ``publication.publish()`` (Step 3), which acquires this itself. Exposing it as a
    request dependency would invite a handler to write canonical state directly.
    """
    async for session in session_for(DatabaseRole.PUBLISHER):
        yield session


async def check_database(*, timeout_seconds: float, role: DatabaseRole = DatabaseRole.API) -> None:
    """Raise if Postgres is unreachable or migrations have not been applied.

    Probes as ``role`` rather than as the owner, so readiness fails when the role's
    own grants are missing instead of passing on borrowed privileges.

    Checking ``alembic_version`` as well as connectivity is deliberate: a pod that
    can reach an un-migrated database is not ready to serve, and reporting it as
    ready is how a half-deployed release starts returning 500s. The bootstrap
    migration grants each runtime role SELECT on that table for exactly this reason.
    """
    import asyncio

    async def _probe() -> None:
        async with engine_for(role).connect() as conn:
            await conn.execute(text("SELECT 1"))
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            if revision is None:
                raise RuntimeError("alembic_version is empty: migrations have not been applied")

    await asyncio.wait_for(_probe(), timeout=timeout_seconds)


async def dispose_engines() -> None:
    """Close every pooled connection on shutdown."""
    for role, engine in list(_engines.items()):
        await engine.dispose()
        logger.info("database_engine_disposed", role=role.value)
    _engines.clear()
    _sessionmakers.clear()
