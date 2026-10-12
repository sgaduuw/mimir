"""Data for `/merge-window` (#672): the patches in linux-next that
Linus's tree does not have yet, grouped by mailing list.

"Queued" is linux-next minus Linus, on message-id rather than commit
sha: quilt-managed parts of linux-next get new shas on every rebuild,
and a patch Linus applied under another sha is still landed. Only
commits with a recognised `Link:` trailer have rows, so the page lists
linked commits only, and says so. Queued messages mimir does not
archive are counted, not listed.

No new tables and no precompute: one anti-join over `mainline_commits`,
cached under a key built from the two trees' walk timestamps, so a
finished walk changes the key and nothing has to delete a cache row.
The linux-next rebuild is atomic (#671), so a page computed during a
walk shows the previous set, never a partial one.
"""

import hashlib
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import Session

import mimir
from mimir import cache
from mimir.config import settings
from mimir.mainline import resolve_tree_path
from mimir.models import Article, MainlineState
from mimir.patch_state import _resolve_tree_label
from mimir.threading import _coerce_dt
from mimir.web.urls import _canonical_inbox_names_for, _msg_path

QUEUED_TREE = "linux-next"
LINUS_TREE = "linus"

# The one spelling of the page's path: the route registers it, and the
# page canonical and the meta-sitemap both build from it, so the three
# are byte-identical.
MERGE_WINDOW_PATH = "/merge-window"

# Walk timestamps move the key, so the TTL only bounds how long an
# unused entry lingers.
MERGE_WINDOW_CACHE_TTL_SEC = 24 * 3600


class MergeWindowRow(BaseModel):
    """One posting: a series (cover letter, or its oldest queued patch
    when the cover is not archived) or a single patch."""

    subject: str | None
    url: str
    author: str | None
    date: datetime | None
    patches: int
    # Other trees carrying any of the posting's patches (net-next,
    # tip, ...), as display labels. linux-next is implied.
    trees: list[str]


class MergeWindowList(BaseModel):
    inbox_name: str
    patches: int
    rows: list[MergeWindowRow]


class MergeWindow(BaseModel):
    lists: list[MergeWindowList]
    # Queued message-ids, archived or not.
    queued: int
    # Queued message-ids with no article mimir can link to.
    not_archived: int
    linus_tag: str | None
    # The release `linus_tag` belongs to (`v7.4` for `v7.4-rc3`), and
    # whether the tag IS that release: right after a release the next
    # merge window is open, from rc1 on it is closed until the release.
    linus_release: str | None = None
    window_open: bool = False
    queued_walked_at: datetime | None
    # The linux-next rebuild's non-merge commits beyond Linus, and how
    # many link a message: the share the page can show (#703). None
    # until the first rebuild that records them.
    walked_commits: int | None = None
    walked_linked: int | None = None


cache.register("MergeWindowRow", MergeWindowRow)
cache.register("MergeWindowList", MergeWindowList)
cache.register("MergeWindow", MergeWindow)


def merge_window_state(session: Session) -> str:
    """Everything the page's output depends on, as one string. The
    route's ETag and the cache key both derive from it, so they cannot
    disagree about what makes the page change.

    The walk timestamps stand in for the two row sets and for Linus's
    tags (a new tag arrives with a Linus walk). Config knobs that change
    on a restart without a version bump are listed explicitly (AGENTS.md
    "A cache validator is the same pair, in time").

    Known gap, accepted: a canonical-inbox recompute (#645) moves
    grouping and links without moving either timestamp. Linus walks
    every 25 minutes, which corrects it. The few seconds between a
    rebuild's commit and its `last_walked_at` write can likewise serve
    the new set under the old key."""
    walked = dict(
        session.execute(
            select(MainlineState.tree_name, MainlineState.last_walked_at).where(
                MainlineState.tree_name.in_((QUEUED_TREE, LINUS_TREE))
            )
        ).all()
    )
    labels = ",".join(
        f"{name}={cfg.display_name or ''}"
        for name, cfg in sorted(settings.trees.items())
    )
    return "|".join(
        [
            mimir.__version__,
            str(walked.get(QUEUED_TREE) or ""),
            str(walked.get(LINUS_TREE) or ""),
            ",".join(sorted(settings.canonical_demoted_inboxes)),
            labels,
            str(_linus_path()),
        ]
    )


