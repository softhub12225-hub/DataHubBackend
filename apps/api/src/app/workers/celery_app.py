"""Celery application.

Runtime roles share this module (ARCHITECTURE.md D11): ``worker`` executes tasks,
``beat`` schedules them. The queues below are declared now because task routing has
to exist before the first task is written -- browser work must never land on the
default worker, which has neither Playwright nor the memory budget for it.

No domain tasks are registered yet. ``ping`` exists solely to verify that broker
round-trips work.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from app.core.clock import SCHEDULER_TIMEZONE
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)
settings = get_settings()

celery_app = Celery(
    "datahub",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Store UTC, schedule in Beijing time: the PRD's 06:00/12:00/18:00 comparison
    # windows and 22:00 review deadline are local-time business rules.
    enable_utc=True,
    timezone=str(SCHEDULER_TIMEZONE),
    # Acknowledge after completion so a worker crash redelivers the task rather than
    # silently dropping a scheduled source comparison.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Fetch-bound tasks are long and uneven; prefetching starves other workers.
    worker_prefetch_multiplier=1,
    task_time_limit=settings.celery.task_time_limit_seconds,
    task_soft_time_limit=settings.celery.task_soft_time_limit_seconds,
    task_default_queue="default",
    task_queues_late_binding=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
    worker_send_task_events=True,
)

# Declared ahead of the tasks that will use them, so routing is never retrofitted.
celery_app.conf.task_routes = {
    "app.workers.tasks.crawl.*": {"queue": "crawl"},
    "app.workers.tasks.browser.*": {"queue": "browser"},
    "app.workers.tasks.extract.*": {"queue": "extract"},
    "app.workers.tasks.detect.*": {"queue": "detect"},
    "app.workers.tasks.sla.*": {"queue": "sla"},
    "app.workers.tasks.notify.*": {"queue": "notify"},
    "app.workers.tasks.maint.*": {"queue": "maint"},
}

# Empty until there is something to schedule. Beat runs with this and does nothing,
# which is the correct behaviour for a bootstrap.
celery_app.conf.beat_schedule = {}


@setup_logging.connect
def _configure_celery_logging(**_kwargs: Any) -> None:
    """Use the application's structured logging instead of Celery's own format."""
    configure_logging(level=settings.log_level, fmt=settings.log_format)


@celery_app.task(name="app.workers.ping")
def ping() -> str:
    """Broker/worker round-trip check. Used by the runbook, not by the application."""
    logger.info("celery_ping")
    return "pong"
