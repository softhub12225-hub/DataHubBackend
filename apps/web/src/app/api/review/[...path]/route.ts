/**
 * The BFF. Every console request to the backend goes through here and nowhere else.
 *
 * WHY A PROXY AND NOT A DIRECT CALL
 * =================================
 * Section R: the browser must never hold a database credential, and must never be the
 * thing that decides where the backend is. `API_BASE_URL` is deliberately *not* a
 * `NEXT_PUBLIC_` variable, so Next.js will not inline it into the client bundle, and
 * `apiBaseUrl()` throws if it is read from client code. Routing through a server route
 * handler is what lets that stay true while the console is still a browser application.
 *
 * It also keeps the session cookie HttpOnly end to end. The browser sends its cookies to
 * this origin; this handler forwards them to FastAPI; FastAPI's `Set-Cookie` comes back
 * through unchanged. At no point does script need to read the session value -- which is
 * the entire reason it can be HttpOnly.
 *
 * WHAT IS FORWARDED, AND WHAT IS NOT
 * ==================================
 * Cookies, the CSRF header and the content type. Not arbitrary client headers: a proxy
 * that forwards everything is a proxy that will eventually forward something it should
 * have dropped. The allow-list is short and explicit for that reason.
 *
 * This handler makes no decisions. It does not inspect the session, does not judge
 * permissions and does not interpret bodies -- FastAPI does all of that, and a proxy that
 * second-guessed it would be the second policy section A.4 forbids.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { apiBaseUrl, apiTimeoutMs } from "@/lib/config/env";

/** Headers we pass through to the backend. Everything else is dropped. */
const FORWARD_TO_BACKEND = ["cookie", "content-type", "x-datahub-csrf", "x-request-id"];

/** Headers we pass back to the browser. `set-cookie` is handled separately. */
const FORWARD_TO_BROWSER = ["content-type", "x-request-id"];

/** The API prefix the backend mounts the console router under. */
const BACKEND_PREFIX = "/api/v1/review";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, path: string[]): Promise<NextResponse> {
  const suffix = path.map(encodeURIComponent).join("/");
  const query = request.nextUrl.search;
  const url = `${apiBaseUrl()}${BACKEND_PREFIX}/${suffix}${query}`;

  const headers = new Headers();
  for (const name of FORWARD_TO_BACKEND) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let response: Response;
  try {
    response = await fetch(url, {
      method: request.method,
      headers,
      body: hasBody ? await request.text() : undefined,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(apiTimeoutMs()),
    });
  } catch (cause) {
    // A backend that is down must not look like a rejected decision. 502 with a named
    // shape, so the console can say "the API is unreachable" rather than "refused".
    return NextResponse.json(
      {
        error: {
          code: "API_UNREACHABLE",
          message: "The review API did not answer. It may be stopped or still starting.",
          details: [{ message: String(cause) }],
        },
      },
      { status: 502 },
    );
  }

  // 204 and 304 are null-body statuses: constructing a Response with ANY body --
  // including the empty string `response.text()` returns -- throws, which turned a
  // perfectly good logout into a 500. The API was right; the proxy was wrong.
  const NULL_BODY_STATUSES = new Set([204, 205, 304]);
  const body = NULL_BODY_STATUSES.has(response.status) ? null : await response.text();
  const out = new NextResponse(body, { status: response.status });
  for (const name of FORWARD_TO_BROWSER) {
    const value = response.headers.get(name);
    if (value) out.headers.set(name, value);
  }
  // getSetCookie() rather than get("set-cookie"): login sets two cookies, and the
  // single-header accessor silently collapses them into one malformed value.
  for (const cookie of response.headers.getSetCookie?.() ?? []) {
    out.headers.append("set-cookie", cookie);
  }
  return out;
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function DELETE(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function PATCH(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}
