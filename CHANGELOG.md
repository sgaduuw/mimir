# Changelog

All notable user-facing changes to mimir.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe behaviour, schema, config, and CLI/route shape changes  
not internal refactors. Categories: **Added**, **Changed**, **Deprecated**,
**Removed**, **Fixed**, **Security**.

## [Unreleased]

### Added

- `write_transaction()` now accepts a `label` argument and logs a
  WARNING when the block holds the SQLite writer lock longer than
  `WRITE_TRANSACTION_SLOW_LOG_MS` (default 1000 ms; set to 0 to
  disable). Operator-facing diagnostic for cross-process writer-
  lock contention: a slow scheduler-side write
  (`label=auto_analyze:lkml held=16234ms`) correlates 1:1 with a
  slow broker dispatch on the cache side, so an operator looking
  at the broker's slow-RPC WARNING can grep the scheduler log for
  the same time window and identify the culprit. Labels added at
  every major write entry point: `ingest_inbox:<inbox>`,
  `promote_list_address:<inbox>`, `auto_analyze:<inbox>` (now also
  wrapped in `write_transaction()` so it shows up),
  `backfill_canonicals[:<inbox>]`, `backfill_article_files`,
  `backfill_article_trailers`, `backfill_patch_series`,
  `update_mainline:maintainers`, `update_mainline:link_trailers`,
  `analyze`, plus `broker:<op>` for each broker handler. Fires on
  both COMMIT and ROLLBACK paths so failures get attributed too.

## [1.33.1], 2026-05-20

### Fixed

- Broker client truncated cache write payloads larger than the
  kernel socket-send buffer (~208 KB on Linux by default),
  causing the broker to log `malformed JSON: Unterminated
  string` and the client to receive
  `cache_set: MalformedJSON`. Hit production for every inbox
  sitemap cache_set on the 1.33.0 deploy. Root cause: the
  client used `makefile('wb', buffering=0).write(...)`, which
  delegates to `SocketIO.write`, which calls `send()` exactly
  once and **returns the number of bytes actually written**;
  for payloads exceeding the socket buffer the short-write
  count was ignored and the leftover bytes silently dropped.
  Server side was already correct (it used `sock.sendall`).
  Switched the client to the same `sock.sendall` shape, which
  loops on partial sends. Regression-pinned with a >1 MB
  payload round-trip
  (`test_client_cache_set_handles_payload_larger_than_socket_buffer`),
  which closes the test-coverage gap that let this slip past
  the 1.33.0 CI (no prior test exercised payloads above a
  handful of bytes).

## [1.33.0], 2026-05-20

### Fixed

- Broker now serves multiple client connections concurrently.
  Replaced the single-threaded
  `socketserver.UnixStreamServer` handler with a queue + worker
  pool: each accepted connection runs on its own reader thread
  that enqueues JSONL request lines onto a shared
  `queue.Queue`, and a single worker thread drains the queue,
  dispatches the RPC, and writes the reply back to the
  originating socket. Fixes the production bug where, with two
  gunicorn workers + scheduler-tasks subprocesses all holding
  persistent connections, the broker only ever served one
  client at a time; the other clients' RPCs sat unread in the
  kernel buffer and timed out at the client's 5 s socket
  timeout. Writes stay serialised at the single worker so
  in-process SQLite contention is unchanged; the queue makes
  backpressure observable too. Slow-RPC WARNING now breaks
  total elapsed into `queued + dispatch` components plus
  current `qsize`, so an operator can tell whether the broker
  is contended at the front of the queue (many clients piling
  on) or at the back (writer lock held by scheduler-side
  ingest). Plan-pinned in
  `test_server_serves_two_clients_concurrently` and
  `test_server_serves_many_clients_concurrently`.

### Added

- Broker daemon now logs a WARNING when an individual RPC takes
  longer than `BROKER_SLOW_RPC_WARN_MS` (default 100 ms; set to
  0 to disable). Useful overload signal: healthy cache writes
  commit in sub-ms, so a sustained stream of slow-RPC warnings
  indicates writer-lock contention (an admin backfill running,
  ingest mid-commit batch) or a genuinely slow operation
  (`cache_delete_for_inbox` on a huge table). Default INFO log
  level surfaces the warnings without needing `-v`; the existing
  DEBUG line that fires on every RPC also gained the elapsed
  duration for ad-hoc latency inspection.

## [1.32.3], 2026-05-20

### Fixed

- Broker client was not thread-safe, producing
  `Errno 11 Resource temporarily unavailable` storms on
  `connect /data/.broker.sock` whenever multiple threads
  shared the process-singleton (notably the scheduler-
  sidecar's `warm-cache`, which fans out across
  `min(cpu_count, 8)` `ThreadPoolExecutor` workers). Concurrent
  RPCs raced on the same socket: interleaved writes broke
  JSONL framing, the broker returned `MalformedJSON` replies,
  the client closed and reconnected, every thread piled into
  fresh connects, and the broker's Python-default listen
  backlog of 5 overflowed. Reported by the operator after
  enabling broker mode on the production deploy with
  warm-cache running.
  - Added per-`BrokerClient` `threading.Lock` wrapping every
    `_rpc` call. Concurrent threads serialize cleanly on this
    client's socket; the broker is single-threaded upstream
    so the lock doesn't reduce achievable throughput, just
    keeps framing intact and avoids connect storms.
  - Bumped `_BrokerServer.request_queue_size` from Python's
    default of 5 to 256 so transient bursts during a slow RPC
    don't surface as EAGAIN to the client even when some
    other code path opens a fresh connection.
  - Regression pinned in
    `test_client_concurrent_rpcs_from_one_singleton`: 8 threads
    each issue 20 unique `cache_set` RPCs through one shared
    client; without the lock the test fails with framing
    errors or connect-storm OSErrors.

## [1.32.2], 2026-05-19

### Fixed

- Broker daemon was silent under default verbosity: `mimir
  broker --socket PATH` called `_configure_logging(0)` which
  sets the root logger to WARNING, suppressing the broker's
  INFO-level startup / shutdown / periodic-purge lines.
  Operators couldn't tell whether the broker container was
  running. Floor at INFO regardless of `-v` so lifecycle
  events are always visible; `-v` now turns on DEBUG (which
  also logs per-RPC: leading bytes of the request line plus
  the reply ok/error flag). Same INFO floor pattern as
  gunicorn's `--access-logfile -` for the web container.

## [1.32.1], 2026-05-19

### Fixed

- Broker daemon crashed per RPC after the first idle window
  longer than the handler's selector poll interval (100 ms),
  logging `OSError: cannot read from timed out object` and
  closing the connection. Root cause: the prior handler used
  `socket.settimeout(0.1)` plus `socket.makefile`'s buffered
  `rfile.readline()`. Python's stdlib `SocketIO` sets a
  permanent `_timeout_occurred=True` flag on the first
  `socket.timeout` and every subsequent buffered read raises
  `OSError` instead of returning to the caller, which my
  `except TimeoutError` clause didn't cover. Replaced with
  `selectors.select(timeout=0.1)` for the wait plus raw
  `socket.recv` for the read, accumulating into a line buffer
  so JSONL framing stays correct without poisoning the socket
  on idle. Functional impact in 1.32.0 was modest (the client
  reconnects on each broken socket and the RPC succeeds via
  retry) but the broker log filled with exceptions and every
  cache.set after the first one on a persistent connection
  paid an extra reconnect round-trip. Regression-pinned in
  `test_client_persistent_connection_survives_idle_window`.

## [1.32.0], 2026-05-19

### Added

- **Write-broker service (Phase 1)**: new `mimir-broker` process
  owns the sole writer connection to the cache table; web tier and
  scheduler-sidecar CLI commands submit `cache.set` / `delete` /
  `delete_for_inbox` / `purge_expired` over a UNIX domain socket
  instead of opening their own DB sessions. Eliminates SQLite
  writer-lock contention between gunicorn workers and the
  scheduler-tier writers, which was the load-bearing cause of
  silently-dropped cache writes and front-page stalls. Opt-in via
  `BROKER_SOCKET_PATH=/data/.broker.sock` on the web + tasks
  containers; broker mode is off when the env var is unset
  (pre-1.32.0 direct-SQLite path). Web tier additionally honours
  `MIMIR_ROLE=web` by issuing `PRAGMA query_only=1` on every
  connection so a non-cache write path that slipped past the
  broker dispatch raises instead of competing for the lock.
  The broker also owns periodic cache-row purge via an internal
  timer thread; the scheduler's `warm-cache` no longer drives
  `purge_expired` when broker mode is on.
  - New CLI commands: `mimir broker --socket PATH` to launch the
    daemon (blocks until SIGTERM/SIGINT), and `mimir broker-ping
    --socket PATH` for the compose healthcheck.
  - New compose service `mimir-broker`. Web `depends_on` it for
    service_healthy ordering; scheduler-tasks `depends_on` it
    transitively (web also depends_on tasks, broker also
    depends_on tasks for the `/data/.migrated` migration gate).
  - Complex writers (ingest, backfill, update-mainline, admin
    inbox CRUD) stay on the scheduler sidecar with direct SQLite
    access for Phase 1. Phase 2+ (deferable) migrates them into
    the broker.
  - `READ_ONLY_DB` continues to work; broker mode subsumes its
    purpose for the web tier (a broker-mode web container is
    already read-only at the SQLite layer), so the flag is
    redundant in compose deploys that adopt broker mode.
  - Plan-pinned in `tests/test_broker/` (protocol round-trips,
    handler dispatch, server lifecycle, client reconnect across
    broker restart).

## [1.31.2], 2026-05-19

### Fixed

- Front-page hangs caused by missing per-inbox cache rows when
  `warm-cache` has fallen behind for some inboxes. `ingest_inbox`
  now runs a lazy post-ingest warm at the end of any tick that
  moved rows (new or linked > 0): calls `archive_stats`,
  `daily_volume`, and `most_active_subsystems_in_inbox` with
  `force=False`, so a present cache row returns instantly (one
  SELECT, ~ms) and only a missing or expired row triggers the
  compute + `cache.set`. Keeps the 24h `archive_stats` TTL
  property intact: a steady-state UPDATE_EVERY=300s tick does
  not re-run the multi-second COUNT(*) on every fire; only
  recovers rows warm-cache failed to refresh. No-op tick
  (moved == 0) skips the warm entirely. Best-effort; a failed
  warm logs at warning and does not crash the ingest tick.

## [1.31.1], 2026-05-19

### Fixed

- Web container no longer crashes at startup with
  `OperationalError: attempt to write a readonly database` when
  `READ_ONLY_DB=true` is set. Hotfix to v1.31.0's `READ_ONLY_DB`
  toggle: `create_app()` used to call `bootstrap_inboxes()`
  directly, so the first env-driven `INSERT ... ON CONFLICT DO
  NOTHING` against the `inboxes` table fired during Flask's
  application factory, before any request was served, and tripped
  the `PRAGMA query_only=1` safety net. Moved inbox bootstrap to
  the scheduler sidecar (new `mimir bootstrap-inboxes` CLI
  command, wired into `deploy/scheduler.sh` right after `alembic
  upgrade head` and before the `/data/.migrated` healthcheck
  sentinel is touched). Web tier is now read-only at startup,
  matching the migration-ownership rule that already governed
  `alembic upgrade head`. Idempotent; admin edits to existing
  rows are never clobbered.

## [1.31.0], 2026-05-19

### Added

