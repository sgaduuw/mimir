# CONTEXT.md

Background and rationale for design decisions in mimir. `AGENTS.md`
says what to do; this file says why, and is the companion to read
before making a non-trivial change.

**Tracked in the repo since 2026-10-02.** It used to be gitignored,
which meant the rules it carries were invisible to every tool except
one. That had a measurable cost: the emitter/acceptor rule below is
this project's most recurring defect class, and two fresh instances
of it shipped in 3.10.0 written by an agent that could not read this
file. Design rationale is not workspace state.

Two consequences of being public:

- **No operator surface here.** Hosts, addresses, filesystem paths,
  container names, tunnel parameters and the post-deploy smoke live
  in the untracked `_claude/CLAUDE.md`. "Production deployment
  posture" below describes the deployment's *shape*, because the
  code depends on it, and deliberately not its *coordinates*.
- **Some pointers lead outside the repo.** References to
  `_claude/MEMORY.md` (often written as just `MEMORY.md`),
  `_claude/specs/` and `_claude/plans/` point at local, untracked
  working notes. They are kept because they are accurate provenance
  for a decision, and because losing them would not make this file
  more useful to someone who does have them. If you are reading this
  from a clone and cannot find one, you are not missing a rule, only
  the story behind it.

## Architecture: git mirror as source of truth

The original schema (Mongo era, then early SQLite) stored full message
bodies, all headers, and attachment bytes inline. Per-message footprint
was ~5.4 KB → ~25 GB projected for the full lkml archive.

We pivoted to using the public-inbox v2 git mirror as authoritative
storage and SQLite as a lean index. Each `articles` row records
`(epoch, commit_sha)` plus a few display fields (subject, author, date,
in_reply_to, references). Body, full headers, and attachment content are
re-derived on demand via `mimir.store.read_message`.

- Per-row size dropped from ~4.2 KB (post-header-filter) to ~471 B  
  ~11× reduction.
- Read latency: ~2 ms warm per message (SQL lookup + dulwich blob fetch
  + parse). A 50-message threaded view ≈ 100 ms.
- Full-text search over body content is **explicitly out of scope.**
  The author has search engines for that. If FTS comes back on the table,
  the conversation is "Xapian externally vs FTS5 with materialized
  body", not "store body in SQLite again."
- Mirror **must be present at read time**, not just at ingest. Any
  deployment of the web UI mounts/contains `lkml/git/`.

## Headers filter (`KEPT_HEADERS`)

We keep ~25 named headers (Subject, From/To/Cc/Bcc, Date, Message-ID,
References, In-Reply-To, MIME structural ones, List-*, User-Agent,
Organization, Archived-At). Dropped: Received chains, DKIM/ARC
signatures, X-Spam-*, Authentication-Results, these accounted for
~25% of pre-filter row weight and aren't useful for any current or
planned query. The full headers are still recoverable from the git blob
on demand.

## Redaction is a display-time decision, not a data transform

Email-address redaction in mimir applies only to rendered HTML. The
git blob is the source of truth and is never modified; any future
`raw` / `.patch` / `mbox` endpoints will stream blob bytes verbatim.
This locks in three invariants:

- **`From:` line.** Visible HTML uses `_safe_from_filter`: full
  address for senders matching `Settings.email_allowlist`
  (kernel.org, well-known maintainers); display name + `<hidden>`
  placeholder for everyone else; bare `<hidden>` if there's no
  display name. The original `From:` header survives in the git
  blob and is still accessible via `parsed.headers["From"]` at read
  time.
- **DCO trailers (`Signed-off-by:`, `Reviewed-by:`, `Acked-by:`,
  `Tested-by:`, `Reported-by:`).** Visible HTML uses
  `_redact_trailer_address`: allowlisted addresses survive
  verbatim; everyone else's address becomes `<redacted>`. The
  trailer text in the rendered body is otherwise unchanged. The
  raw trailer in the blob is byte-identical to what the contributor
  sent, required for DCO chain integrity if those bytes ever flow
  through `git am` after a `.patch` download.
- **Machine-readable surfaces.** JSON-LD `Person.name`, atom-feed
  `<author><name>`, and `data-*` attributes carry the display name
  only, never the `<hidden>` placeholder. The placeholder is a
  rendering decision for the visible HTML; in metadata it reads as
  broken data, and in `data-*` attrs the email-shaped Message-ID
  silently undid the visible redaction (caught in the 2026-05-12
  review). JSON-LD `Person.email` and atom-feed `<author><email>`
  ride along **only when the sender is in the allowlist union**
  (`Settings.email_allowlist` + MAINTAINERS-derived addresses),
  mirroring exactly what `_safe_from_filter` does on the visible
  HTML side. Earlier posture (display-name everywhere, no email
  ever in metadata) was strict-symmetric across *machine* surfaces
  but under-attributed allowlisted senders relative to the
  *rendered page*: the kernel.org address was already on the HTML
  page and in the public git blob, so omitting it from metadata
  gained nothing for redaction and cost real attribution for the
  one set of senders we don't otherwise redact. The new posture
  ("metadata mirrors visible HTML, per sender") closes that gap
  without widening the scrape surface, the address was already
  reachable in one HTTP fetch of the message page.

The redaction is a **friction layer**, not a privacy guarantee. The
same data is already public via lore.kernel.org, vger.kernel.org,
and every other public-inbox mirror; a determined scraper can fetch
the original RFC 5322 bytes in a single HTTP call. The goal is to
make casual scraping less rewarding and to keep the visible page
friendly to readers who don't need the addresses, not to claim that
mimir hides them.

**`[off-list ref]` is not one of the redaction policies**, and it is
worth saying so because it looks like one. It is what the
Message-ID linkifier substitutes for any `<local@domain>` token it
cannot resolve to an archived message, so an address embedded in
*patch content* (a `MODULE_AUTHOR` line, a MAINTAINERS `M:` entry)
got it by accident of where it sat. Which surface a body region was
rendered by therefore decided whether that address showed: a patch
sent to the list rendered it verbatim through the diff path, while
a reviewer's quote of the identical hunk rendered `[off-list ref]`
through the text path. The same substitution on DCO trailers is the
"smear" that motivated `_redact_trailer_address` in the first place
(see above).

Decided 2026-09-29, when quoted hunks began being recognised and
rendered as diffs: quoted and unquoted hunks render the same, i.e.
verbatim, and the divergence goes away. Measured at 4 of 400 recent
linux-wireless messages. What must NOT follow from that is any
promotion that moves a **DCO trailer** off the `linkify` path, and
it cannot: `_TRAILER_LINE_RE` is anchored on a trailer key at line
start while every line of a promotable run begins with a space,
`+` or `-`. That is an invariant spanning `rendering/linkify.py`
and `rendering/blocks.py`, so it is pinned by
`test_dco_trailer_redaction_cannot_be_reached_by_promotion` rather
than left to hold by luck.

The "downloads inherit DCO integrity for free" property is what
makes the future raw-endpoints work decoupled from the redaction
policy: those endpoints just stream blob bytes, with no
special-cased "redact-on-download" code path to maintain. See
"Roadmap (deferred features)" for the planned shape.

### Allowlist source: static config + parsed MAINTAINERS

The allowlist that drives `safe_from`, `_redact_trailer_address`,
and the per-reviewer link-generation check is the **union** of:

- `Settings.email_allowlist` (env-tunable substring tokens; the
  static defaults are `torvalds@`, `gregkh@`, `@kernel.org`).
- The set of `M:`/`R:` addresses parsed from the kernel tree's
  `MAINTAINERS` file, lowercased and exact-matched. Lives in the
  `subsystem_maintainers` table; the lookup is cached in the
  `cache` table for 24h with the `update-mainline` flow
  invalidating after every reload.

