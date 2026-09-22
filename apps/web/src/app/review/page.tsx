/**
 * Section D: the dashboard.
 *
 * Every number here is a live server-side count. None is cached and none is derived in the
 * browser -- a stale safety count is worse than no safety count, because it reads as
 * reassurance.
 *
 * The manifest panel is green only when the file on disk still hashes to the approved
 * digest. That is not decorative: a decision can only be bound to a manifest row while the
 * manifest matches, so red here means every decision path is about to refuse.
 */

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, Empty, ErrorText, Mono, Panel, Stat } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";
import type { DashboardData } from "@/lib/review/types";

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    reviewApi
      .dashboard()
      .then(setData)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load the dashboard"),
      );
  }, []);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!data) return <Empty>Loading…</Empty>;

  return (
    <>
      <div className="rv-headline">
        <div>
          <h1>Review dashboard</h1>
          <p className="rv-sub">
            Signed in as <strong>{data.reviewer.display_name}</strong> ·{" "}
            {data.reviewer.roles.join(", ") || "no roles"}
          </p>
        </div>
      </div>

      {data.blockers.length > 0 ? (
        <section className="rv-verdict" data-state="blocked">
          <div className="rv-verdict-title">ATTENTION</div>
          <ul className="rv-blockers">
            {data.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <Panel title="Pilot">
        <div className="rv-stats">
          <Stat label="institutions" value={data.pilot.institutions} />
          <Stat label="reviewed-domain rows" value={data.pilot.reviewed_domain_rows} />
          <Stat label="physical sources" value={data.pilot.physical_sources} />
          <Stat label="responsibility rows" value={data.pilot.responsibility_rows} />
        </div>
      </Panel>

      <Panel title="Current trust">
        <div className="rv-stats">
          <Stat label="verified domains" value={data.trust.verified_domains} />
          <Stat label="pilot decisions" value={data.trust.pilot_decisions} />
          <Stat label="source mappings" value={data.trust.source_mappings} />
          <Stat label="responsibility decisions" value={data.trust.responsibility_decisions} />
          <Stat label="promoted mappings" value={data.trust.promoted_mappings} />
          <Stat label="eligible sources" value={data.trust.eligible_sources} />
        </div>
      </Panel>

      <Panel title="Safety — these must be zero">
        <div className="rv-stats">
          <Stat label="field_claim" value={data.safety.field_claim} expectZero />
          <Stat label="change_proposal" value={data.safety.change_proposal} expectZero />
          <Stat label="change_event" value={data.safety.change_event} expectZero />
          <Stat label="canonical rows" value={data.safety.canonical_rows} expectZero />
        </div>
        <p className="rv-note">
          Nothing in the pilot has crossed into the canonical plane. A non-zero count here
          means something published without passing through review.
        </p>
      </Panel>

      <Panel title="Frozen review packages">
        <table className="rv-table">
          <thead>
            <tr>
              <th>Package</th>
              <th>Rows</th>
              <th>Status</th>
              <th>Digest on disk</th>
            </tr>
          </thead>
          <tbody>
            {data.manifests.map((manifest) => (
              <tr key={manifest.name}>
                <td>{manifest.name}</td>
                <td>{manifest.rows}</td>
                <td>
                  {manifest.matches ? (
                    <Badge tone="ok">MATCHES APPROVED</Badge>
                  ) : (
                    <Badge tone="error">
                      {manifest.present ? "DOES NOT MATCH" : "MISSING"}
                    </Badge>
                  )}
                </td>
                <td>
                  <Mono>{(manifest.actual_sha256 ?? "—").slice(0, 16)}…</Mono>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Audit">
        <div className="rv-stats">
          <Stat label="audit rows" value={data.audit.rows} />
          <Stat label="hash chain" value={data.audit.chain_ok ? "PASS" : "FAIL"} />
        </div>
      </Panel>

      <p>
        <Link href="/review/institutions">Review institutions →</Link>
      </p>
    </>
  );
}
