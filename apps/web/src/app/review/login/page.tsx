/**
 * Reviewer login.
 *
 * The password goes into one fetch and is never stored, never put in a URL, never logged
 * and never held in state beyond the submit. The response carries no token for script to
 * keep: the session arrives as an HttpOnly cookie the backend set, which this page cannot
 * read and does not need to.
 *
 * Failures are reported with the backend's own message, which is deliberately identical
 * for "no such account" and "wrong password" -- distinguishing them here would rebuild the
 * account-enumeration oracle the backend refuses to be.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorText } from "@/components/review/primitives";
import { reviewApi } from "@/lib/review/client";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await reviewApi.login(email, password);
      setPassword("");
      router.replace("/review");
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed");
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rv-login">
      <section className="rv-panel">
        <h1 style={{ fontSize: "1.2rem", margin: "0 0 0.25rem" }}>Reviewer console</h1>
        <p className="rv-sub" style={{ marginBottom: "1rem" }}>
          Internal source verification. Sign in with your reviewer account.
        </p>
        <form onSubmit={submit}>
          <div className="rv-field">
            <label htmlFor="rv-email">Email</label>
            <input
              id="rv-email"
              className="rv-input"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <div className="rv-field">
            <label htmlFor="rv-password">Password</label>
            <input
              id="rv-password"
              className="rv-input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          {error ? <ErrorText>{error}</ErrorText> : null}
          <button className="rv-button" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </div>
  );
}
