# Single image for three runtime roles: the FastAPI API, the Celery worker and the
# Celery beat scheduler (ARCHITECTURE.md D11). The role is chosen by the command in
# docker-compose, not by building three near-identical images.
#
# The Playwright/browser worker will need its own image -- browser binaries are
# ~400MB and must not bloat the API image -- and is deliberately not built yet.

# ---------------------------------------------------------------------------
# Stage 1: resolve dependencies into a virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned by digest-free tag on purpose: uv is a build-time tool, and a floating
# patch version here cannot affect the runtime image's contents.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Copy only the manifests first so the dependency layer is cached independently of
# application source. Editing a Python file must not trigger a full reinstall.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml

# --no-dev: ruff, mypy and pytest have no business in a runtime image.
# --frozen: fail if uv.lock disagrees with the manifests rather than silently
# resolving something different from what was tested.
#
# `id=uv` is not optional in practice. BuildKit defaults a cache mount's id to
# its target, so leaving it out builds correctly here and in GitHub Actions --
# and Railway's Dockerfile validator rejects the whole file before the build
# even starts:
#
#   dockerfile invalid: flag '--mount=type=cache,target=/root/.cache/uv'
#   is missing an id argument at Line 32
#
# Naming it changes nothing about the build and makes the sharing explicit: the
# two stages below deliberately share one uv download cache. The web image
# always carried `id=pnpm`, which is the only reason it passed the same
# validator while this one did not.
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY apps/api/ apps/api/
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/apps/api/src"

# curl for the compose healthcheck. No build toolchain in the runtime image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /build/.venv /app/.venv
COPY --chown=app:app apps/api/ /app/apps/api/
COPY --chown=app:app pyproject.toml /app/pyproject.toml

# Non-root. The application needs no write access to its own code.
USER app

EXPOSE 8000

# Liveness only -- a readiness check here would make the container unhealthy
# whenever Postgres restarts, and Docker would kill a process that is working fine.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health/live || exit 1

# Overridden for the worker and beat roles in docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
