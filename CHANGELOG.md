# Changelog

All notable user-facing changes to mimir.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe behaviour, schema, config, and CLI/route shape changes —
not internal refactors. Categories: **Added**, **Changed**, **Deprecated**,
**Removed**, **Fixed**, **Security**.

## [Unreleased]

### Fixed

- Patch view: a full `git format-patch` payload now renders as one
  highlighted block. The `index ...` metadata line, follow-up
  `diff --git` blocks (multi-file patches), and the trailing
  `-- \n<version>` signature were being chopped into separate
  `<pre>` blocks, which broke copy-paste-to-patch and looked
  visually fragmented.
- Patch view: diff colours use Pygments class-based output instead
  of inline `style=` attributes, so add/remove tones adapt to
  Pico's light/dark theme via `light-dark()`.
- Patch view: DCO trailers (`Signed-off-by:`, `Reviewed-by:`,
  `Tested-by:`, `Acked-by:`, `Co-developed-by:`, `Reported-by:`,
  `Suggested-by:`, `Cc:`, `To:`, `From:`) no longer route through
  the message-ID linkifier, which had been smearing redacted email
  addresses on these lines as `[off-list ref]` — confusingly close
  to broken metadata for DCO chain verification. Allowlisted
  senders surface verbatim; everyone else is replaced with the
  explicit `<redacted>` placeholder.

## [1.8.1] – 2026-05-09

### Changed

- Dependency refresh. Notable: dulwich `0.21` → `1.x` major bump
  (our API surface is `Repo`, indexing, `commit.tree`,
  `commit.commit_time`, `repo.head`, `repo.get_walker`, all
  unchanged across the boundary); ruff `0.1` → `0.15`. Within-
  constraint patch/minor updates: markupsafe `2`→`3`, click,
  jinja2, pydantic{,-core,-settings}, urllib3, python-dotenv.

## [1.8.0] – 2026-05-08

### Changed

- `update` and `warm-cache` are now terse by default. `update`
  suppresses per-inbox / per-epoch lines on no-op ticks (only
  prints when something was cloned, fetched, ingested, or
  failed); `warm-cache` collapses per-key timings into a single
  `warm-cache: N inboxes, K keys, T ms total` summary line. Pass
  `-v` to either to recover the previous detail.
- `deploy/scheduler.sh` now reads `SCHEDULER_VERBOSE` from env
  (default empty) and splats it into both invocations, so the
  sidecar log is quiet by default and verbose on demand. See the
  commented `SCHEDULER_VERBOSE: "-v"` example in `compose.yaml`,
  or run `podman exec mimir-tasks flask --app mimir warm-cache
  -v` for a one-shot without restarting.

## [1.7.2] – 2026-05-08

### Fixed

- `cve@kernel.org`, `gregkh@kernel.org`, and other bare-`kernel.org`
  personal addresses no longer match the "list-shaped address"
  filter. Bare `kernel.org` was in `LIST_HOST_SUFFIXES`, which
  surfaced personal addresses as off-list-parent hints and skewed
  per-inbox address-observation tallies. List traffic lives on the
  subdomains (`vger.kernel.org`, `lists.linux.dev`); those entries
  are unchanged.

## [1.7.1] – 2026-05-08

### Fixed

- Off-list-parent hint tooltip now renders below the trigger
  (`data-placement="bottom"`) so it escapes the `.thread-box`
  overflow clip. Default top placement was clipped by the box's
  top edge on the first-row trigger, making the hint unreadable.

## [1.7.0] – 2026-05-08

### Added

- Off-list-parent rows in the thread tree now expose a hover
  tooltip listing list-shaped To/Cc addresses on the message that
  don't match any configured inbox. Quick cue for which mailing
  list the operator might want to add to recover the missing
  parent; the line stays compact, the address only appears on
  hover/focus.

## [1.6.1] – 2026-05-08

### Added

- `SQLITE_BUSY_TIMEOUT_MS` env var (default 5000) sets SQLite's
  per-connection `busy_timeout` so writers wait through transient
  contention instead of failing instantly.

### Fixed

- Web tier no longer 500s on "database is locked" when a cache
  upsert collides with a scheduler write. Cache writes are now
  best-effort: the contention is logged at warning and the request
  returns successfully; the next request recomputes.
