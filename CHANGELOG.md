# Changelog

All notable user-facing changes to mimir.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe behaviour, schema, config, and CLI/route shape
changes, not internal refactors. Categories: **Added**,
**Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**.

## [Unreleased]

### Fixed

- **`mimir.mainline` Repo lifecycle: three bare-assignment Repo
  opens now use `with Repo(...) as repo:`.** `walk_commits` wraps
  its body in `with`; `load_maintainers` wraps just the MAINTAINERS
  read (releasing the Repo before the SQL writes); the
  `update_mainline` linus-head probe is extracted into a private
  `_read_linus_head` helper that owns the `with` and the
  `try/except`. dulwich's `__exit__` now fires deterministically at
  function exit instead of relying on GC to invoke `__del__`.
  Addresses the walker-state retention signal from the v3.1.0
  tracemalloc investigation (`dulwich/walk.py:208/225/240` showed
  2 persistent walker states across all observation windows).
  See issue #461.

## [3.1.1] - 2026-06-11

### Fixed

- **Broker no longer accumulates dead reader threads in
  `_BrokerServer._reader_threads`.** Each reader thread now self-
  removes from the list in `_reader_loop`'s `finally:` block, so
  the list size tracks live connections instead of growing
  monotonically with connection churn over the broker's lifetime.
  Modest memory impact (~200-500 B per leaked Thread shell), but
  the list growing without bound was an obvious latent footgun.
  Diagnosed via the 3.1.0 tracemalloc instrumentation on prod
  2026-06-11; see issue #460.

## [3.1.0] - 2026-06-11

### Added

- **`mimir tracemalloc-diff` CLI + broker tracemalloc snapshotter.**
  New env-gated diagnostic for the broker:
  `TRACEMALLOC_INTERVAL_SECONDS` (default unset = off) enables a
  daemon thread that writes `tracemalloc` snapshots to
  `/data/diagnostics/tracemalloc-<ISO>.pkl` every N seconds, and
  logs a top-25-by-current-bytes summary to stderr. Pair with
  `mimir tracemalloc-diff <a.pkl> <b.pkl> [--top N]
  [--filter-prefix /app/mimir]` to rank growers offline. Zero
  cost when disabled. Intended to identify the Python-side
  retention that mimalloc didn't move (per
  `_claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md`).

### Changed

- **CI image-tag emission aligned with sibling projects.** The
  `docker-publish` workflow now emits a literal `v$X.Y.Z` tag in
  addition to the unprefixed `$X.Y.Z` and `latest` tags on tag
  pushes, matching the johnny / johnny-callback shape. Operators
  pinning to either form continue to resolve; no compose change
  needed.

## [3.0.2] - 2026-06-09

### Changed

- **Build tooling: Poetry replaced with uv across pyproject,
  lockfile, Dockerfile, CI, and docs.** `pyproject.toml` uses
  PEP 735 `[dependency-groups]` for dev deps and the `hatchling`
  build backend; `poetry.lock` is gone and `uv.lock` is the new
  source of truth. The `builder` stage in the Dockerfile copies
  `uv` from `ghcr.io/astral-sh/uv:latest` and pins the project
  venv via `UV_PROJECT_ENVIRONMENT=/app/.venv`; the PBS 3.14t
  base stage and the runtime stage are structurally unchanged.
  The three CI jobs (`lint`, `test`, `test-ft`) install
  dependencies via `astral-sh/setup-uv@v7` with the lockfile-
  keyed cache; the free-threaded job additionally pins
  `python-version: '3.14t'` on setup-uv so uv honours the
  freethreaded interpreter that setup-python provisions rather
  than downloading its own (GIL-enabled) default. README,
  deploy/README, and the dev scripts swap every `poetry run X`
  invocation to `uv run X`, including the operator cron
  snippets. No runtime behaviour change; image contents and
  CLI surface are identical.
- **CI test-ft job: GIL assertion moved before `uv sync`.**
  `uv run --no-project --no-sync python -c "..."` runs the
  free-threaded interpreter check immediately after setup-uv,
  so a misconfigured Python (the class of regression fixed by
  the `python-version: '3.14t'` pin) fails the job in seconds
  rather than after wheel install. Cosmetic CI hygiene; no
  effect on what the job actually verifies.

### Added

- **`.github/dependabot.yml`**, matching the johnny + johnny-
  callback sibling shape now that uv is in place. Three
  ecosystems on a weekly Monday cadence: `github-actions`
  (grouped), `uv` (grouped minor+patch vs major; reads
  `pyproject.toml` + `uv.lock` together), and `docker` (tracks
  the `debian:trixie-slim` FROM line). The Astral
  python-build-standalone tarball is `ARG`-pinned and stays
  out of scope for the docker scanner; PBS bumps remain
  manual until a custom updater becomes worth wiring up.

## [3.0.1] - 2026-06-08

### Changed

- **`compose.yaml`: `PYTHON_GIL=0` on mimir-tasks.** Suppresses
  the RuntimeWarning that fired on every Flask CLI subprocess
  spawned by `scheduler.sh` (~one per tick) when SQLAlchemy's
  cyextension import re-enabled the GIL under Python 3.14t.
  Cosmetic log-hygiene only; tasks is single-thread (RPC-shell),
  so free-threading itself buys nothing here. Broker already had
  the same setting since 2.14.1 where it actually pays off.
- **broker: replace glibc malloc with mimalloc.** Installs
  `libmimalloc3` in the runtime image and `LD_PRELOAD`s it on
  the `mimir-broker` service via compose, with
  `MIMALLOC_PURGE_DELAY=200` to release freed segments back to
  the kernel promptly after a warm cycle. glibc's default
  `arena_max = 8 * nproc` (128 arenas on a 16-core host)
  fragments under free-threaded Python's many-threads contention
  and only opportunistically returns memory to the kernel.
  mimalloc's segment-based design uses `madvise(MADV_DONTNEED)`
  proactively. Free-threaded CPython 3.14 already uses a
  vendored mimalloc internally for Python object allocation;
  this puts C-extension allocations (dulwich, OpenSSL, libuv,
  etc.) onto the same allocator. Broker only; app and tasks stay
  on glibc malloc. Issue #447; spec at
  `_claude/specs/2026-06-08-broker-mimalloc-allocator-design.md`.

## [3.0.0] - 2026-06-03

### Breaking changes

- **Wire-protocol bump**: every broker Request and Reply now
  carries a required `rpc_id: int` field. The BrokerClient
  pipelines RPCs over one socket: multiple caller threads can
  have requests in flight concurrently, and a daemon demux
  thread resolves replies by `rpc_id`. Mixed-version broker /
  web / tasks containers fail with `pydantic.ValidationError`
  on the first message. Atomic compose deploy required; the
  standard `podman compose up -d` flow handles this correctly
  via `depends_on`.

  The previous CLI-side `_rpc_lock` (a single per-process lock
  held across every send-plus-receive round-trip) and the
  retry-once-across-reconnect logic are removed. Callers that
  need retry handle it at their layer: `mimir.cache.cache_set`
  logs at warning and swallows `BrokerUnavailable`; long-op CLI
  wrappers raise `ClickException`.

### Changed

- `mimir warm-cache --tier slow` wall time drops from O(263 s) to
  a fraction of that on production-scale corpora; the 8 broker
  warm workers can now actually run concurrently when the slow
  tier fans out per-(inbox, subsystem) RPCs.

### Internal

- Broker per-connection `_send_lock` serialises concurrent
  worker replies on the same client connection at the byte
  level (microsecond hold).
- New `_BrokerRequest` base class in `mimir.broker.protocol`
  carries `rpc_id`; every concrete Request inherits from it.
- New daemon demux thread inside `BrokerClient`; per-RPC
  timeout lives on the caller's `Future` (no socket-level
  read timeout).

## [2.19.0], 2026-06-01

### Changed

- **Slow tier fans out per-(inbox, subsystem) warm RPCs.**
  `mimir warm-cache --tier slow` no longer serializes the
  20-subsystem dashboard sweep inside a single warm_inbox
  worker thread. The CLI pre-computes top-N subsystems per
  inbox and dispatches one `warm_subsystem` RPC per (inbox,
  subsystem) pair via the broker's warm queue. With 8 warm
  workers, hotspot inboxes (linux-arm-kernel,
  linux-devicetree, linux-doc) drop from ~100 s slow-tier wall
  time to ~14 s. Same total compute; just moved from per-thread
  serialization to per-RPC parallelism. New protocol op:
  `warm_subsystem` (`WarmSubsystemRequest`).

### Fixed

- **Scheduler dual-timer: slow tier no longer blocks fast tier
  ticks, and no longer fires on cold boot.** Two related
  scheduler.sh changes:
  - The in-loop slow-tier dispatch (`mimir warm-cache --tier
    slow`) is now backgrounded with `&`, so the scheduler loop
    continues firing fast-tier ticks while the slow tier runs.
    A `kill -0 "$slow_pid"` check guards against overlapping
    cycles when the corpus grows past `WARM_CACHE_SLOW_EVERY`.
    Production observation 2026-06-01: with the slow tier
    taking 30 to 50 min per cycle, fast-tier ticks were stalled
    for the whole window, so sitemap rows could expire (TTL
    4200 s) without being refreshed.
  - `/data/.last_warm_slow` is stamped at boot if missing, so
    the slow tier waits a full `WARM_CACHE_SLOW_EVERY` before
    its first fire after a fresh deploy. Spec §Risk #3
    explicitly said slow tier shouldn't fire on cold boot
    (subsystem dashboards being cold for the first hour is
    acceptable), but missing-sentinel-reads-as-0 made the
    first tick fire slow immediately.

### Added

