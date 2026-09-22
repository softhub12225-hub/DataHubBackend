/**
 * One candidate: everything Step 5C.9 requires on screen, plus its two decisions.
 *
 * TWO DIMENSIONS, SHOWN APART
 * ===========================
 * Jurisdiction hints and qualification hints are rendered in separate rows and never
 * merged into one "scope" line. A Chinese applicant holding A-levels satisfies both, and
 * a combined display would invite a reviewer to treat them as one choice.
 *
 * THE SUGGESTION IS A SUGGESTION
 * ==============================
 * `suggested_criteria` is offered as a button that *fills the form*. It is never applied
 * on its own, and the construction that produced the hint is shown next to it so the
 * reviewer can see what the machine matched on before accepting its reading.
 *
 * NO DEFAULT, AND NO CLIENT-SIDE VERDICT
 * ======================================
 * Nothing is preselected. The confirm control appears only when the server returned a
 * `preview_token`; a blocked preview carries `null` and there is nothing to render. The
 * absence of the button is the server's verdict, not this component's.
 */

"use client";

import { useState } from "react";

import { EvidenceViewer } from "@/components/review/evidence-viewer";
import { Badge, Facts, Mono, stateTone } from "@/components/review/primitives";
import { ReviewApiError, reviewApi } from "@/lib/review/client";
import type {
  CandidateRow,
  ResolutionResult,
  ScopeCriterion,
  ScopePreviewResult,
} from "@/lib/review/types";

/** Which human state a dimension belongs to. Mirrors `precedence.py`; display only. */
function dimensionGroup(dimension: string, jurisdiction: string[]): "A" | "B" {
  return jurisdiction.includes(dimension) ? "A" : "B";
}

