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
docker compose exec mimir-broker mimir-broker-ping --socket /data/.broker.sock
```

`mimir-broker-ping` is the broker health check in the example Compose file.
It does not load the application, so it stays fast on a busy host.
`mimir broker-ping` gives the same answer but loads the whole application
first, which can take longer than the check's timeout under load. If your
own Compose file still calls `mimir broker-ping`, change its `test:` line.

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
| `ANALYZE_FULL_EVERY` | 604800 | Planner statistics refresh |
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
| `sec_ch_ua` | `Sec-CH-UA` |
| `sec_ch_ua_mobile` | `Sec-CH-UA-Mobile` |
| `sec_ch_ua_platform` | `Sec-CH-UA-Platform` |
| `accept` | `Accept` |
| `accept_language` | `Accept-Language` |
| `hx_request` | `HX-Request` |
| `purpose` | `Purpose` |
| `sec_purpose` | `Sec-Purpose` |
| `country` | `CF-IPCountry` |
| `response_bytes` | Known response body length before proxy compression |

Header values are raw strings, with JSON escaping; absent headers are `null`.
They are observations, not verified identities or automatic bot classifications.
Chromium browsers send the three `sec_ch_ua*` Client Hints by default over HTTPS.
Together they add about 150 bytes to a Chromium request's record and about 70
bytes of `null`s to every other record.
Country requires upstream enrichment; mimir performs no IP lookup.
Only treat them as IP metadata when a trusted proxy overwrites incoming values
and direct access to the origin is restricted. `TRUSTED_PROXY_HOPS` configures
forwarded IP handling; it does not authenticate these additional headers.

Each request can add up to about 8 KB per logged header to its access
record, so a client can make single lines large. Cap the container log
size on the host, for example Docker's `json-file` driver with
`max-size` and `max-file`, or the equivalent for your log driver.

Cloudflare can supply `CF-IPCountry` through
[IP geolocation](https://developers.cloudflare.com/network/ip-geolocation/).
Ensure intervening proxies forward it. Without that setup `country` remains `null`.

`response_bytes` is `0` for HEAD and bodyless responses, and `null` when no
content length is known. Logging does not read streamed bodies or measure
bytes actually delivered, and downstream compression can change wire size.
Requests served entirely by a CDN cache do not reach this access log.

## Crawler budgets

One crawler can take most of the web tier, and a CDN limit cannot single it
out: it counts per client IP, and a crawl spread over thousands of IPs stays
under any limit that would not also hit readers. The web tier can instead
answer `429` with `Retry-After` and `Cache-Control: no-store` to a crawler
over its budget, with no database or render work. It is off by default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CRAWLER_BUDGET_PER_MINUTE` | `0` | Requests per minute for all configured crawlers combined; `0` turns the whole gate off |
| `CRAWLER_UA_BUDGETS` | `{}` (defaults below) | JSON map of crawler name to a percentage (1 to 100) of the global budget, merged over the defaults; `0` drops a default |
| `CRAWLER_DEFAULT_UA_PCT` | `10` | Share for a recognised crawler with no entry in `CRAWLER_UA_BUDGETS` |
| `CRAWLER_UNLISTED_POOL_PCT` | `30` | Percentage of the global budget that all unlisted crawlers share between them, on top of their own share |
| `CRAWLER_UA_MARKERS` | `["bot", "crawler", "spider", "curl", "wget", "python-requests", "lightpanda"]` | JSON array; a product token ending in one of these plus a slash marks a crawler |
| `CRAWLER_IMPOSSIBLE_HEADERS_PCT` | `1` | Percentage of the global budget for the one shared bucket of requests with `Sec-Fetch-Site: none` and a `Referer`; `0` turns it off |
| `CRAWLER_RETRY_AFTER_SEC` | `120` | Seconds sent in `Retry-After` |

