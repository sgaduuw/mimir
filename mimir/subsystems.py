"""Subsystem resolution: map article-touched paths to the MAINTAINERS
sections that claim them.

Two read-path helpers:

- `subsystems_for_article(session, article_id)`, given an article,
  return the deduplicated set of `Subsystem` rows that any of its
  `article_files` paths land in. The header on the patch page reads
  this.
- `recent_patches_touching(session, paths, exclude_id, limit)`  
  given a set of paths, return the most-recent articles (other than
  the current one) that share at least one path. The sidebar on the
  patch page reads this.

Glob semantics intentionally simple, enough for the common
MAINTAINERS shapes:

- `dir/` matches every path under that directory (and the literal
  bare-directory path)
- `path/file.c` matches that exact path
- `path/*.c` and friends fall through to `fnmatch.fnmatchcase` for
  the wildcard cases

`X:` (exclude) entries veto a `F:` (include) match within the same
subsystem: if any exclude glob matches the path, that subsystem is
**not** returned for the path. Cross-subsystem exclusions don't
exist in MAINTAINERS, `X:` only acts on its own section.
"""
import fnmatch
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.canonical import fallback_canonical_name
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    Inbox,
    Subsystem,
    SubsystemPath,
)

# Per-subsystem dashboard helpers refresh at most once per hour. The
# underlying joins (article_files × article_lists × articles for
# `recent_articles_in_subsystem`, recursive CTE for
# `active_threads_in_subsystem`, trailer scan for
# `active_reviewers_in_subsystem`) all cost seconds on hot
# subsystems; a per-subsystem surface doesn't need the 5-minute
# real-time feel that drives the front-page TTL. warm-cache pre-
# builds the top-N most active subsystems per inbox at this TTL so
# steady-state visitors hit warm cache.
SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC = 3600  # 1 hour


def path_matches_glob(path: str, glob: str) -> bool:
    """MAINTAINERS-flavoured glob match.

    Trailing-slash entries are directory prefixes (the common shape).
    Wildcard entries (`*`, `?`, `[…]`) fall through to fnmatch.
    Everything else is an exact-string match.

    We deliberately skip `**` semantics: real MAINTAINERS doesn't
    use Bash-style globstars, and fnmatch happens to expand `*` to
    cross slashes anyway, so `fs/*/file.c` and `fs/*` already do
    the right thing for the patterns the upstream file actually
    uses.
    """
    if glob.endswith("/"):
        return path.startswith(glob) or path == glob[:-1]
    if "*" in glob or "?" in glob or "[" in glob:
        return fnmatch.fnmatchcase(path, glob)
    return path == glob


class SubsystemHit(BaseModel):
    """Compact summary of one subsystem match. Just the fields the
    patch-page header needs, avoids carrying the entire SQLA row
    across the cache boundary if we ever wrap this in
    `mimir.cache`.
    """
    model_config = {"arbitrary_types_allowed": False}

    id: int
    name: str
    status: str | None = None
    # `[(role, name, address), ...]`. role is "M" or "R".
    maintainers: list[tuple[str, str, str]] = []
    lists: list[str] = []  # not stored yet; reserved for future use


