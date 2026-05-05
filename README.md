# mimir

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
- **Single-user ingest at a time.** `flask --app mimir update` /
  `ingest` are not safe to run concurrently against the same DB.
  Multiple readers (web server + warm-cache cron) are fine — WAL
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
  `policy.default` — proper handling of MIME multiparts, RFC 2231
  filenames, RFC 2047 encoded headers, and the like.
- **Treats the public-inbox mirror as the source of truth.** SQLite
  is a lean index that records, per message, only what's needed to
  find and display it: `message_id` + threading hints + a few
  display fields (`subject`, `author`, `date`). Body, full headers,
  and attachment bytes are *not* duplicated into SQLite — they're
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

- `DATABASE_URL` — `sqlite:///<project_root>/mimir.db`. Override
  per-deployment, e.g. `DATABASE_URL=sqlite:////data/mimir.db` for a
  container with a persistent volume.
- `SITE_NAME` — `mimir`. The displayed brand in titles, the nav, and
  the `/` heading; set this to whatever you want the public site to
  be called.
- `INBOXES` — a JSON map of `{name: {mirror_path, upstream_url}}`.
  Defaults cover lkml and linux-fsdevel under `Inboxes/<name>/git`.
  See `mimir/config.py` for the exact shape; override via env, e.g.
  `INBOXES='{"lkml": {"mirror_path": "...", "upstream_url": "..."}}'`.
- `EMAIL_ALLOWLIST` — substrings whose email addresses display in
  full; everyone else gets `<hidden>`.
- `TRACKED_AUTHORS` — `{label: from_substring}` pairs; each gets a
  tile on the per-inbox dashboard.

`Settings.inboxes` (env) is the *bootstrap* source: each entry
guarantees an `inboxes` row exists in the DB on first start, but env
never overwrites existing rows on subsequent boots — admin edits to
`mirror_path` / `upstream_url` survive restarts.

## Getting a public-inbox mirror

Each list on lore.kernel.org is published as one git repo per epoch.
The easiest way to set things up is to let mimir do it for you:

```sh
poetry run flask --app mimir update                   # all configured inboxes
poetry run flask --app mimir update --inbox lkml      # one specific inbox
poetry run flask --app mimir update --skip-clone      # only fetch updates on existing epochs
poetry run flask --app mimir update --skip-fetch      # only discover/clone new epochs
poetry run flask --app mimir update --skip-ingest     # download but don't index
```

For each inbox `update` fetches the upstream `manifest.js.gz`, runs
`git clone --mirror -- <url>` on any epoch missing locally, runs
`git fetch --prune` on the ones already present, and then ingests
new commits — all in one shot.

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
poetry run flask --app mimir ingest                   # walk every configured inbox (parallel by default)
poetry run flask --app mimir ingest --inbox lkml      # only one inbox
poetry run flask --app mimir ingest --limit 500       # cap for testing
poetry run flask --app mimir ingest --workers 1       # force sequential (debug)
poetry run flask --app mimir ingest -v                # progress every 100 msgs
poetry run flask --app mimir ingest -vv               # one log line per message
```

Parsing runs in a `ProcessPoolExecutor` (defaults to
`os.cpu_count()`), with the main process collecting results in
commit order and doing the SQL writes. The walker, dedup, batched
commits, and per-(inbox, epoch) `IngestState` checkpoints are
unaffected — parallelism is confined to the CPU-bound
`parse_message` stage.

To inspect a single message (smoke test for the git-backed read path):

```sh
poetry run flask --app mimir show '<message-id-without-angle-brackets>'
poetry run flask --app mimir show '...' --inbox lkml         # read the blob from this inbox's mirror
poetry run flask --app mimir show '...' --body-chars -1      # full body, no truncation
```

By default the ingest is quiet apart from the per-epoch summary
line. Parse failures are surfaced as warnings at any verbosity level.

To re-walk a single epoch — e.g. to backfill messages that failed
under an older parser version:

```sh
poetry run flask --app mimir reindex lkml 0.git                    # rewind state, re-walk; dedup skips existing
poetry run flask --app mimir reindex lkml 0.git --from-scratch     # also DELETE this inbox's links to that epoch first
```

Output is one line per epoch, e.g.:

```
lkml/0.git: new=500 linked=0 dup_batch=0 dup_db=0 failed=0 head=8f282234b668f51b884f3140adf1947d95e32ce7
```

Every commit lands in exactly one bucket: `new` (Article inserted),
`linked` (Article already existed in another inbox — added a new
`article_lists` row, i.e. a cross-post), `dup_batch` (same Message-ID
seen earlier in the current uncommitted batch), `dup_db` (Article
already in DB and already linked to this inbox — re-walks land
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
| Existing article with the same Message-ID    | Left untouched — no updates, ever                 |
| `parse_message` raises                       | Counted in `failed`; SHA still advances           |

The "no updates, ever" stance assumes the underlying archive is
immutable (public-inbox commits are append-only). If you want to
retry a previously failed parse — for example after fixing a parser
bug — wipe or rewind `ingest_state.last_commit_sha` for that
(inbox, epoch).

## Managing inboxes

`Settings.inboxes` (env) seeds the `inboxes` table on first
startup, but you can also create / modify / delete inboxes
directly. The CLI is the front-end to a service layer in
`mimir.inboxes` — the future Flask admin UI will call the same
functions.

```sh
flask --app mimir admin inbox list
flask --app mimir admin inbox show <name>
flask --app mimir admin inbox add <name> --mirror-path PATH --upstream-url URL
flask --app mimir admin inbox update <name> [--mirror-path P] [--upstream-url U] [--rename NEW]
flask --app mimir admin inbox remove <name> [--keep-orphan-articles] [--remove-inbox-data] [--yes]
```

Validation is enforced at the service layer:

- `<name>` must match `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`. The
  name flows into URL paths and cache-key fragments, so it has to
  be lowercase alphanumeric with hyphens, no leading/trailing
  hyphens, ≤64 chars.
- `<upstream_url>` must be `https://` with a non-empty host.
- `<mirror_path>` must be a non-empty string. The directory is
  allowed to not exist yet — `flask --app mimir update --inbox
  <name>` will create it on first clone.

