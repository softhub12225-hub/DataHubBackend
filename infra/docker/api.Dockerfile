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

# UV_PROJECT_ENVIRONMENT builds the virtualenv AT ITS FINAL PATH, and that is not
# a tidiness preference -- it is the difference between an image that runs and one
# that does not.
#
# A virtualenv is not relocatable. Every console script in .venv/bin carries the
# absolute path of its interpreter in its shebang, so a venv built at /build/.venv
# and copied to /app/.venv leaves every entry point pointing at
# /build/.venv/bin/python3, which does not exist in the runtime stage. The symptom
# is not a missing uvicorn, it is a missing interpreter, reported as:
#
#   exec container process (missing dynamic library?)
#   `/app/.venv/bin/uvicorn`: No such file or directory
#
# Nothing in CI caught this: the docker job BUILDS the image and never runs it, and
# the Compose stack had never been executed either. The first thing to actually
# start a container from this file was a deployment.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
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
# NO CACHE MOUNT HERE, AND THAT IS A DECISION
# ===========================================
# There used to be `--mount=type=cache,target=/root/.cache/uv` on both `uv sync`
# lines. It is valid BuildKit -- it builds here and in GitHub Actions -- and Railway
# rejects the file outright before the build starts. Naming the id was not enough:
#
#   dockerfile invalid: flag '--mount=type=cache,target=...'
#     is missing an id argument
#   dockerfile invalid: flag '--mount=type=cache,id=uv,target=...'
#     is missing the cacheKey prefix from its id
#
# Railway requires `id=s/<service id>-<target path>`, and its documentation is
# explicit that environment variables are invalid inside a cache mount id. So the
# only way to keep the mount is to hardcode one platform's service UUID into an
# image recipe that Compose, CI and every other platform also build -- and to have
# it silently stop matching, or start failing validation again, the day that service
# is recreated.
#
# The cost of dropping it is small, because it is not what makes rebuilds fast. The
# COPY order above is: manifests first, then source, so the dependency layer is
# reused whenever pyproject.toml and uv.lock are unchanged, which is almost every
# build. The cache mount only helped on the builds that change dependencies, and
# uv re-resolving from a warm registry is seconds.
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api/ apps/api/
RUN uv sync --frozen --no-dev

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

# Same path on both sides. See UV_PROJECT_ENVIRONMENT in the builder stage: the
# shebangs inside are absolute, so this must be a copy, never a move.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
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
