/**
 * Browser-side access to the review API, through the BFF at `/api/review/*`.
 *
 * This module knows three things and no more: where the BFF is, how to attach the CSRF
 * header, and how to turn a failure into a message a reviewer can act on. It contains no
 * rule about what may be decided -- see `types.ts`.
 *
 * CSRF
 * ====
 * The backend sets two cookies at login: the session (HttpOnly, unreadable here) and a
 * CSRF token (readable). Every mutating request echoes the CSRF value in a header. Script
 * on another origin can cause the browser to *send* cookies but cannot read them, so it
 * cannot produce the header. Reading the cookie here is therefore the point, not a leak.
 */

import type {
  ApplyResult,
  DashboardData,
  EvidenceResult,
  InstitutionDetail,
  InstitutionSummary,
  OperationEntry,
  AuditEntry,
  PreviewResult,
  PromotionApplyResult,
  CandidateGroup,
  CandidateQueue,
  ConflictPreviewResult,
  PromotionPreviewResult,
  ReadinessMatrix,
  ResolutionResult,
  ScopeCriterion,
  ScopePreviewResult,
  SessionInfo,
} from "@/lib/review/types";

const BFF = "/api/review";
const CSRF_COOKIE = "datahub_review_csrf";
const CSRF_HEADER = "X-DataHub-CSRF";

/** Raised for any non-2xx. `status` lets callers tell 401 from 409 from 422. */
export class ReviewApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, message: string, code = "ERROR") {
    super(message);
    this.name = "ReviewApiError";
    this.status = status;
    this.code = code;
  }
}

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${CSRF_COOKIE}=`));
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : "";
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const { method = "GET", body } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers[CSRF_HEADER] = csrfToken();

  const response = await fetch(`${BFF}${path}`, {
    method,
    headers,
    // The session cookie is the whole authentication mechanism; without this the
    // browser omits it and every request is anonymous.
    credentials: "same-origin",
    cache: "no-store",
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return undefined as T;

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ReviewApiError(response.status, messageOf(payload, response.status), codeOf(payload));
  }
  return payload as T;
}

function messageOf(payload: unknown, status: number): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.detail === "string") return record.detail;
    const error = record.error;
    if (error && typeof error === "object") {
      const message = (error as Record<string, unknown>).message;
      if (typeof message === "string") return message;
    }
  }
  return `Request failed with status ${status}`;
}

function codeOf(payload: unknown): string {
  if (payload && typeof payload === "object") {
    const error = (payload as Record<string, unknown>).error;
    if (error && typeof error === "object") {
      const code = (error as Record<string, unknown>).code;
      if (typeof code === "string") return code;
    }
  }
  return "ERROR";
}

export const reviewApi = {
  login: (email: string, password: string) =>
    request<SessionInfo>("/auth/login", { method: "POST", body: { email, password } }),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  me: () => request<SessionInfo>("/auth/me"),

  dashboard: () => request<DashboardData>("/dashboard"),

  institutions: () => request<InstitutionSummary[]>("/institutions"),

  institution: (id: string) => request<InstitutionDetail>(`/institutions/${id}`),

  audit: (id: string, limit = 200) =>
    request<AuditEntry[]>(`/institutions/${id}/audit?limit=${limit}`),

  operations: (limit = 50) => request<OperationEntry[]>(`/operations?limit=${limit}`),

  evidence: (pilotSourceId: string, opts: { skipChrome?: boolean; maxBlocks?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.skipChrome) params.set("skip_chrome", "true");
    if (opts.maxBlocks) params.set("max_blocks", String(opts.maxBlocks));
    const query = params.toString();
    return request<EvidenceResult>(
      `/sources/${pilotSourceId}/evidence${query ? `?${query}` : ""}`,
    );
  },

  previewResponsibility: (
    mappingId: string,
    decision: string,
    reason: string,
    manifestSha256: string,
  ) =>
    request<PreviewResult>(`/responsibilities/${mappingId}/preview`, {
      method: "POST",
      body: { decision, reason, manifest_sha256: manifestSha256 },
    }),

  applyResponsibility: (
    mappingId: string,
    decision: string,
    reason: string,
    manifestSha256: string,
    previewToken: string,
  ) =>
    request<ApplyResult>(`/responsibilities/${mappingId}/apply`, {
      method: "POST",
      body: {
        decision,
        reason,
        manifest_sha256: manifestSha256,
        preview_token: previewToken,
      },
    }),

  previewPromotion: (mappingId: string) =>
    request<PromotionPreviewResult>(`/promotions/${mappingId}/preview`, { method: "POST" }),

  applyPromotion: (mappingId: string, reason: string, previewToken: string) =>
    request<PromotionApplyResult>(`/promotions/${mappingId}/apply`, {
      method: "POST",
      body: { reason, preview_token: previewToken },
    }),

  candidateQueue: (institutionId: string, filters: Record<string, string> = {}) => {
    const params = new URLSearchParams(filters);
    const query = params.toString();
    return request<CandidateQueue>(
      `/institutions/${institutionId}/candidates${query ? `?${query}` : ""}`,
    );
  },

  candidateGroups: (institutionId: string) =>
    request<CandidateGroup[]>(`/institutions/${institutionId}/candidate-groups`),

  candidateReadiness: (institutionId: string) =>
    request<ReadinessMatrix>(`/institutions/${institutionId}/candidate-readiness`),

  previewScope: (
    institutionId: string,
    candidateId: string,
    state: string,
    criteria: ScopeCriterion[],
    reason: string,
  ) =>
    request<ScopePreviewResult>(
      `/institutions/${institutionId}/candidates/${candidateId}/scope/preview`,
      { method: "POST", body: { state, criteria, reason } },
    ),

  applyScope: (
    institutionId: string,
    candidateId: string,
    state: string,
    criteria: ScopeCriterion[],
    reason: string,
    previewToken: string,
  ) =>
    request<ResolutionResult>(
      `/institutions/${institutionId}/candidates/${candidateId}/scope/apply`,
      { method: "POST", body: { state, criteria, reason, preview_token: previewToken } },
    ),

  previewConflict: (
    institutionId: string,
    fingerprint: string,
    action: string,
    selectedCandidateId: string | null,
    reason: string,
  ) =>
    request<ConflictPreviewResult>(
      `/institutions/${institutionId}/candidate-groups/${fingerprint}/preview`,
      { method: "POST", body: { action, selected_candidate_id: selectedCandidateId, reason } },
    ),

  applyConflict: (
    institutionId: string,
    fingerprint: string,
    action: string,
    selectedCandidateId: string | null,
    reason: string,
    previewToken: string,
  ) =>
    request<ResolutionResult>(
      `/institutions/${institutionId}/candidate-groups/${fingerprint}/apply`,
      {
        method: "POST",
        body: {
          action,
          selected_candidate_id: selectedCandidateId,
          reason,
          preview_token: previewToken,
        },
      },
    ),
};