- **Config-drift guards: broker startup WARNING, warm handler
  WARNING, and `mimir doctor` CLI.** Three layered safety nets
  for env-var misconfig:
  - Broker logs a WARNING at startup when `SITE_BASE_URL` is
    unset, naming the affected feature (sitemap warming).
  - Warm handlers log a once-per-process WARNING when sitemap
    labels arrive in `req.targets` but the broker's own
    `SITE_BASE_URL` is unset, identifying the dropped labels.
  - New `mimir doctor` subcommand prints a structured config-
    health report (or `--json`) with per-check ok/warning/
    error status. Operators run it pre-deploy or when
    investigating drift. Exit code 0 (all ok) / 2 (warnings
    only) / 1 (any error).

  Driven by the 2026-06-01 production incident where
  `SITE_BASE_URL` set on `mimir-tasks` and `mimir-app` but
  missing on `mimir-broker` silently neutered the warm-cache
  sitemap targets in the broker handler. 2.18.0's
  conditional-GET semantics still worked for the boot warm
  but the cached sitemap rows were never refreshed past the
  boot-time warm's TTL, opening a cold-compute cliff
  ~70 min after deploy.

## [2.18.0], 2026-06-01

### Added

- **`BROKER_CACHE_WORKERS` and `BROKER_LONG_WORKERS` env knobs.**
  The broker's cache and long queues each had a hardcoded
  single-worker drain; both are now operator-tunable, mirroring
  the existing `BROKER_WARM_WORKERS` shape. Defaults stay at 1
  (preserves FIFO commit order per submitting client on the
  cache queue; matches the WriterThread funnel on the long
  queue), so steady-state behaviour is unchanged. Bumping
  `BROKER_CACHE_WORKERS` parallelises the cache dispatch step
  for workloads where the single worker is the funnel rather
  than the WriterThread; safe for mimir's cache.set (idempotent
  on key). Bumping `BROKER_LONG_WORKERS` lets independent long
  ops overlap their pre-write compute phases (batch building,
  read fan-outs); the actual SQLite writes still funnel through
  the WriterThread. Tests:
  `test_start_workers_honors_per_queue_settings`,
  `test_start_workers_single_worker_keeps_bare_label`.

### Changed

- **Warm-cache split into fast and slow tiers with priority-queue
  routing.** `mimir warm-cache` gains a `--tier {fast,slow,all}`
  flag; the scheduler invokes fast on the per-minute cadence
  (covering sitemaps + archive_stats + latest_pull_requests +
  latest_stable_releases + recent_articles) and slow on a
  per-hour cadence (covering subsystem dashboards + per-tracker
  queries + the rest). The broker's `warm_queue` migrates from
  `queue.Queue` to `queue.PriorityQueue`: fast-tier RPCs
  (priority=0) dequeue before slow-tier RPCs (priority=1) already
  queued, but in-flight slow ops are not preempted. New
  scheduler env vars: `WARM_CACHE_SLOW_EVERY` (default 3600 s).
  `WARM_CACHE_EVERY` keeps its meaning (drives the fast tier)
  and remains an alias for the new `WARM_CACHE_FAST_EVERY`.
  Pydantic settings: `warm_cache_fast_every` (default 60),
  `warm_cache_slow_every` (default 3600),
  `warm_cache_fast_refresh_window_sec` (default 600),
  `warm_cache_slow_refresh_window_sec` (default 7200).

- **Warm-cache cache rows store extended TTL with probabilistic
  refresh window.** Warm-managed rows now store `TTL = nominal +
  window_sec` (e.g. a sitemap row stores 4200 s rather than the
  nominal 3600 s). `mimir.cache.get_or_compute` learns a
  probabilistic refresh decision via the new
  `mimir.cache_warm.should_refresh` helper: for `remaining > 2 ×
  window` the row is served from cache (skip); for
  `window < remaining ≤ 2 × window` the warm-tick refreshes with
  probability ramping 0 → 1 across the band (spreads load so 204
  sibling keys don't all warm on the same tick); for
  `remaining ≤ window` the warm-tick refreshes deterministically
  every time (the insurance zone, so the cache row is always
  overwritten before it expires). Average cached `Last-Modified`
  staleness on sitemaps goes up by roughly 5 minutes; cold-miss
  windows on the read path effectively disappear. Spec:
  `_claude/specs/2026-06-01-warm-cache-fast-slow-tier-split-design.md`.

## [2.17.0], 2026-06-01

### Fixed

- **Sitemap routes now carry `Last-Modified` and honour
  `If-Modified-Since`.** `/sitemap.xml`, `/meta-sitemap.xml`, and
  `/<inbox>/sitemap.xml` each set a `Last-Modified` header derived
  from the sitemap's most-recent content date (per-inbox latest for
  the per-inbox sitemap; global latest for the index + meta), and
  return `304 Not Modified` on a conditional GET whose
  `If-Modified-Since` covers that date. Without these headers,
  Google had no cheap way to know the sitemap had changed and
  deprioritised re-fetching, so a fresh ingest could sit
  un-reindexed for hours. The cached payload now carries
  `(body, last_modified)` together via a new `SitemapPayload`
  dataclass so the body and the header date stay in sync across
  cache reads. Test coverage:
  `test_sitemap_xml_carries_last_modified_header`,
  `test_meta_sitemap_xml_carries_last_modified_header`,
  `test_inbox_sitemap_xml_carries_last_modified_header`,
  `test_sitemap_xml_returns_304_when_if_modified_since_matches`,
  `test_inbox_sitemap_xml_returns_304_when_if_modified_since_matches`,
  `test_sitemap_xml_returns_200_when_if_modified_since_is_older`.

- **WriterThread engine now inherits the shared `_sqlite_pragmas`
  registration.** `mimir/broker/writes.py::WriterThread._run`
  creates its own engine via `create_engine(self._database_url)`;
  until this fix, that engine had no `connect`-event listener
  attached, so every PRAGMA the shared engine sets up
  (`foreign_keys=ON`, `synchronous=NORMAL`,
  `analysis_limit=4000`, `busy_timeout`) was missing on writer
  connections. Surfaced during Phase 5 when `delete_inbox`'s
  `DELETE FROM inboxes` quietly stopped cascading to
  `article_lists` and `ingest_state` (FK CASCADE relies on
  `PRAGMA foreign_keys=ON`); a per-closure workaround in
  `delete_inbox` was the temporary fix. This change attaches
  `mimir.extensions._sqlite_pragmas` as a `connect` listener on
  the writer engine and removes the per-closure workaround.
  Latent perf wins on commit fsync (synchronous=NORMAL vs FULL)
  and ANALYZE lock-hold (`analysis_limit=4000` vs unbounded)
  also apply now. Test: `test_writer_thread_engine_has_sqlite_pragmas_set`.

### Changed

- **Broker two-pool restructure, Phase 5 (admin ops migration).**
  The 12 broker admin RPC handlers (3 inbox CRUD, 4 tracker
  mutators, 4 robots CRUD, 1 failures replay) no longer write
  inline via the service layer's `SessionLocal()` (and
  `write_transaction()` for the four robots functions). Each
  service-layer function in `mimir/inboxes.py`, `mimir/robots.py`,
  and `mimir/ingest/replay.py::replay_failures` now dispatches
  via the active `WriterThread` when a broker context is set;
  falls back to the legacy `SessionLocal()` path for non-broker
  callers (tests, `bootstrap_inboxes`, dev scripts). The
  `_INBOX_NAMES` nav-cache refresh moves outside the writer
  closure (via the existing `refresh_inbox_names()` helper after
  `future.result()`) because the closure receives a Connection
  while `_publish_names` needs a Session. The two-call-chain
  functions (`add_tracked_author`, `remove_tracked_author`) fold
  into a single closure on the writer path. After Phase 5 only
  the broker's periodic purge timer thread still writes outside
  the WriterThread (Phase 6 cleanup). Same RPC contract, same
  `Reply.result` shapes, no env-var or CLI surface change.

## [2.16.0], 2026-05-31

### Changed

- **Revisions fold links non-current version labels.** On a patch
  message page with multiple revisions, each prior revision's
  version label (`v2`, `v3`, ...) in the Revisions fold is now a
  link to that revision's article page. The current revision's
  label stays plain text (no self-link). The data carrier
  (`StateSeriesEntry`) already exposed the per-revision URL via
  its `url` field; this release wires the template to use it.
  The `[diff vs current]` chip beside each prior revision is
  unchanged. The fix also includes a small CSS override so the
  linked variant of `.rev-label` keeps the primary-text weight
  and size, rather than inheriting the dimmed/shrunk treatment
  the generic `.revisions-timeline a` rule applies to the diff
  chip.

## [2.15.1], 2026-05-31

### Fixed

- **Broker cache handlers swallow `TimeoutError` gracefully.** The
  four Phase 4 broker cache handlers
  (`handle_cache_set`, `handle_cache_delete`,
  `handle_cache_delete_for_inbox`, `handle_cache_purge_expired`)
  now catch `TimeoutError` from `.result(timeout=5)` and return
  `Reply(ok=False, error="writer busy")` with a single WARNING
  log line, instead of letting it bubble up to `dispatch`'s outer
  exception handler (which logged each failure as an ERROR with
  full traceback). Same client-visible outcome as before: the web
  tier treats `ok=False` as a transient cache miss and the cached
  value ages out via TTL.
  
  Surfaced on the production 2.15.0 deploy when the first
  successful VACUUM cycle since 2026-05-23 (enabled by 2.14.1's
  `SQLITE_TMPDIR` fix) held the writer lock for 127 seconds and
  produced ~30 `ERROR broker: handler crashed on cache_set`
  traceback lines as cache RPCs from the web tier timed out. The
  failure mode (cache writes degraded during VACUUM) is the
  documented "VACUUM holds the writer lock; accept that" posture
  from CONTEXT.md; the fix is purely log-noise reduction. Pre-Phase-4
  the same window raised `OperationalError("database is locked")`
  which dispatch already handled gracefully as a WARNING.

## [2.15.0], 2026-05-31

### Changed

