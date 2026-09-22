"""Content-addressed evidence storage (Step 5B sections 15-16).

THE ORDERING RULE, AND WHY IT IS THAT WAY ROUND
===============================================
Bytes go to object storage **before** the database transaction that references them::

    fetch bytes -> sha256 -> ensure the object exists -> BEGIN
                                                            content_blob
                                                            fetch_run
                                                            snapshot
                                                            attempt finalisation
                                                         COMMIT

The two failure modes are not symmetrical, and that asymmetry decides the order.

*Object written, transaction fails* leaves an object nothing references: wasted bytes,
reconcilable later, and harmless in the meantime. *Transaction commits, object write
fails* leaves a `content_blob` row pointing at nothing — a published fact whose
evidence cannot be produced, which is the one outcome this whole system exists to
prevent (C12). So the recoverable failure is the one we allow.

ORPHAN RECONCILIATION
=====================
An orphan is an object under `evidence/` whose hash has no `content_blob` row. They
are safe to delete, but only with a generous age cut-off: an object is written seconds
before its row, so anything young may simply be a fetch in flight. `find_orphans`
takes a `min_age`, and the operational default is 24 hours. Deletion is a deliberate
operator action -- there is no automatic GC, because the failure mode of an
over-eager sweeper is deleting evidence, and nobody notices until someone asks for it.

The reverse check -- a `content_blob` whose object is missing -- is the one that
matters, and `find_missing_objects` exists so it can be run as an audit rather than
discovered by a reviewer.

DEDUPLICATION LIVES HERE AND ONLY HERE
======================================
The key is the hash, so identical bytes written twice occupy one object. That is
storage dedup and nothing more: each fetch still writes its own `snapshot`, because
the record of having looked is not the same as the bytes we saw.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.clock import utcnow
from app.core.config import EvidenceBackend, Settings
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client

logger = get_logger(__name__)

#: Prefix for raw fetched bodies. Fanned out by the first two hex pairs so a bucket
#: listing stays usable at a few hundred thousand objects.
EVIDENCE_PREFIX = "evidence"

#: How old an unreferenced object must be before it is even a candidate for deletion.
#: An object is written seconds before its row; anything younger is probably in flight.
ORPHAN_MIN_AGE = timedelta(hours=24)


def storage_key_for(content_hash: str, *, prefix: str = EVIDENCE_PREFIX) -> str:
    """Where an object of this hash lives.

    Fanned out two levels so no directory holds hundreds of thousands of entries.

    `prefix` namespaces whole classes of object. Raw fetched bytes go under
    `evidence/`; Step 5C.1's derived documents go under `derived/`. Separate prefixes
    are what make "extraction never overwrites raw evidence" a structural property
    rather than a convention -- the two could only collide by sharing a prefix, and
    they do not.
    """
    return f"{prefix}/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Where bytes ended up, and whether this call is what put them there."""

    content_hash: str
    storage_key: str
    byte_size: int
    content_type: str | None
    #: False when an object with this key already existed. Not an error: two
    #: observations of identical bytes is the normal case.
    newly_written: bool


class EvidenceStore(ABC):
    """Where raw fetched bytes live. Never PostgreSQL."""

    @abstractmethod
    def put(self, payload: bytes, *, content_type: str | None = None) -> StoredObject:
        """Store bytes under their content hash, returning where they went."""

    @abstractmethod
    def get(self, content_hash: str) -> bytes:
        """Retrieve stored bytes, or raise `KeyError`."""

    @abstractmethod
    def exists(self, content_hash: str) -> bool: ...

    @abstractmethod
    def list_keys(self, *, prefix: str | None = None) -> list[tuple[str, datetime]]:
        """Every key under `prefix` with its last-modified time.

        `None` means this store's own prefix, which is what a caller almost always
        wants -- asking a derived-artifact store to list raw evidence is a mistake
        worth making awkward.
        """

    @abstractmethod
    def delete(self, content_hash: str) -> None: ...