- Per-subsystem triage queues on the subsystem dashboard
  (`/<inbox>/subsystem/<name>/`), closing #209. Two new ranked
  lists below the existing widgets:
  - **Needs attention**: patches with one or more review trailers
    (`Reviewed-by` / `Acked-by` / `Tested-by`) that haven't landed
    in mainline, haven't been superseded by a later same-key
    revision, and haven't been Acked by a subsystem maintainer.
    Older than `SUBSYSTEM_NEEDS_ATTENTION_DAYS` (default 14).
  - **Quiet for N+ days**: patches with no trailers and no
    replies, older than `SUBSYSTEM_QUIET_DAYS` (default 30).
  Both ordered oldest-first so the most concerning entries
  surface at the top; both hidden when empty. Backed by
  `needs_attention_patches_in_subsystem` and
  `quiet_patches_in_subsystem` in
  `mimir/subsystems_dashboard/triage.py`; pre-warmed per
  pinned-subsystem by `_warm_subsystem_dashboards`. The
  maintainer-Ack pickup signal cross-references
  `article_trailers.address_normalized` against
  `subsystem_maintainers` (M:/R: roles only); a Reviewed-by from
  a maintainer is treated as feedback, not pickup. Plan-pinned
  in tests to walk `ix_articles_date` ASC over a bounded date
  range (`SUBSYSTEM_TRIAGE_MAX_AGE_DAYS`, default 180) with no
  full scans on `articles` / `article_trailers` /
  `article_files`. ~200 ms cold on NETWORKING [GENERAL] / MM
  CORE against the prod-mirror DB, well under the issue's 1 s
  budget.
- `READ_ONLY_DB` maintenance toggle for the web container. When set
  to `true`, the process quiesces all DB writes: `cache.set` /
  `delete` / `purge_expired` / `delete_for_inbox` short-circuit to
  no-ops, and `PRAGMA query_only=1` is issued on every connection as
  a belt-and-braces safety net. Used to hand the writer lock to a
  long admin operation on the scheduler sidecar (e.g.
  `admin canonicals backfill --reprocess` on the full corpus) without
  taking the site down. The flag is intentionally not persisted; a
  normal container restart without `READ_ONLY_DB` in the env
  restores read-write. The scheduler sidecar must stay un-flagged so
  migrations / ingest / cache hygiene keep working.

### Changed

- `write_transaction()` now raises the SQLite `busy_timeout` on the
  active connection to `Settings.sqlite_busy_timeout_ms_writes`
  (default 60 s, env-tunable via `SQLITE_BUSY_TIMEOUT_MS_WRITES`)
  for the duration of the block, restoring the web-tier default
  (5 s) when the connection is returned to the pool. Closes the
  follow-up gap left by v1.30.1: BEGIN IMMEDIATE itself can still
  fail with the recoverable `SQLITE_BUSY` when another writer is
  active, and on a busy production deploy (cache-write burst
  after an `archive_stats` invalidation, every cold-miss render
  writes its computed value back) the 5 s web-tier default starves
  a concurrent backfill within seconds. Operators no longer need
  to remember `SQLITE_BUSY_TIMEOUT_MS=60000` on the backfill
  command line; the helper does the right thing automatically.
  Web-tier request handlers keep the short timeout (the short
  budget there is intentional, a stuck request shouldn't hang for
  a minute).

## [1.30.1], 2026-05-18

### Fixed

- Long-running CLI write workloads (`admin canonicals backfill`,
  the `backfill-article-*` family, `ingest_inbox`/`update`, the
  `update-mainline` MAINTAINERS reparse + Link-trailer walk) no
  longer crash with
  `OperationalError("database is locked")` on a busy production
  deploy. Diagnosed on coruscant during a `canonicals backfill
  --reprocess` pass that consistently failed within a few seconds
  even with the scheduler paused and `SQLITE_BUSY_TIMEOUT_MS`
  raised to 10 minutes: the error wasn't the timeout-retryable
  `SQLITE_BUSY` but `SQLITE_BUSY_SNAPSHOT`, which fires when a
  transaction that started as a *reader* tries to upgrade to a
  *writer* and another connection committed in between. Snapshot
  upgrade is non-retryable via `busy_timeout` regardless of how
  patient you set it. The gunicorn-side `cache.set` writes
  (every cold-miss page render writes back its computed value)
  were the concurrent committers, so any backfill that read an
  article batch and then tried to write its updates would
  collide. New `mimir.extensions.write_transaction()` context
  manager opts the wrapped block into `BEGIN IMMEDIATE`, which
  acquires the writer lock at transaction start, so the
  read-then-write upgrade can't happen and concurrent writers
  queue politely via `busy_timeout`. Applied to every long-
  running write path; read-only paths (web routes, cache reads)
  keep the default deferred BEGIN, so they don't serialise on
  the writer lock.

## [1.30.0], 2026-05-18

### Added

- Scheduler sidecar honours `/data/.scheduler-paused` as an ad-hoc
  pause sentinel. `touch /data/.scheduler-paused` quiesces the
  loop (no warm-cache / update / update-mainline / analyze /
  vacuum firings) within ~10s; `rm /data/.scheduler-paused`
  resumes. Used during operator maintenance (e.g.
  `admin canonicals backfill`, manual SQL, `reindex` over a large
  epoch) where scheduler write contention would extend the
  ad-hoc work. One log line on each pause/resume transition (not
  per tick). In-flight tasks finish before the pause takes
  effect; the cadence isn't reset, tasks that became due during
  the pause fire on the next tick after resume.

## [1.29.0], 2026-05-18

### Changed

- Canonical-inbox resolution now demotes lkml (and any inbox in the
  new `Settings.canonical_demoted_inboxes` env, default `["lkml"]`)
  to a fallback tier. A cross-post that lands on a topical list +
  lkml canonicalises to the topical list, even when lkml appears
  first in the message's To/Cc walk. Reflects the convention that
  lkml is a firehose CC and the topical list is the conversational
  home, matches Google's observed canonical pick on prod
  cross-posts, and consolidates link equity on the page where
  review actually happens. The render-time alphabetical fallback
  (`canonical_inbox_id IS NULL`) gets the same demoted-to-back
  ordering for consistency. Re-run
  `mimir admin canonicals backfill --reprocess` after deploy to
  rewrite existing rows; the backfill is idempotent.

### Fixed

- Front-page inbox cards no longer stick on "not yet ingested"
  for the rest of the 24h `archive_stats` TTL after a freshly-added
  inbox finishes its first ingest. The race was: between
  `admin inbox add <name>` and the first `update`, the
  scheduler's `warm-cache` tick wrote `archive_stats:<name>` with
  `total=0`, and the subsequent ingest didn't invalidate it; the
  TTL refresh-window logic in `warm-cache` then preserved the
  stale row for ~24h. `ingest_inbox` now calls
  `cache.delete_for_inbox(name)` exactly on the empty-to-non-empty
  transition (`Inbox.last_article_date` was NULL at the start of
  the run AND ingest landed at least one `new`/`linked` row), so
  steady-state ingests of established inboxes don't churn the
  cache.

### Added

- Scheduler sidecar now runs `update-mainline` on a 10-minute
  cadence (env-tunable via `UPDATE_MAINLINE_EVERY`), alongside the
  existing `warm-cache`/`update`/`analyze`/`vacuum` knobs. Previously
  the kernel-tree pull and MAINTAINERS reparse had to be invoked
  manually (`podman exec mimir-tasks mimir update-mainline`), so
  the maintainer-derived half of the From-line allowlist and the
  per-subsystem dashboards drifted from upstream between manual
  runs. The task no-ops cheaply when mainline HEAD hasn't moved
  (the reparse short-circuits on unchanged `state.last_commit_sha`
  and the Link-trailer walk is incremental), so a 10-minute
  cadence is a per-tick `git fetch` + SHA compare at steady
  state. Sentinel: `/data/.last_update_mainline`.

## [1.28.2], 2026-05-18

### Fixed

- `parse_message` no longer drops messages that carry an empty
  first `Message-Id:` header followed by a real `Message-ID: <...>`
  further down. The naive first-match in `_raw_header` returned
  the empty string and the parser raised "message has no
  Message-ID", silently losing the message at ingest. Surfaced
  on a reindex of alsa-devel where Mark Brown's `Applied "..."`
  auto-reply bot emits exactly this shape; 116 commits on the
  `0.git` epoch were affected. `_raw_header` now picks the first
  non-empty matching header (strict improvement: same behaviour
  for the modal single-header case, and `In-Reply-To` /
  `References` only change when their first occurrence is
  empty). Re-run `mimir reindex alsa-devel 0.git` after deploy
  to recover the missed articles, `parse_failures` rows clear
  themselves on successful re-parse.

### Changed

- Ingest now logs a parse failure at `DEBUG` instead of `WARNING`
  when the same commit is already in `parse_failures` from a
  prior run. First-time failures still log `WARNING` (real new
  event). A reindex pass over a long archive with a stable set
  of untriagable blobs (RFC 5322 violators, oversized payloads)
  no longer floods the journal with one WARNING per known-bad
  commit per run.

## [1.28.1], 2026-05-18

### Fixed

- `parse_message` no longer drops the body when the message
  declares a charset Python's codec registry can't resolve (RFC
  1428's `unknown-8bit`, malformed encoded-word charsets, etc.).
  Previously the `LookupError`/`UnicodeDecodeError` catch on
  `body_part.get_content()` silently set `body = None` and the
  message rendered as `(no body)` even though the git blob held
  real text. Now falls back to
  `body_part.get_payload(decode=True).decode("latin-1", errors="replace")`,
  RFC 1428's recommended interpretation, latin-1 is bijective on
  bytes so high-bit content shows as mojibake but structure
  (ASCII tokens, addresses, patch markers) survives. Symmetric
  with the attachment-side recovery introduced in PR #258 / #262.
- `parse_message` now keeps leaf attachments whose declared
  Content-Type IS handled by the stdlib's content manager (e.g.
  `text/plain`) but whose declared charset isn't in Python's codec
  registry (RFC 1428's `unknown-8bit`, malformed encoded-word
  charsets, etc.). PR #258's catch in `_attachment_bytes` covered
  only `KeyError` (the content-type-registry miss); `LookupError`
  from a `codecs.lookup(charset)` failure on the text-content path
  is a *sibling* of `KeyError` in the stdlib's lookup hierarchy,
  not the parent, so attachments like `r8169-getstats.patch` and
  `putty.log` from older list archives kept getting dropped.
  Catch widened to `(LookupError, UnicodeError)`; same fallback to
  raw transfer-decoded payload.
- `_decode_rfc2047` now logs the fallback path at DEBUG instead of
  WARNING. The fallback is the canonical handling for buggy-mailer
  encoded-words (`=?UNKNOWN?...`, `=?unknown-8bit?...`, whitespace
  charset names, single-letter charsets) and the rendered subject /
  author already carries the verbatim `=?UNKNOWN?B?...?=` string as
  the "broken sender mailer" cue. On multi-list ingest the WARN
  volume swamped real signal.

## [1.28.0], 2026-05-18

### Added

- Message bodies linking to `lore.kernel.org/<slug>/<msgid>/...` now
  surface a trailing ` (local)` link routed to the canonical mimir
  URL when mimir has the referenced message indexed in any inbox.
  The lore URL itself is preserved (add, don't replace), readers
  can stay on-site without losing the external reference. Resolved
  globally (cross-inbox), routed via canonical-inbox just like the
  bare-Message-ID redirect at `/m/<id>`. The DCO-trailer renderer
  applies the same suffix when a trailer line carries a lore URL.

### Fixed

- `parse_message` no longer emits a flood of
  `dropping attachment ... (content-type 'multipart/mixed'): KeyError(...)`
  warnings when a message's non-body branch is itself a multipart
  wrapper (e.g. `multipart/signed` alongside `multipart/alternative`).
  Wrappers are containers, not attachments; skip them outright
  rather than round-trip through a KeyError-then-drop path. Pure
  noise reduction, no real attachment was being lost.
- `parse_message` now keeps leaf attachments whose Content-Type is
  unrecognized by the stdlib's content manager (e.g.
  `chemical/x-mopac-input` carrying an `hcidump.dat`, observed in
  the wild on `linux-bluetooth`). Previously a `KeyError` from
  `EmailMessage.get_content()` caused the attachment to be silently
  dropped with a warning; now we fall back to the raw transfer-
  decoded payload (base64/quoted-printable undone, bytes preserved
  verbatim) for any content-type the registry doesn't recognize.