def merge_window(session: Session, state: str | None = None) -> MergeWindow:
    """The page's data, cached under `merge_window_state`."""
    state = state if state is not None else merge_window_state(session)
    key = hashlib.blake2s(state.encode(), digest_size=8).hexdigest()
    return cache.get_or_compute(
        session,
        f"merge_window:{key}",
        MERGE_WINDOW_CACHE_TTL_SEC,
        lambda: _compute(session),
    )


_QUEUED_SQL = text(f"""
SELECT q.message_id, a.id, a.subject, a.author, a.date,
       a.patch_series_key, a.patch_series_version
  FROM (SELECT DISTINCT n.message_id FROM mainline_commits n
         WHERE n.tree_name = '{QUEUED_TREE}'
           AND NOT EXISTS (SELECT 1 FROM mainline_commits l
                            WHERE l.tree_name = '{LINUS_TREE}'
                              AND l.message_id = n.message_id)) q
  LEFT JOIN articles a ON a.message_id = q.message_id
""")

_OTHER_TREES_SQL = text(f"""
SELECT DISTINCT o.message_id, o.tree_name
  FROM mainline_commits o
  JOIN mainline_commits n
    ON n.message_id = o.message_id AND n.tree_name = '{QUEUED_TREE}'
 WHERE o.tree_name NOT IN ('{QUEUED_TREE}', '{LINUS_TREE}')
   AND NOT EXISTS (SELECT 1 FROM mainline_commits l
                    WHERE l.tree_name = '{LINUS_TREE}'
                      AND l.message_id = o.message_id)
""")


def _compute(session: Session) -> MergeWindow:
    rows = session.execute(_QUEUED_SQL).all()
    queued = len(rows)
    # Raw SQL returns SQLite's stored text for dates; coerce as the
    # triage queries do.
    archived = [
        SimpleNamespace(**{**r._mapping, "date": _coerce_dt(r.date)})
        for r in rows
        if r.id is not None
    ]

    trees_by_msgid: dict[str, set[str]] = {}
    for message_id, tree_name in session.execute(_OTHER_TREES_SQL).all():
        trees_by_msgid.setdefault(message_id, set()).add(tree_name)

    # A posting is one series revision (key plus version); a patch
    # without a series key is a posting of its own.
    postings: dict[tuple, list] = {}
    for r in archived:
        posting = (
            (r.patch_series_key, r.patch_series_version)
            if r.patch_series_key
            else (None, r.id)
        )
        postings.setdefault(posting, []).append(r)

    covers = _covers(session, [p for p in postings if p[0] is not None])

    representatives: dict[tuple, tuple] = {}
    for posting, members in postings.items():
        cover = covers.get(posting)
        oldest = min(members, key=lambda m: (m.date is None, m.date, m.id))
        representatives[posting] = cover or (
            oldest.id,
            oldest.subject,
            oldest.author,
            oldest.date,
        )
    names = _canonical_inbox_names_for(
        session, [rep[0] for rep in representatives.values()]
    )

    by_inbox: dict[str, list[MergeWindowRow]] = {}
    patches_by_inbox: dict[str, int] = {}
    unlinked = 0
    for posting, members in postings.items():
        rep_id, subject, author, date = representatives[posting]
        inbox_name = names.get(rep_id)
        if inbox_name is None:
            # Archived but linked to no inbox: nothing to link to.
            unlinked += len(members)
            continue
        trees = sorted(
            {
                _resolve_tree_label(t)
                for m in members
                for t in trees_by_msgid.get(m.message_id, ())
            }
        )
        by_inbox.setdefault(inbox_name, []).append(
            MergeWindowRow(
                subject=subject,
                url=_msg_path(rep_id, date, inbox_name),
                author=author,
                date=date,
                patches=len(members),
                trees=trees,
            )
        )
        patches_by_inbox[inbox_name] = patches_by_inbox.get(inbox_name, 0) + len(
            members
        )

    lists = [
        MergeWindowList(
            inbox_name=name,
            patches=patches_by_inbox[name],
            rows=sorted(
                rows,
                key=lambda row: (row.date is not None, row.date),
                reverse=True,
            ),
        )
        for name, rows in by_inbox.items()
    ]
    lists.sort(key=lambda lst: (-lst.patches, lst.inbox_name))

    state = session.get(MainlineState, QUEUED_TREE)
    return MergeWindow(
        lists=lists,
        queued=queued,
        not_archived=queued - len(archived) + unlinked,
        **_window(linus_tag(_linus_path())),
        queued_walked_at=state.last_walked_at if state else None,
        walked_commits=state.walked_commits if state else None,
        walked_linked=state.walked_linked if state else None,
    )


