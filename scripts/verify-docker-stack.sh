#!/usr/bin/env bash
# Verify the Docker Compose stack, mechanically.
#
# WHY THIS EXISTS: the Compose stack could not be executed during bootstrap because
# no Docker was available on the development machine. Rather than leave the check as
# a prose list somebody has to work through by hand, every item is asserted here.
#
# Usage, on a Docker-capable machine, from the repository root:
#
#     bash scripts/verify-docker-stack.sh
#
# It brings the stack up, runs migrations, asserts each property below, and prints a
# PASS/FAIL table. Non-zero exit means at least one check failed. It does not tear
# the stack down on failure, so you can inspect it.
#
# Checks, in the order the deliverable listed them:
#   1  postgres container initialises
#   2  runtime-role init script ran (app_api/app_worker/app_publisher exist)
#   3  redis healthy
#   4  minio started
#   5  minio-init created the bucket and enabled versioning
#   6  api image builds and the container starts
#   7  worker image starts and answers on the broker
#   8  beat starts
#   9  web image builds and serves
#  10  healthchecks report healthy
#  11  depends_on conditions were honoured (ordering)
#  12  volume permissions (data survives a restart; the volume is writable)
#  13  containers run as non-root
#  14  migrations apply, and readiness passes as app_api

set -uo pipefail

COMPOSE=(docker compose -f infra/compose/docker-compose.yml --env-file .env)
PASS=0
FAIL=0
RESULTS=()

ok()   { RESULTS+=("PASS  $1"); PASS=$((PASS + 1)); }
bad()  { RESULTS+=("FAIL  $1  -- $2"); FAIL=$((FAIL + 1)); }
note() { printf '\n== %s ==\n' "$1"; }

require() { # require <description> <command...>
  local desc="$1"; shift
  if output=$("$@" 2>&1); then ok "$desc"; else bad "$desc" "${output//$'\n'/ | }"; fi
}

expect_contains() { # expect_contains <description> <needle> <command...>
  local desc="$1" needle="$2"; shift 2
  local output
  output=$("$@" 2>&1)
  if grep -qF -- "$needle" <<<"$output"; then
    ok "$desc"
  else
    bad "$desc" "expected to find '${needle}' in: ${output//$'\n'/ | }"
  fi
}

# ---------------------------------------------------------------------------
note "preflight"
if ! docker info >/dev/null 2>&1; then
  echo "FATAL: docker is not available or the daemon is not running." >&2
  exit 2
fi
if [[ ! -f .env ]]; then
  echo "FATAL: .env is missing. Run: cp .env.example .env (then set the passwords)." >&2
  exit 2
fi

# shellcheck disable=SC1091
set -a; source ./.env; set +a
PG_USER="${POSTGRES_USER:-datahub}"
PG_DB="${POSTGRES_DB:-datahub}"
BUCKET="${S3_EVIDENCE_BUCKET:-datahub-evidence}"

note "bringing the stack up (build included)"
if ! "${COMPOSE[@]}" up --build -d; then
  echo "FATAL: compose up failed; nothing further can be asserted." >&2
  "${COMPOSE[@]}" ps
  exit 1
fi

note "waiting for healthchecks"
# Compose only orders startup; it does not wait for late-arriving health.
for _ in $(seq 1 60); do
  unhealthy=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' \
              | awk '$2 != "" && $2 != "healthy" {print $1}')
  [[ -z "$unhealthy" ]] && break
  sleep 5
done

# --- 1 postgres initialisation ---------------------------------------------
note "1 postgres container initialisation"
expect_contains "postgres accepts connections" "1" \
  "${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT 1"
expect_contains "postgres is version 16+" "16" \
  "${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "SHOW server_version"
expect_contains "database collation is deterministic (C)" "C" \
  "${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT datcollate FROM pg_database WHERE datname = current_database()"

# --- 2 runtime-role init script --------------------------------------------
note "2 runtime-role initialisation script"
roles=$("${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
        "SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles \
         WHERE rolname IN ('app_api','app_worker','app_publisher')" 2>&1 | tr -d '\r')
if [[ "$roles" == "app_api,app_publisher,app_worker" ]]; then
  ok "init script created all three runtime roles"
else
  bad "init script created all three runtime roles" "got: '$roles'"
fi

privileged=$("${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM pg_roles \
   WHERE rolname IN ('app_api','app_worker','app_publisher') \
     AND (rolsuper OR rolcreatedb OR rolcreaterole)" 2>&1 | tr -d '\r ')
if [[ "$privileged" == "0" ]]; then
  ok "runtime roles hold no cluster privileges"
else
  bad "runtime roles hold no cluster privileges" "$privileged role(s) are privileged"
fi

# The separation is only real if the database refuses the write, so prove it.
denied=$("${COMPOSE[@]}" exec -T -e PGPASSWORD="${APP_API_PASSWORD}" \
         postgres psql -U app_api -d "$PG_DB" -c "CREATE TABLE verify_should_fail (id int)" 2>&1)
if grep -q "permission denied" <<<"$denied"; then
  ok "app_api cannot create schema objects"
else
  bad "app_api cannot create schema objects" "${denied//$'\n'/ | }"
fi

# --- 3 redis ----------------------------------------------------------------
note "3 redis health"
expect_contains "redis answers PING" "PONG" "${COMPOSE[@]}" exec -T redis redis-cli ping

# --- 4 minio ----------------------------------------------------------------
note "4 minio startup"
minio_state=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.State}}' | awk '$1=="minio" {print $2}')
if [[ "$minio_state" == "running" ]]; then
  ok "minio container is running"
else
  bad "minio container is running" "state '$minio_state'"
fi

