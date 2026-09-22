/**
 * Section P: the audit trail.
 *
 * THE SIX HISTORICAL ROWS
 * ======================
 * Registration used to append `PILOT_SOURCE_VERIFY` -- the same action name a human
 * verification decision uses. Six such rows exist from the real ANU pass, so a reader
 * counting that action sees twelve decisions where six were made.
 *
 * They are not rewritten. The log is append-only and hash-chained; editing history to make
 * a report tidier is exactly what the chain exists to prevent, and a corrected row would be
 * indistinguishable from a tampered one. So the server flags them
 * (`is_historical_registration`, detected by the `promoted_source_mapping_id` key its
 * after-state carries) and this table labels them in place. Rows appended from Step 5C.7L
 * onward use `PILOT_SOURCE_REGISTERED` and need no annotation.
 *
 * No secret material is displayed, and none is available to display: the API selects its
 * columns explicitly rather than returning whatever `audit_log` happens to hold.
 */

"use client";

import { useState } from "react";

import { Badge, Empty, Mono } from "@/components/review/primitives";
import type { AuditEntry } from "@/lib/review/types";

function StateCell({ state }: { state: Record<string, unknown> | null }) {
  if (!state || Object.keys(state).length === 0) return <span className="rv-sub">—</span>;
  return (
    <div className="rv-mono">
      {Object.entries(state).map(([key, value]) => (
        <div key={key}>
          {key}: {value === null ? "null" : String(value)}
        </div>
      ))}
    </div>
  );
}

export function AuditTable({ rows }: { rows: AuditEntry[] }) {
  const [showStates, setShowStates] = useState(false);
  const historical = rows.filter((row) => row.is_historical_registration).length;

  if (rows.length === 0) return <Empty>No audit entries for this institution.</Empty>;

  return (
    <section className="rv-panel">
      <div className="rv-headline">
        <h2>{rows.length} audit entries</h2>
        <label className="rv-toggle">
          <input
            type="checkbox"
            checked={showStates}
            onChange={(event) => setShowStates(event.target.checked)}
          />
          Show before/after state
        </label>
      </div>

      {historical > 0 ? (
        <p className="rv-warning">
          {historical} row{historical === 1 ? "" : "s"} below {historical === 1 ? "is" : "are"} a{" "}
          <strong>registration</strong> that was recorded under the action name{" "}
          <Mono>PILOT_SOURCE_VERIFY</Mono>, which is also the name a human verification
          decision uses. They are labelled rather than corrected: the log is append-only and
          hash-chained, so rewriting history here would be indistinguishable from tampering.
          Newer registrations use <Mono>PILOT_SOURCE_REGISTERED</Mono>.
        </p>
      ) : null}

      <table className="rv-table">
        <thead>
          <tr>
            <th>Seq</th>
            <th>When</th>
            <th>Group</th>
            <th>Action</th>
            <th>Actor</th>
            <th>Object</th>
            {showStates ? <th>Before</th> : null}
            {showStates ? <th>After</th> : null}
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.seq}>
              <td>{row.seq}</td>
              <td className="rv-sub">{new Date(row.occurred_at).toLocaleString()}</td>
              <td>{row.group}</td>
              <td>
                <span className="rv-inline-list">
                  <Mono>{row.action}</Mono>
                  {row.is_historical_registration ? (
                    <Badge tone="warn">REGISTRATION, MIS-NAMED</Badge>
                  ) : null}
                </span>
              </td>
              <td>
                {row.actor_name ?? <span className="rv-sub">{row.actor_type}</span>}
              </td>
              <td>
                {row.source_ref ? <strong>{row.source_ref}</strong> : null}
                <div className="rv-sub">{row.object_type}</div>
              </td>
              {showStates ? (
                <td>
                  <StateCell state={row.before_state} />
                </td>
              ) : null}
              {showStates ? (
                <td>
                  <StateCell state={row.after_state} />
                </td>
              ) : null}
              <td style={{ maxWidth: "22rem" }}>{row.reason ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
