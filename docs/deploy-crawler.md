# Deploying the crawler

The crawler is not a separate codebase. It is `apps/api/scripts/acquisition.py` plus
the application's models, settings, lease logic and evidence store, and it shares one
database schema with the API. So it deploys from **this** repository as additional
services rather than from a copy. Two copies of the same Alembic history is how a
crawler ends up writing rows the API cannot read.

## Where the configuration lives, and why

`railway.json` carries **only the build settings** — the Dockerfile and its path —
because that is the only part every service built from this repo genuinely shares.
Nothing else is in it.

That is deliberate, and it took two attempts to get right:

1. The obvious approach is a second config file, `railway.crawler.json`, selected per
   service. **Railway rejects this**: config-as-code (`railway.json` / `railway.toml`)
   is deprecated in favour of infrastructure-as-code (`.railway/railway.ts`), and the
   per-service *config file path* setting is no longer accepted by the API.

2. Leaving `startCommand` and `healthcheckPath` in `railway.json` does not work
   either, because a config file at the repo root applies to **every** service built
   from that repo. The crawler services would inherit the API's start command and each
   try to run uvicorn on a cron schedule — and a cron service that never exits is
   worse than one that fails, because Railway then *skips* every subsequent execution.
   A healthcheck is equally wrong for a cron: the service exits when it finishes and
   serves no HTTP, so the check would mark every successful crawl as a failed deploy.

So each service holds its own start command, schedule, restart policy and healthcheck
in its Railway service configuration. The repo holds what they share.

When this project migrates to `.railway/railway.ts`, all of it can move there and be
version-controlled again.

## The services

| Service | Start command | Schedule | Restart |
| --- | --- | --- | --- |
| `DataHubBackend` | `sh -c 'uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}'` | — | `ON_FAILURE` ×3 |
| `crawler-sweep` | `sh -c 'python apps/api/scripts/acquisition.py sweep --quiet'` | `*/15 * * * *` | `NEVER` |
| `crawler-enqueue` | `sh -c 'python apps/api/scripts/acquisition.py enqueue --pilot'` | `0 3 * * *` | `NEVER` |
| `crawler-worker` | `sh -c 'python apps/api/scripts/acquisition.py worker --max-pages 20 --concurrency 4 --i-understand'` | `30 * * * *` | `NEVER` |

`sh -c` because a service-level start command is exec'd directly with no shell, which
is how `${PORT:-8000}` once reached uvicorn as six literal characters.

`python` and the `app` package both resolve without qualification: the runtime image
sets `PATH="/app/.venv/bin:$PATH"` and `PYTHONPATH="/app/apps/api/src"`, with
`WORKDIR /app`.

`NEVER` on the crons: a failed run should wait for its next schedule rather than
restart-loop against the same failure.

### Why three services and not one

**Separation of consequence.** `enqueue` decides *what* will be fetched; `worker` does
the fetching. Keeping them apart means a mistaken enqueue is not instantly several
thousand requests to universities, and the worker can be stopped without stopping the
part that keeps the queue current.

**`sweep` is the safety net and belongs on its own clock.** It sends no HTTP and
contacts no third party — it is a database correction — so it is safe every fifteen
minutes, which the worker is not. Without it a container killed mid-fetch (OOM, a
redeploy, a cron timeout) leaves its attempts in `RUNNING` with an expired lease and
nothing reclaims them. Those pages then fall out of every future cycle *silently*,
because enqueueing is idempotent per cycle and an attempt that already exists is not
re-queued. The crawl would keep reporting success over a shrinking corpus.

## Runs must be bounded

Railway **skips** a cron execution if the previous one has not exited. An unbounded
worker at 500 institutions would overrun its window and quietly drop most of its
cycles, which looks like stale data rather than an error.

So `--max-pages` is not a safety toggle to be removed later — it is what makes the
schedule honest. Size it so a run finishes well inside its interval. The queue is
durable and the next run continues where this one stopped.

Start at `--max-pages 20 --concurrency 4`, read `acquisition.py report`, and raise from
measured throughput. Per-host politeness is a separate mechanism (one request in flight
per host, a two-second floor, `Retry-After` honoured), so raising global concurrency
does not make the crawler less polite.

## Identity

The crawler runs as `app_worker`: it writes `fetch_attempt`, `fetch_run`, `snapshot`
and `content_blob`, and cannot write canonical data. The recovery commands
(`source-reenable`, `clear-cooldown`) deliberately run as the API role instead, because
restoring a page is an operator action taken through the application rather than
something the fetch plane does to itself.

Nothing here can publish. Acquisition never writes `publication_eligibility`, and no
role holds `proposal:publish`. The worst a runaway crawl can do is waste requests and
fill a bucket.

## Variables

Storage is set as **environment-level shared variables** and referenced per service as
`${{shared.NAME}}`; the database settings are referenced from the API service as
`${{DataHubBackend.NAME}}`. Either way each value exists once, so changing it changes
it everywhere.

That matters because three processes touch the same bucket: the worker writes raw
snapshots under `evidence/`, extraction reads those and writes normalised documents
under `derived/`, and the API's evidence viewer reads `derived/`. If those disagree the
viewer shows nothing for snapshots that exist — on the screen a reviewer uses to decide
whether a figure may be published, and it reads as missing evidence rather than as
misconfiguration.

**Service-level variables shadow shared ones.** Two placeholder S3 credentials left on
the API service silently won over the real shared values until they were deleted.
Check for that after any change.

`EVIDENCE_BACKEND` belongs in the shared set too: it is what makes the API read S3
rather than a local directory. Once it is `s3`, `ARTIFACT_ROOT` is unused.

## Before scheduling in earnest

Measure the block rate from Railway. 23% of pilot pages (72 of 319) are already
`BLOCKED` by bot protection from an office IP, and datacenter ranges are treated more
harshly. Run `acquisition_smoke.py run --i-understand` — twelve pages — from a Railway
one-off and compare. Reduced coverage from a cloud IP is the kind of failure that shows
up as missing data, not as an error.