# --- 5 minio-init -----------------------------------------------------------
note "5 minio-init bucket creation and versioning"
init_exit=$(docker inspect -f '{{.State.ExitCode}}' \
            "$("${COMPOSE[@]}" ps -aq minio-init)" 2>/dev/null | tr -d '\r')
if [[ "$init_exit" == "0" ]]; then
  ok "minio-init completed successfully"
else
  bad "minio-init completed successfully" "exit code '$init_exit'"
fi
expect_contains "evidence bucket exists and versioning is enabled" "Enabled" \
  docker run --rm --network datahub_default \
    -e MC_HOST_local="http://${S3_ACCESS_KEY_ID}:${S3_SECRET_ACCESS_KEY}@minio:9000" \
    minio/mc:RELEASE.2024-11-21T17-21-54Z version info "local/${BUCKET}"

# --- 6/7/8/9 application containers ----------------------------------------
note "6-9 application containers"
for svc in api worker beat web; do
  state=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.State}}' | awk -v s="$svc" '$1==s {print $2}')
  if [[ "$state" == "running" ]]; then ok "$svc is running"; else bad "$svc is running" "state '$state'"; fi
done
expect_contains "worker answers on the broker" "pong" \
  "${COMPOSE[@]}" exec -T worker celery -A app.workers.celery_app inspect ping

beat_logs=$("${COMPOSE[@]}" logs --tail 60 beat 2>&1)
if grep -qiE "beat: Starting|Scheduler:" <<<"$beat_logs"; then
  ok "beat started its scheduler"
else
  bad "beat started its scheduler" "${beat_logs//$'\n'/ | }"
fi

# --- 10 healthchecks --------------------------------------------------------
note "10 healthchecks"
unhealthy=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' \
            | awk '$2 != "" && $2 != "healthy" {printf "%s=%s ", $1, $2}')
if [[ -z "$unhealthy" ]]; then
  ok "every service with a healthcheck reports healthy"
else
  bad "every service with a healthcheck reports healthy" "$unhealthy"
fi

# --- 11 depends_on ordering -------------------------------------------------
note "11 depends_on conditions"
pg_started=$(docker inspect -f '{{.State.StartedAt}}' "$("${COMPOSE[@]}" ps -q postgres)")
api_started=$(docker inspect -f '{{.State.StartedAt}}' "$("${COMPOSE[@]}" ps -q api)")
if [[ "$pg_started" < "$api_started" ]]; then
  ok "api started after postgres (condition: service_healthy)"
else
  bad "api started after postgres" "postgres=$pg_started api=$api_started"
fi
mi_finished=$(docker inspect -f '{{.State.FinishedAt}}' "$("${COMPOSE[@]}" ps -aq minio-init)")
if [[ "$mi_finished" < "$api_started" ]]; then
  ok "api started after minio-init completed (condition: service_completed_successfully)"
else
  bad "api started after minio-init completed" "minio-init=$mi_finished api=$api_started"
fi

# --- 12 volume permissions --------------------------------------------------
note "12 volume permissions and persistence"
require "postgres data directory is writable by the container user" \
  "${COMPOSE[@]}" exec -T postgres bash -c 'test -w /var/lib/postgresql/data'
require "minio data directory is writable by the container user" \
  "${COMPOSE[@]}" exec -T minio sh -c 'test -w /data'
"${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -qc \
  "CREATE TABLE IF NOT EXISTS verify_persistence (id int)" >/dev/null 2>&1
"${COMPOSE[@]}" restart postgres >/dev/null 2>&1
for _ in $(seq 1 30); do
  "${COMPOSE[@]}" exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break
  sleep 2
done
expect_contains "data survives a container restart (named volume in use)" "verify_persistence" \
  "${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT tablename FROM pg_tables WHERE tablename = 'verify_persistence'"
"${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -qc \
  "DROP TABLE IF EXISTS verify_persistence" >/dev/null 2>&1

# --- 13 non-root ------------------------------------------------------------
note "13 non-root runtime users"
for svc in api worker beat; do
  uid=$("${COMPOSE[@]}" exec -T "$svc" id -u 2>&1 | tr -d '\r')
  if [[ "$uid" == "10001" ]]; then
    ok "$svc runs as non-root (uid $uid)"
  else
    bad "$svc runs as non-root" "uid '$uid' (expected 10001)"
  fi
done
web_uid=$(docker exec "$("${COMPOSE[@]}" ps -q web)" id -u 2>&1 | tr -d '\r')
if [[ "$web_uid" == "10001" ]]; then
  ok "web runs as non-root (uid $web_uid)"
else
  bad "web runs as non-root" "uid '$web_uid' (expected 10001)"
fi

# --- 14 migrations + readiness ---------------------------------------------
note "14 migrations and readiness"
require "alembic upgrade head succeeds in the container" \
  "${COMPOSE[@]}" exec -T -w /app/apps/api api alembic upgrade head
expect_contains "runtime roles were granted SELECT on alembic_version" "app_api" \
  "${COMPOSE[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT grantee FROM information_schema.role_table_grants \
   WHERE table_name = 'alembic_version' AND grantee = 'app_api'"
expect_contains "api readiness passes (probing as app_api)" '"status":"ready"' \
  "${COMPOSE[@]}" exec -T api curl -fsS http://localhost:8000/health/ready
expect_contains "web serves the system status page" "200" \
  "${COMPOSE[@]}" exec -T web sh -c \
  "curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/system-status"

# ---------------------------------------------------------------------------
note "results"
printf '%s\n' "${RESULTS[@]}"
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"

if (( FAIL > 0 )); then
  echo
  echo "The stack was left running for inspection. Tear down with: make down"
  exit 1
fi

echo
echo "All Docker stack checks passed."
echo "Record the result in docs/assumptions.md and mark the Step 2 Docker item resolved."
