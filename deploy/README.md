# Deployment

[README](../README.md) · [Operations](../docs/operations.md) · [Managing the archive](../docs/archive.md)

The [Compose example](../compose.yaml) runs Mimir on one host with a shared
`/data` directory. It builds the image locally; release automation also publishes
images to `ghcr.io/sgaduuw/mimir`. Pin a release when deploying published images.

## Start with Compose

You need Docker with Compose, Git, and Python 3 to generate a secret. From a fresh
checkout:

```sh
python3 -c 'import secrets; from pathlib import Path; Path(".env").open("x").write("SECRET_KEY=" + secrets.token_hex(32) + "\n")'
mkdir -p data/db data/Inboxes
sudo chown -R 1001:1001 data
```

Keep the generated secret. If `.env` already exists, edit it rather than replacing
it. Before starting, edit [compose.yaml](../compose.yaml) for your deployment:

- Set `SITE_BASE_URL` to the public origin on all three services.
- Set `TRUSTED_PROXY_HOPS` on the web service to match your proxy chain.
  The example has `2`; use `1` for one proxy, or `0` for direct local access.
- Set `INBOXES` consistently if you want a different initial list set.
  The scheduler will download and ingest configured lists automatically.
- Check the broker's `LD_PRELOAD` path: the example names an x86-64 Linux
  mimalloc library. Adapt the image and path if using another architecture.

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f mimir-broker
```

The web app listens on port **5000**. Put a TLS reverse proxy in front for a
public site. First startup can take longer while migrations and backfills run.
The local Flask server in the main README is for development.

## Services and persistent data

| Service | Role |
| --- | --- |
| `mimir-broker` | Owns database writes, migrations, bootstrap, and cache workers |
| `mimir` | Gunicorn web server; SQLite connections are read-only |
| `mimir-tasks` | Scheduler; syncs mirrors and submits database work to the broker |

The broker is mandatory. It sets `MIMIR_IS_BROKER=true`; the web and task services
do not. `MIMIR_DEPLOY=true` enables deployment safeguards. All clients use the
shared UNIX socket `/data/.broker.sock`.

| Location | Contents |
| --- | --- |
| `/data/db/` | SQLite database, WAL, and shared-memory files |
| `/data/Inboxes/` | public-inbox Git mirrors |
| `/data/Mainline/` | Tracked kernel Git trees |
| `/data/.broker.sock` | Broker socket |
| `/data/.last_*` | Scheduler timestamps |
| `/data/diagnostics/` | Optional memory snapshots |

The image runs as UID/GID 1001. Host bind mounts must be writable by that user.
Keep the database and mirrors together in your backup plan; messages are read
from mirrors at request time. For a filesystem copy of SQLite, stop all services
first and retain its WAL alongside the database. Restore into a stopped deployment.
Also back up configuration and any tracked-tree directories mounted separately.

## Startup and health checks

The broker runs `alembic upgrade head` on every start, bootstraps inboxes when
needed, analyzes after schema changes, and fills missing thread roots before
accepting RPCs. Thread-root verification runs afterward in the background.

Startup markers live beside the broker socket. They track completed work;
`.migrated` does **not** cause future migrations to be skipped. A remaining
unrooted row also triggers another backfill even if a prior marker exists.
Check broker logs for `backfill complete` and `remaining=0`, or investigate
`backfill incomplete`.

```sh
docker compose exec mimir-broker mimir broker-ping --socket /data/.broker.sock
```

Both other services wait for broker health. Allow the startup health-check
budget to cover migrations and backfills on your actual dataset. The socket
can exist before requests are served, so early pings may time out.
`/healthz` checks web liveness; `/readyz` checks database access.

For a manually managed systemd or Podman deployment, align its startup timeout
with the same work. Changing Compose does not change another deployment's timeout.
To request a fresh startup analysis, remove `/data/.broker_initial_analyze`
while managing a broker restart; migrations already trigger analysis themselves.

## Scheduled tasks

The task service runs [scheduler.sh](scheduler.sh). Configure these on
`mimir-tasks`; values are seconds.

| Variable | Default | Task |
| --- | --- | --- |
| `WARM_CACHE_EVERY` | 60 | Fast per-inbox cache warming |
| `WARM_CACHE_SLOW_EVERY` | 3600 | Slow warming, including sitemap targets when configured |
| `UPDATE_EVERY` | 300 | Discover/fetch mirrors and ingest |
| `UPDATE_MAINLINE_EVERY` | 600 | Refresh tracked trees and MAINTAINERS |
| `ANALYZE_EVERY` | 86400 | Bounded planner statistics refresh |
| `ANALYZE_FULL_EVERY` | 604800 | Full statistics refresh |
| `VACUUM_EVERY` | 604800 | Database compaction |

Successful task times persist under `/data/.last_*`, so restarts do not reset
long cadences. Failures remain eligible for retry. Set `SCHEDULER_VERBOSE=-v`
for more detail. Only the fast warm runs as the initial background pre-flight;
slow warming follows the periodic loop. See [maintenance and pausing](../docs/operations.md#pause-scheduled-work).

## Access logs

Mimir emits one JSON access record per request, including unmatched routes.
Alongside timestamp, request ID, method, path, status, duration, client IP,
user agent and referrer, it records:

| Field | Source |
| --- | --- |
| `sec_fetch_mode` | `Sec-Fetch-Mode` |
| `sec_fetch_site` | `Sec-Fetch-Site` |
| `sec_fetch_dest` | `Sec-Fetch-Dest` |
| `sec_fetch_user` | `Sec-Fetch-User` |
| `accept` | `Accept` |
| `accept_language` | `Accept-Language` |
| `hx_request` | `HX-Request` |
| `purpose` | `Purpose` |
| `sec_purpose` | `Sec-Purpose` |
| `country` | `CF-IPCountry` |
| `asnum` | Custom `X-ASN` request header |
| `response_bytes` | Known response body length before proxy compression |

Header values are raw strings, with JSON escaping; absent headers are `null`.
They are observations, not verified identities or automatic bot classifications.
Country and ASN require upstream enrichment; mimir performs no IP lookup.
Only treat them as IP metadata when a trusted proxy overwrites incoming values
and direct access to the origin is restricted. `TRUSTED_PROXY_HOPS` configures
forwarded IP handling; it does not authenticate these additional headers.

Cloudflare can supply `CF-IPCountry` through
[IP geolocation](https://developers.cloudflare.com/network/ip-geolocation/).
`X-ASN` is this application's proxy contract, not a default Cloudflare header.
A [request header transform](https://developers.cloudflare.com/rules/transform/request-header-modification/)
can set it dynamically to `to_string(ip.src.asnum)`. Ensure intervening proxies
forward both headers. Without that setup the corresponding fields remain `null`.

`response_bytes` is `0` for HEAD and bodyless responses, and `null` when no
content length is known. Logging does not read streamed bodies or measure
bytes actually delivered, and downstream compression can change wire size.
Requests served entirely by a CDN cache do not reach this access log.

## Reverse proxy

`TRUSTED_PROXY_HOPS` controls forwarded client address, scheme, and host handling.
Count only trusted proxies in front of the app. Restrict direct access to the
origin when relying on proxy headers; an exposed origin lets callers supply them.
This setting does not authenticate country or ASN headers.

### Caddy

```caddy
mimir.example.com {
    reverse_proxy 127.0.0.1:5000
    encode gzip zstd
}
```

See [the Caddy example](caddy/Caddyfile.example). For a single Caddy proxy,
set `TRUSTED_PROXY_HOPS=1`.

### nginx

The [nginx example](nginx/mimir.conf.example) includes TLS, compression, and
forwarded headers. Supply your certificate paths and hostname, then set the
proxy-hop count for your network.

## Routine operation

Use `docker compose exec mimir-tasks mimir <command>` for ad-hoc archive and
maintenance commands. The broker must be running. See
[archive administration](../docs/archive.md) and [operations](../docs/operations.md)
for reindexing, failure replay, cache warming, crawler policy, and diagnostics.
