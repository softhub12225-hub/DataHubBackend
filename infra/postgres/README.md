# PostgreSQL roles and grants

## Four identities, separated from the outset (C11)

| Identity | Used by | Env vars | Privileges today |
|---|---|---|---|
| migration role (db owner) | Alembic **only** | `POSTGRES_MIGRATION_USER` / `_PASSWORD` | owns the schema, full DDL |
| `app_api` | FastAPI request handling | `POSTGRES_API_USER` / `_PASSWORD` | `CONNECT`, `USAGE ON SCHEMA public`, `SELECT` on `alembic_version` |
| `app_worker` | Celery workers (crawl, extract, detect, SLA) | `POSTGRES_WORKER_USER` / `_PASSWORD` | same |
| `app_publisher` | the publication transaction — the only writer of canonical state | `POSTGRES_PUBLISHER_USER` / `_PASSWORD` | same |

`init/10-runtime-roles.sh` creates the three runtime roles on first container start
(and is invoked explicitly in CI, since GitHub service containers cannot mount
`docker-entrypoint-initdb.d`). All three are
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`, and none can create schema objects:
`CREATE ON SCHEMA public` is revoked from `PUBLIC` and from each role.

### There is no shared application credential

This is the part that makes the separation real rather than aspirational:

- **No `POSTGRES_USER`/`POSTGRES_PASSWORD` pair is read by the application.** Those
  two variables exist only because the `postgres` image itself needs them; the
  application reads `POSTGRES_<ROLE>_*` and nothing else.
- Settings validation **rejects** any configuration where `app_api`, `app_worker` or
  `app_publisher` resolves to the migration user — in local and CI as well as in
  deployed environments. A separation that only holds in production is the one that
  gets copied wrong.
- Service roles must also be **distinct from each other**, or per-role grants could
  not be applied.
- The async engine factory **refuses** `DatabaseRole.MIGRATION`, so the owning role
  cannot reach an application connection pool even by mistake.
- The migration credentials are **optional and absent by design** in service
  containers. Compose sets `POSTGRES_MIGRATION_USER: ""` on the `api` service, and
  the settings layer treats blank as "not configured"; asking for those credentials
  then raises instead of yielding a DSN with an empty username.
- The publisher session is deliberately **not** a FastAPI dependency. It is acquired
  inside `publication.publish()` only, so no request handler can pick it up.

Each role connects with its own pool and its own `application_name`
(`datahub-api`, `datahub-worker`, `datahub-publisher`), so `pg_stat_activity`
attributes a session to a privilege level rather than to "the app".

### Why the bootstrap migration grants one thing

Readiness verifies that migrations have been applied by reading `alembic_version`,
and it probes **as its own role** — probing as the owner would pass on borrowed
privileges and hide exactly the misconfiguration the probe should catch. So the
bootstrap migration grants the three runtime roles `SELECT` on that one table, and
nothing else. Every domain grant belongs to the migration that creates the table it
protects.

Migrations run as the owning role. Runtime roles deliberately lack DDL rights, which
is also why the bootstrap migration's `CREATE EXTENSION` works locally but needs a
privileged role on a managed Postgres instance.

## What is deliberately deferred

**No table-level grants or revocations have been applied, because there are no
domain tables yet.**

The architecture's two load-bearing database invariants are privilege-based:

- **I1** — only `app_publisher` may `INSERT`/`UPDATE` canonical projection tables.
- **I2** — nobody, including `app_publisher` and including an administrator, may
  `UPDATE` or `DELETE` the append-only history tables (`field_provenance`,
  `entity_version`, `audit_log`, `change_event`, `entity_relationship`, `snapshot`,
  `extraction`, `field_claim`, `claim_resolution`, `review_decision`,
  `conflict_resolution`).

These **must be written in the same migration set that creates those tables**, not
added afterwards. Two reasons:

1. A grant applied later leaves a window in which the invariant does not hold, and
   anything created during that window is unprotected.
2. `ALTER DEFAULT PRIVILEGES` only affects objects created *after* it runs, so
   ordering is not a matter of taste.

The expected shape, for reference when that migration is written:

```sql
-- I1: canonical projections are writable only by the publisher
REVOKE INSERT, UPDATE, DELETE ON <canonical tables> FROM app_api, app_worker;
GRANT  SELECT                  ON <canonical tables> TO app_api, app_worker;
GRANT  INSERT, UPDATE, SELECT  ON <canonical tables> TO app_publisher;

-- I2: history is append-only for everyone
REVOKE UPDATE, DELETE ON <history tables> FROM app_api, app_worker, app_publisher;
GRANT  INSERT, SELECT  ON <history tables> TO app_publisher;
GRANT  SELECT          ON <history tables> TO app_api, app_worker;

-- plus BEFORE UPDATE OR DELETE triggers on each history table as
-- defense-in-depth, so even a mistaken migration cannot mutate history
```

Grants are privileges, not authorisation logic. Segregation of duties is
cross-row and cross-user and is enforced in the policy layer and re-validated inside
the publication transaction (architecture correction C2); a database trigger is a
backstop only.

## Local credentials

Passwords come from `.env` (`APP_API_PASSWORD`, `APP_WORKER_PASSWORD`,
`APP_PUBLISHER_PASSWORD`). Nothing is committed. `.env.example` carries obvious
placeholders, and the application refuses to start in `staging` or `production` with
a placeholder value still in place.

## Re-running the init script

The postgres entrypoint runs `docker-entrypoint-initdb.d` **only against an empty
data directory**. Editing the script and restarting does nothing. To re-apply:

```bash
make down-volumes   # destroys local Postgres and MinIO data
make up
```
