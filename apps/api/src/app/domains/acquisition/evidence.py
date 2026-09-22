"""Resolving which evidence is current (Step 5B.1 section 23).

WHY THIS HAS TO EXIST BEFORE STEP 5C
====================================
A 304 creates a `fetch_run` and **no snapshot**, because no bytes arrived. That is the
honest record, and it leaves a question every later consumer will ask: *what is the
current evidence for this source?*

Answering it by "the most recent snapshot" is wrong in a way that only shows up over
time. A page checked weekly and unchanged for six months has one snapshot from six
months ago and twenty-five `UNCHANGED` runs — and the naive answer silently reports
six-month-old evidence as though nobody had looked since, which is precisely the
distinction this system exists to preserve.

So `latest_effective_evidence` returns both: the snapshot whose bytes are current,
**and** when we last confirmed they still were. An extractor reads the first; a
freshness check and a reviewer read the second.

The alternative — writing a synthetic snapshot for each 304 — was rejected. It would
make `snapshot` mean "we saw these bytes" in some rows and "we were told these bytes
are still there" in others, and no later reader could tell which.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text


@dataclass(frozen=True, slots=True)
class EffectiveEvidence:
    """The current bytes for a source, and when they were last confirmed."""

    source_id: uuid.UUID
    #: The observation that actually carried these bytes. None when the source has
    #: never returned a body -- a source whose only runs are errors has no evidence,
    #: and saying so is better than returning something plausible.
    snapshot_id: uuid.UUID | None
    content_hash: str | None
    storage_key: str | None
    content_type: str | None
    #: When those bytes were *seen*.
    observed_at: datetime | None
    #: When we last confirmed they are still current -- the 304's `started_at` if the
    #: most recent successful run was one, otherwise the same as `observed_at`.
    confirmed_at: datetime | None
    #: How many consecutive 304s sit on top of the body-bearing observation. A page
    #: unchanged for six months reads 25 here, and that is a fact worth surfacing.
    unchanged_runs_since: int
    #: True when the latest successful run was a 304 rather than a transfer.
    resolved_through_unchanged: bool
    effective_url: str | None
    requested_url: str | None

    @property
    def has_evidence(self) -> bool:
        return self.snapshot_id is not None


def latest_effective_evidence(connection: Connection, source_id: uuid.UUID) -> EffectiveEvidence:
    """The snapshot whose bytes are current for `source_id`, resolving through 304s.

    Walks back from the most recent successful run:

    * latest is a 200 -> that run's snapshot;
    * latest is a 304 -> the blob it names, and the snapshot that carried it;
    * several consecutive 304s -> still the last body-bearing snapshot, with the
      count and the most recent confirmation time.

    No synthetic snapshot is created for a 304, here or anywhere.
    """
    row = connection.execute(
        text(
            """
            WITH successful AS (
                SELECT r.id, r.status::text AS status, r.started_at,
                       r.unchanged_content_hash,
                       row_number() OVER (ORDER BY r.started_at DESC, r.id DESC) AS recency
                  FROM fetch_run r
                 WHERE r.source_id = :source
                   AND r.status IN ('OK', 'UNCHANGED')
            ),
            latest AS (SELECT * FROM successful WHERE recency = 1),
            -- Consecutive 304s sitting on top: the UNCHANGED runs more recent than
            -- the newest run that actually carried bytes. `min(recency)` is that
            -- run's position, so everything strictly above it is the streak.
            unchanged_streak AS (
                SELECT count(*) AS runs
                  FROM successful s
                 WHERE s.status = 'UNCHANGED'
                   AND s.recency < COALESCE(
                           (SELECT min(recency) FROM successful WHERE status = 'OK'),
                           2147483647
                       )
            ),
            -- The body-bearing observation the latest successful run resolves to.
            body AS (
                SELECT sn.id, sn.content_hash, sn.observed_at, sn.effective_url,
                       sn.requested_url, sn.content_type
                  FROM snapshot sn
                 WHERE sn.source_id = :source
                   AND (
                        -- a 304 names the blob it confirmed
                        sn.content_hash = (SELECT unchanged_content_hash FROM latest)
                        -- otherwise simply the most recent snapshot
                        OR (SELECT unchanged_content_hash FROM latest) IS NULL
                   )
                 ORDER BY sn.observed_at DESC, sn.id DESC
                 LIMIT 1
            )
            SELECT body.id            AS snapshot_id,
                   body.content_hash,
                   body.observed_at,
                   body.effective_url,
                   body.requested_url,
                   body.content_type,
                   blob.storage_key,
                   latest.status      AS latest_status,
                   latest.started_at  AS confirmed_at,
                   COALESCE(unchanged_streak.runs, 0) AS unchanged_runs
              FROM latest
              LEFT JOIN body ON TRUE
              LEFT JOIN content_blob blob ON blob.content_hash = body.content_hash
              LEFT JOIN unchanged_streak ON TRUE
            """
        ),
        {"source": source_id},
    ).one_or_none()

    if row is None or row.snapshot_id is None:
        return EffectiveEvidence(
            source_id=source_id,
            snapshot_id=None,
            content_hash=None,
            storage_key=None,
            content_type=None,
            observed_at=None,
            confirmed_at=None,
            unchanged_runs_since=0,
            resolved_through_unchanged=False,
            effective_url=None,
            requested_url=None,
        )

    through_304 = row.latest_status == "UNCHANGED"
    return EffectiveEvidence(
        source_id=source_id,
        snapshot_id=row.snapshot_id,
        content_hash=row.content_hash,
        storage_key=row.storage_key,
        content_type=row.content_type,
        observed_at=row.observed_at,
        confirmed_at=row.confirmed_at,
        unchanged_runs_since=int(row.unchanged_runs) if through_304 else 0,
        resolved_through_unchanged=through_304,
        effective_url=row.effective_url,
        requested_url=row.requested_url,
    )


__all__ = ["EffectiveEvidence", "latest_effective_evidence"]
