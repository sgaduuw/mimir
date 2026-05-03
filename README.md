# mimir

A toy archiver that ingests the [Linux Kernel Mailing List][lkml] (or any
[public-inbox][pi] v2 archive) from a local git mirror into SQLite. Built as a
small Flask app so it can grow a web UI later, but for now it's just a CLI
ingest pipeline.

[lkml]: https://lore.kernel.org/lkml/
[pi]: https://public-inbox.org/

## What it does

- Walks one or more public-inbox v2 epoch repositories (`0.git`, `1.git`, …),
  where each commit's tree contains a single `m` blob holding the raw RFC 5322
  bytes of one message.
- Parses each message with the stdlib email API under `policy.default` —
  proper handling of MIME multiparts, RFC 2231 filenames, RFC 2047 encoded
  headers, and the like.
- **Treats the public-inbox mirror as the source of truth.** SQLite is a
  thin index that records, per message, only what's needed to find and
  display: `message_id`, the (`epoch`, `commit_sha`) pointer back to the
  blob, and a few display fields (`subject`, `author`, `date`,
  `in_reply_to`, `references`). Body, full headers, and attachment bytes
  are *not* duplicated into SQLite — they're re-parsed from the git blob
  on demand. Per-row size is ~500 bytes.
- Re-runs are incremental: only new commits since the last recorded HEAD
  SHA are visited.

The read path costs roughly 2 ms per message (SQL lookup + dulwich blob
fetch + parse). The mirror must be present on disk at *read* time, not
just at ingest time.

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
FLASK_DEBUG=true
SECRET_KEY=<run: python -c "import secrets; print(secrets.token_hex(32))">
DATABASE_URL=sqlite:///mimir.db
LKML_MIRROR_PATH=lkml/git
```

All settings live in `mimir/config.py` as a pydantic-settings `Settings`
class — anything declared there can be overridden via env var or `.env`.

## Getting a public-inbox mirror

Each lore.kernel.org list is published as one git repo per epoch. The
easiest way is to let mimir do it for you:

```sh
poetry run flask --app mimir update
```

This fetches the upstream `manifest.js.gz`, `git clone --mirror`s any
epochs you don't have locally, runs `git fetch --prune` on the ones you
do, and then ingests new commits — all in one shot. Configurable via
`UPSTREAM_URL` (default: `https://lore.kernel.org/lkml`) and
`LKML_MIRROR_PATH` (default: `lkml/git`).

Each epoch is roughly 1 GB and holds several hundred thousand messages,
so a fresh clone of the full lkml archive (currently 19 epochs, ~5M
messages) takes a while and needs ~20 GB of disk for the mirrors. Use
`--skip-clone` if you only want to fetch updates on what's already there,
`--skip-fetch` to skip the per-epoch `git fetch`, or `--skip-ingest`
to download repos without indexing.

If you'd rather drive it manually:

```sh
mkdir -p lkml/git && cd lkml/git
git clone --mirror https://lore.kernel.org/lkml/git/0.git 0.git
git clone --mirror https://lore.kernel.org/lkml/git/1.git 1.git
# … and so on
```

## Ingesting

```sh
poetry run flask --app mimir ingest                   # walk every epoch (parallel by default)
poetry run flask --app mimir ingest --limit 500       # cap for testing
poetry run flask --app mimir ingest --mirror /tmp/foo  # alternate mirror dir
poetry run flask --app mimir ingest --workers 1       # force sequential (debug)
poetry run flask --app mimir ingest -v                # progress every 100 msgs
poetry run flask --app mimir ingest -vv               # one log line per message
```

Parsing runs in a `ProcessPoolExecutor` (defaults to `os.cpu_count()`),
with the main process collecting results in commit order and doing the
SQL writes. The walker, dedup, batched commits, and `IngestState`
checkpoints are unchanged — parallelism is confined to the
CPU-bound `parse_message` stage.

To inspect a single message (smoke test for the git-backed read path):

```sh
poetry run flask --app mimir show '<message-id-without-angle-brackets>'
poetry run flask --app mimir show '...' --body-chars -1   # full body, no truncation
```

By default the ingest is quiet apart from the per-epoch summary line. Parse
failures are surfaced as warnings at any verbosity level.

To re-walk a single epoch — e.g. to backfill messages that failed under
an older parser version:

```sh
poetry run flask --app mimir reindex 0.git           # rewind state, re-walk; dedup skips existing
poetry run flask --app mimir reindex 0.git --from-scratch  # also DELETE existing rows for this epoch first
```

The default form is non-destructive: existing rows are left alone and only
previously-failed (or genuinely new) messages get inserted. Use
`--from-scratch` for a true rebuild.

Output is one line per epoch, e.g.:

```
0.git: new=500 skipped=0 failed=0 head=8f282234b668f51b884f3140adf1947d95e32ce7
```

- `--limit` is a *total* cap across epochs and a deliberate testing knob.
  It records progress, so `--limit 500` followed by `--limit 1000` ingests
  500 then *another* 1000 (1500 total). Wipe `mimir.db` to start over.
- Re-running with no arguments after a successful run is a no-op:
  the per-epoch HEAD SHA is recorded in the `ingest_state` table and the git
  walker prunes everything up to and including it.

### Behaviour on re-runs

| Situation                                | What happens                          |
| ---------------------------------------- | ------------------------------------- |
| Same Message-ID seen across epochs       | Second copy skipped (DB-level check)  |
| Same Message-ID twice within one walk    | Second copy skipped (in-batch set)    |
| Existing article with the same Message-ID| Left untouched — no updates, ever     |
| `parse_message` raises                   | Counted in `failed`; SHA still advances|

