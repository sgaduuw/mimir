# mimir

A toy archiver and read-only web UI for [public-inbox][pi] v2 mailing
list archives. Out of the box it indexes the [Linux Kernel Mailing
List][lkml] and [linux-fsdevel][fsd], but any list published by
public-inbox works. The displayed site name is configurable; "mimir"
appears only as the page generator.

[lkml]: https://lore.kernel.org/lkml/
[fsd]: https://lore.kernel.org/linux-fsdevel/
[pi]: https://public-inbox.org/

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
lkml/0.git: new=500 skipped=0 failed=0 head=8f282234b668f51b884f3140adf1947d95e32ce7
```

The default form is non-destructive: existing rows are left alone
and only previously-failed (or genuinely new) messages get inserted.
`--from-scratch` deletes the per-inbox `article_lists` rows
pointing at this epoch first; the `articles` themselves stay (a
cross-post may still be linked from another inbox).

### Behaviour on re-runs

| Situation                                    | What happens                                      |
| -------------------------------------------- | ------------------------------------------------- |
| Same Message-ID seen across epochs           | Second copy skipped (DB-level check)              |
| Same Message-ID twice within one walk        | Second copy skipped (in-batch set)                |
| Cross-post: Message-ID seen in another inbox | Article reused; one new `article_lists` row added |
| Existing article with the same Message-ID    | Left untouched — no updates, ever                 |
| `parse_message` raises                       | Counted in `failed`; SHA still advances           |

The "no updates, ever" stance assumes the underlying archive is
immutable (public-inbox commits are append-only). If you want to
retry a previously failed parse — for example after fixing a parser
bug — wipe or rewind `ingest_state.last_commit_sha` for that
(inbox, epoch).

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

## Linting and tests

```sh
poetry run ruff check mimir/
poetry run pytest
```

Tests focus on the cache encoder/decoder round-trip — that's where
silent corruption would be most expensive.

## License

MIT — see `LICENSE`.
