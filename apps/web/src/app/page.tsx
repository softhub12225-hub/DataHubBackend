import Link from "next/link";

import { appEnv } from "@/lib/config/env";

export default function HomePage() {
  return (
    <>
      <h1>University DataHub</h1>
      <p className="muted">
        Internal data management and verification platform. Environment:{" "}
        <code>{appEnv()}</code>
      </p>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Bootstrap stage</h2>
        <p>
          This is the development foundation only. No university, program or
          admissions functionality exists yet, and no domain schema has been created.
        </p>
        <p>
          <Link href="/system-status">System status</Link> verifies that the backend
          and its dependencies are reachable.
        </p>
      </div>

      <h2>What is in place</h2>
      <ul>
        <li>FastAPI backend with typed settings, structured logging and a shared error envelope</li>
        <li>Liveness and readiness endpoints, with readiness checking Postgres and Redis</li>
        <li>PostgreSQL via SQLAlchemy 2.x, migrations via Alembic</li>
        <li>Redis and a Celery worker bootstrap</li>
        <li>TypeScript API types generated from the FastAPI OpenAPI document</li>
      </ul>
    </>
  );
}