export function CandidateCard({
  institutionId,
  candidate,
  scopeStates,
  dimensions,
  jurisdictionDimensions,
  onApplied,
}: {
  institutionId: string;
  candidate: CandidateRow;
  scopeStates: string[];
  dimensions: string[];
  jurisdictionDimensions: string[];
  onApplied: () => void;
}) {
  const [state, setState] = useState<string | null>(null);
  const [criteria, setCriteria] = useState<ScopeCriterion[]>([]);
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<ScopePreviewResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<ResolutionResult | null>(null);
  const [showEvidence, setShowEvidence] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function invalidate() {
    setPreview(null);
    setConfirming(false);
    setError(null);
  }

  async function runPreview() {
    if (!state) return;
    setBusy(true);
    setError(null);
    try {
      setPreview(
        await reviewApi.previewScope(institutionId, candidate.candidate_id, state, criteria, reason),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Preview failed");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }

  async function runApply() {
    if (!preview?.preview_token || !state) return;
    setBusy(true);
    setError(null);
    try {
      const applied = await reviewApi.applyScope(
        institutionId,
        candidate.candidate_id,
        state,
        criteria,
        reason,
        preview.preview_token,
      );
      setResult(applied);
      setPreview(null);
      setConfirming(false);
      onApplied();
    } catch (cause) {
      if (cause instanceof ReviewApiError && cause.status === 409) {
        setPreview(null);
        setConfirming(false);
      }
      setError(cause instanceof Error ? cause.message : "Apply failed");
    } finally {
      setBusy(false);
    }
  }

  const scope = candidate.scope;

  return (
    <div className="rv-card">
      <div className="rv-card-head">
        <span className="rv-card-title">
          {candidate.source_ref} · {candidate.field_kind}
        </span>
        <Badge tone={candidate.scope.resolved ? "ok" : "warn"}>
          {scope.human_state}
        </Badge>
        <Badge tone={candidate.conflict.resolved ? "ok" : "warn"}>
          {candidate.conflict.verdict}
        </Badge>
        <Badge tone="neutral">{candidate.confidence}</Badge>
        <Badge tone={candidate.promotion_ready ? "ok" : "neutral"}>
          {candidate.promotion_ready ? "READY" : `${candidate.blockers.length} blockers`}
        </Badge>
      </div>

      <Facts
        rows={[
          ["candidate", <Mono key="c">{candidate.candidate_id}</Mono>],
          ["institution", candidate.institution],
          ["responsibility", candidate.responsibility],
          ["extracted value", <Mono key="v">{candidate.extracted_value}</Mono>],
          [
            "normalized value",
            <Mono key="n">{JSON.stringify(candidate.normalized_value)}</Mono>,
          ],
          ["heading / context", candidate.heading_path.join(" › ") || "—"],
          ["evidence excerpt", candidate.evidence_excerpt || "—"],
          ["requested URL", <Mono key="u">{candidate.requested_url}</Mono>],
          [
            "effective URL",
            candidate.effective_url ? (
              <Mono key="e">{candidate.effective_url}</Mono>
            ) : (
              <span className="rv-sub">no redirect recorded</span>
            ),
          ],
          [
            "source authority",
            <Badge key="sa" tone={stateTone(candidate.source_authority)}>
              {candidate.source_authority}
            </Badge>,
          ],
          ["rule version", <Mono key="rv">{candidate.rule_version}</Mono>],
          ["artifact version", <Mono key="av">{candidate.artifact_version}</Mono>],
          ["review state", candidate.review_decision],
          ["blockers", candidate.blockers.join(", ") || "none"],
        ]}
      />

      {/* ---------------- machine hints, split by dimension ---------------- */}
      <div className="rv-panel">
        <h2>Machine-detected scope hints</h2>
        <Facts
          rows={[
            ["resolution", scope.machine_resolution],
            [
              "A · jurisdiction / origin",
              scope.jurisdiction_hints.join(", ") || <span className="rv-sub">none</span>,
            ],
            [
              "B · qualification system",
              scope.qualification_hints.join(", ") || <span className="rv-sub">none</span>,
            ],
            [
              "applicant category",
              scope.category_hints.join(", ") || <span className="rv-sub">none</span>,
            ],
            [
              "construction that caused the hint",
              scope.machine_raw_text ? (
                <span key="raw">
                  <Mono>{scope.machine_raw_text}</Mono>{" "}
                  <span className="rv-sub">(from {scope.hint_source})</span>
                </span>
              ) : (
                <span className="rv-sub">nothing matched</span>
              ),
            ],
            [
              "existing scope",
              scope.human_actor
                ? `${scope.human_state} — ${scope.human_actor}`
                : "none recorded",
            ],
          ]}
        />
        <p className="rv-note">
          A category such as INTERNATIONAL is a fee status, not a country. It is never
          expanded into a list of nationalities the page did not name.
        </p>
      </div>

      <div className="rv-actions">
        <button
          type="button"
          className="rv-button"
          data-variant="secondary"
          onClick={() => setShowEvidence((shown) => !shown)}
        >
          {showEvidence ? "Hide evidence" : "View full evidence"}
        </button>
      </div>
      {showEvidence ? <EvidenceViewer pilotSourceId={candidate.pilot_source_id} /> : null}

      {/* ---------------- the decision ---------------- */}
      {!result ? (
        <>
          <div className="rv-field">
            <label>Human-selected scope — no default</label>
            <div className="rv-choices">
              {scopeStates.map((option) => (
                <button
                  key={option}
                  type="button"
                  className="rv-choice"
                  aria-pressed={state === option}
                  onClick={() => {
                    setState(option);
                    if (option === "UNSCOPED" || option === "UNIVERSAL_EXPLICIT") setCriteria([]);
                    invalidate();
                  }}
                >
                  {option}
                </button>
              ))}
            </div>
          </div>

          {scope.suggested_criteria.length > 0 ? (
            <div className="rv-actions">
              <button
                type="button"
                className="rv-choice"
                onClick={() => {
                  setCriteria(scope.suggested_criteria);
                  invalidate();
                }}
              >
                Use suggested construction (
                {scope.suggested_criteria
                  .map((c) => `${c.dimension_code}=${c.value}`)
                  .join(", ")}
                )
              </button>
            </div>
          ) : null}

          <div className="rv-field">
            <label>Criteria</label>
            <div className="rv-choices">
              {dimensions.map((dimension) => {
                const active = criteria.some((c) => c.dimension_code === dimension);
                return (
                  <button
                    key={dimension}
                    type="button"
                    className="rv-choice"
                    aria-pressed={active}
                    title={
                      dimensionGroup(dimension, jurisdictionDimensions) === "A"
                        ? "Dimension A — jurisdiction / origin"
                        : "Dimension B — qualification system"
                    }
                    onClick={() => {
                      const value = window.prompt(`Value for ${dimension}`) ?? "";
                      if (!value.trim()) return;
                      setCriteria((current) => [
                        ...current.filter((c) => c.dimension_code !== dimension),
                        { dimension_code: dimension, operator: "EQUALS", value: value.trim() },
                      ]);
                      invalidate();
                    }}
                  >
                    {dimensionGroup(dimension, jurisdictionDimensions)} · {dimension}
                  </button>
                );
              })}
            </div>
            {criteria.length > 0 ? (
              <p className="rv-note">
                selected:{" "}
                {criteria.map((c) => `${c.dimension_code}=${c.value}`).join(" AND ")}{" "}
                <button
                  type="button"
                  className="rv-choice"
                  onClick={() => {
                    setCriteria([]);
                    invalidate();
                  }}
                >
                  clear
                </button>
              </p>
            ) : null}
          </div>

          <div className="rv-field">
            <label htmlFor={`scope-reason-${candidate.candidate_id}`}>Reason (required)</label>
            <textarea
              id={`scope-reason-${candidate.candidate_id}`}
              className="rv-textarea"
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                invalidate();
              }}
            />
          </div>

          <div className="rv-actions">
            <button
              type="button"
              className="rv-button"
              onClick={runPreview}
              disabled={busy || !state || reason.trim().length === 0}
            >
              Preview scope decision
            </button>
          </div>
        </>
      ) : null}

      {error ? <p className="rv-error">{error}</p> : null}

      {/* ---------------- preview ---------------- */}
      {preview ? (
        <div className="rv-verdict" data-state={preview.valid ? "valid" : "blocked"}>
          <div className="rv-verdict-title">{preview.valid ? "VALID PREVIEW" : "BLOCKED"}</div>
          <div className="rv-beforeafter">
            <div className="rv-stat">
              <div className="rv-stat-label">BEFORE</div>
              <Badge tone="neutral">{preview.before.scope_state}</Badge>
              <div className="rv-sub">
                blockers: {preview.before.promotion_blockers.join(", ") || "none"}
              </div>
            </div>
            <div className="rv-stat">
              <div className="rv-stat-label">AFTER</div>
              <Badge tone="ok">{preview.after.scope_state}</Badge>
              <div className="rv-sub">
                blockers: {preview.after.promotion_blockers.join(", ") || "none"}
              </div>
            </div>
          </div>
          <Facts
            rows={[
              ["would append", preview.would_append ?? "(nothing)"],
              ["field_claim created", preview.creates_field_claim ? "YES" : "NO"],
              ["canonical changed", preview.modifies_canonical ? "YES" : "NO"],
            ]}
          />
          {!preview.valid ? (
            <ul className="rv-blockers">
              {preview.blockers.map((blocker) => (
                <li key={blocker.code}>
                  <span className="rv-blocker-code">{blocker.code}</span> — {blocker.message}
                </li>
              ))}
            </ul>
          ) : null}

          {preview.valid && preview.preview_token && !confirming ? (
            <div className="rv-actions">
              <button type="button" className="rv-button" onClick={() => setConfirming(true)}>
                Confirm &amp; Apply…
              </button>
            </div>
          ) : null}

          {preview.valid && confirming ? (
            <div className="rv-panel" style={{ marginTop: "0.75rem" }}>
              <h2>Confirm this scope decision</h2>
              <Facts
                rows={[
                  ["institution", candidate.institution],
                  ["source_ref", candidate.source_ref],
                  ["field", candidate.field_kind],
                  ["value", candidate.extracted_value],
                  ["scope", <strong key="s">{preview.state}</strong>],
                  [
                    "criteria",
                    preview.criteria.map((c) => `${c.dimension_code}=${c.value}`).join(" AND ") ||
                      "none",
                  ],
                  ["reason", preview.reason],
                  ["reviewer", preview.reviewer.display_name],
                ]}
              />
              <div className="rv-actions" style={{ marginTop: "0.75rem" }}>
                <button type="button" className="rv-button" onClick={runApply} disabled={busy}>
                  {busy ? "Applying…" : "Confirm & Apply"}
                </button>
                <button
                  type="button"
                  className="rv-button"
                  data-variant="secondary"
                  onClick={() => setConfirming(false)}
                  disabled={busy}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* ---------------- result ---------------- */}
      {result ? (
        <div className="rv-verdict" data-state="valid">
          <div className="rv-verdict-title">{result.operation}: {result.status}</div>
          <Facts
            rows={[
              ["actor", result.actor],
              ["previous state", result.previous_state],
              ["current state", result.current_state],
              ["promotion blockers now", result.promotion_blockers_now.join(", ") || "none"],
              ["audit sequence", result.audit_seq],
              [
                "audit chain",
                <Badge key="c" tone={result.audit_chain_ok ? "ok" : "error"}>
                  {result.audit_chain_ok ? "PASS" : "FAIL"}
                </Badge>,
              ],
              ["field_claim", result.field_claim],
              ["canonical changed", result.canonical_unchanged ? "NO" : "YES"],
            ]}
          />
          <p className="rv-note">Read back from the database after the write.</p>
        </div>
      ) : null}
    </div>
  );
}
