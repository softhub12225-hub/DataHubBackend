/**
 * Minimal typed fetch wrapper for the backend.
 *
 * Deliberately thin. It exists to do four things no caller should repeat:
 * resolve the base URL, apply a timeout, decode the shared error envelope, and
 * propagate the request id. Everything else -- caching policy, retries, query
 * building -- belongs to the caller, which knows its own requirements.
 *
 * Request and response types come from `@datahub/api-types`, which is generated
 * from the FastAPI OpenAPI document. They are never written by hand.
 */

import { apiBaseUrl, apiTimeoutMs } from "@/lib/config/env";

export const REQUEST_ID_HEADER = "X-Request-ID";

/** The error envelope every backend failure uses (see apps/api/src/app/core/errors.py). */
export interface ApiErrorBody {
  code: string;
  message: string;
  details: { location?: string | null; message: string; type?: string | null }[];
  requestId?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ApiErrorBody["details"];
  readonly requestId: string | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? [];
    this.requestId = body.requestId ?? null;
  }
}

/** Raised when the backend cannot be reached or does not answer in time. */
export class ApiUnreachableError extends Error {
  constructor(message: string, cause: unknown) {
    // Native `cause` rather than a redeclared field: Error already defines it, and
    // shadowing it loses the standard behaviour that tooling relies on.
    super(message, { cause });
    this.name = "ApiUnreachableError";
  }
}

export interface ApiRequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** Forwarded so a single user action can be traced across web and api logs. */
  requestId?: string;
  timeoutMs?: number;
  /** Passed through to fetch; defaults to "no-store" since this data is live. */
  cache?: RequestCache;
  signal?: AbortSignal;
}

/**
 * Perform a request and decode the JSON response.
 *
 * Throws `ApiError` for an enveloped backend failure and `ApiUnreachableError` for
 * a transport failure or timeout. Both are distinguishable by callers, because
 * "the backend said no" and "the backend is down" need different handling.
 */
export async function apiFetch<TResponse>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<{ data: TResponse; status: number; requestId: string | null }> {
  const {
    method = "GET",
    body,
    requestId,
    timeoutMs = apiTimeoutMs(),
    cache = "no-store",
    signal,
  } = options;

  const url = `${apiBaseUrl()}${path.startsWith("/") ? path : `/${path}`}`;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (requestId) headers[REQUEST_ID_HEADER] = requestId;

  // AbortSignal.timeout rather than a manual setTimeout: no timer to leak, and it
  // composes with a caller-supplied signal.
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const composedSignal = signal
    ? AbortSignal.any([signal, timeoutSignal])
    : timeoutSignal;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      cache,
      signal: composedSignal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    const timedOut = timeoutSignal.aborted;
    throw new ApiUnreachableError(
      timedOut
        ? `Request to ${url} timed out after ${timeoutMs}ms`
        : `Request to ${url} failed`,
      cause,
    );
  }

  const responseRequestId = response.headers.get(REQUEST_ID_HEADER);
  const payload: unknown = await response.json().catch(() => null);

  // 503 from /health/ready carries a normal readiness body, not an error envelope,
  // so "not ok" alone is not enough to decide this is a failure.
  if (!response.ok && isErrorEnvelope(payload)) {
    throw new ApiError(response.status, payload.error);
  }
  if (payload === null) {
    throw new ApiUnreachableError(
      `Response from ${url} was not valid JSON (status ${response.status})`,
      null,
    );
  }

  return {
    data: payload as TResponse,
    status: response.status,
    requestId: responseRequestId,
  };
}

function isErrorEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as ApiErrorBody).code === "string" &&
    typeof (error as ApiErrorBody).message === "string"
  );
}
