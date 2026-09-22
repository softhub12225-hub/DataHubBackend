# Next.js console. Three stages so the runtime image contains the standalone build
# output and nothing else -- no pnpm store, no dev dependencies, no source tree.

# ---------------------------------------------------------------------------
# Stage 1: dependencies
# ---------------------------------------------------------------------------
FROM node:22.23-bookworm-slim AS deps

RUN corepack enable && corepack prepare pnpm@9.15.4 --activate

WORKDIR /repo

# Manifests only, so the install layer caches independently of source changes.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-types/package.json packages/api-types/

RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Stage 2: build
# ---------------------------------------------------------------------------
FROM node:22.23-bookworm-slim AS builder

RUN corepack enable && corepack prepare pnpm@9.15.4 --activate

WORKDIR /repo
COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/web/node_modules ./apps/web/node_modules
COPY --from=deps /repo/packages/api-types/node_modules ./packages/api-types/node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* values are inlined at build time, so the build must know which
# environment it is for. Server-only settings (API_BASE_URL) are read at runtime.
ARG NEXT_PUBLIC_APP_ENV=production
ENV NEXT_PUBLIC_APP_ENV=${NEXT_PUBLIC_APP_ENV}

RUN pnpm --filter @datahub/web run build

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM node:22.23-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# `output: "standalone"` emits a self-contained server plus a minimal node_modules.
COPY --from=builder --chown=app:app /repo/apps/web/.next/standalone ./
COPY --from=builder --chown=app:app /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=app:app /repo/apps/web/public ./apps/web/public

USER app

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:3000/ || exit 1

CMD ["node", "apps/web/server.js"]
