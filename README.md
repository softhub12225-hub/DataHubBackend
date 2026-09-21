# Overseas University DataHub — API

FastAPI backend for an internal university data management and verification platform.
Every published fact carries its official source, a stored snapshot of that source, the
reviewer who approved it, and the version it was published in.

The reviewer console is a separate repository and a separate deployment:
**[UniversityDataHub](https://github.com/softhub12225-hub/UniversityDataHub)**. It holds
no database credential — it forwards browser requests to this API server-side.

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Decisions: [docs/adr/](docs/adr/)
- Assumptions and open questions: [docs/assumptions.md](docs/assumptions.md)
- Source verification: [docs/SOURCE_VERIFICATION.md](docs/SOURCE_VERIFICATION.md)
- Candidate review: [docs/CANDIDATE_REVIEW.md](docs/CANDIDATE_REVIEW.md)
- Local development troubleshooting: [ops/runbooks/local-development.md](ops/runbooks/local-development.md)

## What is built

Evidence acquisition, document normalisation, claim extraction, source verification
(domain → responsibility → promotion), and the reviewer console's API: authentication,
responsibility decisions, promotion, candidate scope resolution and conflict resolution.

Nothing is published yet. `field_claim`, `change_proposal`, `change_event` and the
canonical tables are empty by design — publication is gated on a verified source, an
accepted candidate review and a resolved applicant scope, and on an independent
publisher identity that does not yet exist.

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12 | Pinned in `.python-version`; `uv` installs it |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | Dependency management |
| PostgreSQL | 16 | Four roles, see below |
| Redis | 7 | Celery broker and result backend |
| Docker + Compose | v2 | Optional; runs the whole stack locally |

## Quick start

```bash
cp .env.example .env     # then change the placeholder passwords
uv sync --frozen
uv run alembic upgrade head            # from apps/api
uv run uvicorn app.main:app --port 8000
```

| What | Where |
|---|---|
| Readiness | <http://localhost:8000/health/ready> |
| API docs | <http://localhost:8000/docs> |
| Console API | `/api/v1/review/*` |

With Docker instead: `make up` starts Postgres, Redis, MinIO, the API, the Celery
worker and beat. `make help` lists every target; each one is also a plain `uv`
command, because Windows machines usually have no `make`.

## Four database identities, not one

There is deliberately **no shared application user**, and the settings layer refuses
any configuration where a service connects as the schema owner — in local and CI as
well as in production, because a shortcut that works locally is the one that gets
copied.

| Role | Used by | Owns |
|---|---|---|
| `datahub` | Alembic only | every schema object |
| `app_api` | the API process | nothing |
| `app_worker` | Celery workers | nothing |
| `app_publisher` | the publication transaction only | nothing |

`infra/postgres/init/10-runtime-roles.sh` creates the three runtime roles and grants
them. The Compose stack runs it automatically; CI invokes it explicitly; a managed
database needs it run once by hand.

## Deploying

The image is `infra/docker/api.Dockerfile`, built with the **repository root as the
build context** (it copies `pyproject.toml`, `uv.lock` and `apps/api/`). On a platform
that builds a Dockerfile — Render, Railway, Fly, Cloud Run — point it at that file and
leave the context at the root. The same image runs three roles; the command selects
which:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000        # API
celery -A app.workers.celery_app worker                 # worker
celery -A app.workers.celery_app beat                   # scheduler
```

Migrations are not run by the image. Run `alembic upgrade head` as a release step, as
the migration role.

### Environment

`.env.example` is the authoritative list. The settings layer **refuses to start** in a
deployed environment while a placeholder value is still in place, so a forgotten
secret fails at boot rather than at the first request. The ones that always need
setting:

| Variable | Why |
|---|---|
| `ENVIRONMENT` | `staging` or `production` enables the placeholder rejection and secure cookies |
| `POSTGRES_*` | host, port, db, and a user/password pair per role above |
| `REDIS_*` | broker and result backend |
| `SESSION_SECRET` | signs preview tokens. A default here would let anyone who read this file mint a "confirm" for a decision nobody previewed |
| `CORS_ALLOWED_ORIGINS` | the console's origin. No wildcards: a wildcard cannot carry cookies, and the session depends on them |
| `ARTIFACT_ROOT` | where stored document artifacts live — see below |

### Stored evidence must travel with the deployment

The evidence viewer reads bytes from `ARTIFACT_ROOT` and **never refetches**, so a
reviewer judges what the system actually holds. Those artifacts are git-ignored: they
are large and regenerable. A deployment without them answers
`BODY_EVIDENCE_NOT_AVAILABLE` for every source, which is honest but useless — so
either mount the artifact directory as a volume, or point `ARTIFACT_ROOT` at shared
storage the API can read.

A mistyped `ARTIFACT_ROOT` used to surface as HTTP 500 on the evidence screen. It now
reports the named status and says which artifact is missing.

### The approved manifests are committed on purpose

`.reports/step-5c5/` is the one thing excepted from the "derived data" ignore rules.
`app/domains/verification/console.py` pins each manifest's sha256, and every
responsibility decision and promotion is refused unless the bytes on disk hash to the
approved value — so they are an input the code is pinned to, not derived output. A
deployment without them cannot verify anything. `make check-manifests` asserts they
still match, and CI runs it.

### A managed Postgres (Neon, Supabase, RDS) needs two things first

**1. The four roles.** A managed database hands you one owner role. Run
`infra/postgres/init/10-runtime-roles.sh` against it, or create `app_api`,
`app_worker` and `app_publisher` by hand with that script's grants. Pointing every
service at the owner role will be rejected at boot by design.

**2. TLS.** Most managed providers require it, and the DSN builder in
`app/core/config.py` currently composes `scheme://user:password@host:port/db` with no
query parameters — so `sslmode=require` cannot be expressed yet. Until that setting
exists, a provider that mandates TLS will refuse the asyncpg connection. Track this
before pointing a deployment at one.

## Commands

`make help` lists everything. The ones used most:

| Task | Command |
|---|---|
| Lint + format check | `uv run ruff check . && uv run ruff format --check .` |
| Type-check (strict) | `uv run mypy` |
| Tests | `uv run pytest` |
| Migrate | `cd apps/api && uv run alembic upgrade head` |
| Manifest check | `cd apps/api && uv run python -m scripts.check_manifests` |
| Reviewer console CLI | `uv run python -m scripts.source_review --help` |

Integration tests skip themselves unless `DATAHUB_TEST_POSTGRES_DSN` is set. They
create and drop scratch databases, so that DSN must name the owner role.

## Layout

```
apps/api/src/app/
  api/            HTTP surface: routers, dependencies, error envelope
  core/           config, logging, security middleware, engines
  db/             base metadata, enums, table classification
  domains/
    acquisition/  fetching, snapshots, the evidence store
    extraction/   HTML and PDF normalisation into stored artifacts
    claims/       candidate extraction, grouping, scope, precedence, resolution
    verification/ domains, responsibilities, promotion, the console services
    identity/     users, permissions, sessions
    sources/      ORM models
  workers/        Celery app and tasks
apps/api/alembic/ migrations
apps/api/scripts/ operational CLIs
apps/api/tests/   unit and integration tests
infra/            Dockerfile, Compose, Postgres and MinIO init
docs/             architecture, ADRs, per-domain documentation
```
