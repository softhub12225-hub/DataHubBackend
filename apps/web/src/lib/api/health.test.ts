/**
 * Health client behaviour.
 *
 * `fetch` is stubbed so these run with no backend. The cases that matter are the
 * three outcomes the status page must distinguish: ready, not ready (a 503 that is
 * still a valid answer), and unreachable.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getReadiness } from "@/lib/api/health";
import type { ReadinessResponse } from "@/lib/api/health";

const READY: ReadinessResponse = {
  status: "ready",
  service: "Overseas University DataHub API",
  version: "0.1.0",
  checks: [
    { name: "postgres", status: "ok", latency_ms: 1.2, error: null },
    { name: "redis", status: "ok", latency_ms: 0.4, error: null },
  ],
};

const NOT_READY: ReadinessResponse = {
  status: "not_ready",
  service: "Overseas University DataHub API",
  version: "0.1.0",
  checks: [
    {
      name: "postgres",
      status: "error",
      latency_ms: 5000.0,
      error: "TimeoutError: connection timed out",
    },
    { name: "redis", status: "ok", latency_ms: 0.4, error: null },
  ],
};

function jsonResponse(body: unknown, status: number, requestId = "req-abc123"): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "X-Request-ID": requestId },
  });
}

beforeEach(() => {
  process.env.API_BASE_URL = "http://api.test:8000";
  process.env.NEXT_PUBLIC_APP_ENV = "ci";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("getReadiness", () => {
  it("reports ready on a 200 with status ready", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(READY, 200)));

    const result = await getReadiness();

    expect(result.outcome).toBe("ready");
    if (result.outcome === "unreachable") throw new Error("unexpected outcome");
    expect(result.body.checks).toHaveLength(2);
    expect(result.requestId).toBe("req-abc123");
  });

  it("treats a 503 as a valid not-ready answer, not an error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(NOT_READY, 503)));

    const result = await getReadiness();

    expect(result.outcome).toBe("not_ready");
    if (result.outcome === "unreachable") throw new Error("unexpected outcome");
    const failed = result.body.checks.filter((check) => check.status === "error");
    expect(failed.map((check) => check.name)).toEqual(["postgres"]);
    expect(failed[0]?.error).toContain("TimeoutError");
  });

  it("reports unreachable when the transport fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));

    const result = await getReadiness();

    expect(result.outcome).toBe("unreachable");
    if (result.outcome !== "unreachable") throw new Error("unexpected outcome");
    expect(result.error).toContain("http://api.test:8000/health/ready");
  });

  it("calls the readiness path on the configured base URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(READY, 200));
    vi.stubGlobal("fetch", fetchMock);

    await getReadiness({ requestId: "trace-1" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test:8000/health/ready");
    expect(init.cache).toBe("no-store");
    expect((init.headers as Record<string, string>)["X-Request-ID"]).toBe("trace-1");
  });

  it("fails loudly when API_BASE_URL is missing", async () => {
    delete process.env.API_BASE_URL;
    vi.stubGlobal("fetch", vi.fn());

    await expect(getReadiness()).rejects.toThrow(/API_BASE_URL is not set/);
  });

  it("rejects a non-http base URL", async () => {
    process.env.API_BASE_URL = "ftp://api.test";
    vi.stubGlobal("fetch", vi.fn());

    await expect(getReadiness()).rejects.toThrow(/must be http or https/);
  });
});