def subsystems_for_article(
    session: Session, article_id: int,
) -> list[SubsystemHit]:
    """Resolve the subsystem(s) for an article via its touched files.

    Walks every (path, subsystem_path) pair: matches by glob, drops
    subsystems whose `X:` entries also match the path. Returns one
    `SubsystemHit` per unique matched subsystem, sorted by name for
    a stable header order.

    O(paths × globs) Python loop. For ~10 paths × ~10k globs that's
    100k comparisons, fast in practice. If a future deploy with
    much-larger MAINTAINERS makes this hot, the next move is a
    directory-prefix trie over the `F:` rules.
    """
    paths = [
        row.path for row in session.execute(
            select(ArticleFile).where(ArticleFile.article_id == article_id)
        ).scalars()
    ]
    if not paths:
        return []

    # Pull every subsystem_path with the parent Subsystem joined in
    # one query. selectinload avoids the N+1 trap on
    # `subsystem.maintainers` for whichever subsystems end up
    # matching.
    all_rules = list(session.execute(
        select(SubsystemPath, Subsystem)
        .join(Subsystem, Subsystem.id == SubsystemPath.subsystem_id)
        .options(selectinload(Subsystem.maintainers))
    ).all())

    # Group rules by subsystem so we can evaluate include AND exclude
    # together per subsystem.
    includes_by_sub: dict[int, list[SubsystemPath]] = defaultdict(list)
    excludes_by_sub: dict[int, list[SubsystemPath]] = defaultdict(list)
    subs_by_id: dict[int, Subsystem] = {}
    for rule, sub in all_rules:
        subs_by_id[sub.id] = sub
        if rule.is_exclude:
            excludes_by_sub[sub.id].append(rule)
        else:
            includes_by_sub[sub.id].append(rule)

    matched_ids: set[int] = set()
    for sub_id, includes in includes_by_sub.items():
        excludes = excludes_by_sub.get(sub_id, [])
        for path in paths:
            if any(
                path_matches_glob(path, rule.glob) for rule in includes
            ) and not any(
                path_matches_glob(path, rule.glob) for rule in excludes
            ):
                matched_ids.add(sub_id)
                break  # one matching path is enough for this subsystem

    hits = []
    for sub_id in matched_ids:
        sub = subs_by_id[sub_id]
        hits.append(SubsystemHit(
            id=sub.id,
            name=sub.name,
            status=sub.status,
            maintainers=[
                (m.role, m.name, m.address) for m in sub.maintainers
            ],
        ))
    hits.sort(key=lambda h: h.name)
    return hits


class RelatedPatch(BaseModel):
    """One entry in the "other patches touching <path>" sidebar.
    Carries just what the template needs."""
    article_id: int
    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    inbox_name: str  # canonical inbox name for URL building


cache.register("RelatedPatch", RelatedPatch)


def recent_patches_touching(
    session: Session, paths: list[str],
    exclude_article_id: int | None = None,
    limit: int = 5,
) -> list[RelatedPatch]:
    """Top-N most-recent articles (other than `exclude_article_id`)
    whose `article_files` row matches at least one of `paths`.

    `exclude_article_id` is `None` when there's no current-article
    context (e.g. the per-subsystem dashboard's "recent patches"
    surface, which has no anchor article to exclude).

    Returns the canonical-inbox name for each so the template can
    build a URL without re-querying. Articles with no canonical
    inbox fall back to the alphabetically-first linked inbox (same
    rule as `_canonical_inbox_name`).
    """
    if not paths:
        return []
    # DISTINCT on article_id because an article touching multiple
    # of `paths` would otherwise return once per match. Order by
    # date DESC keeps the surface to "what's been active here
    # lately".
    q = (
        select(Article.id, Article.message_id, Article.subject,
               Article.author, Article.date,
               Article.canonical_inbox_id)
        .join(ArticleFile, ArticleFile.article_id == Article.id)
        .where(ArticleFile.path.in_(paths))
        .group_by(Article.id)
        .order_by(Article.date.desc())
        .limit(limit)
    )
    if exclude_article_id is not None:
        q = q.where(Article.id != exclude_article_id)
    rows = session.execute(q).all()
    if not rows:
        return []

    # Resolve inbox names in one bulk query, avoid N+1.
    article_ids = [r[0] for r in rows]
    links = session.execute(
        select(ArticleList.article_id, Inbox.id, Inbox.name)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_(article_ids))
    ).all()
    links_by_article: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for art_id, ix_id, ix_name in links:
        links_by_article[art_id].append((ix_id, ix_name))

    out: list[RelatedPatch] = []
    for art_id, mid, subj, author, date, canon_id in rows:
        link_set = links_by_article.get(art_id, [])
        canon_name = fallback_canonical_name(canon_id, link_set)
        if canon_name is None:
            continue  # shouldn't happen given FK cascades
        out.append(RelatedPatch(
            article_id=art_id, message_id=mid, subject=subj,
            author=author, date=date, inbox_name=canon_name,
        ))
    return out



__all__ = [
    "RelatedPatch",
    "SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC",
    "SubsystemHit",
    "path_matches_glob",
    "recent_patches_touching",
    "subsystems_for_article",
]
