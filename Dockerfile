# syntax=docker/dockerfile:1
#
# Multi-stage build:
# - `python-3.14t`: Astral python-build-standalone install. The official
#   `python:3.14t-slim` image doesn't exist on Docker Hub yet (PEP 779 is
#   supported in CPython core as of 3.14, but docker-library/python is a
#   separate maintenance track that hasn't shipped a free-threaded
#   variant), so we pull the reproducible CPython distribution Astral /
#   uv use directly. Pinned by `PBS_RELEASE` for reproducible builds.
# - `builder`: install dependencies via uv into a project-local
#   venv. Only the venv ships.
# - `runtime`: clean stage with the venv + source, runs gunicorn as a
#   non-root user. Derives from the same `python-3.14t` base as builder
#   so the venv's shebangs (which point at `/opt/python/bin/python3`)
#   stay valid in the runtime image.
#
# Default volume:
#   /data, single state directory. Holds:
#     /data/db       , SQLite DB (DATABASE_URL=sqlite:////data/db/mimir.db)
#     /data/Inboxes  , public-inbox mirrors. /app/Inboxes is a
#                       symlink to this so the default relative
#                       `INBOXES` config still resolves cleanly.
#     /data/Mainline , Linus's linux.git plus any configured
#                       subsystem-tree mirrors for MAINTAINERS +
#                       Link: trailer indexing. Default curated
#                       set: linus + linux-next + 5 *-next trees
#                       (net-next, tip, pci, mm @ mm-stable,
#                       bpf-next). Operators add trees via
#                       `TREES__<name>__URL` / `__PATH` env;
#                       paths land under /data/Mainline/<name>.git.
#                       Non-Linus trees clone with
#                       `--reference linus.git` so marginal disk
#                       per tree is ~100-500 MB rather than ~10 GB.
#                       /app/Mainline symlinks through so the
#                       default relative tree paths resolve here.
#
# Operator must `chown -R 1001:1001 <host-data-dir>` before bringing
# the container up (rootful podman / docker, no UID remapping).

ARG PBS_RELEASE=20260510
ARG PYTHON_VERSION=3.14.5
ARG PBS_ARCH=x86_64-unknown-linux-gnu
ARG PBS_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-freethreaded-install_only.tar.gz


FROM debian:trixie-slim AS python-3.14t

# Pull Astral's python-build-standalone free-threaded CPython 3.14 into
# /opt/python. The `install_only` tarball ships everything needed at
# runtime (no debug symbols, no test suite); extracting under /opt
# gives us `/opt/python/bin/python3` as the interpreter and
# `/opt/python/bin/python3.14t` as the free-threaded-named symlink.
# The build-time `sys._is_gil_enabled() is False` assertion is the
# gate that catches a bad URL or a future tarball schema change at
# build time rather than letting a GIL-enabled binary silently land in
# production. Single RUN layer so the curl-purge + apt-list cleanup
# actually shrinks the layer rather than leaving them in an earlier
# layer that the final image inherits.
ARG PBS_URL
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL "${PBS_URL}" | tar -xz -C /opt \
 && /opt/python/bin/python3 -c "import sys; assert sys._is_gil_enabled() is False, 'expected free-threaded build'" \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/python/bin:${PATH}"


FROM python-3.14t AS builder

# Copy the uv binary from its official image rather than installing
# it via pip. This avoids pip's own resolver and keeps the builder
# stage lean.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# UV_PYTHON_DOWNLOADS=never: the PBS Python at /opt/python/bin/python3
# (PATH-prepended in the python-3.14t base) is what uv must use; we
# do not want uv to attempt a download of its own interpreter.
# UV_LINK_MODE=copy: avoids hardlink errors across the BuildKit cache
# mount filesystem boundary.
# UV_PROJECT_ENVIRONMENT=/app/.venv: pins the venv path so the runtime
# stage's COPY --from=builder and PATH entries resolve correctly.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Copy only what dependency resolution needs first so the wheel-cache
# layer survives unrelated source changes.
COPY pyproject.toml uv.lock README.md ./
COPY mimir/__init__.py ./mimir/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now the rest of the source.
COPY mimir/ ./mimir/
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Install the project itself into the same venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python-3.14t AS runtime