Why dynamic, not just static: hard-coding maintainer addresses in
config would lag the upstream tree. MAINTAINERS is the
authoritative source the kernel community already maintains for
exactly this question ("who is officially recognised on this
subsystem"); reading it gives mimir the same recognition surface
for free, and operators don't have to keep a parallel list.

Why a *union* rather than a replacement: the static tokens carry
broad-net cases the MAINTAINERS file alone doesn't match. Linus's
non-`@kernel.org` addresses, the whole `@kernel.org` domain
(non-MAINTAINERS-listed `@kernel.org` accounts), and any operator
override stay in scope. Removing the static layer would silently
narrow the allowlist for those.

Why `M:` + `R:` and not `L:`: list addresses (the `L:` field) are
per-subsystem list contacts, not personal contact. They have a
separate code path (`canonical.is_list_address`) for per-list
purposes, and including them on the From-line allowlist would
surface list addresses in places where the redaction posture is
about *personal* email visibility.

Graceful degradation: a deploy without the kernel tree mirrored
(or where `update-mainline` hasn't run yet) sees an empty
MAINTAINERS-derived set; only the static tokens apply. This is
the documented behaviour, not an error path.

## MIME parsing: working around a Python stdlib bug

Under `policy.default`, even `msg.items()` triggers
`email.headerregistry.AddressHeader.parse`, which builds
`Address(mb.display_name, mb.local_part, ...)` for every component and
assumes each is a Mailbox. RFC 5322 *group* addresses
(`groupname: a@x, b@y;`) parse to a `Group` instance with no
`.local_part` attribute → `AttributeError`. Real-world lkml messages
hit this often enough to fail dozens of messages per epoch.

**Verified still present on 3.14.4** (re-checked after the Python bump).
Don't assume a future minor bump fixes it, re-test before removing the
workaround.

**Workaround in `mimir.parser`:**
- Read every header via `msg.raw_items()`, bypasses policy parsing.
- Decode RFC 2047 encoded-words ourselves with
  `email.header.decode_header`.
- Parse `Date` via `email.utils.parsedate_to_datetime` on the raw string.

`policy.default` is still used for the structural body and attachment
work (`get_body`, `iter_attachments`, `get_filename`, `get_content`)  
those code paths don't trip the bug and *are* genuinely useful (RFC 2231
filenames, multipart/alternative preference, etc.).

## `articles.date` = public-inbox commit timestamp

We use the **commit time from the public-inbox mirror** as the canonical
date for every article, not the email's `Date:` header.

Rationale: senders' clocks are an unbounded set of untrusted clocks
(we saw real-world examples with `Date: 2085`, `2077`, `2043`, `2083`,
all clearly typos). lore.kernel.org's commit timestamp is a single
trusted clock; messages are committed within seconds of arrival, so
the value is causally consistent with thread order and "when it
became public," which is what an archive cares about.

The trade-off accepted: precision drops from "wall-clock send time" to
"archive arrival time", typically seconds to minutes of difference,
imperceptible at the scale of a listing UI. The original `Date:`
header is preserved verbatim in the git blob and remains accessible
via `parsed.headers["Date"]` at read time if anyone wants to display
"sender claimed".

Implementation: `_walk_epoch` yields `(commit_sha, commit_time,
raw_bytes)` tuples. The worker pickles the commit_time through
unchanged. `_to_article` is called with `date=commit_time` and ignores
`parsed.date`. There's no plausibility check, no fallback logic, the
parser still extracts `Date:` for fidelity but the field isn't used
for the SQL row.

**Exception: SEO `datePublished` uses `parsed.date`.** The JSON-LD
`DiscussionForumPosting` emitted on message pages is for search
engines, which want the message's *send time* as published as a
canonical claim, not our internal "when it became public" reading.
We pull `parsed.date` (with `_aware_utc` normalization for `-0000`
headers) and fall back to `article.date` only if parsed.date is
missing. The trade-off here flips: search engines tolerate a
slightly-off published date better than they tolerate a published
date that doesn't match the visible "From … on …" line, which
displays the parsed Date header.

## Surrogate scrubbing (two layers)

`policy.default`'s content manager surrogate-escapes bytes that don't
decode against the declared charset. Surrogate codepoints can't survive
a UTF-8 round-trip; SQLite refuses them. `_scrub_surrogates` handles
two cases:

1. **Lone surrogates outside the surrogate-escape range** (e.g. U+DF9D
   from broken UTF-16 elsewhere in the chain) → replaced with U+FFFD
   directly. There's no original byte to recover.
2. **Surrogate-escape range** (U+DC80 U+DCFF) → round-tripped through
   bytes via `errors="surrogateescape"`. Pairs that happened to be
   valid UTF-8 (e.g. 0xC3 0xA9) come back as their proper character
   (`é`); anything else becomes U+FFFD.

The scrubber is registered as pydantic `@field_validator`s on
`ParsedArticle` and `ParsedAttachment`, so it covers every `str` field
uniformly (subject, author, body, in_reply_to, references, headers,
filename, content_type). `body_content_type` was in that list until
3.10.0 removed the field as unused.

## Concurrency: ProcessPoolExecutor over Celery

We use `concurrent.futures.ProcessPoolExecutor` to parallelize
`parse_message` (default `os.cpu_count()` workers).

Celery was considered (the author had used it successfully on a different
project) and rejected. Celery is the right tool for distributed
background work driven by a stream of web requests, not for one-off
CLI ingest of a local data set. Operational overhead, running a broker
(Redis/RabbitMQ), worker processes, serialization through the broker
for ~5 KB/msg payloads, is unjustified when in-process IPC via
`multiprocessing` does the same job with zero infrastructure.

Empirical note: small-sample benchmarks (e.g. `--limit 2000`) misled  
dulwich's `reverse=True` walker has a fixed upfront cost that
dominates short runs and amortizes away on real workloads. Real
full-epoch ingests are noticeably faster with parallel workers.

## Schema decisions

The DB is 14 small tables, each carrying its own narrow concern.
Three groups, by what they hold:

**Core archive (5 tables)**, the inbox + message graph the
read path uses every request:

- **`inboxes`**, one row per configured public-inbox archive.
  `name` is the URL slug; `mirror_path`, `upstream_url`,
  `list_address` (auto-promoted, see below), and `tracked_authors`
  (per-inbox dashboard trackers, JSON dict, mutated via
  `admin inbox trackers`) describe its shape. Bootstrapped from
  `Settings.inboxes` env via `mimir.inboxes.bootstrap_inboxes`;
  CRUD via the `admin inbox` subcommand group.
- **`articles`**, unified across all inboxes. One row per unique
  Message-ID; cross-posted messages collapse to one row.
  `canonical_inbox_id` (FK, ON DELETE SET NULL) records which inbox
  the author *meant* to send to (resolved from To/Cc, see "Canonical-
  inbox resolution"). Indexed columns: `message_id` UNIQUE, `date`,
  `thread_parent`, `subject_normalized`, `canonical_inbox_id`,
  `patch_series_key`. All used by current routes.
- **`article_lists`**, many-to-many between articles and inboxes.
  Stores the per-inbox blob pointer `(epoch, commit_sha)` because
  cross-posted messages have *different* SHAs in each mirror. The
  read path uses this row to find the right blob; threading queries
  filter article visibility per inbox via this table.
- **`ingest_state`**, `(inbox_id, epoch)` PK with
  `last_commit_sha` for resuming incremental walks.
- **`cache`**, JSON-encoded values keyed by string with unix-second
  expiry (see "Cross-process cache" below).

**Per-article derived metadata (2 tables)**, produced at ingest
time from `parsed.body`, used by patch-page surfaces and
per-subsystem dashboards:

- **`article_files`**, `(article_id, path)`, the post-rename
  paths a patch body touches (extracted from `diff --git a/x b/y`
  headers in `mimir/patches.py`). Powers the subsystem-header
  per-path reverse lookup.
- **`article_trailers`**, DCO / review trailers
  (`Signed-off-by:`, `Reviewed-by:`, `Acked-by:`, `Tested-by:`,
  `Reported-by:`) parsed out by `mimir/trailers.py`. Stores
  `role`, `name`, `address`, `address_normalized`. Used to render
  per-message attestation + per-reviewer recent-activity.

**MAINTAINERS index (3 tables)**, derived from the kernel tree's
`MAINTAINERS` blob by `flask --app mimir update-mainline`. Refreshed
in lockstep with the mainline pull:

- **`subsystems`**, one row per MAINTAINERS section (`name`,
  `status`).
- **`subsystem_paths`**, per-subsystem `F:` / `X:` glob entries,
  with `is_exclude` flag.
- **`subsystem_maintainers`**, per-subsystem `M:` / `R:` rows,
  carrying `role`, `name`, and `address`. Their addresses feed the
  maintainer-derived half of the allowlist union.

**Mainline tree cursor (2 tables)**, for the future "applied as
<sha>" link surface on patch pages:

- **`mainline_state`**, single-row cursor (`commits_walked_to_sha`)
  for the `update-mainline` `Link:` trailer walker.
- **`mainline_commits`**, one row per tree per commit whose message
  names a posted patch (a lore or patch.msgid.link `Link:`, or a
  `Message-ID:` trailer),
  enabling the "applied as <sha>" backlink on patch views. The
  tree is part of the key: a patch applied in a subsystem tree and
  later merged by Linus has the same SHA in both, and lifecycle
  needs the Linus row to call it landed (#673).

**Operational tallies (2 tables)**, populated by ingest, consumed
by ingest:

- **`inbox_address_observations`**, `(inbox_id, address)` PK,
  count + last_seen. Per-inbox tally of list-shaped addresses
  observed in To/Cc; used to auto-promote `Inbox.list_address` once
  enough evidence accumulates.
- **`parse_failures`**, `(inbox_id, epoch, commit_sha)` PK plus
  `error_class`/`error_message`/timestamps. Persisted so the operator
  can replay them after a parser fix instead of scanning ingest logs;
  cleared automatically on successful re-parse.

Why per-message data isn't sharded by epoch: threading and Message-ID
linkification queries all want a single index. Per-epoch tables would
force UNIONs everywhere with no measurable performance benefit;
SQLite handles 6M+ rows with B-tree indexes just fine.

Attachments are metadata-only at parse time (`filename`,
`content_type`, `size_bytes`); the bytes themselves come from the git
blob on demand. Listings render "📎 foo.patch (12 KB)" without a git
read; downloads fetch the blob on the request path.

**SQLite pragmas** are set on every connection (`mimir/extensions.py`):
`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
`busy_timeout=<Settings.sqlite_busy_timeout_ms>` (default 5000). WAL
is persistent in the DB header once written; we still re-issue on
every connect for idempotence. Alembic was rewired to use the same
engine so migrations run with these pragmas active too. **WAL means
readers don't block writers.** Safe to run the web UI or ad-hoc
queries during an ingest.

`busy_timeout` was added after a production "database is locked"
incident: the web tier rendered a page, then failed to upsert a
`monthly_volume` row into `cache` because the scheduler sidecar held
a write lock. Default 0 turns transient contention into hard 500s;
5s rides out normal scheduler write windows (ingest commits, ANALYZE
after threshold). VACUUM on the full archive can outlast it, that's
intentional, surfacing a true VACUUM-vs-write conflict beats masking
it as a multi-minute request hang. Tunable via
`SQLITE_BUSY_TIMEOUT_MS` so an operator can lengthen it without code
changes if the scheduler grows heavier writers.

**`busy_timeout` doesn't cover `SQLITE_BUSY_SNAPSHOT`**, a distinct
error path SQLite raises when a transaction that started as a
*reader* (taking a snapshot) tries to upgrade to a *writer* and
another connection committed in between. The snapshot is now
inconsistent with the live DB and SQLite returns an immediate
non-retryable failure; raising `busy_timeout` to 10 minutes makes
no difference. Surfaces as the same `OperationalError("database is
locked")` message, easy to misread as a busy-wait timeout.

The long-running CLI write workloads (backfills, `ingest_epoch`,
`update-mainline`) all interleave reads with writes: read the next
batch of articles, mutate fields, write observations, commit,
repeat. On a deploy where gunicorn writes its own cache rows in
the same window, those snapshot upgrades fail. The fix is
`mimir.extensions.write_transaction()`, a ContextVar-gated event
listener that issues `BEGIN IMMEDIATE` instead of SQLAlchemy's
default deferred BEGIN for transactions started inside the block.
`BEGIN IMMEDIATE` acquires the writer lock at transaction start
(other writers queue via `busy_timeout`, which IS retryable), so
no snapshot upgrade is ever attempted. The helper is applied at
every long-running write entry point; read-only paths and the web
tier stay deferred so they don't serialise on the writer lock.

### Single-writer invariant via the broker (v2.0.0+)

The above two mitigations (`write_transaction` BEGIN IMMEDIATE +
`busy_timeout`) ride out contention but don't eliminate it. Through
the broker journey of v1.32-v1.39 every periodic and admin write
path was migrated through a single `mimir-broker` process; v2.0.0
made the broker mandatory and removed the direct-write fallbacks.

The invariant since v2.0.0:

- **The broker is the sole SQLite writer process.** It runs a
  queue + worker pool (cache worker, long-op worker, warm workers)
  inside one process; cross-process writer-lock contention can't
  happen because no other process holds a writer connection.
- **Every other process opens `PRAGMA query_only=1`** on every
  connection (`mimir` web, `mimir-tasks` orchestrator). Any
  accidental write attempt raises `OperationalError: attempt to
  write a readonly database` rather than silently being lost.
- **Cache writes from the web tier RPC into the broker** via the
  UNIX socket at `BROKER_SOCKET_PATH` (default `/data/.broker.sock`).
  Best-effort: `BrokerUnavailable` is logged at warning and
  swallowed, the page already rendered before `cache.set` was
  called.
- **Admin operations RPC into the broker** too. The historical
  `READ_ONLY_DB=true` toggle (used to quiesce the web tier for
  long admin writes) is gone, the broker's own queue serialises
  cache writes behind long-op writes naturally.

`write_transaction()` and the `BEGIN IMMEDIATE` event listener are
**still load-bearing** inside the broker, because the broker is
single-process but multi-threaded: cache + long + warm workers run
in separate threads sharing pooled connections, and intra-broker
snapshot upgrades can still trip `SQLITE_BUSY_SNAPSHOT` when a
long worker reads-then-writes while another worker commits between
the two operations. The listener stays as protection against that
class of bug.

The journey here (in case any of this needs revisiting):
v1.32.0 introduced an opt-in broker for cache writes only; v1.33.0
reworked the single-handler design into a queue+worker pool;
v1.36.0-v1.38.0 migrated ingest, backfills, mainline, analyze, and
vacuum into the broker; v1.39.0 migrated the admin ops; v2.0.0
dropped `READ_ONLY_DB`, dismantled `MIMIR_ROLE`, and removed the
direct-write fallbacks.

**2.10.0: Phase 1 of the two-pool restructure landed.** The broker
now constructs a `ReadSessionPool` (query_only=1 SQLAlchemy
sessions, default size `os.cpu_count()`) and a `WriterThread`
(single-thread actor with a bounded queue, default depth 256). No
handler is migrated yet, so steady-state behaviour is unchanged;
the new primitives sit parallel to the existing
`mimir.extensions.SessionLocal` + `write_transaction()` path.
Phases 2 to 6 migrate one write surface per release (warm-cache
first, then long-ops, cache.set from web tier, admin ops,
cleanup). See `_claude/specs/2026-05-29-broker-two-pool-design.md`
for the full rollout. The new operator-tunable env vars are
`BROKER_READ_POOL_SIZE` and `BROKER_WRITER_QUEUE_DEPTH`.

**2.11.0: Phase 2 of the two-pool restructure landed.** The warm
handlers (`handle_warm_inbox`, `handle_warm_global`) now check
their read session out of the active `ReadSessionPool` instead
of `SessionLocal()`. `cache.set()` calls issued by warm targets
dispatch through the active `WriterThread` (the new
`cache.set_via_writer()` variant); calls from outside the broker
(web tier, tests) still run inline as before. The active broker
is registered in a small `mimir/broker/_context.py` module-level
slot by `serve()` at startup. No RPC contract change. Phases 3
to 6 still pending.

**2.12.0: Phase 3 of the two-pool restructure landed.** The
adopters this phase is narrowed to `update_mainline()`'s
per-tree walk; ingest + backfills are deferred to a separate
Phase 3b to avoid bundling unrelated migrations. The shape
change: instead of holding `write_transaction()` for one
continuous ~62 s window per tree, the per-tree loop now reads
on a `query_only` session from `_context.get_active_pool()`
and dispatches batched WriteOps through `_context.get_active_writer()`.

- `walk_commits()` takes `writer` and `batch_size` keyword args
  (the latter sourced from a new `MAINLINE_COMMIT_BATCH_SIZE`
  Settings field, default 100, env-tunable).
- Each accumulated batch is submitted via
  `_submit_mainline_batch(writer, tree_name, rows)`, which wraps
  `sqlite_insert(MainlineCommit).values(rows).on_conflict_do_nothing(
  index_elements=["commit_sha", "message_id", "tree_name"])`
  (`tree_name` joined the key in #673). The caller waits
  on `.result(timeout=60)` before composing the next batch.
- For `rebases=True` trees (e.g. `linux-next`, `mm`), the pre-walk
  DELETE that purges stale rows is also a WriteOp now
  (`mainline:<tree>:delete-rebases` label).
- The cursor (`MainlineState.commits_walked_to_sha`) is the FINAL
  WriteOp per tree, via `_submit_mainline_cursor_update`. This
  preserves resume-from-cursor semantics: a crash between batch N
  and the cursor submit leaves the cursor at its old position, so
  the next tick re-walks from there, and `on_conflict_do_nothing`
  makes that replay idempotent for batches that already committed
  pre-crash. Pinned by
  `test_mid_walk_cursor_failure_leaves_cursor_at_old_position`.

Writer-lock hold per batch drops from one ~62 s transaction to N
short bursts of tens of ms each. `cache.set` RPCs from the web
tier (which compete for the same writer) drain between bursts,
eliminating the qsize=93 head-of-line stalls and the 100+ s
warm-cycle outliers seen on the post-2.11.0 baseline. Bounded
cache.set tails under walk load are pinned by
`test_cache_writes_drain_between_mainline_batches` (asserts max
round-trip < 5 s while the walk is in flight; observed tens of
ms on a healthy laptop).

Explicitly out of Phase 3 scope (deferred to Phase 3b):
`load_maintainers()` still uses `write_transaction()` (separate
code path inside `update_mainline()`'s per-tree loop, runs only
for Linus); the per-tree `last_walked_at` cadence write still
uses a plain `SessionLocal()`. Both are low-frequency single-row
writes outside the dulwich walker's hot path, and migrating them
needs the cadence-gate read path migrated too. Phases 4 to 6
still pending.

**2.13.0: Phase 3b of the two-pool restructure landed.** The
ingest pipeline migrates: `ingest_inbox` + `ingest_epoch` no
longer hold `write_transaction("ingest_inbox:<name>")` for the
full epoch walk (tens of seconds on a backlogged ingest). The
shape splits into a read/compute phase (a `query_only` session
from the active `ReadSessionPool` builds a `_PendingWrites`
accumulator carrying Articles, ArticleList rows, ParseFailure
deltas, InboxAddressObservation upserts, and the conditional
`Inbox.last_article_date` UPDATE) and a write phase (one composite
WriteOp per batch via `_submit_ingest_batch`, awaited via
`.result()` before the next batch composes).

- `ingest_epoch` takes a new `writer` kwarg with context fallback
  (`_context.get_active_writer()`) and a `batch_flush_seconds`
  kwarg sourced from `INGEST_BATCH_FLUSH_SECONDS` (new Settings
  field, default 0.5, env-tunable).
- The Article INSERTs use SQLite's `RETURNING id` so ArticleList
  / ArticleFile / ArticleTrailer rows land their FKs in the same
  closure. Same-batch cover-letter lookup checks the in-flight
  `pending.articles` list before falling back to SQL so an in-
  series patch can resolve its parent without waiting for the
  cover letter's commit.
- The `IngestState.last_commit_sha` cursor UPSERT is the FINAL
  statement of each batch's closure (mirror of Phase 3a's
  invariant). A crash between batch N and batch N+1 leaves the
  cursor at batch N's last sha; the next tick walks from there
  and `articles.message_id` UNIQUE + the `article_lists`
  composite PK on `(article_id, inbox_id)` absorb any duplicate
  rows from batches that did commit pre-crash. (`epoch` and
  `commit_sha` are plain columns on that row, NOT part of the key:
  one article has exactly one pointer per inbox, which is what lets
  `store.read_message`'s `.one_or_none()` and `read_messages`'
  per-epoch grouping both assume a single row per inbox. This was
  documented as a four-column PK until 2026-07-29; the schema never
  had one.) Pinned by
  `test_mid_epoch_batch_failure_leaves_cursor_at_old_position`.
- `promote_list_address` (the per-inbox auto-promotion of
  `Inbox.list_address` once observations cross threshold) and
  `auto_analyze` (the post-ingest planner-stats refresh once
  enough rows have moved) migrate as small single-statement
  WriteOps via `_submit_promote_list_address` and
  `_submit_analyze`. `_warm_after_ingest` stays on
  `SessionLocal()` because `cache.set` opens its own write
  session internally and the read pool's `query_only=1` sessions
  would reject the write.

Writer-lock hold per batch drops from one continuous transaction
per epoch (tens of seconds on backlogged ingests) to N short
bursts of tens of ms each, so concurrent `cache.set` RPCs from
the web tier drain between batches instead of head-of-line
stalling. Bounded cache.set tails under ingest load are pinned by
`test_cache_writes_drain_between_ingest_batches` (asserts max
round-trip < 5 s while ingest is in flight; observed tens of ms
on a healthy laptop).

Supporting change in `mimir/cache.py`: the in-process writer
path is now gated on `_broker_handler_active()` (the thread-
local flag the broker's worker loops set on entry, clear on
exit) rather than on the global `_context.get_active_writer()`
check. Production behaviour is unchanged (broker workers always
set the flag, so warm + ingest handlers still dispatch via the
writer), but in-process test setups that register a session-
scoped active context no longer take the writer path from test
threads, preserving the SessionLocal monkeypatch contract that
the cache tests rely on.

Explicitly out of Phase 3b scope (deferred to Phase 3c, planned
as 2.14.0): the four backfills (`backfill_canonicals`,
`backfill_article_files`, `backfill_article_trailers`,
`backfill_patch_series`). They share the same `write_transaction`
+ chunked-commit shape as ingest pre-3b and migrate next.

**2.14.0: Phase 3c of the two-pool restructure landed.** The four
patch-metadata backfills (`backfill_article_files`,
`backfill_article_trailers`, `backfill_patch_series`,
`backfill_canonicals`) migrate: each no longer holds
`write_transaction(label)` for the full walk. Per-batch work splits
into a read/compute phase (query_only session from the active
ReadSessionPool, accumulates per-article `_<X>Pending` payloads) and
a write phase (one composite WriteOp per batch via `_submit_<X>_batch`,
awaited via `.result()` before composing the next batch).

- The three patch-metadata backfills share the restructured
  `mimir/_backfill.py::walk_articles` shell, which now takes a
  `flush_batch(writer, payloads)` callable from each caller; reads
  stay on the query_only session, writes go through the active
  writer.
- `backfill_canonicals` keeps its own walk loop (in
  `mimir/ingest/backfill.py`) because it interleaves a periodic
  list-address promotion sweep with the article walk. The
  sweep itself runs as `_submit_promote_list_address_sweep(writer)`,
  separate from `_submit_canonical_batch(writer, payloads)`. After
  each sweep the walker refreshes its local `address_to_inbox_id`
  map from the read session so subsequent canonical-resolution
  picks see promoted addresses.
- New module `mimir/_pending_backfill.py` carries the four
  `_<X>Pending` dataclasses and the five `_submit_*_batch` /
  `_submit_promote_list_address_sweep` helpers. Underscore-prefixed;
  not part of the public surface. Same role for backfills that
  `mimir/ingest/_pending.py` plays for ingest.
- Idempotency is by predicate (`WHERE canonical_inbox_id IS NULL`,
  `WHERE NOT EXISTS (SELECT 1 FROM article_files WHERE article_id =
  ...)`, `WHERE patch_series_position IS NULL`), not by cursor, so
  the Phase 3a/3b "cursor as final WriteOp" invariant does not
  apply. Per-batch atomicity (the entire closure rolls back on
  closure failure) is pinned by
  `test_batch_closure_failure_rolls_back_entire_batch`.

Writer-lock hold per batch drops from one continuous transaction per
walk (minutes on full-corpus `--reprocess` runs) to N short bursts
(~tens of ms each), so concurrent `cache.set` RPCs from the web tier
drain between batches. Pinned by
`test_cache_writes_drain_between_backfill_batches`.

Explicitly out of Phase 3c scope: `cache.set` from the web tier
(Phase 4, planned 2.15.0); admin ops (Phase 5, planned 2.16.0);
removal of `write_transaction()` and the BEGIN IMMEDIATE event
listener (Phase 6).

**2.15.0: Phase 4 of the two-pool restructure landed.** The four
broker cache RPC handlers (`handle_cache_set`,
`handle_cache_delete`, `handle_cache_delete_for_inbox`,
`handle_cache_purge_expired`) migrate: each no longer runs
`cache._direct_*(...)` inline on the cache worker thread wrapped
in `write_transaction("broker:cache_*")`. Each becomes a thin
shim that obtains the active WriterThread via
`_context.get_active_writer()`, dispatches via the corresponding
`cache.<op>_via_writer` helper (four new helpers in
`mimir/cache.py` paralleling the `_direct_*` family:
`_set_via_writer_for_nskey`, `delete_via_writer`,
`delete_for_inbox_via_writer`, `purge_expired_via_writer`), and
awaits `.result()` for the rowcount the reply needs.

Reply shapes preserved exactly: `handle_cache_set` and
`handle_cache_delete` return `Reply(ok=True)` (no `rows_deleted`);
`handle_cache_delete_for_inbox` and `handle_cache_purge_expired`
return `Reply(ok=True, rows_deleted=n)`. No RPC contract change.

The architectural payoff: after Phase 4 every SQLite write inside
the broker funnels through one WriterThread, regardless of which
RPC queue (cache / long / warm) the request landed on first. The
cache worker thread keeps running (dequeues from `cache_queue` and
dispatches) but the actual SQLite write lands on the WriterThread.
Removing the cache worker thread entirely is Phase 5 or 6 cleanup.

Supporting change in `mimir/broker/writes.py`: `_run_one` now
propagates the closure's return value into the WriteFuture
(`future.set_result(op.fn(conn))` instead of
`future.set_result(None)`). Backward-compatible: every existing
closure that returned `None` implicitly continues to behave
identically. The change enables the cache delete-family handlers
to return rowcount via `.result()` without captured-variable
workarounds.

Bounded cache-handler tail under long-op load is pinned by
`test_cache_handlers_dispatch_under_long_op_load` (max round-trip
< 5 s while a 20-article backfill is in flight). Mirror of Phase
3c's `test_cache_writes_drain_between_backfill_batches`, but
exercising the broker handler path instead of submitting WriteOps
directly.

Explicitly out of Phase 4 scope: removing the cache worker thread
(Phase 5 / 6), removing `cache._direct_*` helpers (still used by
tests with monkeypatched `SessionLocal`), removing
`write_transaction()` and the BEGIN IMMEDIATE event listener
(Phase 6). Admin ops migration is Phase 5.

**2.17.0: Phase 5 of the two-pool restructure landed.** The 12
admin RPC handlers (3 inbox CRUD + 4 tracker mutators + 4 robots
CRUD + 1 failures replay, all in
`mimir/broker/handlers/maintenance.py`) no longer write inline via
the service layer's `SessionLocal()` (and `write_transaction()`
for `mimir/robots.py`). Each service-layer function in
`mimir/inboxes.py`, `mimir/robots.py`, and
`mimir/ingest/replay.py::replay_failures` now starts with a
`try: writer = _context.get_active_writer(); except RuntimeError:
writer = None` dispatch fork: when an active broker context is
set (handler invocation), it composes a composite WriteOp whose
closure runs the full SQL sequence and awaits `.result()`; when
not (tests, `bootstrap_inboxes`, dev scripts), it falls back to
the legacy `SessionLocal()` path.

The architectural payoff: after Phase 5 the admin write surface
joins the single-writer invariant. The periodic purge timer
(`mimir/broker/server.py::_purge_loop`) was migrated separately
(it now submits `cache.purge_expired_via_writer(writer)` as a
WriteOp), so that is no longer a residual.

**Phase 6 scope, corrected 2026-06-14 against the actual code (the
earlier "only the purge timer writes outside the WriterThread"
framing was already stale):** several shared-engine write paths
still run *inside* the broker concurrently with the WriterThread,
so `write_transaction()` + the BEGIN-IMMEDIATE listener + the
`_direct_*` helpers remain genuinely load-bearing, not dead code:

- `maintenance.run_analyze` does `engine.begin()` on the shared
  engine (a broker handler invokes it).
- `maintenance.run_vacuum` uses a raw `sqlite3` connection after
  `engine.dispose()`: it cannot become a WriteOp (needs the
  exclusive lock and disposes the pool); it is inherently a
  special case under single-writer.
- the public `cache.set()` falls to `_direct_set()` on the shared
  engine for any in-broker caller not migrated to
  `set_via_writer` (e.g. `_warm_after_ingest`), because
  `_should_dispatch_to_broker()` is False inside the broker.
- `longops.bootstrap_inboxes` and `mainline.load_maintainers`
  (the latter explicitly deferred from Phase 3) still wrap
  shared-engine writes in `write_transaction()`.
- robots.py's legacy fallback path (non-broker callers only:
  tests, dev scripts).

So Phase 6 is **not** a pure deletion: it must first migrate the
remaining in-broker shared-engine writers onto the WriterThread,
after which dropping `write_transaction()` / the BEGIN-IMMEDIATE
listener / `_direct_*` falls out. The cache worker thread is kept
deliberately (load-bearing for dispatch-level concurrency; see the
spec), not collapsed. Scoped via a brainstorming → spec → plan pass:
`_claude/specs/2026-06-14-broker-two-pool-phase-6-design.md` plus
`_claude/plans/2026-06-14-broker-two-pool-phase-6{a,b}.md`.

**Phase 6a SHIPPED in 3.4.0 (2026-06-14).** It migrated the
steady-state shared-engine writers: `cache.delete` /
`cache.delete_for_inbox` (symmetric `_broker_handler_active()`
routing; `cache.set` was already routed), `run_analyze` (dual-fork
WriteOp, with the `analysis_limit` reset needed on BOTH the writer
and the pooled shared-engine fallback path), `load_maintainers`
(composite WriteOp), the `update_mainline` `last_walked_at` cadence
write (this one was missed by the spec inventory and caught only by
the final whole-branch review, it had no `write_transaction` wrapper
so it was invisible to the slow-write gate), and the steady-state
`bootstrap_inboxes` RPC. After 6a the in-broker single-writer
invariant is complete for the migrated scope; the only writes that
bypass the WriterThread during serving are VACUUM and the pre-serve
startup bootstrap (the two documented exception classes).
**Phase 6b IMPLEMENTED 2026-06-14 (pending release).** The 6a
deploy-observe gate cleared on prod (held= log silent across an
update-mainline tick + ingest + warm), so 6b removed the now-dead
scaffolding: `write_transaction()`, the BEGIN-IMMEDIATE `begin`
listener, the slow-write commit/rollback loggers, and the
`_restore_default_busy_timeout` reset listener are gone from
`mimir/extensions.py`; the `sqlite_busy_timeout_ms_writes` and
`write_transaction_slow_log_ms` Settings fields are dropped (the
normal `sqlite_busy_timeout_ms` connect pragma stays). The
cache-write SQL is de-duplicated behind shared per-op
statement-builders (`_set_stmt` / `_delete_one_stmt` /
`_delete_for_inbox_stmt` / `_purge_expired_stmt`) used by both the
`*_via_writer` closures and the `_direct_*` helpers; `_direct_*`
are retained as explicitly TEST-ONLY scaffolding (no production
caller, guarded by `test_no_production_module_references_direct_cache_helpers`).
The robots.py and `load_maintainers` non-broker fallback paths drop
the `write_transaction` wrapper (plain `SessionLocal`; single-threaded,
no concurrent committer, so DEFERRED begin is safe). **The cache
worker thread is KEPT, not collapsed** (it is load-bearing for
dispatch-level concurrency: it lets cache RPCs submit promptly and
interleave into the WriterThread FIFO between a long op's per-batch
WriteOps; collapsing it would regress cache.set latency under
long-op load). With this, the broker single-writer journey
(v1.32 -> v2.0 -> two-pool Phases 1-6) is complete: every write
funnels through the single WriterThread except VACUUM and the
pre-serve startup bootstrap.

Per-handler structural-contract tests in
`tests/test_ingest_phase5.py` capture all WriteOp submits to the
active writer via a recording wrapper around
`_context.get_active_writer()`, run the service function, assert
at least one submit happened (i.e. the dispatch path was taken).
Each of the 12 handlers gets one such test. End-to-end DB-state
correctness is gated by the existing handler-level tests in
`tests/test_broker/test_admin_ops.py` which assert post-RPC
state and continue to pass unchanged.

Subtleties surfaced during the migration (worth durable
documentation):

- **`_publish_names` requires a Session.** Every legacy `inboxes.py`
  write function ended with `_publish_names(session)` to refresh the
  in-memory `_INBOX_NAMES` nav cache. Writer closures receive a
  Connection, not a Session, so Phase 5's writer-path branch calls
  the existing `refresh_inbox_names()` helper after `future.result()`
  (a few ms after the commit lands; acceptable for a nav cache).
  Legacy fallback paths continue to call `_publish_names(session)`
  inline.

- **Two-call-chain functions fold into one closure.**
  `add_tracked_author` and `remove_tracked_author` previously opened
  two separate `SessionLocal()` contexts (first for `get_inbox`,
  then for `set_tracked_authors`). The writer-path closures combine
  both into one closure operating on the writer's `conn`: SELECT
  current `tracked_authors`, mutate in Python, UPDATE in SQL. This
  is atomic per-transaction (vs the legacy two-transaction shape's
  brief TOCTOU window, and the broker is single-writer so the window was
  never observably racy). Net: writer path is slightly more correct
  than the legacy path.

- **`write_transaction()` is dead code on the writer path for
  robots functions.** `write_transaction` is a ContextVar-gated
  event listener that fires on the shared engine's `connect` event.
  The WriterThread uses its own engine (`WriterThread.from_settings()`),
  not the shared pool, so the listener doesn't fire on writer-thread
  connections. The `with write_transaction(...)` wrapper in the
  legacy robots functions becomes a no-op on the writer path; the
  fallback path keeps it (and DOES use the shared engine). The
  writer path doesn't include `write_transaction()` in its closure.

- **WriterThread engine `_sqlite_pragmas` registration (resolved).**
  Phase 5 originally shipped with the WriterThread's
  engine missing the `_sqlite_pragmas` event listener, so
  `PRAGMA foreign_keys=ON` was never issued on writer-thread
  connections and `delete_inbox`'s `DELETE FROM inboxes` (which
  depends on `ondelete="CASCADE"`) relied on an inline
  `conn.exec_driver_sql("PRAGMA foreign_keys=ON")` workaround. The
  structural fix has since landed: `WriterThread._run()` calls
  `event.listen(engine, "connect", _sqlite_pragmas)` at engine
  creation (`mimir/broker/writes.py`), so the writer's connections
  now carry the same pragmas as the shared engine (foreign_keys,
  synchronous=NORMAL, analysis_limit, busy_timeout). The listener
  skips `query_only=1` when `mimir_is_broker=true`, so the writer
  stays writable. The inline workaround in `delete_inbox` was
  removed; the closure relies on the registered cascade pragma.

Explicitly out of Phase 5 scope (Phase 6, see the corrected scope
note above): migrating the residual in-broker shared-engine
writers (analyze, residual `cache.set` callers, bootstrap,
load_maintainers) onto the WriterThread, settling VACUUM's
exclusive-lock contract, then removing `write_transaction()` + the
BEGIN-IMMEDIATE event listener + the `_direct_*` helpers, and
collapsing the cache worker thread into the unified writer path.
The purge timer is already migrated (no longer a residual).

## Threading via recursive CTE

Thread reconstruction lives in `mimir/threading.py` and uses two SQLite
recursive CTEs against the `thread_parent` graph. Each message's root
is ALSO materialised, per inbox, in `article_lists.thread_root_id`
(see "Materialised thread roots" below); the CTEs remain the
definition of correctness and the fallback for rows the column has
not reached.

`Article.thread_parent` is a best-guess "effective parent" computed at
ingest time as `in_reply_to OR references[-1]`. Some mailing-list
software strips `In-Reply-To:` but keeps `References:`; falling back to
the last entry of References recovers many otherwise-orphaned threads.
On the lkml corpus, this recovers ~67k messages (~1.3% of the archive)
that have no `In-Reply-To` but do have `References`. The raw
`in_reply_to` column is preserved for completeness.

- `find_thread_root(session, message_id)` walks **up** via
  `thread_parent`, picking the deepest ancestor present in the DB. A
  message with no parent (or whose parent isn't in our archive) is its
  own root. ~2-3 ms when measured against a 6.2M-row DB; production
  is now 17,681,882 articles (measured 2026-10-02) and this latency
  has not been re-measured since, so treat it as a floor.
- `get_thread(session, root_message_id)` walks **down** from the root,
  building a `sort_path` column inside the CTE
  (`<date>|<id>` segments joined by `/`). Ordering by `sort_path` gives
  a clean depth-first traversal with siblings ordered by date, no
  post-processing needed in Python. ~2 ms for a 71-message thread.

Both CTEs cap recursion at depth 1000 to defend against pathological
cycles in the data (real lkml threads rarely exceed ~50 deep).

### Materialised thread roots (2026-07-28)

The materialised-column approach was considered and rejected for
years, on the grounds that it needs out-of-order arrival handling
(replies before parents, across epoch boundaries), a backfill, and
ingest-time complexity, none of which the CTE needed. That reasoning
was correct while the only consumer was a page render, where 2-3 ms
per lookup is invisible.

What changed the trade is the sitemap. Listing one entry per
conversation with a truthful `<lastmod>` means enumerating every
thread root in an inbox, which is a per-row question the CTE can only
answer by walking each row. So the column shipped, with the three
costs paid explicitly rather than avoided:

- **It lives on `article_lists`, not `articles`.** Threading is
  inbox-scoped: `find_thread_root(session, inbox, mid)` walks only
  within one inbox, so a cross-posted message genuinely has different
  roots in different inboxes, and a column on `articles` would be
  silently wrong for a large fraction of this corpus. A root points at
  ITSELF, so "is a root here" is `thread_root_id = article_id` with no
  NULL-means-root ambiguity.
- **NULL means "not yet computed", never "is a root".** That is what
  lets it ship without a blocking migration: readers fall back to the
  CTE wherever it is NULL. Every repair path keys on NULL, so a
  plausible-but-wrong non-NULL value is strictly worse than none, and
  the write path is written to prefer NULL when unsure.
- **Out-of-order arrival is handled at ingest**, by invalidating a
  subtree rather than guessing (`ingest/_pending.py`).

The CTE is still the oracle: `verify_thread_roots` recomputes against
`_find_thread_root_cte`, deliberately NOT against `find_thread_root`,
which reads the column and would agree with any corruption by
construction.

`text()` raw SQL bypasses SQLAlchemy's type coercion, so the `date`
column comes back as an ISO string from the CTE. `_coerce_dt` parses
it back to `datetime` before the row is wrapped in a `ThreadNode`
dataclass.

### Multi-inbox support

The `inboxes` and `article_lists` tables (see "Schema decisions"
above) let mimir index any number of public-inbox archives in the
same DB. `Settings.inboxes: dict[str, Inbox]` configures them via
env; `bootstrap_inboxes()` reconciles the env to the DB on startup
(insert-only, never clobbers admin-managed rows). URLs are namespaced
as `/<inbox_name>/…`; every dashboard / search / archive query joins
through `article_lists` to filter to the inbox in scope.

`/` is the meta-index: lists every configured inbox with per-inbox
stats (total messages, date span, epoch count). `/<inbox_name>/` is
the per-inbox dashboard. Threads, subject grouping, and Message-ID
linkification are all scoped within an inbox, references that reach
into another inbox render as plain text rather than producing
cross-list links (rare in practice). For *cross-posts*, the article
itself is shared but the rendered inbox is whichever inbox the URL
specified; a "Also in: …" line on the message page links to the
other inboxes' versions.

**SQLite query-planner gotcha**: indexes on low-cardinality columns
(one row per inbox) lead the planner astray on recursive-CTE joins
unless `sqlite_stat1` is populated. We observed >5 minutes for one
message view before `ANALYZE`, 8 ms after. The auto-ANALYZE pass
(see "Auto-ANALYZE after threshold-crossing ingest") handles ongoing
freshness; the `analyze` CLI command is available for manual runs.

### Cross-process cache (`mimir/cache.py`)

`mimir/cache.py` is a DB-backed cache for slow dashboard queries
(`active_threads`, `archive_stats`, sitemap, etc.). Values are
JSON-encoded and stored in the `cache` table keyed by string with a
unix-second expiry. SQLite's WAL lock makes the multi-process case
(Flask server + warm-cache cron + scheduler sidecar) atomic without
any extra coordination.

This was a pivot from an earlier single-pickle-file scheme. The
pickle file worked but had two real problems: pickle is unsafe to
load from untrusted writers (any code path that could write the file
could RCE on the next read), and atomic-rename writes don't compose
across processes, concurrent writers' rename order was a
last-writer-wins race that lost cached entries on warm-up. The DB
table sidesteps both: rows are scalar JSON (no code execution
surface) and SQLite's transaction isolation is the right primitive
for multi-writer correctness.

Only types registered via `cache.register(tag, cls)` round-trip
cleanly. Each module owning a cached dataclass calls `register` at
import time; the dependency stays one-way (cache knows nothing about
its callers).

Every key is silently prefixed with `v{NAMESPACE_VERSION}:` so a
code change that alters cached value shapes (a query rewrite, a
renamed dataclass field) is bumped centrally, old rows simply never
match and age out via `purge_expired`.

`cache.get_or_compute(session, key, ttl, fn)` is the high-level API;
`force=True` on dashboard helpers bypasses the cache and recomputes
(used by `warm-cache`).

`cache.set` is **best-effort**: a `sqlalchemy.exc.OperationalError`
on commit (the SQLite "database is locked" path, after the
connection's `busy_timeout` window has elapsed) is logged at warning
and swallowed. The page already rendered before `set` was called, so
propagating the error would 500 a successful response; the next
request just recomputes and tries again. Encoder / programming
errors are *not* caught, they should still raise loudly.

### Landing page (`/`)

The dashboard is a stack of independent surfaces, each driven by its
own helper in `mimir/dashboard.py` (or `mimir/threading.py` for the
thread-aware ones):

1. **Most active threads**, `active_threads` from `threading.py`,
   recursive CTE, 5-min cache. ~700 ms cold, ~3 ms warm.
2. **Latest pull requests**, `subject LIKE '[GIT PULL]%'`, ORDER BY
   date DESC LIMIT 5. Reverse date-index scan, short-circuits early.
3. **Stable / release announcements**, `subject GLOB 'Linux [0-9]*'`,
   matches both Linus's mainline `Linux 6.13-rc5` and Greg's stable
   `Linux 6.12.4`.
4. **Per-author trackers**, `author_recent(substring)` driven by
   the active inbox's `Inbox.tracked_authors` column (JSON dict of
   label → email substring, managed via `admin inbox trackers`).
   Each query is ~14 ms (date index reverse scan + LIKE filter, fast
   because allowlisted maintainers post often).
5. **This day, 5 years ago**, `Article.date BETWEEN today-5y AND
   today-5y+1d`, ordered by `RANDOM()`. Cheap; the day-window is small.
6. **Recent messages**, last 10, no filters.
7. **Daily volume sparkline**, `daily_volume(days=30)`, GROUP BY
   `date(date)` over the last 30 days, zero-filled in Python so empty
   days still get a tick. Rendered as inline SVG with `currentColor`
   so it adapts to Pico's light/dark theme. ~25 ms uncached, cached 1h.
8. **Archive stats footer**, `archive_stats`, `COUNT(*) + MIN/MAX(date)
   + COUNT(DISTINCT epoch)`, cached 24h. The `COUNT(*)` is the slow
   piece (~6 s when measured at 6.2M rows), hence the long TTL. The
   corpus is now 17,681,882 articles (measured 2026-10-02) and that
   `COUNT(*)` has not been re-timed, so the real figure is larger by
   an unmeasured amount. Do not size anything off the 6 s.

Layout uses Pico's `.grid` class for two-column sections (pulls/stable,
trackers); on narrow viewports they stack automatically. Active threads
and recent messages stay full-width since they're the primary content.

Trackers are **per-inbox state**, not a settings-level dict. The
`Inbox.tracked_authors` JSON column is mutated through
`admin inbox trackers {show,set,add,remove,clear} <inbox>` (service
layer in `mimir/inboxes.py`); cache invalidation for the affected
inbox happens on every write. Adding a tracker shows up as a new
tile on that inbox's dashboard without touching code or env. The
older `Settings.tracked_authors` knob (a single global dict) was
removed once mimir went multi-inbox: a single tracker list across
inboxes was the wrong shape (lkml's `Linus` tracker is meaningless
on `linux-fsdevel`).

### Off-list-parent list hint

When a thread root has an off-list parent, the thread tree's
"off-list ancestor" line is wrapped in a Pico `data-tooltip`
carrying `hint: linux-arm-kernel@lists.infradead.org` (and
others, comma-separated, capped at 3). The address surfaces only
on hover/focus, Pico renders a dotted-underline trigger and the
tooltip popup; the visible line stays compact. Source:
`canonical.extract_list_addresses` over `parsed.headers`,
filtered against the set of configured `Inbox.list_address`
values.

`data-placement="bottom"` is mandatory on this trigger: the
off-list-ancestor row is the first `<li>` inside `.thread-box`
which has `overflow-y: auto`, so a default top-positioned
tooltip is clipped by the box's top edge and is unreadable.
Bottom placement renders the tooltip into the visible scroll
area below the trigger.

It's a *hint*, not a claim: the parent could also simply predate
the indexed window, in which case the To/Cc addresses point at
lists where the parent might not be either. We use the displayed
message's headers (already in scope from `read_message`) rather
than re-parsing the thread root, which means a deep reply that
dropped the list from Cc renders no hint even when the root
would have. Acceptable trade for skipping a second blob fetch on
every message page; revisit if the heuristic misses too often in
practice.

### Subject-based fallback grouping (related orphans)

`Article.subject_normalized` is the Subject with leading reply/forward
prefixes stripped (`Re:`, `Fwd:`, `Aw:`, `Sv:`, `Antw:`, `Wg:`, `Tr:`,
case-insensitive, applied repeatedly so `Re: Re: Fwd: foo` reduces to
`foo`), lowercased, whitespace-collapsed. `[PATCH v3 1/N]`-style tags
are deliberately **kept**, they distinguish patch revisions which we
want to treat as related-but-distinct threads.

When a thread's root has an off-list parent (so the conversation
clearly continues outside our archive), the message view queries other
articles with the same `subject_normalized` not already in the current
thread, and surfaces up to 5 as "Possibly related." This is JWZ's
subject-based grouping, looser than reference-based threading on
purpose, but useful precisely for the orphan case where references
fail.

