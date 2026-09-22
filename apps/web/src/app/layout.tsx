import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "University DataHub",
  description: "Internal university data management and verification platform",
  // This is an internal tool behind authentication; it should never be indexed.
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // lang is provisional: the console is bilingual (zh-CN/en) and this becomes
    // locale-driven when next-intl is introduced with the console proper.
    <html lang="en">
      <body>
        <div className="shell">
          <header className="shell-header">
            <span className="shell-brand">University DataHub</span>
            <nav className="shell-nav">
              <Link href="/">Overview</Link>
              <Link href="/system-status">System status</Link>
            </nav>
          </header>
          <main className="shell-main">{children}</main>
        </div>
      </body>
    </html>
  );
}
