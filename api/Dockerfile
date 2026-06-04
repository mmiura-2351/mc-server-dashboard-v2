# api/ container image (issue #189). Multi-stage:
#   1. builder — resolve the locked dependency set with uv into a venv.
#   2. runtime — a slim python with that venv + the application source, run as a
#      non-root user.
#
# The same image serves the app (default CMD) and runs Alembic migrations (the
# compose `migrate` service overrides the command). Tags are pinned, no `latest`
# (docs/dev/DEPENDENCIES.md Section 1).

# uv binary, copied from the official image (pinned tag).
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

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
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Add the project source (and the packages hatchling ships) and install it into
# the same venv.
COPY src ./src
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime: slim python + the built venv + the app, as a non-root user.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

# The venv from the builder and the code Alembic + the app need at runtime.
# alembic.ini's prepend_sys_path makes `src` importable for migrations.
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

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

# Default: serve the FastAPI app via the factory. The compose `migrate` service
# overrides this with `alembic upgrade head`.
CMD ["uvicorn", "mc_server_dashboard_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
