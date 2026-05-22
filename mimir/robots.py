"""Manage the `/robots.txt` lifecycle: CRUD on `RobotsRule` rows plus
render the file content from the DB. Backs the `admin robots` CLI
subgroup and the `/robots.txt` route.

Shape mirrors `mimir.inboxes`: public CRUD functions are the single
write surface (shared between the CLI shim, the broker handlers, and
the future Flask admin UI), so validation lives in one place.

The `*` stanza is structural and seeded by the migration that creates
the table; `remove_rule('*')` is rejected, use `reset_rules()` to get
back to the seeded default if a `*` mutation has wandered.
"""

import re
from io import StringIO

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal, write_transaction
from mimir.models import RobotsRule

# User-agent token: 1-64 visible-ASCII chars, no whitespace.
# `*` is allowed (the default catch-all stanza). Robots.txt grammar
# is permissive; we tighten enough to keep the file un-pwn-able from
# a single CLI argument (no embedded newlines, no control characters).
_UA_RE = re.compile(r"^[\x21-\x7e]{1,64}$")

# Disallow path: starts with `/` or `*`, ≤256 chars, no control
# characters. Robots.txt's grammar treats Disallow as a prefix match;
# wildcard `*` and end-anchor `$` are non-standard extensions
# universally supported by the bots that matter.
_PATH_RE = re.compile(r"^[/*][\x21-\x7e]{0,255}$")

# Crawl-delay ceiling: 24h. Above that, the operator is doing
# something pathological; surface as a validation error instead of
# silently rendering a value bots will ignore.
_MAX_CRAWL_DELAY = 86_400

# Cloudflare Content-Signal keys + values, per
# https://blog.cloudflare.com/content-signals-policy/. Omitting a key
# means "no preference expressed" rather than an implicit `no`.
CONTENT_SIGNAL_KEYS: frozenset[str] = frozenset({"search", "ai-input", "ai-train"})
CONTENT_SIGNAL_VALUES: frozenset[str] = frozenset({"yes", "no"})


class RobotsValidationError(ValueError):
    """Raised when CRUD input fails validation. Message is
    operator-facing."""


class RobotsRuleNotFound(LookupError):
    """Raised when a CRUD op references a user-agent that doesn't
    have a row."""


def validate_user_agent(ua: str) -> str:
    if not isinstance(ua, str):
        raise RobotsValidationError("user_agent must be a string")
    ua = ua.strip()
    if not _UA_RE.fullmatch(ua):
        raise RobotsValidationError(
            f"user_agent {ua!r} must be 1-64 visible-ASCII characters "
            "with no whitespace"
        )
    return ua


def validate_disallow_path(path: str) -> str:
    if not isinstance(path, str):
        raise RobotsValidationError("disallow path must be a string")
    path = path.strip()
    if not _PATH_RE.fullmatch(path):
        raise RobotsValidationError(
            f"disallow path {path!r} must start with '/' or '*', "
            "1-256 visible-ASCII characters, no whitespace"
        )
    return path


def validate_content_signals(
    signals: dict[str, str] | None,
) -> dict[str, str] | None:
    """Normalize and validate a Content-Signal dict. Returns the same
    dict (lowercased keys + values) on success, None unchanged.
    Raises RobotsValidationError on bad input."""
    if signals is None:
        return None
    if not isinstance(signals, dict):
        raise RobotsValidationError("content_signals must be a dict or None")
    if not signals:
        return None
    out: dict[str, str] = {}
    for k, v in signals.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RobotsValidationError(
                "content_signals keys and values must be strings"
            )
        kk = k.strip().lower()
        vv = v.strip().lower()
        if kk not in CONTENT_SIGNAL_KEYS:
            raise RobotsValidationError(
                f"content_signal key {k!r} must be one of {sorted(CONTENT_SIGNAL_KEYS)}"
            )
        if vv not in CONTENT_SIGNAL_VALUES:
            raise RobotsValidationError(
                f"content_signal value {v!r} must be one of "
                f"{sorted(CONTENT_SIGNAL_VALUES)}"
            )
        out[kk] = vv
    return out


