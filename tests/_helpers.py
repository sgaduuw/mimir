"""Helpers shared by test modules."""

from mimir import cache
from mimir.dashboard import ArchiveStats, _archive_stats_key


def _cache_total(inbox, total):
    """Give `inbox` a live `archive_stats` row of `total` articles."""
    cache.set(
        _archive_stats_key(inbox),
        ArchiveStats(total=total, epochs=1, first_date=None, last_date=None),
        3600,
    )
