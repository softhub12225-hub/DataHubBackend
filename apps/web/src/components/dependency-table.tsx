/**
 * Renders the per-dependency results from /health/ready.
 *
 * Failures show the backend's own error summary: during local setup the useful
 * question is always "which dependency, and what did it say?", and making an
 * operator read container logs to find out is a waste of their time.
 */

import { StatusBadge } from "@/components/status-badge";
import type { DependencyCheck } from "@/lib/api/health";

export function DependencyTable({ checks }: { checks: DependencyCheck[] }) {
  if (checks.length === 0) {
    return <p className="muted">The backend reported no dependency checks.</p>;
  }

  return (
    <table className="status-table">
      <thead>
        <tr>
          <th scope="col">Dependency</th>
          <th scope="col">Status</th>
          <th scope="col">Latency</th>
          <th scope="col">Detail</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((check) => (
          <tr key={check.name}>
            <td>
              <code>{check.name}</code>
            </td>
            <td>
              <StatusBadge
                tone={check.status === "ok" ? "ok" : "error"}
                label={check.status === "ok" ? "OK" : "FAILED"}
              />
            </td>
            <td className="small muted">{check.latency_ms.toFixed(1)} ms</td>
            <td className="small">{check.error ?? <span className="muted">—</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
