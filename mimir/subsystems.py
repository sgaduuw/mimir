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
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.canonical import fallback_canonical_name
from mimir.config import settings
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


# Snapshot cache for the full subsystems rule set. The pull is
# expensive on a kernel-scale tree (~10k SubsystemPath rows + ~5k
# cascaded SubsystemMaintainer rows) and the result only changes
# when `mimir update-mainline` reloads MAINTAINERS, which is rare.
# 24 h TTL is a backstop, the cache is invalidated explicitly from
# `mimir.mainline.load_maintainers` after every reload. Same shape
# as `mimir.maintainer_allowlist`'s cached frozenset.
_RULES_CACHE_KEY = "subsystem_rules_snapshot"
_RULES_CACHE_TTL_SEC = 24 * 3600


class _SubsystemRule(BaseModel):
    """One row from `subsystem_paths`, in cache-safe shape."""

    subsystem_id: int
    glob: str
    is_exclude: bool


class _SubsystemSnap(BaseModel):
    """One `Subsystem` + its cascaded maintainers, in cache-safe shape."""

    id: int
    name: str
    status: str | None = None
    maintainers: list[tuple[str, str, str]] = []


class _RulesSnapshot(BaseModel):
    """The full rule set as cached. Both fields rebuild together so
    a partial cache (rules without subsystem metadata or vice versa)
    can't happen."""

    rules: list[_SubsystemRule] = []
    subsystems: dict[int, _SubsystemSnap] = {}


cache.register("_SubsystemRule", _SubsystemRule)
cache.register("_SubsystemSnap", _SubsystemSnap)
cache.register("_RulesSnapshot", _RulesSnapshot)


