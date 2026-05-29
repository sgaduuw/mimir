from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mimir._outbound import validate_outbound_url

# Project root: <root>/mimir/config.py → parent → parent. Resolving
# follows symlinks so a container running mimir from a bind-mounted
# read-only volume still gets a sane absolute path. NB: this assumes a
# checked-out source tree, under `pip install .` mimir lives in
# site-packages and PROJECT_ROOT becomes meaningless. Set DATABASE_URL
# explicitly in any deployment that doesn't ship the tree as-is.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Single source of truth for the Linus mainline tree URL and local
# mirror path. Referenced by three distinct consumers:
#   - Settings.mainline_tree_url / mainline_tree_path field defaults
#   - _seed_trees() legacy-shim drift detection
#   - _curated_default_trees() linus entry construction
# Defining them once here prevents silent drift where a change to one
# default leaves the others pointing at the old value.
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
    # DELETEs the tree's existing rows then re-walks from scratch,
    # letting INSERT-OR-IGNORE absorb intra-walk dups.
    rebases: bool = False
    # Per-tree cadence: skip the tree on `update_mainline` ticks that
    # arrive sooner than this since the last successful walk.
    walk_every_seconds: int = 1500  # 25 min default (Linus)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        return validate_outbound_url(v, allow_http=False)


