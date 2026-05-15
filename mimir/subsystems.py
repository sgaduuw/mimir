"""Subsystem resolution: map article-touched paths to the MAINTAINERS
sections that claim them.

Two read-path helpers:

- `subsystems_for_article(session, article_id)` — given an article,
  return the deduplicated set of `Subsystem` rows that any of its
  `article_files` paths land in. The header on the patch page reads
  this.
- `recent_patches_touching(session, paths, exclude_id, limit)` —
  given a set of paths, return the most-recent articles (other than
  the current one) that share at least one path. The sidebar on the
  patch page reads this.

Glob semantics intentionally simple — enough for the common
MAINTAINERS shapes:

- `dir/` matches every path under that directory (and the literal
  bare-directory path)
- `path/file.c` matches that exact path
- `path/*.c` and friends fall through to `fnmatch.fnmatchcase` for
  the wildcard cases

`X:` (exclude) entries veto a `F:` (include) match within the same
subsystem: if any exclude glob matches the path, that subsystem is
**not** returned for the path. Cross-subsystem exclusions don't
exist in MAINTAINERS — `X:` only acts on its own section.
"""
import fnmatch
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.dashboard import DAILY_VOLUME_CACHE_TTL_SEC, DailyVolume
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    Inbox,
    Subsystem,
    SubsystemPath,
)
from mimir.threading import (
    ACTIVE_THREADS_CACHE_TTL_SEC,
    ActiveThread,
    _active_threads_query,
)


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
    patch-page header needs — avoids carrying the entire SQLA row
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
    100k comparisons — fast in practice. If a future deploy with
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

    # Resolve inbox names in one bulk query — avoid N+1.
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
        # Canonical inbox fall-back: alphabetically-first link when
        # the explicit canonical_inbox_id isn't in the link set.
        canon_name: str | None = None
        if canon_id is not None:
            for ix_id, name in link_set:
                if ix_id == canon_id:
                    canon_name = name
                    break
        if canon_name is None and link_set:
            canon_name = min(name for _, name in link_set)
        if canon_name is None:
            continue  # shouldn't happen given FK cascades
        out.append(RelatedPatch(
            article_id=art_id, message_id=mid, subject=subj,
            author=author, date=date, inbox_name=canon_name,
        ))
    return out


