# Deploying the crawler

The crawler is not a separate codebase. It is `apps/api/scripts/acquisition.py` plus
the application's models, settings, lease logic and evidence store, and it shares one
database schema with the API. So it deploys from **this** repository as additional
services rather than from a copy.

Railway documents the conflict that arises when several services build from one repo:
they would all inherit `railway.json`'s `startCommand` and every one of them would try
to start uvicorn. `railway.crawler.json` is the second config, selected per service
through that service's *Config file path* setting.

## Why `railway.crawler.json` omits `startCommand`

Deliberately. The three crawler services run three different commands, and one config
file cannot hold three. What it *does* carry is everything they share:

- the same Dockerfile, so all services run the identical image the API runs;
- `restartPolicyType: NEVER`, because a cron that fails should wait for its next
  schedule rather than restart-loop against the same failure; and
- **no `healthcheckPath`** — a cron service exits when it finishes and serves no HTTP,
  so a healthcheck would fail every run. `railway.json` sets one because the API needs
  it; inheriting it here would mark every successful crawl as a failed deploy.

Each service then sets its own start command and cron schedule.

## The three services

| Service | Start command | Schedule |
| --- | --- | --- |
| `crawler-sweep` | `sh -c 'python apps/api/scripts/acquisition.py sweep --quiet'` | `*/15 * * * *` |
| `crawler-enqueue` | `sh -c 'python apps/api/scripts/acquisition.py enqueue --pilot'` | `0 3 * * *` |
| `crawler-worker` | `sh -c 'python apps/api/scripts/acquisition.py worker --max-pages 20 --concurrency 4 --i-understand'` | `30 * * * *` |

`sh -c` because a service-level start command is exec'd directly with no shell, which
is how `${PORT:-8000}` once reached uvicorn as six literal characters.

### Why three and not one

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

Storage is set as **environment-level shared variables**, not per service, because
three processes touch the same bucket: the worker writes raw snapshots under
`evidence/`, extraction reads those and writes normalised documents under `derived/`,
and the API's evidence viewer reads `derived/`. If those disagree the viewer shows
nothing for snapshots that exist — on the screen a reviewer uses to decide whether a
figure may be published, and it reads as missing evidence rather than as
misconfiguration.

Service-level variables shadow shared ones, so a placeholder left on a service wins
over the real shared value. Check for that after any change.

`EVIDENCE_BACKEND` belongs in the shared set too: it is what makes the API read S3
rather than a local directory. Once it is `s3`, `ARTIFACT_ROOT` is unused.

## Before scheduling in earnest

Measure the block rate from Railway. 23% of pilot pages (72 of 319) are already
`BLOCKED` by bot protection from an office IP, and datacenter ranges are treated more
harshly. Run `acquisition_smoke.py run --i-understand` — twelve pages — from a Railway
one-off and compare. Reduced coverage from a cloud IP is the kind of failure that shows
up as missing data, not as an error.
