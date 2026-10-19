"""Data for `/merge-window` (#672): the patches in linux-next that
Linus's tree does not have yet, grouped by primary subsystem (#703).

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
import logging
import re
from collections import Counter
from datetime import datetime
from email.utils import parseaddr, quote
from itertools import batched
from pathlib import Path
from types import SimpleNamespace

from dulwich.errors import NotGitRepository
from dulwich.mailmap import parse_identity
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import func, literal_column, select, text, tuple_
from sqlalchemy.orm import Session

import mimir
from mimir import cache
from mimir.config import settings
from mimir.datetime_utils import coerce_dt
from mimir.mainline import resolve_tree_path
from mimir.models import Article, ArticleFile, MainlineCommit, MainlineState
from mimir.patch_series import representative_key, version_number_sql
from mimir.patch_state import _resolve_tree_label
from mimir.subsystems import ranked_subsystems
from mimir.web.urls import _canonical_inbox_names_for, _msg_path

logger = logging.getLogger(__name__)

QUEUED_TREE = "linux-next"
LINUS_TREE = "linus"

# The one spelling of the page's path: the route registers it, and the
# page canonical and the meta-sitemap both build from it, so the three
# are byte-identical.
MERGE_WINDOW_PATH = "/merge-window"

# Walk timestamps move the key, so the TTL only bounds how long an
# unused entry lingers.
MERGE_WINDOW_CACHE_TTL_SEC = 24 * 3600

# A subsystem gets its own section from this many queued patches; the
# rest share "Smaller subsystems". Chosen 2026-10-12 from a production
# measurement: ~85 sections holding 60% of patches (#703).
SECTION_MIN_PATCHES = 25
ALSO_TOUCHES = 3
NO_SUBSYSTEM = "(no subsystem)"


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
    # The list the row links into.
    inbox_name: str = ""
    # The other subsystems the posting touches, most files first, after
    # the one its section is for (at most `ALSO_TOUCHES`).
    also: list[str] = []


class MergeWindowSection(BaseModel):
    """One primary subsystem's postings, newest first."""

    name: str
    # Stable fragment id, so a section can be linked to directly.
    anchor: str
    patches: int
    rows: list[MergeWindowRow]


class MergeWindow(BaseModel):
    # Subsystems with at least `SECTION_MIN_PATCHES` queued patches,
    # largest first; then the rest, by name, "(no subsystem)" last.
    sections: list[MergeWindowSection]
    smaller: list[MergeWindowSection] = []
    smaller_patches: int = 0
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
cache.register("MergeWindowSection", MergeWindowSection)
cache.register("MergeWindow", MergeWindow)


