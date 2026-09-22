"""Shared column patterns.

Kept deliberately small: a mixin that saves three lines but hides a foreign key is a
bad trade in a schema whose whole point is explicitness.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column

#: Canonical-id slug shape. Lowercase, digits, single hyphens, no leading/trailing
#: hyphen — so an id is safe in a URL and in a filesystem path, and two ids cannot
#: differ only by case.
CANONICAL_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID primary key. Identity only -- never an ordering or integrity mechanism.

    **Currently UUIDv4**, via `gen_random_uuid()`. Step 4 does not need to change
    that, and deliberately will not: UUIDv7 would buy index locality on
    insert-heavy tables, which is a performance optimisation to make when there is
    enough data to measure it.

    What a UUID is explicitly *not* used for is sequence. Audit-chain order is
    modelled by `audit_log.seq`, assigned under a row lock, precisely because a
    UUID -- v4 or v7 -- cannot define a deterministic predecessor across concurrent
    writers. Adopting v7 later must not change that.
    """
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


@declarative_mixin
class TimestampedMixin:
    """`created_at` / `updated_at` for **mutable** tables only.

    Immutable history tables must not have `updated_at`: the column would imply a
    mutation that privileges and triggers forbid.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@declarative_mixin
class RecordedAtMixin:
    """`recorded_at` for **append-only** tables.

    Named for what it means — when the row was written — rather than `created_at`,
    which invites a matching `updated_at`.
    """

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def canonical_id_column(*, length: int = 160) -> Mapped[str]:
    """Immutable, human-readable public identifier (architecture B6).

    Immutability is enforced by trigger rather than by a CHECK, because a CHECK
    cannot see the previous value. Renames are handled by `entity_alias`; real-entity
    replacement by `entity_relationship`.
    """
    return mapped_column(String(length), nullable=False, unique=True)


__all__ = [
    "CANONICAL_ID_PATTERN",
    "RecordedAtMixin",
    "TimestampedMixin",
    "canonical_id_column",
    "uuid_pk",
]
