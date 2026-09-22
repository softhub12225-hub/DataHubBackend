# Evidence object storage

MinIO locally, S3 (or another S3-compatible service) in deployed environments. This
is where source snapshots live: the raw HTML or PDF, the rendered text and a
screenshot for every fetch that backs a published fact.

## What is provisioned now

The `minio-init` service in `infra/compose/docker-compose.yml` runs once at startup
and:

1. creates the bucket named by `S3_EVIDENCE_BUCKET` (default `datahub-evidence`);
2. enables bucket versioning.

Versioning is on from the beginning because a snapshot backs a published fact. If a
key were silently overwritten, the evidence for an already-published field would
change without a trace, which is exactly what the provenance model exists to
prevent.

## Evidence integrity does not depend on this provider

Integrity is guaranteed at the **application and database** level, independently of
whatever the storage backend supports (architecture correction C12, §9.1):

- every `snapshot` row stores the `sha256` of the bytes, so tampering is detectable
  by re-hashing;
- `snapshot`, `extraction` and `field_claim` are append-only, with `UPDATE`/`DELETE`
  revoked from every application role;
- keys are content-addressed and written **only if absent** — the application never
  overwrites an existing key, so evidence behind a published field cannot change;
- a verification job re-hashes a sample of referenced objects and alerts on mismatch;
- a snapshot referenced by a published field is never garbage-collected.

These hold on MinIO, on S3, and on any S3-compatible service, with no provider
feature required.

## Production immutable retention is a deployment decision

**Not an architectural assumption, and deliberately not modelled on MinIO's
behaviour.** WORM / Object-Lock configuration depends on the production
object-storage provider and hosting environment that get chosen, which is blocked on
assumption **A16**. What has to be decided there:

- provider and whether it offers object lock at all (S3, Alibaba OSS, Tencent COS,
  MinIO self-hosted and Cloudflare R2 differ materially);
- compliance mode versus governance mode — compliance mode cannot be undone by
  anyone, including an account administrator, which is a legal and operational
  commitment rather than a technical default;
- retention period, which follows from the record-retention policy and is not an
  engineering choice;
- whether the legal basis calls for immutable retention at all, given that
  application-level integrity already covers detection.

Object Lock must be enabled **at bucket creation** and cannot be added afterwards, so
this must be settled before the first deployed bucket exists. It is recorded as an
open decision rather than pre-empted here.

## What is deliberately deferred locally
- **Lifecycle rules.** Retention for snapshots that no published field references
  is a policy question, not a bootstrap one.
- **Key layout and signed-URL issuance.** Arrives with the acquisition module. The
  planned layout is `sources/{source_id}/{yyyy}/{mm}/{dd}/{sha256}`.
- **Bucket policy / scoped credentials.** The application currently uses the root
  credentials locally. A deployed environment needs a scoped IAM identity limited to
  `GetObject`/`PutObject` on this bucket, with no `DeleteObject`.

## Access model

Objects are private without exception. Nothing is served directly from the bucket:
the API issues short-lived signed URLs after a permission check, and HTML snapshots
are rendered in a sandboxed iframe on a separate origin. Scraped HTML is untrusted
third-party content and must never execute against the console's own origin.

## Local access

```
S3 API      http://localhost:9000
Console     http://localhost:9001
Credentials S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY from .env
```

The evidence store is **not** a required readiness dependency by default; set
`READINESS_CHECK_OBJECT_STORAGE=true` once code depends on it.
