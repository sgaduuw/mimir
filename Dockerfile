# syntax=docker/dockerfile:1
#
# Multi-stage build:
# - `builder`: install dependencies via Poetry into a project-local
#   venv. Poetry stays in this stage; only the venv ships.
# - `runtime`: clean python:3.14-slim with the venv + source, runs
#   gunicorn as a non-root user.
#
# Default volume:
#   /data, single state directory. Holds:
#     /data/db       , SQLite DB (DATABASE_URL=sqlite:////data/db/mimir.db)
#     /data/Inboxes  , public-inbox mirrors. /app/Inboxes is a
#                       symlink to this so the default relative
#                       `INBOXES` config still resolves cleanly.
#     /data/Mainline , Linus's linux.git (and other configured
#                       upstream trees) for MAINTAINERS + Link:
#                       trailer indexing. /app/Mainline symlinks
#                       through so the default relative
#                       MAINLINE_TREE_PATH resolves to here.
#
# Operator must `chown -R 1001:1001 <host-data-dir>` before bringing
# the container up (rootful podman / docker, no UID remapping).

FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

WORKDIR /app

RUN pip install --no-cache-dir 'poetry>=2.0,<3.0'

# Copy only what dependency resolution needs first so the wheel-cache
# layer survives unrelated source changes.
COPY pyproject.toml poetry.lock README.md ./
COPY mimir/__init__.py ./mimir/__init__.py

RUN poetry install --without dev --no-root

# Now the rest of the source.
COPY mimir/ ./mimir/
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Install the project itself (uses the same venv). `--only-root`
# can't be combined with group flags in Poetry 2.x; the deps were
# already installed --without dev above.
RUN poetry install --only-root


FROM python:3.14-slim AS runtime

# git: `mimir update` shells out to `git clone --mirror` / `git fetch`
# to sync upstream public-inbox epochs. ca-certificates: HTTPS verify
# against lore.kernel.org and other upstream hosts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
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
