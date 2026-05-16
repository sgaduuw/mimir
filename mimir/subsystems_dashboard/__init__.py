"""Per-subsystem dashboard helpers + cross-inbox aggregations.

Pulled out of `mimir.subsystems` to keep that module focused on
the article-level path-matching primitives (which inbox lookups
need: `subsystems_for_article`, `recent_patches_touching`,
`path_matches_glob`). This package owns the heavier read-side
fan-out, split across three public submodules plus one shared
infrastructure module:

- `reads`: the per-subsystem read fan-outs
  (`recent_articles_in_subsystem`, `daily_volume_in_subsystem`,
  `active_threads_in_subsystem`).
- `reviewers`: the reviewer surfaces (`articles_reviewed_by`,
  `active_reviewers_in_subsystem`) and their `ReviewerStat` /
  `ReviewEntry` dataclasses.
- `activity`: the cross-inbox "most active subsystems"
  aggregations (`most_active_subsystems_in_inbox`,
  `most_active_subsystems_global`, and the `_full` cached
  payload helpers they slice).
- `_path_filter` (underscore-prefixed): the shared
  `_subsystem_path_filter_sql` builder, imported by `reads` and
  `reviewers`. Infrastructure, not a public surface.

`RelatedPatch` and `path_matches_glob` live in `mimir.subsystems`
(small, no-fan-out neighbours of the message-page header helper);
the submodules import them from there. Asymmetric:
`subsystems_dashboard` depends on `subsystems`, not the other way
around.

Cache-key conventions are documented inline per helper; bumping
`cache.NAMESPACE_VERSION` invalidates everything if a payload
shape changes.
"""
from mimir.subsystems_dashboard.activity import (
    MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC,
    MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
    SubsystemActivity,
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
)
from mimir.subsystems_dashboard.reads import (
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
    recent_articles_in_subsystem,
)
from mimir.subsystems_dashboard.reviewers import (
    REVIEWS_PER_PAGE_LIMIT,
    ReviewEntry,
    ReviewerStat,
    active_reviewers_in_subsystem,
    articles_reviewed_by,
)

__all__ = [
    "MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC",
    "MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP",
    "REVIEWS_PER_PAGE_LIMIT",
    "ReviewEntry",
    "ReviewerStat",
    "SubsystemActivity",
    "active_reviewers_in_subsystem",
    "active_threads_in_subsystem",
    "articles_reviewed_by",
    "daily_volume_in_subsystem",
    "most_active_subsystems_global",
    "most_active_subsystems_in_inbox",
    "recent_articles_in_subsystem",
]
