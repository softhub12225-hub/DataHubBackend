"""Liveness and readiness endpoints.

The distinction is deliberate and matters operationally:

* ``/health/live`` answers "is this process functioning?" It touches no dependency.
  A liveness probe that checks Postgres will restart every healthy pod during a
  database blip, turning a partial outage into a total one.
* ``/health/ready`` answers "should this process receive traffic?" It checks every
  required dependency and returns 503 when any is unhealthy.

Route handlers here contain no business logic: each check lives with the subsystem
it probes (``core.db``, ``core.redis``, ``core.object_storage``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.db import check_database
from app.core.errors import ErrorResponse
from app.core.feature_flags import get_feature_flags
from app.core.logging import get_logger
from app.core.object_storage import check_object_storage
from app.core.redis import check_redis

logger = get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


def get_app_settings(request: Request) -> Settings:
    """Resolve settings from the running app, not from the module-level cache.

    ``get_settings()`` is process-wide and cached, which makes the ``settings``
    argument to ``create_app()`` a lie: an app built with overrides would still read
    the global values. Reading from app state keeps the factory authoritative, which
    matters for tests and for any future per-app configuration.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str


class DependencyCheck(BaseModel):
    name: str
    status: Literal["ok", "error"]
    latency_ms: float = Field(description="Wall-clock duration of the probe")
    error: str | None = Field(default=None, description="Failure summary; null when ok")


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str
    version: str
    checks: list[DependencyCheck]


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
    description="Returns 200 whenever the process is running. Touches no dependency.",
)
async def liveness(settings: SettingsDep) -> LivenessResponse:
    return LivenessResponse(service=settings.app_name, version=settings.app_version)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Verifies every required dependency. Returns 503 with per-check detail when "
        "any is unhealthy, so the failing dependency is visible without log access."
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    flags = get_feature_flags(settings)
    timeout = settings.readiness_timeout_seconds

    probes: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("postgres", lambda: check_database(timeout_seconds=timeout)),
        ("redis", lambda: check_redis(timeout_seconds=timeout)),
    ]
    if flags.readiness_checks_object_storage:
        probes.append(("object_storage", lambda: check_object_storage(timeout_seconds=timeout)))

    # Probes are independent; run them concurrently so a slow dependency does not
    # add its latency to the others and push the whole probe past its own deadline.
    checks = await asyncio.gather(*(_run_probe(name, probe) for name, probe in probes))

    healthy = all(check.status == "ok" for check in checks)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning(
            "readiness_failed",
            failed=[check.name for check in checks if check.status == "error"],
        )

    return ReadinessResponse(
        status="ready" if healthy else "not_ready",
        service=settings.app_name,
        version=settings.app_version,
        checks=list(checks),
    )


async def _run_probe(name: str, probe: Callable[[], Awaitable[None]]) -> DependencyCheck:
    started = time.perf_counter()
    try:
        await probe()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.warning("dependency_check_failed", dependency=name, error_type=type(exc).__name__)
        return DependencyCheck(
            name=name,
            status="error",
            latency_ms=round(elapsed_ms, 2),
            # Type plus a truncated message: enough to diagnose, short enough that a
            # driver error cannot dump connection details into a public response.
            error=f"{type(exc).__name__}: {exc}"[:200] if str(exc) else type(exc).__name__,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    return DependencyCheck(name=name, status="ok", latency_ms=round(elapsed_ms, 2))


# Referenced so the error envelope is emitted into the OpenAPI schema and therefore
# into the generated TypeScript types, which the web client depends on.
__all__ = ["ErrorResponse", "router"]