def _curated_default_trees() -> dict[str, "TreeConfig"]:
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
        "pci": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/helgaas/pci.git",
            path=Path("Mainline/pci.git"),
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

    # Senders whose email address is shown in full in the UI. Everyone
    # else's email gets hidden (display name kept). Substring match against
    # the address part, case-insensitive. Defaults cover well-known kernel
    # maintainers and the kernel.org domain. Override via env (comma-sep
    # in EMAIL_ALLOWLIST).
    email_allowlist: list[str] = [
        "torvalds@",
        "gregkh@",
        "@kernel.org",
    ]

    # Inboxes that should appear first on the meta-index `/`, in this
    # order, regardless of alphabetical order. The rest follow
    # alphabetically. Override via PINNED_INBOXES (comma-separated).
    # Default pins lkml since this archive treats it as the focal list;
    # set to empty to fall back to pure alphabetical.
    pinned_inboxes: list[str] = ["lkml"]

    # Extra mailing-list host suffixes that augment the default
    # `mimir.canonical.LIST_HOST_SUFFIXES` set. Operators with archives
    # for lists hosted on a domain we don't yet ship as a default can
    # add it here without a patch; the entries are unioned with the
    # baseline. Default empty (baseline covers kernel.org-adjacent
    # ecosystems). Override via LIST_HOST_SUFFIX_OVERRIDES
    # (comma-separated).
    list_host_suffix_overrides: list[str] = []

    # Inboxes treated as a *firehose* during canonical-inbox resolution
    # for cross-posts. A message posted to a topical list AND lkml is
    # canonically the topical-list version, even when lkml appears
    # first in the To/Cc walk; the firehose copy is the cross-post.
    # An inbox in this list is only chosen as canonical when no
    # non-firehose inbox in the article's link set matches. Mirrors
    # how kernel devs (and Google's crawler) actually treat the
    # topical lists as the conversational home. Override via
    # CANONICAL_DEMOTED_INBOXES (comma-separated).
    canonical_demoted_inboxes: list[str] = ["lkml"]

    # Number of trusted reverse-proxy hops in front of the app. When
    # > 0, Werkzeug's ProxyFix is wired up so the last N entries of
    # X-Forwarded-{For,Proto,Host} are honoured, request.remote_addr
    # then reflects the real client IP instead of the proxy's address,
    # request.scheme is correct under HTTPS-terminating proxies, etc.
    # Leave at 0 if the app is reachable directly, otherwise anyone
    # could spoof those values via a forged XFF header.
    trusted_proxy_hops: int = 0

    # SQLite per-connection `busy_timeout` (milliseconds) for the
    # write-heavy CLI workloads wrapped in
    # `mimir.extensions.write_transaction()` (backfills,
    # ingest_inbox, update-mainline). The web-tier default
    # (`sqlite_busy_timeout_ms` below) is intentionally short so a
    # stuck request can't hang for minutes; a one-shot backfill has
    # no latency budget and benefits from much more patience. 60s
    # comfortably rides out the cache-write burst that follows an
    # archive_stats invalidation (every cold-miss page render writes
    # its computed value, so a few hundred page renders in a 5s
    # window will starve a backfill on the default timeout). Tunable
    # via SQLITE_BUSY_TIMEOUT_MS_WRITES.
    sqlite_busy_timeout_ms_writes: int = 60000

    # SQLite per-connection `busy_timeout` (milliseconds). When a
    # writer hits a locked DB, SQLite waits up to this long before
    # raising `SQLITE_BUSY`. Default 0 turns transient contention
    # (scheduler ingest / analyze / vacuum overlapping a web cache
    # write) into hard 500s; 5s rides out normal contention windows.
    # VACUUM on the full archive can outlast this; that's intentional,
    # we'd rather surface a true VACUUM-vs-write conflict than mask
    # it with a multi-minute hang. Override via SQLITE_BUSY_TIMEOUT_MS.
    sqlite_busy_timeout_ms: int = 5000

    # Write-broker socket path. The broker daemon (`mimir broker
    # --socket PATH`) is the sole SQLite writer process in mimir
    # 2.0.0; every other process (web, tasks, CLI invocations)
    # routes cache + admin writes through this UNIX socket and
    # opens its own DB connections with `PRAGMA query_only=1`.
    # Default `/data/.broker.sock` matches the canonical compose
    # layout; operators on non-standard layouts override via
    # BROKER_SOCKET_PATH.
    broker_socket_path: Path = Path("/data/.broker.sock")

    # Slow-write-transaction WARNING threshold (milliseconds). When
    # a block wrapped in `mimir.extensions.write_transaction(label=...)`
    # holds the SQLite writer lock longer than this, the COMMIT
    # (or ROLLBACK) listener logs a WARNING with the label and
    # elapsed time. Operator diagnostic for cross-process writer-lock
    # contention: a slow `write_transaction` on the scheduler side
    # correlates 1:1 with a slow broker dispatch on the cache side.
    # Set to 0 (or negative) to disable. Default 1000 ms: healthy
    # commits land in single-digit ms, so 1 s is "this is interesting,
    # not noise". Override via WRITE_TRANSACTION_SLOW_LOG_MS.
    write_transaction_slow_log_ms: int = 1000

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

    # Backfill chunk budget (seconds) for broker-mode cooperative
    # scheduling (Phase 2.2). When a backfill RPC handler runs inside
    # the broker, it walks for at most this long before committing
    # and returning `partial=True, continuation=<cursor>` so the CLI
    # loop can issue a follow-up RPC. Between two chunk-RPCs the
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
    # ANALYZE). Default 0 means "no limit" and ANALYZE scans every
    # row of every index, which on the 11M-row prod corpus holds
    # the writer lock for ~25 s. SQLite's docs hint at "1000 or
    # 1500 for very large databases"; **4000** is a 10x margin on
    # that for an 11M-row corpus and produces accurate-enough stats
    # for the recursive-CTE shapes the read path leans on (thread
    # walk via `ix_articles_thread_parent`, the per-inbox
    # `(article_id, inbox_id)` covering index on `article_lists`,
    # etc.). ANALYZE wall time scales roughly linearly in the
    # limit; 4000 measures at ~1-3 s on this corpus, still 10x
    # better than the 25-30 s full scan. The pragma is set on every
    # connection in `mimir.extensions._sqlite_pragmas`, so it
    # applies uniformly to `mimir analyze`, auto-ANALYZE-after-
    # ingest, and any ad-hoc session running ANALYZE.
    #
    # **1.36.4 history**: 1.35.1 shipped this at 400 on the basis
    # that SQLite docs called 400 "appropriate for typical
    # workloads." It produced catastrophically wrong plans on the
    # production multi-inbox corpus, e.g. `get_thread` for a 15-
    # message thread on lkml took 400 s instead of milliseconds
    # because the planner mis-estimated the recursive CTE's join
    # cardinality from undersampled `article_lists` /
    # `thread_parent` stats. A full ANALYZE (`analysis_limit=0`)
    # restored 200-1700x speedups instantly. 4000 keeps the post-
    # migrate ANALYZE fast at boot while giving the planner the
    # samples it needs at this scale. The weekly full ANALYZE on
    # the scheduler tick is the safety net for distribution drift.
    #
    # Set to 0 to restore full-scan stats (slow but exact).
    # Override via ANALYZE_LIMIT.
    analyze_limit: int = 4000

    # Auto-ANALYZE threshold. After `ingest_inbox` finishes a run, if
    # `new + linked` across that run's epochs reaches this many rows,
    # we issue ANALYZE so the SQLite planner doesn't keep stale stats
    # from when the tables were small (or empty, post-migration). The
    # canonical trigger is the first ingest of a freshly-added inbox,
    # which lands the entire archive in one go and would otherwise leave
    # the planner blind until the next scheduled ANALYZE. Set to 0 to
    # disable.
    analyze_after_ingest_rows: int = 10000

    # Mainline tree (Linus's `linux.git`). Mirrored locally so the
    # `update-mainline` CLI can read MAINTAINERS, and, in a later
    # slice, walk commit-message `Link:` trailers for the patch-page
    # "applied as <sha>" surface.
    #
    # Path follows the same pattern as the per-inbox mirrors under
    # `Inboxes/`; pick `Mainline/linux.git` so a typical deploy gets
    # `<data root>/Inboxes/<list>/git` and `<data root>/Mainline/linux.git`
    # side-by-side. Override via MAINLINE_TREE_PATH / MAINLINE_TREE_URL.
    mainline_tree_path: Path = _LINUS_DEFAULT_PATH
    mainline_tree_url: str = _LINUS_DEFAULT_URL

    # Per-tree config for the multi-tree mainline lifecycle surfaces.
    # Three-way seed:
    #   1. TREES__<slug>__URL / TREES__<slug>__PATH env keys set -> take env
    #      as-is. Uses `env_nested_delimiter="__"` in model_config; slugs
    #      that contain dashes must be env-encoded with underscores
    #      (e.g. `TREES__net_next__URL`) and are normalised back to dashes.
    #      Important: ANY TREES__* env key REPLACES the whole default set;
    #      pydantic-settings does not merge individual keys into the defaults.
    #   2. Legacy MAINLINE_TREE_URL/PATH set -> seed only `linus`.
    #   3. Neither -> seed the full curated default set.
    # The legacy MAINLINE_TREE_URL / MAINLINE_TREE_PATH scalars
    # above are deprecated; removed in the next major.
    trees: dict[str, TreeConfig] = {}

    @field_validator("trees", mode="after")
    @classmethod
    def _seed_trees(cls, v: dict, info) -> dict:
        if v:
            # Operator set TREES__* env keys -> use as-is.
            # Normalise underscore-style slugs to dash-style for the
            # user-facing labels.
            return {k.replace("_", "-"): cfg for k, cfg in v.items()}

        data = info.data
        legacy_url = data.get("mainline_tree_url")
        legacy_path = data.get("mainline_tree_path")
        if (legacy_url and legacy_url != _LINUS_DEFAULT_URL) or (
            legacy_path and legacy_path != _LINUS_DEFAULT_PATH
        ):
            return {
                "linus": TreeConfig(
                    url=legacy_url or _LINUS_DEFAULT_URL,
                    path=legacy_path or _LINUS_DEFAULT_PATH,
                    walk_every_seconds=1500,
                ),
            }

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

    @field_validator("mainline_tree_url", "indexnow_endpoint")
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
    # unbounded range scans 6M+ rows for popular subsystems (8 s
    # cold miss); over 180 days it's ~200k rows and milliseconds.
    # Override via SUBSYSTEM_TRIAGE_MAX_AGE_DAYS.
    subsystem_triage_max_age_days: int = 180

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


settings = Settings()
