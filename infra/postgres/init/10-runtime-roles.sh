#!/bin/bash
# Create the runtime database roles anticipated by the architecture.
#
# Runs once, via the postgres image's entrypoint, on an empty data directory only.
# A shell script rather than plain SQL because passwords come from the environment
# and must not be committed.
#
# WHAT THIS DOES: creates three least-privilege login roles and lets them connect.
# None of them may create schema objects. The migration/owning role is the database
# owner (POSTGRES_USER) and is NOT created here -- the image creates it.
#
# Identity separation is configured in the application from the outset: the API,
# worker and publisher each connect as their own role and the settings layer refuses
# any configuration where a service uses the owning role (core/config.py).
#
# WHAT THIS DELIBERATELY DOES NOT DO: grant or revoke anything on domain tables.
# Those tables do not exist yet. The canonical-write restrictions that make
# ARCHITECTURE.md invariants I1 and I2 real -- only `app_publisher` may write
# canonical tables; nobody may UPDATE or DELETE history tables -- must be applied in
# the same migration set that creates those tables, or they would be silently
# missing for whatever is created in between. See infra/postgres/README.md.
#
# Migrations run as the owning role (POSTGRES_USER), not as any of these.

set -euo pipefail

: "${APP_API_PASSWORD:?APP_API_PASSWORD must be set}"
: "${APP_WORKER_PASSWORD:?APP_WORKER_PASSWORD must be set}"
: "${APP_PUBLISHER_PASSWORD:?APP_PUBLISHER_PASSWORD must be set}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     --set ON_ERROR_STOP=1 \
     --set api_password="$APP_API_PASSWORD" \
     --set worker_password="$APP_WORKER_PASSWORD" \
     --set publisher_password="$APP_PUBLISHER_PASSWORD" <<'SQL'

-- Idempotent: the entrypoint runs init scripts once, but re-running by hand during
-- local troubleshooting should not fail.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
        CREATE ROLE app_api LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
        CREATE ROLE app_publisher LOGIN;
    END IF;
END
$$;

ALTER ROLE app_api       WITH PASSWORD :'api_password'       NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE app_worker    WITH PASSWORD :'worker_password'    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE app_publisher WITH PASSWORD :'publisher_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- Connect, and read the schema. Nothing more.
GRANT CONNECT ON DATABASE :"DBNAME" TO app_api, app_worker, app_publisher;
GRANT USAGE ON SCHEMA public TO app_api, app_worker, app_publisher;

-- PUBLIC can create objects in `public` by default before PG15 and retains some
-- surprising rights after; revoke so only the owner creates schema objects.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM app_api, app_worker, app_publisher;
GRANT USAGE ON SCHEMA public TO app_api, app_worker, app_publisher;

SQL

echo "runtime roles created: app_api, app_worker, app_publisher (no table grants yet)"
