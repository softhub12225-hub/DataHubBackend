/**
 * Health API client.
 *
 * The readiness endpoint answers 200 when ready and 503 when not, and both carry
 * the same body. That is not an error condition for this client -- "not ready" is a
 * legitimate, informative answer that the status page must render -- so the 503 is
 * translated into a result object rather than a thrown error.
 */

import type { components } from "@datahub/api-types";

import { ApiUnreachableError, apiFetch } from "@/lib/api/client";

export type ReadinessResponse = components["schemas"]["ReadinessResponse"];
export type LivenessResponse = components["schemas"]["LivenessResponse"];
export type DependencyCheck = components["schemas"]["DependencyCheck"];

export type ReadinessResult =
  | { outcome: "ready" | "not_ready"; body: ReadinessResponse; requestId: string | null }
  | { outcome: "unreachable"; error: string };

export async function getReadiness(
  options: { requestId?: string } = {},
): Promise<ReadinessResult> {
  try {
    const { data, status, requestId } = await apiFetch<ReadinessResponse>(
      "/health/ready",
      { requestId: options.requestId },
    );
    return {
      outcome: status === 200 && data.status === "ready" ? "ready" : "not_ready",
      body: data,
      requestId,
    };
  } catch (error) {
    if (error instanceof ApiUnreachableError) {
      return { outcome: "unreachable", error: error.message };
    }
    throw error;
  }
}

export async function getLiveness(): Promise<LivenessResponse | null> {
  try {
    const { data } = await apiFetch<LivenessResponse>("/health/live");
    return data;
  } catch (error) {
    if (error instanceof ApiUnreachableError) return null;
    throw error;
  }
}