`add` only inserts the row. To actually populate the inbox:

```sh
flask --app mimir admin inbox add my-list \
    --mirror-path Inboxes/my-list/git \
    --upstream-url https://lore.kernel.org/my-list
flask --app mimir update --inbox my-list   # clone the mirror + ingest
```

`remove` cascade-deletes via FK `ON DELETE CASCADE`: the inbox's
`article_lists` and `ingest_state` rows go with it. By default it
also drops `articles` left without remaining links — cross-posts to
other inboxes survive untouched. `--keep-orphan-articles` opts out.

`--remove-inbox-data` additionally `rm -rf`'s the on-disk
public-inbox mirror at `<mirror_path>`. Permanent — re-cloning all
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
  __init__.py    Flask app factory; bootstraps inboxes from env
  cli.py         Click commands: init-db, ingest, update, reindex, show, warm-cache
  config.py      pydantic-settings Settings class + PROJECT_ROOT
  extensions.py  SQLAlchemy engine + WAL pragmas, sessionmaker, Base
  inboxes.py     bootstrap_inboxes() + nav-name cache
  ingest.py      dulwich walker + per-epoch upsert loop, cross-post linking
  models.py      Inbox, Article, ArticleList, IngestState, CacheEntry
  parser.py      pydantic DTOs + BytesParser-based MIME extraction
  rendering.py   body→HTML pipeline (text/quote/diff blocks)
  store.py       read_message(): SQL lookup + dulwich fetch + parse
  sync.py        public-inbox manifest discovery + git clone/fetch
  threading.py   recursive CTEs for thread reconstruction + active threads
  dashboard.py   landing-page aggregations (trackers, pulls, stats, sparkline)
  cache.py       DB-backed cache with JSON encode/decode + a type registry
  web.py         Flask blueprint, view functions, template filters
  templates/     Jinja2 (base, index, inbox, daily, message, attachment_preview, _recent_items)
