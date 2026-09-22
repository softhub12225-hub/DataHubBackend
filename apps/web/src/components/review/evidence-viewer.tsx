/**
 * Section G: the stored evidence for one workbook row.
 *
 * THIS NEVER FETCHES THE PAGE
 * ===========================
 * It reads the artifact the extraction already produced. That distinction is the whole
 * point: the reviewer must judge the bytes the system actually holds and will later quote,
 * not whatever the site serves today. A viewer that refetched would show a reviewer one
 * page and publish from another.
 *
 * WHEN THERE IS NO BODY
 * =====================
 * `BODY_EVIDENCE_NOT_AVAILABLE` is displayed as a finding, not an error. A dead or
 * unfetchable page is exactly the case a reviewer most needs to see clearly, and the
 * accompanying sentence says what the host's verification does *not* prove about it.
 *
 * THE CHROME TOGGLE
 * =================
 * A display filter over blocks the extractor marked as navigation furniture. It is
 * conservative and under-filters, and the UI says so, because a reviewer who believed it
 * was exhaustive might read a missing section as absent from the page.
 */

"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge, ErrorText, Facts, Mono, stateTone } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";
import type { EvidenceResult } from "@/lib/review/types";

export function EvidenceViewer({ pilotSourceId }: { pilotSourceId: string }) {
  const [data, setData] = useState<EvidenceResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [skipChrome, setSkipChrome] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    (hideChrome: boolean) => {
      setBusy(true);
      reviewApi
        .evidence(pilotSourceId, { skipChrome: hideChrome })
        .then((result) => {
          setData(result);
          setError(null);
        })
        .catch((cause: unknown) =>
          setError(cause instanceof Error ? cause.message : "Could not load evidence"),
        )
        .finally(() => setBusy(false));
    },
    [pilotSourceId],
  );

  useEffect(() => {
    load(skipChrome);
  }, [load, skipChrome]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!data) return <p className="rv-empty">Loading evidence…</p>;

  return (
    <div className="rv-card">
      <div className="rv-card-head">
        <span className="rv-card-title">
          {data.source_ref} · {data.responsibility}
        </span>
        <Badge tone={stateTone(data.verification_state)}>{data.verification_state}</Badge>
        <Badge tone={data.available ? "ok" : "warn"}>{data.access_class}</Badge>
        {data.degree_scope ? <Badge tone="neutral">{data.degree_scope}</Badge> : null}
      </div>

      {data.same_page_note ? (
        <p className="rv-warning">
          <strong>SAME PHYSICAL PAGE — DIFFERENT RESPONSIBILITY.</strong> {data.same_page_note}
        </p>
      ) : null}

      <Facts
        rows={[
          ["institution", data.institution],
          ["original URL", <Mono key="u">{data.requested_url}</Mono>],
          [
            "effective URL",
            data.effective_url ? (
              <Mono key="e">{data.effective_url}</Mono>
            ) : (
              <span className="rv-sub">no redirect recorded</span>
            ),
          ],
          [
            "requested host",
            <span key="rh" className="rv-inline-list">
              <Mono>{data.requested_host}</Mono>
              {data.host_status ? (
                <Badge tone={stateTone(data.host_status)}>{data.host_status}</Badge>
              ) : null}
            </span>,
          ],
          ["snapshot id", <Mono key="s">{data.snapshot_id ?? "—"}</Mono>],
          ["extraction id", <Mono key="x">{data.extraction_id ?? "—"}</Mono>],
          ["artifact version", <Mono key="v">{data.document_artifact_version ?? "—"}</Mono>],
          [
            "body status",
            <Badge key="b" tone={data.available ? "ok" : "warn"}>
              {data.body_status}
            </Badge>,
          ],
          ["http status", data.http_status ?? "—"],
          ["fetch status", data.fetch_status ?? "—"],
          ["error class", data.error_class ?? "—"],
        ]}
      />

      {data.warnings.length > 0 ? (
        <ul className="rv-blockers">
          {data.warnings.map((warning) => (
            <li key={warning} className="rv-warning">
              {warning}
            </li>
          ))}
        </ul>
      ) : null}

      {data.available ? (
        <>
          <div className="rv-actions">
            <label className="rv-toggle">
              <input
                type="checkbox"
                checked={skipChrome}
                disabled={busy}
                onChange={(event) => setSkipChrome(event.target.checked)}
              />
              Hide obvious navigation/chrome
            </label>
            <span className="rv-sub">
              {data.blocks} blocks · {data.tables} tables
            </span>
          </div>
          <p className="rv-note">{data.chrome_filter_note}</p>
          <pre className="rv-body">{data.body}</pre>
        </>
      ) : (
        <div className="rv-verdict" data-state="blocked">
          <div className="rv-verdict-title">BODY_EVIDENCE_NOT_AVAILABLE</div>
          <p className="rv-note">{data.note}</p>
        </div>
      )}
    </div>
  );
}
