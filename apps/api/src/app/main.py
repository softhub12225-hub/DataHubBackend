"""FastAPI application factory.

Composition only: configuration, logging, middleware, error handlers, routers.
No business logic, and no I/O at import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, review
from app.core.config import Environment, Settings, get_settings
from app.core.db import dispose_engines
from app.core.errors import ErrorResponse, register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging, get_logger
from app.core.redis import close_redis
from app.core.security import configure_security

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "application_starting",
        environment=settings.environment.value,
        version=settings.app_version,
    )
    # Dependencies are intentionally NOT connected here. Startup must not fail
    # because Postgres is briefly unavailable; readiness reports that instead, and
    # the orchestrator withholds traffic until the probe passes.
    yield
    logger.info("application_stopping")
    await dispose_engines()
    await close_redis()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        # Interactive docs are useful internally but are not exposed in production.
        docs_url=None if settings.environment is Environment.PRODUCTION else "/docs",
        redoc_url=None,
        openapi_url=(None if settings.environment is Environment.PRODUCTION else "/openapi.json"),
        responses={
            400: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
    )
    app.state.settings = settings

    # Order matters: request context is outermost so every downstream log line and
    # every error envelope carries the request id.
    app.add_middleware(RequestContextMiddleware)
    configure_security(app, settings)
    register_exception_handlers(app)

    # Health endpoints sit outside the versioned prefix: probe URLs are
    # infrastructure contracts and must not move when the API version does.
    app.include_router(health.router)

    # The reviewer console. Versioned, because it is an internal API contract the
    # console depends on and is free to evolve, unlike the probe URLs above.
    app.include_router(review.router, prefix=settings.api_prefix)

    return app


app = create_app()