def merge_window_state(session: Session) -> str:
    """Everything the page's output depends on, as one string. The
    route's ETag and the cache key both derive from it, so they cannot
    disagree about what makes the page change.

    The walk timestamps stand in for the two row sets and for Linus's
    tags (a new tag arrives with a Linus walk). Config knobs that change
    on a restart without a version bump are listed explicitly (AGENTS.md
    "A cache validator is the same pair, in time").

    Known gap, accepted: a canonical-inbox recompute (#645), or an
    `article_files` reprocess, moves links or grouping without moving
    either timestamp. Linus walks every 25 minutes, which corrects it. The few seconds between a
    rebuild's commit and its `last_walked_at` write can likewise serve
    the new set under the old key."""
    states = {
        row.tree_name: row
        for row in session.execute(
            select(
                MainlineState.tree_name,
                MainlineState.last_walked_at,
                MainlineState.last_commit_sha,
            ).where(MainlineState.tree_name.in_((QUEUED_TREE, LINUS_TREE)))
        ).all()
    }
    walked = {name: row.last_walked_at for name, row in states.items()}
    # The subsystem grouping follows MAINTAINERS; this is its version
    # (see `subsystems._rules_index`).
    rules = states[LINUS_TREE].last_commit_sha if LINUS_TREE in states else None
    labels = ",".join(
        f"{name}={cfg.display_name or ''}"
        for name, cfg in sorted(settings.trees.items())
    )
    return "|".join(
        [
            mimir.__version__,
            str(walked.get(QUEUED_TREE) or ""),
            str(walked.get(LINUS_TREE) or ""),
            rules or "",
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
       a.patch_series_key, a.patch_series_version,
       {version_number_sql("a.patch_series_version")} AS series_rev
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
        SimpleNamespace(**{**r._mapping, "date": coerce_dt(r.date)})
        for r in rows
        if r.id is not None
    ]

    trees_by_msgid: dict[str, set[str]] = {}
    for message_id, tree_name in session.execute(_OTHER_TREES_SQL).all():
        trees_by_msgid.setdefault(message_id, set()).add(tree_name)

    # A posting is one series revision (key plus revision number, so an
    # unversioned `resend` or `rfc` is the same posting as `v1`, #691);
    # a patch without a series key is a posting of its own.
    postings: dict[tuple, list] = {}
    for r in archived:
        posting = (
            (r.patch_series_key, r.series_rev) if r.patch_series_key else (None, r.id)
        )
        postings.setdefault(posting, []).append(r)

    covers = _covers(session, [p for p in postings if p[0] is not None])

    representatives: dict[tuple, tuple] = {}
    for posting, members in postings.items():
        # The article id makes every rank unique, so `min` never
        # compares the rows themselves.
        first_rank, first = min(
            (representative_key(m.patch_series_version, m.date, m.id), m)
            for m in members
        )
        # A cover names the posting unless it is an `rfc` or `resend`
        # one and a queued patch carries a stored `vN`.
        rank, cover = covers.get(posting, ((True,), None))
        representatives[posting] = (
            cover
            if cover and rank[0] <= first_rank[0]
            else (first.id, first.subject, first.author, first.date)
        )
    nameless = {
        posting
        for posting in postings
        if not parseaddr(representatives[posting][2] or "")[0]
    }
    votes = _commit_author_votes(
        session, [m.message_id for p in nameless for m in postings[p]]
    )
    names = _canonical_inbox_names_for(
        session, [rep[0] for rep in representatives.values()]
    )

    files = _files(session, [r.id for r in archived])
    # The posting's files together decide its primary subsystem, so a
    # series appears once, under the most specific subsystem covering at
    # least half as many of its files as the best-covering one
    # (`ranked_subsystems`).
    posting_keys = list(postings)
    ranked_by_posting = dict(
        zip(
            posting_keys,
            ranked_subsystems(
                session,
                [
                    [path for m in postings[k] for path in files.get(m.id, ())]
                    for k in posting_keys
                ],
            ),
            strict=True,
        )
    )
    by_subsystem: dict[str, list[MergeWindowRow]] = {}
    patches_by_subsystem: dict[str, int] = {}
    unlinked = 0
    for posting, members in postings.items():
        rep_id, subject, author, date = representatives[posting]
        if posting in nameless:
            # The commit author's name stands in for a sender with no
            # display name; the address stays, `display_name` hides it.
            named = sum(
                (votes[m.message_id] for m in members if m.message_id in votes),
                Counter(),
            )
            if named:
                # Quoted, not RFC 2047 encoded: `display_name` never
                # decodes encoded words, so "Jürgen" would show as one.
                # Ties go to the alphabetically first name, so a rebuilt
                # walk's row order cannot flip the row.
                name = quote(min(named, key=lambda n: (-named[n], n)))
                author = f'"{name}" <{parseaddr(author or "")[1]}>'
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
        ranked = ranked_by_posting[posting]
        primary = ranked[0] if ranked else NO_SUBSYSTEM
        by_subsystem.setdefault(primary, []).append(
            MergeWindowRow(
                subject=subject,
                url=_msg_path(rep_id, date, inbox_name),
                author=author,
                date=date,
                patches=len(members),
                trees=trees,
                inbox_name=inbox_name,
                also=ranked[1 : 1 + ALSO_TOUCHES],
            )
        )
        patches_by_subsystem[primary] = patches_by_subsystem.get(primary, 0) + len(
            members
        )

    groups = [
        MergeWindowSection(
            name=name,
            anchor=_anchor(name),
            patches=patches_by_subsystem[name],
            rows=sorted(
                rows,
                key=lambda row: (row.date is not None, row.date),
                reverse=True,
            ),
        )
        for name, rows in by_subsystem.items()
    ]
    sections = sorted(
        (
            g
            for g in groups
            if g.patches >= SECTION_MIN_PATCHES and g.name != NO_SUBSYSTEM
        ),
        key=lambda g: (-g.patches, g.name),
    )
    smaller = sorted(
        (g for g in groups if g not in sections),
        key=lambda g: (g.name == NO_SUBSYSTEM, g.name),
    )

    state = session.get(MainlineState, QUEUED_TREE)
    return MergeWindow(
        sections=sections,
        smaller=smaller,
        smaller_patches=sum(g.patches for g in smaller),
        queued=queued,
        not_archived=queued - len(archived) + unlinked,
        **_window(linus_tag(_linus_path())),
        queued_walked_at=state.last_walked_at if state else None,
        walked_commits=state.walked_commits if state else None,
        walked_linked=state.walked_linked if state else None,
    )


def _commit_author_votes(
    session: Session, message_ids: list[str]
) -> dict[str, Counter]:
    """Author-name votes of the linux-next commits per message-id, for
    senders whose mail has none. The mirror is the only place the name
    lives: mainline_commits stores no author and Signed-off-by trailers
    are not kept. Merge commits are skipped, because a merge linked to a
    series cover is the maintainer's, not the author's. A missing mirror
    or commit leaves the id out, so the page keeps the sender it had."""
    tree = settings.trees.get(QUEUED_TREE)
    if tree is None or not message_ids:
        return {}
    shas: list[tuple[str, str]] = []
    for chunk in batched(message_ids, 900):
        shas.extend(
            session.execute(
                select(MainlineCommit.message_id, MainlineCommit.commit_sha).where(
                    MainlineCommit.tree_name == QUEUED_TREE,
                    MainlineCommit.message_id.in_(chunk),
                )
            ).all()
        )
    votes: dict[str, Counter] = {}
    # Broad on purpose: the name is decoration, and a mirror that is
    # unreadable or mid-gc (OSError, corrupt object, missing repo) must
    # never take the page down.
    try:
        with Repo(str(resolve_tree_path(tree))) as repo:
            for message_id, sha in shas:
                try:
                    commit = repo[sha.encode()]
                except KeyError:
                    continue
                if len(commit.parents) > 1:
                    continue
                # `parse_identity` is not parseaddr: a git ident may
                # carry a comma ("Doe, Jane"), which parseaddr rejects.
                # An ident with an empty name (`<addr>`) has no name to give.
                name = (parse_identity(commit.author)[0] or b"").decode(
                    errors="replace"
                )
                if name and "@" not in name:
                    votes.setdefault(message_id, Counter())[name] += 1
    except Exception as exc:
        logger.warning("merge-window: cannot read author names: %r", exc)
        return {}
    return votes


def _files(session: Session, article_ids: list[int]) -> dict[int, list[str]]:
    """Touched files per article, for the subsystem grouping."""
    out: dict[int, list[str]] = {}
    for i in range(0, len(article_ids), 900):
        for aid, path in session.execute(
            select(ArticleFile.article_id, ArticleFile.path).where(
                ArticleFile.article_id.in_(article_ids[i : i + 900])
            )
        ).all():
            out.setdefault(aid, []).append(path)
    return out


def _anchor(name: str) -> str:
    """A section's fragment id: the subsystem name, lowercased, with
    every run of other characters as one hyphen (`KERNEL VIRTUAL MACHINE
    FOR X86 (KVM/x86)` gives `kernel-virtual-machine-for-x86-kvm-x86`)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "section"


def _covers(
    session: Session, postings: list[tuple]
) -> dict[tuple, tuple[tuple, tuple]]:
    """Cover letter (position 0) of each posting that has one archived,
    as `(rank, (id, subject, author, date))`, keyed by series key and
    revision number, where rank is its `representative_key`. The cover
    is rarely itself in a tree, so it is looked up rather than found
    among the queued rows. Where a revision has more than one cover
    (`rfc` and `v1`, or a repost), `representative_key` picks, so an
    RFC archived first does not name the posting."""
    out: dict[tuple, tuple[tuple, tuple]] = {}
    rev = literal_column(version_number_sql("articles.patch_series_version"))
    posting_key = tuple_(Article.patch_series_key, rev)
    for i in range(0, len(postings), 400):
        for a in session.execute(
            select(
                Article.id,
                Article.subject,
                Article.author,
                Article.date,
                Article.patch_series_key,
                Article.patch_series_version,
                rev.label("series_rev"),
            ).where(
                posting_key.in_(postings[i : i + 400]),
                func.coalesce(Article.patch_series_position, 0) == 0,
            )
        ).all():
            key = (a.patch_series_key, a.series_rev)
            rank = representative_key(a.patch_series_version, a.date, a.id)
            if key not in out or rank < out[key][0]:
                out[key] = (rank, (a.id, a.subject, a.author, a.date))
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
    """The highest version tag in Linus's mirror, which is where that
    tree is: Linus tags HEAD. None when the mirror is missing or has no
    version tags."""
    tags = version_tags(path)
    return tags[-1] if tags else None


def version_tags(path: Path | None) -> list[str]:
    """Release and release-candidate tags in Linus's mirror, lowest
    version first. Read from the refs, so it costs nothing; `git
    describe` would walk history, and dulwich's walk is pure Python
    over a 1.5M-commit tree. Empty when the mirror is missing."""
    if path is None:
        return []
    try:
        with Repo(str(path)) as repo:
            names = repo.refs.as_dict(b"refs/tags").keys()
    except NotGitRepository, OSError:
        return []
    ranked = []
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
        ranked.append((rank, name))
    return [name for _, name in sorted(ranked)]
