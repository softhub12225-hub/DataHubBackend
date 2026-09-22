/**
 * Section E: every pilot institution, with compact progress.
 *
 * Progress is shown as decided/total rather than as a percentage. A reviewer needs to know
 * *how many rows are left*, and 55% of eleven is not a number anyone can act on.
 */

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, Empty, ErrorText } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";
import type { InstitutionSummary } from "@/lib/review/types";

export default function InstitutionsPage() {
  const [rows, setRows] = useState<InstitutionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    reviewApi
      .institutions()
      .then(setRows)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load institutions"),
      );
  }, []);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!rows) return <Empty>Loading…</Empty>;

  return (
    <>
      <div className="rv-headline">
        <div>
          <h1>Institutions</h1>
          <p className="rv-sub">{rows.length} in the pilot</p>
        </div>
      </div>

      <section className="rv-panel">
        <table className="rv-table">
          <thead>
            <tr>
              <th>Institution</th>
              <th>Domains</th>
              <th>Source review</th>
              <th>Registered</th>
              <th>Responsibility review</th>
              <th>Promoted</th>
              <th>Unresolved</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.institution_id}>
                <td>
                  <Link href={`/review/institutions/${row.institution_id}`}>{row.name}</Link>
                </td>
                <td>
                  {row.verified_domains} / {row.total_domains} verified
                </td>
                <td>
                  {row.pilot_decided} / {row.pilot_rows} decided
                </td>
                <td>{row.registered_mappings}</td>
                <td>
                  {row.responsibility_decided} / {row.registered_mappings} decided
                </td>
                <td>{row.promoted}</td>
                <td>
                  {row.unresolved_sources > 0 ? (
                    <Badge tone="warn">{row.unresolved_sources}</Badge>
                  ) : (
                    <Badge tone="neutral">0</Badge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