class FilesystemEvidenceStore(EvidenceStore):
    """A local directory. For tests and for a single-node deployment.

    Exists because MinIO needs Docker and Docker is not available on this machine, so
    without it every storage test would be skipped rather than run. It implements the
    same contract, so the tests exercise the real ordering and the real dedup — what
    it does not exercise is S3 itself, which stays an open integration gate.
    """

    def __init__(self, root: Path | str, *, prefix: str = EVIDENCE_PREFIX) -> None:
        self.root = Path(root)
        #: Namespaces this store's keys. Raw fetched bytes use `evidence/`; Step
        #: 5C.1's derived documents use `derived/`, so the two cannot overwrite one
        #: another even sharing a root.
        self.prefix = prefix
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, content_hash: str) -> Path:
        return self.root / storage_key_for(content_hash, prefix=self.prefix)

    def put(self, payload: bytes, *, content_type: str | None = None) -> StoredObject:
        content_hash = hash_bytes(payload)
        path = self._path(content_hash)
        if path.exists():
            return StoredObject(
                content_hash=content_hash,
                storage_key=storage_key_for(content_hash, prefix=self.prefix),
                byte_size=len(payload),
                content_type=content_type,
                newly_written=False,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary name and renamed, so a crash mid-write cannot leave
        # a truncated object under a hash that claims to describe it.
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(payload)
        temporary.replace(path)
        return StoredObject(
            content_hash=content_hash,
            storage_key=storage_key_for(content_hash, prefix=self.prefix),
            byte_size=len(payload),
            content_type=content_type,
            newly_written=True,
        )

    def get(self, content_hash: str) -> bytes:
        path = self._path(content_hash)
        if not path.exists():
            raise KeyError(content_hash)
        return path.read_bytes()

    def exists(self, content_hash: str) -> bool:
        return self._path(content_hash).exists()

    def list_keys(self, *, prefix: str | None = None) -> list[tuple[str, datetime]]:
        base = self.root / (prefix or self.prefix)
        if not base.exists():
            return []
        from datetime import UTC

        return [
            (
                str(path.relative_to(self.root)).replace("\\", "/"),
                datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            )
            for path in base.rglob("*")
            if path.is_file() and path.suffix != ".partial"
        ]

    def delete(self, content_hash: str) -> None:
        self._path(content_hash).unlink(missing_ok=True)


class S3EvidenceStore(EvidenceStore):
    """S3-compatible object storage. The deployed store.

    **Verified against a real endpoint.** This said it had never opened a connection,
    which was true until it was driven against an IDrive e2 bucket (us-west-2) through
    `build_evidence_store`: put, dedup-on-second-put, exists, get with a sha256
    comparison, list_keys and delete all behaved as the contract requires.

    "S3" here means the S3 API, not AWS specifically. It uses only put_object,
    get_object, head_object, list_objects_v2 and delete_object -- no storage classes,
    no ACLs, no KMS -- so any provider implementing that core works. The endpoint,
    region and path-style addressing come from `ObjectStorageSettings`.

    MinIO specifically is still unexercised, since it needs Docker and Docker is not
    available on the development machine; `make verify-docker` remains that gate.
    """

    def __init__(self, client: S3Client, bucket: str, *, prefix: str = EVIDENCE_PREFIX) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def put(self, payload: bytes, *, content_type: str | None = None) -> StoredObject:
        content_hash = hash_bytes(payload)
        key = storage_key_for(content_hash, prefix=self.prefix)
        if self.exists(content_hash):
            return StoredObject(
                content_hash=content_hash,
                storage_key=key,
                byte_size=len(payload),
                content_type=content_type,
                newly_written=False,
            )
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=payload, **extra)  # type: ignore[arg-type]
        return StoredObject(
            content_hash=content_hash,
            storage_key=key,
            byte_size=len(payload),
            content_type=content_type,
            newly_written=True,
        )

    def get(self, content_hash: str) -> bytes:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=storage_key_for(content_hash, prefix=self.prefix)
            )
        except self.client.exceptions.NoSuchKey as exc:
            raise KeyError(content_hash) from exc
        body: bytes = response["Body"].read()
        return body

    def exists(self, content_hash: str) -> bool:
        try:
            self.client.head_object(
                Bucket=self.bucket, Key=storage_key_for(content_hash, prefix=self.prefix)
            )
        except Exception:  # botocore raises a generated ClientError for a 404
            return False
        return True

    def list_keys(self, *, prefix: str | None = None) -> list[tuple[str, datetime]]:
        keys: list[tuple[str, datetime]] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix or self.prefix):
            for item in page.get("Contents", []):
                keys.append((item["Key"], item["LastModified"]))
        return keys

    def delete(self, content_hash: str) -> None:
        self.client.delete_object(
            Bucket=self.bucket, Key=storage_key_for(content_hash, prefix=self.prefix)
        )


