import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Fail the build on type or lint errors rather than shipping them. Next's
  // defaults already do this; stated explicitly so nobody "temporarily" flips it.
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: false },
  // Standalone output so the production image copies only what it needs instead of
  // the whole node_modules tree.
  output: "standalone",
  poweredByHeader: false,
  // Server-only configuration is never listed in `env`, which would inline it into
  // the client bundle. API_BASE_URL is read at request time via process.env.
  experimental: {},
};

export default nextConfig;
