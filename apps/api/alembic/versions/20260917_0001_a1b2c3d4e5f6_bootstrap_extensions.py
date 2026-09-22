"""Bootstrap: required PostgreSQL extensions and readiness grants

Creates no tables. The domain schema arrives in a later, separate migration set.

Why extensions are the bootstrap migration rather than an init script: they are a
schema-level prerequisite that must exist in every environment, including managed
Postgres where ``docker-entrypoint-initdb.d`` does not run. Keeping them in Alembic
means one authoritative definition, applied the same way everywhere, and it gives
this step a genuine end-to-end check that migrations execute DDL successfully.

* ``pg_trgm``    -- trigram indexes for institution and program name search
                    (ARCHITECTURE.md section 8.3)
* ``btree_gist`` -- exclusion constraints preventing overlapping effective periods
                    (ARCHITECTURE.md section 8.1)

Requires a role with CREATE privilege on the database. Migrations run as the owning
role, not as a runtime role; see ``infra/postgres/README.md``.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS: tuple[str, ...] = ("pg_trgm", "btree_gist")

#: Runtime roles created by infra/postgres/init/10-runtime-roles.sh. Fixed names,
#: because they are an architectural contract rather than a deployment detail.
RUNTIME_ROLES = "ARRAY['app_api', 'app_worker', 'app_publisher']"

# Readiness verifies that migrations have been applied by reading alembic_version, so
# the runtime roles need SELECT on it -- and only on it. This is the single grant the
# bootstrap installs; every domain grant belongs to the Step 3 migration set that
# creates the tables it protects (see infra/postgres/README.md).
#
# Guarded on role existence so this also runs where a single database user is used
# (CI, a throwaway scratch database).
_READINESS_GRANT = """
DO $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT rolname FROM pg_roles WHERE rolname = ANY ({roles})
    LOOP
        EXECUTE format('{verb} SELECT ON TABLE alembic_version {preposition} %I', target.rolname);
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')
    op.execute(_READINESS_GRANT.format(roles=RUNTIME_ROLES, verb="GRANT", preposition="TO"))


def downgrade() -> None:
    op.execute(_READINESS_GRANT.format(roles=RUNTIME_ROLES, verb="REVOKE", preposition="FROM"))
    # Dropping the extensions is safe only while nothing depends on them. Once domain
    # indexes and exclusion constraints exist this revision is effectively
    # irreversible in practice: RESTRICT (the default) refuses rather than cascading
    # away real constraints.
    for extension in reversed(EXTENSIONS):
        op.execute(f'DROP EXTENSION IF EXISTS "{extension}"')