Indexed and computed at ingest in `_to_article`. Existing rows are
backfilled by a one-shot Python loop because SQL doesn't easily express
the regex-based prefix stripping.

### Active-threads ranking with recency decay

`active_threads` orders by a half-life-weighted score, not raw
COUNT(*). Each recent message in a thread contributes
`pow(0.5, julianday('now') - julianday(message.date))`, roughly: 1.0
for "this hour," 0.5 for "yesterday," ~0.008 for "a week ago." The sum
across the thread's messages is the ranking score. The raw count is
still shown in the UI ("N messages in 7d") for context. Effect:
threads with bursts of activity *today* outrank older threads with a
larger total count, which is what "active" should mean.

### Quoted-block collapse

Quoted text rendering (`mimir/rendering.py`) wraps quotes at depth ≥ 2
in `<details>` (collapsed by default). First-level `>` quotes stay
plain `<blockquote>` so the immediate context of a reply is always
visible; `>>` and deeper get the `<details>` toggle so a wall-of-quoted-
history doesn't dominate the page. `<details>` is native HTML, no
JS or HTMX dependency.

**Two caps bound it, and they bound different things (3.9.0).**
Quote rendering recurses once per level, so the nesting depth was the
*sender's* to choose and the C stack was the only limit: a crafted
post hit `RecursionError` at roughly 497 levels (lower inside a
request) and 500'd its own page, permanently, because the body is
re-derived from the mirror on every read and the view is public and
unauthenticated.

