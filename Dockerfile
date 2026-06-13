# syntax=docker/dockerfile:1
#
# Multi-stage build for the ha-airspace service.
#
# Stage 1 resolves the locked dependency set + installs the package into a
# self-contained venv with uv. Stage 2 is a slim glibc runtime that carries
# only that venv — no uv, no build tooling, no source tree.
#
# Base is python:3.12-slim (glibc), not Alpine: pydantic-core and httpx ship
# manylinux wheels that install cleanly here, whereas musl forces slow source
# builds on armv7 (Raspberry Pi). Multi-arch (amd64/arm64/armv7) is handled by
# the publish workflow (Phase 4 slice 3); this file is arch-agnostic.

ARG PYTHON_VERSION=3.12

# --- Stage 1: build the venv -------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# Pinned uv for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer first: resolve deps from the lockfile without the project,
# so editing source doesn't bust the (slow) dependency cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now install the package itself into the same venv. --no-editable installs a
# built wheel into site-packages (not an editable .pth into /app/src), so the
# venv is self-contained and survives the copy into the runtime stage without
# the source tree.
COPY src/ ./src/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Stage 2: runtime --------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

# Non-root. /config and /data are owned by the runtime user so a bind-mounted
# config is readable and the journal/DB cache are writable.
RUN useradd --system --create-home --uid 10001 airspace \
    && mkdir -p /config /data \
    && chown -R airspace:airspace /config /data

COPY --from=builder --chown=airspace:airspace /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER airspace
WORKDIR /app

# Only /data is a declared volume — the SQLite journal + reference-DB cache, the
# state worth persisting. /config is NOT declared: it is a read-only bind target
# for the user's config, and declaring it would spawn a junk anonymous volume on
# every run that doesn't bind-mount it.
VOLUME ["/data"]

# No HEALTHCHECK here on purpose: the container's main process *is* the service,
# so a crash exits the container and the restart policy handles it. There is no
# always-on health endpoint by default (the /metrics exposition is opt-in). When
# /metrics is enabled, add a healthcheck downstream, e.g.:
#   HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:9091/metrics')"

ENTRYPOINT ["ha-airspace"]
CMD ["-c", "/config/config.yaml"]
