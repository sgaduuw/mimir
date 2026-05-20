from pathlib import Path
from typing import Literal

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        extra="ignore",
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

    # Quiesce DB writes from this process for the lifetime of the
    # container. Used as a maintenance toggle: when a long admin
    # operation (e.g. `admin canonicals backfill --reprocess`) needs
    # the writer lock to itself, set READ_ONLY_DB=true on the web
    # container so its gunicorn workers stop competing for the lock
    # via `cache.set`. The scheduler sidecar keeps the default (False)
    # so it can still run migrations / ingest / cache hygiene.
    #
    # Two layers when enabled:
    #   1. `cache.set` / `cache.delete` / `cache.purge_expired` /
    #      `cache.delete_for_inbox` short-circuit to no-ops so the
    #      best-effort write path doesn't log a warning per request.
    #   2. `PRAGMA query_only=1` is issued on every connection as a
    #      belt-and-braces safety net catching any non-cache write
    #      path; offending statements raise `OperationalError:
    #      attempt to write a readonly database`.
    #
    # Intentionally not persisted anywhere: the toggle is a runtime
    # env var. A normal container restart (without READ_ONLY_DB set)
    # restores read-write mode automatically. Override via
    # READ_ONLY_DB.
    read_only_db: bool = False

    # Write-broker integration. When `broker_socket_path` is set,
    # `cache.set` / `cache.delete` / `cache.delete_for_inbox` /
    # `cache.purge_expired` forward to the broker daemon at the given
    # UNIX-socket path instead of opening their own DB sessions. The
    # broker (a separate `mimir broker` process) is then the sole
    # writer to the cache table, eliminating SQLite writer-lock
    # contention between gunicorn workers and the scheduler sidecar.
    # Unset = direct-SQLite writes (today's behaviour). Override via
    # BROKER_SOCKET_PATH.
    broker_socket_path: Path | None = None

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

    # Slow-RPC warning threshold for the write-broker (milliseconds).
    # When the broker takes longer than this to handle a single RPC,
    # it logs a WARNING with the leading bytes of the request and
    # the elapsed time. Healthy RPCs commit in sub-ms; sustained
    # warnings indicate writer-lock contention (e.g. an admin
    # backfill running) or a slow operation
    # (`cache_delete_for_inbox` on a huge table). Set to 0 or
    # negative to disable. Override via BROKER_SLOW_RPC_WARN_MS.
    broker_slow_rpc_warn_ms: int = 100

    # Per-process role tag. Drives broker-mode side effects on
    # connection setup: when `broker_socket_path` is set AND
    # `mimir_role` is `"web"`, every SQLAlchemy connection is opened
    # with `PRAGMA query_only=1`. The scheduler sidecar
    # (`mimir_role="tasks"`) and the broker itself
    # (`mimir_role="broker"`) keep RW connections so their direct-
    # write paths (ingest, backfill, broker handlers) still work.
    # Unset = no role-based enforcement; broker mode then routes
    # writes through the broker on every process indiscriminately.
    # Override via MIMIR_ROLE.
    mimir_role: Literal["web", "tasks", "broker"] | None = None

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
    mainline_tree_path: Path = Path("Mainline/linux.git")
    mainline_tree_url: str = (
        "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
    )

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


settings = Settings()