def recent_articles_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem, limit: int = 20,
) -> list[RelatedPatch]:
    """Recent articles linked to `inbox` whose `article_files` paths
    match any of `subsystem`'s F: globs and aren't vetoed by its
    X: globs.

    Per-inbox scoping matches the other per-inbox surfaces
    (today / yesterday / year / month). The subsystem dashboard
    URL is `/<inbox>/subsystem/<name>/`, so we want articles linked
    to *this* inbox via `article_lists`.

    Glob handling in this slice covers the two MAINTAINERS shapes
    that dominate the file: trailing-slash directory prefixes and
    exact paths. Wildcard globs (`fs/*/file.c` and friends) are
    skipped silently — they're a small minority of rules and add
    a full-table scan to every dashboard hit. A follow-up slice
    can fold them in once the simple-glob case is shipping.
    """
    includes = [r for r in subsystem.paths if not r.is_exclude]
    excludes = [r.glob for r in subsystem.paths if r.is_exclude]

    # Build OR conditions for each supported include glob.
    or_conds = []
    for rule in includes:
        g = rule.glob
        if g.endswith("/"):
            # Directory prefix. SQLite LIKE doesn't treat `_` and `%`
            # as literal — escape them so a path glob containing `_`
            # (rare but possible: `arch/x86_64/`) doesn't widen the
            # match.
            prefix = g.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            or_conds.append(ArticleFile.path.like(prefix + "%", escape="\\"))
            # Also match the bare directory path (no trailing slash).
            or_conds.append(ArticleFile.path == g[:-1])
        elif not any(c in g for c in "*?["):
            or_conds.append(ArticleFile.path == g)
        # else: wildcard — skipped in slice 1
    if not or_conds:
        return []

    # Over-fetch by a factor so the X: exclude pass below has room
    # to filter without starving the result list.
    overfetch = max(limit * 3, 60)
    rows = session.execute(
        select(Article.id, Article.message_id, Article.subject,
               Article.author, Article.date,
               Article.canonical_inbox_id,
               ArticleFile.path)
        .join(ArticleFile, ArticleFile.article_id == Article.id)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(or_(*or_conds), ArticleList.inbox_id == inbox.id)
        .order_by(Article.date.desc())
        .limit(overfetch)
    ).all()

    # Group touched paths per article so the X: filter can see every
    # path a given article touched (not just the one row that matched
    # an include).
    art_paths: dict[int, set[str]] = defaultdict(set)
    art_data: dict[int, tuple] = {}
    art_order: list[int] = []  # preserve newest-first order from SQL
    for art_id, mid, subj, author, date, canon_id, path in rows:
        if art_id not in art_data:
            art_data[art_id] = (mid, subj, author, date, canon_id)
            art_order.append(art_id)
        art_paths[art_id].add(path)

    # The X: pass operates over the per-article path set: a subsystem
    # vetoes an article only if *every* matched path is excluded. If
    # at least one path remains in-scope after applying X:, the
    # article still belongs to the subsystem.
    valid_ids: list[int] = []
    for art_id in art_order:
        in_scope = [
            p for p in art_paths[art_id]
            if any(
                path_matches_glob(p, inc.glob) for inc in includes
            ) and not any(
                path_matches_glob(p, x) for x in excludes
            )
        ]
        if in_scope:
            valid_ids.append(art_id)
            if len(valid_ids) >= limit:
                break
    if not valid_ids:
        return []

    # Resolve inbox names for the canonical-or-fallback URL building,
    # one bulk query.
    links = session.execute(
        select(ArticleList.article_id, Inbox.id, Inbox.name)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_(valid_ids))
    ).all()
    links_by_article: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for art_id, ix_id, ix_name in links:
        links_by_article[art_id].append((ix_id, ix_name))

    out: list[RelatedPatch] = []
    for art_id in valid_ids:
        mid, subj, author, date, canon_id = art_data[art_id]
        link_set = links_by_article.get(art_id, [])
        canon_name: str | None = None
        if canon_id is not None:
            for ix_id, name in link_set:
                if ix_id == canon_id:
                    canon_name = name
                    break
        if canon_name is None and link_set:
            canon_name = min(name for _, name in link_set)
        if canon_name is None:
            continue
        out.append(RelatedPatch(
            article_id=art_id, message_id=mid, subject=subj,
            author=author, date=date, inbox_name=canon_name,
        ))
    return out


def _subsystem_path_filter_sql(
    subsystem: Subsystem, prefix: str = "ssp",
) -> tuple[str, dict] | None:
    """Return `(sql, params)` where `sql` is a
    `SELECT article_id FROM article_files WHERE ...` clause that
    enumerates article IDs matching the subsystem's F: globs and
    not vetoed by its X: globs. `params` is the bind-parameter
    dict to pass to `text()`.

    Per-path semantics: an article belongs to the subsystem if at
    least one of its `article_files.path` rows is matched by an
    include and not by any exclude. Same rule as the in-memory
    pass in `recent_articles_in_subsystem`.

    Returns `None` when the subsystem has no supported (non-
    wildcard) include rules — caller should treat that as "no
    articles" rather than running an unfiltered query. Wildcard
    F: rules (`fs/*/file.c`-style) are skipped silently in this
    slice for the same reason they're skipped in
    `recent_articles_in_subsystem`.
    """
    includes = [r.glob for r in subsystem.paths if not r.is_exclude]
    excludes = [r.glob for r in subsystem.paths if r.is_exclude]

    params: dict[str, str] = {}

    def build(globs: list[str], label: str) -> str:
        parts: list[str] = []
        for i, g in enumerate(globs):
            if g.endswith("/"):
                pname_pre = f"{prefix}_{label}_pre_{i}"
                # Escape LIKE wildcards so a glob containing `_`
                # (e.g. `arch/x86_64/`) doesn't widen the match.
                like_val = (
                    g.replace("\\", "\\\\")
                     .replace("%", "\\%")
                     .replace("_", "\\_")
                )
                params[pname_pre] = like_val + "%"
                parts.append(f"path LIKE :{pname_pre} ESCAPE '\\'")
                # Also include the bare directory path.
                pname_eq = f"{prefix}_{label}_eq_{i}"
                params[pname_eq] = g[:-1]
                parts.append(f"path = :{pname_eq}")
            elif not any(c in g for c in "*?["):
                pname = f"{prefix}_{label}_eq_{i}"
                params[pname] = g
                parts.append(f"path = :{pname}")
            # else: wildcard skipped (slice 1/2)
        return " OR ".join(parts)

    inc_sql = build(includes, "inc")
    if not inc_sql:
        return None
    sql = f"SELECT article_id FROM article_files WHERE ({inc_sql})"
    exc_sql = build(excludes, "exc")
    if exc_sql:
        sql += f" AND NOT ({exc_sql})"
    return sql, params


