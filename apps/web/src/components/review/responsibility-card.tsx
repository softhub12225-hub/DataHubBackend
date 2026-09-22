/**
 * Sections H, I, J, K, L, N and O for one registered mapping.
 *
 * THE FLOW IS DELIBERATELY THREE STEPS
 * ====================================
 * Choose → Preview → Confirm & Apply. Each is a separate act:
 *
 * - **Choose** has no default. A pre-selected VERIFIED is an invitation to click through,
 *   and the earlier pilot-source VERIFIED is explicitly *not* inherited here (section I):
 *   "this URL is worth registering" and "this page is authoritative for this
 *   responsibility" are different judgements.
 * - **Preview** calls the server, which runs the identical validation the apply runs. The
 *   button below is enabled because the server returned `valid`, never because this
 *   component reasoned about it.
 * - **Confirm** restates institution, source_ref, responsibility, decision, reason and
 *   both states, and requires a second explicit action. Not a `window.confirm`: a native
 *   dialog cannot show any of that, and section L requires it to be shown.
 *
 * ANY EDIT INVALIDATES THE PREVIEW
 * ================================
 * Changing the decision or the reason clears the token locally. That is a convenience, not
 * a safeguard -- the server recomputes the fingerprint from the live database on apply and
 * refuses with PREVIEW_STALE regardless. Both exist because the local clear keeps the
 * screen honest, and the server check is what actually holds.
 */

"use client";

import { useState } from "react";

import { EvidenceViewer } from "@/components/review/evidence-viewer";
import { Badge, Facts, Mono, stateTone } from "@/components/review/primitives";
import { ReviewApiError, reviewApi } from "@/lib/review/client";
import type {
  ApplyResult,
  MappingState,
  PreviewResult,
  PromotionApplyResult,
  PromotionPreviewResult,
  ResponsibilityCard as CardData,
} from "@/lib/review/types";

function StatePanel({ title, state }: { title: string; state: MappingState }) {
  return (
    <div className="rv-stat">
      <div className="rv-stat-label">{title}</div>
      <div style={{ marginTop: "0.3rem", display: "grid", gap: "0.2rem" }}>
        <Badge tone={stateTone(state.verification_status)}>{state.verification_status}</Badge>
        <span className="rv-sub">{state.publication_eligibility}</span>
        <span className="rv-sub">verified_by: {state.verified_by_name ?? "NULL"}</span>
      </div>
    </div>
  );
}