def _compute_rules_snapshot() -> _RulesSnapshot:
    """Pull every SubsystemPath + Subsystem + cascaded maintainers
    into a flat Python snapshot. Two queries (one for subsystems
    with `selectinload` on maintainers, one for the rules).
    Cached by `_rules_snapshot()` below."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as session:
        subs = (
            session.execute(
                select(Subsystem).options(selectinload(Subsystem.maintainers))
            )
            .scalars()
            .all()
        )
        sub_map = {
            s.id: _SubsystemSnap(
                id=s.id,
                name=s.name,
                status=s.status,
                maintainers=[(m.role, m.name, m.address) for m in s.maintainers],
            )
            for s in subs
        }
        rule_rows = session.execute(
            select(
                SubsystemPath.subsystem_id,
                SubsystemPath.glob,
                SubsystemPath.is_exclude,
            )
        ).all()
    rules = [
        _SubsystemRule(
            subsystem_id=sub_id,
            glob=glob,
            is_exclude=is_exclude,
        )
        for sub_id, glob, is_exclude in rule_rows
    ]
    return _RulesSnapshot(rules=rules, subsystems=sub_map)


def _rules_snapshot(session: Session) -> _RulesSnapshot:
    """Return the cached subsystems rule snapshot. `session` is the
    caller's session so the `get_or_compute` cache lookup runs in
    the same transaction as the surrounding work."""
    return cache.get_or_compute(
        session,
        _RULES_CACHE_KEY,
        _RULES_CACHE_TTL_SEC,
        _compute_rules_snapshot,
    )


def invalidate_rules_snapshot() -> None:
    """Drop the cached rule set. Called from `update-mainline`'s
    `load_maintainers` after a reload commits, so the next reader
    sees the fresh state instead of waiting for the 24 h TTL."""
    cache.delete(_RULES_CACHE_KEY)


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


def subsystems_for_article(
    session: Session,
    article_id: int,
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

    The rule set itself (every SubsystemPath + cascaded maintainers,
    ~15k rows on the kernel tree) is fetched once from the
    cross-process cache, not per call, the data only changes when
    `update-mainline` reloads MAINTAINERS. The per-article query
    against `article_files` still hits the DB.
    """
    paths = [
        row.path
        for row in session.execute(
            select(ArticleFile).where(ArticleFile.article_id == article_id)
        ).scalars()
    ]
    if not paths:
        return []

    snap = _rules_snapshot(session)
    # Group cached rules by subsystem so we can evaluate include AND
    # exclude together per subsystem.
    includes_by_sub: dict[int, list[str]] = defaultdict(list)
    excludes_by_sub: dict[int, list[str]] = defaultdict(list)
    for rule in snap.rules:
        if rule.is_exclude:
            excludes_by_sub[rule.subsystem_id].append(rule.glob)
        else:
            includes_by_sub[rule.subsystem_id].append(rule.glob)

    matched_ids: set[int] = set()
    for sub_id, includes in includes_by_sub.items():
        excludes = excludes_by_sub.get(sub_id, [])
        for path in paths:
            if any(path_matches_glob(path, glob) for glob in includes) and not any(
                path_matches_glob(path, glob) for glob in excludes
            ):
                matched_ids.add(sub_id)
                break  # one matching path is enough for this subsystem

    hits = []
    for sub_id in matched_ids:
        snap_sub = snap.subsystems.get(sub_id)
        if snap_sub is None:
            # Rule references a subsystem that's no longer in the
            # snapshot. Can happen briefly if the cached rules
            # outlive the parent rows (e.g. cache invalidation
            # ordering during a race). Skip rather than crash; the
            # next snapshot refresh will reconcile.
            continue
        hits.append(
            SubsystemHit(
                id=snap_sub.id,
                name=snap_sub.name,
                status=snap_sub.status,
                maintainers=list(snap_sub.maintainers),
            )
        )
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
    session: Session,
    paths: list[str],
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

    Query shape: walks `ix_articles_date` DESC over a bounded recent
    window (`settings.recent_patches_max_age_days`, default 180) and
    tests `EXISTS (SELECT 1 FROM article_files af WHERE af.article_id
    = a.id AND af.path IN (...))` per article via the
    `(article_id, path)` PK on `article_files`. Stops after `limit`
    matches.

    The earlier `JOIN article_files ... WHERE path IN (...) GROUP BY
    article_id ORDER BY date DESC` materialised every match (up to
    millions of rows for "hot" files like `Makefile`,
    `include/linux/kernel.h`) before sorting, taking 5+ minutes per
    request on the production corpus. The same shape on
    `mimir.subsystems_dashboard._path_filter` was already rewritten
    to this EXISTS-with-date-bound pattern (see
    `_subsystem_path_filter_exists_sql`); this function gets the
    matching treatment. Plan pinned in
    `tests/test_subsystems/test_path_matching.py`.
    """
    if not paths:
        return []
    min_date = datetime.now(timezone.utc) - timedelta(
        days=settings.recent_patches_max_age_days
    )
    # EXISTS-with-date-bound. The driver is `ix_articles_date` DESC
    # over the bounded window; the planner tests the EXISTS per
    # candidate article via the `(article_id, path)` PK on
    # `article_files` (cheap, ~3 files per article). Stops as soon
    # as `limit` matches are found.
    q = (
        select(
            Article.id,
            Article.message_id,
            Article.subject,
            Article.author,
            Article.date,
            Article.canonical_inbox_id,
        )
        .where(Article.date >= min_date)
        .where(
            exists(
                select(1)
                .select_from(ArticleFile)
                .where(ArticleFile.article_id == Article.id)
                .where(ArticleFile.path.in_(paths))
            )
        )
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
        out.append(
            RelatedPatch(
                article_id=art_id,
                message_id=mid,
                subject=subj,
                author=author,
                date=date,
                inbox_name=canon_name,
            )
        )
    return out


__all__ = [
    "RelatedPatch",
    "SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC",
    "SubsystemHit",
    "path_matches_glob",
    "recent_patches_touching",
    "subsystems_for_article",
]
