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

| Env var                 | Default         | What it controls                              |
| ----------------------- | --------------- | --------------------------------------------- |
| `WARM_CACHE_EVERY`      | `60` (s)        | Refresh dashboard helpers                     |
| `UPDATE_EVERY`          | `300` (s)       | Sync upstream + ingest new commits            |
| `UPDATE_MAINLINE_EVERY` | `600` (s)       | Fetch `linux.git` + (re)parse `MAINTAINERS`   |
| `ANALYZE_EVERY`         | `86400` (s)     | Refresh `sqlite_stat1` (bounded)              |
| `ANALYZE_FULL_EVERY`    | `604800` (s)    | Full ANALYZE (no `analysis_limit` cap)        |
| `VACUUM_EVERY`          | `604800` (s)    | Compact DB + collapse WAL                     |

Timing is wall-clock, persisted across container restarts via
`/data/.last_<task>` sentinel files. Without persistence a release
rollover at a cadence shorter than the slowest task (weekly VACUUM,
daily ANALYZE) would reset the timer on every restart and the task
would never fire (#202); reading the sentinel mtime on boot means
the cadence intent survives. A task that overruns its slot is logged
but the loop continues; sentinels are only touched on successful
runs so a failure doesn't push the next retry out by a full cadence.

### Ad-hoc pause

For maintenance windows (e.g. `admin canonicals backfill`, manual
SQL, `reindex` over a large epoch) where scheduler write contention
would extend the work, quiesce the loop with a sentinel file:

```sh
# Pause: scheduler stops firing tasks within ~10s. Currently-running
# tasks finish first; new ticks are skipped while the sentinel exists.
podman exec mimir-tasks touch /data/.scheduler-paused

# Resume.
podman exec mimir-tasks rm /data/.scheduler-paused
```

The journal carries one `paused` line on entry and one `resumed` line
on exit, not one per tick. Tasks that became due during the pause fire
in the next tick after resume; the cadence isn't reset.

The sentinel gates only the in-loop ticks; the boot-time
`alembic upgrade head` + `bootstrap-inboxes` + `(initial)`
warm-cache/update passes ignore it. If you need to restart the
sidecar mid-maintenance, drop the sentinel first.

The sidecar owns schema: `alembic upgrade head` runs once on start
(before the loop) and is the single place that touches DDL.
`bootstrap-inboxes` follows so env-configured inboxes exist before
the web tier comes up. Finally a pre-flight `warm-cache` runs so
the dashboard cache is hot before serving. The web container's
`depends_on: { mimir-tasks: { condition: service_healthy } }`
gates gunicorn on the `/data/.migrated` healthcheck sentinel
(touched only after those passes succeed), so a fresh `/data`
volume migrates before serving any requests. systemd deployments
are unaffected, `mimir.service` runs its own
`ExecStartPre=alembic upgrade head` and an
`ExecStartPre=mimir bootstrap-inboxes`.

**Post-migrate ANALYZE**: under broker mode (see below) the
broker container runs a bounded `ANALYZE` on first start, gated
by `/data/.broker_initial_analyze`. Web tier gates on the
broker's healthcheck so cold requests after a deploy never walk
un-`ANALYZE`'d indexes. Without broker mode the daily scheduled
`ANALYZE` in the loop above catches stat drift after each ingest
delta; new indexes from a fresh migration start life invisible to
the planner until that next pass fires.

### Broker mode (opt-in)

The write-broker is a third container that owns the sole SQLite
writer connection for every periodic and admin write op: cache
writes, ingest, the four backfills, warm-cache fan-out,
`update-mainline`, `analyze`, `vacuum`, `bootstrap-inboxes`, the
seven `admin inbox` CRUD ops, and `admin failures replay`. Web
and scheduler-tasks containers run with `PRAGMA query_only=1`
and dispatch their writes via RPC over a UNIX socket at
`/data/.broker.sock`. See `mimir/broker/` for the daemon
internals; the high-level contract is "broker is the sole SQLite
writer; everything else is `query_only=1`."

Opt-in in 1.x; 2.0.0 will make it mandatory. Enable by un-
commenting three blocks in `compose.yaml`:

1. `BROKER_SOCKET_PATH` + `MIMIR_ROLE: "web"` on the `mimir`
   service env.
2. `BROKER_SOCKET_PATH` + `MIMIR_ROLE: "tasks"` on the
   `mimir-tasks` service env.
3. The full `mimir-broker:` service block lower in the file,
   plus add `mimir-broker: { condition: service_healthy }` to
   the web service's `depends_on:`.

The broker's healthcheck is `mimir broker-ping --socket
/data/.broker.sock` with `start_period: 30s` to cover the
first-start bounded `ANALYZE`. On a fresh `/data` volume the
boot chain is: scheduler-tasks runs alembic +
`bootstrap-inboxes` + pre-flight warm-cache → touches
`/data/.migrated` → broker comes up, runs post-migrate ANALYZE
if `/data/.broker_initial_analyze` is missing, then accepts
RPCs → web tier starts serving. Subsequent restarts skip the
ANALYZE (sentinel-gated).

To force a re-ANALYZE on the next broker start (e.g. after a
manual schema change), delete the sentinel:

```sh
podman exec mimir-broker rm /data/.broker_initial_analyze
podman restart mimir-broker
```

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

Broker mode isn't packaged for systemd; single-host compose is the
canonical multi-process shape. A systemd broker would mean a fifth
unit (`mimir-broker.service`) gating the others with
`After=mimir-broker.service` and a Type=notify readiness signal.
Possible, not shipped; ask if needed.

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
canonical `compose.yaml` ships `TRUSTED_PROXY_HOPS=2` for the
production-targeted "Tailscale Funnel → Caddy → mimir" chain; drop to
`1` for a single Caddy / nginx layer. Leave at `0` if mimir is
reachable directly, otherwise anyone could spoof those values via a
forged `X-Forwarded-For`.

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