- `MAX_QUOTE_DEPTH` (64) bounds a *single* block. Past it the quote
  renders **flat with a summary line naming the depth, rather than
  truncated**, so no text is dropped. That choice has a sharp
  consequence: flattening has to strip the remaining `>` prefixes,
  because `_TRAILER_LINE_RE` is anchored at line start, and leaving
  them on would have silently disabled DCO trailer redaction past the
  cap. A cap that quietly turns off a redaction is worse than the
  crash it prevents.
- `MAX_QUOTE_LEVELS_PER_RENDER` (512) bounds the *whole render*,
  across every block in the message. The depth cap alone is not
  enough because a body may hold unboundedly many blocks: quote
  rendering still amplified a body about 89x into HTML (measured
  1.05 MB in, 93 MB out, linearly), which against the 50 MB message
  ceiling reaches gigabytes on an uncached public page. With the
  budget it is about 3x; a 4 MB adversarial body renders in 1.2 s at
  46 MB peak instead of 9.6 s at 1.1 GB.

Measured headroom, so the caps are known to be far above real mail
rather than guessed: deepest nesting across 1,000 messages from two
inboxes (2026-09-29) was **7**; worst total levels observed in real
mail is **44**, p99 is **14**.

### HTMX "load more" on Recent

`/api/recent?offset=N` returns a `_recent_items.html` partial: 10 next
articles plus a trailing `<li class="recent-more-trigger">` containing
the next "Load more" button. HTMX swaps `outerHTML` on the trigger's
`<li>`, so the response replaces it with new items + a fresh trigger
(or nothing, when the page exhausts). One round-trip per click,
indexed `LIMIT/OFFSET` queries with `LIMIT+1` to detect "has more"
without a `COUNT(*)`.

### Attachment routes

`GET /<inbox_name>/<YYYY>/<MM>/<article_id>/attachment/<n>` and
`.../attachment/<n>/preview`. Both fetch the message via
`mimir.store.read_message` (re-parses the git blob), index into
`parsed.attachments[n]`, and either serve the bytes (download, with an
RFC 6266 Content-Disposition that escapes non-ASCII filenames) or
hand the decoded text to Pygments (preview).

Pygments lexer choice: `get_lexer_for_filename` first, fallback to
`guess_lexer`, fallback to `TextLexer` so we never raise on weird
content. Previewability is heuristic on content-type + filename
extension; binary attachments hit the preview route still get a clean
"can't preview, here's the download link" page rather than a broken
hex-dump.

### Daily views (`/<inbox_name>/today`, `/<inbox_name>/yesterday`)

Per-epoch listings were rejected: epoch numbers (0.git, 1.git, …) are a
public-inbox storage chunking artifact with no semantic meaning to a
reader. Date-scoped views are the right shape for "what happened on a
day."

`/<inbox_name>/today` and `/<inbox_name>/yesterday` render
`daily.html`: heading
with the date, summary count line ("N messages across M threads"),
and **every** thread that had activity that day, ordered by last
activity desc. Unbounded, no top-N cutoff, because the author
explicitly wants the full day's grouping, not a curated subset.

`threads_for_day(session, day)` shares the recursive CTE with the
front-page `active_threads()` via the shared `_active_threads_query`
helper, parameterized on `(start, end)` and on ordering: the front
page uses decay-weighted score (`order_by="score"`), the daily view
uses plain recency (`order_by="last_activity"`) since decay is
irrelevant inside a single 24h window. Daily windows are cheap
(~50-100 ms uncached for ~500 messages / 200-300 threads on a typical
day); caching is mostly about consistency with the broader 7-day
surface than necessity.

Nav links live in `base.html` so they're discoverable from anywhere,
not just `/`. A `current_inbox` context processor injects the active
inbox name into every template render so the nav doesn't have to
receive it explicitly per view.

### Most active threads (`/`)

