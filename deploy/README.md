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
- `CMD` runs `alembic upgrade head` then `gunicorn` with
  `${WORKERS:-2}` workers.
- Volume mounts:
  - `/app/Inboxes` — public-inbox mirrors. Default `INBOXES`
    config in `mimir/config.py` looks here, so a bind mount Just
    Works.
  - `/data` — SQLite DB. `DATABASE_URL=sqlite:////data/mimir.db`
    is baked in.
- `EXPOSE 5000` — proxy from your reverse proxy of choice.
- Container healthcheck pings `/healthz`.

Bumping worker count: set `WORKERS` in the env / compose file.
Going much past 4 isn't useful for this workload — the SQLite
write lock serializes anyway.

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
does *not* stop `mimir.service` automatically — systemd refuses
transactions that simultaneously stop and start the same unit. If
you care about the WAL truncate landing fully, run vacuum during a
brief planned window:

```sh
sudo systemctl stop mimir.service
sudo systemctl start mimir-vacuum.service  # ~2 min on lkml-scale
sudo systemctl start mimir.service
```

Or let the weekly timer fire as-is and accept that the WAL truncate
is best-effort while the web is serving — the VACUUM rebuild
itself still succeeds.

## Reverse proxy

### Caddy (preferred — automatic HTTPS)

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
