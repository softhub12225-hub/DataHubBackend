/**
 * Step 5C.9: the candidate scope and conflict review queue.
 *
 * SUPERSEDED CANDIDATES ARE NOT A FILTER
 * ======================================
 * There is no "show superseded" toggle, because the server never returns them. A toggle
 * would eventually be toggled, and a candidate whose rule or document version has moved
 * on is not a thing a reviewer should be able to decide.
 *
 * FILTERING HAPPENS ON THE SERVER
 * ===============================
 * Every filter is a query parameter, so the count on screen is a count of what the server
 * would act on. Filtering a client-side array would let the two disagree the moment a
 * decision changes a candidate's state.
 *
 * THE BLOCKER MATRIX IS SHOWN FIRST
 * =================================
 * A reviewer resolving scope needs to know whether scope is the only thing in the way.
 * For the ANU pilot it is not -- no source has been promoted, so `SOURCE_NOT_ELIGIBLE`
 * sits on every candidate and no amount of scope work will clear it. Putting the matrix
 * at the top makes that visible before anyone spends an afternoon on it.
 */

"use client";

import { use, useCallback, useEffect, useState } from "react";

import { CandidateCard } from "@/components/review/candidate-card";
import { Badge, Empty, ErrorText, Panel, Stat } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";
import type { CandidateGroup, CandidateQueue, ReadinessMatrix } from "@/lib/review/types";

const FIELD_KINDS = [
  "",
  "ADMISSION_REQUIREMENT",
  "APPLICATION_DEADLINE",
  "LANGUAGE_TEST",
  "LANGUAGE_OVERALL_SCORE",
  "TUITION",
];
const CONFIDENCE = ["", "HIGH", "MEDIUM", "LOW"];
const REVIEW_STATES = ["", "UNREVIEWED", "ACCEPTED", "REJECTED", "NEEDS_CONTEXT"];

