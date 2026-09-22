"""S3-compatible evidence store access.

Only a client factory and a health probe at this stage. Snapshot writing, retention
and signed-URL issuance arrive with the acquisition module.

boto3 is synchronous, so the probe is run on a worker thread to avoid blocking the
event loop during a readiness check.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = get_logger(__name__)


def create_client_from_settings(settings: Settings) -> S3Client:
    storage = settings.object_storage
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=storage.endpoint_url,
        region_name=storage.region,
        aws_access_key_id=storage.access_key_id.get_secret_value(),
        aws_secret_access_key=storage.secret_access_key.get_secret_value(),
        config=Config(
            s3={"addressing_style": "path" if storage.use_path_style else "auto"},
            connect_timeout=storage.connect_timeout_seconds,
            read_timeout=storage.connect_timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    return client


@lru_cache(maxsize=1)
def get_object_storage() -> S3Client:
    logger.info("object_storage_client_created")
    return create_client_from_settings(get_settings())


async def check_object_storage(*, timeout_seconds: float) -> None:
    """Raise if the evidence bucket is missing or unreachable."""
    settings = get_settings()
    bucket = settings.object_storage.evidence_bucket

    def _probe() -> None:
        get_object_storage().head_bucket(Bucket=bucket)

    await asyncio.wait_for(asyncio.to_thread(_probe), timeout=timeout_seconds)
