"""What a 404 page may offer a reader (#1002, #984).

The page names the path, says what kind of address it looks like, and
links to places that exist: the front page always, and from a path of the
shape `/<inbox>/<year>/<month>/...` the inbox dashboard and the month, but
only when the routes would serve them. The inbox is looked up with the
routes' own `_find_inbox` and the month checked with their own
`_month_in_range`, so the page never links to another 404.

Kept cheap and unable to fail, because it runs on every 404, and bots
probe for `/wp-login.php` by the thousand: a first segment that cannot be
an inbox name costs no query, one that can costs one indexed read, and a
database error leaves a plain 404 behind.
"""

import logging
from dataclasses import dataclass
from datetime import date

from mimir._inbox_name import INBOX_NAME_RE, inbox_path
from mimir.extensions import SessionLocal
from mimir.web.urls import _find_inbox, _month_in_range, _month_path

logger = logging.getLogger(__name__)

# The path is echoed to the reader, so cap it. A Message-ID is at most 998
# characters (RFC 5322), which is also the form's `maxlength`.
SHOWN_PATH_MAX = 120
PREFILL_MAX = 998


@dataclass
class NotFoundHelp:
    heading: str
    shown_path: str
    links: list[tuple[str, str]]
    prefill: str


def _number(segment: str) -> int | None:
    return int(segment) if segment.isascii() and segment.isdecimal() else None


def not_found_help(path: str, args) -> NotFoundHelp:
    segments = [s for s in path.split("/") if s]
    numbers = [_number(s) for s in segments[1:4]]
    is_message = len(segments) >= 4 and None not in numbers
    links: list[tuple[str, str]] = []
    inbox_name = _existing_inbox(segments[0]) if segments else None
    if inbox_name:
        if len(segments) >= 3 and None not in numbers[:2]:
            year, month = numbers[0], numbers[1]
            if _month_in_range(year, month):
                label = date(year, month, 1).strftime("%B %Y")
                links.append(
                    (f"{label} in {inbox_name}", _month_path(inbox_name, year, month))
                )
        links.append((f"The {inbox_name} dashboard", inbox_path(inbox_name)))
    links.append(("All inboxes", "/"))
    shown = path if len(path) <= SHOWN_PATH_MAX else path[:SHOWN_PATH_MAX] + "…"
    return NotFoundHelp(
        heading=(
            "This message isn't in the archive" if is_message else "Page not found"
        ),
        shown_path=shown,
        links=links,
        prefill=_prefill(segments, args),
    )


def _existing_inbox(segment: str) -> str | None:
    if not INBOX_NAME_RE.fullmatch(segment):
        return None
    try:
        with SessionLocal() as session:
            inbox = _find_inbox(session, segment)
            return inbox.name if inbox else None
    except Exception:  # an error page must not fail on its own lookup
        logger.warning("404 page: inbox lookup failed", exc_info=True)
        return None


def _prefill(segments: list[str], args) -> str:
    """Text for the Message-ID form: what a missed `/m/?id=` lookup was
    given, else the first path segment that looks like an ID (it has an
    `@`)."""
    typed = args.get("id") or next((s for s in segments if "@" in s), "")
    return typed[:PREFILL_MAX]
