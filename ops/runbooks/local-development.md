# Runbook: local development

## ⚠ Docker stack verification is OUTSTANDING

The Compose stack has **never been executed**. It was written on a machine with no
Docker installed, so image builds, healthcheck wiring, `depends_on` conditions,
volume permissions and the container init path are all unverified.

Until this is done, treat "the stack comes up" as an assumption:

```bash
# On a Docker-capable machine, from the repository root:
cp .env.example .env          # set the passwords
bash scripts/verify-docker-stack.sh
```

That script asserts all fourteen properties the Step 2 review asked for and prints a
PASS/FAIL table; a non-zero exit means at least one failed, and it leaves the stack
running for inspection. Then run `make check`.

Record the outcome in [docs/assumptions.md](../../docs/assumptions.md) and only then
consider Step 2 fully verified.

## First run

```bash
cp .env.example .env     # then change the placeholder passwords
make bootstrap           # uv sync + pnpm install
make up                  # build and start the full stack
make migrate             # apply migrations
```

Then check:

- API readiness — <http://localhost:8000/health/ready>
- API docs — <http://localhost:8000/docs>
- Console status page — <http://localhost:3000/system-status>
- MinIO console — <http://localhost:9001>

Without `make` (Windows), every target has a direct equivalent; see the command
table in [README.md](../../README.md).

## Running the apps on the host

Useful when you want a debugger attached:

```bash
make up-deps     # postgres, redis, minio only
make api         # uvicorn with reload, on the host
make worker      # celery worker, on the host
```

`.env` defaults point at `localhost`, which is what the host processes need.
Compose overrides these to service DNS names for the containerised services.

---

## Symptom: `/health/ready` returns 503 with `postgres` failing

Read the `error` field on the failing check first; it names the exception type.

| Error contains | Cause | Fix |
|---|---|---|
| `alembic_version is empty` | Database reachable but not migrated | `make migrate` |
| `ConnectionRefusedError` | Postgres not running, or wrong port | `make ps`; check `POSTGRES_PORT` |
| `InvalidPasswordError` | `.env` password differs from the one the volume was initialised with | See "stale volume" below |
| `TimeoutError` | Postgres starting, or overloaded | Wait for the healthcheck; then investigate |

Readiness deliberately fails when migrations have not been applied. A process that
can reach an un-migrated database is not ready to serve, and reporting it as ready
is how a half-deployed release starts returning 500s.

## Symptom: password authentication fails after changing `.env`

The Postgres image initialises users **only on an empty data directory**. Changing
`POSTGRES_PASSWORD` or any `APP_*_PASSWORD` in `.env` does not affect an existing
volume.

```bash
make down-volumes   # DESTROYS local Postgres and MinIO data
make up
make migrate
```

The same applies to `infra/postgres/init/*.sh`: edits do nothing until the volume is
recreated.

## Symptom: `make up` fails with "POSTGRES_PASSWORD must be set"

Compose is configured to fail loudly rather than fall back to a default password.
Copy `.env.example` to `.env` and fill it in.

## Symptom: the console shows "UNREACHABLE"

1. Is the API up? `curl http://localhost:8000/health/live`
2. Is `API_BASE_URL` right? Inside Compose it must be `http://api:8000`, not
   `localhost` — `localhost` in the web container is the web container.
3. Check `make logs`.

## Symptom: object storage check fails

`READINESS_CHECK_OBJECT_STORAGE=false` by default, so this only appears once you
enable it. The usual cause is a missing bucket: the `minio-init` service creates it
and must have completed successfully.

```bash
make ps                                   # minio-init should show "exited (0)"
docker compose -f infra/compose/docker-compose.yml logs minio-init
```

## Symptom: Celery worker starts but processes nothing

The worker subscribes to an explicit queue list. A task routed to `browser` is
never consumed, because that worker is not built yet (deliberately). Check
`task_routes` in `apps/api/src/app/workers/celery_app.py` against the `--queues`
argument in `infra/compose/docker-compose.yml`.

Broker round-trip check:

```bash
docker compose -f infra/compose/docker-compose.yml exec worker \
  celery -A app.workers.celery_app call app.workers.ping
```

## Symptom: CI fails on `check-api-types`

The committed API contract no longer matches the code.

```bash
make generate-api-types
git add packages/api-types
```

Never edit the generated files to make the check pass; regenerate them.

## Resetting everything

```bash
make down-volumes
rm -rf .venv node_modules apps/web/.next
make bootstrap up migrate
```