- Alembic no longer silently disables `mimir.*` loggers when it
  runs in-process. `alembic/env.py` now passes
  `disable_existing_loggers=False` to `fileConfig`, so warnings
  emitted by code paths that ran before alembic (typically: any
  caller that imported `mimir` first) actually surface.

## [1.6.0] – 2026-05-07

### Added

- Per-inbox author trackers. The tracker tiles on `/<inbox>/` are
  now driven by `Inbox.tracked_authors` (a JSON column) instead of
  a global env var. Manage via
  `flask --app mimir admin inbox trackers {show,set,add,remove,clear}`.
  An inbox with no trackers configured renders no tracker section
  at all. `admin inbox list` gained a `trackers=N` / `trackers=none`
  marker per row.

### Removed

- `TRACKED_AUTHORS` env var (and the `Settings.tracked_authors`
  defaults `Linus Torvalds` / `Greg KH`). Tracker config now lives
  in the database, edited via the new admin CLI. **Post-deploy
  step:** existing operators must re-add desired trackers via
  `admin inbox trackers set <inbox> Linus=torvalds@ Greg=gregkh@`
  (or whatever substrings they previously had); the env var is
  silently ignored after this release.

## [1.5.1] – 2026-05-07

### Changed

- Thread list on the message page is now scroll-contained
  (max-height clamped to the smaller of 50vh and 24rem, with a
  border + padding so it reads as a widget). Long threads no
  longer push the message body off-screen; the `»` marker still
  points to the active message inside the scrollable box. Threads
  with more than 12 messages get a pure-CSS expand/collapse
  toggle in the heading to drop the height cap when needed.

## [1.5.0] – 2026-05-07

### Added

- Per-page `<meta name="description">` summarising the page —
  inbox dashboards report message count + date range, message
  pages synthesise "Message from <author> on <date> in <inbox>:
  <subject>", search results carry the query, archive/author
  views describe their scope. Replaces Google's auto-generated
  snippets in SERPs and feeds social-preview cards.
