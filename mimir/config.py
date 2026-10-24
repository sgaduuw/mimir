import os
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mimir._inbox_name import INBOX_NAME_RE
from mimir._outbound import validate_outbound_url

# Project root: <root>/mimir/config.py → parent → parent. Resolving
# follows symlinks so a container running mimir from a bind-mounted
# read-only volume still gets a sane absolute path. NB: this assumes a
# checked-out source tree, under `pip install .` mimir lives in
# site-packages and PROJECT_ROOT becomes meaningless. Set DATABASE_URL
# explicitly in any deployment that doesn't ship the tree as-is.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Linus's `linux.git`, the `linus` entry of the curated default tree
# set. The path follows the same pattern as the per-inbox mirrors
# under `Inboxes/`, so a typical deploy gets `<data root>/Inboxes/<list>/git`
# and `<data root>/Mainline/linux.git` side-by-side.
_LINUS_DEFAULT_URL = (
    "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
)
_LINUS_DEFAULT_PATH = Path("Mainline/linux.git")


class InboxConfig(BaseModel):
    """Env-side description of an inbox. The matching ORM model is
    `mimir.models.Inbox`, which is bootstrapped from these entries on
    app startup. The `Settings.inboxes` dict key becomes `Inbox.name`
    (URL slug)."""

    mirror_path: Path
    upstream_url: str

    @field_validator("upstream_url")
    @classmethod
    def _validate_upstream_url(cls, v: str) -> str:
        # Same validator the admin CLI uses; fail fast at config-load
        # rather than letting a bad URL reach `sync.fetch_manifest`.
        return validate_outbound_url(v, allow_http=False)


class TreeConfig(BaseModel):
    """One git tree mimir indexes for `Link:` trailers feeding the
    patch-lifecycle surfaces. The dict key on `Settings.trees`
    becomes the user-facing tree slug (rendered on the patch-state
    card, listing pill, lifecycle timeline)."""

    url: str
    path: Path
    display_name: str | None = None
    # Default "HEAD" means "use whatever the mirror's HEAD points at",
    # which is fine for trees that publish their canonical branch as
    # HEAD. Override per-tree for branches like akpm/mm's `mm-stable`
    # where the canonical pickup signal isn't HEAD.
    branch: str = "HEAD"
    # True for trees that force-push (linux-next, daily). The walker
    # re-walks the whole tree and replaces its rows by difference in
    # one transaction, so readers never see a partial tree (#671).
    rebases: bool = False
    # Per-tree cadence: skip the tree on `update_mainline` ticks that
    # arrive sooner than this since the last successful walk.
    walk_every_seconds: int = 1500  # 25 min default (Linus)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        return validate_outbound_url(v, allow_http=False)


def _curated_default_trees() -> dict[str, TreeConfig]:
    """The 7-tree default set. Function (not module-level constant)
    so each Settings() instantiation gets fresh TreeConfig objects;
    avoids mutation across tests."""
    return {
        "linus": TreeConfig(
            url=_LINUS_DEFAULT_URL,
            path=_LINUS_DEFAULT_PATH,
            display_name="mainline (Linus)",
            walk_every_seconds=1500,
            rebases=False,
        ),
        # linux-next force-pushes daily; full re-walks are heavier, so
        # 24 h cadence + rebases=True is the right pairing.
        "linux-next": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git",
            path=Path("Mainline/linux-next.git"),
            walk_every_seconds=86400,
            rebases=True,
        ),
        "net-next": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net-next.git",
            path=Path("Mainline/net-next.git"),
            walk_every_seconds=3600,
        ),
        "tip": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/tip/tip.git",
            path=Path("Mainline/tip.git"),
            walk_every_seconds=3600,
        ),
        # The tree linux-next pulls (`Next/Trees`: pci/pci.git#next). The
        # old helgaas/pci.git HEAD is `main`, which holds nothing beyond
        # Linus, so it produced no rows at all (#713).
        "pci": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/pci/pci.git",
            path=Path("Mainline/pci.git"),
            branch="next",
            walk_every_seconds=3600,
        ),
        "mm": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/akpm/mm.git",
            path=Path("Mainline/mm.git"),
            branch="mm-stable",
            walk_every_seconds=3600,
        ),
        "bpf-next": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/bpf/bpf-next.git",
            path=Path("Mainline/bpf-next.git"),
            walk_every_seconds=3600,
        ),
    }


