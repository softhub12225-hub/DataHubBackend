"""Liveness and readiness behaviour.

Dependency probes are patched rather than provisioned: the point of these tests is
the endpoint's contract and failure semantics, not whether Postgres works. Live
dependencies are exercised in ``tests/integration``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import Environment, Settings
from app.core.logging import REQUEST_ID_HEADER


async def test_liveness_is_ok(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


async def test_liveness_does_not_touch_dependencies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A liveness probe that checks Postgres restarts healthy pods during a blip."""

    def _explode(**_kwargs: object) -> None:
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr("app.api.health.check_database", _explode)
    monkeypatch.setattr("app.api.health.check_redis", _explode)

    assert (await client.get("/health/live")).status_code == 200


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.headers[REQUEST_ID_HEADER]


async def test_inbound_request_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={REQUEST_ID_HEADER: "trace-abc_123"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc_123"


async def test_malformed_inbound_request_id_is_replaced(client: AsyncClient) -> None:
    """Client-controlled strings reach the logs; they must be sanitised."""
    response = await client.get("/health/live", headers={REQUEST_ID_HEADER: "bad id\ninjected"})
    assert response.headers[REQUEST_ID_HEADER] != "bad id\ninjected"
    assert "\n" not in response.headers[REQUEST_ID_HEADER]


async def test_security_headers_are_present(client: AsyncClient) -> None:
    headers = (await client.get("/health/live")).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


async def _patch_probes(
    monkeypatch: pytest.MonkeyPatch, *, database_ok: bool, redis_ok: bool
) -> None:
    async def ok(**_kwargs: object) -> None:
        return None

    async def fail(**_kwargs: object) -> None:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr("app.api.health.check_database", ok if database_ok else fail)
    monkeypatch.setattr("app.api.health.check_redis", ok if redis_ok else fail)


async def test_readiness_is_ready_when_dependencies_are_healthy(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patch_probes(monkeypatch, database_ok=True, redis_ok=True)
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert {check["name"] for check in body["checks"]} == {"postgres", "redis"}
    assert all(check["status"] == "ok" for check in body["checks"])
    assert all(check["error"] is None for check in body["checks"])


async def test_readiness_returns_503_and_names_the_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patch_probes(monkeypatch, database_ok=False, redis_ok=True)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    failed = {check["name"]: check for check in body["checks"] if check["status"] == "error"}
    assert set(failed) == {"postgres"}
    assert "ConnectionRefusedError" in failed["postgres"]["error"]


async def test_readiness_reports_all_failures_not_just_the_first(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patch_probes(monkeypatch, database_ok=False, redis_ok=False)
    body = (await client.get("/health/ready")).json()
    assert {c["name"] for c in body["checks"] if c["status"] == "error"} == {"postgres", "redis"}


async def test_object_storage_is_checked_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    settings = Settings(
        environment=Environment.CI,
        log_format="console",
        log_level="WARNING",
        readiness_check_object_storage=True,
        _env_file=None,
    )
    await _patch_probes(monkeypatch, database_ok=True, redis_ok=True)

    async def ok(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.api.health.check_object_storage", ok)

    transport_app = create_app(settings)
    async with AsyncClient(
        transport=__import__("httpx").ASGITransport(app=transport_app),
        base_url="http://testserver",
    ) as scoped_client:
        body = (await scoped_client.get("/health/ready")).json()

    assert {check["name"] for check in body["checks"]} == {
        "postgres",
        "redis",
        "object_storage",
    }


async def test_readiness_error_detail_is_truncated(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver error must not dump unbounded internals into an HTTP response."""

    async def verbose_failure(**_kwargs: object) -> None:
        raise RuntimeError("x" * 5000)

    async def ok(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.api.health.check_database", verbose_failure)
    monkeypatch.setattr("app.api.health.check_redis", ok)

    body = (await client.get("/health/ready")).json()
    error = next(check["error"] for check in body["checks"] if check["name"] == "postgres")
    assert len(error) <= 200