The defaults are the heavy known crawlers that name themselves in the
User-Agent, each at 10%: Applebot, Bytespider, PetalBot, meta-externalagent,
facebookexternalhit, Slurp, msnbot and Baiduspider. Googlebot and bingbot are
also listed, at 80% (#848): search crawlers back off on `429` and visit a few
times a day, so a small share split over many workers refused them.
`CRAWLER_UA_BUDGETS` is merged over them, not a replacement:
`CRAWLER_UA_BUDGETS='{"Applebot": 50}'` changes only Applebot, and
`{"Bytespider": 0}` drops Bytespider. New keys are added after the defaults,
and an overridden default keeps its position. Each key is a plain
case-insensitive substring of the `User-Agent` header, first match in that
order wins, so pick distinctive names. Blank keys, and keys equal to a marker
(`bot` would match the `CUBOT` browser), are refused at startup. With a global
budget of 120 a minute, `{"Applebot": 10}` gives Applebot 12 a minute. A
request needs a token in every bucket it draws on; a refused request spends
none.

A crawler you did not list is still recognised by a product token: a name
ending in a marker, followed by a slash, such as `GPTBot/1.0`,
`AhrefsBot/7.0` or `curl/8.5.0`. A token that is just a marker (`bot/1.0`)
counts too. It gets the default share, so **with the global budget set, every
recognised crawler is held to 10% unless you give it its own percentage**. The
slash is required, so a browser on a device called `CUBOT X30` is never gated. Unlisted crawlers
also share one pool at `CRAWLER_UNLISTED_POOL_PCT` of the global budget
(default 30%), so invented names cannot together starve real crawlers; listed
crawlers never draw on the pool. The flip side: unlisted real crawlers
(GPTBot, for example) share that pool with any flood of invented names,
so list a crawler to protect it from a flood. Each unlisted crawler has its
own bucket, keyed by its lowercased token; at most 256 are held, idle ones are
dropped after ten minutes, and past 256 new names share one overflow bucket at
the default share. Never gated: robots, sitemaps, `/healthz`, `/readyz`,
static files, and requests that are not a recognised crawler by their
`User-Agent` (the impossible-headers bucket below still applies). The Markdown
pages (`.md`) draw on the same buckets as the HTML pages. An agent fetching a
page on a user's behalf, such as `Claude-User (claude-code/...)`, carries no
marker token, so it is not a recognised crawler and is not gated by its
`User-Agent` on either.

The default markers include `curl`, `wget` and `python-requests`, so your own
scripts and smoke checks that use those tools count as a crawler once the
global budget is set. Give them their own percentage in `CRAWLER_UA_BUDGETS`
(for example `{"curl": 20}`) or send a different `User-Agent`.

A request that did not name a crawler in its `User-Agent` but sends
`Sec-Fetch-Site: none` together with a `Referer` (#903) is held to
`CRAWLER_IMPOSSIBLE_HEADERS_PCT` of the global budget, in one bucket shared by
all such requests. `none` means a user-initiated navigation (a typed URL or a
bookmark), and a browser sends no `Referer` on one, so a real reader almost
never matches (8 requests in three days outside the crawl pools, #903); the
pair is a client faking a click. It charges the global bucket but
not the unlisted pool, and a declared crawler keeps its own bucket. Like the
rest of the gate it does nothing while the global budget is `0`.

Buckets live in each gunicorn worker, so each refills at the budget divided by
`WORKERS` (mimir reads the same `WORKERS` that sets `--workers`): global 120
with 8 workers and Applebot at 10% is 12 a minute overall, 1.5 a minute per
worker. A bucket holds ten seconds of its rate, or one token if that is more.
In any window the workers together admit at most that window's share of the
budget plus ten seconds' worth, so a worker can burst ten seconds' worth at
start or after an idle spell. They admit less when a crawler's requests land
unevenly on the workers.

To pick a budget, measure the crawler's request rate in the access log first
(its `ua` field), then set the global budget to about a third of the combined
crawler rate and give each crawler a percentage of it (Applebot at about 9,000
an hour, 150 a minute, is a global 50 with Applebot at 100, or a global 100
with Applebot at 50). Watch its request rate and the `crawler budget:`
warnings (logger `mimir.web.crawler_budget`, at most one line a minute per
bucket per worker, with the deferred count; the header bucket's line reads
`matched on request headers, not User-Agent` instead of `crawler=<name>`) and
adjust. Crawlers slow down on
`429`, so the rate should settle near the budget.

## Reverse proxy

`TRUSTED_PROXY_HOPS` controls forwarded client address, scheme, and host handling.
Count only trusted proxies in front of the app. Restrict direct access to the
origin when relying on proxy headers; an exposed origin lets callers supply them.
This setting does not authenticate the country header.

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
