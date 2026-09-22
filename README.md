# Overseas University DataHub

Internal university data management and verification platform. Every published fact
carries its official source, a stored snapshot of that source, the reviewer who
approved it and the version it was published in.

**Current stage: development bootstrap.** There is no university, program or
admissions functionality yet, and no domain schema. This repository currently
contains the foundation the domain will be built on.

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Decisions: [docs/adr/](docs/adr/)
- Assumptions and open questions: [docs/assumptions.md](docs/assumptions.md)
- Local development troubleshooting: [ops/runbooks/local-development.md](ops/runbooks/local-development.md)

---

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Docker + Compose | v2 | Runs the whole stack. **See the verification note below.** |
| Python | 3.12 | Pinned in `.python-version`; `uv` installs it |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | Python dependency management |
| Node | 22.23.2 | Pinned in `.node-version` / `.nvmrc`; floor is `>=22.13.0` |
| pnpm | 9.15.4 | `corepack enable` |

`engine-strict=true` is set in `.npmrc`, so pnpm refuses to install or run under a
Node version outside the supported range. That is deliberate: local-vs-CI Node
divergence produces builds that work on one machine and not another, and the same
`.node-version` file drives both local development and CI.

> **⚠ The Docker Compose stack has not yet been executed.** It was authored on a
> machine without Docker. Before relying on it, run
> `bash scripts/verify-docker-stack.sh` on a Docker-capable machine; it asserts
> image builds, healthchecks, `depends_on` ordering, volume permissions, the
> Postgres role-init path and non-root users, and prints a PASS/FAIL table. See
> [docs/assumptions.md](docs/assumptions.md).

## Quick start

```bash
cp .env.example .env     # then change the placeholder passwords
make bootstrap           # uv sync + pnpm install
make up                  # build and start the stack
make migrate             # apply migrations
```

| What | Where |
|---|---|
| Console | <http://localhost:3000> |
| System status page | <http://localhost:3000/system-status> |
| API readiness | <http://localhost:8000/health/ready> |
| API docs | <http://localhost:8000/docs> |
| MinIO console | <http://localhost:9001> |

## Commands

`make help` lists everything. Windows developers usually have no `make`; the direct
equivalent of each target is given here.

| Task | make | Direct equivalent |
|---|---|---|
| Install everything | `make bootstrap` | `uv sync && pnpm install` |
| Start the stack | `make up` | `docker compose -f infra/compose/docker-compose.yml --env-file .env up --build -d` |
| Dependencies only | `make up-deps` | `docker compose ... up -d postgres redis minio minio-init` |
| Stop | `make down` | `docker compose ... down` |
| Stop and wipe data | `make down-volumes` | `docker compose ... down --volumes` |
| Apply migrations | `make migrate` | `cd apps/api && uv run alembic upgrade head` |
| New migration | `make migration m="..."` | `cd apps/api && uv run alembic revision --autogenerate -m "..."` |
| Lint | `make lint` | `uv run ruff check . && uv run ruff format --check . && pnpm run lint` |
| Type-check | `make typecheck` | `uv run mypy && pnpm run typecheck` |
| Test | `make test` | `uv run pytest && pnpm --filter @datahub/web run test` |
| Frontend build | `make build-web` | `pnpm --filter @datahub/web run build` |
| Regenerate API types | `make generate-api-types` | `pnpm generate:api-types` |
| Check types are current | `make check-api-types` | `pnpm check:api-types` |
| Everything CI runs | `make check` | — |
| Verify the Docker stack | `make verify-docker` | `bash scripts/verify-docker-stack.sh` |

## Repository layout

```
apps/
  api/          FastAPI + Celery. One codebase, four container roles (D11).
  web/          Next.js 15 App Router console.
packages/
  api-types/    GENERATED TypeScript types + openapi.json. Never hand-edited (D10).
infra/
  docker/       Dockerfiles per image (api serves api/worker/beat).
  compose/      Local development stack.
  postgres/     Runtime role initialisation. Table grants deliberately deferred.
  minio/        Evidence-store notes.
docs/           Architecture, ADRs, assumptions.
ops/runbooks/   Operational procedures.
scripts/        Repository tooling (API type generation).
```

## How the API contract works

FastAPI/Pydantic is the single source of truth (architecture decision D10):

```
apps/api  ->  packages/api-types/openapi.json  ->  packages/api-types/src/schema.d.ts
```

Both generated artifacts are committed, so a contract change appears as a reviewable
diff on the pull request that caused it, and CI can detect drift without booting
Python. Run `make generate-api-types` after changing any request or response model;
`make check-api-types` fails if you forget.

## Testing

```bash
make test-py-unit   # no external services needed
make test-py        # adds integration tests, which skip without live services
make test-web
```

Integration tests run only when `DATAHUB_TEST_POSTGRES_DSN` and
`DATAHUB_TEST_REDIS_URL` are set, so the suite is green on a laptop with nothing
running and thorough in CI. They cover Alembic upgrade from an empty database,
downgrade, idempotency, Postgres version and extensions, Redis round-trip, and the
readiness endpoint against real services.

## Conventions worth knowing before you write code

- **No business logic in route handlers.** Routes compose; behaviour lives in the
  module that owns it.
- **All timestamps are UTC and timezone-aware.** `core/clock.py` is the only source
  of "now". Source-domain time semantics (a deadline stated as "23:59 GMT", the
  Beijing-time review SLA) are properties of the *data* and are carried by explicit
  columns, never by the process clock.
- **Startup never connects to anything.** `create_app()` must stay free of I/O so
  the OpenAPI document can be generated without a database, and so a pod does not
  crash-loop because Postgres is briefly unavailable. Readiness reports that
  instead.
- **Errors use one envelope**, defined in `core/errors.py`. Clients parse
  `error.code`, never prose. Unexpected exceptions never leak internals.
- **Secrets are `SecretStr`, and connection URLs are plain properties** rather than
  pydantic computed fields — a computed field is part of the model and would print
  the password in `repr()` and `model_dump()`. There is a regression test for this.
- **Nothing is committed to `.env`.** The application refuses to start in staging or
  production while a placeholder secret is still in place.