- **Broker two-pool restructure, Phase 4 (cache handlers
  migration).** The four broker cache RPC handlers
  (`handle_cache_set`, `handle_cache_delete`,
  `handle_cache_delete_for_inbox`, `handle_cache_purge_expired`)
  no longer run `cache._direct_*(...)` inline on the cache worker
  thread wrapped in `write_transaction("broker:cache_*")`. Each
  becomes a thin shim that dispatches a single-statement WriteOp
  through the active WriterThread via
  `cache.<op>_via_writer(writer, ...).result()`. Four new helpers
  in `mimir/cache.py` (`_set_via_writer_for_nskey`,
  `delete_via_writer`, `delete_for_inbox_via_writer`,
  `purge_expired_via_writer`) parallel the `_direct_*` family's
  pre-prepared-inputs signatures. The cache worker thread keeps
  running but the actual SQLite write lands on the WriterThread;
  after Phase 4 every SQLite write inside the broker funnels
  through one WriterThread. Reply shapes preserved exactly: no
  RPC contract change. Supporting change in
  `mimir/broker/writes.py`: `_run_one` now propagates the
  closure's return value into the WriteFuture so handlers can
  return rowcount via `.result()` (backward-compatible; existing
  closures returning `None` continue to behave identically). No
  env-var or CLI surface change.

## [2.14.1], 2026-05-31

### Fixed

- **`compose.yaml`: `SQLITE_TMPDIR=/data/db` on the broker.** SQLite
  VACUUM writes a full temp copy of the live DB during the operation.
  Default temp location is `/tmp`, which inside the broker container
  lives on the overlayfs storage pool (bounded by remaining headroom).
  On the production deploy where mimir.db is ~14 GB and the storage
  pool had ~11 GB free, VACUUM failed with `SQLITE_FULL` (`sqlite3.
  OperationalError: database or disk is full`) and the scheduled
  weekly VACUUM hadn't completed since 2026-05-23. Routing temp to
  `/data/db` (the 92 TB pool where the live DB also lives) eliminates
  the constraint. Broker-only since the broker is the sole VACUUM
  caller.
- **`compose.yaml`: `PYTHON_GIL=0` on the broker.** CPython 3.14t
  auto-re-enables the GIL when any C extension that hasn't declared
  `Py_GIL_DISABLED` is imported. SQLAlchemy's
  `cyextension.collections` is one such module, so the broker (which
  imports SQLAlchemy at startup) was running with the GIL ON despite
  being on the free-threading build, defeating the multi-core
  parallelism story Phases 2/3 depend on. `PYTHON_GIL=0` suppresses
  the re-enable; the import-time RuntimeWarning still fires but the
  runtime stays GIL-free. Broker-only: it is the multi-thread process
  (cache + long + 4 warm workers) where free-threading pays off.

## [2.14.0], 2026-05-30

### Changed

- **Broker two-pool restructure, Phase 3b (`ingest_inbox` long-op
  migration).** `ingest_inbox()` + `ingest_epoch()` no longer hold
  `write_transaction("ingest_inbox:<name>")` for one continuous
  epoch-long window. Per-batch work splits: read/compute phase runs
  on a `query_only` session from the active `ReadSessionPool` and
  builds a `_PendingWrites` accumulator; at each
  `INGEST_BATCH_FLUSH_SECONDS` interval (new env var, default 0.5 s)
  the accumulator is submitted as one composite WriteOp via
  `_submit_ingest_batch` and the walker awaits `.result()` before
  composing the next batch. The Article INSERTs use `RETURNING id`
  so ArticleList / ArticleFile / ArticleTrailer rows land their
  FKs in the same closure. `promote_list_address` and
  `auto_analyze` migrate as small single-statement WriteOps too.
  Resume semantics preserved: the `IngestState.last_commit_sha`
  cursor advance is the FINAL statement of each batch's closure,
  so a crash between two batches leaves the cursor at the prior
  batch and the next tick re-walks from there. Idempotency via
  `articles.message_id` UNIQUE + `article_lists` composite PK on
  `(article_id, inbox_id, epoch, commit_sha)`. Writer-lock hold per
  batch drops from one continuous transaction per epoch (tens of
  seconds on backlogged ingests) to N short bursts (~tens of ms
  each), so concurrent `cache.set` RPCs from the web tier drain
  between batches instead of head-of-line stalling. Phase 3b is
  narrowed to ingest; the four backfills are deferred to Phase 3c.
  New env var: `INGEST_BATCH_FLUSH_SECONDS` (float, default 0.5).
- **`cache.set` writer-path gate.** The in-process WriterThread
  shortcut (Phase 2, 2.11.0) is now gated on
  `_broker_handler_active()` (the thread-local flag the broker's
  worker loops set on entry, clear on exit) rather than on the
  global active-context registration. Production behaviour
  unchanged (broker workers always set the flag, so warm + ingest
  handlers still dispatch via the writer); in-process test setups
  with a session-scoped active context no longer take the writer
  path from test threads. No operator-visible change.
- **Broker two-pool restructure, Phase 3c (the four patch-metadata
  backfills migration).** `backfill_article_files`,
  `backfill_article_trailers`, `backfill_patch_series`, and
  `backfill_canonicals` no longer hold `write_transaction(label)` for
  the full walk. Per-batch work splits: read/compute phase runs on a
  `query_only` session from the active `ReadSessionPool` and
  accumulates per-article pending-writes payloads (`_ArticleFilesPending`
  / `_ArticleTrailersPending` / `_PatchSeriesPending` /
  `_CanonicalPending` in the new `mimir/_pending_backfill.py`); at each
  batch boundary one composite `WriteOp` is submitted through the
  active `WriterThread` and awaited via `.result()` before composing
  the next batch. The three patch-metadata backfills share the
  restructured `mimir/_backfill.py::walk_articles` shell (now takes a
  `flush_batch(writer, payloads)` callable); `backfill_canonicals`
  keeps its own walk because of the interleaved periodic
  `_maybe_promote_list_address` sweep (now its own WriteOp via
  `_submit_promote_list_address_sweep`). Per-batch writer-lock hold
  drops from one continuous transaction per backfill walk (minutes
  on `--reprocess` runs of the full corpus) to N short bursts (~tens
  of ms each); concurrent `cache.set` RPCs drain between batches.
  Idempotency is by predicate (`WHERE NOT EXISTS ...`,
  `WHERE canonical_inbox_id IS NULL`, `WHERE patch_series_position
  IS NULL`), same shape as today. No env-var or CLI surface changes;
  the existing `BROKER_BACKFILL_CHUNK_SECONDS` cooperative-scheduling
  knob keeps its semantics.

## [2.12.0], 2026-05-29

### Changed

