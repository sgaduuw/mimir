# mimir

[![ci](https://github.com/sgaduuw/mimir/actions/workflows/ci.yml/badge.svg)](https://github.com/sgaduuw/mimir/actions/workflows/ci.yml)

A toy archiver and read-only web UI for [public-inbox][pi] v2 mailing
list archives. Out of the box it indexes the [Linux Kernel Mailing
List][lkml] and [linux-fsdevel][fsd], but any list published by
public-inbox works. The displayed site name is configurable; "mimir"
appears only as the page generator.

[lkml]: https://lore.kernel.org/lkml/
[fsd]: https://lore.kernel.org/linux-fsdevel/
[pi]: https://public-inbox.org/

## Scope and assumptions

mimir targets a personal or small-team archive deployment. The
defaults assume:

- **Single host, single SQLite file.** The web side scales fine
  behind a CDN / reverse proxy; writes (ingest) need to be
  serialized to one process at a time. No Postgres path; SQLite
  handles the lkml-scale corpus comfortably.
- **Multi-million-message scale.** Tested on the full lkml corpus
  (~6 M articles, ~3.6 GB DB on disk). Comfortable on a laptop;
  growing past ~50 M would warrant revisiting SQLite.
- **Single-user ingest at a time.** `mimir update` /
  `ingest` are not safe to run concurrently against the same DB.
  Multiple readers (web server + warm-cache cron) are fine, WAL
  mode handles that.
- **Append-only upstreams.** public-inbox v2 commits are append-
  only by design; mimir's "no updates ever" rule for existing rows
  assumes that. If an upstream rewrites history, you'll need to
  wipe and re-ingest.
- **Mirrors stay on disk.** The git mirror is the source of truth;
  re-ingesting is cheap, re-cloning isn't (~20 GB and hours for
  the full lkml archive).

## What it does

- Walks one or more public-inbox v2 epoch repositories (`0.git`,
  `1.git`, …), where each commit's tree contains a single `m` blob
  holding the raw RFC 5322 bytes of one message.
- Parses each message with the stdlib email API under
  `policy.default`, proper handling of MIME multiparts, RFC 2231
  filenames, RFC 2047 encoded headers, and the like.
- **Treats the public-inbox mirror as the source of truth.** SQLite
  is a lean index that records, per message, only what's needed to
  find and display it: `message_id` + threading hints + a few
  display fields (`subject`, `author`, `date`). Body, full headers,
  and attachment bytes are *not* duplicated into SQLite, they're
  re-parsed from the git blob on demand.
- **Cross-posted messages dedupe.** A message that appears in both
  lkml and linux-fsdevel produces one `articles` row plus one
  `article_lists` row per inbox.
- Re-runs are incremental: only new commits since the last recorded
  HEAD SHA per (inbox, epoch) are visited.

The read path costs roughly 2 ms per message (SQL lookup + dulwich
blob fetch + parse). The mirror must be present on disk at *read*
time, not just at ingest time.

## Requirements

- Python 3.14 (declared in `.python-version`)
- [Poetry](https://python-poetry.org/) for dependency management

## Setup

```sh
poetry install
poetry run alembic upgrade head
```

Then create a `.env` in the project root. Minimum:

```
SECRET_KEY=<run: python -c "import secrets; print(secrets.token_hex(32))">
```

Defaults baked into `mimir/config.py`:

- `DATABASE_URL`, `sqlite:///<project_root>/mimir.db`. Override
  per-deployment, e.g. `DATABASE_URL=sqlite:////data/mimir.db` for a
  container with a persistent volume.
- `SITE_NAME`, `mimir`. The displayed brand in titles, the nav, and
  the `/` heading; set this to whatever you want the public site to
  be called.
- `INBOXES`, a JSON map of `{name: {mirror_path, upstream_url}}`.
  Defaults cover lkml and linux-fsdevel under `Inboxes/<name>/git`.
  See `mimir/config.py` for the exact shape; override via env, e.g.
  `INBOXES='{"lkml": {"mirror_path": "...", "upstream_url": "..."}}'`.
- `EMAIL_ALLOWLIST`, substrings whose email addresses display in
  full; everyone else gets `<hidden>`.
- Per-inbox author trackers (each renders as a tile on the inbox
  dashboard) are mutated via `mimir admin inbox trackers {set,add,
  remove,clear} <inbox> [<label>=<email-substring>]`. State lives
  on `Inbox.tracked_authors` (JSON column), not in env, so each
  inbox can carry its own list and admin edits survive restarts.
- `SECURITY_CONTACT`, enables `/security.txt` and
  `/.well-known/security.txt` (RFC 9116). Typical value:
  `mailto:security@example.com`. Without it, both routes 404, better
  than serving a contact-less file. Optional companions:
  `SECURITY_POLICY_URL`, `SECURITY_ENCRYPTION_URL`,
  `SECURITY_PREFERRED_LANGUAGES` (default `en`). The `Expires:` field
  is computed at request time as `now + 1 year`, so there's no
  annual rotation chore.

`Settings.inboxes` (env) is the *bootstrap* source: each entry
guarantees an `inboxes` row exists in the DB on first start, but env
never overwrites existing rows on subsequent boots, admin edits to
`mirror_path` / `upstream_url` survive restarts.

## Getting a public-inbox mirror

Each list on lore.kernel.org is published as one git repo per epoch.
The easiest way to set things up is to let mimir do it for you:

```sh
poetry run mimir update                   # all configured inboxes
poetry run mimir update --inbox lkml      # one specific inbox
poetry run mimir update --skip-clone      # only fetch updates on existing epochs
poetry run mimir update --skip-fetch      # only discover/clone new epochs
poetry run mimir update --skip-ingest     # download but don't index
```

(All `mimir <cmd>` invocations are also reachable as
`flask --app mimir <cmd>`, both share the same Click commands.
Pick whichever reads better in your scripts.)

For each inbox `update` fetches the upstream `manifest.js.gz`, runs
`git clone --mirror -- <url>` on any epoch missing locally, runs
`git fetch --prune` on the ones already present, and then ingests
new commits, all in one shot.

Each epoch is roughly 1 GB and holds several hundred thousand
messages, so a fresh clone of all of lkml (currently 19 epochs, ~6M
messages) takes a while and needs ~20 GB of disk. linux-fsdevel is
about an order of magnitude smaller.

If you'd rather drive it manually:

```sh
mkdir -p Inboxes/lkml/git && cd Inboxes/lkml/git
git clone --mirror -- https://lore.kernel.org/lkml/git/0.git 0.git
git clone --mirror -- https://lore.kernel.org/lkml/git/1.git 1.git
# … and so on
```

## Ingesting

```sh
poetry run mimir ingest                   # walk every configured inbox (parallel by default)
poetry run mimir ingest --inbox lkml      # only one inbox
poetry run mimir ingest --limit 500       # cap for testing
poetry run mimir ingest --workers 1       # force sequential (debug)
poetry run mimir ingest -v                # progress every 100 msgs
poetry run mimir ingest -vv               # one log line per message
```

Parsing runs in a `ProcessPoolExecutor` (defaults to
`os.cpu_count()`), with the main process collecting results in
commit order and doing the SQL writes. The walker, dedup, batched
commits, and per-(inbox, epoch) `IngestState` checkpoints are
unaffected, parallelism is confined to the CPU-bound
`parse_message` stage.

To inspect a single message (smoke test for the git-backed read path):

```sh
poetry run mimir show '<message-id-without-angle-brackets>'
poetry run mimir show '...' --inbox lkml         # read the blob from this inbox's mirror
poetry run mimir show '...' --body-chars -1      # full body, no truncation
```

By default the ingest is quiet apart from the per-epoch summary
line. Parse failures are surfaced as warnings at any verbosity level.

To re-walk a single epoch, e.g. to backfill messages that failed
under an older parser version:

```sh
poetry run mimir reindex lkml 0.git                    # rewind state, re-walk; dedup skips existing
poetry run mimir reindex lkml 0.git --from-scratch     # also DELETE this inbox's links to that epoch first
```

Output is one line per epoch, e.g.:

```
lkml/0.git: new=500 linked=0 dup_batch=0 dup_db=0 failed=0 head=8f282234b668f51b884f3140adf1947d95e32ce7
```

Every commit lands in exactly one bucket: `new` (Article inserted),
`linked` (Article already existed in another inbox, added a new
`article_lists` row, i.e. a cross-post), `dup_batch` (same Message-ID
seen earlier in the current uncommitted batch), `dup_db` (Article
already in DB and already linked to this inbox, re-walks land
here), or `failed` (`parse_message` raised).

The default form is non-destructive: existing rows are left alone
and only previously-failed (or genuinely new) messages get inserted.
`--from-scratch` deletes the per-inbox `article_lists` rows
pointing at this epoch first; the `articles` themselves stay (a
cross-post may still be linked from another inbox).

### Ingest contract

| Situation                                    | What happens                                      |
| -------------------------------------------- | ------------------------------------------------- |
| Same Message-ID seen across epochs           | Counted in `dup_db` (DB-level check)              |
| Same Message-ID twice within one walk        | Counted in `dup_batch` (in-batch set)             |
| Cross-post: Message-ID seen in another inbox | Article reused; one new `article_lists` row added (counts as `linked`) |
| Existing article with the same Message-ID    | Left untouched, no updates, ever                 |
| `parse_message` raises                       | Counted in `failed`; row recorded in `parse_failures`; SHA still advances |

The "no updates, ever" stance assumes the underlying archive is
immutable (public-inbox commits are append-only). If you want to
retry a previously failed parse, for example after fixing a parser
bug, see "Replaying parse failures" below, or wipe / rewind
`ingest_state.last_commit_sha` for that (inbox, epoch).

### Replaying parse failures

Every commit whose `m` blob raises in `parse_message` is recorded in
`parse_failures` keyed by `(inbox, epoch, commit_sha)` with the
exception class, message, attempt count, and timestamps. A re-walk
that parses the same commit cleanly clears the row automatically.

To enumerate or replay without re-walking the whole epoch:

```sh
mimir admin failures list                                 # all
mimir admin failures list --inbox lkml --error-class ValueError
mimir admin failures replay lkml                          # re-parse all of lkml's failures
mimir admin failures replay lkml --epoch 0.git --limit 100
```

`replay` re-fetches each failure's blob from the mirror, re-runs the
parser, and either inserts the article (success → row deleted) or
bumps `attempts` + `last_attempt` (still failing → row kept). Skipped
rows mean the commit or `m` blob is no longer in the mirror.

## Managing inboxes

`Settings.inboxes` (env) seeds the `inboxes` table on first
startup, but you can also create / modify / delete inboxes
directly. The CLI is the front-end to a service layer in
`mimir.inboxes`, the future Flask admin UI will call the same
functions.

```sh
mimir admin inbox list
mimir admin inbox show <name>
mimir admin inbox add <name> [--mirror-path PATH] [--upstream-url URL]
mimir admin inbox update <name> [--mirror-path P] [--upstream-url U] [--rename NEW]
mimir admin inbox remove <name> [--keep-orphan-articles] [--remove-inbox-data] [--yes]
```

Validation is enforced at the service layer:

- `<name>` must match `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`. The
  name flows into URL paths and cache-key fragments, so it has to
  be lowercase alphanumeric with hyphens, no leading/trailing
  hyphens, ≤64 chars.
- `<upstream_url>` must be `https://` with a non-empty host.
- `<mirror_path>` must be a non-empty string. The directory is
  allowed to not exist yet, `mimir update --inbox
  <name>` will create it on first clone.

`add` only inserts the row. With just a name, it defaults to
`Inboxes/<name>/git` on disk and `https://lore.kernel.org/<name>`
upstream, matching the conventional lore.kernel.org public-inbox
layout. Pass `--mirror-path` and/or `--upstream-url` to override
either independently. To actually populate the inbox:

```sh
mimir admin inbox add linux-arm-kernel    # defaults to lore.kernel.org
mimir update --inbox linux-arm-kernel     # clone the mirror + ingest
```

`remove` cascade-deletes via FK `ON DELETE CASCADE`: the inbox's
`article_lists` and `ingest_state` rows go with it. By default it
also drops `articles` left without remaining links, cross-posts to
other inboxes survive untouched. `--keep-orphan-articles` opts out.

`--remove-inbox-data` additionally `rm -rf`'s the on-disk
public-inbox mirror at `<mirror_path>`. Permanent, re-cloning all
of lkml takes hours and ~20 GB. The command prompts for explicit
confirmation; use `--yes` to skip both the DB-removal prompt and
the on-disk-removal prompt in a script.

`update --rename` and `remove` invalidate the cache rows that
reference the affected name (`mimir.cache.delete_for_inbox`) so
subsequent reads don't return stale entries pointing at a now-
defunct inbox.

## Schema

```
inboxes
  id, name (UNIQUE),                  -- name is the URL slug
  mirror_path, upstream_url

articles
  id, message_id (UNIQUE),
  subject, author, date,              -- for listings; date indexed
  thread_parent,                      -- best-guess parent (in_reply_to OR refs[-1]); indexed
  subject_normalized                  -- prefixes stripped, lowercased; indexed for JWZ subject grouping

article_lists                         -- per-inbox presence; cross-posts get N rows
  article_id (FK → articles.id, ON DELETE CASCADE),
  inbox_id   (FK → inboxes.id,  ON DELETE CASCADE),
  epoch, commit_sha,                  -- pointer back to the public-inbox blob in *this* inbox's mirror
  PRIMARY KEY (article_id, inbox_id)

ingest_state
  inbox_id (FK → inboxes.id, ON DELETE CASCADE),
  epoch,
  last_commit_sha,
  PRIMARY KEY (inbox_id, epoch)

cache                                 -- DB-backed cache for slow dashboard queries
  key (PK), value (JSON), expires_at (indexed)

parse_failures                        -- one row per (inbox, epoch, commit_sha) whose blob couldn't be parsed
  inbox_id (FK → inboxes.id, ON DELETE CASCADE),
  epoch, commit_sha,
  error_class (indexed), error_message,
  first_seen, last_attempt, attempts,
  PRIMARY KEY (inbox_id, epoch, commit_sha)
```

`mimir.store.read_message(session, inbox, message_id)` is the
canonical read path: looks up `(epoch, commit_sha)` for the message
in the given inbox, opens the dulwich repo, fetches the blob, runs
`parse_message` to return a `ParsedArticle` with body, full headers,
and attachment bytes.

SQLite runs in WAL mode with `synchronous=NORMAL` and
`foreign_keys=ON`, set on every connection from
`mimir/extensions.py`.

Models are defined as SQLAlchemy 2.0 typed `Mapped[]` classes in
`mimir/models.py`. Migrations live under `alembic/versions/`.

## Project layout

```
mimir/
  __init__.py            Flask app factory; bootstraps inboxes from env
  cli/                   Click commands, one submodule per concern group:
                         initdb, ingest, mainline, backfill, show, cache,
                         maintenance, devseed, bootstrap, broker,
                         admin/{inbox,failures,canonicals}.
                         register_cli(app) wires them onto Flask's cli.
  config.py              pydantic-settings Settings class + PROJECT_ROOT
  extensions.py          SQLAlchemy engine + WAL pragmas, sessionmaker, Base
  inboxes.py             Inbox lifecycle: bootstrap from env, mutate via admin,
                         expose via nav-name cache; shared validators.
  ingest/                Ingest pipeline split by flow: epoch (hot per-epoch
                         walk + shared helpers), replay (parse-failures
                         re-walk), backfill (canonical-inbox resolution),
                         orchestrate (per-inbox + cross-inbox drivers).
  models.py              All ORM tables (Inbox, Article, ArticleList,
                         IngestState, Subsystem, MainlineCommit, etc.).
  parser.py              pydantic DTOs + BytesParser-based MIME extraction
  rendering/             body→HTML pipeline split by concern: blocks
                         (segmentation), diff (per-hunk anchors +
                         per-language Pygments overlay), linkify
                         (URL / Message-ID + DCO trailer redaction),
                         body (orchestrator + render_body entry).
  store.py               read_message(): SQL lookup + dulwich fetch + parse
  sync.py                public-inbox manifest discovery + git clone/fetch
  threading.py           recursive CTEs for thread reconstruction + active threads
  dashboard.py           landing-page aggregations (trackers, pulls, stats, sparkline)
  cache.py               DB-backed cache with JSON encode/decode + a type registry
  canonical.py           canonical-inbox resolution from To/Cc headers
  patches.py,            patch path / trailer / patch-series extractors;
  trailers.py,           shared backfill walker shell in _backfill.py.
  patch_series.py,
  _backfill.py
  subsystems.py          article-level path-matching primitives over MAINTAINERS
  subsystems_dashboard/  per-subsystem dashboard surfaces split by concern:
                         reads (per-subsystem fan-outs), reviewers
                         (attestation surfaces), activity (cross-inbox
                         "most active subsystems"), triage (needs-
                         attention + quiet-for-N+-days queues).
  maintainers.py         MAINTAINERS file parser (no DB)
  maintainer_allowlist.py  dynamic email allowlist sourced from MAINTAINERS
  mainline.py            mainline-tree end-to-end: MAINTAINERS reload +
                         Link-trailer commit walker + update_mainline
                         orchestrator
  maintenance.py         SQLite hygiene operations: run_analyze, run_vacuum
  patch_revisions.py     v1/v2/v3 series grouping logic for patch pages
  patch_state.py         lifecycle classifier (applied / under review / ...)
  datetime_utils.py      tz-aware UTC normalisation for Date headers
  broker/                write-broker daemon + JSONL protocol + client:
                         server (queue + worker pool), handlers
                         (cache / longops / maintenance / warm),
                         protocol (pydantic request types), client
                         (process-singleton, thread-safe RPC)
  indexnow.py            IndexNow push-notification client
  seo/                   SEO output split by format: sitemaps (XML),
                         json_ld (schema.org payloads), atom (Atom 1.0 feeds).
  web/                   Flask blueprint package: routes (one submodule per
                         URL family), filters (template filters), hooks
                         (request/response hooks + context processor),
                         urls (URL composition + site-base memo).
  templates/             Jinja2 (base, index, inbox, daily, since, year,
                         month, search, author, reviewer, subsystem,
                         message, attachment_preview, _recent_items)
alembic/                 migrations
tests/                   pytest
Inboxes/                 default mirror root (per-inbox subdirs; gitignored)
```

## Web UI

A read-only browser for the archive. Lightweight stack: Flask +
Jinja2, [Pico CSS](https://picocss.com/) and
[HTMX](https://htmx.org/) from CDN with SRI pins (no build step),
and [Pygments](https://pygments.org/) for server-side syntax
highlighting.

```sh
poetry run mimir run        # http://127.0.0.1:5000/
```

Routes:

- `GET /`, meta-index: list of configured inboxes with
  per-inbox row counts, epoch counts, and date spans.
- `GET /<inbox>/`, per-inbox dashboard: most active threads (last
  7 days, top 10 by decay-weighted score); side-by-side latest
  `[GIT PULL]` requests and `Linux N.N.N` release announcements;
  side-by-side per-author trackers driven by `Inbox.tracked_authors`
  (manage via `mimir admin inbox trackers …`; the
  section is hidden when the inbox has no trackers configured); a
  "this day, 5 years ago" sample; the last 10 messages in the
  inbox; a 30-day daily-volume sparkline + archive stats footer.
- `GET /<inbox>/today` and `GET /<inbox>/yesterday`, daily views
  showing every thread with at least one message on that calendar
  day (UTC), plus the day's total message count.
- `GET /<inbox>/<YYYY>/`, year archive: 12-month grid with per-month
  message counts; cells link to the month view, missing months
  dimmed. Prev/next year nav bounded by the plausible-archive range
  (1995..now+1).
- `GET /<inbox>/<YYYY>/<MM>/`, month archive: every thread with at
  least one message that month, ordered by last activity desc, capped
  at 100 with a count notice when truncated. Prev/next month nav
  with year wraparound; breadcrumb up to the year view.
- `GET /<inbox>/search?q=<query>`, substring search over `subject`
  and `author` (case-insensitive, OR-combined). 100-result cap,
  cached per (inbox, query). Form lives on the inbox dashboard.
  Caveats: queries with no matches can take seconds to scan on a
  cold cache; the date-index short-circuit only helps when *some*
  rows match. See `mimir.dashboard.search_articles`.
- `GET /m/<message-id>`, Message-ID lookup. 301-redirects to the
  article's canonical `/<inbox>/<YYYY>/<MM>/<article-id>` URL. For
  cross-posts, the destination is the article's pinned
  `canonical_inbox_id` (or the alphabetically-first link with
  firehose inboxes demoted, when canonical isn't pinned yet); the
  message page's "Also in:" line surfaces the rest. Useful for
  linking from outside the archive (commit trailers, IRC,
  lore.kernel.org refs). An inbox-scoped variant
  `GET /<inbox>/m/<message-id>` 404s when the message exists but
  isn't in the named inbox.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>`, single message:
  headers, full thread tree with the current message highlighted,
  body, attachment list. Patch articles also render a per-patch
  state card above the body summarising trailers (with maintainer
  attestation chips), mainline-landing record, cross-revision
  series timeline (with `[diff vs current]` links), and thread
  activity. When the thread root has an off-list parent, also
  shows a "Possibly related" surface of other archived messages
  with the same normalized subject (JWZ subject-based grouping).
  The year/month must match the article's archived date;
  mismatches return 404. ETag-based conditional revalidation:
  responses carry `Cache-Control: public, no-cache` and a strong
  ETag, repeat requests resolve as 304 with no body. The HTMX
  intra-thread swap (click a tree link) returns just the
  `_message_body.html` partial, leaving the tree + chrome on
  the client.
- `GET /<inbox>/series/<patch_series_key>/diff?from=<vN>&to=<vM>&pos=<pos>`  
  Inter-revision diff for a single patch-series position.
  `pos=cover` (or `pos=0`) diffs the cover-letter bodies between
  two revisions; `pos=N` diffs the N-th in-series patch's body
  across revisions. The body diff covers both commit message and
  patch hunks. 24h cached, source emails are immutable in the
  mirror. Linked from the state card's "Series revisions" row.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>/attachment/<n>`, binary
  download of the n-th attachment, served from the dulwich-fetched
  blob.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>/attachment/<n>/preview`  
  Pygments-highlighted inline preview for text-like attachments
  (patches, .c, .py, etc.); falls back to a "binary, can't preview"
  page otherwise.
- `GET /api/<inbox>/recent?offset=N`, HTMX partial: next page of
  "Recent messages" entries plus a fresh "Load more" trigger.
- `GET /healthz` and `GET /readyz`, cheap probes for orchestrators.
  `/healthz` does no DB work; `/readyz` runs a `SELECT 1`. Both
  bypass the route cache via `Cache-Control: no-store`.
- `GET /robots.txt`, disallows `/*/attachment/*` and points at the
  sitemap.
- `GET /sitemap.xml`, meta-index + per-inbox dashboards + recent
  100 message URLs per inbox. Cached for 5 min.
- `GET /security.txt` and `GET /.well-known/security.txt`  
  RFC 9116 contact info. 404 unless `SECURITY_CONTACT` is set.

The body rendering pipeline (`mimir/rendering/`) walks the body
line by line, segments it into runs of *text*, *quote*, and *diff*,
and emits HTML accordingly:

- Quoted blocks (`>`-prefixed lines) become `<blockquote>` and
  recurse for nested levels, `>>>>` ends up four `<blockquote>`
  deep. Levels at or beyond depth 2 collapse into `<details>` so
  the reader can expand on demand.
- Inline unified diffs (recognized by `diff --git`, `--- `, `+++ `,
  `@@` starts) get the standard green/red/cyan add/remove/header
  rendering, with two extras: each hunk wraps in
  `<div id="h-N" class="hunk">` and each line within carries
  `id="h-N-LM"` so URL fragments like `#h-2-L15` jump to a
  specific line of a patch; context, add, and remove lines also
  receive a per-language Pygments overlay (the lexer is detected
  from each `+++ b/<path>`), so a C patch's hunk content reads as
  C, a Python patch's reads as Python, and unknown / binary
  targets fall through to plain monospace.
- Plain text runs are escaped, preserve newlines via `<pre>`, and
  have URLs and `<Message-ID>`s linkified, clicking a referenced
  Message-ID inside one message takes you to that message's
  per-inbox URL when it's in the archive (and renders as a neutral
  `[ref]` placeholder so the address part isn't re-leaked); refs
  not in the archive render as `[off-list ref]`.

## Cache warming

The dashboard helpers run through a DB-backed cache (the `cache`
table; values JSON-encoded with a small dataclass registry in
`mimir/cache.py`). TTLs are sized to the cost of recomputation:

| Helper                    | TTL    |
| ------------------------- | ------ |
| `archive_stats`           | 24 h   |
| `daily_volume`            | 1 h    |
| `active_threads`          | 5 min  |
| `threads_for_day`         | 5 min  |
| `author_recent` (each)    | 5 min  |
| `latest_pull_requests`    | 5 min  |
| `latest_stable_releases`  | 5 min  |
| `this_day_in_history`     | 5 min  |

To eliminate user-facing cold-start latency, run:

```sh
poetry run mimir warm-cache
```

from cron or a systemd timer. Sample `crontab`:

```cron
* * * * * cd ~/Projects/python/mimir && poetry run mimir warm-cache >/dev/null
```

A warm-cache run refreshes every cached helper for every configured
inbox. With this in place, dashboard loads come back in
single-digit-millisecond range regardless of how big the archive
gets.

Per-key work fans out across `min(cpu_count, 8)` worker threads by
default; pass `--workers N` to override (e.g. `--workers 1` when
debugging a slow target). Keys whose remaining TTL exceeds the
cron-period buffer are skipped on each tick: 5-min-TTL keys still
refresh every run, 1-hour-TTL keys refresh on the tick nearest the
hour boundary, and 24h-TTL keys refresh once per day.

## Reclaiming space (VACUUM)

SQLite never reclaims freed pages on its own; the `.db` file grows
past its actual content over time, and the WAL grows during long
ingests until something checkpoints it. To compact both:

```sh
poetry run mimir vacuum
```

Reports before/after sizes for `mimir.db`, `mimir.db-wal`, and
`mimir.db-shm`. VACUUM holds an exclusive lock for the duration and
needs ~2× the on-disk size of free space (the rebuild lives in the
WAL until checkpoint). Other processes with the DB open (web
server, warm-cache cron) prevent the post-VACUUM WAL truncate, so
run it during a quiet window.

Sample `crontab` (daily at 04:00, only if no ingest is running):

```cron
0 4 * * * cd ~/Projects/python/mimir && poetry run mimir vacuum >/dev/null
```

On lkml-scale (~6 M articles, ~3.6 GB DB) a full VACUUM takes ~80 
120 s.

## Refreshing query-planner stats (ANALYZE)

SQLite's planner reads `sqlite_stat1` to pick query plans. The
migration runs `ANALYZE` once on an empty schema, which doesn't
help; as ingest fills the tables the stats stay zero and the
planner can flip to bad plans (e.g. scanning all of `article_lists`
instead of walking the date index). Run periodically, daily or
after big ingest deltas:

```sh
poetry run mimir analyze
```

Example crontab (4:30am, just after the daily vacuum):

```cron
30 4 * * * cd ~/Projects/python/mimir && poetry run mimir analyze
```

On lkml-scale ANALYZE takes ~5 15 s.

## Deployment

For real-host deployment (everything above runs as `flask run` for
dev), see `deploy/README.md`. Three shapes are covered:

- **Container**, `Dockerfile` and `compose.yaml` at the repo root.
  Multi-stage build, non-root runtime, gunicorn behind a `${WORKERS}`
  knob, auto-`alembic upgrade head` on start, `/healthz` container
  healthcheck, `/data` (with `/data/db/` and `/data/Inboxes/`
  subpaths) as the single bind mount. `docker compose up --build`
  once `SECRET_KEY` is set.
- **systemd**, `deploy/systemd/` carries the web-server unit plus
  three timer/oneshot pairs replacing the cron lines for warm-cache
  (every minute), analyze (daily), and vacuum (weekly). For the
  weekly vacuum, the WAL truncate only lands fully when no other
  process holds the DB; do `systemctl stop mimir.service` before
  triggering it manually if that matters, or let the timer fire and
  accept best-effort.
- **Reverse proxy**, `deploy/caddy/Caddyfile.example` (5 lines,
  automatic HTTPS) and `deploy/nginx/mimir.conf.example` (full
  TLS site block with the `X-Forwarded-Proto` and `X-Request-Id`
  headers mimir reads).

## Mainline tree (MAINTAINERS)

mimir mirrors Linus's `linux.git` locally so it can read
`MAINTAINERS` and surface subsystem ownership across the UI:
per-subsystem dashboards (`/<inbox>/subsystem/<name>/`), reviewer
pages, attestation chips on patch views, the most-active-subsystems
aggregation on `/`, and the MAINTAINERS-derived half of the email
allowlist that drives From-line / DCO-trailer redaction. Tree path
is configurable via `MAINLINE_TREE_PATH` (default
`Mainline/linux.git` alongside the per-inbox mirrors) and
`MAINLINE_TREE_URL`.

```sh
# Clone (first run) or fetch + load:
poetry run mimir update-mainline

# Re-parse the local HEAD without fetching:
poetry run mimir update-mainline --skip-fetch

# Force re-parse even when HEAD hasn't moved (after a parser fix):
poetry run mimir update-mainline --force
```

Steady-state ticks (HEAD unchanged) are cheap: fetch, compare,
no-op. Operator can run this on a cron / systemd timer; the
schema is replaced transactionally on every change so consumers
never see a half-loaded subsystems table.

`update-mainline` runs two passes against the tree:

1. **MAINTAINERS load**, replaces the `subsystems` schema as
   above. Skipped when HEAD is unchanged. `--skip-maintainers`
   disables this pass for the tick.
2. **`Link:`-trailer walk**, scans every new commit for
   `Link: https://lore.kernel.org/.../<msgid>` trailers and
   inserts `mainline_commits` rows. Resumable; the first run
   walks the full history (~1.5M commits on Linus's tree, a few
   minutes). The patch page's state card surfaces these as a
   "Landed: `<sha>` in `<tree>` on YYYY-MM-DD" row whenever an
   article's Message-ID matches one or more recorded commits.
   `--skip-commits` disables this pass.

### Backfilling `article_files`

New articles get their diff-touched paths extracted automatically
at ingest time (parsing `diff --git a/<old> b/<new>` headers out
of patch bodies). For articles ingested before that landed, run:

```sh
poetry run mimir backfill-article-files [-v]
```

Idempotent, articles that already have rows are skipped. Pass
`--limit N` to bound a session for huge archives; the walker is
newest-first, so a `--limit` run covers the most-visible articles
first. `--reprocess` re-extracts for articles whose rows already
exist (use after an extractor change).

### Backfilling patch-series detection

Cover-letter subjects (`[PATCH ... 0/N] <title>`) and in-series
patches (`[PATCH ... M/T] <title>`) are tagged with
`patch_series_key + patch_series_version + patch_series_position`
at ingest (in-series patches inherit key + version from the cover
via their thread parent). For articles ingested before that
landed:

```sh
poetry run mimir backfill-patch-series [-v]
```

Cheaper than the article-files backfill, only reads
subject + author + thread_parent, no body re-parse via git mirror.
Idempotent; `--limit N` and `--reprocess` work the same way.
Output buckets: `indexed` (covers), `in_series_indexed`
(in-series patches linked to a cover), `in_series_orphan`
(in-series patch with position set but cover not yet known,
re-attempted on the next run), `not_cover`, `skipped`.

## IndexNow (Bing / Yandex push notifications)

Off by default. Set `INDEXNOW_KEY` to enable: the `update`
scheduler tick will push the canonical URL of every newly-ingested
article to `https://api.indexnow.org/indexnow`, which fans out to
Bing, Yandex, Naver, Seznam, and Yep. Google does **not** consume
IndexNow, so this won't help Google discovery; it only accelerates
Bing-family crawlers.

```sh
# Generate a key once (32 hex chars; the spec is loose on length).
python -c "import secrets; print(secrets.token_hex(16))"

# Set in the deployment env. SITE_BASE_URL must also be set so the
# protocol can build the keyLocation URL and the host field.
export INDEXNOW_KEY=8c3aef...        # the generated key
export SITE_BASE_URL=https://example.test
```

Once enabled, mimir serves the key file at
`https://<host>/<key>.txt` (registered only when `INDEXNOW_KEY` is
set, an unconfigured deploy doesn't expose the endpoint). Pre-
existing articles in the DB are **not** backfilled to IndexNow;
only articles newly created on each `update` tick are pushed.

`INDEXNOW_MAX_PER_TICK` (default `1000`) is a backfill guard:
when a single `update` produces more new URLs than this (fresh
deploy, post-outage catch-up, etc.), the push is skipped entirely
and a warning logged, the sitemap remains the discovery path for
the backlog. Raise the cap if your steady-state new-articles-per-
tick legitimately exceeds it.

All notification calls are best-effort: network errors and non-2xx
responses log a warning and don't break the ingest tick.

## Linting and tests

```sh
poetry run ruff check mimir/
poetry run pytest
```

Tests focus on the cache encoder/decoder round-trip; that's where
silent corruption would be most expensive.

## License

MIT, see `LICENSE`.