The "no updates, ever" stance assumes the underlying archive is immutable
(public-inbox commits are append-only). If you want to retry a previously
failed parse — for example after fixing a parser bug — wipe or rewind
`ingest_state.last_commit_sha` for that epoch.

## Schema

```
articles
  id, message_id (UNIQUE),
  epoch, commit_sha,             -- pointer back to the public-inbox blob
  subject, author, date,         -- for listings; date indexed
  in_reply_to,                   -- indexed, for threading
  references (JSON list[str])

attachments
  id, article_id (FK → articles.id, ON DELETE CASCADE),
  filename, content_type, size_bytes
  -- contents are NOT stored; re-derived from the git blob on demand

ingest_state
  epoch (PK), last_commit_sha, last_ingested_at
```

`mimir.store.read_message(session, message_id)` is the canonical read
path: looks up `(epoch, commit_sha)` in SQL, opens the dulwich repo,
fetches the blob, runs `parse_message` to return a `ParsedArticle`
with body, full headers, and attachment bytes.

SQLite runs in WAL mode with `synchronous=NORMAL` and `foreign_keys=ON`,
set on every connection from `mimir/extensions.py`.

Models are defined as SQLAlchemy 2.0 typed `Mapped[]` classes in
`mimir/models.py`. Migrations live under `alembic/versions/`.

## Project layout

```
mimir/
  __init__.py    Flask app factory; wires up CLI commands
  cli.py         Click commands: init-db, ingest, show
  config.py      pydantic-settings Settings class
  extensions.py  SQLAlchemy engine + WAL pragmas, sessionmaker, Base
  ingest.py      dulwich walker + per-epoch upsert loop, batched commits
  models.py      Article (lean index), Attachment (metadata only), IngestState
  parser.py      pydantic DTOs + BytesParser-based MIME extraction
  store.py       read_message(): SQL lookup + dulwich fetch + parse
alembic/         migrations
lkml/git/        default mirror location (must be present at read time too)
```

## Web UI

A read-only browser for the archive. Lightweight stack: Flask + Jinja2,
[Pico CSS](https://picocss.com/) and [HTMX](https://htmx.org/) from CDN
(no build step), and [Pygments](https://pygments.org/) for server-side
syntax highlighting.

```sh
poetry run flask --app mimir run        # http://127.0.0.1:5000/
```

Routes shipped so far:

- `GET /` — landing page with: most active threads (last 7 days, top 10
  by reply count); side-by-side latest `[GIT PULL]` requests and
  `Linux N.N.N` release announcements; side-by-side per-author trackers
  driven by `Settings.tracked_authors` (defaults: Linus Torvalds, Greg
  KH); a "this day, 5 years ago" sample; the last 10 messages overall;
  a 30-day daily-volume sparkline + archive stats footer.
- `GET /<list>/<YYYY>/<MM>/<article-id>` — single message: headers,
  full thread tree with the current message highlighted, body,
  attachment list. When the thread root has an off-list parent, also
  shows a "Possibly related" surface of other archived messages with the
  same normalized Subject (JWZ subject-based grouping). The list segment
  is `LIST_NAME` from settings (default `lkml`); the year/month must
  match the article's archived date. Mismatches return 404.
- `GET /<list>/<YYYY>/<MM>/<article-id>/attachment/<n>` — binary download
  of the n-th attachment, served from the dulwich-fetched blob.
- `GET /<list>/<YYYY>/<MM>/<article-id>/attachment/<n>/preview` — Pygments-
  highlighted inline preview for text-like attachments (patches, .c,
  .py, etc.); falls back to a "binary, can't preview" page otherwise.
- `GET /api/recent?offset=N` — HTMX partial: next page of "Recent
  messages" entries plus a fresh "Load more" trigger.
- `GET /<list>/today` and `GET /<list>/yesterday` — daily views
  showing the most active threads scoped to that calendar day (UTC),
  plus the total message count for the day.

The body rendering pipeline (`mimir/rendering.py`) walks the body line
by line, segments it into runs of *text*, *quote*, and *diff*, and
emits HTML accordingly:

- Quoted blocks (`>`-prefixed lines) become `<blockquote>` and recurse
  for nested levels — `>>>>` ends up four `<blockquote>` deep.
- Inline unified diffs (recognized by `diff --git`, `--- `, `+++ `, `@@`
  starts) are run through Pygments' `DiffLexer` with inline styles
  (`noclasses=True`), giving the standard green/red/cyan rendering.
- Plain text runs are escaped, preserve newlines via `<pre>`, and have
  URLs and `<Message-ID>`s linkified — clicking a referenced
  Message-ID inside one message takes you to that message's
  `/msg/...` view, even across epochs.

## Roadmap

Tracked here so we don't forget across sessions.

- **Multiple inbox support** — URL is already namespaced
  (`/<list>/...`) and `Settings.list_name` is in place. Real multi-list
  needs an `Article.list` column, an alembic migration, the route's
  hardcoded `list_name` check replaced by a SQL filter, and a
  `Settings.inboxes` map.

## Cache warming

Several landing-page queries are slow on the first run after a cache
expires (the archive `COUNT(*)` is ~6 s on 6.2M rows; the active-threads
recursive CTE is ~700 ms). Results are cached to a single pickle file at
`Settings.cache_path` (default `mimir-cache.pickle`) so they're shared
across processes. To eliminate user-facing cold-start latency, run:

```sh
poetry run flask --app mimir warm-cache
```

from cron or a systemd timer. Sample `crontab`:

```cron
*/5 * * * * cd ~/Projects/python/mimir && poetry run flask --app mimir warm-cache >/dev/null
```

After a warm `warm-cache` run, page loads of `/` drop from ~7.5 s to
~50 ms.

## Linting

```sh
poetry run ruff check mimir/
```

## License

MIT — see `LICENSE`.