alembic/         migrations
tests/           pytest
Inboxes/         default mirror root (per-inbox subdirs; gitignored)
```

## Web UI

A read-only browser for the archive. Lightweight stack: Flask +
Jinja2, [Pico CSS](https://picocss.com/) and
[HTMX](https://htmx.org/) from CDN with SRI pins (no build step),
and [Pygments](https://pygments.org/) for server-side syntax
highlighting.

```sh
poetry run flask --app mimir run        # http://127.0.0.1:5000/
```

Routes:

- `GET /` — meta-index: list of configured inboxes with
  per-inbox row counts, epoch counts, and date spans.
- `GET /<inbox>/` — per-inbox dashboard: most active threads (last
  7 days, top 10 by decay-weighted score); side-by-side latest
  `[GIT PULL]` requests and `Linux N.N.N` release announcements;
  side-by-side per-author trackers driven by
  `Settings.tracked_authors` (defaults: Linus Torvalds, Greg KH); a
  "this day, 5 years ago" sample; the last 10 messages in the
  inbox; a 30-day daily-volume sparkline + archive stats footer.
- `GET /<inbox>/today` and `GET /<inbox>/yesterday` — daily views
  showing every thread with at least one message on that calendar
  day (UTC), plus the day's total message count.
- `GET /<inbox>/<YYYY>/` — year archive: 12-month grid with per-month
  message counts; cells link to the month view, missing months
  dimmed. Prev/next year nav bounded by the plausible-archive range
  (1995..now+1).
- `GET /<inbox>/<YYYY>/<MM>/` — month archive: every thread with at
  least one message that month, ordered by last activity desc, capped
  at 100 with a count notice when truncated. Prev/next month nav
  with year wraparound; breadcrumb up to the year view.
- `GET /<inbox>/search?q=<query>` — substring search over `subject`
  and `author` (case-insensitive, OR-combined). 100-result cap,
  cached per (inbox, query). Form lives on the inbox dashboard.
  Caveats: queries with no matches can take seconds to scan on a
  cold cache; the date-index short-circuit only helps when *some*
  rows match. See `mimir.dashboard.search_articles`.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>` — single message:
  headers, full thread tree with the current message highlighted,
  body, attachment list. When the thread root has an off-list
  parent, also shows a "Possibly related" surface of other archived
  messages with the same normalized subject (JWZ subject-based
  grouping). The year/month must match the article's archived date;
  mismatches return 404.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>/attachment/<n>` — binary
  download of the n-th attachment, served from the dulwich-fetched
  blob.
- `GET /<inbox>/<YYYY>/<MM>/<article-id>/attachment/<n>/preview` —
  Pygments-highlighted inline preview for text-like attachments
  (patches, .c, .py, etc.); falls back to a "binary, can't preview"
  page otherwise.
- `GET /api/<inbox>/recent?offset=N` — HTMX partial: next page of
  "Recent messages" entries plus a fresh "Load more" trigger.

The body rendering pipeline (`mimir/rendering.py`) walks the body
line by line, segments it into runs of *text*, *quote*, and *diff*,
and emits HTML accordingly:

- Quoted blocks (`>`-prefixed lines) become `<blockquote>` and
  recurse for nested levels — `>>>>` ends up four `<blockquote>`
  deep. Levels at or beyond depth 2 collapse into `<details>` so
  the reader can expand on demand.
- Inline unified diffs (recognized by `diff --git`, `--- `, `+++ `,
  `@@` starts) are run through Pygments' `DiffLexer` with inline
  styles, giving the standard green/red/cyan rendering.
- Plain text runs are escaped, preserve newlines via `<pre>`, and
  have URLs and `<Message-ID>`s linkified — clicking a referenced
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
poetry run flask --app mimir warm-cache
```

from cron or a systemd timer. Sample `crontab`:

```cron
* * * * * cd ~/Projects/python/mimir && poetry run flask --app mimir warm-cache >/dev/null
```

A warm-cache run refreshes every cached helper for every configured
inbox. With this in place, dashboard loads come back in
single-digit-millisecond range regardless of how big the archive
gets.

## Reclaiming space (VACUUM)

SQLite never reclaims freed pages on its own; the `.db` file grows
past its actual content over time, and the WAL grows during long
ingests until something checkpoints it. To compact both:

```sh
poetry run flask --app mimir vacuum
```

Reports before/after sizes for `mimir.db`, `mimir.db-wal`, and
`mimir.db-shm`. VACUUM holds an exclusive lock for the duration and
needs ~2× the on-disk size of free space (the rebuild lives in the
WAL until checkpoint). Other processes with the DB open (web
server, warm-cache cron) prevent the post-VACUUM WAL truncate, so
run it during a quiet window.

Sample `crontab` (daily at 04:00, only if no ingest is running):

```cron
0 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir vacuum >/dev/null
```

On lkml-scale (~6 M articles, ~3.6 GB DB) a full VACUUM takes ~80–
120 s.

## Refreshing query-planner stats (ANALYZE)

SQLite's planner reads `sqlite_stat1` to pick query plans. The
migration runs `ANALYZE` once on an empty schema, which doesn't
help; as ingest fills the tables the stats stay zero and the
planner can flip to bad plans (e.g. scanning all of `article_lists`
instead of walking the date index). Run periodically — daily or
after big ingest deltas:

```sh
poetry run flask --app mimir analyze
```

Example crontab (4:30am, just after the daily vacuum):

```cron
30 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir analyze
```

On lkml-scale ANALYZE takes ~5–15 s.

## Linting and tests

```sh
poetry run ruff check mimir/
poetry run pytest
```

Tests focus on the cache encoder/decoder round-trip — that's where
silent corruption would be most expensive.

## License

MIT — see `LICENSE`.
