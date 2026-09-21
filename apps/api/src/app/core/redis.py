"""Redis client for the API process.

Redis is the Celery broker, the crawler's per-domain rate-limit store and the
short-TTL read cache. The API only needs a connection pool and a health probe at
this stage.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None


def create_client_from_settings(settings: Settings) -> Redis:
    client: Redis = Redis.from_url(
        settings.redis.dsn,
        decode_responses=True,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_timeout_seconds,
        health_check_interval=30,
    )
    return client


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = create_client_from_settings(get_settings())
        logger.info("redis_client_created")
    return _client


async def check_redis(*, timeout_seconds: float) -> None:
    """Raise if Redis does not answer PING within the timeout."""
    await asyncio.wait_for(get_redis().ping(), timeout=timeout_seconds)


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        logger.info("redis_client_closed")
    _client = None
