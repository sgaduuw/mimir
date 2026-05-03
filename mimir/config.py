from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    flask_debug: bool = False
    secret_key: str = Field(min_length=16)

    database_url: str = "sqlite:///mimir.db"
    cache_path: Path = Path("mimir-cache.pickle")

    lkml_mirror_path: Path = Path("lkml/git")
    upstream_url: str = "https://lore.kernel.org/lkml"

    # Name used as the first URL segment, e.g. /lkml/2024/01/<msgid>.
    # Single-list world for now; when a second list is added we add an
    # Article.list column and drop the hardcoded match in the route.
    list_name: str = "lkml"

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