def daily_volume_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 30, force: bool = False,
) -> DailyVolume:
    """Daily message counts in `inbox` for articles whose paths
    match `subsystem`'s F: globs (minus X: vetoes) over the last
    `days` days, zero-filled. Cached per `(inbox, subsystem, days)`
    key for the same TTL as the global `daily_volume`.

    Returns an empty-but-zero-filled series (all zeros) when the
    subsystem has no supported globs; the sparkline still renders,
    just flat.
    """
    def compute() -> DailyVolume:
        today = date_cls.today()
        start = today - timedelta(days=days - 1)
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="dvss")
        if path_filter is None:
            return DailyVolume(
                days=[
                    (start + timedelta(days=i), 0)
                    for i in range(days)
                ],
                max_count=1,
            )
        path_sql, path_params = path_filter
        rows = session.execute(
            text(
                f"""
                SELECT date(a.date) AS day, COUNT(*) AS n
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id AND a.date >= :start
                  AND a.id IN ({path_sql})
                GROUP BY day
                """
            ),
            {"inbox_id": inbox.id, "start": start.isoformat(), **path_params},
        ).all()
        counts = {date_cls.fromisoformat(r.day): r.n for r in rows if r.day}
        series = [
            (start + timedelta(days=i), counts.get(start + timedelta(days=i), 0))
            for i in range(days)
        ]
        return DailyVolume(
            days=series,
            max_count=max((c for _, c in series), default=1),
        )

    return cache.get_or_compute(
        session,
        f"daily_volume_in_subsystem:{inbox.name}:{subsystem.id}:{days}",
        DAILY_VOLUME_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def active_threads_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 7, limit: int = 10, force: bool = False,
) -> list[ActiveThread]:
    """Most-active threads in `inbox` over the last `days` days
    among messages whose paths match `subsystem`'s F: globs (minus
    X: vetoes). Same decay-weighted score as the landing-page
    `active_threads`; the only difference is the seed-set filter.

    Returns an empty list when the subsystem has no supported
    globs (slice 1/2 ignores wildcard rules). Cached per
    `(inbox, subsystem, days, limit)` for the same TTL as the
    global active-threads cache.
    """
    def compute() -> list[ActiveThread]:
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="atss")
        if path_filter is None:
            return []
        path_sql, path_params = path_filter
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return _active_threads_query(
            session, inbox, start, end,
            order_by="score", limit=limit,
            extra_seed_filter_sql=f" AND a.id IN ({path_sql})",
            extra_params=path_params,
        )

    return cache.get_or_compute(
        session,
        f"active_threads_in_subsystem:{inbox.name}:{subsystem.id}:{days}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


@dataclass
class ReviewerStat:
    """One reviewer's activity in a subsystem over the recent window.

    `name` is the display name from the most-recent trailer the
    reviewer authored (the address is the stable identity; the name
    can drift across messages, and "their latest" is least-surprising
    for the visible label).

    `address` is the verbatim address from the most-recent trailer —
    same rationale as `name`. The render-time redaction policy
    (see `_redact_trailer_address` in `mimir.web`) consumes this and
    decides whether to show it or substitute `<hidden>` based on the
    allowlist.

    `address_normalized` is the lowercased address used for grouping
    and as the stable per-author identity (slice 3 will key URLs off
    it).

    `role_counts` is `{role: n}` summed across the window, e.g.
    `{"Reviewed-by": 12, "Acked-by": 4, "Tested-by": 1}`. `total`
    is the sum, kept separately so the template doesn't need a
    Jinja `sum` over the dict.

    `last_seen` is the most recent article date carrying any
    attestation by this reviewer, used as the secondary sort key
    (after `total`) so equally-active reviewers are ordered by
    freshness.

    Dataclass (not pydantic) for cache-encoder compatibility — see
    `mimir.cache` which round-trips registered types via
    `dataclasses.fields`.
    """
    name: str
    address: str
    address_normalized: str
    role_counts: dict[str, int]
    total: int
    last_seen: datetime


cache.register("ReviewerStat", ReviewerStat)


@dataclass
class ReviewEntry:
    """One attestation by a specific reviewer on a specific article.

    Same person under two roles on one article (Reported-by +
    Tested-by) shows as two entries — that's accurate to the source
    trailer block and useful for the per-reviewer page reading.

    `inbox_name` is the canonical inbox for the article (resolved
    via Article.canonical_inbox or falling back to any linked inbox);
    used to construct the message URL. The reviewer page itself is
    inbox-scoped, but cross-posted articles still get the right
    canonical link.
    """
    article_id: int
    message_id: str
    subject: str | None
    date: datetime | None
    role: str
    inbox_name: str


cache.register("ReviewEntry", ReviewEntry)


# Cap on per-reviewer attestation listings. 100 covers a year of
# heavy maintainer activity (Greg KH, Linus Torvalds, etc.) without
# the page growing unbounded; the cap surfaces in the truncation
# notice on the reviewer template.
REVIEWS_PER_PAGE_LIMIT = 100


def articles_reviewed_by(
    session: Session, inbox: Inbox, address_normalized: str,
    limit: int = REVIEWS_PER_PAGE_LIMIT, force: bool = False,
) -> list[ReviewEntry]:
    """All attestations by `address_normalized` on articles linked
    to `inbox`, newest-first.

    `address_normalized` is matched exactly against
    `article_trailers.address_normalized` (already lowercased at
    ingest); callers should `.lower()` before passing if the input
    came from a URL or untrusted source.

    Cached per `(inbox.name, address, limit)` for the same TTL as
    the threads helper. Cache key uses the address verbatim — it's
    already lowercased so casing collisions are impossible.
    """
    def compute() -> list[ReviewEntry]:
        rows = session.execute(
            text(
                """
                SELECT a.id AS article_id, a.message_id, a.subject,
                       a.date AS art_date, t.role,
                       COALESCE(canon.name, fb.name) AS inbox_name
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                LEFT JOIN inboxes canon
                    ON canon.id = a.canonical_inbox_id
                LEFT JOIN (
                    SELECT al2.article_id, MIN(i.name) AS name
                    FROM article_lists al2
                    JOIN inboxes i ON i.id = al2.inbox_id
                    GROUP BY al2.article_id
                ) fb ON fb.article_id = a.id
                WHERE al.inbox_id = :inbox_id
                  AND t.address_normalized = :addr
                ORDER BY a.date DESC
                LIMIT :limit
                """
            ),
            {
                "inbox_id": inbox.id,
                "addr": address_normalized,
                "limit": limit,
            },
        ).all()
        return [
            ReviewEntry(
                article_id=r.article_id,
                message_id=r.message_id,
                subject=r.subject,
                date=_coerce_dt(r.art_date) if r.art_date else None,
                role=r.role,
                inbox_name=r.inbox_name,
            )
            for r in rows
        ]

    return cache.get_or_compute(
        session,
        f"articles_reviewed_by:{inbox.name}:{address_normalized}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def active_reviewers_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 30, limit: int = 10, force: bool = False,
) -> list[ReviewerStat]:
    """Most-active reviewers in `inbox` over the last `days` days
    among messages whose paths match `subsystem`'s F: globs (minus
    X: vetoes).

    "Reviewer" here is anyone whose address appears on an indexed
    review-attestation trailer (`Reviewed-by` / `Acked-by` /
    `Tested-by` / `Reported-by` / `Suggested-by` /
    `Co-developed-by` / `Reported-and-tested-by`). Each trailer row
    is one attestation, counted once per role per article.

    Why a 30-day window (not 7d like `active_threads_in_subsystem`):
    review cadence is slower than discussion cadence — a maintainer
    who reviews two patches a week would render zero or one entries
    in a 7-day window, which is too lossy to rank usefully. 30 days
    is roughly one release-cycle's worth of activity.

    Returns an empty list when the subsystem has no supported globs
    (slice 1/2 ignores wildcard rules) or the window has no
    attestations. Cached per `(inbox, subsystem, days, limit)` for
    the same TTL as the threads helper.
    """
    def compute() -> list[ReviewerStat]:
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="arss")
        if path_filter is None:
            return []
        path_sql, path_params = path_filter
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        # Pull every matching trailer row for the window. ORDER BY
        # date DESC so the in-Python aggregator sees the freshest
        # attestation per reviewer first; that's the row whose
        # `name` / `address` we keep as the display surface.
        rows = session.execute(
            text(
                f"""
                SELECT t.role, t.name, t.address, t.address_normalized,
                       a.date AS art_date
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id
                  AND a.date >= :start
                  AND a.id IN ({path_sql})
                ORDER BY a.date DESC
                """
            ),
            {
                "inbox_id": inbox.id,
                "start": start.isoformat(),
                **path_params,
            },
        ).all()

        agg: dict[str, dict] = {}
        for r in rows:
            key = r.address_normalized
            entry = agg.get(key)
            if entry is None:
                # First (= most recent) row for this reviewer wins
                # the display name and verbatim address.
                entry = {
                    "name": r.name or "",
                    "address": r.address,
                    "address_normalized": key,
                    "role_counts": defaultdict(int),
                    "last_seen": _coerce_dt(r.art_date),
                }
                agg[key] = entry
            entry["role_counts"][r.role] += 1
            # Older rows can't lift `last_seen` (we sorted DESC) so
            # only the first-seen value matters; no max() needed.

        stats = [
            ReviewerStat(
                name=e["name"],
                address=e["address"],
                address_normalized=e["address_normalized"],
                role_counts=dict(e["role_counts"]),
                total=sum(e["role_counts"].values()),
                last_seen=e["last_seen"],
            )
            for e in agg.values()
        ]
        # Primary: total attestations DESC; tiebreak: last_seen DESC
        # so equally-active reviewers are ordered by freshness.
        stats.sort(key=lambda s: (-s.total, -s.last_seen.timestamp()))
        return stats[:limit]

    return cache.get_or_compute(
        session,
        f"active_reviewers_in_subsystem:{inbox.name}:{subsystem.id}:{days}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def _coerce_dt(value) -> datetime:
    """`text()` raw SQL returns the `articles.date` column as an ISO
    string; SQLAlchemy bypasses its type coercion on raw queries.
    Mirror of the `_coerce_dt` in `mimir.threading` — kept local so
    the import surface stays one-way (subsystems → threading is
    already the established direction)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "REVIEWS_PER_PAGE_LIMIT",
    "RelatedPatch",
    "ReviewEntry",
    "ReviewerStat",
    "SubsystemHit",
    "_subsystem_path_filter_sql",
    "active_reviewers_in_subsystem",
    "active_threads_in_subsystem",
    "articles_reviewed_by",
    "daily_volume_in_subsystem",
    "path_matches_glob",
    "recent_articles_in_subsystem",
    "recent_patches_touching",
    "subsystems_for_article",
]