export default function CandidatesPage({
  params,
}: {
  params: Promise<{ institutionId: string }>;
}) {
  const { institutionId } = use(params);
  const [queue, setQueue] = useState<CandidateQueue | null>(null);
  const [groups, setGroups] = useState<CandidateGroup[] | null>(null);
  const [matrix, setMatrix] = useState<ReadinessMatrix | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [sourceRef, setSourceRef] = useState("");
  const [fieldKind, setFieldKind] = useState("");
  const [confidence, setConfidence] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [scopeUnresolved, setScopeUnresolved] = useState(false);
  const [conflictUnresolved, setConflictUnresolved] = useState(false);
  const [readyOnly, setReadyOnly] = useState(false);

  const load = useCallback(() => {
    const filters: Record<string, string> = {};
    if (sourceRef) filters.source_ref = sourceRef;
    if (fieldKind) filters.field_kind = fieldKind;
    if (confidence) filters.confidence = confidence;
    if (reviewStatus) filters.review_status = reviewStatus;
    if (scopeUnresolved) filters.scope_unresolved = "true";
    if (conflictUnresolved) filters.conflict_unresolved = "true";
    if (readyOnly) filters.promotion_ready = "true";

    Promise.all([
      reviewApi.candidateQueue(institutionId, filters),
      reviewApi.candidateGroups(institutionId),
      reviewApi.candidateReadiness(institutionId),
    ])
      .then(([q, g, m]) => {
        setQueue(q);
        setGroups(g);
        setMatrix(m);
        setError(null);
      })
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "Could not load the queue"),
      );
  }, [
    institutionId,
    sourceRef,
    fieldKind,
    confidence,
    reviewStatus,
    scopeUnresolved,
    conflictUnresolved,
    readyOnly,
  ]);

  useEffect(load, [load]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!queue || !matrix) return <Empty>Loading…</Empty>;

  return (
    <>
      <div className="rv-headline">
        <div>
          <h1>Candidate scope &amp; conflict review</h1>
          <p className="rv-sub">
            {queue.institution} · showing {queue.shown} of {queue.total_current} current
            candidates. Superseded candidates are never returned.
          </p>
        </div>
      </div>

      <Panel title="Promotion readiness">
        <div className="rv-stats">
          <Stat label="current candidates" value={matrix.current_candidates} />
          <Stat label="promotion ready" value={matrix.ready} />
          <Stat label="scope unresolved" value={matrix.scope_unresolved} />
          <Stat label="conflict unresolved" value={matrix.conflict_unresolved} />
        </div>
        <table className="rv-table" style={{ marginTop: "0.75rem" }}>
          <thead>
            <tr>
              <th>Blocker</th>
              <th>Candidates</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(matrix.blocker_counts).map(([code, count]) => (
              <tr key={code}>
                <td>
                  <Badge tone="warn">{code}</Badge>
                </td>
                <td>{count}</td>
              </tr>
            ))}
            {Object.keys(matrix.blocker_counts).length === 0 ? (
              <tr>
                <td colSpan={2} className="rv-sub">
                  no blockers
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </Panel>

      <Panel title="Filters">
        <div className="rv-actions">
          <input
            className="rv-input"
            style={{ maxWidth: "10rem" }}
            placeholder="source_ref"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
          />
          <select
            className="rv-input"
            style={{ maxWidth: "14rem" }}
            value={fieldKind}
            onChange={(event) => setFieldKind(event.target.value)}
          >
            {FIELD_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind || "any field type"}
              </option>
            ))}
          </select>
          <select
            className="rv-input"
            style={{ maxWidth: "10rem" }}
            value={confidence}
            onChange={(event) => setConfidence(event.target.value)}
          >
            {CONFIDENCE.map((band) => (
              <option key={band} value={band}>
                {band || "any confidence"}
              </option>
            ))}
          </select>
          <select
            className="rv-input"
            style={{ maxWidth: "12rem" }}
            value={reviewStatus}
            onChange={(event) => setReviewStatus(event.target.value)}
          >
            {REVIEW_STATES.map((value) => (
              <option key={value} value={value}>
                {value || "any review status"}
              </option>
            ))}
          </select>
          <label className="rv-toggle">
            <input
              type="checkbox"
              checked={scopeUnresolved}
              onChange={(event) => setScopeUnresolved(event.target.checked)}
            />
            scope unresolved
          </label>
          <label className="rv-toggle">
            <input
              type="checkbox"
              checked={conflictUnresolved}
              onChange={(event) => setConflictUnresolved(event.target.checked)}
            />
            conflict unresolved
          </label>
          <label className="rv-toggle">
            <input
              type="checkbox"
              checked={readyOnly}
              onChange={(event) => setReadyOnly(event.target.checked)}
            />
            promotion ready
          </label>
        </div>
      </Panel>

      <Panel title={`Groups (${groups?.length ?? 0})`}>
        {groups && groups.length > 0 ? (
          <table className="rv-table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Verdict</th>
                <th>Members</th>
                <th>Resolution</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.context_fingerprint}>
                  <td>{group.field_kind}</td>
                  <td>
                    <Badge tone={group.needs_resolution ? "warn" : "neutral"}>
                      {group.verdict}
                    </Badge>
                  </td>
                  <td>{group.members.length}</td>
                  <td>
                    {group.action ? (
                      <span>
                        {group.action}
                        <span className="rv-sub"> · {group.actor}</span>
                      </span>
                    ) : (
                      <span className="rv-sub">unresolved</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No groups over the current candidates.</Empty>
        )}
        <p className="rv-note">
          No winner is chosen or implied. A group with several values reports every one of
          them, and a reviewer decides.
        </p>
      </Panel>

      <div className="rv-page">
        {queue.candidates.length === 0 ? (
          <Empty>No candidates match these filters.</Empty>
        ) : (
          queue.candidates.map((candidate) => (
            <CandidateCard
              key={candidate.candidate_id}
              institutionId={institutionId}
              candidate={candidate}
              scopeStates={queue.scope_states}
              dimensions={queue.dimensions}
              jurisdictionDimensions={queue.jurisdiction_dimensions}
              onApplied={load}
            />
          ))
        )}
      </div>
    </>
  );
}
