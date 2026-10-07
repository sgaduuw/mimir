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
| `EMAIL_ALLOWLIST` | JSON array of substrings whose email addresses may display in full |
| `SECURITY_CONTACT` | Enables `/security.txt`, for example `mailto:security@example.com` |
| `TRUSTED_PROXY_HOPS` | Number of trusted proxies; zero for direct access |

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

The fast tier covers cheap per-inbox helpers. The slow tier covers sitemaps,
subsystem dashboards, trackers, and other expensive queries. The unqualified
command runs both. Use `--workers 1` to debug serially and `-v` for more output.

The container scheduler runs fast warming every minute and slow warming hourly.
Cache entries are reused or refreshed according to their remaining lifetime;
a tick does not necessarily rebuild every entry. Sitemaps have a one-hour origin
TTL and need `SITE_BASE_URL` for warming. Without it, sitemap warm targets are
omitted. Subsystem discovery can be empty until its slow-tier data has been warmed.

If you run outside Compose, arrange equivalent recurring commands with the broker
running. See the [scheduler settings](../deploy/README.md#scheduled-tasks) rather
than running a second scheduler alongside the container service.

### Change the thread render cap

`THREAD_VIEW_RENDER_CAP` sets messages per thread page, so it is part of the
cache keys for the sitemap index and every per-inbox sitemap. A new value makes
all of those miss at once, and `sitemap:index` alone takes tens of seconds to
compute on a large archive. Without warming, the first `/sitemap.xml` request
pays that cost.

1. Set the same value on the web, broker, and task services, and restart them
   together. Services with different values compute two sets of sitemap rows.
2. Run `uv run mimir warm-cache --tier slow` straight away, so no reader or
   crawler waits for the cold compute.

## Database maintenance

```sh
uv run mimir analyze
uv run mimir analyze --full
uv run mimir vacuum
```

`analyze` refreshes query-planner statistics with a bounded sample; `--full`
scans all index entries. The broker also analyzes after schema changes. The
scheduler defaults to daily bounded analysis and weekly full analysis.

`vacuum` compacts the database and attempts a WAL checkpoint. Schedule it during
quiet periods: it occupies the writer and needs substantial temporary disk space.
Allow headroom for a database-sized rebuild and WAL growth. A busy deployment
may prevent the final WAL truncation; inspect the command's reported sizes.
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
crawling attachment URLs, internal search, and `/api/` partials. Inspect or edit it:

```sh
uv run mimir admin robots list
uv run mimir admin robots show '*'
uv run mimir admin robots add GPTBot --disallow /
uv run mimir admin robots update '*' --add-disallow '/private/'
uv run mimir admin robots update '*' --crawl-delay 10
uv run mimir admin robots update '*' --set-content-signal ai-train=yes
```

Content Signals support `search`, `ai-input`, and `ai-train`, each `yes` or `no`.
Defaults are `search=yes, ai-input=no, ai-train=no`. Omit a key to express no
preference; use `--clear-content-signal KEY` or `--clear-all-content-signals`
to remove settings. The `*` stanza cannot be deleted. `admin robots reset --yes`
restores defaults, replacing custom rules. These are crawler instructions, not
an enforcement mechanism. Use `admin robots --help` for the command list.

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
