# Changelog

All notable user-facing changes to mimir.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe behaviour, schema, config, and CLI/route shape changes —
not internal refactors. Categories: **Added**, **Changed**, **Deprecated**,
**Removed**, **Fixed**, **Security**.

## [Unreleased]

### Security

- DCO-trailer rendering (`Signed-off-by:` / `Reviewed-by:` /
  `Acked-by:` / `Tested-by:` / `Reported-by:` lines in message
  bodies) escapes the redactor's return value before splicing into
  HTML output. Previously, the trailer renderer trusted the
  redactor to return safe HTML and spliced its output verbatim;
  combined with a permissive email regex that allowed `"`, `'`,
  `<`, `=` in the local-part, a message body carrying a crafted
  `Signed-off-by: X <a"onmouseover=alert(1)@kernel.org>` line
  could land a live event-handler attribute on the rendered page
  whenever the substring allowlist matched. The fix is
  defense-in-depth: the renderer now `html.escape`s the redactor
  output, and `_EMAIL_ANGLE_RE` is tightened to exclude HTML
  metacharacters so hostile addresses fall through to the default
  escaped-text path. No data migration required; effective on
  every page render after upgrade.
- `_content_disposition` strips control bytes (CR, LF, NUL, tab,
  DEL) from the ASCII `filename="…"` form of the
  Content-Disposition header on attachment downloads. A
  maliciously-crafted attachment filename carrying CR/LF would
  otherwise have permitted HTTP response-header injection (RFC
  7230 header-line splitting) had the WSGI layer not already
  rejected the bytes. Defense in depth at the application layer.

## [1.13.1] – 2026-05-13

Polish PATCH on top of 1.13.0, addressing the four nits the
2026-05-13 launch-approval review flagged on the search and
author pages. No behavior changes; pure SEO / a11y / consistency.

### Added

- `SearchResultsPage` JSON-LD on `/<inbox>/search` when the route is
  rendering actual results (skipped on no-query / too-short / zero-
  results forms — those are bare search boxes, not results pages).
  `url` mirrors the `<link rel="canonical">` (bare `/<inbox>/search`,
  no query string), keeping individual `?q=` URLs out of the index
  while still giving crawlers a structured-data signal that this
  page is search results.
- `ProfilePage` JSON-LD on `/<inbox>/author/<sub>` with a `Person`
  mainEntity whose `name` is the sender substring the page indexes
  against. We don't try to resolve the substring to a single
  identity (queries like `@kernel.org` deliberately match many
  people); the structured data describes the page, not a single
  person.

### Changed

- `<h2>` on the search and author pages promoted to `<h1>` — both
  are top-level pages and accessibility / SEO consumers expect a
  single top-level heading per page. The page `<title>` is
  captured separately in `base.html` so this is purely a
  body-content fix.

### Fixed

- `<link rel="canonical">` on `/<inbox>/author/<sub>` now uses
  `urllib.parse.quote` on the `sub` segment, matching the
  `urlencode` filter used by the `<link rel="alternate"
  type="application/atom+xml">` for the author-feed. Pre-fix, a
  query like `torvalds@` rendered the canonical with raw `@` and
  the atom link with `%40` — same target, two encodings.

## [1.13.0] – 2026-05-12

Driven by the 2026-05-12 production-page re-review. User-visible
branding on the front page and link-card previews, plus a sweep of
privacy / metadata / scheme correctness fixes flagged by the
review and a few adjacent items the bundles surfaced.

### Added

- Homepage hero on `/`: small (~220px) Ratatoskr figure left of the
  site wordmark and tagline. Manuscript-marginalia layout, stacks on
  narrow viewports. Same source image as the OG card so a link-card
  preview reads as a screenshot of the destination.
- Footer credit line on every page: `Logo: Ratatoskr from a
  17th-century Icelandic Edda manuscript, Árni Magnússon Institute
  (public domain).` Same string reused as the `og:image:alt` value.
