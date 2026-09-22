/**
 * Shell for the reviewer console.
 *
 * The identity shown in the header comes from `/auth/me`, which resolves the session
 * cookie on the server. It is displayed, never trusted: no request carries it, and the
 * backend derives the actor from the same cookie on every call. Showing it is so a
 * reviewer can see who the system thinks they are before they decide anything.
 */

"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ReviewApiError, reviewApi } from "@/lib/review/client";
import type { SessionInfo } from "@/lib/review/types";

import "./review.css";

export default function ReviewLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [checked, setChecked] = useState(false);
  const onLogin = pathname === "/review/login";

  useEffect(() => {
    let cancelled = false;
    reviewApi
      .me()
      .then((info) => {
        if (!cancelled) setSession(info);
      })
      .catch((error: unknown) => {
        // 401 is the ordinary "not logged in" answer, not a failure to report.
        if (!cancelled && error instanceof ReviewApiError && error.status === 401 && !onLogin) {
          router.replace("/review/login");
        }
      })
      .finally(() => {
        if (!cancelled) setChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname, onLogin, router]);

  const logout = useCallback(async () => {
    await reviewApi.logout().catch(() => undefined);
    setSession(null);
    router.replace("/review/login");
  }, [router]);

  if (!checked && !onLogin) {
    return <p className="rv-empty">Checking your session…</p>;
  }

  return (
    <div className="rv-page">
      {!onLogin ? (
        <div className="rv-headline">
          <nav className="rv-inline-list">
            <Link href="/review">Dashboard</Link>
            <Link href="/review/institutions">Institutions</Link>
            <Link href="/review/operations">Operations</Link>
          </nav>
          {session ? (
            <span className="rv-inline-list">
              <span>
                <strong>{session.display_name}</strong>{" "}
                <span className="rv-sub">{session.roles.join(", ") || "no roles"}</span>
              </span>
              <button type="button" className="rv-button" data-variant="secondary" onClick={logout}>
                Log out
              </button>
            </span>
          ) : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
