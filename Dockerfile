# syntax=docker/dockerfile:1
#
# Multi-stage build:
# - `builder`: install dependencies via Poetry into a project-local
#   venv. Poetry stays in this stage; only the venv ships.
# - `runtime`: clean python:3.14-slim with the venv + source, runs
#   gunicorn as a non-root user.
#
# Default volumes:
#   /app/Inboxes  — public-inbox mirrors (matches the default
#                   `INBOXES` config so a bind mount Just Works)
#   /data         — SQLite DB. Override DATABASE_URL accordingly.

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

# Non-root user. UID/GID stable for predictable bind-mount perms.
RUN groupadd --system --gid 1001 mimir \
 && useradd --system --uid 1001 --gid mimir --create-home --shell /usr/sbin/nologin mimir

WORKDIR /app

# Bring in just the runtime artifacts from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/mimir /app/mimir
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/alembic /app/alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/mimir.db \
    WORKERS=2

# Default mount points. Owned by the runtime user so a fresh bind
# mount inherits writable perms.
RUN mkdir -p /data /app/Inboxes \
 && chown -R mimir:mimir /data /app/Inboxes /app

USER mimir

EXPOSE 5000

# Auto-migrate on start so a fresh /data volume comes up usable.
# Acceptable for the single-user-archive deployment shape; managed
# DB shops should set DATABASE_URL elsewhere and run alembic
# explicitly.
CMD ["sh", "-c", "alembic upgrade head && exec gunicorn 'mimir:create_app()' --bind 0.0.0.0:5000 --workers ${WORKERS} --access-logfile - --error-logfile -"]
