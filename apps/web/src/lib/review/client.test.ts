/**
 * Tests for the console's API client.
 *
 * WHAT IS WORTH TESTING HERE, AND WHAT IS NOT
 * ===========================================
 * Not the rules. Whether a decision may be applied is decided by the server, and a test
 * here that asserted "VERIFIED is allowed when the manifest matches" would be asserting a
 * belief this layer is not entitled to hold.
 *
 * What *is* worth testing is the handful of things this module genuinely owns, each of
 * which fails silently if it is wrong:
 *
 * - the CSRF header is attached to mutating requests and not to reads;
 * - cookies are sent at all (without `credentials`, every request is anonymous and the
 *   console would look broken in a way that resembles a permissions problem);
 * - a 401/409/422 is distinguishable by status, because the UI must treat "log in again",
 *   "your preview went stale" and "refused" differently;
 * - the backend's message survives, rather than being replaced by a generic string.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewApiError, reviewApi } from "@/lib/review/client";

const CSRF_HEADER = "X-DataHub-CSRF";

function mockFetch(response: {
  status?: number;
  body?: unknown;
}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => ({
    status: response.status ?? 200,
    ok: (response.status ?? 200) < 400,
    json: async () => response.body ?? {},
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function setCookie(value: string): void {
  vi.stubGlobal("document", { cookie: value });
}

beforeEach(() => {
  setCookie("");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CSRF", () => {
  it("sends the CSRF cookie value in the header on a mutating request", async () => {
    setCookie("datahub_review_csrf=token-abc; other=ignored");
    const fetchMock = mockFetch({ body: { valid: true } });

    await reviewApi.previewResponsibility("m1", "VERIFIED", "because", "sha");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[CSRF_HEADER]).toBe("token-abc");
  });

  it("does not attach the header to a read", async () => {
    setCookie("datahub_review_csrf=token-abc");
    const fetchMock = mockFetch({ body: {} });

    await reviewApi.dashboard();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[CSRF_HEADER]).toBeUndefined();
  });

  it("sends an empty header rather than throwing when no CSRF cookie is present", async () => {
    // The server rejects it. The client's job is to make a request the server can refuse
    // clearly, not to pre-judge one it might have accepted.
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.logout();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[CSRF_HEADER]).toBe("");
  });
});

describe("credentials", () => {
  it("always sends cookies, or every request would be anonymous", async () => {
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.me();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("same-origin");
  });

  it("never caches, because this data is live trust state", async () => {
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.dashboard();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.cache).toBe("no-store");
  });
});

describe("routing", () => {
  it("goes through the BFF and never to the backend directly", async () => {
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.institution("abc-123");
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/review/institutions/abc-123");
    expect(url.startsWith("http")).toBe(false);
  });

  it("passes evidence view options as query parameters", async () => {
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.evidence("src-1", { skipChrome: true, maxBlocks: 25 });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("skip_chrome=true");
    expect(url).toContain("max_blocks=25");
  });

  it("omits the query string entirely when no options are given", async () => {
    const fetchMock = mockFetch({ body: {} });
    await reviewApi.evidence("src-1");
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/review/sources/src-1/evidence");
  });
});

describe("failures", () => {
  it("distinguishes a stale preview (409) from a refusal (422)", async () => {
    mockFetch({ status: 409, body: { detail: "PREVIEW_STALE: the mapping changed" } });
    await expect(
      reviewApi.applyResponsibility("m1", "VERIFIED", "r", "sha", "tok"),
    ).rejects.toMatchObject({ status: 409 });

    mockFetch({ status: 422, body: { detail: "BINDING_MISMATCH: not in the manifest" } });
    await expect(
      reviewApi.applyResponsibility("m1", "VERIFIED", "r", "sha", "tok"),
    ).rejects.toMatchObject({ status: 422 });
  });

  it("keeps the backend's own message", async () => {
    mockFetch({ status: 409, body: { detail: "PREVIEW_STALE: re-preview and look again" } });
    await expect(
      reviewApi.applyResponsibility("m1", "VERIFIED", "r", "sha", "tok"),
    ).rejects.toThrow(/PREVIEW_STALE/);
  });

  it("reads the enveloped error shape the API uses elsewhere", async () => {
    mockFetch({
      status: 502,
      body: { error: { code: "API_UNREACHABLE", message: "The review API did not answer." } },
    });
    const failure = await reviewApi.dashboard().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ReviewApiError);
    expect((failure as ReviewApiError).code).toBe("API_UNREACHABLE");
    expect((failure as ReviewApiError).message).toMatch(/did not answer/);
  });

  it("reports a 401 by status so the console can send the reviewer to log in", async () => {
    mockFetch({ status: 401, body: { detail: "no session" } });
    await expect(reviewApi.me()).rejects.toMatchObject({ status: 401 });
  });

  it("does not throw on 204, which logout returns", async () => {
    mockFetch({ status: 204 });
    await expect(reviewApi.logout()).resolves.toBeUndefined();
  });
});