## [1.27.0], 2026-05-18

### Changed

- JSON-LD `Person.email` and Atom `<author><email>` now ride along
  on message metadata for senders in the allowlist union
  (`Settings.email_allowlist` + MAINTAINERS-derived addresses),
  mirroring exactly what `_safe_from_filter` already does on the
  visible HTML side. Non-allowlisted senders' addresses stay
  redacted across both metadata and HTML, unchanged. Closes the
  prior under-attribution gap where allowlisted maintainers' email
  was on the rendered page but absent from machine-readable
  surfaces. The earlier display-name-only-everywhere posture
  (2026-05-12 review) is superseded; see CONTEXT.md "Redaction is
  a display-time decision".

## [1.26.0], 2026-05-17

### Changed

- `mimir admin inbox add <name>` now defaults `--mirror-path` to
  `Inboxes/<name>/git` and `--upstream-url` to
  `https://lore.kernel.org/<name>`, matching the conventional
  lore.kernel.org public-inbox layout (and the shape already used
  by `Settings.inboxes`). Both flags remain available to override
  either side independently. The command also echoes the resolved
  `mirror_path` / `upstream_url` on success so the operator sees
  exactly what got stored.

### Security

- SSRF hardening on the three operator-supplied outbound URL
  knobs (`Inbox.upstream_url`, `Settings.mainline_tree_url`,
  `Settings.indexnow_endpoint`). A new shared
  `validate_outbound_url` (in `mimir/_outbound.py`) rejects any
  IP literal in loopback (`127.0.0.0/8`, `::1`), link-local
  (`169.254.0.0/16` incl. cloud-metadata `169.254.169.254`,
  `fe80::/10`), RFC 1918 (`10/8`, `172.16/12`, `192.168/16`),
  IPv6 ULA (`fc00::/7`), unspecified / multicast / reserved,
  plus the `localhost` hostname. IPv4-mapped IPv6 addresses
  (`::ffff:127.0.0.1`) are unwrapped before the check so the
  IPv4 deny list isn't trivially bypassed. Scheme allowlist is
  `https` only (previously `mainline_tree_url` and
  `indexnow_endpoint` had no validator at all, accepting
  `http://`, `file://`, `git://`). Pydantic `field_validator`s
  on the two `Settings` fields and `InboxConfig.upstream_url`
  fail fast at config-load; `mimir.inboxes.validate_upstream_url`
  delegates to the shared helper for the admin-CLI surface.