- Visible **Search** submit button on `/<inbox>/search` (paired with
  the existing input inside a Pico `[role="group"]` for the inline
  pill look). Enter-to-submit on a hardware keyboard already worked;
  the button matters for phone-thumb usability.

### Changed

- The OG image is now a 1200×630 PNG instead of a templated SVG
  wordmark. Twitter/X doesn't render SVG and LinkedIn is inconsistent
  on it; PNG is the safer baseline. Composition: a 17th-century
  Icelandic Edda manuscript depiction of Ratatoskr (Árni Magnússon
  Institute, public domain) on the left, the `ratatoskr.run`
  wordmark + tagline on the right in Palatino on a sampled
  parchment background. Asset is pre-baked by `bake_og_image.py`
  (Pillow, dev-only dep) and checked in at
  `mimir/static/img/og-image.png`. Route is now `/og-image.png`;
  `/og-image.svg` is removed (was a wordmark-only placeholder, low
  reach). `og:image:width=1200`, `og:image:height=630`, and
  `og:image:alt` (mirrored on `twitter:image:alt`) are now emitted
  for the picky link-card renderers.

### Fixed

- Message-ID no longer leaks via the thread-tree `data-*` attributes.
  Tree `<li>` elements, the `<article id="msg">` wrapper, and the
  `<html>`-level fold-controller hooks previously carried the RFC 822
  Message-ID (which often encodes a sender's email-shaped token), even
  while the visible HTML hid it via `<hidden>`. The attributes now
  carry the integer `Article.id` and have been renamed
  `data-article-id` and `data-thread-root-id` to make the migration
  obvious. Thread-fold pins keyed by Message-ID in `localStorage` will
  miss once and re-establish on next interaction.
- JSON-LD `author.name` on message pages is now the display name
  only, matching the visible page's privacy posture. Previously it
  carried the literal `<hidden>` placeholder for redacted senders
  (reads as broken metadata in structured data) and the full email
  for allowlisted senders (defeats the visible redaction symmetry).
  Both surfaces now consistently render the display name.
- Atom feed `<author><name>` now uses display-name only, matching
  JSON-LD's `author.name`. Previously a redacted sender's byline
  rendered as `David Woodhouse <hidden>` in feed readers — same
  broken-metadata shape the JSON-LD fix already cleaned up.
- `_site_base()` now upgrades the URL scheme to `https` whenever
  `X-Forwarded-Proto: https` is on the request, even if `ProxyFix`
  isn't decoding it (wrong hop count, header outside the trusted
  set). Defends against the canonical / og:url / og:image / JSON-LD
  URLs splitting between `http` and `https` on the same page when
  only one signal is wired up. `SITE_BASE_URL` remains the
  deterministic override.
- `/static/*` assets (currently `thread-fold.js` plus the new
  Ratatoskr image) now carry `Cache-Control: public, max-age=86400`
  instead of Flask's default `no-cache`. Every page load was
  re-fetching the JS controller; with the homepage hero image
  landing in the same release, the cost would have scaled by the
  size of any new static asset. The routed Cache-Control entries
  (favicon, og-image, sitemap, etc.) are unaffected — they're
  applied by the `bp_web` after-request hook, which doesn't see
  `/static/*` traffic. 1-day TTL trades a small bandwidth saving
  for fast deploy-cycle propagation: a JS bug fix lands within 24
  hours rather than the week the routed-asset entries use.
- `<html lang="en">` now renders as a clean single-line tag on every
  route. The pre-fix shape left a stray indented `>` on its own line
  in view-source on every non-message page (the `html_data_attrs`
  block was always empty there). Cosmetic.

## [1.12.3] – 2026-05-11

### Fixed

- `dev-seed-thread` would silently no-op when re-run within the same
  second (message-ids stamped with second-precision `%H%M%S` collided
  on the dup-on-ingest check). Stamp now uses microsecond precision
  so re-runs always append fresh messages. Caught by the new
  idempotency test.
- In `expanded` thread-fold state, the box border was dropped
  entirely, breaking the visual connection with the bordered
  toolbar above. The box now keeps its border in `expanded` (just
  loses the height cap), so the toolbar + tree stay a single
  connected unit. The "tree flows inline" was the original intent
  but the half-bordered look made it feel broken.

