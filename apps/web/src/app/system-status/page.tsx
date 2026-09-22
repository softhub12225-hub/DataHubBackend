/**
 * System status page.
 *
 * A server component: the backend base URL is server-only configuration, and the
 * browser should never call the API directly. This is the same BFF shape the console
 * will use throughout, established here on something trivial.
 */

import { DependencyTable } from "@/components/dependency-table";
import { StatusBadge } from "@/components/status-badge";
import { getReadiness } from "@/lib/api/health";
import { appEnv } from "@/lib/config/env";

// Health is live state; never serve it from a cache.
export const dynamic = "force-dynamic";
export const revalidate = 0;

export default async function SystemStatusPage() {
  const result = await getReadiness();

  return (
    <>
      <h1>System status</h1>
      <p className="muted">
        Live result of <code>GET /health/ready</code> on the backend. Environment:{" "}
        <code>{appEnv()}</code>
      </p>

      {result.outcome === "unreachable" ? (
        <div className="card">
          <p>
            <StatusBadge tone="error" label="UNREACHABLE" /> The backend did not
            respond.
          </p>
          <p className="small muted">{result.error}</p>
          <p className="small">
            Start the stack with <code>make up</code>, then confirm{" "}
            <code>API_BASE_URL</code> in <code>.env</code> points at the API.
          </p>
        </div>
      ) : (
        <>
          <div className="card">
            <p style={{ marginBottom: 0 }}>
              {result.outcome === "ready" ? (
                <StatusBadge tone="ok" label="READY" />
              ) : (
                <StatusBadge tone="error" label="NOT READY" />
              )}{" "}
              <strong>{result.body.service}</strong>{" "}
              <span className="muted">v{result.body.version}</span>
            </p>
          </div>

          <h2>Dependencies</h2>
          <DependencyTable checks={result.body.checks} />

          {result.requestId ? (
            <p className="small muted">
              Request id <code>{result.requestId}</code> — matches the backend log
              entry for this check.
            </p>
          ) : null}
        </>
      )}
    </>
  );
}