# git: `mimir update` shells out to `git clone --mirror` / `git fetch`
# to sync upstream public-inbox epochs. ca-certificates: HTTPS verify
# against lore.kernel.org and other upstream hosts. tini: minimal init
# that runs as PID 1 and reaps orphaned grandchildren. `git fetch`
# spawns helper grandchildren (`git-remote-https` and friends) that
# reparent to PID 1 when the direct `git` child exits; without an
# init shim the broker process (PID 1 in production) accumulates them
# as zombies for the life of the container (observed 2026-05-29: ~30
# `[git]` zombies after 35 h uptime, ~6-7 per `update_mainline` tick
# matching the per-tick fetch count across `Settings.trees`). tini
# reaps any orphan that reparents to PID 1, leaving the broker's own
# `subprocess.run(..., check=True)` semantics intact (so a failing
# `git fetch` still raises CalledProcessError instead of being
# silently auto-reaped by a SIGCHLD=SIG_IGN disposition).
# libmimalloc3: replaces glibc malloc for the mimir-broker service
# via LD_PRELOAD (set in compose.yaml on the broker only). Default
# allocator stays glibc for app and tasks. See issue #447 for the
# rationale (warm-cycle RSS oscillation, post-cycle release rate).
# The ABI version (libmimalloc.so.3) tracks Debian Trixie's package;
# bump in lockstep with compose.yaml's LD_PRELOAD path when moving
# to a newer base image that ships a different libmimalloc<N>.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini libmimalloc3 \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. UID/GID stable for predictable bind-mount perms.
RUN groupadd --system --gid 1001 mimir \
 && useradd --system --uid 1001 --gid mimir --create-home --shell /usr/sbin/nologin mimir

WORKDIR /app

# Bring in just the runtime artifacts from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/mimir /app/mimir
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/alembic /app/alembic
# Scheduler script for the sidecar tasks container.
COPY deploy/scheduler.sh /app/scheduler.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/db/mimir.db \
    WORKERS=2 \
    WORKER_TIMEOUT=60 \
    GIT_TERMINAL_PROMPT=0 \
    GIT_PROTOCOL_FROM_USER=0
# `GIT_TERMINAL_PROMPT=0` makes `git clone` / `git fetch` fail
# rather than block on a credential prompt; a hostile upstream
# can't pin a worker on stdin. `GIT_PROTOCOL_FROM_USER=0` tells
# git to treat all URLs in argv as not-user-supplied for the
# protocol-allowlist check (the allowlist defaults to safe schemes
# in modern git, but the explicit env makes the posture
# deploy-stable).

# /data is the only stateful path. Subdirs are created at build time
# so a bind-mounted /data with the right owner just works on first
# run. /app/Inboxes is a symlink so the default relative INBOXES
# config (Inboxes/<name>/git, resolved from cwd /app) still finds
# the mirrors.
RUN mkdir -p /data/db /data/Inboxes /data/Mainline \
 && ln -s /data/Inboxes /app/Inboxes \
 && ln -s /data/Mainline /app/Mainline \
 && chown -R mimir:mimir /data /app

USER mimir

EXPOSE 5000

# tini wraps every container role launched from this image (web,
# broker, tasks). The `--` separates tini's own args from the
# downstream command so compose-level `command:` overrides still
# compose correctly.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Web is migration-free at startup, alembic lives in the scheduler
# sidecar (`deploy/scheduler.sh`) so there's a single place that
# touches schema. Operator's compose must order the sidecar before
# the web service (depends_on with a healthcheck-gated sidecar, or
# bring the sidecar up first by hand) so gunicorn doesn't serve
# requests against an unmigrated DB on a fresh /data volume.
# `--timeout` (default ${WORKER_TIMEOUT}=60s) is higher than
# gunicorn's stock 30s so the occasional genuinely-slow render
# (cold cache on a heavy `msg_related` compute, oversized thread
# CTE) completes rather than getting SIGKILL'd mid-request. The
# 1.36.3 query-plan fix in `mimir.subsystems.recent_patches_touching`
# brought typical message-view renders well under a second; the
# margin here exists for the rare outlier rather than as the steady
# state. Tune via WORKER_TIMEOUT env if a deployment needs different
# headroom.
# `--access-logfile` is deliberately omitted: the app emits its own
# JSON access-log line per request via `mimir.web.hooks._log_request`
# (carries the same fields plus request_id + duration_ms). Letting
# gunicorn also write its stock Apache-style line produced two
# entries per request in the container log.
CMD ["sh", "-c", "exec gunicorn 'mimir:create_app()' --bind 0.0.0.0:5000 --workers ${WORKERS} --timeout ${WORKER_TIMEOUT} --error-logfile -"]