export function ResponsibilityCard({
  card,
  manifestSha256,
  decisionsOffered,
  pilotSourceId,
  onApplied,
}: {
  card: CardData;
  manifestSha256: string;
  decisionsOffered: string[];
  pilotSourceId: string | null;
  onApplied: () => void;
}) {
  const [decision, setDecision] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<ApplyResult | null>(null);
  const [promotion, setPromotion] = useState<PromotionPreviewResult | null>(null);
  const [promotionReason, setPromotionReason] = useState("");
  const [confirmingPromotion, setConfirmingPromotion] = useState(false);
  const [promotionResult, setPromotionResult] = useState<PromotionApplyResult | null>(
    null,
  );
  const [showEvidence, setShowEvidence] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function invalidate() {
    setPreview(null);
    setConfirming(false);
    setError(null);
  }

  async function runPreview() {
    if (!decision) return;
    setBusy(true);
    setError(null);
    try {
      setPreview(await reviewApi.previewResponsibility(card.mapping_id, decision, reason, manifestSha256));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Preview failed");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }

  async function runApply() {
    if (!preview?.preview_token || !decision) return;
    setBusy(true);
    setError(null);
    try {
      const applied = await reviewApi.applyResponsibility(
        card.mapping_id,
        decision,
        reason,
        manifestSha256,
        preview.preview_token,
      );
      setResult(applied);
      setPreview(null);
      setConfirming(false);
      onApplied();
    } catch (cause) {
      if (cause instanceof ReviewApiError && cause.status === 409) {
        // PREVIEW_STALE. Drop the token and make the reviewer look again.
        setPreview(null);
        setConfirming(false);
      }
      setError(cause instanceof Error ? cause.message : "Apply failed");
    } finally {
      setBusy(false);
    }
  }

  async function runPromotionPreview() {
    setBusy(true);
    setError(null);
    try {
      setPromotion(await reviewApi.previewPromotion(card.mapping_id));
      setConfirmingPromotion(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Promotion preview failed");
    } finally {
      setBusy(false);
    }
  }

  async function runPromotionApply() {
    if (!promotion?.preview_token) return;
    setBusy(true);
    setError(null);
    try {
      const applied = await reviewApi.applyPromotion(
        card.mapping_id,
        promotionReason,
        promotion.preview_token,
      );
      setPromotionResult(applied);
      setPromotion(null);
      setConfirmingPromotion(false);
      onApplied();
    } catch (cause) {
      if (cause instanceof ReviewApiError && cause.status === 409) {
        // PREVIEW_STALE: drop the token so the reviewer has to look again.
        setPromotion(null);
        setConfirmingPromotion(false);
      }
      setError(cause instanceof Error ? cause.message : "Promotion failed");
    } finally {
      setBusy(false);
    }
  }

  const decided = card.verified_by_name !== null;

  return (
    <div className="rv-card">
      <div className="rv-card-head">
        <span className="rv-card-title">
          {card.source_ref} · {card.responsibility}
        </span>
        <Badge tone={stateTone(card.verification_status)}>{card.verification_status}</Badge>
        <Badge tone="neutral">{card.publication_eligibility}</Badge>
        {card.redirected ? <Badge tone="warn">REDIRECTED</Badge> : null}
        {!card.body_available ? <Badge tone="warn">NO BODY EVIDENCE</Badge> : null}
        <Badge tone={card.manifest_bound ? "ok" : "error"}>
          {card.manifest_bound ? "MANIFEST BOUND" : "NOT BOUND"}
        </Badge>
      </div>

      <Facts
        rows={[
          ["mapping id", <Mono key="m">{card.mapping_id}</Mono>],
          ["requested URL", <Mono key="u">{card.requested_url}</Mono>],
          [
            "effective URL",
            card.effective_url ? (
              <Mono key="e">{card.effective_url}</Mono>
            ) : (
              <span className="rv-sub">no redirect recorded</span>
            ),
          ],
          [
            "requested host",
            <span key="rh" className="rv-inline-list">
              <Mono>{card.requested_host}</Mono>
              <Badge tone={card.requested_host_authority === "VERIFIED_OFFICIAL" ? "ok" : "error"}>
                {card.requested_host_authority}
              </Badge>
            </span>,
          ],
          [
            "effective host",
            <span key="eh" className="rv-inline-list">
              <Mono>{card.effective_host}</Mono>
              <Badge tone={card.effective_host_authority === "VERIFIED_OFFICIAL" ? "ok" : "error"}>
                {card.effective_host_authority}
              </Badge>
            </span>,
          ],
          ["decided by", card.verified_by_name ?? "—"],
          ["promoted", card.promoted_source_id ? <Mono key="p">{card.promoted_source_id}</Mono> : "NO"],
        ]}
      />
      {card.manifest_blocker ? <p className="rv-warning">{card.manifest_blocker}</p> : null}

      <div className="rv-actions">
        {pilotSourceId ? (
          <button
            type="button"
            className="rv-button"
            data-variant="secondary"
            onClick={() => setShowEvidence((shown) => !shown)}
          >
            {showEvidence ? "Hide evidence" : "View evidence"}
          </button>
        ) : null}
      </div>
      {showEvidence && pilotSourceId ? <EvidenceViewer pilotSourceId={pilotSourceId} /> : null}

      {/* ---------------- section I: the decision controls ---------------- */}
      {!result ? (
        <>
          <div className="rv-field">
            <label>Decision — no default; the pilot-source decision is not inherited</label>
            <div className="rv-choices">
              {decisionsOffered.map((choice) => (
                <button
                  key={choice}
                  type="button"
                  className="rv-choice"
                  aria-pressed={decision === choice}
                  onClick={() => {
                    setDecision(choice);
                    invalidate();
                  }}
                >
                  {choice}
                </button>
              ))}
            </div>
          </div>
          <div className="rv-field">
            <label htmlFor={`reason-${card.mapping_id}`}>Reason (required)</label>
            <textarea
              id={`reason-${card.mapping_id}`}
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
              disabled={busy || !decision || reason.trim().length === 0}
            >
              Preview decision
            </button>
            {decided ? (
              <span className="rv-sub">
                Already decided by {card.verified_by_name}. A preview will say whether this
                would change anything.
              </span>
            ) : null}
          </div>
        </>
      ) : null}

      {error ? <p className="rv-error">{error}</p> : null}

      {/* ---------------- section K: the preview panel -------------------- */}
      {preview ? (
        <div className="rv-verdict" data-state={preview.valid ? "valid" : "blocked"}>
          <div className="rv-verdict-title">{preview.valid ? "VALID PREVIEW" : "BLOCKED"}</div>

          <div className="rv-beforeafter">
            <StatePanel title="BEFORE" state={preview.before} />
            {preview.after ? <StatePanel title="AFTER" state={preview.after} /> : null}
          </div>

          <Facts
            rows={[
              [
                "manifest",
                preview.binding ? (
                  <Badge key="mb" tone="ok">MATCH</Badge>
                ) : (
                  <Badge key="mb" tone="error">NO BINDING</Badge>
                ),
              ],
              [
                "requested authority",
                <Badge key="ra" tone={preview.authority?.requested_authority === "VERIFIED_OFFICIAL" ? "ok" : "error"}>
                  {preview.authority?.requested_authority ?? "—"}
                </Badge>,
              ],
              [
                "effective authority",
                <Badge key="ea" tone={preview.authority?.effective_authority === "VERIFIED_OFFICIAL" ? "ok" : "error"}>
                  {preview.authority?.effective_authority ?? "—"}
                </Badge>,
              ],
              ["would append", preview.would_append ?? "(nothing)"],
              ["would create field_claim", preview.creates_field_claim ? "YES" : "NO"],
              ["would modify canonical", preview.modifies_canonical ? "YES" : "NO"],
              ["promotes", preview.promotes ? "YES" : "NO"],
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

          {preview.valid && !confirming ? (
            <div className="rv-actions">
              <button type="button" className="rv-button" onClick={() => setConfirming(true)}>
                Confirm &amp; Apply…
              </button>
            </div>
          ) : null}

          {/* ------------- section L: the confirmation screen ------------- */}
          {preview.valid && confirming ? (
            <div className="rv-panel" style={{ marginTop: "0.75rem" }}>
              <h2>Confirm this decision</h2>
              <Facts
                rows={[
                  ["institution", preview.binding?.institution ?? "—"],
                  ["source_ref", preview.binding?.source_ref ?? "—"],
                  ["responsibility", preview.binding?.responsibility ?? "—"],
                  ["decision", <strong key="d">{preview.decision}</strong>],
                  ["reason", preview.reason],
                  [
                    "before",
                    `${preview.before.verification_status} / ${preview.before.publication_eligibility}`,
                  ],
                  [
                    "after",
                    preview.after
                      ? `${preview.after.verification_status} / ${preview.after.publication_eligibility}`
                      : "—",
                  ],
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

      {/* ---------------- section N: the result panel --------------------- */}
      {result ? (
        <div className="rv-verdict" data-state="valid">
          <div className="rv-verdict-title">{result.status}</div>
          <Facts
            rows={[
              ["operation", result.operation],
              ["actor", result.actor],
              ["source", result.source_ref],
              ["responsibility", result.responsibility],
              ["previous state", result.previous_state.verification_status],
              ["current state", result.current_state.verification_status],
              ["audit sequence", result.audit_seq],
              [
                "audit chain",
                <Badge key="c" tone={result.audit_chain_ok ? "ok" : "error"}>
                  {result.audit_chain_ok ? "PASS" : "FAIL"}
                </Badge>,
              ],
              ["publication eligibility", result.publication_eligibility],
              ["promoted", result.promoted ? "YES" : "NO"],
              ["field_claim created", result.field_claim_created ? "YES" : "NO"],
              ["canonical changed", result.canonical_unchanged ? "NO" : "YES"],
            ]}
          />
          <p className="rv-note">Read back from the database after the write.</p>
        </div>
      ) : null}

      {/* ---------------- section O: promotion controls ------------------- */}
      {card.verification_status === "VERIFIED_OFFICIAL" ? (
        <>
          <div className="rv-actions">
            <button
              type="button"
              className="rv-button"
              data-variant="secondary"
              onClick={runPromotionPreview}
              disabled={busy}
            >
              Preview promotion
            </button>
          </div>
          {promotion ? (
            <div className="rv-verdict" data-state={promotion.valid ? "valid" : "blocked"}>
              <div className="rv-verdict-title">
                {promotion.valid ? "PROMOTION WOULD PROCEED" : "PROMOTION BLOCKED"}
              </div>
              <Facts
                rows={[
                  ["mapping", <Mono key="m">{promotion.mapping_id}</Mono>],
                  ["responsibility", promotion.responsibility],
                  [
                    "manifest binding",
                    <Badge key="b" tone={promotion.manifest_bound ? "ok" : "error"}>
                      {promotion.manifest_bound ? "BOUND" : "NOT BOUND"}
                    </Badge>,
                  ],
                  [
                    "requested domain authority",
                    promotion.authority.requested_authority,
                  ],
                  [
                    "effective domain authority",
                    promotion.authority.effective_authority,
                  ],
                  [
                    "field bindings that would be created",
                    promotion.field_bindings_that_would_be_created.length === 0
                      ? "none"
                      : promotion.field_bindings_that_would_be_created
                          .map((binding) => `${binding.entity_type}.${binding.field_path}`)
                          .join(", "),
                  ],
                  [
                    "source eligibility",
                    `${promotion.source_eligibility_before ?? "—"} → ${
                      promotion.source_eligibility_after ?? "—"
                    }`,
                  ],
                  [
                    "promoted_source_id",
                    `${promotion.promoted_source_id_before ?? "NULL"} → ${
                      promotion.promoted_source_id_after ?? "NULL"
                    }`,
                  ],
                  ["field_claim remains 0", promotion.field_claim_remains_zero ? "YES" : "NO"],
                  ["canonical unchanged", promotion.canonical_unchanged ? "YES" : "NO"],
                ]}
              />
              {promotion.blockers.length > 0 ? (
                <ul className="rv-blockers">
                  {promotion.blockers.map((blocker) => (
                    <li key={blocker.code}>
                      <span className="rv-blocker-code">{blocker.code}</span> — {blocker.message}
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="rv-note">{promotion.note}</p>

              {/*
                The confirm control appears only when the SERVER issued a token. A blocked
                preview carries preview_token: null, so there is nothing to render and
                nothing for the client to disable. The absence of the button is the
                server verdict, not a judgement this component made.
              */}
              {promotion.valid && promotion.preview_token ? (
                <>
                  <div className="rv-field" style={{ marginTop: "0.75rem" }}>
                    <label htmlFor={`promo-reason-${card.mapping_id}`}>
                      Reason for promoting (required)
                    </label>
                    <textarea
                      id={`promo-reason-${card.mapping_id}`}
                      className="rv-textarea"
                      value={promotionReason}
                      onChange={(event) => {
                        setPromotionReason(event.target.value);
                        setConfirmingPromotion(false);
                      }}
                    />
                  </div>
                  {!confirmingPromotion ? (
                    <div className="rv-actions">
                      <button
                        type="button"
                        className="rv-button"
                        onClick={() => setConfirmingPromotion(true)}
                        disabled={busy || promotionReason.trim().length === 0}
                      >
                        Confirm &amp; Apply Promotion…
                      </button>
                    </div>
                  ) : (
                    <div className="rv-panel" style={{ marginTop: "0.75rem" }}>
                      <h2>Confirm this promotion</h2>
                      <Facts
                        rows={[
                          ["source", promotion.source_ref],
                          ["responsibility", promotion.responsibility],
                          ["reason", promotionReason],
                          [
                            "source eligibility",
                            `${promotion.source_eligibility_before ?? "—"} → ${
                              promotion.source_eligibility_after ?? "—"
                            }`,
                          ],
                          [
                            "field bindings created",
                            promotion.field_bindings_that_would_be_created
                              .map((binding) => `${binding.entity_type}.${binding.field_path}`)
                              .join(", ") || "none",
                          ],
                          ["creates field_claim", "NO"],
                          ["modifies canonical", "NO"],
                        ]}
                      />
                      <p className="rv-warning">
                        This makes the source publishable. It is the act the whole trust
                        chain exists to gate.
                      </p>
                      <div className="rv-actions" style={{ marginTop: "0.75rem" }}>
                        <button
                          type="button"
                          className="rv-button"
                          onClick={runPromotionApply}
                          disabled={busy}
                        >
                          {busy ? "Promoting…" : "Confirm & Apply Promotion"}
                        </button>
                        <button
                          type="button"
                          className="rv-button"
                          data-variant="secondary"
                          onClick={() => setConfirmingPromotion(false)}
                          disabled={busy}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </>
              ) : null}
            </div>
          ) : null}

          {promotionResult ? (
            <div className="rv-verdict" data-state="valid">
              <div className="rv-verdict-title">Promotion: {promotionResult.status}</div>
              <Facts
                rows={[
                  ["actor", promotionResult.actor],
                  ["source", promotionResult.source_ref],
                  ["responsibility", promotionResult.responsibility],
                  ["promoted", promotionResult.promoted ? "YES" : "NO"],
                  ["publication eligibility", promotionResult.publication_eligibility],
                  ["field bindings written", promotionResult.field_bindings_written],
                  [
                    "audit chain",
                    <Badge key="chain" tone={promotionResult.audit_chain_ok ? "ok" : "error"}>
                      {promotionResult.audit_chain_ok ? "PASS" : "FAIL"}
                    </Badge>,
                  ],
                  ["audit sequence", promotionResult.audit_seq],
                  ["field_claim", promotionResult.field_claim],
                  ["change_proposal", promotionResult.change_proposal],
                  ["change_event", promotionResult.change_event],
                  ["canonical changed", promotionResult.canonical_unchanged ? "NO" : "YES"],
                ]}
              />
              <p className="rv-note">Read back from the database after the write.</p>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
