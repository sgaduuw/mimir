from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InboxConfig(BaseModel):
    """Env-side description of an inbox. The matching ORM model is
    `mimir.models.Inbox`, which is bootstrapped from these entries on
    app startup. The `Settings.inboxes` dict key becomes `Inbox.name`
    (URL slug)."""
    mirror_path: Path
    upstream_url: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    flask_debug: bool = False
    secret_key: str = Field(min_length=16)

    database_url: str = "sqlite:///mimir.db"
    cache_path: Path = Path("mimir-cache.pickle")

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

    # Authors whose recent messages get a dedicated tile on the landing
    # page. Maps a display label to a substring of the From-address; the
    # query is an indexed-date reverse scan with a LIKE filter, so any
    # number of trackers is cheap as long as each person posts often
    # enough to terminate the scan quickly.
    tracked_authors: dict[str, str] = {
        "Linus Torvalds": "torvalds@",
        "Greg KH": "gregkh@",
    }


settings = Settings()