- Open Graph + Twitter Card tags on every page: `og:title`,
  `og:description`, `og:url`, `og:site_name`, `og:type` (article
  on message pages, website everywhere else), and the matching
  `twitter:card`/`title`/`description`. URL previews now render
  with title + description when ratatoskr.run links are pasted in
  Slack, Discord, Mastodon, etc. Will gain `og:image` once a
  favicon (#50) lands.
- schema.org JSON-LD on the meta-index (`WebSite`) and message
  pages (`@graph` of `DiscussionForumPosting` + `BreadcrumbList`,
  rendered against the canonical inbox so cross-posts collapse
  correctly). Makes message pages eligible for Google's
  "Discussions and forums" rich-results section and gives search
  engines a clean Site → Inbox → Subject breadcrumb. Author goes
  through the same redaction filter as the rendered page;
  `datePublished` prefers the message's RFC 5322 Date header over
  the public-inbox commit time.

### Changed

- Message-ID lookup redirects (`/m/<id>` and `/<inbox>/m/<id>`) are
  now `301 Moved Permanently` instead of `302 Found`, and the
  unscoped `/m/<id>` redirects directly to the *canonical* inbox's
  URL (using `articles.canonical_inbox_id`, falling back to
  alphabetically-first when canonical is unset). Saves crawlers a
  hop and consolidates link equity on the canonical destination.
  301 + 302 redirects now also pick up `Cache-Control` from the
  per-endpoint rule (was 200/302 only).
- Per-page `<title>` tags now follow the pattern
  `<page-specific> | <inbox> | <site_name>`, replacing the prior
  `· `-separated style and adding the inbox + scope tokens that
  were missing on a few pages (message, search, daily). Search
  results' title now includes the query string. Long subjects on
  patch-series messages truncate at 80 chars so the `<title>` stays
  readable in SERPs. Atom feed `<title>` strings switched to the
  same `|` separator for consistency.
- `/sitemap.xml` entries now carry `<lastmod>` (date-only,
  `YYYY-MM-DD`): per-article entries use the article's own date;
  per-inbox dashboards use the latest article date in that inbox;
  the meta-index uses the global latest. Helps crawlers prioritise
  recheck schedules without affecting cached output (existing 1h
  TTL still applies).

## [1.4.1] – 2026-05-07

### Fixed

- Ingest no longer crashes mid-batch when a message's RFC 5322 `Date`
  header carries `-0000` (which `email.utils.parsedate_to_datetime`
  returns as a tz-naive datetime). The Phase 1 observation tally
  used `max(prev_ts, parsed.date)` and raised `TypeError: can't
  compare offset-naive and offset-aware datetimes` the moment a
  `-0000` message landed in the same batch as a tz-aware one,
  rolling back the entire batch — which is why a fresh lkml ingest
  walked all 20 epochs but persisted only 26 articles. Now
  normalised to aware UTC at the entry point, in both `ingest_epoch`
  and `backfill_canonicals`.

## [1.4.0] – 2026-05-06

### Changed

- **Schema migration ownership moves entirely to the scheduler
  sidecar.** The web container's `CMD` no longer runs
  `alembic upgrade head` — only `mimir-tasks` does, before its
  loop starts. Single source of DDL truth, no race between two
  parallel `alembic upgrade head` invocations on cold start. The
  example `compose.yaml` now flips `depends_on` so `mimir-web`
  waits on `mimir-tasks` with `condition: service_healthy`; the
  sidecar reports healthy after touching `/data/.migrated`, so a
  fresh volume bootstraps cleanly without gunicorn ever serving
  against an unmigrated DB. systemd deployments are unaffected —
  `mimir.service` still has its own `ExecStartPre=alembic`.

### Added

- **SEO Phase 3: render-side canonical surface.** Each cross-posted
  article is now served at one URL as far as search engines and feed
  readers are concerned:
  - Message pages emit `<link rel="canonical" href="...">` pointing
    at the canonical inbox's URL (`/<canonical-inbox>/YYYY/MM/<id>`).
    Falls back to the alphabetically-first linked inbox when
    `canonical_inbox_id` is NULL — stable across renders so the SEO
    signal doesn't flicker.
  - `/sitemap.xml` now emits one `<url>` per article (the canonical
    URL) instead of per-inbox-recent-N. Walk is global, ordered by
    date desc, capped at `SITEMAP_RECENT_GLOBAL` (1000). Eliminates
    the duplicate-content signal from cross-posts.
  - Atom feed entries' `<id>` and `<link>` use the canonical inbox
    name regardless of which feed served the entry. Feed readers
    that key on `<id>` collapse cross-posts to a single entry across
    feeds.
- **SEO Phase 2: backfill CLI** for resolving canonical_inbox_id on
  pre-existing articles. `flask --app mimir admin canonicals backfill`
  walks articles newest-first, reads each one's RFC 5322 blob via the
  existing read path, accumulates per-inbox address observations, and
  resolves canonicals against the (auto-promoted) list_address map.
  Idempotent + resumable: by default skips already-set canonicals;
  `--reprocess` re-examines them. `--inbox NAME` restricts the walk;
  `--limit N` caps a session. Mid-walk auto-promotion every 200
  articles ensures list_addresses settle early so the bulk of the
  pass resolves canonicals correctly.
- **SEO Phase 1: ingest-time canonical-inbox resolution.** New
  `articles.canonical_inbox_id` (FK → inboxes, ON DELETE SET NULL)
  records the author's intended primary list, derived from the first
  list-shaped address in `To:` then `Cc:` at ingest time. Render-time
  use of this field comes in Phase 3.
- New `inboxes.list_address` column + `inbox_address_observations`
  table (composite PK on `(inbox_id, address)`). On every parse,
  list-shaped addresses from To/Cc are tallied per inbox; once an
  inbox has ≥50 observations and a clear modal address (≥70%
  dominance over the runner-up), `Inbox.list_address` is auto-promoted
  so subsequent ingests resolve canonical correctly. No operator
  configuration needed for kernel-relevant lists.
- `mimir.canonical` module with the conservative list-shape filter
  (suffix-match against a baseline of known list hosts: vger.kernel.org,
  kvack.org, lists.{infradead,freedesktop,ozlabs,linux-foundation,
  linaro,linux.it,debian}.org, lists.linux.dev, alsa-project.org,
  nongnu.org, ffmpeg.org, redhat.com, kernel.org).

## [1.3.0] – 2026-05-06

### Added

- Footer now records the running mimir version (e.g. "Generated by
  mimir 1.3.0.") and `mimir.__version__` is exposed for any other
  callers that want to read it. Reads from the installed package
  metadata; falls back to `0.0.0+unknown` in source-tree-only
  checkouts.

### Fixed

- CI now triggers on `v*` tag pushes, not just `main` pushes, so
  released versions actually publish images. Previously `:1.0.0`,
  `:1.1.0`, `:1.1.1`, and `:1.2.0` never reached
  `ghcr.io/sgaduuw/mimir`, and `:latest` (gated on tag pushes) never
  moved — the registry only carried `:main` and `:sha-*` from main
  pushes.

## [1.2.0] – 2026-05-06

### Added

- `PINNED_INBOXES` setting (default `["lkml"]`). Inboxes listed here
  surface at the top of the meta-index `/` in config order, with the
  rest following alphabetically. Comma-separated as an env var. Set
  to empty for pure alphabetical.

## [1.1.1] – 2026-05-06

### Added

- `TRUSTED_PROXY_HOPS` setting (default `0`). When `> 0`, mimir wraps
  its WSGI app in Werkzeug's `ProxyFix` so `request.remote_addr`,
  `.scheme`, and `.host` reflect the real client through that many
  trusted reverse-proxy hops — fixing the access log showing the
  proxy's address instead of the client's. Off by default because
  enabling it on a directly-exposed app would let anyone spoof those
  values via a forged `X-Forwarded-For`. `compose.yaml` ships
  `TRUSTED_PROXY_HOPS=1` (replacing the earlier `FORWARDED_ALLOW_IPS`,
  which only handled scheme detection — gunicorn doesn't rewrite
  `REMOTE_ADDR` on its own). systemd deployments are unaffected; set
  the env var if you stack a reverse proxy in front.

### Fixed

- Structured access log now records the actual `User-Agent` header.
  Previously every request logged `"ua": null` because the code
  guarded on `request.user_agent`, whose `__bool__` depends on
  Werkzeug's UA parser recognising a known browser — non-browser
  values like `curl/8.20.0` (and, in this Werkzeug, even Firefox)
  evaluated falsy. Now reads the raw header directly.

## [1.1.0] – 2026-05-06

### Added

- Auto-ANALYZE at the tail of `ingest_inbox`: when a run lands at least
  `ANALYZE_AFTER_INGEST_ROWS` (default `10000`) new + cross-post-linked
  messages, refresh the SQLite query-planner stats. Catches the
  freshly-added-inbox bootstrap case where the planner stats from the
  post-migration empty-table ANALYZE go stale once millions of rows
  land. Set to `0` to disable.

## [1.0.0] – 2026-05-06

First production release. Live at <https://ratatoskr.run> serving
linux-fsdevel and lkml.

### Added

- Persist parse failures: every commit whose `m` blob can't be parsed
  during ingest lands in a new `parse_failures` table keyed by
  `(inbox, epoch, commit_sha)` with `error_class`, `error_message`,
  `first_seen`, `last_attempt`, `attempts`. Cleared automatically when
  a re-walk parses the commit cleanly.
- `flask --app mimir admin failures list` — enumerate persisted failures,
  filter by `--inbox` / `--epoch` / `--error-class`.
- `flask --app mimir admin failures replay <inbox>` — re-fetch each
  failure's blob, re-run the parser, insert the article (or cross-post
  link) on success, bump `attempts` on continued failure. Use after a
  parser fix.
- Scheduled-tasks sidecar: `deploy/scheduler.sh` shipped in the image
  at `/app/scheduler.sh`. Runs `warm-cache`, `update`, `analyze`,
  `vacuum` on env-tunable cadences (`WARM_CACHE_EVERY`,
  `UPDATE_EVERY`, `ANALYZE_EVERY`, `VACUUM_EVERY` — seconds).
  `compose.yaml` adds a `mimir-tasks` service for container
  deployments, replacing the cron / systemd-timer trio for that
  shape.
- CI publishes the Docker image to `ghcr.io/sgaduuw/mimir` on every
  push to `main` (`:main`, `:sha-<short>`) and on `v*` tags
  (`:<version>`, `:<major>.<minor>`, `:latest`). PRs still build
  for verification only.

### Changed

- **Container layout**: consolidated to a single `/data` bind mount
  with `/data/db/mimir.db` (SQLite) and `/data/Inboxes/<name>/git/`
  (mirrors) underneath. `/app/Inboxes` is now a symlink to
  `/data/Inboxes` so the default relative `INBOXES` config still
  resolves cleanly. Default `DATABASE_URL` updated to
  `sqlite:////data/db/mimir.db`.

### Fixed

- Container image now includes `git` and `ca-certificates`, so
  `mimir update` (which shells out to `git clone --mirror` /
  `git fetch`) actually works in the container. The `python:3.14-slim`
  base ships neither.

## [0.1.0]

First tracked release. Summary of what shipped in the public-inbox
indexer line (post-rewrite from the early NNTP/mongo prototype).

### Added

- public-inbox v2 ingest: dulwich walker over per-epoch repos, parallel
  parse via `ProcessPoolExecutor`, per-(inbox, epoch) `IngestState`
  checkpoints, five outcome buckets (`new` / `linked` / `dup_batch` /
  `dup_db` / `failed`).
- Multi-inbox support with cross-post dedup: one `articles` row, one
  `article_lists` row per inbox.
- Read path: `mimir.store.read_message` does SQL lookup + dulwich blob
  fetch + `parse_message`; bodies / full headers / attachment bytes are
  never stored in SQLite.
- Web UI: per-inbox dashboard (active threads, trackers, pulls, releases,
  this-day-in-history, sparkline, archive stats); daily / monthly /
  yearly archive views; subject + author search; per-author chronological
  view; Message-ID lookup at `/m/<id>` and `/<inbox>/m/<id>`; per-message
  view with thread tree and JWZ subject grouping; attachment download +
  Pygments-highlighted preview; HTMX-driven "load more" recents.
- Atom feeds (per-inbox, per-author).
- Admin CLI for inbox CRUD with cache invalidation
  (`flask --app mimir admin inbox …`).
- Sync command (`flask --app mimir update`): manifest discovery,
  `git clone --mirror` + `git fetch --prune`, then ingest.
- Operations CLI: `warm-cache`, `analyze`, `vacuum`, `reindex`, `show`.
- Deployment artifacts: multi-stage Dockerfile, `compose.yaml`,
  systemd units + timers, Caddy and nginx reverse-proxy examples.
- Observability: `/healthz` + `/readyz` probes, structured per-request
  logging with request IDs.
- Standards files: `robots.txt`, `sitemap.xml`, `security.txt`
  (RFC 9116, gated on `SECURITY_CONTACT`).
- DB-backed cache (JSON-encoded with a small dataclass registry) +
  `cache.get_or_compute(session, key, ttl, fn)`; `CACHE_NAMESPACE_VERSION`
  cache-buster.
- GitHub Actions CI.
- Tests: parser contracts, cache encoder/decoder, route smoke,
  rendering pipeline, inbox service layer, threading CTEs, dashboard
  helpers, ingest outcome bucketing, store / web helpers.

### Security

- Response headers: CSP, HSTS, X-Frame-Options, Referrer-Policy,
  X-Content-Type-Options.
- Email-address redaction outside an allowlist (`<hidden>`); referenced
  Message-IDs render as neutral `[ref]` placeholders.
- 50 MB hard cap on `parse_message` input.
- `git clone` argv hardened against manifest-driven injection.
- Pinned CDN assets with SRI hashes.

[Unreleased]: https://github.com/sgaduuw/mimir/compare/v1.5.1...HEAD
[1.5.1]: https://github.com/sgaduuw/mimir/releases/tag/v1.5.1
[1.5.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.5.0
[1.4.1]: https://github.com/sgaduuw/mimir/releases/tag/v1.4.1
[1.4.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.4.0
[1.3.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.3.0
[1.2.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.2.0
[1.1.1]: https://github.com/sgaduuw/mimir/releases/tag/v1.1.1
[1.1.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.1.0
[1.0.0]: https://github.com/sgaduuw/mimir/releases/tag/v1.0.0
