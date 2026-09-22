/**
 * Section F: one institution, in tabs.
 *
 * Overview · Domains · Sources · Responsibilities · Evidence · Audit.
 *
 * The Sources tab makes five distinctions visible because each one changes what a reviewer
 * should conclude: VERIFIED, NEEDS_REVIEW, no stored body, UNCLASSIFIED, redirected, and
 * a duplicate responsibility on a page already submitted under another one. A row that is
 * NEEDS_REVIEW *because nothing could be fetched* is a different problem from one a human
 * looked at and deferred, and the table says which.
 */

"use client";

import { use, useCallback, useEffect, useState } from "react";

import { AuditTable } from "@/components/review/audit-table";
import { EvidenceViewer } from "@/components/review/evidence-viewer";
import { Badge, Empty, ErrorText, Mono, Panel, Stat, stateTone } from "@/components/review/primitives";
import { ResponsibilityCard } from "@/components/review/responsibility-card";
import { reviewApi } from "@/lib/review/client";
import type { AuditEntry, InstitutionDetail } from "@/lib/review/types";

const TABS = ["Overview", "Domains", "Sources", "Responsibilities", "Evidence", "Audit"] as const;
type Tab = (typeof TABS)[number];

export default function InstitutionPage({
  params,
}: {
  params: Promise<{ institutionId: string }>;
}) {
  const { institutionId } = use(params);
  const [tab, setTab] = useState<Tab>("Overview");
  const [data, setData] = useState<InstitutionDetail | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    reviewApi
      .institution(institutionId)
      .then(setData)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load the institution"),
      );
  }, [institutionId]);

  useEffect(load, [load]);

  useEffect(() => {
    if (tab !== "Audit" || audit) return;
    reviewApi
      .audit(institutionId)
      .then(setAudit)
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load the audit trail"),
      );
  }, [tab, audit, institutionId]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!data) return <Empty>Loading…</Empty>;

  const mappingToPilot = new Map(
    data.sources.filter((row) => row.mapping_id).map((row) => [row.mapping_id!, row.pilot_source_id]),
  );

  return (
    <>
      <div className="rv-headline">
        <div>
          <h1>{data.name}</h1>
          <p className="rv-sub">
            {data.summary.verified_domains}/{data.summary.total_domains} verified domains ·{" "}
            {data.summary.pilot_decided}/{data.summary.pilot_rows} pilot decisions ·{" "}
            {data.summary.registered_mappings} registered mappings
          </p>
        </div>
      </div>

      <div className="rv-tabs" role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            className="rv-tab"
            aria-selected={tab === name}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === "Overview" ? (
        <Panel title="Progress">
          <div className="rv-stats">
            <Stat label="verified domains" value={data.summary.verified_domains} />
            <Stat label="pilot decisions" value={data.summary.pilot_decided} />
            <Stat label="registered mappings" value={data.summary.registered_mappings} />
            <Stat label="responsibility decisions" value={data.summary.responsibility_decided} />
            <Stat label="promoted mappings" value={data.summary.promoted} />
            <Stat label="unresolved sources" value={data.summary.unresolved_sources} />
          </div>
        </Panel>
      ) : null}

      {tab === "Domains" ? (
        <Panel title={`${data.domains.length} domain(s)`}>
          <table className="rv-table">
            <thead>
              <tr>
                <th>Host</th>
                <th>Status</th>
                <th>Active</th>
                <th>Verified by</th>
              </tr>
            </thead>
            <tbody>
              {data.domains.map((domain) => (
                <tr key={domain.domain_id}>
                  <td>
                    <Mono>{domain.host}</Mono>
                  </td>
                  <td>
                    <Badge tone={stateTone(domain.verification_status)}>
                      {domain.verification_status}
                    </Badge>
                  </td>
                  <td>{domain.is_active ? "yes" : "no"}</td>
                  <td>{domain.verified_by_name ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}

      {tab === "Sources" ? (
        <Panel title={`${data.sources.length} workbook row(s)`}>
          <table className="rv-table">
            <thead>
              <tr>
                <th>Ref</th>
                <th>Responsibility</th>
                <th>State</th>
                <th>Flags</th>
                <th>Registered</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {data.sources.map((source) => (
                <tr key={source.pilot_source_id}>
                  <td>{source.source_ref}</td>
                  <td>
                    {source.responsibility}
                    {source.degree_scope ? (
                      <span className="rv-sub"> · {source.degree_scope}</span>
                    ) : null}
                  </td>
                  <td>
                    <Badge tone={stateTone(source.verification_state)}>
                      {source.verification_state}
                    </Badge>
                  </td>
                  <td>
                    <span className="rv-inline-list">
                      {source.responsibility === "UNCLASSIFIED" ? (
                        <Badge tone="warn">UNCLASSIFIED</Badge>
                      ) : null}
                      {source.redirected ? <Badge tone="warn">REDIRECTED</Badge> : null}
                      {!source.body_available ? (
                        <Badge tone="warn">BODY_EVIDENCE_NOT_AVAILABLE</Badge>
                      ) : null}
                      {source.duplicate_of ? (
                        <Badge tone="neutral">SAME PAGE AS {source.duplicate_of}</Badge>
                      ) : null}
                    </span>
                  </td>
                  <td>{source.mapping_id ? "yes" : "no"}</td>
                  <td>
                    <button
                      type="button"
                      className="rv-choice"
                      onClick={() => {
                        setSelectedSource(source.pilot_source_id);
                        setTab("Evidence");
                      }}
                    >
                      View evidence
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}

      {tab === "Responsibilities" ? (
        <div className="rv-page">
          <p className="rv-sub">
            Registration asserts only that this URL is a candidate for this responsibility.
            The earlier pilot-source decision is <strong>not</strong> carried across.
          </p>
          {data.responsibilities.length === 0 ? (
            <Empty>No registered mappings yet.</Empty>
          ) : (
            data.responsibilities.map((card) => (
              <ResponsibilityCard
                key={card.mapping_id}
                card={card}
                manifestSha256={data.manifest_sha256}
                decisionsOffered={data.decisions_offered}
                pilotSourceId={mappingToPilot.get(card.mapping_id) ?? null}
                onApplied={load}
              />
            ))
          )}
        </div>
      ) : null}

      {tab === "Evidence" ? (
        selectedSource ? (
          <EvidenceViewer pilotSourceId={selectedSource} />
        ) : (
          <Empty>Pick a row on the Sources tab to see its stored evidence.</Empty>
        )
      ) : null}

      {tab === "Audit" ? (
        audit ? (
          <AuditTable rows={audit} />
        ) : (
          <Empty>Loading the audit trail…</Empty>
        )
      ) : null}
    </>
  );
}
