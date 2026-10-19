"""Pull requests to Linus on `/merge-window` (#703): the `[GIT PULL]`
messages addressed to Linus since the anchor tag (`anchor_tag`: the
latest tag, or the one before it while the merge window is open),
merged or pending.

Git decides "merged": a pull request is merged once its tip (the body's
"up to <sha>") is in `git rev-list <anchor>..HEAD` on the Linus mirror.
One subprocess call; measured 2026-10-18 on production at 0.19 s for
20,259 commits, where a dulwich walk took 5.0 s for 288. A pr-tracker-bot
reply saying it was merged also counts, for good: the bot replies once
the merge is in Linus's tree, so it cannot invent one, and it covers a
pull whose named tip never appears there (a re-pushed tag, or a
different tip pulled).

Bodies and headers come from git through `read_message`, a few dozen
per cycle (106 candidates read in 0.5 s, same measurement), cached
under `linus_pulls_state` so new mail never recomputes the linux-next
queue.
"""

import hashlib
import logging
import re
import subprocess
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

import mimir
from mimir import cache
from mimir.config import settings
from mimir.merge_window import LINUS_TREE, _linus_path, _window, version_tags
from mimir.models import Article, MainlineState
from mimir.store import read_first_readable
from mimir.web.urls import _canonical_inbox_names_for, _msg_path

logger = logging.getLogger(__name__)

LINUS_ADDRESS = "torvalds@linux-foundation.org"
PR_TRACKER_BOT = "pr-tracker-bot@kernel.org"
_TIP_RE = re.compile(r"\bup to ([0-9a-f]{40})\b")
_BOT_MERGED = "has been merged into torvalds/linux.git"
# As `dashboard.latest_pull_requests` matches them: originals only.
_PULL_SUBJECT = "[GIT PULL]%"

# A stalled git (a mirror mid-repack, a hung NFS read) must not hold a
# web worker: past this the section shows pending. Generous, against
# 0.19 s measured for a whole merge window's range.
GIT_REV_LIST_TIMEOUT_SEC = 30

# The state moves the key; the TTL only bounds an unused entry.
LINUS_PULLS_CACHE_TTL_SEC = 24 * 3600


class LinusPull(BaseModel):
    subject: str | None
    url: str
    author: str | None
    date: datetime | None
    merged: bool


class LinusPulls(BaseModel):
    # The tag the pull requests count from (`anchor_tag`).
    anchor: str | None
    # Newest first.
    rows: list[LinusPull]


cache.register("LinusPull", LinusPull)
cache.register("LinusPulls", LinusPulls)


