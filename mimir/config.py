from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: <root>/mimir/config.py → parent → parent. Resolving
# follows symlinks so a container running mimir from a bind-mounted
# read-only volume still gets a sane absolute path. NB: this assumes a
# checked-out source tree — under `pip install .` mimir lives in
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
    # wired correctly — typically right in production but a footgun
    # when the proxy chain is wrong. The 2026-05-11 external review
    # saw `http://` leaking into og:url + JSON-LD on a production
    # page; set `SITE_BASE_URL=https://ratatoskr.run` to force-
    # correct the scheme regardless of proxy config. Leave empty for
    # local dev so URLs match the test host.
    site_base_url: str = ""

    # Default: <project_root>/mimir.db, so cwd doesn't matter (systemd,
    # container, anywhere). Override with DATABASE_URL=... — typically
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

    # Number of trusted reverse-proxy hops in front of the app. When
    # > 0, Werkzeug's ProxyFix is wired up so the last N entries of
    # X-Forwarded-{For,Proto,Host} are honoured — request.remote_addr
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
    # VACUUM on the full archive can outlast this — that's intentional,
    # we'd rather surface a true VACUUM-vs-write conflict than mask
    # it with a multi-minute hang. Override via SQLITE_BUSY_TIMEOUT_MS.
    sqlite_busy_timeout_ms: int = 5000

    # Auto-ANALYZE threshold. After `ingest_inbox` finishes a run, if
    # `new + linked` across that run's epochs reaches this many rows,
    # we issue ANALYZE so the SQLite planner doesn't keep stale stats
    # from when the tables were small (or empty, post-migration). The
    # canonical trigger is the first ingest of a freshly-added inbox,
    # which lands the entire archive in one go and would otherwise leave
    # the planner blind until the next scheduled ANALYZE. Set to 0 to
    # disable.
    analyze_after_ingest_rows: int = 10000

    # IndexNow (https://www.indexnow.org/). Push-notification protocol
    # for new URLs, consumed by Bing/Yandex/Naver/Seznam/Yep. Google
    # is *not* a consumer as of this writing — set this expecting Bing
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
    # `site_base_url` MUST also be set for IndexNow to work — the
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

    # security.txt (RFC 9116). Setting `security_contact` enables
    # /security.txt and /.well-known/security.txt; the routes 404 when
    # it's empty. The `Expires:` field is computed at request time as
    # `now + 1 year`, so there's no annual rotation chore.
    security_contact: str | None = None  # e.g. "mailto:security@example.com"
    security_policy_url: str | None = None
    security_encryption_url: str | None = None
    security_preferred_languages: str = "en"


settings = Settings()