- The two `urlopen` outbound call sites (manifest fetch in
  `mimir/sync.py`, IndexNow POST in `mimir/indexnow.py`) now go
  through a shared `OUTBOUND_OPENER` whose `NoRedirectHandler`
  refuses to follow any 3xx. The stdlib default follows up to
  10 redirects across hosts and schemes with no per-target
  inspection; a compromised upstream could 302 fetches into
  internal-only URLs, and for the IndexNow POST the same
  redirect path would exfiltrate the request body (carrying the
  operator's IndexNow key) to attacker hosts. Known limitation,
  not addressed: DNS rebinding (host resolves to an allowed IP
  at validation and to a denied IP at fetch). (#250)

## [1.25.1], 2026-05-17

### Fixed

- Patch-series detection silently dropped zero-padded position
  shapes (`[PATCH v3 00/27]`, `[PATCH v3 01/27]`, ...) produced by
  `git format-patch --numbered` on series with >=10 patches. The
  cover-letter discriminator anchored on `\b0/` (single zero only)
  and the in-series matcher on `\b([1-9]\d*)` (first digit
  non-zero), so any series ingested with column-aligned `NN/MM`
  positions lost both its cover-letter row and every in-series
  patch row. User-visible effect: a v3 cover page rendered without
  the "previous revisions" timeline, and the inter-revision diff
  surface had no positions to compare. The Rust HRT series surfaced
  this on lkml. Fix is two single-character regex relaxations
  (`0+` on the cover side, `0*` prefix on the matcher side); a
  `mimir backfill-patch-series` pass after deploy reindexes the
  affected articles. (#247)

## [1.25.0], 2026-05-17

### Added

- Branded 4xx/5xx error pages. Werkzeug's default plaintext error
  bodies are replaced with `mimir/templates/error.html` extending
  `base.html`, so 404 / 410 / 500 responses now carry the site
  shell (nav, footer, favicon, kbd-help dialog) and the same
  response headers as every other route. Error pages emit
  `<meta name="robots" content="noindex">` and skip the
  `<link rel="canonical">` so crawlers don't index typo URLs or
  treat the error URL as authoritative. (#237)

### Changed

- Page CSS now ships as a single external stylesheet at
  `/static/css/mimir.css` instead of inline `<style>` blocks in
  `base.html`, `index.html`, and `message.html`. Browsers cache
  the sheet across pages (subject to the existing
  `SEND_FILE_MAX_AGE_DEFAULT = 86400` 1-day window); a `?v=`
  query string keyed to `mimir.__version__` busts the cache on
  every release. (#228)
- Dependency refresh: `click 8.3.3 -> 8.4.0`, `ruff 0.15.12 ->
  0.15.13`. Both are minor / patch bumps that pyproject.toml's
  caret constraints already accept; lock-file only. (#236)

### Fixed

- `/readyz` no longer leaks the SQLAlchemy / driver exception
  repr in the 503 body when the DB is unreachable. Connection-
  string fragments, driver type, and any embedded credential leak
  in the URL were previously visible to any unauthenticated
  probe; the body is now a fixed `db unreachable` string and the
  exception goes to the structured access log via
  `logger.exception(...)`. (#233)

### Security

- CSP `style-src` no longer carries `'unsafe-inline'`. Every
  remaining per-element `style="..."` attribute (thread-tree
  depth indent, year-archive month tiles, patch-state aside,
  keyboard-help dialog, subsystem path lists, sparkline SVGs,
  body-text block) moved to a CSS class in `mimir.css`. The
  thread-tree depth case uses a finite `data-depth="N"` ladder
  enumerated 0..20 in the stylesheet; the template clamps deeper
  threads to 20. Pygments output on the attachment-preview route
  switched from `noclasses=True` to `noclasses=False` so token
  theming comes from the existing `.highlight .X` rules instead
  of inline `style="color:..."` per span. A future regression
  that re-introduces inline styles now fails CSP and the page
  refuses to render the offending element, instead of silently
  widening the XSS blast radius. (#230)
- CSP `script-src` pins the specific htmx version path
  (`https://unpkg.com/htmx.org@1.9.12/`) instead of the bare
  `https://unpkg.com` origin. An htmx bump in `base.html` must
  update the CSP entry in lockstep; the bare-origin form would
  have allowed any unpkg package or version to load. (#230)
- Added `Permissions-Policy` response header denying every
  powerful feature mimir doesn't use (camera, microphone,
  geolocation, payment, USB, MIDI, magnetometer, accelerometer,
  and the broader set, with empty allowlists `()`). Caps what an
  injected `<iframe>` / `<embed>` could activate under a future
  XSS-gated bug. (#230)
- HSTS now carries the `preload` directive. The site is
  HTTPS-only behind Caddy + Tailscale Funnel, so opting into the
  browser-bundled HSTS preload list (after submission via
  hstspreload.org) is consistent with the current posture. The
  directive alone doesn't auto-submit; it signals readiness.
  Removing it would walk back the security ratchet without an
  explicit code signal. (#230)
- CI workflow's `GITHUB_TOKEN` is now scoped to `contents: read`
  at workflow level (the docker job's `packages: write` override
  for GHCR push still wins). Narrows the blast radius if a
  workflow step is ever compromised. (#233)

## [1.24.0], 2026-05-17

### Changed

- Footer now links "mimir" to its source repository and credits
  the author with a `rel="me"` link to the author's personal site.
- `<meta name="generator">` now carries the running version
  (`mimir X.Y.Z`) instead of a bare `mimir`.

## [1.23.0], 2026-05-17

### Added

- Inline-rendered patches now carry per-hunk and per-line anchor
  IDs so URL fragments like `/<inbox>/.../<id>#h-2-L15` jump to
  line 15 of hunk 2. Each hunk wraps in
  `<div id="h-N" class="hunk">`; each line within carries
  `id="h-N-LM"` (1-indexed, including the `@@` header itself as
  line 1). Reviewers can deep-link to specific lines of a patch
  from issue trackers, Slack, IRC, etc. Anchors are suppressed on
  diffs nested inside quote blocks (the "quoted hunk" reply
  pattern) to avoid `h-N` collisions on pages where both render.
  (#211)
- Patch context, add, and remove lines pick up per-language
  Pygments highlighting overlaid on the existing add/remove
  prefix colouring. The lexer is detected from each
  `+++ b/<path>` line and stays active until the next `+++`
  switch; multi-file patches transition lexer on each new file.
  Unknown extensions, `/dev/null` (file-deletion targets), and
  binary patches fall back to `TextLexer` cleanly. (#211)
- New `mimir` shell command (Poetry console script) as a first-class
  CLI entry point. `mimir ingest`, `mimir backfill-patch-series`,
  `mimir admin inbox list` etc. work the same as
  `flask --app mimir <cmd>`; both invocations share the underlying
  Click commands via Flask's `FlaskGroup` discovery, so no operator
  retraining or migration window is needed. The scheduler sidecar
  (`deploy/scheduler.sh`) and the systemd one-shot units
  (`deploy/systemd/mimir-{analyze,vacuum,warm-cache}.service`)
  switch to the shorter form; the longer `flask --app mimir` form
  stays available indefinitely. (#221)

## [1.22.0], 2026-05-17

### Added

- New column `articles.patch_series_position` records each
  article's position within its patch series (NULL = not a series
  patch; 0 = cover letter; 1+ = in-series patch position from the
  `M` of `[PATCH M/T]`). Ingest writes it from the subject parser
  on every fresh article; in-series patches additionally inherit
  `patch_series_key + patch_series_version` from their thread
  parent's cover letter when present. `flask --app mimir
  backfill-patch-series` extended to handle in-series patches via
  the same thread-parent walk, with new `in_series_indexed` /
  `in_series_orphan` buckets in the output. The
  `/<inbox>/series/<key>/diff` route now resolves both sides via
  an indexed `(key, version, position)` lookup, falling back to
  the per-render heuristic resolver from #210 only when a side is
  awaiting backfill. The state card's "Series revisions" row now
  renders on `[PATCH N/M]` message pages too (previously cover-
  letter only), with a same-position filter so the timeline shows
  every revision of THIS patch instead of every patch in every
  revision; `[diff vs current]` links carry `pos=N` accordingly.
  (#212)

### Fixed

- Front-page "Last activity" string per inbox card no longer lags
  behind ingest by up to 24 hours. The value previously rode the
  same cache row as `archive_stats`'s `COUNT(*)` (24 h TTL,
  justified by the ~6 s COUNT pass on a 6M-row inbox), so a fresh
  ingest landed but the dashboard stayed pinned to whatever
  `MAX(date)` was when the cache was last refreshed. Materialised
  `Inbox.last_article_date` (bumped on every successful ingest
  commit, including cross-post links) and made `archive_stats`
  overlay it on the cached row at read time. Schema migration adds
  the column and backfills existing inboxes via correlated
  `MAX(a.date)` per row. (#216)

### Changed

- Message pages on patch articles now render a consolidated
  per-patch state card at the top of the body, replacing the
  previous standalone "Applied as" line and "Patch series"
  sidebar. The card has four rows, each rendered only when it has
  data: **Trailers** (per-role tally across `Reviewed-by`,
  `Acked-by`, `Tested-by`, `Reported-by`, `Suggested-by`,
  `Co-developed-by`, `Reported-and-tested-by`, with a
  `(N maintainer)` chip marking the subset attested by an
  M:/R: address on a subsystem this patch touches);
  **Landed** (mainline-commit landings via `Link:`-trailer
  reverse lookup, same surface as the old "Applied as" aside);
  **Series revisions** (cover-letter timeline with
  `[diff vs current]` links per non-current revision, carried
  over from the inter-revision diff route added under #210);
  **Activity** (days since the last reply in the thread). The
  card is gated on the subject reading as `[PATCH …]` so prose
  and `[GIT PULL]` / `[ANNOUNCE]` brackets get no card at all.
  Result round-trips through the existing 5-min cache so the
  render stays cheap on repeated views.
- Message page (`/<inbox>/<year>/<month>/<id>`) now uses ETag-based
  conditional revalidation instead of a 60-second `max-age` window.
  The route emits an ETag computed from `(article.id,
  mimir.__version__, max(thread node date), HX-Request flag)` and
  returns `304 Not Modified` when `If-None-Match` matches. The
  Cache-Control header flips from `public, max-age=60` to
  `public, no-cache`, which directs browsers and edges to always
  revalidate. Matched ETags resolve as a tiny 304 (no body), so the
  bandwidth cost is small; the within-window stale-after-deploy
  problem (a code change leaving cached pages mis-rendered up to a
  minute after release) is eliminated entirely. The thread walk
  moved before the body fetch so 304 responses skip the git-mirror
  read and the template render. HTMX intra-thread swaps get a
  distinct ETag so browsers don't confuse the partial response with
  the full page.

### Added

- New route `/<inbox>/series/<patch_series_key>/diff?from=<vN>&to=<vM>&pos=<pos>`
  surfaces the diff between two revisions of a patch series. `pos=cover`
  diffs the cover-letter bodies (changelog evolution); `pos=N` diffs the
  N-th in-series patch's body across revisions, surfacing both commit-
  message and patch-hunk changes in one view. The cover-letter sidebar
  on `[PATCH 0/N]` message pages gains a `[diff vs current]` link next
  to each non-current revision pointing at the new route. Scope is
  cover-letter+request-time-resolved-in-series-patches: in-series
  patches don't have `patch_series_key` set today, so the resolver
  walks each cover letter's thread children at request time and matches
  positions across revisions by canonical-subject equality with file-
  overlap fallback. Refuses to guess on ambiguous matches (returns a
  "couldn't pick" 404 rather than picking wrong on a review surface).
  Diffs cached 24h, source emails being immutable in the public-inbox
  mirror. Follow-up (#212) will persist `patch_series_position` to
  in-series patches, simplifying the resolver into an indexed lookup.

### Fixed

- Cover-letter patch-series sidebar (`/<inbox>/.../msg/<id>` on a
  `[PATCH 0/N]` view) no longer lazy-loads the inbox row for each
  cross-post inbox the series has touched. The handler eager-loads
  `Article.lists` but the chain stopped there; per-revision
  `al.inbox.name` traversal fell back to lazy fetches, deduped by
  the identity map to one query per distinct inbox above the
  eager-load. Chained the selectinload through `ArticleList.inbox`
  so the worst case is zero extra round-trips. Regression test pins
  that no `WHERE inboxes.id = ?` (per-id lazy form) fires during a
  cross-posted series render.

## [1.21.3] - 2026-05-16

### Fixed

- Scheduled `ANALYZE` and `VACUUM` in `deploy/scheduler.sh` no longer
  reset their cadence clock on every sidecar restart. The previous
  shape initialised `last_analyze`/`last_vacuum` to container start
  time, so a release rollover (or any restart) at a cadence shorter
  than the task's interval pushed the next firing out by another
  full interval. With recent release cadence the daily ANALYZE had
  effectively never run since the `article_files` / `article_trailers`
  indexes were added; `sqlite_stat1` on the prod corpus had zero
  entries for either table, leaving the planner blind for any query
  shape that depended on those indexes for cost estimation (#202).
  Each task's last-run timestamp is now persisted to
  `/data/.last_<task>` and read off the sentinel mtime at boot, so
  the cadence intent survives restarts. Sentinels are only touched
  on successful runs so a transient failure doesn't push the next
  retry out by a full cadence.
- `deploy/scheduler.sh` now runs `ANALYZE` immediately after
  `alembic upgrade head`, before touching the `/data/.migrated`
  healthcheck sentinel. Any new index introduced by the
  just-applied migration starts life with planner stats instead of
  being invisible to the optimiser until the next scheduled
  ANALYZE pass (or, prior to the scheduling fix above, never).

## [1.21.2] - 2026-05-16

### Added

- `warm-cache` now pre-warms the per-reviewer pages each pre-warmed
  per-subsystem dashboard surfaces. The dashboard's "Active reviewers"
  list links to `/<inbox>/reviewer/<address>`; previously that page
  paid a cold-miss on first click. The warm pass collects addresses
  from `active_reviewers_in_subsystem` across the top-N most-active
  subsystems, dedups (Greg KH, david@kernel.org, etc. show up across
  many subsystems), and warms `articles_reviewed_by` for each. TTL-
  aware refresh window is already in scope from the parent call, so
  this composes with the cron cadence cleanly. Companion to the
  `articles_reviewed_by` query rewrite below.

### Fixed

- Per-subsystem dashboard (`/<inbox>/subsystem/<name>/`) cold-miss
  latency: the shared `_subsystem_path_filter_sql` builder (used by
  `recent_articles_in_subsystem`, `daily_volume_in_subsystem`,
  `active_threads_in_subsystem`, `active_reviewers_in_subsystem`) no
  longer emits `path LIKE :prefix ESCAPE '\'` for directory-prefix
  globs. SQLite disables its LIKE-to-range-scan optimisation when
  `ESCAPE` is present, so every prefix branch fell back to a full
  scan of `article_files` (7M rows on prod); on `lkml` with
  NETWORKING [GENERAL] that produced ~10 s cold misses for
  `recent_articles_in_subsystem` and ~9.5 s for
  `active_reviewers_in_subsystem`. Replaced with UNION-of-seeks
  shaped as `path >= lo AND path < hi` per branch, each independently
  sargable against `ix_article_files_path`. Cold misses drop ~10-25×;
  warmer subsystems collapse to sub-100 ms. The literal `_` in
  globs like `arch/x86_64/` is handled correctly by the range
  comparison (no escape machinery needed). `recent_articles_in_subsystem`
  also drops its overfetch + Python X-filter pass since the SQL-side
  X: predicate has the same per-row semantics. Plan pinned in
  `test_subsystem_path_filter_uses_index_seeks`.
- Per-reviewer page (`/<inbox>/reviewer/<address>`) cold-miss latency:
  `articles_reviewed_by` no longer JOINs against an unfiltered
  MATERIALIZE'd derived table to compute the canonical-NULL fallback
  inbox name. The old shape scanned every `article_lists` row in the
  archive (millions) just to provide `MIN(inbox.name)` per article;
  on the prod corpus that blew past gunicorn's worker timeout for
  prolific reviewers (e.g. `david@kernel.org` on `linux-mm`).
  Replaced with a correlated subquery inside the COALESCE that fires
  only when `canon.name` is NULL and only for the (≤100) result
  rows. Same alphabetical-first fallback as before; the cache TTL is
  unchanged. Verified via `EXPLAIN QUERY PLAN`; pinned in
  `test_articles_reviewed_by_plan_drops_materialize`. External
  report 2026-05-16.

## [1.21.1] - 2026-05-16

### Security

- `/<inbox>/subsystem/<name>/`: control bytes in `<name>` (NUL,
  CR, LF, tab, every C0/C1 byte) now 404 at the URL boundary
  rather than being percent-encoded into the case-correction
  Location header. Also: the route now resolves the inbox via
  `_get_inbox_or_404` before the lowercase-correction 301, so a
  request for `/<bogus>/subsystem/UPPER/` 404s directly instead
  of wasting a 301 hop on the case fix first.
- CLI input validation at two boundaries:
  - `reindex` rejects EPOCH arguments that don't match
    `<N>.git` before joining onto `inbox.mirror_path`. Catches
    typos like `0` (missing `.git`) and refuses traversal
    shapes like `../../etc`.
  - `dev-seed-thread --inbox <name>` validates the name against
    the admin service's slug regex. The name flows into both a
    filesystem path and the synthesised RFC 5322 `To:` header
    bytes, so CR/LF in the value would inject a second header
    line. Local-dev-only command, but the cheap guard is worth it.
- Web tier: defense-in-depth response headers (CSP,
  X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
  X-Request-Id), Cache-Control, and the structured access log
  now fire on URLs that don't match any route. The hooks moved
  from blueprint scope (`@bp_web.before_request` /
  `@bp_web.after_request`) to app scope (`before_app_request` /
  `after_app_request`); previously, Flask's built-in 404 for
  unmatched paths bypassed all of them.
- `admin inbox remove --remove-inbox-data`: parent-directory
  promotion is now gated on the parent's basename equalling the
  inbox name. Previously, any `mirror_path` ending in a `git/`
  segment (e.g. an operator-supplied `/some/dir/git`) would have
  the parent `rm -rf`'d along with the mirror. The audit flagged
  this as the worst data-loss vector in the codebase. Now only
  the documented `<root>/<name>/git` layout triggers promotion;
  every other shape removes the literal `mirror_path` only. The
  resolved target is logged at WARNING level before deletion.
- `sync`: enforce wall-clock timeouts on the `manifest.js.gz`
  fetch (60 s) and on `git clone` (30 min) / `git fetch` (10 min),
  plus a 16 MiB cap on the manifest body. A hostile or misbehaving
  upstream can no longer stall the scheduler tick indefinitely or
  OOM the ingest sidecar via a giant response. `sync_epochs`
  catches `subprocess.SubprocessError` so a timeout on one epoch
  surfaces in `SyncResult.failed` and the tick continues.
### Fixed

- Front page (`/`): `<title>` now follows the established
  `<page-specific> | <site>` pattern as
  `indexed mailing list archives | ratatoskr.run` instead of just
  the site name. Bare site name was too short to be useful in
  SERPs and didn't tell crawlers / shared-link previews what the
  page is about. Cascades through `og:title` and `twitter:title`
  via the captured-block plumbing in `base.html`. External review
  2026-05-16.
- Front page (`/`): the Ratatoskr hero image now carries a
  substantive `alt` (`Ratatoskr, the messenger squirrel of Norse
  mythology`) instead of the previous empty alt. The squirrel is
  the brand identity beside the wordmark, not pure decoration;
  the prior rationale (rely on `og:image:alt` + footer credit)
  doesn't help, og:image:alt only reaches link-card crawlers and
  the footer credit covers attribution, not what the image
  depicts. External review 2026-05-16.
- `daily_volume` and `this_day_in_history` now derive 'today' from
  `datetime.now(timezone.utc).date()` instead of `date.today()`.
  `Article.date` stores public-inbox commit times in UTC; using
  the local date slid the window by the server's UTC offset (on
  Coruscant: CEST = 2 hours), slipping edge messages in/out
  around UTC midnight. The `this_day_in_history` cache key was
  also keyed on the local date while the SQL window was UTC, so
  the same request hit a stale key inside the offset window. The
  result is visible on the landing-page daily-volume sparkline
  and the "this day, 5 years ago" tile.
- Patch-metadata backfills (`backfill-article-files`,
  `backfill-article-trailers`) now read the body via
  `article.canonical_inbox` before falling back to
  `article.lists[0]`. The old behaviour was order-dependent on the
  SQLA loader: which mirror got read for a cross-posted article
  was non-deterministic across two runs, so on a partial-mirror
  host (one inbox available, the other not) the backfill could
  flip between "indexed" and "skipped" between ticks. Falling
  back to `lists[0]` only when canonical is NULL matches the
  render-time canonical-inbox rule. The shared walker
  (`mimir._backfill.walk_articles`) now eager-loads
  `Article.canonical_inbox` alongside `Article.lists` so the
  per-row decision doesn't N+1.
- `most_active_subsystems_global` now propagates `force=True`
  through to the per-inbox `_most_active_subsystems_in_inbox_full`
  call inside its `compute()` closure. Previously the outer cache
  wrap bypassed correctly but the inner per-inbox cache silently
  read stale rows, so a `warm-cache --force` recomputed the global
  aggregator off whatever the per-inbox caches happened to hold.
- `search_articles` cache key now case-folds the query so `Foo`
  and `foo` share one cache row instead of writing distinct
  rows for each casing. SQLite `ilike()` is case-insensitive so
  the underlying SQL would return identical results either way;
  the per-casing cache rows just wasted space and made cache
  hits less likely for the next visitor's query.
- `mimir.cache.delete` and `cache.delete_for_inbox` now swallow
  `OperationalError("database is locked")` and log a warning,
  matching `cache.set`'s best-effort posture. Admin CRUD callers
  (inbox rename / delete) would otherwise 500 on a successfully-
  completed DB change because the cache invalidation lost the
  lock race against a long-running VACUUM. Cached values still
  age out via TTL, so a missed invalidation is recoverable
  without operator intervention.
- `mimir.parser`: `In-Reply-To` headers carrying multiple
  msg-ids (broken senders sometimes emit `<a@x> <b@y>`) now
  resolve to the first msg-id rather than being stored as the
  literal `a@x> <b@y` string that never joined to any real
  Message-ID. `References:` and `In-Reply-To:` both also strip
  RFC 5322 CFWS comments `(...)` before splitting, so
  `<a@x> (comment) <b@x>` extracts `["a@x", "b@x"]` instead of
  surfacing the comment text as a junk reference.
- `_daily_view` and `threads_since_view` now compare `Article.date`
  against `datetime` bind parameters rather than
  `start.strftime(...)` / `end.strftime(...)` strings. SQLite is
  lax today, but the strftime form drops tz info on a tz-aware UTC
  column and is brittle on SQLAlchemy 2.x typing; the helper layer
  (`mimir.dashboard`) already uses the datetime form, so this just
  brings the route layer in line.
- `mimir.store._read_blob`: context-manage the dulwich `Repo`
  and surface `KeyError` from a stale commit_sha / GC'd blob as
  `MessageNotFound`. Previously the `Repo` instance kept packfile
  file descriptors open until GC (every message-page render
  reopened the repo, so the FD count grew over time), and a
  real-but-stale `article_lists.commit_sha` row would bubble out
  as a `KeyError` 500 instead of the 404 every caller already
  handles via `MessageNotFound`. Web / CLI / ingest call sites
  recover transparently.
- Canonical-inbox resolution and the off-list-parent hint now
  read `To:` / `Cc:` headers case-insensitively. RFC 5322 field
  names are case-insensitive and `mimir.parser` preserves the
  wire casing; some ML re-mailers downcase headers, so a strict
  `headers.get("To")` silently returned `None` and dropped the
  list addresses on the floor. Both `extract_list_addresses`
  callers (canonical-inbox pinning at ingest and the off-list
  list-host hint on the message page) recover transparently.
- `active_threads` decay score now clamps the recency exponent at
  zero. `pow(0.5, julianday('now') - julianday(date))` blows up to
  astronomically large values when `date` is in the future, letting
  a single mis-ingested or typoed row dominate the ranking. The
  clamp (`MAX(diff, 0)`) caps a future-dated row's contribution at
  `pow(0.5, 0) = 1.0`. `articles.date` is the public-inbox commit
  time per CONTEXT.md so future dates shouldn't arise, but the
  defensive clamp is the right shape for the silent-bug surface
  the audit flagged.

## [1.21.0], 2026-05-15

Per-subsystem dashboard latency pass. A real-world cold load on
`/linux-devicetree/subsystem/open firmware and flattened device
tree bindings/` was clocking ~10 s. Three reasons stacked up:
`recent_articles_in_subsystem` (the article_files × article_lists
× articles join with one OR per F: glob and a Python X: pass) was
the only one of the four dashboard helpers without a cache wrap;
the two trailer / threads helpers ran on a 5-min TTL that fit the
front-page real-time feel but was overkill for a per-subsystem
page; and the top-N most active subsystems weren't pre-warmed at
all.

This release caches the missing helper, raises the two short TTLs
to 1 h to match the other dashboard surfaces, and warms the top-20
most active subsystems per inbox so steady-state visitors land on
warmed cache. Long-tail subsystems (not in any inbox's top-20)
still eat a single cold load per hour; subsequent visitors in
that hour get the cached payload.

### Added

- `recent_articles_in_subsystem` is now cache-backed (1h TTL,
  matching the other per-subsystem dashboard helpers). Previously
  uncached: every cold subsystem-dashboard hit re-ran the
  article_files × article_lists × articles join with one OR clause
  per F: glob plus a Python X: filter pass, which on a busy
  subsystem like "open firmware and flattened device tree
  bindings" added ~10 s to the cold load.
- `warm-cache` now pre-warms the top-20 most active subsystems
  per inbox across all four per-subsystem dashboard helpers
  (`recent_articles_in_subsystem`, `active_threads_in_subsystem`,
  `daily_volume_in_subsystem`, `active_reviewers_in_subsystem`).
  Reuses the already-cached `most_active_subsystems_in_inbox`
  ranked list. Long-tail subsystems (not in any inbox's top-20)
  still eat one cold load per hour; subsequent visitors land on
  warmed cache.
- `mimir.cache` now round-trips Pydantic `BaseModel` types in
  addition to `@dataclass` ones. Required for caching
  `RelatedPatch`, which is the return shape of
  `recent_articles_in_subsystem`. Iterates `model_fields` + `getattr`
  rather than `model_dump()` so nested registered types keep their
  type tags through the encode pass.

### Changed

- `active_threads_in_subsystem` and `active_reviewers_in_subsystem`
  TTLs raised from 5 min to 1 h, matching the other per-subsystem
  dashboard helpers. The 5-min cadence fit the front-page
  real-time feel but was overkill for a per-subsystem page; the
  recursive CTE and trailer scans don't need to refresh every
  five minutes for a surface readers visit, not watch.

## [1.20.0], 2026-05-15

A focused warm-cache pass. The 1.19.3 hotfix dropped the heavy
subsystem aggregator from three cache keys to one per inbox, but
the prod tick (28 inboxes / 365 keys) was still ~185 s. Profiling
showed two unrelated culprits: `latest_stable_releases` was paying
~3 s per inbox to prove a negative across the full per-inbox date
index (only `stable` actually has matches), and every key was
being force-recomputed on every 5-minute cron tick regardless of
its TTL headroom.

This release bounds the offending queries, replaces unconditional
recompute with TTL-aware refresh, and parallelises the remaining
target loop across a thread pool. Combined effect on a typical
tick: ~185 s → roughly 15-25 s, with sub-linear scaling as new
inboxes are added.

### Added

- `warm-cache --workers N` flag controls the size of the worker
  pool that fans out the per-inbox target loop. Defaults to
  `min(cpu_count, 8)`. Pass `--workers 1` to force the serial
  path when debugging a slow target.

### Changed

- `latest_pull_requests` and `latest_stable_releases` are now
  scoped to the last 180 days. Without the floor, an inbox with
  fewer than `limit` matches forced SQLite to walk the full
  per-inbox date index proving the negative, costing ~3 s per
  inbox in warm-cache and dominating wall time on quiet inboxes.
  Inboxes with no matching activity in the window render an
  empty panel.
- `warm-cache` no longer force-recomputes every key on every
  tick. A new `cache.refresh_window(...)` context manager makes
  `get_or_compute` recompute only rows whose remaining TTL is
  below the window. With the cron firing every 5 minutes and a
  window of 450 s, 5-min-TTL keys still refresh every tick (no
  change), 1-hour-TTL keys refresh on the tick that lands in the
  last ~7.5 min of the hour, and 24h-TTL keys (`archive_stats`)
  refresh once per day instead of 288 times. Behavior at the
  request path is unchanged.
- `warm-cache` runs per-inbox and sitemap targets in parallel
  across a thread pool, each worker on its own SQLAlchemy
  session. The cross-inbox subsystem aggregator runs after a
  barrier so it consumes the just-warmed per-inbox cache rows.
  The `refresh_window` contextvar is propagated to workers via
  `contextvars.copy_context()`. Per-key `-v` lines may
  interleave; the summary line is unchanged.

## [1.19.3], 2026-05-15

Third PATCH on top of 1.19.0. The 1.19.1 and 1.19.2 hotfixes
eliminated the two fan-out shapes inside one
`most_active_subsystems_in_inbox` call; the warm-cache tick on
prod (28 inboxes / 393 keys) was still spending ~210 s, because
each inbox warmed three distinct cache keys for the same
aggregation (`limit=10` for the inbox dashboard, `limit=30` for
the cross-inbox aggregator's hedge, plus the global helper's
`limit*3=36` cold-miss path against an unwarmed key). Three
identical bulk-SQL + inverted-index walks per inbox per tick.

### Fixed

- `most_active_subsystems_in_inbox` and `most_active_subsystems_global`
  now cache a single limit-less full ranked list (internally
  capped at 100) per `(inbox, days)` resp. `(days)`, and callers
  slice from there for their specific `limit`. The public
  function signatures are unchanged; the cache key drops `:limit`.
  Warm-cache collapses to one target per inbox plus one global
  target, cutting the heavy subsystem work to a third of the
  previous tick.

## [1.19.2], 2026-05-15

Second PATCH on top of 1.19.0. The 1.19.1 hotfix addressed the
per-subsystem COUNT fan-out but left a second fan-out in place:
the front-page subsystem cards still called
`daily_volume_in_subsystem` per surviving top-N entry for the
inline sparkline. On prod that meant `limit*3 = 36` calls per
inbox cold (the global aggregator pulls the top-30 plus a hedge
to get a clean global top-10), each running its own per-
subsystem path-scoped GROUP BY against the articles table.
Cold call exceeded the worker timeout again.

### Fixed

- Inline sparkline buckets are now built in-memory from the
  same `(article_id, path, date)` tuples the bulk SQL already
  fetches for the activity ranking; no per-subsystem fan-out
  remains in `most_active_subsystems_in_inbox`. The window
  also switches from rolling 168 hours to a calendar 7-day
  span (today + 6 prior days) so the inline spark buckets line
  up with what `daily_volume_in_subsystem` would have queried.

## [1.19.1], 2026-05-15

PATCH on top of 1.19.0 fixing a fan-out blowup in the front-
page and per-inbox subsystem surfaces. The 1.19.0 deploy
returned 500 on `/` and `/<inbox>/` against the production
corpus when the cache was empty: `most_active_subsystems_in_inbox`
ran one COUNT query per MAINTAINERS subsystem (~1500 on lkml),
and the resulting cold call exceeded gunicorn's 30s worker
timeout. The cache-namespace bump in 1.19.0 forced exactly that
cold path on every request immediately after deploy.

### Fixed

- `most_active_subsystems_in_inbox` and the downstream
  `most_active_subsystems_global` rewritten to one bulk SQL plus
  a Python inverted-index walk: a single SELECT pulls every
  `(article_id, path, date)` tuple for the recent in-window
  articles in the inbox, and `subsystem_paths` rules are
  bucketed into `dir_prefix → {subsystem_id}` and
  `exact_path → {subsystem_id}` lookup maps so each path-row
  resolves to its matching subsystems in O(path_components).
  Sparkline fetch is deferred to the surviving top-N rather
  than every matching subsystem. Cold call drops from ≈30s
  (timeout) to ≈1-2s on the lkml corpus. The cached value
  shape is unchanged (`list[SubsystemActivity]`); no schema or
  namespace bump.

## [1.19.0], 2026-05-15

MINOR release. UX-focused pass: the front page and per-inbox
dashboards trade flat lists for a card-shaped layout, the per-
subsystem dashboards become navigable from every surface (front
page, inbox dashboard, patch page), and subsystem URLs get a
canonical lowercase form. No schema changes; no migrations
required.

### Added

- **Subsystem discoverability** across three surfaces. The per-
  subsystem dashboards (`/<inbox>/subsystem/<name>/`) were
  previously reachable only by direct URL; mimir now exposes a
  navigation path from every entry point a reader is likely to
  start at.
  - **Front page** (`/`) carries an "Active subsystems (last 7
    days)" card grid below the inbox cards. Each card links to
    the per-subsystem dashboard on whichever inbox saw the most
    activity for that subsystem in the window, surfacing the
    busiest variant. Cards carry the top `M:` maintainer's
    display name ("maintained by Kent Overstreet", with
    "et al." when MAINTAINERS lists more than one M: row), a
    status badge in the top-right for non-default `S:` values
    (`Supported`, `Orphan`, `Obsolete`, `Odd Fixes`;
    `Maintained` is suppressed since most subsystems sit at
    that value), a "Last activity: N{m,h,d} ago" relative-time
    line, and a 7-day daily-volume sparkline. Sizing matches
    the inbox cards above (same `minmax(18rem, 1fr)` grid +
    padding) for a shared vertical rhythm.
  - **Per-inbox dashboard** (`/<inbox>/`) carries a "Most active
    subsystems (last 7 days)" list below "Most active threads".
    Plain `<ul>` matches the surrounding section layouts on this
    page; the cards are reserved for the front-page surface
    where they have something to anchor against.
  - **Patch pages** linkify the "Subsystem: bcachefs" header so
    each subsystem name acts as a launch pad into the broader
    subsystem context.
  - New helpers in `mimir.subsystems`:
    `most_active_subsystems_in_inbox` (per-inbox) and
    `most_active_subsystems_global` (cross-inbox aggregator with
    busiest-inbox attribution). Both cached for 5 min;
    `warm-cache` pre-populates them. New `relative_time` Jinja
    filter.

### Changed

- **Subsystem dashboard URLs are now lowercase**. MAINTAINERS
  stores subsystem names in upper-case ASCII (`BCACHEFS`,
  `BTRFS FILE SYSTEM`); the route now treats lowercase as the
  canonical URL form. Uppercase or mixed-case requests return a
  301 redirect to the canonical lowercase URL, consolidating
  bookmarks and search-engine indexing on one shape. Lookup is
  case-insensitive against the stored row. The visible
  subsystem name is also lowercased everywhere it renders
  (heading, patch-page header, sparkline aria-label, subsystem
  cards): the upstream all-caps reads as shouty in body copy.
  DB rows keep the upstream-verbatim casing; only display
  lowercases.

- **Inbox overview on `/` is now a card grid** rather than a flat
  list. Each card shows the inbox name, a "📌 pinned" badge when
  the inbox is in `Settings.pinned_inboxes`, the message and epoch
  counts, the first-to-last date span, a "Last activity: N{m,h,d}
  ago" relative-time line, and a 30-day daily-volume sparkline.
  The whole card is the click target. CSS Grid auto-fit reflows
  cards from N-up on wide screens to single-column on phones; no
  breakpoint math.

### Internal

- `mimir.cache` `NAMESPACE_VERSION` bumped to v2 so pre-existing
  cached rows for the prior `SubsystemActivity` shape don't get
  decoded against the new fields. Other cached helpers ride on
  the new namespace transparently; warm-cache re-populates on
  the next 5-minute cron tick.

## [1.18.0], 2026-05-15

MINOR release. Three slices of #97 (review-attestation trailer
indexing, per-subsystem active-reviewers section, per-reviewer
page) plus a behaviour change: the email-redaction allowlist now
unions in every `M:` / `R:` address parsed from MAINTAINERS, so
recognised kernel maintainers and reviewers surface verbatim
without operator-side config.

### Added

- **Index review-attestation trailers** at ingest time (slice 1
  of #97). New `article_trailers` table records one row per
  `Reviewed-by:`, `Acked-by:`, `Tested-by:`, `Reported-by:`,
  `Suggested-by:`, `Co-developed-by:`, or
  `Reported-and-tested-by:` line in a message body. Address is
  stored verbatim plus a lowercased `address_normalized` for
  case-insensitive lookups. Indexed on
  `(role, address_normalized)` for the per-author lookup and on
  `article_id` for the per-message lookup. `Signed-off-by:` is
  deliberately not indexed (chain-of-custody, not a review
  signal). Quoted trailer lines from a parent message are
  skipped via the same line-start regex anchor used for diff
  extraction. New CLI subcommand `flask --app mimir
  backfill-article-trailers` walks historical articles to
  populate the table; idempotent and resumable, mirrors
  `backfill-article-files` in shape and flags.
- **Active reviewers** section on per-subsystem dashboards
  (slice 2 of #97). New section between "Most active threads"
  and "Recent patches" lists the people who have been most
  active on review-attestation trailers for patches in the
  subsystem over the last 30 days. Each entry shows total
  attestations, the per-role breakdown, and the date of the
  most recent attestation. Helper:
  `mimir.subsystems.active_reviewers_in_subsystem`. Cached for
  5 min per `(inbox, subsystem, days, limit)` key. Wildcard-
  only F: subsystems and 30-day windows with no attestations
  render no section.
- **Per-reviewer page** at `/<inbox>/reviewer/<address>` (slice
  3 of #97). Lists every patch in the inbox where this person
  appears on a review-attestation trailer, newest-first, with
  role badges and a per-role total in the header. Capped at 100
  most-recent attestations per page; a notice surfaces when
  the cap fires. Address from the URL is lowercased to match
  the `address_normalized` index. The route accepts any
  well-formed address (hostile shapes 404 via a regex defense
  on the URL parameter), but mimir only generates outbound
  links to this surface for allowlisted addresses, mirroring
  the redaction posture used by the From line and inline DCO
  trailers. The per-subsystem dashboard reviewer list renders
  allowlisted entries as clickable links to this page; non-
  allowlisted entries continue to show as `<hidden>` with no
  link. Helper: `mimir.subsystems.articles_reviewed_by`.
  Cached for 10 min per `(inbox, address, limit)` key. New
  template filter `is_allowlisted_address` exposes the
  allowlist check to Jinja.

### Changed

- **Email allowlist now unions the static `email_allowlist`
  setting with addresses parsed from `MAINTAINERS`.** Every
  `M:` (maintainer) and `R:` (reviewer) entry contributes its
  address to a frozenset that the redaction filters
  (`safe_from`, `_redact_trailer_address`,
  `is_allowlisted_address`) consult alongside the static list.
  Net effect: anyone the kernel tree recognises as a maintainer
  or reviewer surfaces verbatim across From lines, DCO trailer
  rendering, and the per-subsystem reviewer list (with
  clickable links to the per-reviewer page) without operator-
  side config. Cached in the `cache` table for 24h with a
  shared key; the `update-mainline` flow invalidates the cache
  after every MAINTAINERS reload so the web tier picks up
  changes on the next request. Degrades cleanly when MAINTAINERS
  hasn't been parsed yet (e.g. fresh deploys without the
  kernel tree mirrored): the dynamic set is empty and only the
  static allowlist applies. `L:` (list) addresses are
  deliberately excluded: they're per-subsystem list addresses,
  not personal contact, and have a separate code path. New
  helper module `mimir.maintainer_allowlist`. New cache
  primitive `cache.delete(key)` for single-key invalidation.

### Migration

After upgrading: run `alembic upgrade head` to create
`article_trailers`, then `flask --app mimir
backfill-article-trailers` to populate it for existing
articles. The dynamic-allowlist effect kicks in automatically
once `update-mainline` has populated `subsystem_maintainers`
(no separate step required).

### Deferred (follow-ups)

- Linkifying allowlisted reviewers in the inline body trailer
  block (the `Reviewed-by:` lines on the message page itself).
  The render-time path through `mimir.rendering` doesn't yet
  consult the allowlist for outbound link decisions.
- Cross-inbox per-reviewer aggregation (`/reviewer/<address>`
  at the root, no inbox scoping). Deferred until there's
  demand.

## [1.17.0], 2026-05-15

MINOR release. Two feature batches: per-subsystem dashboards
(#72) and message-page reader UX (#68). No schema changes;
ships as HTML/JS/CSS additions plus two new helpers in
`mimir.subsystems`.

### Added

- **Per-subsystem dashboards** at `/<inbox>/subsystem/<name>/`
  (closes issue #72). The MAINTAINERS data indexed in 1.15.0
  now powers a dedicated dashboard per subsystem with four
  surfaces: section header (name, status, full `M:`/`R:`
  maintainers, operator-facing with addresses verbatim), F:/X:
  paths list, 30-day daily-volume sparkline scoped to the
  subsystem's paths, and most-active threads (last 7 days,
  decay-weighted). Recent patches list at the bottom for direct
  navigation. Article scoping and active-threads ranking go
  through a new `_subsystem_path_filter_sql` helper that emits a
  parameterised `SELECT article_id FROM article_files WHERE …`
  clause; the existing `_active_threads_query` gains an optional
  `extra_seed_filter_sql` so the same recursive CTE serves both
  the landing page and the per-subsystem variant. Wildcard `F:`
  globs (`fs/*/file.c`-style) are skipped silently; they're a
  small minority of MAINTAINERS rules. Active-reviewers cross-
  ref (the originally-scoped slice 3 of #72) is split out as
  issue #97 since it requires indexing
  `Reviewed-by:` / `Acked-by:` / `Tested-by:` trailers (parser +
  schema change of its own).
- **Hunk-anchored quote rendering** on reply bodies (closes
  issue #68 slice 1). When a first-level `>` quote in a message
  body contains a diff hunk (the patch-review "let me quote this
  chunk of your patch" pattern), the renderer wraps the quote in
  a `<details class="hunk-quote">` so the wall of quoted diff
  doesn't bury the inline commentary. When the parent message is
  in this archive, the `<summary>` carries a "↗ jump to hunk"
  link pointing at it (resolved from `article.thread_parent` via
  the thread's URL map). First-level non-diff quotes stay as
  plain `<blockquote>` so the immediate context of a reply stays
  visible by default; deeper quotes still collapse via the
  existing depth-based rule.
- **Long-thread sidebar layout** on the message page (closes
  issue #68 slice 2). Threads at or above 20 messages render the
  thread tree as a sticky right rail (22rem wide) on viewports
  >= 60rem, with the message body and asides flowing in the main
  column to its left. The above-body box stays the default for
  short threads and on narrow viewports. Tree-on-rail matches
  the canonical desktop mail-client layout (mutt / Thunderbird /
  Discourse) and stops the height-capped box from paginating
  most of a long tree out of view. The fold toggle and
  active-marker scaffolding stay put; the rail's inner box caps
  to viewport height and scrolls inside its own bounds.

## [1.16.0], 2026-05-14

MINOR release adding vim-style keyboard navigation. Single-feature
release on top of the 1.15.x patch chain.

### Added

- **Vim-style keyboard navigation** (issue #70). `h` (back out
  one level), `j` (next message in thread), `k` (previous
  message in thread), `l` (drill in, placeholder that becomes
  useful on list surfaces in a follow-up), `Esc` (close help /
  fold thread tree), `?` (toggle keyboard-help overlay).
  Shortcuts bypass when focus is in an `<input>`, `<textarea>`,
  `<select>`, or `[contenteditable]` element; modifier keys
  (Ctrl/Cmd/Alt) pass through to the browser. `j`/`k` drive the
  existing intra-thread HTMX swap so the thread tree's active
  marker tracks the rendered message without a full page reload.
  Help overlay opens via `?` and closes with `Esc` or backdrop
  click; rendered on every page so the binding is universally
  available.

## [1.15.4], 2026-05-14

PATCH on top of 1.15.3 fixing an HTMX-versus-cache collision on
the message endpoint. The endpoint returns one of two responses
keyed only on the `HX-Request` request header: the full page on
plain navigation, or just the `_message_body.html` partial on
HTMX intra-thread swaps. Without `Vary: HX-Request`, any
intermediary cache (Cloudflare, browser bfcache, Chrome prerender
cache for sites with speculation rules) keyed both responses by
URL alone and could serve either to either request type. Two
visible failure modes were observed on the production deploy:

- A direct navigation rendering just the `<article id="msg">`
  with no surrounding chrome (the cached HTMX partial served to a
  full-page request).
- An HTMX click duplicating the entire page chrome into `#msg`
  (the cached full page served to an HTMX swap request, whose
  `<html>`/`<body>` wrappers the parser strips while keeping
  their `<header>` / `<main>` / `<footer>` children).

### Fixed

- `web.message` now emits `Vary: HX-Request` on every response.
  Caches key partial vs full responses separately, so the wrong
  shape can't be served to either request type. Other endpoints
  don't vary by `HX-Request` and don't carry the header.

## [1.15.3], 2026-05-14

PATCH on top of 1.15.2 fixing a second mainline-walker crash
hit on the production `update-mainline` run. The 1.15.1 fix
covered within-commit duplicate `Link:` trailers; this one
covers the cross-commit case where the same
`(commit_sha, message_id)` pair reaches the insert batch twice
(observed on linux.git, batch starting at commit
`6cd249cfad68`). Symptom is identical (`UNIQUE constraint
failed: mainline_commits.commit_sha, mainline_commits.message_id`);
root cause is in the walker, not the trailer parser.

### Fixed

- `mainline.walk_commits` now inserts via SQLite's
  `INSERT ... ON CONFLICT DO NOTHING` on the
  `(commit_sha, message_id)` UNIQUE, so duplicate observations
  are silently ignored at the DB layer. Three independent paths
  could produce a duplicate: (1) two `Link:` URL variants of one
  Message-ID within one commit (already deduped at the extract
  layer in 1.15.1, kept as defence in depth); (2) dulwich's
  `reverse=True` walker re-emitting the same commit when the
  graph has merge commits; (3) a cursor-missing rewalk
  re-recording rows from a prior successful run. All three
  failure modes are now handled by the same conflict clause.
- A new test (`test_walk_commits_full_rewalk_is_idempotent_via_on_conflict`)
  exercises the rewalk-over-populated-table path so a regression
  would surface in CI.

## [1.15.2], 2026-05-14

PATCH on top of 1.15.1 declaring `/data/Mainline` as a canonical
state subdir in the Dockerfile. No code or schema change; the
running container behaves identically once the directory exists
(which `git clone --bare` would have ensured anyway). The fix
makes the image self-consistent for fresh deploys where the
`/data` volume doesn't pre-exist with operator-created subdirs.

### Fixed

- Dockerfile now creates `/data/Mainline` at build time and
  symlinks `/app/Mainline → /data/Mainline`, matching the
  Inboxes pattern. Without this, `chown -R mimir:mimir /data`
  didn't pre-stage the Mainline subdir, leaving it
  root-owned-on-first-clone on hosts where the volume mount
  was empty at container start. The 1.15.0 first run on
  Coruscant got past it because `git clone --bare` creates the
  parent path, but the image is now self-consistent without
  relying on that side effect.

## [1.15.1], 2026-05-14

PATCH on top of 1.15.0 fixing a mainline-walker crash hit on the
first production `update-mainline` run against linux.git. No
schema, no config, no behaviour change for the happy path; the
walker now survives commits that carry the same Message-ID under
multiple `Link:` URL forms.

### Fixed

- `mainline.extract_message_ids` now dedupes Message-IDs within a
  single commit's `Link:` trailers. Real commits occasionally
  carry the same Message-ID under both the `/r/` and `/all/` lore
  URL forms, or duplicate a `Link:` trailer in a stable
  cherry-pick. Without dedup those collide on the
  `(commit_sha, message_id)` UNIQUE constraint and abort the
  insert batch (observed against linux.git commit
  `9e8e8912b05f` on the 1.15.0 first run). On the next
  `update-mainline` invocation after upgrading, the walker
  resumes from the cursor it had reached before the crash and
  continues cleanly.

## [1.15.0], 2026-05-14

MINOR release with five Tier-1 feature additions, three of them
mimir-specific differentiators against lore.kernel.org:
MAINTAINERS-driven subsystem awareness on patch pages, mainline
correlation (`Link:`-trailer indexing of Linus's tree),
patch-series version timelines on cover letters. Plus a
fenced-code highlighter for non-diff code blocks and a
date-range "what I missed" view.

### Added

- **MAINTAINERS-driven subsystem awareness** (issue #67). Patch
  pages now show subsystem ownership inside the article header
  alongside From / Date: `Subsystem: BCACHEFS · Maintainer: Kent
  Overstreet`. Below the body, a collapsible "Other recent
  patches touching these files" block surfaces up to 5 recent
  articles sharing any touched path. Ingest extracts the
  `b/<path>` from every `diff --git` header into a new
  `article_files` join table; `update-mainline` mirrors Linus's
  `linux.git` locally (configurable via `MAINLINE_TREE_PATH`)
  and parses MAINTAINERS into `subsystems` / `subsystem_paths` /
  `subsystem_maintainers` tables. `--skip-fetch` / `--force` /
  `--skip-maintainers` / `--skip-commits` flags for partial
  passes. `backfill-article-files` CLI fills `article_files` on
  existing deployments (idempotent, newest-first).
- **Mainline correlation** (issue #66). `update-mainline` now
  also walks every commit on the configured tree, extracts
  `Link: https://lore.kernel.org/.../<msgid>` trailers, and
  records them in a new `mainline_commits` table. Patch pages
  whose Message-ID matches a recorded commit surface "Applied
  as `<sha>` in the `<tree>` tree on YYYY-MM-DD" as a
  prominent left-bordered aside above the body. This is the
  user-visible payoff that closes the lore-archive →
  mainline-tree loop. First-run on a fresh deploy walks the full
  ~1.5M-commit Linus history; subsequent ticks resume via a
  second cursor on `MainlineState` and only walk new commits.
- **Patch-series cover-letter timeline** (issue #65 slice 1).
  Cover-letter subjects (`[PATCH ... 0/N] <title>`) are detected
  at ingest and tagged with `patch_series_key` +
  `patch_series_version` columns on Article. Cover-letter pages
  with ≥2 revisions render `Series revisions: v1 (date) → v2
  (date) → **v3**`, each prior revision linked to its own page.
  Series identity is SHA-1 of `(normalised-title, author-address)`
  so a query or log line can't leak the author's email through
  the key. Individual `1/N`+ patches don't attach in this slice
  (subject churn between revisions is a separate heuristic
  problem). `backfill-patch-series` CLI fills the columns on
  existing deployments. Cheap: subject + author only, no body
  re-parse.
- **Fenced-code-block syntax highlighting** (issue #69). Markdown
  triple-backtick fences in patch bodies (common in cover
  letters and design discussions) are now Pygments-highlighted
  with the language from the fence info string. `\`\`\`c` /
  `\`\`\`python` / `\`\`\`bash` use the matching lexer; a bare
  `\`\`\`` defaults to C (kernel-list context); unknown info
  strings fall back to TextLexer rather than crash. Detection is
  fence-anchored (no indent-based heuristics) so prose with
  code-shaped tokens doesn't get false-positive highlighting.
  Fences inside quoted blocks keep the quote structure. The
  Pygments stylesheet was extended with token classes for
  keyword / function / builtin / string / number / comment /
  preprocessor / operator, `light-dark()`-paired for both themes.
- **"What I missed" date-range view** at `/<inbox>/since/<YYYY-MM-DD>`
  (issue #73). Lists every thread with activity from the given
  date to now, ordered by last activity desc. Window clamps to
  90 days below the present so a "since 2010" URL doesn't drag
  a multi-year recursive CTE walk into a synchronous request;
  the template surfaces a notice when the requested date falls
  before the cap. Reuses the active-threads CTE infrastructure
  via a new `threads_since` helper. Cached 10 minutes per
  `(inbox, since)`.

## [1.14.1], 2026-05-14

PATCH on top of 1.14.0 surfacing successful IndexNow pushes in
default scheduler output. UX-only; no schema, no config, no wire
contract change.

### Changed

- `update` now prints `indexnow: pushed N URL(s)` at default
  verbosity on every successful push, mirroring the existing per-
  epoch state-change lines. Previously the only IndexNow signal
  in default scheduler output was failure paths (cap exceeded,
  network error); the success line was an INFO log suppressed at
  default verbosity, so "is IndexNow actually working?" needed
  `-v` to answer. The per-chunk INFO log inside `notify` stays
  put for operators who want the full status detail.

## [1.14.0], 2026-05-14

MINOR release adding off-by-default IndexNow push-notification
support. The `update` scheduler tick now notifies Bing/Yandex/
Naver/Seznam/Yep of newly-ingested article URLs when an operator
sets `INDEXNOW_KEY`. Google does not consume IndexNow, so this
won't accelerate Google discovery (separate JSON-LD fixes in 1.13
.4 handled the Search Console findings).

### Added

- IndexNow (https://www.indexnow.org/) push-notification support.
  Off by default; set `INDEXNOW_KEY` to enable. When enabled, the
  `update` scheduler tick POSTs the canonical URLs of newly-
  ingested articles to `api.indexnow.org`, which fans out to Bing,
  Yandex, Naver, Seznam, and Yep. Google does not consume
  IndexNow. Pre-existing articles are not backfilled to the
  protocol; only articles newly created per tick are pushed.
  `INDEXNOW_MAX_PER_TICK` (default 1000) is a backfill guard:
  ticks that produce more new URLs than this skip the push and
  log a warning, leaving the sitemap as the discovery path for the
  backlog. The ownership-verification file is served at
  `/<key>.txt` (route only registered when the key is set; an
  unconfigured deploy doesn't expose it). All network calls are
  best-effort: failures log and never break the ingest tick.
  Operator setup: see "IndexNow" section in README.

## [1.13.4], 2026-05-14

PATCH on top of 1.13.3 fixing two `DiscussionForumPosting`
structured-data findings Google Search Console flagged against
ratatoskr.run on 2026-05-14. No schema or behaviour change for
human readers; the JSON-LD blob on message pages grows a `text`
snippet and an `author.url`.

### Fixed

- `DiscussionForumPosting` JSON-LD on message pages now carries a
  `text` snippet derived from the parsed body (truncated at the
  last whitespace inside 2000 chars) and an `author.url` pointing
  at the per-inbox author view. Both were flagged by Google Search
  Console on 2026-05-14: missing `text` blocked Discussions
  rich-result eligibility, missing `author.url` was a non-critical
  field. Empty or whitespace-only bodies still omit `text`, and
  fallback "unknown sender" authors omit `url`, so the structured
  data validator stays clean across the corpus.
- Body-text snippet emitted into JSON-LD passes through the same
  DCO trailer redaction the visible HTML applies (new
  `rendering.redact_trailer_addresses` plaintext helper), so
  non-allowlisted `Signed-off-by:` addresses don't leak through
  structured data even though they're redacted on the rendered
  page.

## [1.13.3], 2026-05-13

Dev/CI-only PATCH on top of 1.13.2. No user-visible behaviour
change; the runtime artifact is byte-identical apart from the
footer version string. Bumped to lock in the post-audit suite
state and refresh the GitHub Actions toolchain ahead of the
Node.js 20 deprecation deadline.

### Changed

- GitHub Actions refreshed to Node.js-24-capable majors ahead of
  the 2026-06-02 forced-migration date (`actions/checkout@v6`,
  `actions/setup-python@v6`, `actions/cache@v5`,
  `docker/setup-buildx-action@v4`, `docker/login-action@v4`,
  `docker/metadata-action@v6`, `docker/build-push-action@v7`). No
  workflow behaviour change; the run-output annotations about
  Node.js 20 are gone.
- Test suite hardened against the 2026-05-13 audit findings.
  Net +23 tests (511 → 534): real-path cache round-trips for
  every registered dataclass, SQLite PRAGMA verification (WAL /
  foreign_keys / busy_timeout / synchronous / per-connection
  application), multi-worker ingest (pins `parse_message`
  picklability), `KEPT_HEADERS` filter assertion, surrogate-
  escape range path, `replay_failures` cross-post branch,
  `_INBOX_NAMES` republish after CRUD, URL 4-tuple identity
  (`/<inbox>/<YYYY>/<MM>/<id>` 404s on mismatch, never 301),
  recursive-CTE cycle termination (replaces a tautology),
  out-of-order thread arrival, `init-db` end-to-end,
  `admin failures replay` happy path, pickle-pivot regression
  marker. Five LOW polish items reshaped in place so a regression
  would actually trip them.

## [1.13.2], 2026-05-13

Security PATCH addressing a stored XSS in DCO-trailer rendering
surfaced by a test-suite audit, plus a defense-in-depth strip of
control bytes from the Content-Disposition header on attachment
downloads. No schema or behaviour changes for benign inputs;
deployable as a drop-in replacement on 1.13.x.

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

## [1.13.1], 2026-05-13

Polish PATCH on top of 1.13.0, addressing the four nits the
2026-05-13 launch-approval review flagged on the search and
author pages. No behavior changes; pure SEO / a11y / consistency.

### Added

- `SearchResultsPage` JSON-LD on `/<inbox>/search` when the route is
  rendering actual results (skipped on no-query / too-short / zero-
  results forms, those are bare search boxes, not results pages).
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

- `<h2>` on the search and author pages promoted to `<h1>`, both
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
  the atom link with `%40`, same target, two encodings.

## [1.13.0], 2026-05-12

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
  rendered as `David Woodhouse <hidden>` in feed readers, same
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
  (favicon, og-image, sitemap, etc.) are unaffected, they're
  applied by the `bp_web` after-request hook, which doesn't see
  `/static/*` traffic. 1-day TTL trades a small bandwidth saving
  for fast deploy-cycle propagation: a JS bug fix lands within 24
  hours rather than the week the routed-asset entries use.
- `<html lang="en">` now renders as a clean single-line tag on every
  route. The pre-fix shape left a stray indented `>` on its own line
  in view-source on every non-message page (the `html_data_attrs`
  block was always empty there). Cosmetic.

## [1.12.3], 2026-05-11

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

## [1.12.2], 2026-05-11

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

## [1.12.1], 2026-05-11

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

## [1.12.0], 2026-05-11

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
  partial, and the client swaps it in place, leaving the tree,
  navigation, and scroll position intact. The active marker in the
  tree follows the new message via class-toggling on
  `htmx:afterSwap`; the URL updates via `hx-push-url` so back/forward
  and share-the-URL still work. Falls back to a full page load when
  JS is off.

## [1.11.0], 2026-05-11

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
- `warm-cache` also refreshes the atom-feed data sources  
  `recent_articles(limit=50)` per inbox (drives
  `/<inbox>/feed.atom`) and `author_recent(..., limit=50)` per
  tracked author (drives `/<inbox>/author/<sub>/feed.atom`).
  Different cache keys from the dashboard's `limit=5/10` flavours,
  so feed polls had been paying the cold compute first-per-hour.

## [1.10.0], 2026-05-11

### Changed

- `/sitemap.xml` is now a sitemap index (`<sitemapindex>`) listing
  one sub-sitemap per inbox plus `/meta-sitemap.xml`, replacing the
  previous single monolithic `<urlset>`. Crawlers fetch sub-sitemaps
  independently and can skip unchanged inboxes via the per-entry
  `<lastmod>`. Each per-inbox sitemap (`/<inbox>/sitemap.xml`) lists
  the dashboard, year and month archives that actually have data,
  and the inbox's 5000 most-recent article URLs. Cross-posted
  articles appear in each linked inbox's sitemap, the canonical
  `<link>` on the page itself remains the deduplication signal.

### Added

- `/meta-sitemap.xml`, one-URL sub-sitemap covering `/`. Lives
  behind the sitemap index so the index can stay pure
  `<sitemapindex>` per the sitemaps.org schema.
- `/<inbox>/sitemap.xml`, per-inbox sub-sitemap. Cached per inbox,
  so an ingest into one inbox doesn't invalidate cached sitemaps
  for the others.

## [1.9.0], 2026-05-11

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
  `/favicon.svg` route, squirrel-adjacent placeholder until a
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
- `display_name` Jinja filter, display-name-only From line, no
  `<hidden>` placeholder. Used in `<meta name="description">`
  on message pages so search snippets and link cards don't carry
  the redaction placeholder as literal text.
- `clean_subject` Jinja filter, collapses RFC 5322 header-folding
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
  addresses on these lines as `[off-list ref]`, confusingly close
  to broken metadata for DCO chain verification. Allowlisted
  senders surface verbatim; everyone else is replaced with the
  explicit `<redacted>` placeholder.

## [1.8.1], 2026-05-09

### Changed

- Dependency refresh. Notable: dulwich `0.21` → `1.x` major bump
  (our API surface is `Repo`, indexing, `commit.tree`,
  `commit.commit_time`, `repo.head`, `repo.get_walker`, all
  unchanged across the boundary); ruff `0.1` → `0.15`. Within-
  constraint patch/minor updates: markupsafe `2`→`3`, click,
  jinja2, pydantic{,-core,-settings}, urllib3, python-dotenv.

## [1.8.0], 2026-05-08

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

## [1.7.2], 2026-05-08

### Fixed

- `cve@kernel.org`, `gregkh@kernel.org`, and other bare-`kernel.org`
  personal addresses no longer match the "list-shaped address"
  filter. Bare `kernel.org` was in `LIST_HOST_SUFFIXES`, which
  surfaced personal addresses as off-list-parent hints and skewed
  per-inbox address-observation tallies. List traffic lives on the
  subdomains (`vger.kernel.org`, `lists.linux.dev`); those entries
  are unchanged.

## [1.7.1], 2026-05-08

### Fixed

- Off-list-parent hint tooltip now renders below the trigger
  (`data-placement="bottom"`) so it escapes the `.thread-box`
  overflow clip. Default top placement was clipped by the box's
  top edge on the first-row trigger, making the hint unreadable.

## [1.7.0], 2026-05-08

### Added

- Off-list-parent rows in the thread tree now expose a hover
  tooltip listing list-shaped To/Cc addresses on the message that
  don't match any configured inbox. Quick cue for which mailing
  list the operator might want to add to recover the missing
  parent; the line stays compact, the address only appears on
  hover/focus.

## [1.6.1], 2026-05-08

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

## [1.6.0], 2026-05-07

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

## [1.5.1], 2026-05-07

### Changed

- Thread list on the message page is now scroll-contained
  (max-height clamped to the smaller of 50vh and 24rem, with a
  border + padding so it reads as a widget). Long threads no
  longer push the message body off-screen; the `»` marker still
  points to the active message inside the scrollable box. Threads
  with more than 12 messages get a pure-CSS expand/collapse
  toggle in the heading to drop the height cap when needed.

## [1.5.0], 2026-05-07

### Added

- Per-page `<meta name="description">` summarising the page  
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

## [1.4.1], 2026-05-07

### Fixed

- Ingest no longer crashes mid-batch when a message's RFC 5322 `Date`
  header carries `-0000` (which `email.utils.parsedate_to_datetime`
  returns as a tz-naive datetime). The Phase 1 observation tally
  used `max(prev_ts, parsed.date)` and raised `TypeError: can't
  compare offset-naive and offset-aware datetimes` the moment a
  `-0000` message landed in the same batch as a tz-aware one,
  rolling back the entire batch, which is why a fresh lkml ingest
  walked all 20 epochs but persisted only 26 articles. Now
  normalised to aware UTC at the entry point, in both `ingest_epoch`
  and `backfill_canonicals`.

## [1.4.0], 2026-05-06

### Changed

- **Schema migration ownership moves entirely to the scheduler
  sidecar.** The web container's `CMD` no longer runs
  `alembic upgrade head`, only `mimir-tasks` does, before its
  loop starts. Single source of DDL truth, no race between two
  parallel `alembic upgrade head` invocations on cold start. The
  example `compose.yaml` now flips `depends_on` so `mimir-web`
  waits on `mimir-tasks` with `condition: service_healthy`; the
  sidecar reports healthy after touching `/data/.migrated`, so a
  fresh volume bootstraps cleanly without gunicorn ever serving
  against an unmigrated DB. systemd deployments are unaffected  
  `mimir.service` still has its own `ExecStartPre=alembic`.

### Added

- **SEO Phase 3: render-side canonical surface.** Each cross-posted
  article is now served at one URL as far as search engines and feed
  readers are concerned:
  - Message pages emit `<link rel="canonical" href="...">` pointing
    at the canonical inbox's URL (`/<canonical-inbox>/YYYY/MM/<id>`).
    Falls back to the alphabetically-first linked inbox when
    `canonical_inbox_id` is NULL, stable across renders so the SEO
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

## [1.3.0], 2026-05-06

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
  moved, the registry only carried `:main` and `:sha-*` from main
  pushes.

## [1.2.0], 2026-05-06

### Added

- `PINNED_INBOXES` setting (default `["lkml"]`). Inboxes listed here
  surface at the top of the meta-index `/` in config order, with the
  rest following alphabetically. Comma-separated as an env var. Set
  to empty for pure alphabetical.

## [1.1.1], 2026-05-06

### Added

- `TRUSTED_PROXY_HOPS` setting (default `0`). When `> 0`, mimir wraps
  its WSGI app in Werkzeug's `ProxyFix` so `request.remote_addr`,
  `.scheme`, and `.host` reflect the real client through that many
  trusted reverse-proxy hops, fixing the access log showing the
  proxy's address instead of the client's. Off by default because
  enabling it on a directly-exposed app would let anyone spoof those
  values via a forged `X-Forwarded-For`. `compose.yaml` ships
  `TRUSTED_PROXY_HOPS=1` (replacing the earlier `FORWARDED_ALLOW_IPS`,
  which only handled scheme detection, gunicorn doesn't rewrite
  `REMOTE_ADDR` on its own). systemd deployments are unaffected; set
  the env var if you stack a reverse proxy in front.

### Fixed

- Structured access log now records the actual `User-Agent` header.
  Previously every request logged `"ua": null` because the code
  guarded on `request.user_agent`, whose `__bool__` depends on
  Werkzeug's UA parser recognising a known browser, non-browser
  values like `curl/8.20.0` (and, in this Werkzeug, even Firefox)
  evaluated falsy. Now reads the raw header directly.

## [1.1.0], 2026-05-06

### Added

- Auto-ANALYZE at the tail of `ingest_inbox`: when a run lands at least
  `ANALYZE_AFTER_INGEST_ROWS` (default `10000`) new + cross-post-linked
  messages, refresh the SQLite query-planner stats. Catches the
  freshly-added-inbox bootstrap case where the planner stats from the
  post-migration empty-table ANALYZE go stale once millions of rows
  land. Set to `0` to disable.

## [1.0.0], 2026-05-06

First production release. Live at <https://ratatoskr.run> serving
linux-fsdevel and lkml.

### Added

- Persist parse failures: every commit whose `m` blob can't be parsed
  during ingest lands in a new `parse_failures` table keyed by
  `(inbox, epoch, commit_sha)` with `error_class`, `error_message`,
  `first_seen`, `last_attempt`, `attempts`. Cleared automatically when
  a re-walk parses the commit cleanly.
- `flask --app mimir admin failures list`, enumerate persisted failures,
  filter by `--inbox` / `--epoch` / `--error-class`.
- `flask --app mimir admin failures replay <inbox>`, re-fetch each
  failure's blob, re-run the parser, insert the article (or cross-post
  link) on success, bump `attempts` on continued failure. Use after a
  parser fix.
- Scheduled-tasks sidecar: `deploy/scheduler.sh` shipped in the image
  at `/app/scheduler.sh`. Runs `warm-cache`, `update`, `analyze`,
  `vacuum` on env-tunable cadences (`WARM_CACHE_EVERY`,
  `UPDATE_EVERY`, `ANALYZE_EVERY`, `VACUUM_EVERY`, seconds).
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
