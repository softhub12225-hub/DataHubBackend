/**
 * Section Q: recent real operations and how they landed.
 *
 * WHY PREVIEWS ARE NOT LISTED HERE
 * ================================
 * A preview writes nothing. Recording one would put an entry in the operator's history for
 * something that never happened, and the list's whole value is that everything on it is
 * real. So this is built from `audit_log` -- the record of operations that actually
 * occurred -- and each row carries the audit sequence a reader can verify against the
 * chain.
 *
 * The live preview for a decision in progress is shown beside that decision, on the
 * institution page, where it belongs.
 */

"use client";

import { useEffect, useState } from "react";

import { Badge, Empty, ErrorText, Mono } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";
import type { OperationEntry } from "@/lib/review/types";

export default function OperationsPage() {
  const [rows, setRows] = useState<OperationEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    reviewApi
      .operations(100)
      .then(setRows)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load operations"),
      );
  }, []);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!rows) return <Empty>Loading…</Empty>;

  return (
    <>
      <div className="rv-headline">
        <div>
          <h1>Operation history</h1>
          <p className="rv-sub">
            Every real operation, newest first. Previews are absent by design — they write
            nothing, and listing them would show events that never happened.
          </p>
        </div>
      </div>

      <section className="rv-panel">
        <table className="rv-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Operation</th>
              <th>Group</th>
              <th>Actor</th>
              <th>Source</th>
              <th>Applied</th>
              <th>Audit seq</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.seq}>
                <td className="rv-sub">{new Date(row.occurred_at).toLocaleString()}</td>
                <td>
                  <span className="rv-inline-list">
                    <Mono>{row.operation}</Mono>
                    {row.is_historical_registration ? (
                      <Badge tone="warn">REGISTRATION, MIS-NAMED</Badge>
                    ) : null}
                  </span>
                </td>
                <td>{row.group}</td>
                <td>{row.actor_name ?? "—"}</td>
                <td>{row.source_ref ?? "—"}</td>
                <td>
                  <Badge tone="ok">APPLIED</Badge>
                </td>
                <td>{row.audit_seq}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