def find_orphans(
    store: EvidenceStore, known_hashes: set[str], *, min_age: timedelta = ORPHAN_MIN_AGE
) -> list[str]:
    """Stored objects that no `content_blob` row references, and are old enough.

    The age floor is what makes this safe to run while workers are active: an object
    is written seconds before the transaction that references it, so a young orphan is
    very likely a fetch in flight rather than a failure.

    Returns keys rather than deleting them. Deletion is an operator's decision, and an
    over-eager sweeper here deletes evidence that nobody notices is gone until someone
    asks to see it.
    """
    cutoff = utcnow() - min_age
    orphans: list[str] = []
    for key, modified in store.list_keys():
        content_hash = key.rsplit("/", 1)[-1]
        if content_hash in known_hashes:
            continue
        if modified > cutoff:
            continue
        orphans.append(key)
    return orphans


def find_missing_objects(store: EvidenceStore, known_hashes: set[str]) -> list[str]:
    """`content_blob` rows whose object is absent. This is the serious direction.

    A missing object means a published fact's evidence cannot be produced. The
    ordering rule is designed to make this impossible; this exists so the claim can be
    audited rather than assumed.
    """
    return sorted(h for h in known_hashes if not store.exists(h))


def build_evidence_store(
    settings: Settings,
    *,
    prefix: str = EVIDENCE_PREFIX,
    local_root: Path | str | None = None,
) -> EvidenceStore:
    """The evidence store this process should write to.

    WHY A FACTORY AND NOT A CONSTRUCTOR AT EACH CALL SITE
    =====================================================
    There were six call sites, and every one of them built `FilesystemEvidenceStore`
    directly. That made `S3EvidenceStore` unreachable: the S3 settings existed, could
    be filled in correctly, and would change nothing -- which is the worst kind of
    configuration, because it looks applied. One factory means selecting the backend
    is a single decision made in one place, and adding a seventh caller cannot
    silently opt out of it.

    `local_root` lets a caller override where the filesystem backend writes -- the
    smoke script and the acquisition CLI both take an evidence root argument. It is
    ignored when the backend is S3, where the bucket is the root.

    `prefix` namespaces the keys: raw fetched bytes under `evidence/`, derived
    documents under `derived/`, so the two cannot overwrite one another.
    """
    if settings.evidence_backend is EvidenceBackend.S3:
        from app.core.object_storage import create_client_from_settings

        return S3EvidenceStore(
            create_client_from_settings(settings),
            settings.object_storage.evidence_bucket,
            prefix=prefix,
        )
    return FilesystemEvidenceStore(local_root or settings.artifact_root, prefix=prefix)


__all__ = [
    "EVIDENCE_PREFIX",
    "ORPHAN_MIN_AGE",
    "EvidenceStore",
    "FilesystemEvidenceStore",
    "S3EvidenceStore",
    "StoredObject",
    "build_evidence_store",
    "find_missing_objects",
    "find_orphans",
    "hash_bytes",
    "storage_key_for",
]
