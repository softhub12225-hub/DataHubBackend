"""Shared test fixtures.

Unit tests must run with no Postgres, Redis or MinIO available. Integration tests
(marked ``integration``) require live Postgres and Redis and skip themselves when
those are unreachable, so ``pytest`` is green on a laptop and thorough in CI.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, Settings
from app.db.safety import require_test_database_dsn
from app.main import create_app
from tests.runtime_credentials import (
    ENV_VARS,
    ProvisioningUnavailableError,
    ephemeral_role_passwords,
)


@pytest.fixture(scope="session", autouse=True)
def _isolate_environment() -> Iterator[None]:
    """Stop a developer's ambient environment from reaching the test suite.

    Without this, a exported POSTGRES_HOST or a local .env silently changes what the
    tests assert, and config tests pass or fail depending on whose machine runs them.
    """
    # APP_*_PASSWORD are deliberately NOT managed here: integration tests need
    # them to reach the runtime_role_passwords fixture.
    managed_prefixes = ("POSTGRES_", "REDIS_", "CELERY_", "S3_")
    managed_exact = {
        "ENVIRONMENT",
        "DEBUG",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "RANKINGS_ENABLED",
        "READINESS_CHECK_OBJECT_STORAGE",
        "CORS_ALLOWED_ORIGINS",
        # Step 5C.9. `.env` now carries a real SESSION_SECRET, and without isolating it
        # the placeholder-rejection test passes or fails depending on whether the shell
        # that ran pytest had sourced .env -- which is precisely what this fixture exists
        # to prevent.
        "SESSION_SECRET",
        "SESSION_COOKIE_NAME",
        "SESSION_TTL_MINUTES",
        "PREVIEW_TTL_SECONDS",
        "ARTIFACT_ROOT",
    }
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(managed_prefixes) or key in managed_exact
    }
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture
def settings() -> Settings:
    """Settings for a test process: CI environment, readable logs, no .env file."""
    return Settings(
        environment=Environment.CI,
        log_format="console",
        log_level="WARNING",
        _env_file=None,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """In-process HTTP client. No network socket, no running server."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


# --------------------------------------------------------------------------------
# Integration support
# --------------------------------------------------------------------------------

INTEGRATION_ENV_VARS = ("DATAHUB_TEST_POSTGRES_DSN", "DATAHUB_TEST_REDIS_URL")


def _reachable(coro_factory: object) -> bool:
    try:
        asyncio.run(coro_factory())  # type: ignore[operator]
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """Sync DSN for a live test database, or skip.

    **Every DB-backed test reaches the database through here.** `owner_engine`, `conn`,
    `role_engines` and `runtime_role_passwords` all descend from this fixture, so it is
    the one place a database-safety check cannot be forgotten -- which is why the check
    lives here rather than in an `assert_test_database()` that each test author has to
    remember to call.

    The guard asks the server `SELECT current_database()` rather than reading the DSN or
    an environment variable. In Step 5C.7E the environment was *correct* and still wrong:
    `POSTGRES_DB=datahub` is the right value for operating the real system and the wrong
    one for a test, so no amount of re-reading it would have caught the mistake. See
    `app.db.safety`.

    Unit tests are unaffected: they never request this fixture, so they never connect.
    """
    dsn = os.environ.get("DATAHUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DATAHUB_TEST_POSTGRES_DSN is not set; skipping integration test")
    # Fails closed, before any fixture opens a writable engine against it.
    require_test_database_dsn(dsn, context="DATAHUB_TEST_POSTGRES_DSN")
    return dsn


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("DATAHUB_TEST_REDIS_URL")
    if not url:
        pytest.skip("DATAHUB_TEST_REDIS_URL is not set; skipping integration test")
    return url


@pytest.fixture(scope="session")
def runtime_role_passwords(postgres_dsn: str) -> Iterator[dict[str, str]]:
    """Passwords for the per-role database identities.

    Two ways to get them, and a skip only when neither is possible.

    **Given.** If `APP_API_PASSWORD`, `APP_WORKER_PASSWORD` and
    `APP_PUBLISHER_PASSWORD` are all set, they are used unchanged and nothing is
    altered. This is what CI does, and what a shared database requires.

    **Provisioned.** Otherwise the suite generates one for each role against the local
    server, using the owner connection it already has, and restores the original
    verifier afterwards. See `tests.runtime_credentials` for why this is a real test of
    the runtime roles rather than a simulation of them.

    Fifteen privilege tests used to skip here, on a machine where the real passwords do
    not exist in any recoverable form. A skipped privilege test is not a passing one:
    those grants are what stops a worker writing canonical facts, and "we could not
    check" is the same evidence as "it does not work".
    """
    mapping = {role: os.environ.get(var) for role, var in ENV_VARS.items()}
    supplied = {role: value for role, value in mapping.items() if value}
    absent = tuple(role for role, value in mapping.items() if not value)
    if not absent:
        yield supplied
        return

    # Rotate ONLY the roles with no real password. A supplied credential is left
    # untouched, because rotating one that something else is using breaks it: a test run
    # doing exactly that to `app_api` killed a live `reviewer-enrol-password` mid-handoff
    # in Step 5C.7F. The suite still exercises every role for real -- it just borrows the
    # working credential instead of replacing it.
    missing = sorted(ENV_VARS[role] for role in absent)
    try:
        with ephemeral_role_passwords(postgres_dsn, roles=absent) as generated:
            yield {**supplied, **generated}
    except ProvisioningUnavailableError as exc:
        pytest.skip(
            f"runtime role passwords not set ({', '.join(missing)}) and test-only "
            f"credentials could not be provisioned: {exc}"
        )
