# 2. One Python codebase, several container roles

Date: 2026-09-17

## Status

Accepted (architecture decision D11)

## Context

The platform needs a request-serving API, Celery workers for crawl/extract/detect,
a scheduler, and eventually a Playwright worker for JavaScript-rendered pages.

The obvious alternative is separate services per concern (a crawler service, an API
service) communicating over HTTP or a queue.

## Decision

One Python project at `apps/api`, built into one image, run as four roles selected
by the container command: `api`, `worker`, `beat`, `worker-browser`.

## Rationale

The crawler, the detector and the API all read and write the same SQLAlchemy models
and call the same domain services. Splitting them into separate codebases would
force either duplicated models or a shared package that both depend on, which is
the same coupling with more moving parts and a version skew problem.

The isolation genuinely required for Playwright is a *container* boundary: its own
image (browser binaries are ~400MB), its own queue, its own memory limits. That is
achieved by a second Dockerfile and a queue routing rule, not a second repository.

At pilot scale (36 institutions, ~600 programs, ~1,500–3,000 source URLs) nothing
about throughput argues for independent scaling of these components.

## Consequences

- One dependency set, one lockfile, one test suite, one migration history.
- Deploying any role deploys the same artifact; a worker-only change still rebuilds
  the API image. Acceptable at this scale.
- Queue routing must be correct, because a misrouted browser task would land on a
  worker with no browser. Routes are declared in `celery_app.py` ahead of the tasks
  that will use them, and the worker's `--queues` list is explicit.
- If independent scaling or independent deploy cadence becomes a real need, the
  domain module boundaries inside the project are where a split would cut.
