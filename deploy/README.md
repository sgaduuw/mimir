# Deployment

Examples for the three realistic mimir deployment shapes.

## Container (Docker / Compose)

The canonical files live at the repo root: `Dockerfile` and
`compose.yaml`.

```sh
echo "SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')" > .env
docker compose up --build
```

The image:
- Multi-stage build; final ~slim runtime, runs as `mimir:1001`.
- `CMD` is just `gunicorn` with `${WORKERS:-2}` workers, schema
  migration is the sidecar's job (see below). Order the sidecar
  ahead of the web service in `compose.yaml` so the first run
  doesn't serve requests against an unmigrated DB.
- Volume mount: `/data` is the single stateful path.
  - `/data/db/mimir.db`, SQLite DB + WAL files
    (`DATABASE_URL=sqlite:////data/db/mimir.db` baked in).
  - `/data/Inboxes/<name>/git/`, public-inbox mirrors.
    `/app/Inboxes` is a symlink to this so the default
    relative `INBOXES` config resolves correctly.
  - Pre-flight on the host:
    `mkdir -p data/db data/Inboxes && chown -R 1001:1001 data`.
- `EXPOSE 5000`, proxy from your reverse proxy of choice.
- Container healthcheck pings `/healthz`.

Bumping worker count: set `WORKERS` in the env / compose file.
Going much past 4 isn't useful for this workload, the SQLite
write lock serializes anyway.

### Scheduled-tasks sidecar

`compose.yaml` ships a second service, `mimir-tasks`, that runs
`/app/scheduler.sh` in a loop and replaces cron / systemd timers
for container deployments. It shares the `/data` volume with the
web container, exposes no ports, and runs the same image with a
different `command:`.

| Env var            | Default         | What it controls                      |
| ------------------ | --------------- | ------------------------------------- |
| `WARM_CACHE_EVERY` | `60` (s)        | Refresh dashboard helpers             |
| `UPDATE_EVERY`     | `300` (s)       | Sync upstream + ingest new commits    |
| `ANALYZE_EVERY`    | `86400` (s)     | Refresh `sqlite_stat1`                |
| `VACUUM_EVERY`     | `604800` (s)    | Compact DB + collapse WAL             |

Timing is relative to container start, not wall-clock. A task that
overruns its slot is logged but the loop continues; cadences use
absolute timestamps so they don't drift.

The sidecar owns schema: `alembic upgrade head` runs once on start
(before the loop) and is the single place that touches DDL. After
a successful migration, scheduler.sh touches `/data/.migrated`,
which the sidecar's healthcheck looks for. The web container's
`depends_on: { mimir-tasks: { condition: service_healthy } }` then
gates gunicorn on that sentinel, so a fresh `/data` volume migrates
before serving any requests. systemd deployments are unaffected  
`mimir.service` runs its own `ExecStartPre=alembic upgrade head`.

## systemd

`deploy/systemd/` carries unit files for `/opt/mimir`:

| Unit                          | Role                                  |
| ----------------------------- | ------------------------------------- |
| `mimir.service`               | Main web server (gunicorn).           |
| `mimir-warm-cache.service`    | Refresh dashboard helpers.            |
| `mimir-warm-cache.timer`      | Every minute.                         |
| `mimir-analyze.service`       | Refresh SQLite query-planner stats.   |
| `mimir-analyze.timer`         | Daily at 04:30.                       |
| `mimir-vacuum.service`        | Compact DB + collapse WAL.            |
| `mimir-vacuum.timer`          | Weekly Sunday 04:00.                  |

Setup sketch:

```sh
sudo useradd --system --create-home --home-dir /opt/mimir mimir
sudo -u mimir git clone https://github.com/sgaduuw/mimir /opt/mimir
cd /opt/mimir
sudo -u mimir python3.14 -m venv .venv
sudo -u mimir .venv/bin/pip install poetry
sudo -u mimir .venv/bin/poetry install --without dev

# Drop your env file (SECRET_KEY at minimum)
sudo -u mimir cp .env.example .env  # then edit

# Install the units
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mimir.service
sudo systemctl enable --now mimir-warm-cache.timer
sudo systemctl enable --now mimir-analyze.timer
sudo systemctl enable --now mimir-vacuum.timer
```

`mimir.service` includes the standard hardening flags (NoNewPrivileges,
ProtectSystem=strict, etc). `ReadWritePaths=/opt/mimir` is what lets
SQLite write back; if you move the DB elsewhere, add that path too.

VACUUM needs an exclusive lock for the post-VACUUM
`wal_checkpoint(TRUNCATE)` to actually collapse the WAL. The unit
does *not* stop `mimir.service` automatically, systemd refuses
transactions that simultaneously stop and start the same unit. If
you care about the WAL truncate landing fully, run vacuum during a
brief planned window:

```sh
sudo systemctl stop mimir.service
sudo systemctl start mimir-vacuum.service  # ~2 min on lkml-scale
sudo systemctl start mimir.service
```

Or let the weekly timer fire as-is and accept that the WAL truncate
is best-effort while the web is serving, the VACUUM rebuild
itself still succeeds.

## Reverse proxy

When the app sits behind a reverse proxy, set `TRUSTED_PROXY_HOPS` to
the number of trusted proxies in front. mimir then wraps its WSGI app
in Werkzeug's `ProxyFix` so `request.remote_addr` (and `.scheme` /
`.host`) reflect the real client instead of the proxy's address. The
canonical `compose.yaml` ships `TRUSTED_PROXY_HOPS=1` for the typical
"Caddy → mimir" shape. Leave at `0` if mimir is reachable directly  
otherwise anyone could spoof those values via a forged
`X-Forwarded-For`.

### Caddy (preferred, automatic HTTPS)

```caddy
mimir.example.com {
    reverse_proxy 127.0.0.1:5000
    encode gzip zstd
}
```

Full example: `deploy/caddy/Caddyfile.example`.

### nginx

`deploy/nginx/mimir.conf.example` carries a complete site block
with TLS, gzip, and the `X-Forwarded-Proto` / `X-Request-Id`
headers mimir reads.

## Out of scope

- Pre-built image push to a registry. Build locally; pin a tag if
  you want reproducibility across hosts.
- Kubernetes manifests. systemd + Docker Compose covers the
  realistic single-host archive shape.
- HTTPS certificate provisioning. Caddy automates this; the nginx
  example assumes Let's Encrypt or similar already provisioned a
  cert.