## [1.12.2] – 2026-05-11

### Fixed

- Thread-fold toggle buttons + tree-tab-on-load were dead on the
  v1.12.0/v1.12.1 deploy: the JS was inline in `message.html`, and
  the production CSP (`script-src 'self' https://unpkg.com`) blocks
  inline scripts without an explicit nonce or `'unsafe-inline'`.
  Browsers silently dropped the controller code, so clicks gave
  focus rings but no state change. Moved the controller to a static
  asset at `/static/js/thread-fold.js` (CSP `'self'` allows it).
  Per-page context (`data-thread-root`, `data-thread-context`) now
  rides on `<html>` via Jinja blocks, so the FOUC-free synchronous
  script in `<head>` still has everything it needs without inlining.

## [1.12.1] – 2026-05-11

### Fixed

- Thread-fold toolbar visual + interactivity issues spotted on the
  v1.12.0 deploy: the three toggle buttons no longer stretch to
  full-width-thirds (caused by Pico v2's `[role="group"]` pill
  styling forcing `width: 100%` on the wrapper). Restructured the
  toolbar into a single `<header>` flex row sitting flush against
  the top of the thread box in `partial` mode (shared borders, top
  corners squared). Squares-as-list-markers fixed by also setting
  `list-style-type: none` on the `<li>`. In `closed` mode, clicking
  anywhere on the toolbar (not just the summary text) opens the
  tree to `partial`.

## [1.12.0] – 2026-05-11

### Changed

- Thread tree on message pages now has three fold states: `closed`
  (one-liner `N messages, M authors, Th ago`), `partial` (the previous
  bordered scrollable box, `min(50vh, 24rem)`), and `expanded` (no
  cap, tree flows inline with the page). Default is context-aware
  (root view → `partial`, deep reply → `closed`) and can be pinned
  per-thread via localStorage; the pin survives across sessions. A
  three-button toggle in the tree heading lets the user switch
  between states without leaving the page. Pin-vs-default mismatch
  is FOUC-free: an inline `<head>` script sets `data-thread-fold`
  on `<html>` before the section paints, so CSS resolves to the
  correct state on first paint.
- Tree-scroll-follows-active in `partial` mode: the currently-viewed
  message's `<li>` is centered in the scrollable box on render and
  after any state change, so the active row is never off-screen on
  long threads.

### Added

- HTMX-driven intra-thread navigation. Clicking a sibling/reply in
  the thread tree fires `hx-get` against the message URL with
  `HX-Request: true`, the server returns just the `<article id="msg">`
  partial, and the client swaps it in place — leaving the tree,
  navigation, and scroll position intact. The active marker in the
  tree follows the new message via class-toggling on
  `htmx:afterSwap`; the URL updates via `hx-push-url` so back/forward
  and share-the-URL still work. Falls back to a full page load when
  JS is off.

## [1.11.0] – 2026-05-11

### Added

- Per-author atom feed (`/<inbox>/author/<sub>/feed.atom`) is now
  discoverable: the per-author HTML page emits a
  `<link rel="alternate" type="application/atom+xml">` for feed-reader
  autodiscovery, and the inbox dashboard's tracker tiles surface a
  small `atom` link next to the existing `all →`.

### Changed

- `warm-cache` now also refreshes the three sitemap caches
  (`sitemap:index`, `sitemap:meta`, `sitemap:inbox:<name>`) when
  `SITE_BASE_URL` is set. Closes #14. The first crawler hit per
  hour no longer pays the cold compute on the chunky per-inbox
  sitemap. Skipped silently when `SITE_BASE_URL` is unset (the
  helper has no `request.url_root` to fall back on from the CLI).
- `warm-cache` also refreshes the atom-feed data sources —
  `recent_articles(limit=50)` per inbox (drives
  `/<inbox>/feed.atom`) and `author_recent(..., limit=50)` per
  tracked author (drives `/<inbox>/author/<sub>/feed.atom`).
  Different cache keys from the dashboard's `limit=5/10` flavours,
  so feed polls had been paying the cold compute first-per-hour.