def _covers(session: Session, postings: list[tuple]) -> dict[tuple, tuple]:
    """Cover letter (position 0) of each posting that has one archived,
    as `(id, subject, author, date)`. The cover is rarely itself in a
    tree, so it is looked up rather than found among the queued rows."""
    out: dict[tuple, tuple] = {}
    posting_key = tuple_(Article.patch_series_key, Article.patch_series_version)
    for i in range(0, len(postings), 400):
        for a in session.execute(
            select(
                Article.id,
                Article.subject,
                Article.author,
                Article.date,
                Article.patch_series_key,
                Article.patch_series_version,
            )
            .where(
                posting_key.in_(postings[i : i + 400]),
                func.coalesce(Article.patch_series_position, 0) == 0,
            )
            .order_by(Article.id)
        ).all():
            out.setdefault(
                (a.patch_series_key, a.patch_series_version),
                (a.id, a.subject, a.author, a.date),
            )
    return out


def _linus_path() -> Path | None:
    tree = settings.trees.get(LINUS_TREE)
    return resolve_tree_path(tree) if tree is not None else None


# Release and release-candidate tags only (`v7.3`, `v7.3-rc4`,
# `v2.6.39`); anything else in Linus's tree is not a version.
_VERSION_TAG_RE = re.compile(r"^v(\d+(?:\.\d+)+)(?:-rc(\d+))?$")


def _window(tag: str | None) -> dict:
    """`linus_tag`, `linus_release` and `window_open` for a tag. The
    page never names the next version: Linus picks major bumps by
    hand (6.19 to 7.0)."""
    m = _VERSION_TAG_RE.match(tag or "")
    if m is None:
        return {"linus_tag": tag, "linus_release": None, "window_open": False}
    return {
        "linus_tag": tag,
        "linus_release": f"v{m.group(1)}",
        "window_open": m.group(2) is None,
    }


def linus_tag(path: Path | None) -> str | None:
    """The highest version tag in Linus's mirror, which is where his
    tree is: he tags his own HEAD. Read from the refs, so it costs
    nothing; `git describe` would walk history, and dulwich's walk is
    pure Python over a 1.5M-commit tree. None when the mirror is
    missing or has no version tags."""
    if path is None:
        return None
    try:
        with Repo(str(path)) as repo:
            names = repo.refs.as_dict(b"refs/tags").keys()
    except NotGitRepository, OSError:
        return None
    best: tuple | None = None
    best_name = None
    for raw in names:
        name = raw.decode("utf-8", errors="replace")
        m = _VERSION_TAG_RE.match(name)
        if m is None:
            continue
        # A release sorts after every one of its release candidates.
        rank = (
            tuple(int(p) for p in m.group(1).split(".")),
            int(m.group(2)) if m.group(2) else float("inf"),
        )
        if best is None or rank > best:
            best, best_name = rank, name
    return best_name