def linus_pulls_state(session: Session) -> str:
    """Everything the section depends on, as one string; the route's
    ETag and the cache key both derive from it.

    Every successful Linus walk writes its timestamp, and the mirror's
    HEAD and tags only move in the fetch of that same tick, so it stands
    in for the anchor tag and the merged set. The anchor is deliberately not read here: listing the tags
    costs about 20 ms (2,214 packed tags, synthetic repo, 2026-10-18) on
    every request including 304s, and the queue's `window_open` follows
    the walk timestamp too, so both caches flip on the same walk. Known
    gap, as in `merge_window_state`: a fetch whose walk then fails moves
    the tags without the timestamp, until the next successful walk. The
    newest matching article id covers new mail: any newly ingested pull
    request or bot reply gets a higher id."""
    # `ORDER BY id DESC LIMIT 1`, not `max(id) ... WHERE`: SQLite stops
    # this one at the first match while scanning down the rowid (query
    # plan `SCAN articles`, no temp B-tree), so it reads only the
    # articles newer than the last pull request. It is not limited to
    # "since the anchor": a date bound would read every article since
    # the tag through the date index, on every request including 304s.
    newest = session.execute(
        select(Article.id)
        .where(
            or_(
                Article.subject.ilike(_PULL_SUBJECT),
                Article.author.ilike(f"%{PR_TRACKER_BOT}%"),
            )
        )
        .order_by(Article.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return "|".join(
        [
            mimir.__version__,
            str(_linus_walked_at(session) or ""),
            str(newest or ""),
            str(_linus_path()),
            # Decides which inbox each pull links into.
            ",".join(sorted(settings.canonical_demoted_inboxes)),
        ]
    )


def linus_pulls(session: Session, state: str | None = None) -> LinusPulls:
    """The section's data, cached under `linus_pulls_state`."""
    state = state if state is not None else linus_pulls_state(session)
    key = hashlib.blake2s(state.encode(), digest_size=8).hexdigest()
    return cache.get_or_compute(
        session,
        f"linus_pulls:{key}",
        LINUS_PULLS_CACHE_TTL_SEC,
        lambda: _compute(session),
    )


def anchor_tag(path: Path | None) -> str | None:
    """The tag the section counts from: candidates are mails since its
    commit, merged tips are commits after it. The latest tag, except
    while the merge window is open (the latest tag is a release): then
    the tag before it, normally the last rc. Merge-window pull requests
    are sent in the last rc week, before the release exists, and an
    rc-week fixes pull merged before the release must still show
    merged, so the mails and `rev-list` share this one anchor. A mirror
    with no earlier version tag falls back to the latest."""
    tags = version_tags(path)
    if not tags:
        return None
    if _window(tags[-1])["window_open"] and len(tags) > 1:
        return tags[-2]
    return tags[-1]


def _compute(session: Session) -> LinusPulls:
    path = _linus_path()
    anchor = anchor_tag(path)
    since = _tag_time(path, anchor) if anchor else None
    if since is None:
        return LinusPulls(anchor=anchor, rows=[])

    pulls = []
    for art in session.execute(
        select(Article)
        .options(selectinload(Article.lists))
        .where(Article.subject.ilike(_PULL_SUBJECT), Article.date >= since)
        .order_by(Article.date.desc(), Article.id.desc())
    ).scalars():
        parsed = _body(session, art)
        if parsed is None or LINUS_ADDRESS not in _recipients(parsed.headers):
            continue
        tip = _TIP_RE.search(parsed.body or "")
        pulls.append((art, tip.group(1) if tip else None))

    bot_said = _bot_merged(session, [art.message_id for art, _ in pulls])
    merged_tips = _merged_tips(path, anchor)
    names = _canonical_inbox_names_for(session, [art.id for art, _ in pulls])

    rows = []
    for art, tip in pulls:
        rows.append(
            LinusPull(
                subject=art.subject,
                url=_msg_path(art.id, art.date, names[art.id]),
                author=art.author,
                date=art.date,
                merged=tip in merged_tips or art.message_id in bot_said,
            )
        )
    return LinusPulls(anchor=anchor, rows=rows)


def _linus_walked_at(session: Session) -> datetime | None:
    return session.execute(
        select(MainlineState.last_walked_at).where(
            MainlineState.tree_name == LINUS_TREE
        )
    ).scalar_one_or_none()


def _body(session: Session, art: Article):
    found = read_first_readable(session, [art], art.canonical_inbox_id)
    return found[1] if found else None


def _recipients(headers: dict[str, str]) -> set[str]:
    by_lower = {k.lower(): v for k, v in headers.items()}
    values = [by_lower[k] for k in ("to", "cc") if by_lower.get(k)]
    return {addr.lower() for _, addr in getaddresses(values)}


def _bot_merged(session: Session, message_ids: list[str]) -> set[str]:
    """The pull requests pr-tracker-bot replied were merged into Linus's
    tree, by message-id."""
    out: set[str] = set()
    if not message_ids:
        return out
    for reply in session.execute(
        select(Article)
        .options(selectinload(Article.lists))
        .where(Article.thread_parent.in_(message_ids))
    ).scalars():
        if parseaddr(reply.author or "")[1].lower() != PR_TRACKER_BOT:
            continue
        parsed = _body(session, reply)
        if parsed is not None and _BOT_MERGED in (parsed.body or ""):
            out.add(reply.thread_parent)
    return out


def _tag_time(path: Path | None, tag: str) -> datetime | None:
    """Commit time of the commit `tag` points at, read through the refs
    (an annotated tag peels to it); no history walk."""
    try:
        with Repo(str(path)) as repo:
            commit = repo[repo.get_peeled(b"refs/tags/" + tag.encode())]
    except NotGitRepository, OSError, KeyError:
        return None
    return datetime.fromtimestamp(commit.commit_time, UTC)


def _merged_tips(path: Path | None, tag: str) -> set[str]:
    """Every commit in Linus's tree after `tag`. Empty, logged, when
    git cannot answer, so the section shows pending rather than
    failing the page."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-list", f"refs/tags/{tag}..HEAD"],
            capture_output=True,
            timeout=GIT_REV_LIST_TIMEOUT_SEC,
            text=True,
            check=True,
        ).stdout
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        logger.warning("linus_pulls: git rev-list failed in %s: %r", path, exc)
        return set()
    return set(out.split())