`active_threads(session, days, limit)` powers the landing page's "most
active threads" surface. Algorithm: a recursive CTE finds each
recent-window message's topmost in-DB ancestor (same walk-up logic as
`find_thread_root`, but over many starting points at once), groups by
root, and ranks by a half-life-weighted score (see "Active-threads
ranking with recency decay" above). Raw COUNT(*) is still kept and
shown in the UI for context.

Performance is borderline, ~700 ms for a 12k-message window measured
over a 6.2M-row archive (now 17,681,882 articles as of 2026-10-02,
and not re-timed), so results live in the disk cache (5 min TTL) and
get pre-warmed by `flask --app mimir warm-cache`. If it becomes a real
concern, the materialised root column now exists (see "Materialised
thread roots"), though on `article_lists` rather than on Article,
because the answer is per-inbox. Reworking `active_threads` to use it
is an open opportunity, not yet done.

## Web UI stack choice

Flask + Jinja2 (already in the project) + Pico CSS + HTMX (both from
CDN; no build step) + Pygments for server-side syntax highlighting.

Heavier alternatives (FastAPI, Tailwind + a build pipeline, React)
were considered and rejected, they'd add operational and code weight
without buying anything for a read-only archive browser of this size.

The body rendering pipeline (`mimir/rendering.py`) does:
- Block segmentation: `text` / `quote` / `diff` runs.
- Recursive `<blockquote>` for nested quotes (`>>>>` → 4 deep).
- Pygments `DiffLexer` for inline patches. The diff-start regex
  matches both git-style `--- a/` and pre-2000s lkml `--- foo.c.orig`
  formats.
- Linkification of URLs and `<Message-IDs>`. Cross-archive Message-ID
  links use `url_for('web.message', ...)` so they survive any future
  URL-scheme change.

URL scheme: `/<inbox_name>/<YYYY>/<MM>/<article_id>` (e.g.
`/lkml/2024/01/12345`). Three reasons for the shape:

- **Inbox namespace up front**, works for any configured inbox.
  Routes validate the inbox slug exists in the `inboxes` table;
  queries join through `article_lists` to filter by inbox.
- **At-a-glance age-gauge**, you can see how old a message is from the
  URL alone (browser bar, bookmarks, shared links, log lines), without
  having to load the page.
- **Privacy**, Message-IDs leak email information. Some encode just
  the sender's host (`...@example-domain.org`); some encode the
  literal local-part, so the Message-ID carries a real person's name
  and employer (`...20850618...-Given.Family@vendor.example.com`).
  Both shapes are common in the real corpus; the examples here are
  anonymised on purpose, since republishing a live address in a
  tracked file is the exact harm this bullet argues against. Putting
  them in URLs
  means they end up in browser history, server logs, bookmarks, and
  shared links, defeating the From-line obfuscation work entirely.
  Using the integer `Article.id` instead, already a primary key, no
  schema change, keeps URLs opaque and shorter. The Message-ID stays
  internal: still used for thread joins, cross-archive linkification
  lookups, the `show` CLI, and as a stable identity across re-ingests.

List, year, month, and article-id are all part of the URL identity:
mismatches return 404 rather than redirect, so a URL either resolves
exactly or doesn't resolve at all. One exception (#648): when the
article exists and the date matches but the list is wrong, the message
route answers 301 to the article's canonical message URL, so links
collected from the misassigned-list-address bug (#644) consolidate
instead of 404ing. A wrong date, an unknown id and an unknown list
stay 404, including a wrong list combined with a wrong date.

Cross-archive Message-ID linkification (in body text) requires the
view to resolve referenced Message-IDs via a single bulk SELECT before
rendering, building a `{message_id → url}` dict that's threaded through
the renderer. The dict's *values* are the int-based URLs (built via
`_msg_url`), so the leak doesn't return through the back door.
Message-IDs not in the archive render as plain text rather than dead
links.

## Canonical-inbox resolution (cross-posts)

Same message, multiple inboxes (e.g. lkml + linux-fsdevel) is
common. Search engines treat the same content at multiple URLs as a
duplication signal, diluting ranking. So each article needs one
*canonical* URL, and we have to pick which inbox owns it.

Strategy: read the author's intent from the RFC 5322 `To:` / `Cc:`
headers. The first list-shaped address there is what they sent the
message to; later ones are cross-post recipients.

The implementation has three phases, each in its own pass over the
data:

1. **Observation tally (ingest-time, `mimir.ingest`).** As each
   article lands, every list-shaped To/Cc address is bumped in
   `inbox_address_observations` for the inbox we're writing to.
   `canonical.is_list_address` does conservative suffix filtering
   (`vger.kernel.org`, `lists.linux.dev`, `lists.freedesktop.org`,
   etc., see `LIST_HOST_SUFFIXES`) so personal addresses, vendor
   replies, and bot accounts don't pollute the tally.
2. **Auto-promotion.** When an inbox crosses
   `MIN_PROMOTE_OBSERVATIONS = 50` observations and the top address
   covers ≥70% of the **top-two combined** (`PROMOTE_DOMINANCE = 0.7`,
   evaluated as `top / (top + second)`), that address becomes
   `Inbox.list_address`. The top-two ratio is gentler than 70%-of-
   total on a noisy inbox with several near-equal addresses; we'd
   rather pick the higher one and let the operator override than
   leave the slot NULL indefinitely. Bootstraps without hardcoded
   name→address tables; new inboxes self-identify after a short
   warm-up. Operator can override via `admin inbox update`.
   Promotion skips an address another inbox already holds, because a
   small list's To/Cc is often dominated by cross-posts to a bigger
   one (#647). The first inbox to promote an address keeps it, so on
   a from-scratch ingest (alphabetical) cocci can take lkml's address
   before lkml does; an operator corrects that with `--list-address`.
3. **Per-article pinning.** With `Inbox.list_address` populated,
   `pick_canonical_inbox_id` walks the message's To/Cc addresses
   in order and returns the first inbox whose `list_address`
   matches, **with a two-tier preference**: an inbox listed in
   `Settings.canonical_demoted_inboxes` (default `["lkml"]`) only
   wins as a fallback when no non-demoted match is found later in
   the walk. So `Cc: linux-kernel@vger.kernel.org,
   linux-arm-kernel@lists.infradead.org` pins to linux-arm-kernel
   even though lkml is positionally first. NULL is allowed (and
   common during warm-up); render-time falls back to the
   alphabetically-first inbox among the article's `lists`, with
   the same demoted-to-back ordering.

The demotion encodes that lkml (and any operator-configured
firehose) is a general-visibility CC, not the conversational home
of a thread; the topical list is. Original posture was "first
list-shaped address wins, regardless"; field-tested against
Google's canonical pick on production cross-posts, the search
engine consistently selected the topical list, and the operator
agreed that's the right signal (the topical list is where review
happens, lkml is the firehose). The convention generalises to any
inbox that's a broadcast catch-all, not just lkml; operators
extend via `CANONICAL_DEMOTED_INBOXES` if they add more
firehose-shaped lists.

Backfill for already-ingested articles runs via
`admin canonicals backfill`, one pass per existing article with
the resolver. Idempotent; re-runs are safe.

The Article→canonical_inbox FK is `ON DELETE SET NULL` (not
CASCADE) so removing an inbox doesn't strand or drop articles  
they just lose their canonical pin and fall back to the
alphabetical rule.

Render-time, every page that has one canonical URL emits it as a
`<link rel="canonical">`, an `og:url`, and as the `url` field of
the JSON-LD entity. The unscoped Message-ID redirect (`/m/<id>`)
also routes 301 → the canonical inbox's URL, saving crawlers a hop
and consolidating link equity on the canonical destination.

## SEO posture

mimir is publicly indexed (production deploy at ratatoskr.run), so
we treat search-engine signals as a real interface, not an
afterthought:

- **Per-page `<title>` and `<meta name="description">`.** Captured
  via `{% set page_title %}{% block title %}…{% endblock %}{% endset %}`
  in `base.html` so the same string flows through `<title>`,
  description, OG, and Twitter Card tags without per-template
  duplication. Title pattern is `<page-specific> | <inbox> | <site>`,
  except the message page, which is `<subject>(truncate 50) | <site>`
  (the inbox segment was dropped in 3.6.3 to keep message titles under
  the ~65-char SERP budget; Bing "title too long"). Every content page
  leads with exactly one `<h1>` (the page's primary heading, e.g. the
  message subject in the `_message_body.html` partial); base.html and
  the nav emit no `<h1>`. Seven templates that had led with `<h2>` were
  corrected in 3.6.3 (Bing "the `<h1>` tag is missing"). Guarded by
  `test_message_page_has_single_h1` and the `test_search.py` lead-with-h1
  assertion.
- **Open Graph + Twitter Cards.** `og:type=article` on message
  pages, `website` everywhere else. URL previews on Slack /
  Discord / Mastodon / etc. render with proper title + description.
- **schema.org JSON-LD.** `WebSite` on `/`, `@graph` of
  `DiscussionForumPosting` + `BreadcrumbList` on message pages.
  Renders against the canonical inbox so cross-posts collapse to
  one entity. Eligible for Google's "Discussions and forums" rich-
  results section. Author goes through `_safe_from_filter`, same
  redaction as the rendered page, and `datePublished` prefers
  `parsed.date` (the original RFC 5322 Date header) over
  `article.date` (which is the public-inbox commit time, see above).
- **Sitemap** at `/sitemap.xml` with `<lastmod>` (per-thread latest
  activity for thread entries, per-inbox latest, global latest for
  the meta-index). Cached 1h
  via `cache.get_or_compute`. The index is a single-level
  `<sitemapindex>` referencing urlsets only: `/meta-sitemap.xml`,
  the per-inbox `/<inbox>/sitemap.xml`, and `/sitemap-maintainers.xml`
  (one urlset of every `/maintainers/<address>` page). The
  maintainers layer is deliberately ONE urlset, not a nested index
  and not per-maintainer files: Google rejects a nested sitemap index
  ("Incorrect sitemap index format: Nested sitemap indexes"), so the
  "group inboxes vs maintainers" tree can't be expressed as nested
  sitemap indexes. A flat index of urlsets is the shape that gets
  crawled.

  **Per-inbox time granularity is adaptive (3.9.0, #551).** The
  per-inbox layer is not one urlset per inbox: it is time-sliced, and
  an inbox-year is emitted as a single *yearly* urlset when it holds
  at most 20,000 thread roots AND its expanded thread-page URLs also
  fit under the 50,000-URL limit. Years failing either test stay
  monthly. Both conditions are needed because the two counts differ:
  pagination means one thread root can expand to several thread-page
  URLs, so a year can pass the root test and still breach the URL
  limit. Existing monthly URLs keep working, and a yearly urlset
  supports paging once it grows. The point is index headroom, since
  the flat-index constraint above means the index itself cannot be
  subdivided by nesting. 3.10.0 then keyed these caches by the thread
  render cap, because the cap changes how many thread-page URLs a
  root expands to; see the ETag section for the surface that was
  missed the first time.

  Note the flat-index constraint is Google's, not the protocol's:
  sitemaps.org explicitly permits more than one index file. Every
  copy of this claim in the tree credited sitemaps.org until
  2026-08-04, which is the propagation the "grep the repo for the same
  claim in prose" rule exists to catch.
  The maintainers urlset carries no per-url `<lastmod>` (deriving a
  per-maintainer date is ~4000 queries for a hint crawlers treat as
  secondary); the index entry reuses the already-computed
  `global_latest`. **Sitemap responses send no HTTP conditional-GET
  validator (no `Last-Modified` / `ETag`, always 200; 3.6.1).** They
  used to set `Last-Modified` to the newest article date and 304 on
  `If-Modified-Since`, but that validator is a lie about *structural*
  changes: it advances only when a newer article lands, not when a
  deploy changes the sitemap's shape. 3.6.0 added the
  `/sitemap-maintainers.xml` entry to the index without moving the
  date, so Cloudflare's edge (revalidating after `max-age=300`) got a
  304 and served the pre-deploy index for hours; crawlers doing
  `If-Modified-Since` were pinned the same way. Freshness now rides the
  in-XML `<lastmod>` (the SEO-relevant signal) plus `Cache-Control:
  max-age=300` (set per-endpoint in `hooks.py` for every sitemap
  surface, including maintainers), so edges cache briefly then
  re-fetch a full 200. The lost HTTP-304 bandwidth on a small,
  origin-cached, edge-cached XML doc is negligible against silently
  serving a stale structure. Reason a `<lastmod>`-plus-max-age policy
  is correct where a conditional-GET one isn't: the former can't pin,
  it only delays, bounded by `max-age`. (Note when smoke-testing prod:
  Cloudflare *synthesizes* a `Last-Modified: <fetch-time>` on the MISS
  response because the origin sets none, so `curl` against ratatoskr.run
  shows a `Last-Modified` that equals the `Date` header and advances on
  every fetch. That is the CDN's, not mimir's, and it's harmless, a
  fetch-time stamp can't pin the way a content-date could. The decisive
  check is that a conditional GET returns **200, never 304**.)
- **301, not 302**, on Message-ID lookup redirects (`/m/<id>` and
  `/<inbox>/m/<id>`). Cache-Control headers are also set on the
  redirect responses.
- **robots.txt and security.txt** at root and `/.well-known/`.

## CI flow: PR-gate + tag-publish, loose develop (Tier C, 2026-06-14)

CI is two workflows with one job each kind. `ci.yml` is the **test
gate** and runs on `pull_request` only (`[develop, main, release/*,
hotfix/*]`): `lint` / `test` / `test-ft` plus a docker **build-verify**
(`push: false`). `release.yml` is the **publish** and runs on `push:
tags: ['v*']` only: it builds + publishes the GHCR image
(`:vX.Y.Z`/`:X.Y.Z`/`:X.Y`/`:latest`) and runs no tests.

Why this shape (the reasoning, after two incremental cuts):

- **A branch in both `push` and `pull_request` triggers double-runs CI
  on the same commit.** The `concurrency` group keys on `github.ref`,
  which differs for a branch push (`refs/heads/...`) and its PR
  (`refs/pull/N/merge`), so the two never dedupe. Listing
  `release/*`/`hotfix/*`/`main` under `push` made every release cut run
  the ~11-min `test`/`test-ft` jobs two-to-three times on identical
  code. The fix: the PR is the single test gate; branch pushes are not
  triggers at all.
- **The tag trusts the PR gate.** The `v*` tag sits on the
  `main`-merge commit, whose tree (solo repo, no races onto `main`) is
  exactly what the `release->main` PR already tested, and deps are
  `--frozen`. So `release.yml` rebuilds + publishes the image (which
  also re-checks the Dockerfile/deps at tag time) without re-running
  the Python suite. A release's code is tested once, on its PR.
- **`develop` is a loose integration branch; `main` is the hard
  gate.** `develop` has no required checks and allows direct pushes, so
  the release back-merge (and the occasional comment-fix) is a silent
  direct push with no CI; feature work goes through PRs by convention
  and gets advisory CI. `main` requires `lint`/`test`/`test-ft`/`docker`
  and a PR. Loosening `develop` is what lets the checkless back-merge
  push land (GitHub blocks a checkless direct push to a
  required-checks branch), and the maintainer is the sole admin, so
  `develop` is convention-with-discipline.

Full design + the rejected alternatives (fast-smoke-on-tag,
keep-develop-gated, query-check-runs) are in
`_claude/specs/2026-06-14-ci-flow-tier-c-design.md`. The release flow's
Step 9 (tag -> `release.yml`) and Step 10 (direct-push back-merge)
in the portfolio's `MIMIR-RELEASE-FLOW.md` follow from this.

## Production deployment posture

The reference deployment serves the app from a container on a host
with no public address, reached through a chain of proxies: a CDN at
the edge, then an L4 stream proxy that passes TCP and UDP :443
through without terminating TLS, then a private tunnel, then a
reverse proxy that terminates TLS and forwards to the container.

Three properties of that shape are what the code actually depends
on, and they generalise to any similar arrangement:

- **Client TLS is end to end from the CDN to the innermost reverse
  proxy.** The L4 hop never sees plaintext, which is why it is not a
  header-modifying hop (see `TRUSTED_PROXY_HOPS` below).
- **The L4 hop prepends PROXY-protocol v1 on TCP** so the reverse
  proxy still learns the real client address for HTTP/2. The
  UDP/HTTP-3 path deliberately omits PROXY, because QUIC
  PROXY-protocol parsing proved brittle; the cost is that HTTP/3
  requests present the tunnel address as their source.
- **The innermost hop is the only one that sees the application.**

The specific hosts, addresses, tunnel parameters and proxy tuning
are deliberately not in this file; they are operator surface and
live in the untracked `_claude/CLAUDE.md` alongside the post-deploy
smoke.

Decisions specific to that shape:

- **Schema migration ownership lives on the broker** (since
  v2.0.0; was the `mimir-tasks` sidecar through v1.x). The
  `mimir-broker` container's startup runs `alembic upgrade head`,
  gated by a `/data/.migrated` sentinel file with a healthcheck.
  Web + tasks `depends_on: condition: service_healthy` on the
  broker so they only start after migrations land. Single source
  of DDL truth on the single-writer process, no race between
  parallel `alembic upgrade head` invocations on cold start.
- **Inbox bootstrap also lives on the broker.** Same principle
  as the migration ownership rule above: writes belong on the
  one writer process, every other tier opens `query_only=1` at
  startup. The broker runs `mimir bootstrap-inboxes` right after
  `alembic upgrade head` and before touching `/data/.migrated`,
  so the web tier comes up to a populated `inboxes` table
  without ever issuing a write itself. Idempotent via `ON
  CONFLICT (name) DO NOTHING`; admin edits to existing rows are
  never clobbered.
- **Post-migrate ANALYZE lives on the broker.** Migrations can
  introduce new indexes whose `sqlite_stat1` entries don't get
  populated as a side effect. After `alembic upgrade head` and
  `bootstrap_inboxes`, the broker runs a bounded ANALYZE
  (`analysis_limit=4000`, ~10.8 s on the production corpus as measured
  2026-08-04; it was ~1-3 s when the corpus was 11M rows)
  gated on a `/data/.broker_initial_analyze` sentinel so
  subsequent restarts skip it. Web tier doesn't start serving
  until the broker's healthcheck flips green, the right
  ordering: serve queries only once the planner has stats for
  the schema.
- **`tini` runs as PID 1 in every container role.** Long-op
  handlers (mainline + sync) shell out to `git fetch` /
  `git clone`, and `git` spawns helper grandchildren
  (`git-remote-https` and friends) that often outlive the
  direct git child. Without an init shim those grandchildren
  reparent to PID 1 when the direct git exits, and a Python
  process at PID 1 with the default SIGCHLD disposition
  doesn't reap them, accumulating `[git]` zombies for the life
  of the container (observed 2026-05-29: ~30 zombies after
  35 h uptime on `mimir-broker`, ~6-7 per `update_mainline`
  tick). Installing tini via the Dockerfile ENTRYPOINT
  delegates orphan reap to a small, purpose-built init while
  leaving `subprocess.run(..., check=True)`'s ability to
  detect *direct-child* git failures intact, the in-process
  alternative (`signal.signal(SIGCHLD, SIG_IGN)`) defeats
  that detection because Linux turns the auto-reaped child's
  status into a returncode=0 via CPython's
  `_try_wait`/`ChildProcessError` path. The trade-off (extra
  ~250 KB binary in the image, one indirection on every
  container start) is trivial against the silent-git-failure
  risk of the SIG_IGN alternative.
- **`TRUSTED_PROXY_HOPS=N` env var** wraps the WSGI app in
  `werkzeug.middleware.proxy_fix.ProxyFix(x_for=N, x_proto=N, x_host=N)`
  so `request.remote_addr`, `request.is_secure`, AND `request.host`
  reflect the real client + the public host across N reverse-proxy
  hops. In the reference deployment `N=2`, counting the CDN and the
  innermost reverse proxy; the L4 stream hop does not touch HTTP
  headers, so it does not count. `x_host` was
  added so absolute URLs derived from `request.url_root` (sitemap
  entries, `og:url`, JSON-LD `url`, canonical links when
  `site_base_url` is unset) carry the public hostname instead of the
  container-internal name Caddy passes upstream as `Host:`. Don't
  trust headers when behind 0 proxies, the default 0 disables the
  wrapper.
- **HSTS is gated on `request.is_secure`.** The header only emits
  on HTTPS requests so a misconfigured local-dev session over HTTP
  doesn't pin HSTS into the browser cache.
- **Structured access log** reads `request.headers.get("User-Agent")`
  directly rather than `request.user_agent.string`, Werkzeug's
  parsed UA returns falsy for non-browser UAs (curl, monitoring
  bots, even some Firefox versions in this Werkzeug release), and
  we want every request logged.

## Scheduler verbosity

`update` and `warm-cache` default to terse output and dial up
under `-v`; `deploy/scheduler.sh` exposes the dial via the
`SCHEDULER_VERBOSE` env var, which it splats into every
`flask --app mimir <task>` invocation.

Default behaviour:
- `update` only prints a `<inbox> sync: ...` line when something
  was cloned, fetched, or failed; only prints a `<inbox>/<epoch>:
  ...` line when `new`, `linked`, or `failed` is non-zero.
  Steady-state ticks on a settled archive (only `dup_batch` /
  `dup_db` accumulating) become silent, anything in the log
  then means something actually changed.
- `warm-cache` collapses per-key timings into a single summary
  line `warm-cache: N inboxes, K keys, T ms total`. Per-key
  timings still come back under `-v` for chasing slow queries.
  `purged N expired cache rows` is unconditional because it's a
  state change, not steady-state noise.

This was driven by the log volume scaling with inbox count:
`warm-cache` alone emitted ~8 lines per inbox per minute. With
5+ inboxes the journal becomes hard to scan. Goal: idle ticks
silent, anything in the log signals a real event.

For ad-hoc inspection without restarting the sidecar:

```sh
podman exec mimir-tasks flask --app mimir warm-cache -v
podman exec mimir-tasks flask --app mimir update -v
```

The scheduler keeps running quietly while the one-shot prints
to its own stdout.

## Auto-ANALYZE after threshold-crossing ingest

After an ingest writes "enough" new rows (default `10000`, tunable
via `settings.analyze_after_ingest_rows`), `ingest_inbox` runs
`ANALYZE` before returning. Without this, the SQLite query planner's
stats go stale on a fresh deploy / first big ingest, and recursive-
CTE joins pick the wrong index (see "Multi-inbox support"). The
threshold check avoids running ANALYZE on every small incremental
ingest. The default is conservative (fires ANALYZE more often than
strictly needed) on the principle that a planner stat refresh is
cheap and a wrong-index recursive-CTE choice is multi-minute pain.

## ANALYZE sample size: 4000 is calibrated for production, not just defaulted

`settings.analyze_limit` (default 4000) is `PRAGMA analysis_limit`
applied on every SQLite connection. ANALYZE samples at most this
many rows per index to estimate the per-key distribution that the
query planner reads from `sqlite_stat1`.

The history of this knob matters:

1. **1.35.0 and earlier**: no cap. ANALYZE scanned every row of
   every index. On the production 11M-row corpus that held the
   writer lock for 25-30 s once a day. Acceptable in isolation,
   but with broker mode (1.36.0+) the long-op writer-lock hold is
   load-bearing for cache-set queue depth; a 27 s post-migrate
   ANALYZE stalled every cache write behind it.

2. **1.35.1**: capped at 400 on the basis that SQLite docs called
   400 "appropriate for typical workloads." ANALYZE dropped to
   ~100 ms. **Catastrophic for the multi-inbox corpus**: with
   only 400 samples per index, the planner mis-estimated join
   cardinalities on `articles.thread_parent` and the
   `(article_id, inbox_id)` covering index on `article_lists`,
   choosing recursive-CTE shapes that scanned millions of rows
   per iteration. `get_thread` for a 15-message thread on lkml
   ran for 400 seconds (vs the ~2 ms documented baseline). The
   regression wasn't surfaced until 1.36.0 (Phase 2.1) moved
   ingest into the broker, which exposed the underlying per-
   request slowness via worker-queue saturation rather than the
   visible-as-slow-rendering-on-quiet-corpus shape it had been.

3. **1.36.4**: bumped to 4000. Roughly 10× the SQLite-docs hint
   for "very large databases" (1000-1500). ANALYZE wall time is
   ~1-3 s on the production corpus AT THE TIME (11M rows); measured
   2026-08-04 at 28.8M rows it is ~10.8 s and trips the broker's own
   slow-write warning nightly. The planner gets enough
   samples to estimate join cardinalities correctly. Validated
   end-to-end: the URLs that took 400 s pre-fix returned in
   ms after a full ANALYZE; 4000 produces the same plans as a
   full scan, so the bounded daily ANALYZE catches the common
   case. The weekly full ANALYZE (analysis_limit=0) on the
   scheduler tick is the safety net for distribution drift in
   long-tail indexes that the 4000-row sample might miss.

The takeaway when this knob comes back up: 400 is **not** "what
SQLite recommends." 400 is what SQLite docs call appropriate for
"typical workloads," which does not include 11M-row multi-tenant
SQLite databases with hot recursive-CTE read paths. Calibrate to
the corpus, validate planner output (EXPLAIN), don't take SQLite-
docs defaults as production-ready at scale.

## Scheduler-loop timers persist across restarts; post-migrate ANALYZE

`deploy/scheduler.sh` runs `warm-cache`/`update`/`update-mainline`/
`analyze`/`vacuum` on env-tunable cadences (`WARM_CACHE_EVERY`,
`UPDATE_EVERY`, `UPDATE_MAINLINE_EVERY`, `ANALYZE_EVERY`,
`VACUUM_EVERY`). `update-mainline` short-circuits cheaply when the
fetched mainline HEAD hasn't moved (the MAINTAINERS reparse early-
returns on unchanged `state.last_commit_sha`; the Link-trailer walk
is incremental), so the 10-minute default lets a kernel-tree
subsystem rename or a new MAINTAINERS section reach the From-line
allowlist within one rebase cycle without making the sidecar chatty
against mainline.kernel.org. The original shape initialised
`last_<task>` to container start time, which silently broke the
slower cadences (daily ANALYZE, weekly VACUUM) the moment release
rollover frequency dipped below the cadence: every restart pushed
the next firing out by another full interval and the task never
fired. With recent release cadence the daily ANALYZE on the prod
sidecar effectively hadn't run since `article_files` and
`article_trailers` were added, leaving `sqlite_stat1` with zero
entries for those tables and the planner blind for any query that
needed them for cost estimation.

Fix: persist each task's last-run timestamp to `/data/.last_<task>`
and read sentinel mtime at boot. Missing or unreadable sentinel
returns 0 so the next tick fires immediately, which is the right
default for a fresh `/data` volume and for the first deploy
carrying the change. Sentinels are only touched on successful runs
so a transient failure doesn't push the next retry out by a full
cadence.

Companion: the broker runs a bounded `ANALYZE`
(`analysis_limit=4000`) immediately after `alembic upgrade head`
and `bootstrap_inboxes`, before touching the `/data/.migrated`
healthcheck sentinel, gated on `/data/.broker_initial_analyze` so
subsequent restarts skip it (it's a one-shot per schema). The
v2.0.0 broker owns this; through v1.x it lived in scheduler.sh.
Migrations can introduce new indexes whose `sqlite_stat1` entries
don't get populated as a side effect; the post-migrate pass closes
that gap and is bounded (~10.8 s as of 2026-08-04, vs the ~25-30 s
unbounded
pass that earlier shipped). Web tier gates on the broker's
healthcheck so gunicorn doesn't start serving until the
post-migrate ANALYZE finishes, the right ordering: serve queries
against the schema only once the planner has stats for the schema.

This used to carry a note that the systemd deployment path was
unaffected, because `mimir-analyze.timer` fired on its own absolute
schedule via systemd's persistent-timer semantics and so had no
equivalent restart-resets-clock bug. **3.9.0 deleted
`deploy/systemd/` (#558)**: production runs podman quadlets from
`ansible-eelco`, and those units described an arrangement nothing
used while keeping a second, diverging description of the deploy
alongside the real one. So the sentinel mechanism above is not one
of two scheduling paths any more, it is the only one, and the
restart-resets-clock bug it fixes has no counterexample left in the
tree to contrast against.

## Path-filter SQL: UNION of range seeks, not OR of LIKE-ESCAPE

The shared `_subsystem_path_filter_sql` builder
(`mimir/subsystems_dashboard/_path_filter.py`) emits the
"article IDs matching this subsystem's F: globs and not vetoed by
its X: globs" SELECT used by every per-subsystem dashboard helper
(`recent_articles_in_subsystem`, `daily_volume_in_subsystem`,
`active_threads_in_subsystem`, `active_reviewers_in_subsystem`).
Its shape is load-bearing for cold-miss latency on busy
subsystems.

The original shape emitted directory-prefix matches as
`path LIKE :prefix ESCAPE '\'`. The ESCAPE was needed so a literal
`_` in globs like `arch/x86_64/` wouldn't widen the match. SQLite
**disables its LIKE→range-scan optimisation whenever ESCAPE is
present**, so every prefix branch silently fell back to a full
scan of `article_files` (7M rows on prod) even though
`ix_article_files_path` exists and would have been used without
the escape. NETWORKING [GENERAL] on lkml ran ~10 s cold; ARM/APPLE
~7 s. The OR-of-conditions enclosing the LIKE branches compounded
the loss: even when one branch could have been an index seek, the
OR-disjunction defeated planning per branch.

Replaced with a UNION of independent range-seek branches:
`path >= lo AND path < hi` per directory prefix (the `_` in
`arch/x86_64/` is the literal byte it is under `>=`/`<`, so no
escape machinery is needed), `path = :exact` per exact path. Each
branch is independently sargable against `ix_article_files_path`.
The per-row NOT(exclude) predicate moves into each branch so the
X: semantics ride along on already-narrowed rows. NETWORKING cold
miss drops to ~400 ms (25x), ARM/APPLE to ~7 ms (1000x).

The rule generalises: any time you reach for `LIKE 'prefix%' ESCAPE`
on an indexed TEXT column, you've disabled the index. Use
`col >= lo AND col < hi` (where `hi` is the prefix with its last
byte incremented) instead, and AND-NOT exclude predicates onto
the matched rows rather than OR-ing them into the include
disjunction. Plan pinned in
`test_subsystem_path_filter_uses_index_seeks` (asserts no
`SCAN article_files` in EXPLAIN, only `SEARCH ... USING INDEX
ix_article_files_path`).

### Sibling: `_subsystem_path_filter_exists_sql` for ORDER-BY-driven queries

The UNION-of-seeks shape is right when the path filter is the most
selective predicate AND the outer query has no order/limit (every
matching article must come back). The triage queues (#209) flip
the constraint: they ORDER BY `articles.date` ASC LIMIT 10 and
want the planner to walk `ix_articles_date` ASC, predicate-test
per article, stop at 10. The `a.id IN (UNION-of-seeks)` shape
materialises the full path-filtered set before LIMIT (100k+ rows
on NETWORKING [GENERAL]) and sorts it, ~8 s cold.

The sibling `_subsystem_path_filter_exists_sql` returns a
correlated `EXISTS (...)` predicate instead of an article_id
enumerator. With it, the planner walks the date index and tests
the per-article path EXISTS via the `(article_id, path)` PK on
`article_files` (cheap, ~3 files per article). Drops to ~2 s.

Companion trick: bound the date range with a `min_date` lower
bound (`SUBSYSTEM_TRIAGE_MAX_AGE_DAYS`, default 180). The date
index walk is then over ~200k articles instead of all 6M+ in the
corpus. ~200 ms cold. The bound also encodes the operational
truth that patches older than ~6 months are abandoned, not
"needs attention". Plan pinned in
`test_triage_queries_use_date_index_no_full_scans`.

When to reach for which: UNION-of-seeks for "give me every
matching article" (the original `recent_articles_in_subsystem`
shape). EXISTS for "give me the top N matching articles by some
order" (the triage queues).

## tz-aware UTC normalization for `Date:` headers

`email.utils.parsedate_to_datetime` returns a tz-naive `datetime`
when the Date header carries `-0000` (RFC 5322 explicitly allows
this for "no time-zone information available"). Mixing tz-naive
and tz-aware datetimes raises `TypeError` on comparison, which
caused a real outage: the Phase 1 observation tally's
`max(prev_ts, parsed.date)` blew up the entire batch the moment a
single `-0000` message landed alongside a tz-aware one, rolling
back ~all writes and stalling lkml ingest at 26 articles.

Fix: `_aware_utc(dt)` normalizes any `datetime` to aware UTC at
entry points (`ingest_epoch`, `backfill_canonicals`,
`_json_ld_message`'s date emission, etc.). Naive datetimes are
assumed UTC, consistent with the RFC 5322 semantics of `-0000`
and with how lore's commit timestamps already arrive.

## Tracemalloc diagnostic: latent, env-gated, snapshot-and-diff-offline (3.1.0)

The broker carries a `tracemalloc` snapshotter for diagnosing
Python-side memory retention, shipped in 3.1.0. Off by default
(`TRACEMALLOC_INTERVAL_SECONDS=0`), zero cost when disabled (no
thread, no `tracemalloc.start()`, no `/data/diagnostics/`
directory).

Three design choices worth recording:

- **Latent, not always-on.** `tracemalloc` adds 10-30% memory
  overhead and slows allocation-heavy paths by ~10-15% per its
  own docs. The leak we are hunting (post-3.0.1 mimalloc switch,
  Python-side retention ~45 MiB/min sustained) is not urgent
  enough to justify that tax permanently. The operator turns it
  on via env when investigating, off when done.

- **Snapshot-to-disk, diff-offline.** The snapshotter writes a
  pickle of `tracemalloc.Snapshot` per interval (atomic rename
  to `/data/diagnostics/tracemalloc-<UTC-ISO>.pkl`), plus a
  top-25-by-current-bytes summary to stderr. The `mimir
  tracemalloc-diff` CLI reads two pickles, runs `compare_to`,
  ranks growers, prints a table. Two reasons for that shape:
  the on-broker observation cost is just a per-interval
  pickle dump (fast, no comparison work on the hot path); and
  the offline diff lets the operator iterate on `--top` /
  `--filter-prefix` arguments without restarting the broker
  or re-collecting data.

- **`traceback[-1]` is the allocation site, not `[0]`.**
  Python's `tracemalloc.Traceback` is documented as sorted
  oldest-to-most-recent. The diagnostic logs and the CLI table
  both pick the allocation site (innermost frame) as the leader
  line; the three frames before (`reversed(frames[-4:-1])`)
  render as caller context below. This was caught twice during
  code review while implementing the feature; worth pinning
  here because the natural reading of "first frame" is the
  opposite of what tracemalloc means.

Full design captured in
`_claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md`.
The 14-sample on-prod trajectory check the same day (results
under `_claude/broker-monitor-state.json`) confirmed the
VmData drift; running the diagnostic on prod over the
following 4h refined the diagnosis (analysis at
`_claude/tracemalloc-analysis-2026-06-11.md`).

**Calibration carried forward from the 3.1.1 cycle:**
tracemalloc is blind to C-extension allocations, `mmap`'d
regions, mimalloc's per-thread page-cache, and thread stacks.
When VmData drift and tracemalloc's per-line totals disagree,
that gap is the diagnostic correctly telling you the leak is
below the Python heap, not "tracemalloc isn't seeing it." In
mimir's case the dominant retention is dulwich's `mmap`'d
pack files across many open Repos (audit and lifecycle fix
queued for 3.1.2 at #461). The next layer's diagnostic tools
are `pmap` / `/proc/<pid>/smaps` and mimalloc's
`MIMALLOC_VERBOSE`, not "wait longer for tracemalloc."

## Branching model: git-flow

From v1.0.0 onward, mimir follows git-flow: `develop` is the default
branch and integration target; `main` holds tagged released code
only. Feature work branches off `develop` as `feature/*`; releases
go via `release/X.Y.Z`; urgent fixes via `hotfix/X.Y.Z` off `main`.

The motivation is project-portfolio consistency, not anything
mimir-specific. All `~/Projects` projects past 1.0.0 use the same
branching shape so the cadence and tooling carry across.

The current gates and their rationale are described in
[CI flow: PR-gate + tag-publish, loose develop](#ci-flow-pr-gate--tag-publish-loose-develop-tier-c-2026-06-14).
That section owns the CI and branch-protection account; keeping a
second copy here previously left contradictory release instructions.

The operational sequence lives in the portfolio's
`MIMIR-RELEASE-FLOW.md`. The release branch reaches `main` through a
PR and returns to `develop` through a direct back-merge.

## Emitter and acceptor must share one validity rule

A recurring defect shape here, and the source of every blocking bug in
the 2026-07-29 hub-page work: one module DECIDES a URL and another
module DECIDES WHETHER TO SERVE IT, and nothing forces the two to
agree. It is invisible while the link is incidental and becomes real
the moment a sitemap advertises it, because then crawlers walk the
disagreement systematically rather than a reader stumbling on it.

Four instances, the first three live at once:

- `subsystems.subsystem_path` percent-encoded any name at all, while
  `subsystem_dashboard` rejected C0 controls and 404'd. Production
  carried three section titles containing a TAB (see MEMORY.md), so
  three inboxes were about to publish dead URLs. Resolved by
  `subsystems.is_addressable_subsystem_name`, the single predicate the
  route's acceptor and every emitter now consult.
- The subsystem page took its canonical from `default_canonical_url`,
  which is `_site_base() + request.path`, and Werkzeug's `request.path`
  is URL-DECODED. So the sitemap advertised a percent-encoded URL and
  the page self-nominated one containing raw spaces. Nearly every
  MAINTAINERS title has a space. Routes that are sitemapped must pass
  an EXPLICIT canonical built from the same helper the sitemap uses.
- The maintainer link gated on `is_allowlisted_address`, which is the
  union of `M:` and `R:`, while `/maintainers/<address>` serves `M:`
  only. The gate answered a real question ("is this address safe to
  display") that was not the question that mattered ("does this page
  exist").
- The series diff page (#661, 3.11.1) set no canonical, so it fell
  back to `default_canonical_url`, which also drops the query string.
  Every diff page nominated the bare `/<inbox>/series/<key>/diff`,
  which 404s, and Search Console listed those bare URLs as 404s. The
  page is linked, not sitemapped, so "sitemapped routes" was too
  narrow: any route whose identity includes query parameters must pass
  an explicit canonical. `_series_diff_url` builds both the revision
  panel's link and the page's canonical.

The rule that falls out: **whenever code emits a URL for another
route to serve, the validity predicate and the URL builder are shared
code, not parallel implementations.** `maintainer_path` and
`subsystem_path` exist for exactly this, and they are why a link, a
sitemap `<loc>` and a page's own canonical are byte-identical rather
than merely equivalent-after-normalisation.

## Hub pages and the internal link graph

The consolidation work (thread views, canonical routing, materialised
thread roots) made `/t` the canonical target of every message in a
multi-message thread, the URL both sitemaps list, and what IndexNow
announces. An audit on 2026-07-29 found that the entire template set
contained exactly ONE link to it, conditional, on the message page,
while ten templates linked message pages. Crawlers reached the
consolidated document almost solely via the pages that disclaim
themselves.

Maintainer profiles were the mirror image: 2,248 URLs in
`/sitemap-maintainers.xml` with zero inbound links, because every
place a maintainer appeared rendered as plain text.

Both are now linked, and the lesson generalises: **a canonical tag and
a sitemap entry are assertions, an internal link is a path.** Shipping
the first two without the third leaves the surface nominally
authoritative and practically unreachable. When a change makes page A
the canonical for page B, check what links to A.

Two deliberate limits on how far the sitemap goes, both from measured
cardinality (CLAUDE.md "Corpus scale" carries the numbers):

- **Author and reviewer pages are linked but NOT sitemapped.** 52,989
  distinct reviewer addresses breaches the 50,000-per-urlset protocol
  limit by itself, and 14,460 of those have a single trailer.
  Advertising them would inflate the index this work exists to shape.
- **The subsystem index lists the ACTIVE set, not the MAINTAINERS
  taxonomy.** The dashboards are per-inbox, so the full taxonomy is
  ~660k URLs, nearly all empty for their inbox. Those URLs are today
  content-gated by only being linked when a real patch matched, which
  is the property that makes them worth indexing; a complete index
  would trade it away.

The index is also omitted from the sitemap when it would be empty
(~45 of 203 inboxes see no MAINTAINERS-claimed path in a week). It
stays linked from the inbox dashboard, so it is discoverable the
moment it has content: linked-but-not-advertised is the same posture
as the author and reviewer pages.

## MAINTAINERS reparse gates on the blob, not the tree HEAD

`MainlineState.last_commit_sha` holds the git object id of the
MAINTAINERS **blob**, not the HEAD of Linus's tree. It held HEAD until
2026-07-29, which meant `load_maintainers` re-parsed the file and
rewrote the whole ~15k-row subsystems triple to identical values on
every tick where the tree had been pushed to, i.e. most of them, on
the single-writer broker. The model docstring already described the
cursor as existing because "MAINTAINERS only changes when that one
file does", so this was a bug against documented intent.

It became load-bearing beyond the wasted writes once the web tier
started folding that cursor into page ETags as the subsystem-rule
version: on HEAD it rotated every message page's validator several
times a day for a rendered output that is almost always identical,
which is the 3.6.1 CDN incident inverted (excess fetching rather than
staleness).

Git object ids are content-addressed, so the blob id is exactly the
"did this file change" signal the gate wanted, and it needed no
migration: same column, same 40-hex shape, and a row still holding a
head sha mismatches once and self-corrects.

Two consequences worth knowing:

- **A forced rebuild carries a generation suffix** (`<blob>.<n>`), and
  the gate compares only the content half. `--force` is the operator
  asserting the rules may differ from what the bytes imply (a parser
  fix, a hand-repaired triple), so re-storing an unchanged cursor
  would rebuild the rules while freezing every page's validator. The
  suffix costs no extra reparse on the next ordinary tick.
- **A subsystems table emptied out-of-band no longer self-heals.**
  Nothing about the file changed, so an ordinary tick correctly
  no-ops. `mimir update-mainline --force` is the repair, and it now
  propagates.

## What the page ETags version

Both the message page and the thread view are served
`Cache-Control: public, no-cache`, so their ETag is the only thing
between a source-data change and an edge (plus every crawler) serving
a body the data has already contradicted.

`web/routes/_validators.py::render_state_tag` is the shared input
covering the DERIVED state both surfaces render and neither an article
id nor a thread's shape can express: landing state, review roll-up,
subsystem attribution (rules version AND the article's touched paths,
since the attribution is the product of both), and
revision/supersedance. It is shared precisely so the two surfaces
cannot drift apart again: the thread view had per-input coverage while
the message page had no state input at all, so a patch landing, a
MAINTAINERS reparse, a new trailer or a v2 posting pinned it until an
unrelated reply happened to land in its thread.

Cost, measured 2026-07-29 on the production corpus: 0.752 ms per call
on patch-shaped articles (the expensive shape), paid on every request
to both surfaces including the 304s, which is the crawler path. All
five reads are index seeks and none writes, so it adds no broker RPC.

**Sharing `render_state_tag` did not stop the two surfaces drifting;
3.10.0 found them apart again on a different axis.** The message page
omitted `thread_view_render_cap` from its validator while its own
canonical was `thread_page_url(..., thread_page_of(..., cap))`, so a
cap change left caches and crawlers holding a page whose canonical
named a thread page that 404s. The thread view had folded the cap in
from the start.

The paragraph above was written as though a shared helper closed the
class. It closed the DERIVED-state half of it. What it could not cover
is an input that is neither derived state nor thread shape, and the
cap is the nastiest shape of that: it is an operator env knob, so
changing it restarts the same image and `mimir.__version__` does not
move either, leaving nothing in the validator to notice.

So the durable test for this area is not "do both surfaces call the
shared helper" but **"for each surface, enumerate every input its
rendered output depends on, and check the validator carries all of
them"**, config knobs included. A validator is the emitter/acceptor
rule in time rather than in space: the thing that decides a response
and the thing that decides whether a cached copy is still that
response must agree on what the response depends on.

## Version source of truth

`pyproject.toml`'s `version` field. Read at runtime via
`importlib.metadata.version("mimir")` and exposed as
`mimir.__version__`; surfaced in the web footer (`base.html` →
`{{ mimir_version }}`). Bumped on `release/X.Y.Z` alongside the
annotated tag, per the portfolio-wide versioning convention (see
`~/Projects/CLAUDE.md` "Versioning"). The CI image-tag derivation
reads the git tag, not pyproject; the two stay in sync via the
release-branch step.

## Related discussions for non-patch threads (3.2.0)

Patch threads get relatedness for free from path signals
(subsystem chips, recent-patches-touching, revisions fold);
non-patch threads (bug reports, RFC discussions, anything with no
diff and hence no `article_files` rows) had nothing. 3.2.0 ships a
"Related discussions" panel scoped to exactly that residual,
built from signals already in SQLite: one date-index-bounded
candidate query (exact `subject_normalized` OR rare-token LIKE OR
shared authors), candidates collapsed to thread roots, scored
(+6 exact subject, +3 per token, +2 per participant capped at 3,
180d half-life decay for ordering, threshold 2.0 on the undecayed
base so old-but-strong matches still render), top 5, cached per
root for 1h. Patch threads stay eligible as *candidates* (a bug
report's fix series is its most-related thread); the old
"Possibly related" exact-subject surface narrowed to patch
threads since the panel subsumes it.

The deliberately-cheap design is instrumented rather than
defended: every cold compute logs `related-discussions: ...
rendered=N strong=N weak=N ...`. The #71 escalation rule reads
empty-rate (`rendered=0`) and weak-match rate (`rendered>0
strong=0`) after ~4 weeks on prod; only if those look bad does
the tier-B body-token derived table (stdlib symbol/error-string
extraction at ingest, same pattern as `article_trailers`) get
built, with vector similarity as last resort. Full design:
`_claude/specs/2026-06-12-related-discussions-design.md`.

**Bot-sender exclusion (3.3.0).** Day-1 production sampling of the
3.2.0 panel showed the non-patch population on lkml is dominated by
*automated* traffic (syzbot, the kernel test robot's sparse/BUILD
SUCCESS reports, tip-bot), not the human discussions the panel was
scoped for, and on those the matches were low-value (generic crash
vocabulary like "general protection", or participant-only on a
shared bot author). The instrument did its job: the samples drove a
refinement, not a redesign. `related.is_bot_sender` (substring match
vs `Settings.related_discussions_bot_senders`, default syzbot / lkp /
tip-bot) now excludes bots at three points: dropped from the authors
used for candidate generation and participant scoring; a candidate
thread excluded wholesale if its *root* author is a bot; and the
panel suppressed entirely on bot-authored roots (gated in the route
before compute, so suppressed roots stay out of the empty-rate
metric). Curated denylist, deliberately not a volume heuristic, so
Stephen Rothwell's semi-automated linux-next dailies (the one *good*
result in the sample) survive. The log line gained `bot_filtered=N`.
The remaining known-weak case (a human bug report matching unrelated
patch series on a coincidental generic token) is the
stopword/token-rarity lever, still deferred to the #71 data.

## Why the thread view paginates rather than caps (3.8.0)

The view used to render `THREAD_VIEW_RENDER_CAP` messages and link the
rest, which is two decisions wearing one number: how much HTML a page
may be, and how much of a conversation a reader may reach. The second
was never wanted; it existed because rendering more cost more.

What made the cap load-bearing was not the rendering at all. Membership
came from `get_thread`'s recursive walk, so serving fifty messages
visited every message in the thread. Measured on production 2026-08-02
against the largest thread in the corpus (syzbot, 12,342 messages,
depth 63): **14,424 ms of a 15,467 ms response**, and paid on 304s too,
because the page's ETag derives from the thread's shape. The cap was
optimising blob fetches, which were about 10 ms each and never the
cost.

The materialised `article_lists.thread_root_id` column (3.7.0) answers
membership by index seek instead: **6 ms** for the same page. Once
membership is free, truncation buys nothing, so the cap becomes purely
a page-weight budget and the tail becomes further pages.

Three consequences worth recording, because each one is a thing that
can silently go wrong:

- **The render order and the rank predicate must be ONE rule.** A
  message's canonical names the page holding it, computed by
  `thread_page_of` as a COUNT under the same `(COALESCE(date, floor),
  id)` order `thread_by_root_id` sorts by. Deriving the page
  independently (a Python re-sort, a different NULL-date treatment) is
  how a canonical comes to name a page that does not contain the
  message, on the one surface crawlers are aimed at. This is the
  emitter/acceptor rule from the section above, applied to an ordering
  rather than a string.

- **Ordering changed from depth-first to arrival.** Reconstructing
  depth-first needs the traversal this work exists to avoid, and the
  page never drew the hierarchy: `ThreadNode.depth` reached the
  template and was used zero times. A flat view in arrival order is
  what a mailbox is. `depth` is now `None` on the indexed path rather
  than `0`, so any future arithmetic raises instead of being quietly
  wrong for every reply.

- **A thread the column cannot rank makes NO page claims.** One
  predicate, `threading.unmaterialised_roots`, is consulted by the
  renderer, the canonical, the sitemap and IndexNow alike. When it says
  a thread is unrankable, the page renders whole from the walk, its
  messages keep their own canonical, and its root is advertised as a
  message URL. Less consolidated while data is unrepaired, never
  pointing at a page that does not hold the message. The alternative,
  letting each surface decide for itself, is what produced every
  blocking defect across this feature's six review rounds.

## Roadmap (deferred features)

These are committed in spirit but not yet built. Re-read this list
when planning what to do next.

- **FTS5 escalation for search.** Gated on measured cold-miss
  latency, scope changes
  (cross-inbox search), or a decision to add body search. **Do not
  pick up speculatively.**
- **Admin-UI write-boundary validators**, service-layer
  validation in `mimir.inboxes` exists; the planned web admin UI
  needs the same validators wired to its forms once that UI is
  built.
- **Error tracking integration**, Sentry/GlitchTip when the
  surface area justifies it.
- **Favicon / logo.** Tracked, punted to an external tool.
- **Raw / `.patch` / `mbox` endpoints.** public-inbox archives
  conventionally expose `/<msgid>/raw` (one RFC 5322 message),
  `/<msgid>/t.mbox.gz` (the whole thread as mbox), and an
  applies-clean `.patch` view. mimir doesn't ship any of these yet;
  the message page is the only read surface. Adding them is mostly
  routing + content-type handling, `mimir.store.read_message`
  already returns the parsed shape, and the underlying blob bytes
  are reachable via dulwich one indirection further. Out of scope
  for the 1.x line until there's a concrete maintainer-workflow
  ask. Design rule when this gets picked up: stream blob bytes
  verbatim, no redaction or re-serialisation, see "Redaction is a
  display-time decision, not a data transform" above. That's what
  preserves DCO chain integrity for `git am`-friendly downloads
  and keeps the contract with lore.kernel.org's analogous endpoints
  intact.
- **Markdown content negotiation on the message view.** A client
  sending `Accept: text/markdown` against
  `/<inbox>/<YYYY>/<MM>/<article-id>` should get the message as
  Markdown instead of HTML. Headers as YAML front-matter (message-
  id, subject, from, date, inbox, in_reply_to) then body. Body
  renders through a new sibling to `mimir/rendering/body.py`
  walking the same `parse_blocks` output (quotes → `>` lines,
  code → fenced blocks, diffs → triple-backtick blocks).
  `Content-Type: text/markdown; charset=utf-8` on the response.
  `Vary: Accept` so caches don't confuse the two representations.
  ETag input gains the negotiated content type so the conditional-
  GET path stays correct.

  **Distinct from `/raw`** above: Markdown is a *rendered* output
  that inherits the redaction posture (allowlisted senders show
  full From; others show `<hidden>`; DCO trailer addresses
  redacted), while `/raw` streams blob bytes verbatim. Operators
  who need DCO-integrity bytes use `/raw`; operators who want
  paste-into-Ghost / paste-into-an-LLM-context content use
  Markdown. The two endpoints have different contracts and should
  not be conflated.

  First slice covers the message view only; dashboards, search
  results, and lists are lower-value as Markdown and can wait.
  Sized at ~400 LOC + tests. After 2.0.0 ships, since the broker
  cleanup work pre-empts everything else until then.

## Multi-tree mainline tracking

mimir indexes Linus's tree plus a curated set of subsystem-tree
mirrors (linux-next, net-next, tip, pci, mm, bpf-next; ops
extends via env). Each tree contributes rows to
`mainline_commits` under its own `tree_name`; the same
message-id can have multiple rows when a patch lands in a
subsystem tree, gets aggregated into linux-next, and eventually
lands in Linus.

The 5-state lifecycle taxonomy (`mimir/lifecycle_status.py`,
priority: Landed > Superseded > Queued > Reviewed > Pending)
collapses per-article tree+trailer+series state into one
glanceable badge for listings. The patch-state card on message
pages stays the snapshot ("where is this now"); the lifecycle
timeline below the card renders the chronological journey
("how did it get here").

Three design decisions worth their own paragraphs:

**Env-driven dict-of-trees, no DB table.** Matches the
`Settings.inboxes` env-bootstrap-only era posture. Operator
extension via `TREES__<name>__URL`; redeploy required.
A future DB-backed shape (with admin CRUD) can retrofit by
bootstrapping a `trees` table from `Settings.trees`, same as
`inboxes` did.

**Per-tree `rebases` flag.** Force-pushed trees (linux-next,
daily) would invalidate the SHA cursor every tick. The
`rebases=True` branch in `walk_commits` `DELETE`s the tree's
existing rows and re-walks from scratch each tick; the
`INSERT OR IGNORE` constraint dedups intra-walk. Like every
non-Linus tree it excludes commits reachable from Linus's HEAD, so
the daily walk covers only what linux-next holds beyond Linus
(#673; before that it walked the whole history). Cheaper than the
always-full-walk alternative that would also burn Linus's tick
budget for zero new commits 99% of the time.

**`--reference linus.git` shared object storage.** Each non-
Linus tree is cloned with `git clone --mirror --reference linus.git
-- <url> <path>`. Marginal disk per tree drops from ~10 GB to
the divergent objects only (~100-500 MB typically). The trade:
a corrupted Linus clone breaks every dependent tree. Recovery:
re-clone the broken tree without `--reference`, or re-clone
everything. Acceptable for the disk win.

## Badge primitive + page layout

The patch-page header reads as a one-line status panel: activity
chip + lifecycle pill, sitting on the same baseline under the
Subject. Both badges share a single CSS `.badge` primitive
(monospace + uppercase, identical height + padding). Activity
varies by colour-coded dot, six heat states from same-day "hot"
to dormant. Lifecycle varies by pill label + colour, five states
plus an inline count `: N (XM)` for review-trailer total +
maintainer-subset. Per-reviewer names live in the pill's tooltip
(commit hash + timestamp on the leading line; one reviewer per
line, M-prefix on maintainers).

Three load-bearing decisions worth their own paragraphs:

**One shared badge primitive.** Pre-2.5 the listing pill and the
proposed activity chip were separate visual languages (different
heights, padding, typography). A maintainer eye scanning a list
saw two competing widgets. The unified primitive makes them
sibling readouts on the same instrument panel.

**Drop the lifecycle timeline section.** The 2.4.0 timeline
section was vertically expensive (h3 + left rail + per-event
rows) and most patches' lifecycles were two events (`Posted ...
→ Aggregated on $tree ...`). Pushing the same data into a pill
+ tooltip frees vertical real estate, the message body starts
~50px higher on a typical patch page. The PatchState data shape
keeps everything; only the rendering changed.

**Counts in the pill itself, not just the tooltip.** Real
maintainer triage often wants the count at-a-glance:
`REVIEWED: 12 (4M)` immediately reads as "heavily reviewed,
maintainer attention has reached it" without hovering. Hover then
surfaces the per-reviewer names. Two tiers of detail, each one
interaction step deeper.

The lifecycle pill's tooltip leading line carries the commit
hash + timestamp only, the tree name is already on the pill
itself (`IN NET-NEXT`, `LANDED`), so repeating it was redundant.
Reviewer names stack one per line via `white-space: pre` (not
`pre-line`) on the `[data-tooltip]::before` override so multi-
word names like "Vlastimil Babka" don't break at the space.

Revisions move out of the dissolved patch-state aside into a
foldable timeline below the From block. The summary shows the
revision count (e.g. `▸ Revisions (8)`); the body is the same
timeline primitive that the old lifecycle-timeline section
used, repurposed for the per-revision view. Single-revision
patches get no fold at all.

The trailers line is dropped entirely. The pill's `: N (XM)`
suffix carries the count; the tooltip carries the names.
Per-role breakdown (Reviewed-by vs. Acked-by vs. Tested-by)
intentionally collapses into one number, readers needing the
role split read the trailer block in the message body.

## Project history (brief)

- Originated as a Flask + MongoDB + NNTP toy. The author picked Mongo
  because it was top-of-mind from a contemporaneous work project.
- Picked back up in 2026-04 after a long pause. Decision points (in
  order):
  - Drop Mongo for SQLAlchemy + SQLite (KISS, fewer surprises).
  - Drop NNTP for public-inbox git mirror walking (offline,
    deduplicated, immutable, what lore.kernel.org itself runs).
  - Add pydantic at the validation boundary (`ParsedArticle`,
    `Settings`, `IngestResult`), partly genuine fit, partly to
    give the author practice for work projects.
  - Rewrite parser to dodge the Py3.11 AddressHeader bug.
  - Pivot from "store everything in SQLite" to "git is truth,
    SQLite is index" after seeing 1.3 GB at 250k messages
    projected to ~25 GB full archive.
  - Add web UI (Flask + Pico + HTMX + Pygments) with body
    rendering.
  - Parallelize ingest via `ProcessPoolExecutor`.
  - Add `update` for one-shot manifest discovery + clone + fetch
    + ingest.
- Multi-inbox refactor: `Article.list_name` field replaced by an
  `inboxes` table + `article_lists` M:N. Cross-posted messages
  collapse to one Article row with multiple per-inbox blob
  pointers.
- Cache pivot: single-pickle-file → `cache` table (JSON values,
  WAL-coordinated, no RCE surface).
- Ship deployment artifacts (Dockerfile, compose example, reverse-proxy
  and stream-proxy examples). The systemd units shipped here were
  removed in 3.9.0 (#558) as an arrangement nothing used.
- 2026-05-06: first production deploy at https://ratatoskr.run,
  rootful podman behind the proxy chain described under "Production
  deployment posture".
  v1.0.0 tagged.
- SEO pass (1.5.0): per-page titles, sitemap `<lastmod>`, meta
  description, Open Graph + Twitter Cards, 301 redirects routed
  to canonical inbox, schema.org JSON-LD.
- Canonical-inbox resolution rolled out (auto-promotion via
  `inbox_address_observations` + `Article.canonical_inbox_id`
  + render-time canonical URL).
- Migration ownership pulled out of the web container into a
  scheduler sidecar with a healthcheck-gated sentinel
  (1.4.0).
- Write-broker journey (1.32.0 → 2.0.0). Driven by recurring
  SQLite writer-lock contention symptoms (silent `cache.set`
  failures under load, `SQLITE_BUSY_SNAPSHOT` incidents during
  admin backfills, multi-minute lock-holds during the daily
  ANALYZE). Shape:
  - **1.32.0 (Phase 1)**: opt-in `mimir-broker` process owning
    cache writes via UNIX-socket RPC. Web tier opens
    `query_only=1`; scheduler retains direct writes for ingest +
    admin paths.
  - **1.33.0 (Phase 1.5)**: queue + worker pool inside the
    broker (after the single-handler design refused to serve
    more than one client concurrently in prod).
  - **1.36.0 (Phase 2.1)**: ingest into the broker. Surfaced as
    a four-hotfix cascade (1.36.1-1.36.4) tracing back to
    `PRAGMA analysis_limit=400` under-sampling the 11M-row
    corpus; bumped to 4000 + weekly full ANALYZE.
  - **1.37.0 (Phase 2.2)**: backfills into the broker with
    cooperative scheduling (chunked handlers + continuation
    cursor), scheduler pre-flight warm, broker warm-worker
    queue.
  - **1.38.0 (Phase 2.3)**: update-mainline + analyze + vacuum
    into the broker, plus a safety pass (broker hardening,
    parser/rendering caps, web+deploy hygiene).
  - **1.39.0 (Phase 2.4)**: admin ops (inbox CRUD, failures
    replay) into the broker. Broker self-bootstraps the
    post-migrate ANALYZE.
  - **2.0.0 (cleanup)**: broker becomes mandatory.
    `READ_ONLY_DB` silently removed, `MIMIR_ROLE` literal
    dismantled, direct-write fallbacks dropped from every CLI
    command, broker takes ownership of `alembic upgrade head` +
    `bootstrap_inboxes` + post-migrate ANALYZE. `_direct_*` cache
    helpers folded into the broker handler module.
  - **Still load-bearing post-cleanup**: `write_transaction()` +
    `BEGIN IMMEDIATE` event listener (the broker is single-
    process but multi-threaded, intra-broker snapshot upgrades
    still trip `SQLITE_BUSY_SNAPSHOT` without it).