# Heavy known crawlers that name themselves in the User-Agent, each held
# to 10% of the global budget (#761). The two search crawlers get 80%
# instead: they back off on 429 and visit a handful of times a day, and
# 10% over many workers refused them (#848). Merged under
# CRAWLER_UA_BUDGETS. Substring matches, so keep the names distinctive.
DEFAULT_CRAWLER_UA_BUDGETS: dict[str, int] = {
    "Applebot": 10,
    "Bytespider": 10,
    "PetalBot": 10,
    "meta-externalagent": 10,
    "facebookexternalhit": 10,
    "Slurp": 10,
    "msnbot": 10,
    "Baiduspider": 10,
    "Googlebot": 80,
    "bingbot": 80,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        extra="ignore",
        # `__` as nested delimiter enables `FIELD__subkey__subsubkey`
        # env vars for dict-of-model fields like `trees` and `inboxes`.
        # e.g. `TREES__net_next__URL=...` seeds a single-tree config.
        # Existing scalar env vars (SECRET_KEY, DATABASE_URL, etc.) are
        # unaffected; none of them contain `__`.
        env_nested_delimiter="__",
    )

    flask_debug: bool = False
    secret_key: str = Field(min_length=16)

    # Brand shown in titles, the nav, and the index heading. The
    # project name "mimir" stays as the page-generator credit
    # regardless. Override with SITE_NAME=...
    site_name: str = "mimir"

    # Canonical absolute base URL of the deployed site, no trailing
    # slash. When set, used verbatim for every emitted absolute URL
    # (canonical link, og:url, JSON-LD `url`, sitemap). When empty,
    # `request.url_root` is used, which depends on ProxyFix being
    # wired correctly, typically right in production but a footgun
    # when the proxy chain is wrong. The 2026-05-11 external review
    # saw `http://` leaking into og:url + JSON-LD on a production
    # page; set `SITE_BASE_URL=https://ratatoskr.run` to force-
    # correct the scheme regardless of proxy config. Leave empty for
    # local dev so URLs match the test host.
    site_base_url: str = ""

    # Default: <project_root>/mimir.db, so cwd doesn't matter (systemd,
    # container, anywhere). Override with DATABASE_URL=..., typically
    # `sqlite:////data/mimir.db` for a container with a persistent
    # volume mount.
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'mimir.db'}"

    # Indexed inboxes. Add another entry to start tracking a second
    # mailing list; the schema and routes are list-aware. Override via
    # JSON in the INBOXES env var, e.g.
    #   INBOXES='{"lkml": {"mirror_path": "...", "upstream_url": "..."}, "linux-arm-kernel": {...}}'
    # Because `env_nested_delimiter="__"` is active, pydantic-settings
    # also accepts per-key env vars of the form
    #   INBOXES__lkml__MIRROR_PATH=...  INBOXES__lkml__UPSTREAM_URL=...
    # Important: ANY INBOXES__* env key REPLACES the whole default dict;
    # pydantic-settings does not merge individual keys into the default.
    # Keys must be slugs (`mimir._inbox_name`): a key is the inbox's name
    # in URLs and the database, so `INBOXES__linux_arm_kernel__...` is
    # refused at load rather than renamed, unlike TREES keys (#747).
    inboxes: dict[str, InboxConfig] = {
        "lkml": InboxConfig(
            mirror_path=Path("Inboxes/lkml/git"),
            upstream_url="https://lore.kernel.org/lkml",
        ),
        "linux-fsdevel": InboxConfig(
            mirror_path=Path("Inboxes/linux-fsdevel/git"),
            upstream_url="https://lore.kernel.org/linux-fsdevel",
        ),
    }

    @field_validator("inboxes", mode="after")
    @classmethod
    def _refuse_non_slug_inbox_keys(cls, v: dict) -> dict:
        # Fail at boot, naming the key: from 3.18.0 the broker refuses a
        # non-slug name on the wire, so a bad key would otherwise stop
        # `mimir ingest` at that inbox instead (#738, #747).
        for name in v:
            if not INBOX_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"INBOXES key {name!r} must be a slug: lowercase letters, "
                    "digits and hyphens, 1 to 64 characters, not starting or "
                    "ending with a hyphen. Inbox names are in URLs and the "
                    "database, so they are not renamed as TREES keys are."
                )
        return v

    # Senders whose email address is shown in full in the UI. Everyone
    # else's email gets hidden (display name kept). Substring match against
    # the address part, case-insensitive. Defaults cover well-known kernel
    # maintainers and the kernel.org domain. Override via env as a JSON
    # array (EMAIL_ALLOWLIST).
    email_allowlist: list[str] = [
        "torvalds@",
        "gregkh@",
        "@kernel.org",
    ]

    # Inboxes that should appear first on the meta-index `/`, in this
    # order, regardless of alphabetical order. The rest follow
    # alphabetically. `warm-cache` refreshes them first in the same
    # order (#763), so pinning also sets warm priority, and `ingest` /
    # `update` walk them first too (#766). Override via
    # PINNED_INBOXES as a JSON array. Default pins lkml since this
    # archive treats it as the focal list; set to empty to fall back to
    # pure alphabetical.
    pinned_inboxes: list[str] = ["lkml"]

    # Extra mailing-list host suffixes that augment the default
    # `mimir.canonical.LIST_HOST_SUFFIXES` set. Operators with archives
    # for lists hosted on a domain we don't yet ship as a default can
    # add it here without a patch; the entries are unioned with the
    # baseline. Default empty (baseline covers kernel.org-adjacent
    # ecosystems). Override via LIST_HOST_SUFFIX_OVERRIDES as a JSON
    # array.
    list_host_suffix_overrides: list[str] = []

    # Inboxes treated as a *firehose* during canonical-inbox resolution
    # for cross-posts. A message posted to a topical list AND lkml is
    # canonically the topical-list version, even when lkml appears
    # first in the To/Cc walk; the firehose copy is the cross-post.
    # An inbox in this list is only chosen as canonical when no
    # non-firehose inbox in the article's link set matches. Mirrors
    # how kernel devs (and Google's crawler) actually treat the
    # topical lists as the conversational home. Override via
    # CANONICAL_DEMOTED_INBOXES as a JSON array.
    canonical_demoted_inboxes: list[str] = ["lkml"]

    # Number of trusted reverse-proxy hops in front of the app. When
    # > 0, Werkzeug's ProxyFix is wired up so the last N entries of
    # X-Forwarded-{For,Proto,Host} are honoured, request.remote_addr
    # then reflects the real client IP instead of the proxy's address,
    # request.scheme is correct under HTTPS-terminating proxies, etc.
    # Leave at 0 if the app is reachable directly, otherwise anyone
    # could spoof those values via a forged XFF header.
    trusted_proxy_hops: int = 0

    # SQLite per-connection `busy_timeout` (milliseconds). When a
    # writer hits a locked DB, SQLite waits up to this long before
    # raising `SQLITE_BUSY`. Default 0 turns transient contention
    # (scheduler ingest / analyze / vacuum overlapping a web cache
    # write) into hard 500s; 5s rides out normal contention windows.
    # VACUUM on the full archive can outlast this; that's intentional,
    # we'd rather surface a true VACUUM-vs-write conflict than mask
    # it with a multi-minute hang. Override via SQLITE_BUSY_TIMEOUT_MS.
    sqlite_busy_timeout_ms: int = 5000

    # Broker tracemalloc snapshotter interval. 0 (default) disables.
    # When > 0, the broker enables `tracemalloc` on startup and writes
    # a Snapshot pickle to /data/diagnostics/ every N seconds. Used
    # for diagnosing Python-side allocation growth in the broker (see
    # _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md).
    # Snapshots can run 10-50 MB each; consider disk impact for long
    # observation windows.
    tracemalloc_interval_seconds: int = 0

    # Write-broker socket path. The broker daemon (`mimir broker
    # --socket PATH`) is the sole SQLite writer process in mimir
    # 2.0.0; every other process (web, tasks, CLI invocations)
    # routes cache + admin writes through this UNIX socket and
    # opens its own DB connections with `PRAGMA query_only=1`.
    # Default `/data/.broker.sock` matches the canonical compose
    # layout; operators on non-standard layouts override via
    # BROKER_SOCKET_PATH.
    broker_socket_path: Path = Path("/data/.broker.sock")

    # Number of broker warm-worker threads (Phase 2.2). The warm
    # queue is drained by N parallel workers so the read-heavy
    # compute phase of `warm_inbox` jobs overlaps across inboxes;
    # cache.set commits still funnel through the SQLite writer lock,
    # but the upstream compute is concurrent. 4 is a reasonable
    # default for a 200-inbox corpus: warm-cache fans out ~200
    # warm_inbox jobs, the broker chews them ~4 at a time, finishing
    # the cycle in ~50× per-task time rather than 200×. Tune higher
    # on bigger corpora; tune to 1 for serial-equivalent behaviour
    # (useful for tests). Override via BROKER_WARM_WORKERS.
    broker_warm_workers: int = 4

    # Cadence for the fast warm tier (seconds). Drives the
    # scheduler's per-minute warm tick. The fast tier covers
    # sitemaps + a handful of cheap front-page helpers, where
    # freshness matters more than recompute cost. WARM_CACHE_EVERY
    # is kept as an alias for back-compat with the pre-tier-split
    # single-cadence env var. Override via WARM_CACHE_FAST_EVERY.
    warm_cache_fast_every: int = Field(
        default=60,
        validation_alias=AliasChoices("WARM_CACHE_FAST_EVERY", "WARM_CACHE_EVERY"),
    )

    # Cadence for the slow warm tier (seconds). Drives the
    # scheduler's hourly warm tick. The slow tier covers
    # subsystem dashboards + per-tracker queries + the rest,
    # where compute cost dominates and an hour of staleness is
    # acceptable. Override via WARM_CACHE_SLOW_EVERY.
    warm_cache_slow_every: int = 3600

    # Refresh window for fast-tier keys (seconds). In the last
    # `window` seconds before a fast-tier key's nominal TTL
    # expires, warm-cache fires probabilistically with p ramping
    # 0→1; in the final `window` seconds of stored TTL (the
    # insurance zone), every tick fires deterministically. See
    # `_claude/specs/2026-06-01-warm-cache-fast-slow-tier-split-design.md`
    # §5.2. Override via WARM_CACHE_FAST_REFRESH_WINDOW_SEC.
    warm_cache_fast_refresh_window_sec: int = 600

    # Refresh window for slow-tier keys (seconds). Same shape as
    # fast tier; the larger default reflects the slow tier's
    # hourly cadence, which needs a wider window to see enough
    # ticks for the probability ramp to fire. 7200 = 2 h gives a
    # 24h-TTL key 2 ticks in its window. Override via
    # WARM_CACHE_SLOW_REFRESH_WINDOW_SEC.
    warm_cache_slow_refresh_window_sec: int = 7200

    # Number of broker cache-worker threads. Cache ops are sub-ms
    # dispatches (build the closure, hand off to the WriterThread,
    # await its result), so a single worker is rarely the funnel
    # under realistic load. The default 1 also preserves FIFO commit
    # order per submitting client: every cache.set / delete on a
    # given key from a given client lands on the WriterThread in
    # the order it was submitted. Bumping to N>1 parallelises the
    # dispatch step but reorders the WriterThread submits across
    # concurrent workers. Safe when callers don't rely on inter-op
    # ordering, which is the case for mimir's cache.set (idempotent
    # on key, last-writer-wins after TTL anyway). Override via
    # BROKER_CACHE_WORKERS.
    broker_cache_workers: int = 1

    # Number of broker long-worker threads. Long ops (ingest,
    # backfills, mainline walk, ANALYZE, VACUUM) are write-heavy
    # and naturally serialise at the WriterThread, which holds the
    # SQLite writer lock for whatever duration the op needs. The
    # default 1 reflects that: bumping to N lets multiple long ops
    # *start* in parallel, but the WriterThread funnels them
    # regardless, so total wall time rarely improves. The case
    # where it does: when long ops have non-trivial pre-write
    # compute (e.g. building a batch of inserts) that runs against
    # the read pool, multiple workers can overlap that compute
    # phase. Override via BROKER_LONG_WORKERS.
    broker_long_workers: int = 1

    # Backfill chunk budget (seconds) for broker-mode cooperative
    # scheduling (Phase 2.2). When a backfill RPC handler runs inside
    # the broker, it walks for at most this long before committing
    # and returning `partial=True` so the CLI loop can issue a
    # follow-up RPC, which resumes below the run's persisted cursor. Between two chunk-RPCs the
    # broker's long worker is free; queued cache writes and other
    # long ops (a scheduler `update` tick) get serviced naturally
    # before the next chunk arrives. Tuneable: shorter dial = more
    # interleaving (better front-page liveness during a long
    # backfill) at the cost of more RPC overhead; longer dial = less
    # overhead at the cost of longer pauses for queued cache writes.
    # 10 s is a reasonable balance on the production corpus. Override
    # via BROKER_BACKFILL_CHUNK_SECONDS.
    broker_backfill_chunk_seconds: int = 10

    # Slow-RPC warning threshold for the write-broker (milliseconds).
    # When the broker takes longer than this to handle a single RPC,
    # it logs a WARNING with the leading bytes of the request and
    # the elapsed time. Healthy RPCs commit in sub-ms; sustained
    # warnings indicate writer-lock contention (e.g. an admin
    # backfill running) or a slow operation
    # (`cache_delete_for_inbox` on a huge table). Set to 0 or
    # negative to disable. Override via BROKER_SLOW_RPC_WARN_MS.
    broker_slow_rpc_warn_ms: int = 100

    # Phase 1 of the broker two-pool restructure introduces
    # ReadSessionPool sized by this field. `os.cpu_count()` is the
    # natural starting default so the pool sizes itself to the host.
    # On the production 4-core coruscant box: 4 read-pool threads +
    # 1 writer = 5 threads total. With 3.15t free-threading, the
    # 4 read-pool threads can occupy 4 cores. Operator override:
    # BROKER_READ_POOL_SIZE. See
    # _claude/plans/2026-05-29-broker-two-pool-phase-1-infrastructure.md.
    broker_read_pool_size: int = Field(default_factory=lambda: os.cpu_count() or 4)

    # Bounded queue depth for the Phase 1 WriterThread. submit()
    # blocks if the queue is full; the blocking is the backpressure
    # signal back to read-pool threads. Default 256 absorbs ~2-3 s
    # of burst at typical write rates. Operator override:
    # BROKER_WRITER_QUEUE_DEPTH.
    broker_writer_queue_depth: int = 256

    # Phase 3 of the broker two-pool restructure
    # (_claude/specs/2026-05-29-broker-two-pool-design.md): per-tree
    # mainline walks now commit in batches of this many commits per
    # WriteOp. Each batch commit holds the SQLite writer lock for
    # ~30-80 ms at production scale (a few hundred INSERT OR IGNORE
    # statements), letting cache.set RPCs from the web tier drain
    # freely between batches. Default 100 matches the Phase 3 spec
    # walkthrough. Operator override: MAINLINE_COMMIT_BATCH_SIZE.
    # Plan: _claude/plans/2026-05-30-broker-two-pool-phase-3-mainline.md.
    mainline_commit_batch_size: int = 100

    # Phase 3b of the broker two-pool restructure (ingest rewrites):
    # time budget per ingest batch before flushing pending writes via
    # a composite WriteOp. The walker accumulates per-message work on
    # the read session, then at this interval submits the whole batch
    # as one WriteOp through the active WriterThread, awaits .result(),
    # and resumes. Smaller values shorten writer-lock-hold per batch at
    # the cost of more round-trips; larger values reduce overhead at the
    # cost of longer head-of-line stalls for concurrent cache.set RPCs.
    # Default 0.5 s preserves the pre-Phase-3b COMMIT_EVERY_SECONDS value.
    # Operator override: INGEST_BATCH_FLUSH_SECONDS.
    # Plan: _claude/plans/2026-05-30-broker-two-pool-phase-3b-ingest.md.
    ingest_batch_flush_seconds: float = 0.5

    # True iff this process IS the broker daemon. The broker
    # container sets MIMIR_IS_BROKER=true; every other process
    # (web, tasks, dev CLI invocations) leaves it false. Drives
    # two pieces of behaviour:
    #
    #   1. `extensions._sqlite_pragmas` opens every connection
    #      with `PRAGMA query_only=1` unless `mimir_is_broker` is
    #      true. The broker IS the writer; everyone else reads.
    #
    #   2. `cache._should_dispatch_to_broker` skips the RPC and
    #      writes directly when the caller is inside the broker
    #      process (otherwise the broker would self-RPC for its
    #      own ingest/backfill/warm cache.set calls).
    #
    # Override via MIMIR_IS_BROKER.
    mimir_is_broker: bool = False

    # True iff this process is one of the deploy containers (web,
    # tasks, or broker), used by `create_app` to refuse
    # `FLASK_DEBUG=true` on a deploy. Distinct from
    # `mimir_is_broker`: web + tasks set MIMIR_DEPLOY=true without
    # being the broker, and the broker container sets BOTH
    # (MIMIR_DEPLOY=true marks the deploy posture; MIMIR_IS_BROKER
    # marks "this is the writer process"). Dev / CLI invocations
    # leave it false so `flask run` debugging stays available.
    # Override via MIMIR_DEPLOY.
    mimir_deploy: bool = False

    # SQLite `PRAGMA analysis_limit` (max rows sampled per index by
    # ANALYZE). 0 means "no limit": ANALYZE reads every row of every
    # index. That is the default since #817.
    #
    # A non-zero limit is not a random sample. SQLite reads about that
    # many rows from the first key values and stops, so every index
    # with more rows per key than the limit records limit + 1 (4001 at
    # 4000) however large the real figure is. On production that put
    # `ix_article_lists_inbox_id` at 4001 per inbox against a real
    # average near 150k, and the daily bounded pass overwrote the
    # weekly full figures within a day. Full figures kept 474 of 476
    # captured statements on their plan and improved the other two.
    #
    # Cost, measured on the production corpus: a full pass holds the
    # writer lock for 16.7 s (2026-10-22); the bounded pass took 1.2
    # to 6.6 s. The pragma is set on every connection in
    # `mimir.extensions._sqlite_pragmas`, so the 0 applies to every
    # ANALYZE: post-migrate (before the broker serves, so on the
    # startup budget), after ingest, and `mimir analyze`.
    #
    # History: 400 (1.35.1) mis-planned the recursive CTEs badly (a
    # 15-message `get_thread` took 400 s); 4000 (1.36.4) was the
    # compromise until the cap itself was found to be the problem.
    #
    # Set to a positive number to cap the sample again (faster,
    # inexact). Override via ANALYZE_LIMIT.
    analyze_limit: int = 0

    # Auto-ANALYZE threshold. After `ingest_inbox` finishes a run, if
    # `new + linked` across that run's epochs reaches this many rows,
    # we issue ANALYZE so the SQLite planner doesn't keep stale stats
    # from when the tables were small (or empty, post-migration). The
    # canonical trigger is the first ingest of a freshly-added inbox,
    # which lands the entire archive in one go and would otherwise leave
    # the planner blind until the next scheduled ANALYZE. Set to 0 to
    # disable.
    analyze_after_ingest_rows: int = 10000

    # Per-tree config for the multi-tree mainline lifecycle surfaces.
    # Mirrored locally so the `update-mainline` CLI can read MAINTAINERS
    # and walk commit-message `Link:` trailers for the patch-page
    # "applied as <sha>" surface. Two-way seed:
    #   1. TREES__<slug>__URL / TREES__<slug>__PATH env keys set -> take env
    #      as-is. Uses `env_nested_delimiter="__"` in model_config; slugs
    #      that contain dashes must be env-encoded with underscores
    #      (e.g. `TREES__net_next__URL`) and are normalised back to dashes.
    #      Important: ANY TREES__* env key REPLACES the whole default set;
    #      pydantic-settings does not merge individual keys into the defaults.
    #   2. None set -> seed the full curated default set.
    trees: dict[str, TreeConfig] = {}

    @field_validator("trees", mode="after")
    @classmethod
    def _seed_trees(cls, v: dict) -> dict:
        if v:
            # Operator set TREES__* env keys -> use as-is.
            # Normalise underscore-style slugs to dash-style for the
            # user-facing labels.
            return {k.replace("_", "-"): cfg for k, cfg in v.items()}
        return _curated_default_trees()

    # IndexNow (https://www.indexnow.org/). Push-notification protocol
    # for new URLs, consumed by Bing/Yandex/Naver/Seznam/Yep. Google
    # is *not* a consumer as of this writing, set this expecting Bing
    # to discover new posts faster, not Google.
    #
    # Setting `indexnow_key` enables the feature: the `update` CLI
    # batches the canonical URLs of articles created in each tick
    # and POSTs them to `indexnow_endpoint`. The key is a 8-128 char
    # hex/alnum string (the spec is loose); generate one with e.g.
    # `python -c "import secrets; print(secrets.token_hex(16))"` and
    # set INDEXNOW_KEY in the env. Unset = feature disabled, no calls
    # made, key-verification route not registered.
    #
    # `site_base_url` MUST also be set for IndexNow to work, the
    # protocol needs an absolute host and the keyLocation URL.
    #
    # `indexnow_max_per_tick` is the "looks like a backfill, skip the
    # push" cap. When `update` produces more new articles than this
    # in one tick (fresh deploy, post-outage catch-up, etc.), the
    # whole notification is skipped and a warning logged; the sitemap
    # remains the discovery path for the backlog. Steady-state lkml
    # is dozens to hundreds per tick, so 1000 is a comfortable
    # ceiling.
    indexnow_key: str | None = None
    indexnow_endpoint: str = "https://api.indexnow.org/indexnow"
    indexnow_max_per_tick: int = 1000

    @field_validator("indexnow_endpoint")
    @classmethod
    def _validate_outbound_endpoint(cls, v: str) -> str:
        # Shared with `Inbox.upstream_url`'s validator and
        # `mimir.inboxes.validate_upstream_url` (admin CLI). Rejects
        # `http://`, `file://`, `git://`, and IP literals in
        # loopback / link-local / RFC 1918 / ULA / etc. so a
        # mistyped or attacker-controlled env doesn't aim mimir at
        # the deploy's internal network or cloud-metadata service.
        return validate_outbound_url(v, allow_http=False)

    # security.txt (RFC 9116). Setting `security_contact` enables
    # /security.txt and /.well-known/security.txt; the routes 404 when
    # it's empty. The `Expires:` field is computed at request time as
    # `now + 1 year`, so there's no annual rotation chore.
    security_contact: str | None = None  # e.g. "mailto:security@example.com"
    security_policy_url: str | None = None
    security_encryption_url: str | None = None
    security_preferred_languages: str = "en"

    # Per-subsystem triage queue thresholds (issue #209). Defaults
    # chosen with the kernel-review cadence in mind: 14 days is
    # roughly one merge-window iteration, by which point a patch
    # with review trailers but no pickup deserves a maintainer's
    # attention; 30 days of total silence is the point at which a
    # patch is unlikely to land without a re-post. Override via
    # SUBSYSTEM_NEEDS_ATTENTION_DAYS / SUBSYSTEM_QUIET_DAYS.
    subsystem_needs_attention_days: int = 14
    subsystem_quiet_days: int = 30

    # Hard upper bound on triage-queue age (#209). Patches older than
    # this are considered abandoned, not "needs attention" or
    # "quiet": the author has moved on, the patch won't land without
    # a fresh post. Bounding the queue this way is also load-bearing
    # for the query plan, walking `ix_articles_date` ASC over an
    # unbounded range scans the whole `articles` table for popular
    # subsystems (17.4M rows as of 2026-08-04; the "6M+" this used to
    # say was lkml alone read as the archive, the trap CLAUDE.md
    # documents). Measured at 8 s cold miss when that figure was
    # written; over 180 days it is ~200k rows and milliseconds.
    # Override via SUBSYSTEM_TRIAGE_MAX_AGE_DAYS.
    subsystem_triage_max_age_days: int = 180

    # Messages rendered per PAGE of the whole-thread view
    # (`/<inbox>/<YYYY>/<MM>/<root_id>/t`, and `/t/<page>` beyond the
    # first). Since 3.8.0 this is a PAGE-WEIGHT budget and nothing else:
    # the view paginates, so no message is dropped at any value, which is
    # what it used to control.
    #
    # The question it answers is "how large a download is one page of a
    # flattened thread". Per-message weight varies about 10x by inbox,
    # measured against production 2026-08-02: ~11 KB on linux-fsdevel,
    # ~101 KB on syzbot, whose CI reports carry kernel logs and
    # reproducers. So:
    #
    # The per-message weights are MEASURED; the per-page figures below
    # are that weight times the cap, i.e. derived, not observed. Stated
    # because an earlier version of this table and the changelog quoted
    # two different numbers for the same page (2.4 MB and 2.6 MB), which
    # is what an unlabelled extrapolation invites.
    #
    #     cap  50 -> ~0.5 MB typical,  ~5.1 MB on syzbot
    #     cap  75 -> ~0.8 MB typical,  ~7.6 MB
    #     cap 100 -> ~1.1 MB typical,  ~10.1 MB
    #     cap 200 -> ~2.2 MB typical,  ~20.2 MB
    #
    # 75 keeps the worst inbox comfortably under 10 MB while leaving the
    # overwhelmingly common case under a megabyte.
    #
    # A CRAWL-BUDGET argument used to sit here as the deciding factor and
    # has been removed because it was wrong, not because it lost. Every
    # page is sitemapped, and the note compared the extra pagination URLs
    # (60,653 at cap 50, 13,013 at 100, 2,759 at 200) against "32,229
    # indexed pages". 32,229 is the number of sitemap FILES in the index,
    # not URLs; the population those entries actually join is the
    # 6,074,810 advertised thread URLs, so the real cost is +1.0%,
    # +0.21% and +0.045%. Negligible at every candidate, and therefore
    # unable to discriminate between them. The raw counts were right and
    # current; the denominator was a different thing entirely. Standing
    # rule, earned again: name the population a figure counts, next to
    # the figure.
    #
    # A second, byte-based budget would dominate this one and is
    # deliberately NOT added: one knob set conservatively gets most of
    # the benefit, and two interacting caps is more machinery than this
    # has earned. Revisit if a real page misbehaves.
    #
    # CHANGING THIS RE-SLICES EVERY THREAD. Advertised `/t/<page>` URLs
    # move or 404, every message's canonical moves with them, and the
    # sitemap cache holds the old slicing for up to `SITEMAP_TTL_SEC`.
    # Treat it like `SITEMAP_URLS_PER_PAGE`, which carries the same
    # hazard and says so: bump `cache.NAMESPACE_VERSION` alongside any
    # change so stale sitemaps cannot advertise the old page count.
    #
    # The 100 -> 75 change on this branch did NOT bump it, and that is
    # the one exemption: thread pagination is unreleased, so no cached
    # sitemap row anywhere was ever cut at 100. Stated explicitly
    # because otherwise this reads as a rule sitting directly above a
    # counter-example, with nothing telling a future reader which of
    # the two to follow. Any change after this ships needs the bump.
    #
    # Blob cost still applies per rendered message (~10 ms each, one
    # `read_messages` bulk fetch per page) but is no longer the binding
    # constraint the pre-3.8.0 comment here described: it reasoned
    # entirely about blob fetches while 93% of the response was a
    # recursive thread walk that no longer happens.
    #
    # Also the threshold above which the message page windows its
    # thread tree (#798): a thread that fits on one thread page is shown
    # whole there.
    #
    # Override via THREAD_VIEW_RENDER_CAP. Must be at least 1; startup
    # fails otherwise, so callers can use the value unclamped. The sitemap
    # index and per-inbox sitemap keys include it, so a change misses them all:
    # set it on all three services together and run
    # `mimir warm-cache --tier slow` (docs/operations.md).
    thread_view_render_cap: int = Field(default=75, ge=1)

    # Hard upper bound on age of "related patches" surfaced on a
    # message page (1.36.3). The `recent_patches_touching` helper
    # joins `article_files` by `path IN (...)` and asks for the
    # top-N most-recent matches. Without a date bound, a patch that
    # touches a popular file (Makefile, include/linux/kernel.h,
    # anything in arch/x86/) matches millions of rows and the
    # GROUP BY + ORDER BY DESC materialises + sorts the whole set
    # before LIMITing, ran for 5+ minutes per request on the
    # production corpus and starved gunicorn workers under bot
    # load. Bounded to a 180-day window the planner walks the date
    # index DESC and tests EXISTS per candidate article via the
    # `(article_id, path)` PK on `article_files`; cold miss drops
    # to milliseconds. Same rationale + same default as
    # `subsystem_triage_max_age_days` above. Override via
    # RECENT_PATCHES_MAX_AGE_DAYS.
    recent_patches_max_age_days: int = 180

    # Candidate window (days) for the related-discussions panel on
    # non-patch threads. Bounds every candidate query on the date
    # index so nothing scans the full corpus; deploy-varying because
    # corpus sizes differ. See #71 and
    # _claude/specs/2026-06-12-related-discussions-design.md.
    # Override via RELATED_DISCUSSIONS_WINDOW_DAYS.
    related_discussions_window_days: int = 365

    # Senders whose threads are treated as automated/bot noise by the
    # related-discussions panel (#71): their threads are dropped from
    # candidates and the panel is suppressed on bot-authored roots.
    # Substring-matched (case-insensitive) against `Article.author`.
    # Default covers the three high-volume kernel bots.
    # Override via env as a JSON array (RELATED_DISCUSSIONS_BOT_SENDERS).
    related_discussions_bot_senders: list[str] = [
        "syzkaller.appspotmail.com",
        "lkp@intel.com",
        "tip-bot2@linutronix.de",
    ]

    # Crawler budgets (#761). Measured on production 2026-10-18, one
    # crawler (Applebot) made 1,828 of 2,041 requests in a 10-minute
    # sample and nothing in front of mimir can slow it without slowing
    # readers, so the web tier answers 429 + Retry-After past a budget.
    # Every default is 0 (off): deploying changes nothing until an
    # operator opts in. See mimir/web/crawler_budget.py.
    #
    # Master switch and budget for ALL configured crawlers combined,
    # requests per minute across the whole web tier. 0 = the whole gate
    # is off and nothing is gated, whatever CRAWLER_UA_BUDGETS says.
    crawler_budget_per_minute: int = Field(default=0, ge=0)

    # Overrides on top of DEFAULT_CRAWLER_UA_BUDGETS (merged, not
    # replaced: `{"Applebot": 50}` changes only Applebot). Each key is a
    # case-insensitive substring of the raw User-Agent header (first
    # match, in merged order); the value is that crawler's own share as
    # a PERCENTAGE (1..100) of the global budget, so 10 with a global
    # 120 is 12 a minute, and 0 DROPS a default entry. Env as JSON:
    #   CRAWLER_UA_BUDGETS='{"Applebot": 50, "Bytespider": 0}'
    # Because `env_nested_delimiter="__"` is active,
    # CRAWLER_UA_BUDGETS__Applebot=50 also works (pydantic-settings
    # lowercases that key, which is fine: matching and merging both
    # ignore case). Keys are plain substrings
    # with no slash rule, so pick distinctive ones; a key equal to a
    # marker below is refused ("bot" would gate the CUBOT browser).
    crawler_ua_budgets: dict[str, int] = {}

    # Crawlers that are NOT listed above are still recognised by a
    # product token: a name ending in one of these markers, followed by
    # a slash (AhrefsBot/7.0, GPTBot/1.0, curl/8.5.0); a token that is
    # just the marker counts. The slash keeps a browser on a device such
    # as `CUBOT X30` from being gated. Script tools (curl, wget,
    # python-requests) are markers too, so operator scripts and smoke
    # checks using them are gated once the global budget is set.
    # Case-insensitive. JSON array in env (CRAWLER_UA_MARKERS).
    crawler_ua_markers: list[str] = [
        "bot",
        "crawler",
        "spider",
        "curl",
        "wget",
        "python-requests",
        "lightpanda",
    ]

    @property
    def effective_crawler_ua_budgets(self) -> dict[str, int]:
        """The listed crawlers the gate uses: the defaults first in
        their order, then new keys; an overridden default keeps its
        position and a value of 0 removes the entry."""
        merged = dict(DEFAULT_CRAWLER_UA_BUDGETS)
        # Keys merge case-insensitively: the nested env form lowercases
        # its key, and `applebot` must override `Applebot`, not sit
        # beside it.
        spelling = {k.lower(): k for k in merged}
        for key, pct in self.crawler_ua_budgets.items():
            merged[spelling.setdefault(key.lower(), key)] = pct
        return {k: v for k, v in merged.items() if v > 0}

    @model_validator(mode="after")
    def _listed_keys_are_not_markers(self) -> Settings:
        # A listed key is a bare substring, a marker is only a suffix
        # before a slash: a key equal to a marker would gate any UA
        # that merely contains it (CUBOT). Checked on the merged map so
        # defaults and overrides are both covered.
        markers = {m.lower() for m in self.crawler_ua_markers}
        clash = sorted(
            k for k in self.effective_crawler_ua_budgets if k.lower() in markers
        )
        if clash:
            raise ValueError(f"crawler_ua_budgets keys equal to markers: {clash}")
        return self

    @field_validator("crawler_ua_markers")
    @classmethod
    def _validate_crawler_ua_markers(cls, v: list[str]) -> list[str]:
        # A blank marker makes the token regex match any `name/` pair.
        if any(not m.strip() for m in v):
            raise ValueError("crawler_ua_markers entries must not be blank")
        return v

    # Share (percentage of the global budget) for a recognised crawler
    # with no entry in CRAWLER_UA_BUDGETS. With the global budget set,
    # every such crawler is held to this unless listed.
    crawler_default_ua_pct: int = Field(default=10, ge=1, le=100)

    # Unlisted crawlers also draw on ONE shared bucket at this
    # percentage of the global budget. Without it, invented names
    # (a-bot/1, b-bot/1, ...) each get their own default share and a
    # handful drain the global bucket, so real crawlers get 429s.
    # Listed crawlers never touch it.
    crawler_unlisted_pool_pct: int = Field(default=30, ge=1, le=100)

    # Retry-After on a refused request, seconds. Long enough that a
    # crawler backs off past a bucket refill instead of retrying at once.
    crawler_retry_after_sec: int = Field(default=120, ge=1)

    # gunicorn worker processes, read from the `WORKERS` the container
    # already sets for `--workers`. The budget is enforced per worker at
    # budget / web_workers, so this must match the real worker count.
    web_workers: int = Field(default=1, ge=1, validation_alias="WORKERS")

    @field_validator("crawler_ua_budgets")
    @classmethod
    def _validate_crawler_ua_budgets(cls, v: dict[str, int]) -> dict[str, int]:
        # A blank key is a substring of every User-Agent, so it would
        # gate browsers and requests with no User-Agent at all.
        if any(not k.strip() for k in v):
            raise ValueError("crawler_ua_budgets keys must not be blank")
        if any(not 0 <= n <= 100 for n in v.values()):
            raise ValueError(
                "crawler_ua_budgets values are percentages, 1 to 100, or 0 to drop"
            )
        return v


settings = Settings()
