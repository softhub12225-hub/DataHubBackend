/**
 * Validated environment configuration.
 *
 * Two rules this module enforces:
 *
 * 1. Server-only values never reach the browser. `API_BASE_URL` is read in server
 *    components and route handlers only; it is not `NEXT_PUBLIC_`, so Next.js will
 *    not inline it into the client bundle. Reading it from client code throws.
 * 2. Missing or malformed configuration fails loudly at first use rather than
 *    producing a request to `undefined/health/ready`.
 *
 * Hand-rolled rather than schema-validated: there are three variables. A validation
 * library here would be more code than the thing it validates.
 */

export type AppEnv = "local" | "ci" | "staging" | "production";

const APP_ENVS: readonly AppEnv[] = ["local", "ci", "staging", "production"];

function requireServer(name: string): void {
  if (typeof window !== "undefined") {
    throw new Error(
      `${name} is a server-only setting and must not be read from client code`,
    );
  }
}

function parseBaseUrl(raw: string | undefined): string {
  const value = (raw ?? "").trim();
  if (!value) {
    throw new Error(
      "API_BASE_URL is not set. Copy .env.example to .env and set it " +
        "(local default: http://localhost:8000).",
    );
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`API_BASE_URL is not a valid absolute URL: ${value}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`API_BASE_URL must be http or https, got ${parsed.protocol}`);
  }
  // Normalise away a trailing slash so callers can always join with "/path".
  return value.replace(/\/+$/, "");
}

/** Base URL of the FastAPI backend. Server-side only. */
export function apiBaseUrl(): string {
  requireServer("API_BASE_URL");
  return parseBaseUrl(process.env.API_BASE_URL);
}

/** Deployment environment. Safe on the client; used only for display. */
export function appEnv(): AppEnv {
  const raw = (process.env.NEXT_PUBLIC_APP_ENV ?? "local").trim();
  return (APP_ENVS as readonly string[]).includes(raw) ? (raw as AppEnv) : "local";
}

/** Request timeout for backend calls, in milliseconds. */
export function apiTimeoutMs(): number {
  const raw = Number(process.env.API_TIMEOUT_MS ?? "5000");
  return Number.isFinite(raw) && raw > 0 ? raw : 5000;
}