- **`deploy/scheduler.sh` no longer blocks the loop on the initial
  warm-cache.** The pre-flight `warm-cache` now runs in the
  background so the periodic loop is responsive from t=0; the
  separate synchronous `update (initial)` block is gone too, the
  loop's first tick fires update immediately because its sentinel
  doesn't exist yet (`now - 0 >= UPDATE_EVERY`). Previously, on a
  container recreate with a populated `/data` volume, the
  synchronous initial warm could run for hours and serialised
  every other loop tick behind it: inbox `update`, `update-mainline`,
  `analyze`, and `vacuum` all sat idle until warm-cache returned.
  This matches the actual compose dependency chain (web depends
  only on the broker's healthcheck, not on tasks), so cold-cache
  exposure for first-wave requests is unchanged. Affects
  containerised deploys; systemd timers in `deploy/systemd/` are
  unaffected.
- **Broker two-pool restructure, Phase 3 (`update_mainline` long-op
  migration).** `update_mainline()`'s per-tree walk no longer holds
  `write_transaction()` for one continuous ~62 s window. The walker
  reads on a `query_only` session from the active `ReadSessionPool`
  and dispatches batched WriteOps through the active `WriterThread`:
  each batch of `MAINLINE_COMMIT_BATCH_SIZE` rows (new env var,
  default 100) goes through `_submit_mainline_batch` and is awaited
  before composing the next; the per-tree cursor
  (`MainlineState.commits_walked_to_sha`) is the FINAL WriteOp via
  `_submit_mainline_cursor_update`; trees configured with
  `rebases=True` also dispatch their pre-walk DELETE as its own
  WriteOp. Resume-from-cursor semantics are preserved: a crash
  between batch N and the cursor submit leaves the cursor at its
  old position so the next tick re-walks from there, and
  `on_conflict_do_nothing` on `mainline_commits` makes that replay
  a no-op for batches that did commit pre-crash. Writer-lock hold
  per batch drops to tens of ms, so concurrent `cache.set` RPCs
  from the web tier drain between bursts instead of head-of-line
  stalling for the full walk. Phase 3 is narrowed to
  `update_mainline()`; ingest + backfills are deferred to a
  later Phase 3b. `load_maintainers()` and the per-tree
  `last_walked_at` cadence write continue to use their original
  paths (out of Phase 3 scope). New env var:
  `MAINLINE_COMMIT_BATCH_SIZE` (int, default 100).

## [2.11.0], 2026-05-30

### Changed

- **Broker two-pool restructure, Phase 2 (warm-cache migration).**
  The warm handlers (`handle_warm_inbox`, `handle_warm_global`)
  now check their read session out of the active
  `ReadSessionPool` (added in 2.10.0). `cache.set()` calls issued
  by warm-target helpers dispatch through the active
  `WriterThread` via the new `cache.set_via_writer()` variant;
  calls from outside the broker (web tier, tests without an
  active broker) still run inline as before. The active broker
  is registered in a small `mimir/broker/_context.py` module by
  `serve()` at startup. With CPython 3.14t free-threading, this
  means up to N read-pool threads can do warm compute work on
  separate cores instead of serialising on the writer lock; the
  observable signal is `broker_cpu` averaging higher (more cores
  in use) while the 100+ s warm-cycle outliers should compress
  toward the writer-lock-bounded floor. Phase 3 (long-ops) and
  Phase 4 (web-tier `cache.set` RPC) still pending. No RPC
  contract change in this release.

## [2.10.0], 2026-05-29

### Changed

- **Broker two-pool restructure, Phase 1 (infrastructure).** The
  broker now constructs a `ReadSessionPool` (query_only=1
  SQLAlchemy sessions, sized by `BROKER_READ_POOL_SIZE`,
  default `os.cpu_count()`) and a `WriterThread` (single-thread
  actor with a bounded queue sized by `BROKER_WRITER_QUEUE_DEPTH`,
  default 256, that commits one `WriteOp` per BEGIN IMMEDIATE
  transaction). No handler is migrated yet, so steady-state
  behaviour is unchanged; the new primitives sit parallel to the
  existing single-writer path. Phases 2 to 6 migrate one write
  surface per release (warm-cache first, then long-ops, web-tier
  `cache.set`, admin ops, cleanup). The motivation is to let the
  broker actually use multiple cores under CPython 3.14t
  free-threading; see `_claude/MEMORY.md` 2026-05-29 for the
  observation that 3.14t alone did not move `broker_cpu` off its
  single-core pin, and
  `_claude/specs/2026-05-29-broker-two-pool-design.md` for the
  full rollout.

## [2.9.0], 2026-05-29

### Changed

- **Container image now runs CPython 3.14t (free-threaded,
  PEP 703 / PEP 779)** so the broker's cache + long-op +
  4-worker warm pool can use multiple cores in parallel
  rather than timesharing on one under the GIL. PEP 779
  lifted free-threading from "experimental" to "supported"
  in 3.14, so this is no longer experimental from CPython's
  side. Observable via `podman exec mimir-broker python -c
  "import sys; print(sys._is_gil_enabled())"` returning
  `False`.

  Sourced from Astral's `python-build-standalone` release
  `20260510` (x86_64-unknown-linux-gnu, install_only) because
  the official `python:3.14t-slim` image doesn't exist on
  Docker Hub yet (PEP 779 is supported in CPython core, but
  docker-library/python is a separate maintenance track that
  hasn't shipped a free-threaded variant).
  python-build-standalone is the runtime uv ships and
  conda-forge consumes; pinned via the `PBS_RELEASE`,
  `PYTHON_VERSION`, and `PBS_ARCH` Dockerfile build args for
  reproducibility.

  Per-thread perf is ~5-15 % slower vs the GIL build, which
  matters less for mimir's broker (multi-thread-CPU-bound,
  pinned single-core pre-migration per `_claude/MEMORY.md`
  2026-05-29) than the multi-core win. CI's new `test-ft`
  job runs pytest under 3.14t alongside the regular 3.14
  `test` job to guard against free-threading-unsafe code at
  PR time. Applies to all three container roles (web,
  broker, tasks) since they share the same image.

## [2.8.1], 2026-05-29

### Fixed

- Revert the subsystem-dashboard EXISTS-shape rewrite shipped
  in [2.8.0]. The new plan regressed warm-cycle cost on inboxes
  the original PR did not measure (observed in production:
  lkml subsystem dashboards 351 s, netdev 107 s,
  linux-arm-kernel 117 s, against a pre-fix worst case of
  ~25 s). `ANALYZE` shifted cost estimates but did not
  eliminate the bad plan because the EXISTS-driver-on-
  `ix_articles_date` shape walks too many candidate rows on
  broad-`F:`-rule subsystems (linux-arm-kernel covers
  `arch/arm/`, `arch/arm64/`, plus a wall of devicetree
  bindings). `cache_set` timeouts remained zero throughout,
  so user-facing impact was nil; the regression was broker-
  internal CPU + warm cycle wall time. The tini fix from
  [2.8.0] is preserved. Reverts 344ae0d.

  Re-attempting the warm-cycle optimisation will require
  measuring against the worst-case inboxes
  (linux-arm-kernel, netdev, dri-devel, kvm, linux-arm-msm)
  rather than only the ones that showed improvement in
  isolation; the original PR's measurements covered lkml,
  linuxppc-dev, linux-tegra, and linux-trace-kernel, none of
  which exhibited the regression pattern.

## [2.8.0], 2026-05-29

### Changed

- **Subsystem-dashboard warm-cycle cost cut 3-7x by rewriting
  three path-filter helpers to the EXISTS shape.** The pre-fix
  shape (`a.id IN (UNION-of-seeks)`) materialised the entire
  archive's subsystem-paths article-id set before intersecting
  with the inbox+date slice, dominating warm-cycle cold misses
  on every medium-traffic inbox. On NETWORKING [GENERAL]
  (1500+ MAINTAINERS rules) that materialisation alone was
  ~1-4 s per call, depending on helper. The rewrite walks
  `ix_articles_date` DESC in the date window and tests inbox +
  path EXISTS per row, so the planner caps work at the
  in-window candidate set instead. Affects:

  1. `recent_articles_in_subsystem` — driver of the per-
     subsystem "Recent patches" panel.
  2. `daily_volume_in_subsystem` — driver of the per-
     subsystem 30-day sparkline.
  3. `active_reviewers_in_subsystem` — driver of the per-
     subsystem "Active reviewers" list (also feeds the
     deduped per-reviewer page warm targets).

  Plans pinned via three sibling tests modelled on
  `test_subsystem_path_filter_uses_index_seeks` /
  `test_triage_queries_use_date_index_no_full_scans`. Combined
  projected cold-cycle impact per the production measurements
  on 2026-05-29: lkml 24.7 s → 3.4 s (7.3x), linux-trace-kernel
  11.4 s → 3.6 s (3.2x), linuxppc-dev 11.2 s → 3.8 s (2.9x).

  Sibling helpers `active_threads_in_subsystem` (recursive-CTE
  shape, different optimisation surface) and the two triage
  queues (`needs_attention_patches_in_subsystem` /
  `quiet_patches_in_subsystem`, already EXISTS-shaped per
  #209 but with a separate flat-baseline issue) are out of
  scope for this PR.

  `SUBSYSTEM_RECENT_MAX_AGE_DAYS` env-tunable (default 180),
  same semantics + shape as `RECENT_PATCHES_MAX_AGE_DAYS` and
  `SUBSYSTEM_TRIAGE_MAX_AGE_DAYS`. Caps the worst-case date-walk
  on `recent_articles_in_subsystem` for sparse subsystems where
  the LIMIT cutoff doesn't trigger; without a bound, a top-N
  subsystem with a handful of historical articles in scope
  would walk the date index back to the dawn of lkml.

### Fixed

- Container image now installs `tini` and uses it as the
  ENTRYPOINT so PID 1 reaps orphaned grandchildren of
  `git fetch` / `git clone` (`git-remote-https` and friends).
  Without an init shim the broker runs as PID 1 and never
  reaps these orphans, accumulating `[git]` zombies for the
  life of the container (observed 2026-05-29: ~30 zombies
  after 35 h uptime on the production broker, clustered in
  groups of 6-7 per `update_mainline` tick matching
  `Settings.trees`). tini is chosen over a `SIGCHLD = SIG_IGN`
  in-process fix because the latter would also defeat
  `subprocess.run(..., check=True)`'s ability to detect git
  failures (`waitpid()` for any direct child returns ECHILD
  under SIG_IGN on Linux, which CPython converts to
  returncode=0 silently). The fix applies to all three
  container roles (web, broker, tasks) since they share the
  same image.

## [2.7.0], 2026-05-28

### Changed

- Footer layout: the Privacy link moves to the right-hand side of
  the footer, separated from the version + source link on the
  left. Same destination (`/privacy`), same per-page placement,
  just visually distinguished from the navigation pair so the
  GDPR notice reads as a footer affordance rather than another
  source link.

## [2.6.0], 2026-05-27

### Changed

- Reviewer-name dedup in the lifecycle pill tooltip. A single
  maintainer filing one `Reviewed-by:` per patch in a multi-patch
  series previously rendered as one tooltip line per trailer (real
  prod case: `REVIEWED: 11 (11M)` showed the same name 11 times).
  First occurrence wins per case-insensitive name; if the same name
  appears as both maintainer and non-maintainer the maintainer
  variant carries the `M ` prefix. Pill counts (`N (XM)`) are
  unchanged: the count reflects the trailer total, the tooltip
  reflects unique people.
- Activity-heat on the message-page badge is now sourced from the
  same SQL recursive CTE that powers listing-row badges (single
  source of truth in `lifecycle_status._BULK_SQL`). `PatchState`
  no longer carries duplicate `activity_heat` / `activity_detail`
  fields. Cache `NAMESPACE_VERSION` bumped 2 → 3 to invalidate
  pre-bump `PatchState` rows so deserialisation can't trip on the
  removed fields; brief cache cold-start window on the next deploy,
  warm-cache cron repopulates on its next tick.

## [2.5.0], 2026-05-27

### Changed

- Patch-page header redesign. The patch-state aside is dissolved
  into two top-row badges (activity heat chip + lifecycle pill
  with inline review counts). The "Lifecycle" timeline section
  is removed; the same per-tree data lives in the lifecycle
  pill's tooltip (commit hash + timestamp on top line, reviewer
  names with `M ` prefix on maintainers one-per-line below).
  The patch-revisions surface moves to a foldable Revisions
  timeline below the From block, summary showing the count.
  Listing rows switch to the same badge primitive; height +
  typography unify across activity and lifecycle badges so the
  row reads as a cohesive metadata pair instead of two competing
  widgets. The trailers line drops; counts live in the pill's
  `: N (XM)` suffix, per-reviewer names in the tooltip.

## [2.4.1], 2026-05-26

### Fixed

- Broker now runs `alembic upgrade head` on every startup instead
  of skipping when `.migrated` exists. Pre-2.4.1, the sentinel acted
  as a "ran once, skip forever" gate, so any release that introduced
  a new alembic revision silently swallowed its migration on the
  upgrade of a long-lived deploy. 2.4.0 surfaced this in production:
  `mimir update-mainline` raised `OperationalError: no such column:
  mainline_state.last_walked_at` because the new migration never
  applied. `alembic upgrade head` is idempotent and cheap when no
  migrations are pending; the sentinel survives only as a first-vs-
  subsequent-run marker for operator log reading.
- Broker `_migrate_if_needed` now constructs `alembic.config.Config`
  programmatically (script_location + sqlalchemy.url) instead of
  reading `alembic.ini`. The ini's `[loggers]` / `[handlers]` /
  `[formatters]` sections cause `logging.config.fileConfig()` to
  fire, which strips every handler off the root logger (including
  pytest's caplog and any operator-attached handlers). The
  programmatic Config bypasses this. Operator-visible: any log
  handlers attached before broker startup now survive the alembic
  call.

## [2.4.0], 2026-05-26

### Added

- Multi-tree mainline tracking. Patches are now tracked through
  subsystem `*-next` trees (`net-next`, `tip`, `pci`, `mm`,
  `bpf-next`), `linux-next`, and Linus's mainline. The `mm` tree
  walks the `mm-stable` branch. The patch-state
  card on message pages labels each landing with its tree; a new
  lifecycle timeline below the card renders the chronological
  journey (post → review trailers → tree pickups → mainline merge).
  Listing rows (recent / daily / inbox / search / subsystem /
  month / author / reviewer / since) carry a status pill:
  Landed / Superseded / Queued / Reviewed / Pending. Operators
  extend the tree set via `TREES__<name>__URL` env. First
  deploy may run a longer `update-mainline` tick as new trees
  clone (`--reference linus.git` keeps marginal disk small).

### Deprecated

- `MAINLINE_TREE_URL` / `MAINLINE_TREE_PATH`. Continue to work
  (auto-seed the `linus` entry); will be removed in the next
  major release. Operators should migrate to
  `TREES__linus__URL` / `TREES__linus__PATH`.

### Changed

- `env_nested_delimiter="__"` is now active on `Settings`, enabling
  `INBOXES__<name>__<field>` and `TREES__<name>__<field>` per-key env
  overrides for both dict settings. Operators with pre-existing
  `INBOXES__*` env var names (previously ignored) will now have
  pydantic-settings parse them; ANY `INBOXES__*` key replaces the
  default `inboxes` dict (no merging). Review env before upgrade.

- `mimir update-mainline` CLI now exits non-zero when any tree's
  walk fails (previously exited 0 with the error logged). Per-tree
  isolation preserved: the operation still attempts every tree;
  the exit code surfaces failures for systemd / cron alerting.

### Fixed

- `mimir-web` container healthcheck no longer shells out to `wget`.
  The base image is `python:3.x`-derived and does not ship `wget`,
  so every probe failed with `executable file not found in $PATH`
  and the container sat at `unhealthy` indefinitely despite gunicorn
  serving normally. Replaced with a `python -c
  urllib.request.urlopen` one-liner against the same `/healthz`
  endpoint; urlopen raises on non-2xx or network failure, mirroring
  `wget --tries=1 --spider` semantics.

## [2.3.0], 2026-05-25

Single operator-facing change: the `mimir-web` container's gunicorn
no longer emits its stock Apache-style access log line. The app's
own JSON access log (already richer; carries `request_id` +
`duration_ms`) becomes the sole per-request record, dropping the
duplicate entry that operators have been parsing around.

### Changed

- Web container / systemd unit no longer pass `--access-logfile -`
  to gunicorn. Every request was being logged twice: once as
  gunicorn's stock Apache-style line, once as the app's own JSON
  line emitted by `mimir.web.hooks._log_request`. The JSON form
  carries the same fields plus `request_id` and `duration_ms`, so
  the gunicorn line was strictly redundant. `--error-logfile -`
  stays, gunicorn-internal errors still surface.

## [2.2.0], 2026-05-25

Adds a GDPR transparency notice (`/privacy`) and fixes two small
renderer / date-window bugs the production deploy surfaced. Also
moves the Ratatoskr logo attribution out of the global footer onto
the landing-page image where the logo actually lives.

### Added

- `/privacy` GDPR Art. 13 transparency notice covering controller
  identity, browser storage (Cloudflare cookies, `mimir.fold.*`
  localStorage), server-side log retention, third parties in the
  request path, redaction posture, data-subject rights, and the
  Dutch supervisory-authority complaint route. Linked from the
  footer on every page.

### Fixed

- `daily_volume_in_subsystem` and `most_active_subsystems` now
  build their date window from `datetime.now(timezone.utc).date()`
  instead of `date.today()`. The local-TZ form silently dropped
  boundary-day articles (the SQL bucket-key landed in UTC while
  the zero-fill range was in local time) on any non-UTC container
  or dev machine. Production deploys default Docker's TZ to UTC so
  the bug was latent there, but it surfaced as a `pytest` failure
  in `test_daily_volume_in_subsystem_counts_matching_articles`
  during late-evening runs on West-Coast machines.
- Body renderer no longer pulls a trailing `---` scissors line into
  the diff block. `b4 send` puts a bare `---` between the last hunk
  and the `base-commit:` / `change-id:` trailers; the
  diff-continuation rule (line starts with " +-") used to swallow
  that line, which Pygments then rendered as a ghost
  deletion-of-`--` at the end of the patch. `parse_blocks` now
  strips a trailing `---` off each diff block (real hunk `---`
  delete-lines are always followed by more diff content, so the
  last-line check distinguishes the two cases safely).

### Changed

- The Ratatoskr logo attribution moved from a global footer line
  (visible on every page, including ones where the logo isn't
  rendered) into the landing-page `<img>`'s `alt` and `title`
  attributes. Hovering the image now reveals the credit, and screen
  readers + image-fallback both surface it where the image actually
  lives.

## [2.1.0], 2026-05-23

Adds an operator-diagnostic surface on the broker's warm path so a
slow `warm_inbox` / `warm_global` RPC can be attributed to a
specific helper without a separate `-v` repro.

### Added

- Broker warm handlers emit a top-5 per-target breakdown WARNING
  when `warm_inbox` / `warm_global` elapsed time crosses
  `broker_slow_rpc_warn_ms`, alongside a new `per_target` key on
  the reply listing every target's elapsed milliseconds (sorted
  desc). Pairs with the server's existing `broker slow rpc` line
  so a slow warm RPC in journalctl carries its own attribution.

## [2.0.1], 2026-05-23

Hotfix for a 2.0.0 cleanup miss: every scheduler `update` tick was
aborting before any inbox was fetched, so LKML (and every other
inbox) stopped updating the moment 2.0.0 deployed. Upgrade
immediately if you're on 2.0.0.

### Fixed

- CLI ingest paths no longer attempt a write on startup. `mimir
  update` / `ingest` / `reindex` / `show` previously called
  `bootstrap_inboxes()` defensively; the broker self-bootstraps
  inboxes on its startup since 2.0.0 and every other process opens
  `PRAGMA query_only=1`, so the defensive call raised
  `OperationalError: attempt to write a readonly database` and
  aborted every scheduler `update` tick before any inbox was
  fetched. The CLI now reads via `list_inboxes()` and the
  orchestrate fallback follows the same shape.

## [2.0.0], 2026-05-22

The broker becomes the sole SQLite writer process. The pre-2.0.0
additive scaffolding that allowed direct + broker paths to
coexist is gone; every other process opens connections with
`PRAGMA query_only=1` and dispatches writes through the broker
over a UNIX socket.

### Breaking changes

- **`Settings.read_only_db` removed.** The maintenance-toggle
  flag is permanently subsumed by `mimir_is_broker`: web + tasks
  containers are unconditionally read-only at the SQLite layer
  now. Operators relying on `READ_ONLY_DB=true` should drop the
  env var.
- **`Settings.mimir_role` removed**, replaced by
  `Settings.mimir_is_broker: bool` (env `MIMIR_IS_BROKER`). The
  previous three-valued role (`web`/`tasks`/`broker`) collapses
  to broker-vs-everyone-else. Set `MIMIR_IS_BROKER=true` on the
  broker container.
- **New `MIMIR_DEPLOY` env**, set `MIMIR_DEPLOY=true` on web +
  tasks + broker containers. Drives the FLASK_DEBUG refusal
  guard that previously keyed off `mimir_role`.
- **`broker_socket_path` default = `/data/.broker.sock`** (was
  `None`). Operators on non-standard layouts override via
  `BROKER_SOCKET_PATH`.
- **Per-CLI direct-path fallbacks removed.** Every CLI mutation
  (`bootstrap-inboxes`, `update`, three patch backfills +
  canonicals backfill, `analyze`, `vacuum`, `update-mainline`,
  `warm-cache`, all `admin inbox` / `admin robots` / `admin
  failures` subcommands) hard-fails with `ClickException` if
  the broker is unavailable. No silent fall-back to direct
  writes.
- **Tasks container is read-only.** `scheduler.sh` no longer
  runs `alembic upgrade head` or `bootstrap-inboxes` or the
  post-migrate `analyze`. The broker container self-bootstraps
  each (sentinel-gated at `/data/.migrated`, `.bootstrapped`,
  `.broker_initial_analyze`) before flipping its healthcheck.

### Migration notes for operators on 1.42.x

1. Drop `READ_ONLY_DB` from your compose / env.
2. Set `MIMIR_IS_BROKER=true` on the broker container.
3. Set `MIMIR_DEPLOY=true` on web + tasks + broker containers.
4. Leave `BROKER_SOCKET_PATH` unset to inherit the new
   `/data/.broker.sock` default.

Pull the new `compose.yaml` as a reference; the three-service
layout (broker + web + tasks) is now the canonical shape, with
`depends_on: mimir-broker (service_healthy)` on web + tasks.

### Rollback

Not supported. Once 2.0.0 is deployed, downgrade to a 1.x
release requires reverting the compose layout and re-enabling
the now-removed env knobs. The 1.x line remains available for
new deploys.

## [1.42.1], 2026-05-22

### Fixed

- **Warm-cache `articles_reviewed_by` fan-out recomputed every
  tick.** The reviewer-attestation helper was wired to
  `ACTIVE_THREADS_CACHE_TTL_SEC = 300 s` (5 min), shorter than
  the warm cycle's 450 s refresh window. Every warm tick
  recomputed the full per-reviewer fan-out for every inbox; on
  the production lkml corpus, that's ~140 reviewers × ~2.4 s per
  query ≈ 6 minutes of broker compute every minute, surfacing in
  the broker log as a `slow rpc [warm-N]` line with a multi-
  hundred-second `dispatch=` figure. Now uses
  `SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC = 3600 s` to match the rest
  of the per-subsystem dashboard helpers the same warm cycle
  drives through this code path. Reviewer pages don't change
  faster than once an hour in practice (a new review only lands
  when an article with that person's trailer is ingested), so the
  hour-long TTL is honest. Regression pinned by
  `test_articles_reviewed_by_caches_for_one_hour`.

## [1.42.0], 2026-05-22

### Added

- **TDM-reservation preamble on `/robots.txt`.** Patterned on
  Cloudflare's AI Crawl Control output but reframed for what a
  mailing-list mirror operator has standing to claim. The header
  comment block now: (a) acknowledges that copyright in individual
  messages belongs to their authors, (b) explains the meaning of
  the `Content-Signal` directives below, and (c) reserves the
  operator's rights in the compilation (index, deduplication,
  threading, cross-list resolution, rendering) under EU Directive
  96/9/EC on the legal protection of databases (the sui generis
  database right, distinct from Article 4 of Directive 2019/790
  which would presume operator ownership of the underlying
  content). Suppressed when no rule carries `Content-Signal` so a
  signal-less file doesn't carry a glossary describing directives
  that don't appear below.

## [1.41.1], 2026-05-22

### Changed

- **`/robots.txt` Cache-Control TTL: 24h → 5min.** The route was
  previously cached for one day at the edge, matching its
  pre-1.40.0 static-file character. Since 1.40.0 it's been
  operator-mutable at runtime via `admin robots`; a 24h edge
  cache made an `admin robots add GPTBot --disallow /`
  invisible to the world for up to a day. 300s matches the
  sitemap.xml TTL for the same reason (underlying state is
  mutable). Operators who already deployed 1.41.0 can purge
  their CDN cache once to get instant propagation; from 1.41.1
  forward, future mutations propagate within 5 minutes
  automatically.

## [1.41.0], 2026-05-22

### Added

- **Content Signals on `robots_rules`.** Operators can now express
  Cloudflare's proposed
  ([blog](https://blog.cloudflare.com/content-signals-policy/))
  `Content-Signal: search=yes, ai-train=no, ai-input=no` semantics
  per User-agent stanza. New JSON column on `robots_rules`; the
  migration backfills the seeded `*` row with
  `search=yes, ai-train=no, ai-input=no` (matching mimir's existing
  redaction-as-friction posture). CLI surface:
  `admin robots add <ua> --content-signal KEY=VALUE` and
  `admin robots update <ua> {--set-content-signal KEY=VALUE,
  --clear-content-signal KEY, --clear-all-content-signals}`. The
  rendered file emits `Content-Signal:` as a sibling line under
  `User-agent:`, before `Crawl-delay:`/`Disallow:`. The
  isitagentready.com `botAccessControl.contentSignals` check now
  passes.

### Fixed

- **`/robots.txt` + `/security.txt` Content-Type duplication.** The
  responses previously carried
  `content-type: text/plain; charset=utf-8; charset=utf-8` because
  the `Response(..., mimetype="text/plain; charset=utf-8")` call
  passed an already-charsetted mimetype that Flask's response
  builder then double-appended. Changed to
  `mimetype="text/plain"` on both routes so Flask appends its
  default charset exactly once.

## [1.40.0], 2026-05-22

### Added

- **Operator-managed `/robots.txt` via `admin robots …`.** The
  file is now rendered from a new `robots_rules` table on every
  request, so operators can add per-bot stanzas, tune Disallow
  paths, and adjust Crawl-delay without a redeploy. Six CLI
  commands (`list`, `show`, `add`, `update`, `remove`, `reset`)
  match the shape of `admin inbox`. Mutations route through the
  broker when `BROKER_SOCKET_PATH` is set; reads stay direct
  under `query_only=1`. The migration seeds the `*` stanza with
  the previous hardcoded values (`Crawl-delay: 5`,
  `Disallow: /*/attachment/`), so fresh deploys serve a
  byte-identical default. `remove '*'` is refused; use
  `reset --yes` to restore defaults if `*` has wandered.

## [1.39.0], 2026-05-21

### Added

- **Write-broker Phase 2.4**: the last admin write ops migrate
  through the broker when `BROKER_SOCKET_PATH` is set. Seven
  split RPCs cover `admin inbox` CRUD (`inbox_create`,
  `inbox_update`, `inbox_delete`, plus the four tracker
  mutators); one more covers `admin failures replay`. Direct
  fallback path preserved on every CLI command. After Phase 2.4
  every periodic + admin writer except the post-migrate ANALYZE
  is on the broker; the post-migrate ANALYZE moves below.
- **Broker self-bootstraps the post-migrate ANALYZE.** On first
  startup the broker checks `/data/.broker_initial_analyze`; if
  absent it runs a bounded `ANALYZE` (the existing
  `analysis_limit=4000` pragma) inline and touches the sentinel.
  Subsequent restarts skip the pass. The web tier's
  `depends_on: mimir-broker (service_healthy)` orders the chain
  so cold requests after a deploy never walk un-ANALYZE'd
  indexes. Replaces `scheduler.sh`'s direct
  `mimir analyze (post-migrate)` call, which was the last
  direct-write code path on the scheduler container.

### Changed

- `mimir/web/urls.py`: `_relative_time` and `_thread_summary`
  moved to `mimir/web/filters.py` (their natural home alongside
  `_relative_time_filter`). External callers see no change;
  `mimir.web` continues to re-export both.
- `scheduler.sh` no longer runs a direct `mimir analyze` after
  `alembic upgrade head`; the broker owns that pass now (see
  Added).
- `compose.yaml` example block for `mimir-broker` documents the
  new `start_period: 30s` healthcheck (covers the post-migrate
  ANALYZE on first cold deploy) and updated commentary on which
  ops the broker handles.
- `cache._should_dispatch_to_broker()` gains a thread-local
  "currently inside a broker handler" check on top of the
  existing `MIMIR_ROLE=broker` env check. Catches in-process
  test setups where broker thread and CLI invocations share
  `settings.mimir_role`; production behaviour unchanged.

### Fixed

- **`/api/<inbox>/recent?offset=` hard ceiling.** Caps offset at
  100 pages back (`RECENT_PAGE_SIZE * 100`); past it the route
  404s. SQLite's OFFSET walks the index N rows before returning
  anything, so an unbounded offset let a crawler at `?offset=5M`
  burn a gunicorn worker. Real readers stop scrolling long before
  the cap.
- **Message-view in-body Message-ID linkifier capped at 100
  distinct refs per render.** Past the cap, additional refs
  render as plain text rather than going through the bulk
  `.in_(...)` SELECT against `articles`. Bounds DB work + memory
  on a pathological body within the parser's body-size budget.
  Typical messages carry <10 refs; the cap is far above any real
  ask.

### Changed (perf)

- **`mainline.load_maintainers` switches to three bulk inserts**
  in place of the prior per-row ORM `session.add()` flush loop.
  The MAINTAINERS file expands to ~1.5k Subsystem + ~10k
  SubsystemPath + ~5k SubsystemMaintainer rows; under the old
  shape SQLAlchemy's unit-of-work flushed one INSERT per row at
  commit time, holding the writer lock for the full round-trip
  count. The bulk-INSERT-with-RETURNING idiom is `sqlite_insert`
  with `sort_by_parameter_order=True` for the parent table,
  then plain `insert(...)` with the returned ids wired in for
  the two child tables.
- **`subsystems.subsystems_for_article` reads from a cached rule
  snapshot.** The ~15k-row rule set + cascaded maintainers used
  to be pulled per message-view render; now it's fetched once,
  cached in the cross-process cache table with a 24 h TTL, and
  invalidated from `mainline.load_maintainers` after every
  MAINTAINERS reload (same pattern as `maintainer_allowlist`).
  Per-article query against `article_files` still hits the DB.

## [1.38.0], 2026-05-21

### Added

- **Write-broker Phase 2.3**: the periodic-maintenance writers
  (`mimir update-mainline`, `mimir analyze`, `mimir vacuum`) route
  through the broker when `BROKER_SOCKET_PATH` is set. Each CLI
  command checks the setting and either RPCs to the broker (which
  calls the same library function inside its single-writer
  process) or falls back to the direct-SQLite path. With Phase
  2.3 landed, every periodic writer except the post-migrate
  ANALYZE in `scheduler.sh` is on the broker; the 2.0.0 cleanup
  will close the last gap.
- New `BROKER_VACUUM` op emits a high-visibility WARNING at start
  so an operator correlating cache-write stalls against the
  broker log can tell "weekly maintenance, not a fault." VACUUM
  holds the SQLite exclusive lock for the duration; every other
  broker worker pauses.
- `LIST_HOST_SUFFIX_OVERRIDES` env var augments the default
  list-host suffix set used by canonical-inbox resolution. Comma-
  separated; the effective set is the union of the baseline and
  the overrides, so operators with archives for lists hosted on
  domains we don't ship as a default can add them without a patch.

### Changed

- **Landing page copy refresh**: the hero tagline + meta description
  + JSON-LD `description` for the meta-index `/` now name the
  distinctive surfaces concretely (cross-list deduplication,
  subsystem dashboards, patch-series timelines, reviewer activity)
  rather than describing the storage layer. Same SEO + link-card
  budget; better hook for first-time visitors.
- `TRUSTED_PROXY_HOPS` example in `compose.yaml` bumped from `"1"`
  to `"2"` (the Caddy-behind-Tailscale-Funnel shape the project
  actually targets); both shapes documented inline.
- Per-inbox author trackers are now managed via the `mimir admin
  inbox trackers` CLI surface; the README no longer documents a
  `TRACKED_AUTHORS` env var (the global env knob was removed in
  the multi-inbox refactor, the README hadn't been updated).
- `/m/<message-id>` is documented as a 301 (was 302 in the README;
  the actual code has long since been 301 to the article's
  canonical inbox URL).

### Fixed

- HSTS and `_site_base()` scheme detection now gate on
  `request.is_secure` rather than the raw `X-Forwarded-Proto`
  header. A directly-exposed mimir (`TRUSTED_PROXY_HOPS=0`) can
  no longer have HSTS pinned into a casual browser cache by a
  forged header; production deploys through ProxyFix are
  unchanged.
- `create_app()` refuses `flask_debug=True` when `MIMIR_ROLE` is
  one of `web`/`tasks`/`broker`. A workspace `.env` that
  accidentally lands in a container env can no longer leak
  verbose tracebacks.

### Security

- **Broker hardening**: socket file mode is now born `0660` via
  an `os.umask` dance around `bind()` (previously relied on a
  trailing `chmod` that races a local connector). Per-connection
  line buffer capped at 16 MiB; an idle peer is closed after 5
  minutes. On Linux, `SO_PEERCRED` is read on every accept and
  connections from a different euid are refused. The
  `cache_delete_for_inbox` request gains slug-regex validation
  at the wire boundary so a buggy or hostile peer can't smuggle
  LIKE metacharacters.
- **Parser / rendering caps**: `parse_message` rejects MIME
  trees with more than 256 leaf parts; `extract_touched_paths`
  caps at 1000 paths per body; oversized code / diff / attachment
  blocks fall back to `TextLexer` past 64 KiB to bound
  pathological-input Pygments cost; `extract_list_addresses`
  strips C0/C1 control bytes and surrogate-range codepoints, and
  caps per-address length at the RFC 5321 254-char path limit.
- **Cache write cap**: `cache.set` refuses payloads larger than
  8 MiB. Below the broker wire cap (16 MiB), well above real
  values today.
- **Attachment Content-Type allowlist**: download route coerces
  any Content-Type outside an allowlist of safe prefixes
  (text/*, image/*, application/pdf, message/rfc822, archive
  families, etc.) to `application/octet-stream`. Same for the
  ASCII-form `filename=` Content-Disposition fallback, clamped
  to 200 characters.
- **`sync.clone_epoch` re-validates the composed clone URL**
  through `validate_outbound_url` so a hostile manifest can't
  smuggle a non-https or RFC1918 destination through the
  origin-plus-key concatenation.
- **External links carry the full rel set**
  (`nofollow noopener noreferrer`) in linkify-rendered URLs.
- **CI actions SHA-pinned** so a moved tag in an upstream
  action publisher can't silently land a new ref on the next
  run. CodeQL coverage (python + actions + javascript) is
  provided by the repo's default-setup CodeQL configuration
  (already enabled before the sweep; no custom workflow needed).
- **Container/systemd hardening**: Dockerfile sets
  `GIT_TERMINAL_PROMPT=0` + `GIT_PROTOCOL_FROM_USER=0`.
  systemd unit gains `ExecStartPre=mimir bootstrap-inboxes` (a
  fresh systemd deploy no longer comes up with an empty
  `inboxes` table) plus `MemoryHigh=2G` and `LimitNOFILE=4096`
  resource ceilings.

### Notes

- The `tests/` tree still carries `--` double-dashes in
  prose-style docstrings. Tests don't flow into release notes,
  so the cleanup was deferred; flagged here for visibility.

## [1.37.0], 2026-05-21

### Added

- **Write-broker Phase 2.2**: the four backfill commands route
  through the broker as a chain of chunked RPCs when
  `BROKER_SOCKET_PATH` is set, with **cooperative scheduling**
  baked in. Each broker handler runs the underlying walker for at
  most `BROKER_BACKFILL_CHUNK_SECONDS` seconds (default 10),
  commits, and returns `partial=True, continuation=<last id>`;
  the CLI loops with a follow-up RPC until completion, summing
  per-chunk counters. Between two chunks the broker's long-op
  worker is free, queued cache writes and other long ops (a
  scheduler `update` tick) get serviced before the next chunk
  arrives. Multi-hour `backfill_canonicals --reprocess` runs no
  longer monopolise the long worker.

  Affected commands:
  `mimir backfill-article-files`,
  `mimir backfill-article-trailers`,
  `mimir backfill-patch-series`,
  `mimir admin canonicals backfill`.

  New protocol message types: `BackfillArticleFilesRequest`,
  `BackfillArticleTrailersRequest`, `BackfillPatchSeriesRequest`,
  `BackfillCanonicalsRequest` (each carrying `limit`,
  `reprocess`, `continuation`; canonicals also carries
  `inbox_filter`). `Reply.result` carries
  `{counters, partial, continuation}` for these ops. New matching
  methods on `BrokerClient` with default per-RPC timeout 3600 s.

  Direct (non-broker) path preserved as the fallback for deploys
  not yet in broker mode. When broker mode is on the CLI's `-v`
  flag becomes a one-line stderr hint pointing at the broker log
  (`podman logs -f mimir-broker`) since the walker is no longer
  running in the CLI's own process.

- **Write-broker Phase 2.2 (warm queue)**: `mimir warm-cache`
  dispatches through the broker as a fan-out of per-inbox
  `warm_inbox` RPCs plus one final `warm_global` when
  `BROKER_SOCKET_PATH` is set. The broker grows a **third queue**
  (`warm_queue`) drained by **N parallel warm-workers** (default
  4, env `BROKER_WARM_WORKERS`) sibling to the cache and long
  queues. Read-heavy warm computes parallelise across inboxes;
  cache.set commits still funnel through the SQLite writer lock
  but the upstream compute overlaps freely.

  New protocol types: `WarmInboxRequest(inbox_name, targets=None)`,
  `WarmGlobalRequest()`. `Reply.result` carries
  `{warmed, elapsed_ms, errors}`. Per-target exceptions are
  captured into `errors` rather than failing the whole RPC,
  mirroring `_warm_after_ingest`'s best-effort posture. New
  matching `BrokerClient.warm_inbox` + `warm_global` methods
  (default per-RPC timeout 300 s).

  `mimir/cli/cache.py` refactored: per-inbox + global target
  lists hoisted into `_build_inbox_targets(inbox, today,
  yesterday, sitemap_base)` and `_build_global_targets(sitemap_base)`
  so the broker handler and the legacy in-process CLI share one
  target list. Direct (non-broker) path preserved as the fallback
  for deploys not yet in broker mode.

- **`BROKER_WARM_WORKERS`** (default 4): number of broker warm-
  worker threads. The warm queue drains with this many concurrent
  workers, parallelising the read-heavy compute phase of warming
  across inboxes. Tune higher for bigger corpora; set to 1 for
  serial behaviour.

- **`BROKER_BACKFILL_CHUNK_SECONDS`** (default 10): per-chunk time
  budget for the Phase 2.2 backfill RPC handlers. Shorter dial
  yields finer interleaving with queued cache writes / ingest
  ticks at the cost of more RPC overhead; longer dial cuts
  overhead at the cost of longer pauses for queued cache writes.

### Changed

- **`backfill_canonicals` ordering**: switched from
  `Article.date DESC NULLS LAST` to `Article.id DESC` so the
  cooperative-scheduling continuation cursor is a single
  integer. Production ingest order is date-monotonic so the
  practical effect is negligible (id-desc ≈ date-desc); the
  former `nullslast()` bucketed NULL-date rows at the very end
  whereas they now fall in their natural id position. NULL-date
  rows are rare and carry no list-address signal anyway.

- **Walker API**: `mimir._backfill.walk_articles` gains
  `max_seconds` + `start_cursor` parameters and now returns
  `(partial, continuation)`. Direct callers that ignore the
  return value see the historical behaviour unchanged.

- **`BackfillResult` shapes**: each of the four backfill result
  classes (`patches.BackfillResult`, `trailers.BackfillResult`,
  `patch_series.BackfillResult`,
  `ingest.backfill.BackfillResult`) gains `partial: bool` and
  `continuation: int | None` fields plus a `merge(other)`
  helper that sums counter fields while carrying `other`'s
  partial/continuation forward. Powers the CLI's per-chunk
  aggregation; direct callers see `partial=False,
  continuation=None`.

- **Scheduler boot sequence**: `warm-cache (initial)` now runs
  before the `/data/.migrated` healthcheck sentinel is touched
  (was: after, between `update (initial)` and the loop). The web
  tier waits 30-60 s longer on every recreate but never serves
  cold-cache requests after `.migrated` lands. `update (initial)`
  now runs after the sentinel, so a backlogged upstream doesn't
  gate web startup behind a multi-minute ingest. Closes the gap
  that triggered the 1.36.0-era dashboard timeouts on every
  container recreate even with 1.36.1's commit-cadence fix in
  place.

## [1.36.4], 2026-05-20

### Fixed

- **Catastrophic recursive-CTE plans on production-scale corpus
  (1.35.1 regression)**: 1.35.1 set `PRAGMA analysis_limit=400` on
  every SQLite connection on the basis that SQLite docs called 400
  "appropriate for typical workloads." On the production 11M-row
  multi-inbox corpus this undersampled the join-driving tables
  (`articles.thread_parent`, the `(article_id, inbox_id)` covering
  index on `article_lists`) so badly that the planner picked
  catastrophically wrong recursive-CTE shapes. Worst confirmed
  case: `get_thread` for a **15-message** thread on lkml took
  **400 seconds** instead of the documented ~2 ms baseline. A
  manual full-scan ANALYZE (`analysis_limit=0; ANALYZE`) on
  production restored 200-1700× speedups instantly across the
  previously-slowest message URLs.

- **`ANALYZE_LIMIT` default bumped 400 → 4000**. SQLite docs hint
  at 1000-1500 for "very large databases"; 4000 is a 10× margin
  on that for an 11M-row corpus and brings the post-migrate
  ANALYZE wall time to ~1-3 s (vs 100 ms at 400 and 25-30 s
  uncapped). Validated end-to-end on the production corpus:
  produces accurate recursive-CTE plans across every read path
  hot enough to matter.

### Added

- **Weekly full ANALYZE** safety net via the scheduler sidecar.
  New `ANALYZE_FULL_EVERY` env (default 604800 = 7 days) drives
  `mimir analyze --full` once a week; the `--full` flag overrides
  `analysis_limit` to 0 for that pass so every row of every index
  is sampled. Holds the writer lock for ~25-30 s once a week in
  exchange for guaranteed-accurate stats catching any tail-heavy
  index that drifts under the bounded daily ANALYZE. Daily
  `analyze` remains the limited fast pass. The two sentinels
  (`/data/.last_analyze` + `/data/.last_analyze_full`) survive
  container restarts so the weekly cadence holds across deploys.

- **`mimir analyze --full` CLI flag**. Per-invocation override of
  the connection's `PRAGMA analysis_limit` for one full-scan
  ANALYZE pass. Used by the scheduler weekly tick; also available
  ad-hoc when an operator wants to refresh stats after a large
  ingest delta or to diagnose a planner regression.

## [1.36.3], 2026-05-20

### Fixed

- **Message-view renders taking 12-300+ seconds, starving gunicorn
  workers**: `recent_patches_touching` (the `msg_related` cache
  compute behind the "Other recent patches touching ..." sidebar
  on every message page) was structured as
  `JOIN article_files ... WHERE path IN (...) GROUP BY article_id
  ORDER BY date DESC LIMIT 5`. When a patch touched a popular
  file (`Makefile`, `include/linux/kernel.h`, anything under
  `arch/x86/`), `path IN (...)` matched millions of rows on the
  7M-row `article_files` table, the GROUP BY + ORDER BY
  materialised + sorted the whole set, and only then LIMITed.
  Result: 5-minute single-message renders. Direct localhost curl
  on `/lkml/2014/01/4189394` returned 200 at TTFB 304 s.

  Compounding factor: gunicorn's default 30 s worker timeout
  SIGKILL'd most of those requests before they could write the
  result back to cache. Production cache held **13** `msg_related`
  rows total against a corpus of millions of message views;
  steady-state cache-hit rate was effectively 0, so every bot
  request to a message page paid the full cold-compute cost. The
  bots retried, workers were continuously killed, and the
  cumulative effect made every other request (including the
  dashboards) wait in gunicorn's request queue. Cloudflare 524 by
  the time anything got served.

  Two-layer fix:

  1. **`recent_patches_touching` rewritten** to the same date-
     bound EXISTS pattern already used by
     `mimir.subsystems_dashboard._path_filter.
     _subsystem_path_filter_exists_sql`. Walks `ix_articles_date`
     DESC over a `RECENT_PATCHES_MAX_AGE_DAYS=180` window, tests
     `EXISTS (SELECT 1 FROM article_files af WHERE af.article_id
     = a.id AND af.path IN (...))` per article via the
     `(article_id, path)` PK on `article_files`, stops at LIMIT.
     Cold miss drops from minutes to milliseconds for any
     touched-path mix. Plan pinned in
     `test_recent_patches_touching_uses_date_index_no_full_scan`.
  2. **Gunicorn `--timeout` raised to 60 s** (env
     `WORKER_TIMEOUT`, default 60). Margin for any single
     genuinely-slow render (cold thread CTE, oversized
     `msg_related` mix) without SIGKILL'ing the worker mid-write.
     The query fix above brings typical renders well under a
     second; this margin only matters for outliers.

  `RECENT_PATCHES_MAX_AGE_DAYS` env-tunable (default 180), same
  semantics + shape as `SUBSYSTEM_TRIAGE_MAX_AGE_DAYS`. A patch
  touching a path with no activity in the last 180 days surfaces
  no "related patches" sidebar; an empty sidebar is the
  conservative answer when there's no recent neighbourhood to
  display, and the bound is the load-bearing piece of the query
  plan.

## [1.36.2], 2026-05-20

### Fixed

- **Front-page 524 timeouts after 1.36.1 deploy (cold-miss on
  `most_active_subsystems_global`)**: on a 200+ inbox corpus
  `warm-cache` takes ~10 min per cycle. The
  `most_active_subsystems_global` Phase B target ran once at the
  end of each cycle with a 5 min TTL, so the cache row was
  expired for ~5 min of every cycle. Front-page renders landing
  in that window fell through to a cross-inbox aggregation that
  iterates every configured inbox's per-inbox subsystem
  aggregation in turn (minutes of CPU on a 200-inbox corpus).
  Multiple gunicorn workers would race the same cold compute,
  Cloudflare's 100 s gateway timeout fired first, and operators
  saw HTTP 524 / TTFB > 100 s even with thousands of valid cache
  rows in the table.

  Two-layer fix:

  1. **`MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC` 300 → 3600** (5 min
     → 1 h). Comfortably exceeds any plausible `warm-cache` cycle
     time, so the row no longer expires faster than warm-cache
     can refresh it. The cached "active subsystems over the last
     7 days" is allowed to lag by up to 1 h, an acceptable
     trade-off versus the request-path-recompute footgun.
  2. **Request-path `compute_on_miss=False`** on
     `most_active_subsystems_global` and
     `most_active_subsystems_in_inbox`. The meta-index and per-
     inbox dashboard routes now serve an empty "Active
     subsystems" widget on cache miss instead of blocking the
     render on a cold compute. `warm-cache` keeps
     `compute_on_miss=True` so the cache stays populated; only
     the request path opts out.

## [1.36.1], 2026-05-20

### Fixed

- **Dashboard timeouts under broker mode (1.36.0 regression)**: hot
  inboxes (`stable`, `linux-mm`, `linux-arm-kernel`) committing a
  few hundred new messages per scheduler tick held the broker's
  SQLite writer lock for 1.7-2.2 s per `ingest_inbox` commit. The
  broker's cache worker queued behind, so every `cache_set` from
  the web tier waited 1-2 s for the lock. Dashboard renders that
  hit ~8 cached surfaces (front page, per-inbox pages) stacked
  multi-second waits per surface and tripped the gateway timeout
  with HTTP 524 / TTFB > 100 s.

  `ingest_epoch` now commits when **either** `processed %
  COMMIT_EVERY == 0` (the existing message-count side) **or**
  `time.monotonic() - last_commit_at >= COMMIT_EVERY_SECONDS`
  (new, default 0.5 s) is true. The writer-lock hold per commit
  is bounded to ~500 ms regardless of inbox burst size, so the
  cache worker gets the lock back within half a second of any
  long-op commit.

  Direct (non-broker) ingests are unaffected in throughput: same
  total number of inserts, just more commits on hot inboxes;
  commit overhead on SQLite WAL is single-digit ms each.

## [1.36.0], 2026-05-20

### Added

- **Write-broker Phase 2.1**: `mimir ingest` and `mimir update`
  dispatch each per-inbox ingest through the broker when
  `BROKER_SOCKET_PATH` is set. The broker's long worker runs
  `ingest_inbox` against its own writer connection; cache writes
  riding the broker's cache worker no longer compete cross-
  process with scheduler-side ingest commits. New
  `IngestInboxRequest(inbox_name, limit, workers)` long-op RPC.
  Default per-call timeout 3600 s. Direct path is preserved as
  the fallback for deploys not yet in broker mode.

  Cross-inbox `--limit` semantics are unchanged: the CLI
  decrements the limit as inboxes complete and stops once
  exhausted. Per-epoch `IngestResult` rows are reconstructed
  client-side from the JSON payload so output formatting is
  identical between direct and broker paths.

  Hard-fail (no silent fallback) when broker mode is set but
  the broker is unreachable; the Phase 2 architecture was
  built specifically to eliminate scheduler-side direct writes,
  silent fallback would defeat that.

### Changed

- **`handlers.py` split into a package**: `mimir/broker/handlers/`
  now carries `cache.py` (sub-ms ops + ping), `longops.py`
  (`bootstrap_inboxes`, `ingest_inbox`, plus Phase 2.2+ ops as
  they land), and `__init__.py` (dispatch table, queue routing,
  error boundary). Pre-emptive refactor ahead of Phase 2.2's
  backfill family; keeps each file's job sayable in one
  sentence. `server.py`'s import path is unchanged.

### Fixed

- **`cache.set` (and `delete` / `delete_for_inbox` /
  `purge_expired`) skip the self-RPC when called inside the
  broker process** (`MIMIR_ROLE=broker`). Phase 2.1 made this
  load-bearing: now that `ingest_inbox` runs in the broker,
  its post-ingest warm fires three cache writes per inbox; each
  would otherwise round-trip through the broker's own socket
  → cache_queue → cache worker. Direct write inside the broker
  drops the per-write cost from ~ms to microseconds, and
  removes the self-RPC traffic the broker's own log would
  otherwise show.

## [1.35.1], 2026-05-20

### Fixed

- `PRAGMA analysis_limit=400` is now set on every SQLite
  connection, bounding ANALYZE's per-index row sample to
  SQLite's recommended value. On the 11M-row prod corpus that
  drops ANALYZE from ~25 s (full scan) to ~100 ms while still
  producing planner stats good enough for sargable index
  choices. The 25 s lock-hold was the dominant source of
  broker-side cache.set stalls in production (e.g.
  `21545 ms total = 0 ms queued + 21545 ms dispatch` waiting
  for the daily ANALYZE to release the writer lock). Applies
  uniformly to `mimir analyze`, auto-ANALYZE-after-ingest, and
  any ad-hoc session running ANALYZE. Env-tunable via
  `ANALYZE_LIMIT`; set to 0 to restore the previous full-scan
  behaviour.

## [1.35.0], 2026-05-20

### Added

- **Write-broker Phase 2.0 scaffolding**: the broker now serves
  two queues with two dedicated worker threads. Cache ops
  (`cache_set` / `cache_delete` / `cache_delete_for_inbox` /
  `cache_purge_expired` / `ping`) keep flowing through the cache
  worker; new **long ops** (starting with `bootstrap_inboxes`)
  flow through the long worker. The two workers contend for the
  SQLite writer lock at the SQLite level, so cache writes only
  wait for the long worker's *current commit batch*, not the
  whole long op. Cache-op latency under load gains a fast lane.
- **Per-op timeout override on `BrokerClient`**: long ops can
  legitimately take minutes (Phase 2.1 ingest, future backfills);
  the per-RPC client API now accepts a `timeout=` kwarg that
  overrides the default 5s socket timeout for the duration of
  that one RPC and restores the default afterwards. Plan-pinned
  in `test_per_op_timeout_restored_after_rpc`.
- **`bootstrap_inboxes` migrated to long-op RPC**: when
  `BROKER_SOCKET_PATH` is set, `mimir bootstrap-inboxes` (the
  scheduler-tasks startup step) sends an RPC to the broker
  instead of writing the DB directly. Broker handler delegates to
  the same `mimir.inboxes.bootstrap_inboxes()` function. CLI
  echoes `... reconciled (via broker)` in broker mode to make the
  path visible. Direct path is preserved as the fallback for
  unconfigured deploys. This is the canary migration for the long-
  op family; Phase 2.1 migrates `ingest_epoch`, Phase 2.2 the
  backfills, etc., per the broker plan in MEMORY.md.

## [1.34.0], 2026-05-20

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