def validate_crawl_delay(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise RobotsValidationError("crawl_delay must be an integer or None")
    if seconds < 0 or seconds > _MAX_CRAWL_DELAY:
        raise RobotsValidationError(
            f"crawl_delay {seconds} must be between 0 and {_MAX_CRAWL_DELAY}"
        )
    return seconds


def list_rules(session: Session) -> list[RobotsRule]:
    """Return all rows. `*` first, remainder alphabetical."""
    rows = list(
        session.execute(select(RobotsRule).order_by(RobotsRule.user_agent)).scalars()
    )
    # Sort `*` to the front while preserving alphabetical order for
    # the rest. Cheap: this list is typically <10 rows.
    rows.sort(key=lambda r: (r.user_agent != "*", r.user_agent))
    return rows


def get_rule(session: Session, user_agent: str) -> RobotsRule | None:
    ua = validate_user_agent(user_agent)
    return session.execute(
        select(RobotsRule).where(RobotsRule.user_agent == ua)
    ).scalar_one_or_none()


def add_rule(
    user_agent: str,
    *,
    disallow: list[str] | None = None,
    crawl_delay: int | None = None,
    content_signals: dict[str, str] | None = None,
) -> RobotsRule:
    """Insert one new RobotsRule. Raises RobotsValidationError if the
    user-agent already has a row (use `update_rule` instead)."""
    ua = validate_user_agent(user_agent)
    paths = [validate_disallow_path(p) for p in (disallow or [])]
    delay = validate_crawl_delay(crawl_delay)
    signals = validate_content_signals(content_signals)
    with write_transaction(f"robots:add:{ua}"), SessionLocal() as session:
        if session.get(RobotsRule, ua) is not None:
            raise RobotsValidationError(
                f"user_agent {ua!r} already has a rule; use update"
            )
        rule = RobotsRule(
            user_agent=ua,
            crawl_delay=delay,
            disallow_paths=paths or None,
            content_signals=signals or None,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        session.expunge(rule)
    return rule


def update_rule(
    user_agent: str,
    *,
    add_disallow: list[str] | None = None,
    remove_disallow: list[str] | None = None,
    crawl_delay: int | None = None,
    clear_crawl_delay: bool = False,
    set_content_signal: dict[str, str] | None = None,
    clear_content_signal: list[str] | None = None,
    clear_all_content_signals: bool = False,
) -> RobotsRule:
    """Mutate an existing RobotsRule. `crawl_delay` sets the value;
    `clear_crawl_delay=True` sets it to NULL (distinct from
    `crawl_delay=None` which means "don't touch"). Disallow path
    add/remove is set-semantic: adding an already-present path is a
    no-op, removing a missing path is a no-op.

    Content-Signal mutations parallel the disallow shape:
    `set_content_signal` (dict) upserts the supplied keys;
    `clear_content_signal` (list of keys) drops them; both can be
    combined in one call. `clear_all_content_signals=True` wipes
    the dict to NULL and is mutually exclusive with the other two."""
    ua = validate_user_agent(user_agent)
    adds = [validate_disallow_path(p) for p in (add_disallow or [])]
    removes = [validate_disallow_path(p) for p in (remove_disallow or [])]
    delay = validate_crawl_delay(crawl_delay)
    if delay is not None and clear_crawl_delay:
        raise RobotsValidationError("set crawl_delay or clear_crawl_delay, not both")
    set_signals = validate_content_signals(set_content_signal)
    if clear_content_signal:
        bad = [k for k in clear_content_signal if k not in CONTENT_SIGNAL_KEYS]
        if bad:
            raise RobotsValidationError(
                f"unknown content_signal key(s) {bad!r}; valid keys are "
                f"{sorted(CONTENT_SIGNAL_KEYS)}"
            )
    if clear_all_content_signals and (set_signals or clear_content_signal):
        raise RobotsValidationError(
            "clear_all_content_signals is mutually exclusive with set/clear"
        )
    with write_transaction(f"robots:update:{ua}"), SessionLocal() as session:
        rule = session.get(RobotsRule, ua)
        if rule is None:
            raise RobotsRuleNotFound(f"no rule for user_agent {ua!r}")
        paths = list(rule.disallow_paths or [])
        for p in adds:
            if p not in paths:
                paths.append(p)
        if removes:
            paths = [p for p in paths if p not in removes]
        rule.disallow_paths = paths or None
        if clear_crawl_delay:
            rule.crawl_delay = None
        elif delay is not None:
            rule.crawl_delay = delay
        # Content-Signal mutation. set/clear merge into existing dict;
        # clear_all wipes to NULL.
        if clear_all_content_signals:
            rule.content_signals = None
        else:
            signals = dict(rule.content_signals or {})
            if set_signals:
                signals.update(set_signals)
            for k in clear_content_signal or []:
                signals.pop(k, None)
            rule.content_signals = signals or None
        session.commit()
        session.refresh(rule)
        session.expunge(rule)
    return rule


def remove_rule(user_agent: str) -> None:
    """Drop a rule. Refuses `*` (use `reset_rules` instead)."""
    ua = validate_user_agent(user_agent)
    if ua == "*":
        raise RobotsValidationError(
            "cannot remove the '*' stanza; use reset to restore defaults"
        )
    with write_transaction(f"robots:remove:{ua}"), SessionLocal() as session:
        result = session.execute(delete(RobotsRule).where(RobotsRule.user_agent == ua))
        if result.rowcount == 0:
            raise RobotsRuleNotFound(f"no rule for user_agent {ua!r}")
        session.commit()


# Seeded defaults for the `*` stanza, identical to the previous
# hardcoded template (`Crawl-delay: 5`, `Disallow: /*/attachment/`)
# plus the Content-Signal posture from
# https://blog.cloudflare.com/content-signals-policy/: search=yes
# (this IS a publicly-indexed archive, see SEO posture in
# CONTEXT.md); ai-train=no + ai-input=no (matches the "redaction as
# friction layer" posture, and Cloudflare's own recommended default).
# The two migrations that create + extend `robots_rules` seed these
# same values; this constant is the single source of truth for
# `reset_rules`.
_DEFAULT_STAR_CRAWL_DELAY = 5
_DEFAULT_STAR_DISALLOW: tuple[str, ...] = ("/*/attachment/",)
_DEFAULT_STAR_CONTENT_SIGNALS: dict[str, str] = {
    "search": "yes",
    "ai-train": "no",
    "ai-input": "no",
}


def reset_rules() -> None:
    """Drop every row and re-seed the `*` stanza with the defaults."""
    with write_transaction("robots:reset"), SessionLocal() as session:
        session.execute(delete(RobotsRule))
        session.execute(
            sqlite_insert(RobotsRule).values(
                user_agent="*",
                crawl_delay=_DEFAULT_STAR_CRAWL_DELAY,
                disallow_paths=list(_DEFAULT_STAR_DISALLOW),
                content_signals=dict(_DEFAULT_STAR_CONTENT_SIGNALS),
            )
        )
        session.commit()


def _format_content_signals(signals: dict[str, str]) -> str:
    """Render one stanza's Content-Signal value: comma-delimited
    `key=value` pairs in the canonical order (`search`, `ai-input`,
    `ai-train`) so the line is stable across deploys."""
    canonical_order = ("search", "ai-input", "ai-train")
    parts = [f"{k}={signals[k]}" for k in canonical_order if k in signals]
    return ", ".join(parts)


def render_robots_txt(session: Session, sitemap_url: str) -> str:
    """Produce the rendered robots.txt body. Stanzas are emitted in
    `list_rules` order (`*` first, then alphabetical); blank line
    between stanzas; trailing `Sitemap:` line. Inside each stanza:
    `User-agent:`, `Content-Signal:` (when set), `Crawl-delay:`
    (when set), then any `Disallow:` lines."""
    out = StringIO()
    first = True
    for rule in list_rules(session):
        paths = rule.disallow_paths or []
        signals = rule.content_signals or {}
        # Skip degenerate rows (no crawl_delay AND no disallow paths
        # AND no content_signals). A stanza with only a User-agent
        # line and nothing else is nonsense and confuses some bots.
        if rule.crawl_delay is None and not paths and not signals:
            continue
        if not first:
            out.write("\n")
        first = False
        out.write(f"User-agent: {rule.user_agent}\n")
        if signals:
            out.write(f"Content-Signal: {_format_content_signals(signals)}\n")
        if rule.crawl_delay is not None:
            out.write(f"Crawl-delay: {rule.crawl_delay}\n")
        for p in paths:
            out.write(f"Disallow: {p}\n")
    if not first:
        out.write("\n")
    out.write(f"Sitemap: {sitemap_url}\n")
    return out.getvalue()
