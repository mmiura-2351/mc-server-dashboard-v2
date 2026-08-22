# api/ container image (issue #189). Multi-stage:
#   1. webui  — build the React SPA (webui/dist) with the pinned Node + npm.
#   2. builder — resolve the locked dependency set with uv into a venv.
#   3. runtime — a slim python with that venv, the application source, and the
#      built SPA, run as a non-root user.
#
# The same image serves the app (default CMD) and runs Alembic migrations (the
# compose `migrate` service overrides the command). Tags are pinned, no `latest`
# (docs/dev/DEPENDENCIES.md Section 1).
#
# Build context is the repo ROOT (see compose.yaml) so the webui stage can reach
# `webui/` while the python stages copy from `api/`.

# uv binary, copied from the official image (pinned tag).
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

# ---------------------------------------------------------------------------
# Webui: build the SPA into /webui/dist. Node major matches webui/.nvmrc (24);
# npm is pinned to webui/package.json engines (11.13.0).
# ---------------------------------------------------------------------------
FROM node:24-slim AS webui

WORKDIR /webui

# Pin npm to the repo's engine version before installing, so the lockfile is
# resolved by the same npm everywhere (docs/dev/DEPENDENCIES.md).
RUN npm install -g npm@11.13.0

# Install deps first (no app source) so this layer caches across source-only
# changes. `npm ci` requires package.json + package-lock.json.
COPY webui/package.json webui/package-lock.json ./
RUN npm ci

# Build the SPA. The OpenAPI client (src/api/schema.ts) is checked in, so the
# build does not regenerate it; `npm run build` runs `tsc -b && vite build`.
COPY webui/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Builder: install the locked dependencies into /app/.venv.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

# The locked deps ship wheels, so no compiler is needed. Keep uv from fetching a
# managed interpreter — use the image's python — and copy (not hardlink) into the
# venv so it survives the stage boundary.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app

# Resolve third-party dependencies first (no project source) so this layer caches
# across source-only changes. --no-dev drops the test/lint toolchain.
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Add the project source (and the packages hatchling ships) and install it into
# the same venv.
COPY api/src ./src
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime: slim python + the built venv + the app + the built SPA, as non-root.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

# The venv from the builder and the code Alembic + the app need at runtime.
# alembic.ini's prepend_sys_path makes `src` importable for migrations.
COPY --from=builder /app/.venv /app/.venv
COPY api/src ./src
COPY api/migrations ./migrations
COPY api/alembic.ini ./alembic.ini

# The built SPA, served same-origin via MCD_API_WEBUI__DIST_DIR (compose sets it
# to this path). Static read-only assets are fine for the non-root app to read
# (WEBUI_SPEC 7.7, issue #490).
COPY --from=webui /webui/dist /app/webui/dist

# Put the venv first on PATH so `uvicorn` / `alembic` resolve to it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# compose mounts the `api-storage` named volume here (MCD_API_STORAGE__FS__ROOT).
# Create the mount point owned by the app user before the USER switch so Docker's
# named-volume initialization copies that ownership; otherwise the volume root is
# created root-owned and the non-root app cannot write to it.
RUN mkdir -p /data/storage && chown app:app /data/storage

USER app

EXPOSE 8000 50051

# Default: serve the FastAPI app. The compose `migrate` service overrides this
# with `alembic upgrade head`.
#
# No `--host` / `--port` here on purpose (issue #2585): `server.host` and
# `server.http_port` are the authoritative bind, and flags baked into this image
# would silently outrank them — an operator setting MCD_API_SERVER__HTTP_PORT
# would get no error, no warning and no effect. The entry point reads the same
# settings the app is built from and hands uvicorn the bind (`serve.py`); the
# EXPOSE above still names the defaults (0.0.0.0:8000, :50051). Exec form, so
# the python process is PID 1 and receives the container's SIGTERM directly.
CMD ["python", "-m", "mc_server_dashboard_api.serve"]