## [1.10.0] – 2026-05-11

### Changed

- `/sitemap.xml` is now a sitemap index (`<sitemapindex>`) listing
  one sub-sitemap per inbox plus `/meta-sitemap.xml`, replacing the
  previous single monolithic `<urlset>`. Crawlers fetch sub-sitemaps
  independently and can skip unchanged inboxes via the per-entry
  `<lastmod>`. Each per-inbox sitemap (`/<inbox>/sitemap.xml`) lists
  the dashboard, year and month archives that actually have data,
  and the inbox's 5000 most-recent article URLs. Cross-posted
  articles appear in each linked inbox's sitemap — the canonical
  `<link>` on the page itself remains the deduplication signal.

### Added

- `/meta-sitemap.xml` — one-URL sub-sitemap covering `/`. Lives
  behind the sitemap index so the index can stay pure
  `<sitemapindex>` per the sitemaps.org schema.
- `/<inbox>/sitemap.xml` — per-inbox sub-sitemap. Cached per inbox,
  so an ingest into one inbox doesn't invalidate cached sitemaps
  for the others.

## [1.9.0] – 2026-05-11

### Added

- Year-browse list on the inbox dashboard now groups by decade
  (`2020s · 2026 2025 ... · 2010s · 2019 ... · 2000s · ...`) instead
  of a flat row of ~30 inline years that read as a wall on narrow
  viewports. Each decade gets its own line; no bullets, compact
  small-text styling, no JS.
- `SITE_BASE_URL` setting (optional). When set, used verbatim for
  every emitted absolute URL (`<link rel="canonical">`, `og:url`,
  JSON-LD `url`, sitemap, atom feed `id`). Force-corrects the scheme
  on deployments where the proxy chain (Tailscale Funnel + Caddy on
  ratatoskr.run) doesn't reliably set `X-Forwarded-Proto`. Empty
  default falls through to `request.url_root` for local dev.
- `<link rel="canonical">` on the homepage and the per-inbox
  dashboard (previously only message pages carried one). All routes
  now emit one via a fallback computed in a context processor.
- `<link rel="icon" type="image/svg+xml">` pointing at a new
  `/favicon.svg` route — squirrel-adjacent placeholder until a
  proper logo lands. Stops browsers from 404'ing on every page load.
- `<meta name="theme-color">` matching Pico's amber accent
  (`#ffc107`) so mobile browser chrome picks up the brand colour.
- `og:image` + `twitter:image` pointing at a new `/og-image.svg`
  route (1200x630 SVG wordmark). `twitter:card` bumped to
  `summary_large_image` to match. SVG is templated against
  `SITE_NAME` so a forked deploy gets a matching preview without
  per-fork art assets.
- `ItemList` JSON-LD on `/` (configured inboxes as list items) and
  `DiscussionForum` + `ItemList` JSON-LD on `/<inbox>/` (most-
  active threads as list items). Tells search engines these are
  topical hubs rather than flat link lists.
- `display_name` Jinja filter — display-name-only From line, no
  `<hidden>` placeholder. Used in `<meta name="description">`
  on message pages so search snippets and link cards don't carry
  the redaction placeholder as literal text.
- `clean_subject` Jinja filter — collapses RFC 5322 header-folding
  whitespace (`\n  ` continuation lines) into a single space.
  Applied at every subject render site; raw value stays untouched
  in the DB.

### Changed

- Jinja's `trim_blocks` + `lstrip_blocks` are now enabled in the app
  factory. Cosmetic, but the rendered HTML no longer carries the run
  of empty lines that un-rendered `{% if %}` / `{% for %}` blocks
  used to leave behind.
- Nav slot `N inboxes` (when on `/`) is now an anchor link to the
  inbox list (`#inboxes`) instead of bare text. The inbox-list
  heading carries a matching `id="inboxes"`.

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
