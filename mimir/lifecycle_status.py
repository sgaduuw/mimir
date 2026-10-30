"""5-state lifecycle status for the listing-row pill and at-a-glance
maintainer triage. See spec
`_claude/specs/2026-05-26-multi-tree-lifecycle-design.md` for the
taxonomy rationale.

State priority (highest first): LANDED > SUPERSEDED > QUEUED >
REVIEWED > PENDING. The bulk fetcher reads `mainline_commits`,
`article_trailers`, and a self-join on `articles` (for supersedance)
in one combined SELECT; a query-plan-pin test (Task 9) ensures all
three LEFT JOIN sources stay on index seeks rather than scans
(regression guard against an ANALYZE drift).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import aware_utc as _aware_utc
from mimir.datetime_utils import coerce_dt
from mimir.models import ArticleList
from mimir.parser import unfold
from mimir.patch_series import (
    newer_revision_sql,
    parse_cover_letter,
    same_series_sql,
    version_number_sql,
)
from mimir.threading import unmaterialised_roots
from mimir.trailers import REVIEW_TRAILER_ROLES

# Strip control bytes + surrogates before flowing reviewer names
# into the lifecycle pill's `data-tooltip` attribute. Same shape as
# `mimir.canonical._UNSAFE_ATTR_CHARS_RE` (kept duplicated rather
# than imported across module boundaries, both definitions are
# private; promote to a shared utility module if a third use case
# appears). Jinja autoescape covers `<`, `>`, `"`, `&`; this
# regex catches C0/C1 control bytes + the surrogate range that
# Jinja would happily pass through to break a rendered attribute.
_UNSAFE_ATTR_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


class LifecycleStatus(Enum):
    LANDED = "landed"
    SUPERSEDED = "superseded"
    QUEUED = "queued"
    REVIEWED = "reviewed"
    PENDING = "pending"


@dataclass
class LifecycleStatusInfo:
    """One article's lifecycle status + display data.

    `state_value` is the canonical state name (one of
    `LifecycleStatus` values, stored as string for cache
    round-trip). `tree` is the earliest non-Linus tree for the
    QUEUED state, None otherwise.

    The remaining display-derivation fields are pre-computed by
    `_bulk_uncached` so templates render without further
    Python-side logic. They default to dormant / empty so cache
    rows from prior versions deserialize cleanly during rollout.

    The evidence fields are the state's own row, for the status line
    (#844), so it can never name a different commit or revision than
    the pill: the Linus commit for LANDED, the earliest other tree's
    for QUEUED (`tree`), and the newest revision for SUPERSEDED (`superseded_by_id` is
    the slot's patch of that revision (#949), None on a row cached
    before it existed).

    `rollup` is a cover's progress when its patches differ (#938), like
    "3 of 5 landed": the state is the lowest rung every patch of the
    posting reached, and this counts the patches that reached the highest
    rung above it. None for a patch, and for a cover whose patches are
    level."""

    state_value: str
    tree: str | None = None
    activity_heat: str = "dormant"
    activity_detail: str = "no replies"
    pill_label: str = ""
    count_suffix: str | None = None
    count_words: str | None = None
    tooltip: str | None = None
    commit_sha: str | None = None
    committed_at: datetime | None = None
    superseded_by_version: str | None = None
    superseded_at: datetime | None = None
    superseded_by_id: int | None = None
    rollup: str | None = None

    @property
    def state(self) -> LifecycleStatus:
        return LifecycleStatus(self.state_value)


cache.register("LifecycleStatusInfo", LifecycleStatusInfo)

LIFECYCLE_STATUS_TTL_SEC = 300


_REVIEW_ROLES_PARAM = bindparam(
    "review_roles", value=list(REVIEW_TRAILER_ROLES), expanding=True
)


# Use a parameterised IN(...) via SQLAlchemy text() with expanding
# bindparam. Keeps the query plan-pinned and dodges SQLite's limit
# on positional params for very large lists. Exposed at module
# level so the plan-pin test (Task 9) can EXPLAIN it.
# Thread activity is not in this statement: see `_thread_dates`.
#
# `mc` carries the in_linus count + linus_sha/at AND the earliest non-
# linus tree + sha/at for the LANDED/QUEUED tooltip leading lines.
#
# `rc` is the per-article review-trailer count (total + maintainer
# subset). The maintainer flag is an EXISTS on LOWER(sm.address) =
# t.address_normalized, not a JOIN: an address is listed once per
# MAINTAINERS section, so a join multiplies each trailer by the number
# of sections its reviewer sits in (#800). The
# `at_review` CTE is the cheap "any review trailer exists" predicate
# that still drives the REVIEWED state classification (kept distinct
# from rc so the state derivation doesn't pay the COUNT cost when
# rc is None).
#
# `rollup` is a cover's progress (#938): its posting's slot winners
# (#861), each probed for a Linus landing, another tree's, a review
# trailer and a newer revision of its own.
#
# `sup` compares revision numbers, not strings, so v10 > v9, and takes
# the newest revision's own version and date for the SUPERSEDED
# tooltip and the status line, so the two always describe one real
# posting. Within that version it picks by the slot rule (#861, #949):
# a copy with a diff first, then the lowest id, so the pill, the
# timeline's entry and the status line's link name one article and the
# date is when the version was first posted with a diff. The Linus sha
# and date in `mc` come from one row for the same reason: the earliest,
# which is the landing the status line names.
#
# The `mc` CTE's "earliest other tree" orders by committed_at, then
# prefers a subsystem tree over linux-next, then the name. A subsystem
# tree's commit reaches linux-next with the same sha and committed_at,
# so committed_at alone ties and the pill would follow the physical row
# order that linux-next's daily rebuild rewrites, while the page
# validator (row count, max date) stays put (#673). The status line
# reads these columns from `LifecycleStatusInfo`, so it has no ordering
# of its own to keep in step (#844).
_V2 = version_number_sql("a2.patch_series_version")
_NEWER = newer_revision_sql("a1", "a2")
# A cover's posting (#938): the patches of its series and version string
# (`patch_series.same_posting`, in SQL), and a patch's own newer revision.
_POSTING = (
    same_series_sql("p", "c")
    + " AND p.patch_series_version = c.patch_series_version"
    + " AND p.patch_series_position > 0"
)
_NEWER_PATCH = newer_revision_sql("p", "a2")
# Has-diff probe: sorts a diff-carrying row ahead of a bare one (#949, #938).
_NO_DIFF = (
    "NOT EXISTS (SELECT f.article_id FROM article_files f WHERE f.article_id = {}.id)"
)
_NO_DIFF_A2 = _NO_DIFF.format("a2")
_NO_DIFF_P = _NO_DIFF.format("p")

_BULK_SQL = text(f"""
WITH
mc AS (
    SELECT a.id AS article_id,
           SUM(CASE WHEN c.tree_name = 'linus' THEN 1 ELSE 0 END) AS in_linus,
           (SELECT cc.commit_sha FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name = 'linus'
              ORDER BY cc.committed_at ASC, cc.commit_sha
              LIMIT 1) AS linus_sha,
           (SELECT cc.committed_at FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name = 'linus'
              ORDER BY cc.committed_at ASC, cc.commit_sha
              LIMIT 1) AS linus_committed_at,
           (SELECT cc.tree_name FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC, cc.tree_name = 'linux-next', cc.tree_name,
                       cc.commit_sha
              LIMIT 1) AS earliest_other_tree,
           (SELECT cc.commit_sha FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC, cc.tree_name = 'linux-next', cc.tree_name,
                       cc.commit_sha
              LIMIT 1) AS earliest_other_sha,
           (SELECT cc.committed_at FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC, cc.tree_name = 'linux-next', cc.tree_name,
                       cc.commit_sha
              LIMIT 1) AS earliest_other_at
      FROM articles a
      LEFT JOIN mainline_commits c ON c.message_id = a.message_id
     WHERE a.id IN :ids
     GROUP BY a.id
),
rc AS (
    SELECT t.article_id,
           COUNT(*) AS review_total,
           SUM(EXISTS (SELECT 1 FROM subsystem_maintainers sm
                        WHERE LOWER(sm.address) = t.address_normalized
                          AND sm.role IN ('M', 'R'))) AS review_maintainer
      FROM article_trailers t
     WHERE t.article_id IN :ids
       AND t.role IN :review_roles
     GROUP BY t.article_id
),
at_review AS (
    SELECT t.article_id, 1 AS has_review
      FROM article_trailers t
     WHERE t.article_id IN :ids
       AND t.role IN :review_roles
     GROUP BY t.article_id
),
newer AS (
    SELECT a1.id AS leaf,
           a2.patch_series_version AS sup_by_version,
           a2.date AS sup_by_date,
           a2.id AS sup_by_id,
           ROW_NUMBER() OVER (
               PARTITION BY a1.id
               ORDER BY {_V2} DESC,
                        {_NO_DIFF_A2},
                        a2.id
           ) AS rn
      FROM articles a1
      JOIN articles a2 ON {_NEWER}
     WHERE a1.id IN :ids
),
sup AS (
    SELECT leaf, sup_by_version, sup_by_date, sup_by_id FROM newer WHERE rn = 1
),
posting AS (
    SELECT c.id AS cover,
           p.id AS patch,
           ROW_NUMBER() OVER (
               PARTITION BY c.id, p.patch_series_position
               ORDER BY {_NO_DIFF_P},
                        p.id
           ) AS rn
      FROM articles c
      JOIN articles p ON {_POSTING}
     WHERE c.id IN :ids
       AND c.patch_series_key IS NOT NULL
       AND COALESCE(c.patch_series_position, 0) = 0
),
rollup AS (
    SELECT cover,
           COUNT(*) AS slots,
           SUM(landed) AS n_landed,
           SUM(landed OR (live AND other)) AS n_queued,
           SUM(landed OR (live AND (other OR reviewed))) AS n_reviewed
      FROM (
        SELECT po.cover AS cover,
               EXISTS (SELECT 1 FROM mainline_commits m
                        WHERE m.message_id = p.message_id
                          AND m.tree_name = 'linus') AS landed,
               EXISTS (SELECT 1 FROM mainline_commits m
                        WHERE m.message_id = p.message_id
                          AND m.tree_name != 'linus') AS other,
               EXISTS (SELECT t.article_id FROM article_trailers t
                        WHERE t.article_id = p.id
                          AND t.role IN :review_roles) AS reviewed,
               NOT EXISTS (SELECT 1 FROM articles a2
                            WHERE {_NEWER_PATCH}) AS live
          FROM posting po
          JOIN articles p ON p.id = po.patch
         WHERE po.rn = 1
      )
     GROUP BY cover
)
SELECT a.id,
       a.subject,
       a.date AS article_date,
       COALESCE(mc.in_linus, 0) AS in_linus,
       mc.linus_sha,
       mc.linus_committed_at,
       mc.earliest_other_tree AS earliest_other,
       mc.earliest_other_sha,
       mc.earliest_other_at,
       (at_review.has_review IS NOT NULL) AS has_review,
       (sup.leaf IS NOT NULL) AS superseded,
       sup.sup_by_version,
       sup.sup_by_date,
       sup.sup_by_id,
       ru.slots,
       ru.n_landed,
       ru.n_queued,
       ru.n_reviewed,
       COALESCE(rc.review_total, 0) AS review_total,
       COALESCE(rc.review_maintainer, 0) AS review_maintainer
  FROM articles a
  LEFT JOIN mc ON mc.article_id = a.id
  LEFT JOIN at_review ON at_review.article_id = a.id
  LEFT JOIN sup ON sup.leaf = a.id
  LEFT JOIN rollup ru ON ru.cover = a.id
  LEFT JOIN rc ON rc.article_id = a.id
 WHERE a.id IN :ids
""").bindparams(bindparam("ids", expanding=True), _REVIEW_ROLES_PARAM)


# Thread activity, read two ways. The walk is recursive-CTE shaped:
# roots(leaf, ...) walks UP from each leaf via thread_parent so
# root_per_leaf picks the topmost ancestor; then descendants(...) walks
# DOWN from each root and thread_dates takes the MAX(date) over the
# subtree. Both walks stay inside the requesting inbox, so the chip reads
# the same inbox-scoped thread the page and its ETag do (#676), root
# included: `_find_thread_root_cte` stops at the same ancestors. EXISTS,
# not JOIN: without sqlite_stat1 a JOIN is driven from
# `ix_article_lists_inbox_id` and reads the whole inbox per message walked.
#
# `_THREAD_DATES_COLUMN_SQL` reads the materialised
# `article_lists.thread_root_id` through `ix_article_lists_thread_root`: the newest other article in the
# leaf's own inbox-scoped thread, one range read instead of a walk of
# every message in it (#798: a 12,342 reply thread paid that walk on
# every first visit to a message page). It is used for threads
# `unmaterialised_roots` calls materialised. `_THREAD_DATES_WALK_SQL` is
# the fallback for the rest. Both exclude the leaf itself, so
# `last_thread_date` is NULL for an article with no replies.
_THREAD_DATES_COLUMN_SQL = text("""
SELECT l.article_id AS leaf,
       (SELECT MAX(a.date)
          FROM article_lists al
          JOIN articles a ON a.id = al.article_id
         WHERE al.inbox_id = :inbox_id
           AND al.thread_root_id = l.thread_root_id
           AND al.article_id != l.article_id) AS last_thread_date
  FROM article_lists l
 WHERE l.inbox_id = :inbox_id AND l.article_id IN :ids
""").bindparams(bindparam("ids", expanding=True))

_THREAD_DATES_WALK_SQL = text("""
WITH RECURSIVE
roots(leaf, message_id, thread_parent, depth) AS (
    SELECT id, message_id, thread_parent, 0
      FROM articles WHERE id IN :ids
    UNION ALL
    SELECT r.leaf, p.message_id, p.thread_parent, r.depth + 1
      FROM roots r
      JOIN articles p ON p.message_id = r.thread_parent
     WHERE r.thread_parent IS NOT NULL AND r.depth < 1000
       AND EXISTS (SELECT 1 FROM article_lists pl
                    WHERE pl.article_id = p.id AND pl.inbox_id = :inbox_id)
),
root_per_leaf AS (
    SELECT leaf, message_id AS root_msgid
      FROM (
          SELECT leaf, message_id,
                 ROW_NUMBER() OVER (PARTITION BY leaf ORDER BY depth DESC) AS rn
            FROM roots
      )
     WHERE rn = 1
),
descendants(root_msgid, message_id, thread_parent, depth) AS (
    SELECT root_msgid, root_msgid, NULL, 0 FROM root_per_leaf
    UNION
    SELECT d.root_msgid, c.message_id, c.thread_parent, d.depth + 1
      FROM descendants d
      JOIN articles c ON c.thread_parent = d.message_id
     WHERE d.depth < 1000
       AND EXISTS (SELECT 1 FROM article_lists cl
                    WHERE cl.article_id = c.id AND cl.inbox_id = :inbox_id)
),
thread_dates AS (
    SELECT r.leaf, MAX(a.date) AS last_thread_date
      FROM root_per_leaf r
      JOIN descendants d ON d.root_msgid = r.root_msgid
      JOIN articles a ON a.message_id = d.message_id
     WHERE a.message_id != (SELECT message_id FROM articles WHERE id = r.leaf)
     GROUP BY r.leaf
)
SELECT leaf, last_thread_date FROM thread_dates
""").bindparams(bindparam("ids", expanding=True))


def _thread_dates(
    session: Session, article_ids: list[int], inbox_id: int
) -> dict[int, object]:
    """Newest other-article date in each leaf's thread, by leaf id.

    Leaves in a materialised thread take the column read; the rest
    (NULL `thread_root_id`, or a thread with an unrooted row) take the
    walk. Leaves with no replies, or not in this inbox, are absent.
    """
    roots = dict(
        session.execute(
            select(ArticleList.article_id, ArticleList.thread_root_id).where(
                ArticleList.inbox_id == inbox_id,
                ArticleList.article_id.in_(article_ids),
            )
        ).all()
    )
    stale = unmaterialised_roots(
        session, inbox_id, sorted({r for r in roots.values() if r is not None})
    )
    fast = {a for a, r in roots.items() if r is not None and r not in stale}
    walk = [a for a in article_ids if a not in fast]
    out: dict[int, object] = {}
    for ids, sql in (
        (sorted(fast), _THREAD_DATES_COLUMN_SQL),
        (walk, _THREAD_DATES_WALK_SQL),
    ):
        if ids:
            params = {"ids": ids, "inbox_id": inbox_id}
            out.update(
                (r.leaf, r.last_thread_date)
                for r in session.execute(sql, params)
                if r.last_thread_date is not None
            )
    return out


# Per-article reviewer names for the tooltip. Kept separate from
# `_BULK_SQL` so the main fetcher's plan stays clean (only the
# tooltip renderer consumes the names). Order by `t.id` so the
# emission order is stable + matches trailer insertion order
# (article_trailers has no `created_at`; PK id ascends in insert
# order). `_format_tooltip` re-orders maintainers-first at render.
_REVIEWER_NAMES_SQL = text("""
SELECT t.article_id,
       t.name,
       EXISTS (SELECT 1 FROM subsystem_maintainers sm
                WHERE LOWER(sm.address) = t.address_normalized
                  AND sm.role IN ('M', 'R')) AS is_maintainer
  FROM article_trailers t
 WHERE t.article_id IN :ids
   AND t.role IN :review_roles
 ORDER BY t.article_id, t.id ASC
""").bindparams(bindparam("ids", expanding=True), _REVIEW_ROLES_PARAM)


def _format_pill_label(state: LifecycleStatus, tree: str | None) -> str:
    """Build the uppercase pill label for a state. QUEUED reads
    `IN <TREE>` (kebab-case slug uppercased). LANDED / REVIEWED /
    SUPERSEDED use the state name verbatim. PENDING returns "" so
    a defensive caller that bypasses the template's `state.value
    != 'pending'` gate never renders a stray label."""
    if state == LifecycleStatus.PENDING:
        return ""
    if state == LifecycleStatus.QUEUED:
        return f"IN {tree.upper()}" if tree else "QUEUED"
    return state.value.upper()


def _format_tooltip(
    *,
    state: LifecycleStatus,
    linus_sha: str | None,
    linus_committed_at: datetime | None,
    earliest_other_sha: str | None,
    earliest_other_at: datetime | None,
    superseded_by_version: str | None,
    superseded_by_posted_at: datetime | None,
    reviewers: list[tuple[str, bool]],
) -> str | None:
    """Per-state tooltip text for the lifecycle pill. None when
    state is PENDING (no pill rendered, no tooltip).

    Maintainers ordered first within the reviewer list (preserving
    input order within each group); each line carries an `M `
    prefix for maintainers, plain name otherwise.

    Leading line by state:
      - LANDED: `<linus_sha[:12]> · <linus_at YYYY-MM-DD HH:MM>`
      - QUEUED: `<earliest_other_sha[:12]> · <earliest_other_at>`
      - REVIEWED: (none; names start on line 1)
      - SUPERSEDED: `Superseded by vN posted <date YYYY-MM-DD>`
      - PENDING: returns None
    """
    if state == LifecycleStatus.PENDING:
        return None

    lines: list[str] = []
    if state == LifecycleStatus.LANDED and linus_sha and linus_committed_at:
        lines.append(
            f"{linus_sha[:12]} · {linus_committed_at.strftime('%Y-%m-%d %H:%M')}"
        )
    elif state == LifecycleStatus.QUEUED and earliest_other_sha and earliest_other_at:
        lines.append(
            f"{earliest_other_sha[:12]} · {earliest_other_at.strftime('%Y-%m-%d %H:%M')}"
        )
    elif state == LifecycleStatus.SUPERSEDED:
        if superseded_by_version and superseded_by_posted_at:
            lines.append(
                f"Superseded by {superseded_by_version} "
                f"posted {superseded_by_posted_at.strftime('%Y-%m-%d')}"
            )
        else:
            lines.append("Superseded")

    # Reviewer names: dedup by name (a single maintainer filing
    # Reviewed-by once per patch in a multi-patch series surfaces
    # as N copies of the same name in `reviewers`; the tooltip
    # should show one line per human, not one per trailer row).
    # First-seen wins; if the same name appears both as maintainer
    # and non-maintainer (parser quirk), the maintainer-flagged
    # entry takes precedence so we don't lose the `M ` prefix.
    #
    # Scrub control bytes from each name before it flows into the
    # `data-tooltip` attribute (same posture as
    # `canonical.extract_list_addresses` for similarly-routed
    # data). Jinja autoescape handles `<`, `>`, `"`, `&`; the
    # surrogate-escaped or C0/C1 control byte that survives
    # autoescape but breaks the rendered attribute is what this
    # filter covers. The parser already scrubs surrogates on the
    # ParsedArticle side, but trailers come from message bodies
    # and the trailer name is whatever bytes the contributor put
    # after `Reviewed-by:` and before the `<address>` bracket.
    # Function-local, like `mimir.seo`: `mimir.web` imports this module.
    from mimir.web.filters import _usable_name

    seen_as_maintainer: set[str] = set()
    seen_as_other: set[str] = set()
    maintainers: list[str] = []
    others: list[str] = []
    for raw_name, is_m in reviewers:
        # Same rule as a From name: an address-shaped name is no name (#902).
        name = _UNSAFE_ATTR_CHARS_RE.sub("", _usable_name(raw_name) or "").strip()
        if not name:
            continue
        if is_m:
            if name not in seen_as_maintainer:
                seen_as_maintainer.add(name)
                maintainers.append(name)
        else:
            # Skip if we already accepted this name as a maintainer;
            # otherwise add as non-maintainer (first-seen wins).
            if name in seen_as_maintainer:
                continue
            if name not in seen_as_other:
                seen_as_other.add(name)
                others.append(name)

    for name in maintainers:
        lines.append(f"M {name}")
    for name in others:
        lines.append(name)

    return "\n".join(lines) if lines else None


def _format_count_suffix(total: int, maintainer_count: int) -> str | None:
    """Build the inline count suffix `: N (XM)` for the pill, or
    None when the count is zero (`LANDED` with no reviews is just
    `LANDED`, no suffix)."""
    if total == 0:
        return None
    return f": {total} ({maintainer_count}M)"


def _format_count_words(total: int, maintainer_count: int) -> str | None:
    """`_format_count_suffix` as words, for prose with room for them:
    `2 reviews, 2 by maintainers`. None when the count is zero."""
    if total == 0:
        return None
    words = f"{total} review{'' if total == 1 else 's'}"
    if maintainer_count:
        who = "a maintainer" if maintainer_count == 1 else "maintainers"
        words += f", {maintainer_count} by {who}"
    return words


def _cover_rollup(own: LifecycleStatus, row) -> tuple[LifecycleStatus, str | None]:
    """A cover's state and `rollup` from its posting's patches (#938).
    `row` is a bulk row with at least one stored patch (`row.slots`).

    The state is the lowest rung every patch reached (landed, queued,
    reviewed, else pending), over the cover's declared total, so a patch
    missing from the archive counts as pending. A cover superseded by a
    newer cover stays superseded and only adds the landed count. The count
    names the highest rung above the state that any patch reached. A cover
    never names a tree: its patches may sit in several."""
    cover = parse_cover_letter(unfold(row.subject))
    total = max(row.slots, cover.total if cover else 0)
    # Patches that reached each rung or a higher one, highest rung first.
    rungs = (
        (LifecycleStatus.LANDED, row.n_landed),
        (LifecycleStatus.QUEUED, row.n_queued),
        (LifecycleStatus.REVIEWED, row.n_reviewed),
    )
    if own == LifecycleStatus.SUPERSEDED:
        state = own
        above = rungs[:1]
    else:
        i = next((i for i, (_, n) in enumerate(rungs) if n == total), len(rungs))
        state = rungs[i][0] if i < len(rungs) else LifecycleStatus.PENDING
        above = rungs[:i]
    count = next(((rung, n) for rung, n in above if n), None)
    if count is None:
        return state, None
    return state, f"{count[1]} of {total} {count[0].value}"


def _bulk_uncached(
    session: Session,
    article_ids: list[int],
    inbox_id: int,
) -> dict[int, LifecycleStatusInfo]:
    """Run the bulk SQL + reviewer-names query, derive the 5-state
    classification, and pre-compute all display fields per row.
    Pure compute; no cache. Called by `lifecycle_status_for_
    articles` on cache misses and by the query-plan-pin test
    directly."""
    rows = session.execute(_BULK_SQL, {"ids": article_ids, "inbox_id": inbox_id}).all()
    thread_dates = _thread_dates(session, article_ids, inbox_id)
    name_rows = session.execute(_REVIEWER_NAMES_SQL, {"ids": article_ids}).all()

    # Index reviewer names per article: [(name, is_maintainer), ...]
    # in trailer-insertion order (PK id ascending). `_format_tooltip`
    # re-sorts maintainers-first at render time.
    names_by_article: dict[int, list[tuple[str, bool]]] = {}
    for nr in name_rows:
        names_by_article.setdefault(nr.article_id, []).append(
            (nr.name or "", bool(nr.is_maintainer))
        )

    # `_activity_heat` lives on patch_state to keep the heat-bucket
    # definition next to its sibling `_days_since_last_reply` helper.
    # Deferred function-scope import dodges a circular module-load
    # path through cache.register at import time.
    from mimir.patch_state import _activity_heat

    now = datetime.now(UTC)
    out: dict[int, LifecycleStatusInfo] = {}
    for row in rows:
        # State classification: same five-way priority as before
        # (LANDED > SUPERSEDED > QUEUED > REVIEWED > PENDING).
        if row.in_linus and row.in_linus > 0:
            state = LifecycleStatus.LANDED
            tree = None
        elif row.superseded:
            state = LifecycleStatus.SUPERSEDED
            tree = None
        elif row.earliest_other:
            state = LifecycleStatus.QUEUED
            tree = row.earliest_other
        elif row.has_review:
            state = LifecycleStatus.REVIEWED
            tree = None
        else:
            state = LifecycleStatus.PENDING
            tree = None

        rollup = None
        if row.slots:
            state, rollup = _cover_rollup(state, row)
            tree = None

        # Thread-activity → heat. Compute days from the SQL-derived
        # `last_thread_date` (NULL when the article has no replies).
        # Treat last_thread_date <= article_date as "no replies"
        # (e.g. a malformed thread where a parent post-dates a
        # child); the heat bucket then collapses to dormant.
        last_at = coerce_dt(thread_dates.get(row.id))
        article_at = coerce_dt(row.article_date)
        days: int | None
        age_days = max(0, (now - _aware_utc(article_at)).days) if article_at else None
        if last_at is None or article_at is None:
            days = None
        else:
            last_at_aware = _aware_utc(last_at)
            article_at_aware = _aware_utc(article_at)
            if last_at_aware <= article_at_aware:
                days = None
            else:
                days = max(0, (now - last_at_aware).days)
        heat, detail = _activity_heat(days, age_days)

        pill_label = _format_pill_label(state, tree)
        if rollup and state == LifecycleStatus.PENDING:
            pill_label = "PENDING"
        count_suffix = _format_count_suffix(
            row.review_total or 0,
            row.review_maintainer or 0,
        )
        # `sup_by_date` and the committed_at fields likewise need
        # coercion: they're carried through CTE projections + outer
        # SELECTs, which SQLAlchemy can't type-annotate for raw text().
        # A rolled-up cover names no commit: its own row is not the
        # posting's evidence, and would be paired with the posting's state.
        own_commits = not row.slots
        tooltip = _format_tooltip(
            state=state,
            linus_sha=row.linus_sha if own_commits else None,
            linus_committed_at=coerce_dt(row.linus_committed_at)
            if own_commits
            else None,
            earliest_other_sha=row.earliest_other_sha if own_commits else None,
            earliest_other_at=coerce_dt(row.earliest_other_at) if own_commits else None,
            superseded_by_version=row.sup_by_version,
            superseded_by_posted_at=coerce_dt(row.sup_by_date),
            reviewers=names_by_article.get(row.id, []),
        )

        commit_sha = committed_at = None
        if own_commits and state == LifecycleStatus.LANDED:
            commit_sha, committed_at = row.linus_sha, row.linus_committed_at
        elif own_commits and state == LifecycleStatus.QUEUED:
            commit_sha, committed_at = row.earliest_other_sha, row.earliest_other_at
        superseded = state == LifecycleStatus.SUPERSEDED
        out[row.id] = LifecycleStatusInfo(
            state_value=state.value,
            tree=tree,
            activity_heat=heat,
            activity_detail=detail,
            pill_label=pill_label,
            count_suffix=count_suffix,
            count_words=_format_count_words(
                row.review_total or 0, row.review_maintainer or 0
            ),
            tooltip=tooltip,
            commit_sha=commit_sha,
            committed_at=coerce_dt(committed_at),
            superseded_by_version=row.sup_by_version if superseded else None,
            superseded_at=coerce_dt(row.sup_by_date) if superseded else None,
            superseded_by_id=row.sup_by_id if superseded else None,
            rollup=rollup,
        )
    return out


def lifecycle_status_for_articles(
    session: Session,
    article_ids: list[int],
    inbox_id: int,
    *,
    state_key: str | None = None,
) -> dict[int, LifecycleStatusInfo]:
    """Bulk lifecycle-status fetch for a listing of articles.

    `inbox_id` scopes the activity chip to replies in that inbox, as the
    page's own thread is, and is part of the cache key: a cross-posted
    article has one chip per inbox.

    Per-article cache rows with a 5-minute TTL. Cache key
    `lifecycle_status:<id>:i<inbox_id>`, backed by `cache.get_many` so a
    single SELECT covers all hits. Misses are computed together in
    `_bulk_uncached`, a fixed number of statements however many ids. Empty input
    -> empty dict. Missing IDs (e.g. articles not in the corpus)
    are absent from the result.

    `state_key` is for the pages that carry an ETag (message and
    thread), which pass the digest of the ETag's content part (the ETag
    minus its page and full/htmx fields). A pill cached under the bare
    id can be older than the validator, and once the row expired the
    body changed under an ETag that did not, pinning the stale pill in
    every cache that kept it. Keyed by the content part, a cached pill
    is reused only while the page content is unchanged, and every page
    of a thread shares one entry. Time still moves the pill under an
    unchanged ETag: the activity chip's buckets are relative to now and
    age within the TTL. Cached rather than computed live because a miss
    costs several statements, and an unmaterialised thread falls back to
    walking it whole.
    """
    if not article_ids:
        return {}
    suffix = f":i{inbox_id}" + (f":{state_key}" if state_key is not None else "")
    key_to_id = {f"lifecycle_status:{a}{suffix}": a for a in article_ids}
    hits = cache.get_many(list(key_to_id))
    out: dict[int, LifecycleStatusInfo] = {key_to_id[k]: v for k, v in hits.items()}
    missing_ids = [a for a in article_ids if a not in out]
    if missing_ids:
        computed = _bulk_uncached(session, missing_ids, inbox_id)
        for article_id, info in computed.items():
            cache.set(
                f"lifecycle_status:{article_id}{suffix}",
                info,
                ttl=LIFECYCLE_STATUS_TTL_SEC,
            )
            out[article_id] = info
    return out


__all__ = [
    "LIFECYCLE_STATUS_TTL_SEC",
    "LifecycleStatus",
    "LifecycleStatusInfo",
    "_bulk_uncached",
    "_format_count_suffix",
    "_format_count_words",
    "_format_pill_label",
    "_format_tooltip",
    "lifecycle_status_for_articles",
]
