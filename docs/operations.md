# Operations

[README](../README.md) · [Managing the archive](archive.md) · [Deployment](../deploy/README.md)

Commands below assume a source checkout and a running broker. In Compose,
replace `uv run mimir` with `docker compose exec mimir-tasks mimir`.

## Configuration

Source checkouts read `.env` from the project root. Containers receive settings
through their environment; a Compose `.env` provides interpolation values and
does not automatically pass every setting into every container. Put settings in
the relevant service's `environment` block.

| Setting | Purpose |
| --- | --- |
| `SECRET_KEY` | Required, persistent secret of at least 16 characters |
| `DATABASE_URL` | SQLite URL; containers use `sqlite:////data/db/mimir.db` |
| `BROKER_SOCKET_PATH` | Client socket path; container default `/data/.broker.sock` |
| `SITE_NAME` | Displayed site name; default `mimir` |
| `SITE_BASE_URL` | Canonical public origin, including scheme; also needed for sitemap warming |
| `INBOXES` | JSON map of names to `mirror_path` and `upstream_url`, used at bootstrap |
| `EMAIL_ALLOWLIST` | JSON array of tokens whose email addresses may display in full: `@domain` (or bare `domain`) is exactly that domain (no subdomains), `local@` is that local part at any domain (spoofable: anyone can send as that local part from their own domain), `local@domain` is that exact address; case-insensitive |
| `SECURITY_CONTACT` | Enables `/security.txt`, for example `mailto:security@example.com` |
| `TRUSTED_PROXY_HOPS` | Number of trusted proxies; zero for direct access |
| `CRAWLER_BUDGET_PER_MINUTE`, `CRAWLER_BUDGET_SHARED_DIR`, `CRAWLER_UA_BUDGETS`, `CRAWLER_UA_EXEMPT`, `CRAWLER_DEFAULT_UA_PCT`, `CRAWLER_UNLISTED_POOL_PCT`, `CRAWLER_UA_MARKERS`, `CRAWLER_RETRY_AFTER_SEC` | Optional `429` throttle for crawlers (the global budget is the master switch, per-crawler values are percentages merged over a default crawler list, UA exemptions replace their default list, unlisted recognised crawlers get the default percentage and share a pool), off by default; see [deploy/README.md](../deploy/README.md#crawler-budgets) |
| `WORKERS` | Gunicorn worker count; also divides each unlisted crawler's budget per worker (the other crawler buckets are shared) |

Keep database and socket settings consistent across services. Set the canonical
site URL on the web, broker, and task services. Existing inbox records are edited
through [inbox commands](archive.md#change-or-remove-an-inbox), not overwritten by
environment changes. See [config.py](../mimir/config.py) for all settings and defaults.

Email hiding reduces exposure in the interface; it is not access control for
public upstream messages. Review the site's `/privacy` page and security contact
before publishing it. Optional security settings include `SECURITY_POLICY_URL`,
`SECURITY_ENCRYPTION_URL`, and `SECURITY_PREFERRED_LANGUAGES`.

## Keep dashboards warm

```sh
uv run mimir warm-cache --tier fast
uv run mimir warm-cache --tier slow
uv run mimir warm-cache
```

The fast tier covers cheap per-inbox helpers, the front page's 30-day message
total (a count of `articles`, so a cross-post counts once; refreshed hourly) and
the sitemap index, `/sitemap.xml`. The front page leaves that total out until the first warm run fills it. The slow
tier covers the other sitemaps, subsystem dashboards, trackers, release history, and other expensive queries. The unqualified
command runs both. Use `--workers 1` to debug serially and `-v` for more output.

The slow tier warms the per-inbox 30-day volumes used by the front page.
Those rows survive for two hours past their refresh deadline so the page
can show the previous counts during a refresh. An inbox without a retained
value stays in the inbox list and enters the busiest-inbox ranking once
it has a warmed count and recent activity.

The container scheduler runs fast warming every minute and slow warming hourly.
Cache entries are reused or refreshed according to their remaining lifetime;
a tick does not necessarily rebuild every entry. Non-archive sitemaps keep a
one-hour origin TTL. Archive files use an age-based TTL from one hour to 30 days,
with a Cloudflare edge TTL capped at one week. Sitemap warming needs
`SITE_BASE_URL`. Without it, sitemap warm targets are
omitted. Subsystem discovery can be empty until its slow-tier data has been warmed.

If you run outside Compose, arrange equivalent recurring commands with the broker
running. See the [scheduler settings](../deploy/README.md#scheduled-tasks) rather
than running a second scheduler alongside the container service.

Expired-cache cleanup does not depend on warming. The broker attempts it in
the background when its writer starts, then waits 60 seconds after each
attempt completes. Active VACUUM or a full writer queue defers that attempt;
one admitted purge can wait for the writer without queuing repeated purges.
Expired values remain unreadable while physical cleanup waits. Nonzero counts
appear in the broker log as `broker purge: N expired rows deleted`;
`warm-cache` no longer requests a purge or prints a purge count.
The `cache_delete`, `cache_delete_for_inbox` and `cache_purge_expired` RPCs are
removed; use supported inbox/admin operations for invalidation and the broker's
cleanup timer for expiry. Probes can submit `cache_set` with a short TTL and
observe its row expire. Rolling updates must also replace task containers older
than 3.51.0, which still send the retired trailing purge and receive `UnknownOp`.


### Change the thread render cap

`THREAD_VIEW_RENDER_CAP` sets messages per thread page, so it is part of the
cache keys for the sitemap indexes and every year and month sitemap file. A new
value makes all of those miss at once. Until they are warmed, the first request
for each sitemap pays its compute cost, which is about ten seconds for
`sitemap:index` on a large archive. It is also the size above which a message
page shows part of its thread rather than all of it.

1. Set the same value on the web, broker, and task services, and restart them
   together. Services with different values compute two sets of sitemap rows.
2. The scheduler warms the sitemap indexes within a minute. Run
   `uv run mimir warm-cache --tier slow` straight away to warm each inbox's
   current and previous year or month sitemap files too.
3. Purge the sitemap paths at the CDN edge. New origin cache keys do not
   invalidate archive copies already held at the edge, which can live for
   up to a week.

## Cache month archives at the edge

Deploy the month archive edge header before enabling this optional rule.
In Cloudflare, open **Caching > Cache Rules > Create rule**. Limit the rule
to the archive hostname and month paths; never include message or thread
paths, which have different full-page and HTMX representations.

For plans supporting regular expressions, use this expression, replacing
the hostname with your deployment's hostname:

```text
(http.host eq "archive.example.com" and
 http.request.uri.path matches r"^/[^/]+/[0-9]{4}/(0[1-9]|1[0-2])/$")
```

Cloudflare's `matches` operator requires Business or Enterprise. On other
plans, use equivalent narrowly scoped path conditions supported by your plan;
do not replace this with a broad HTML cache rule.

Set these rule options:

- **Cache eligibility:** Eligible for cache.
- **Edge TTL:** Use cache-control header if present, bypass cache if not.
  Do not set a fixed TTL or status-code TTL override. The origin's
  `Cloudflare-CDN-Cache-Control` selects 10 minutes for current/future months,
  one hour for the previous month and one week for older months.
- **Browser TTL:** Respect origin TTL, preserving the 600-second browser cache.
- **Cache key:** Keep the default, including the complete query string.
  Ignoring `filter` would mix filtered and unfiltered pages.

Check that no later matching rule overrides these settings. Cloudflare
consumes the Cloudflare-specific header, so inspect it at the origin and
use `CF-Cache-Status` and `Age` at the public URL. Request an old month twice:

```sh
curl -sS -D - -o /dev/null https://archive.example.com/list/2024/05/
```

The second request should normally be a HIT with an Age header; eviction or
requests reaching different edge locations can produce another MISS.
Confirm a filtered URL still shows its selected filter. A new reply or
late-arriving message can leave an old month stale for up to a week. Cache
hits do not reach the origin request log or crawler budget. Purge month
archive URLs, including cached query variants, after a release changes their
templates or content policy; an origin cache-key change cannot invalidate
an edge copy. Removing the rule alone does not purge copies already cached.

References: [Cloudflare Cache Rule settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/),
[CDN cache-control headers](https://developers.cloudflare.com/cache/concepts/cdn-cache-control/),
and [rule operators](https://developers.cloudflare.com/ruleset-engine/rules-language/operators/).

## Database maintenance

```sh
uv run mimir analyze
uv run mimir vacuum
```

`analyze` refreshes query-planner statistics from every index entry and holds
the writer lock while it runs. The broker also analyzes after schema changes,
and a deploy that migrates runs a pass before the broker serves. The scheduler
runs it weekly.

`vacuum` compacts the database and attempts a WAL checkpoint. Schedule it during
quiet periods: it occupies the writer and needs substantial temporary disk space.
Allow headroom for a database-sized rebuild and WAL growth. A busy deployment
may prevent the final WAL truncation; inspect the command's reported sizes.
Application and writer connections configure a connection-local 64 MiB
journal retention limit, also applied by the raw VACUUM connection. This is
not a peak-size cap: active read transactions or large writes can keep a
larger WAL. Once blocking transactions finish and a checkpoint completes, a
subsequent write can reset and shrink it without closing idle connections.
The policy is not persisted in the database, so querying the pragma from a
fresh ad-hoc SQLite connection does not report the app connection's setting.
The Compose example puts SQLite temporary files under `/data/db` so they use
the data volume rather than the container's temporary filesystem.

## Pause scheduled work

```sh
docker compose exec mimir-tasks touch /data/.scheduler-paused
# Perform maintenance, then resume:
docker compose exec mimir-tasks rm /data/.scheduler-paused
```

The loop notices the sentinel on its next tick. Already-running work continues.
The initial background warm on sidecar startup is separate from the loop and can
still run while paused. Do not treat the sentinel as proof that the writer is idle.

## Crawler policy

`/robots.txt` is generated from database rules. The default `*` stanza discourages
crawling attachment URLs, internal search, `/api/` partials and bare series-diff
paths. The end-anchored `/*/series/*/diff$` rule leaves query-bearing comparison
URLs crawlable. Inspect or edit it:

```sh
uv run mimir admin robots list
uv run mimir admin robots show '*'
uv run mimir admin robots add GPTBot --disallow /
uv run mimir admin robots update '*' --add-disallow '/private/'
uv run mimir admin robots update '*' --crawl-delay 10
uv run mimir admin robots update '*' --set-content-signal ai-train=yes
```

Content Signals support `search`, `ai-input`, and `ai-train`, each `yes` or `no`.
Defaults are `search=yes, ai-input=yes, ai-train=no`, with no crawl delay.
Crawler budgets provide backoff. Omit a key to express no
preference; use `--clear-content-signal KEY` or `--clear-all-content-signals`
to remove settings. The `*` stanza cannot be deleted. `admin robots reset --yes`
restores defaults, replacing custom rules. These are crawler instructions, not
an enforcement mechanism. Use `admin robots --help` for the command list.

`/llms.txt` repeats the `*` stanza's Content-Signal as is, so a change to it
shows in both files. There is no separate rule for agents: a fetch made on a
user's behalf is governed by that signal as configured.

### Reserved inbox names for lore redirects

Exact inbox names `r` and `all` are reserved for `/r/<id>` and `/all/<id>/`
recovery routes. Similar names such as `r-list` and `all-dev` remain valid.
Before deploying the release that introduces these routes, run both inventories
inside the OLD deployment environment, using its normal container wrapper:

```sh
mimir admin inbox list
python -c 'from mimir.config import settings; print("\n".join(sorted(settings.inboxes)))'
```

Inspect the full persisted inbox list and every effective bootstrap key for
exact `r` or `all`. Public page links are not a complete inventory. Resolve any
conflict with the old version's admin rename workflow (`mimir admin inbox update
--help`) and matching configuration before upgrading. The new broker rejects
reserved names even as the source of a rename, so a failed new deployment is
not the place to perform that remediation. The migration fails before changing
robots rules if a persisted conflict remains; it never renames or deletes data.

The default wildcard robots stanza disallows `/r/` and `/all/`, since the site
only accepts these aliases and never emits them. Its append-only data migration
retains custom paths, delays and content signals; no operator reset is needed.

## IndexNow

IndexNow notifications are disabled by default. To enable them, set
`INDEXNOW_KEY` and `SITE_BASE_URL` on the task process that runs `update`.
The web app also needs `INDEXNOW_KEY` to serve its verification file.
Generate a key with:

```sh
uv run python -c 'import secrets; print(secrets.token_hex(16))'
```

Mimir serves `/<key>.txt` and submits newly ingested message URLs to the configured
endpoint. Existing articles are not backfilled. If one tick exceeds
`INDEXNOW_MAX_PER_TICK` (default 1,000), the entire submission is skipped and logged;
sitemaps remain the discovery path. Notification errors do not abort ingestion.

## Logs and memory diagnostics

The [access-log reference](../deploy/README.md#access-logs) covers JSON request
fields, proxy-supplied country and ASN, and their trust boundaries.

For a broker job that hangs, see
[Diagnosing a stuck broker](../deploy/README.md#diagnosing-a-stuck-broker):
the broker warns while the job runs past its client timeout, and `SIGUSR1`
dumps every thread's stack without stopping it.

To investigate broker memory retention, set `TRACEMALLOC_INTERVAL_SECONDS`
on the broker to a positive interval in seconds, then restart it. For example,
`1800` writes a snapshot every 30 minutes under `/data/diagnostics/` and logs the
25 largest allocation groups. The default is off.

```sh
docker compose exec mimir-broker mimir tracemalloc-diff \
  /data/diagnostics/tracemalloc-<early>.pkl \
  /data/diagnostics/tracemalloc-<late>.pkl \
  --top 25 --filter-prefix /app/mimir
```

Substitute actual snapshot filenames. Snapshots consume disk space and tracing
adds overhead. Disable the setting and restart when finished, then remove the
snapshots you no longer need. Only load snapshots from a trusted source.


### Maintainer review counts

The slow global warm target `maintainer_review_counts` refreshes one primitive
inbox/count map per supported, normalized `M:` address under
`maintainer:review_counts_v2:<rules-version>:<address>`. Retired addresses from
the kernel's `.mailmap` contribute to the same identity. Profile and count
cache keys carry the rules version, so a reload invalidates both.
It uses the existing broker cache-write
path, directory TTL of 3600 seconds, refresh window and slow-tier extension
(7200 seconds by default). This is approximate freshness, not an hourly-update
guarantee. New owners enter when the cached directory refreshes. The profile
route checks current `M:` existence before reading either cache, so removed
owners return 404 even while old entries survive.

Requests only read these count entries. A missing or expired entry uses the
profile's alphabetical inbox list without counts. A present map, including an
empty one, supplies both the displayed inboxes and the header total because
profile and count entries can refresh independently. The existing registered
profile payload remains unchanged for rolling versions. Profile responses keep
their existing `public, max-age=600` policy without ETag or Last-Modified.

The read-only production measurement on 2026-11-02 covered all 2070 eligible
addresses: 3.950 seconds of summed query time, 4.067 seconds wall time for reads
and encoding, 113 ms maximum single query, and at most 2547 encoded JSON bytes
per address. Sample plans used the trailer role/address index. These figures
exclude cache writes and do not predict the complete warm-cycle duration.

### Window listing caches

Daily, month and catch-up listings share compact, level-1 zlib-compressed
window results across filter choices and pages. Daily targets use the existing slow tier.
The slow tier also defines the current-month and maximum catch-up-window
targets; arbitrary historical months and other catch-up dates stay on demand.
The proposed 406 windows across 203 inboxes took 38.974 seconds of serial
SQL time in the 2026-11-02 production measurement. This does not measure
concurrent warm-worker load or promise a complete warm-cycle duration.

The nominal listing TTL is 300 seconds. With the default slow warm extension
of 7200 seconds, a warmed row can live for 7500 seconds. The existing refresh
policy does not guarantee a refresh on every hourly tick or five-minute
freshness. This listing change preserves that policy. Oversized compressed
values are logged and skipped; web requests still render the complete result, while
listing warm targets report an error rather than claiming a successful warm.
