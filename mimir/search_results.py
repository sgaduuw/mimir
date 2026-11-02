"""Group the bounded, cached search matches using current inbox membership."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from mimir.dashboard import ArticleSummary
from mimir.datetime_utils import coerce_dt
from mimir.models import Inbox
from mimir.patch_series import series_head_sql
from mimir.patch_subjects import _is_patch_subject

logger = logging.getLogger(__name__)

SEARCH_METADATA_SQL = f"""
    SELECT /* search_metadata */ a.id, a.date AS message_date,
           al.thread_root_id, root.id AS root_id, root.date AS root_date,
           a.subject, a.patch_series_key, a.patch_series_version,
           a.patch_series_position, {series_head_sql("a")} AS series_head
    FROM article_lists al
    JOIN articles a ON a.id = al.article_id
    LEFT JOIN article_lists rl ON rl.article_id = al.thread_root_id
         AND rl.inbox_id = al.inbox_id AND rl.thread_root_id = rl.article_id
    LEFT JOIN articles root ON root.id = rl.article_id
    WHERE al.inbox_id = :inbox_id AND al.article_id IN :ids
"""


@dataclass
class SearchGroup:
    representative: ArticleSummary
    root_id: int | None
    destination_date: datetime | None
    count: int = 0
    authors: list[str | None] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    version: str | None = None


def _posting(row):
    if (
        row.patch_series_key
        and row.patch_series_version
        and row.patch_series_position is not None
        and row.patch_series_position > 0
        and _is_patch_subject(row.subject)
    ):
        return (row.patch_series_key, row.series_head, row.patch_series_version)
    return None


def group_search_results(
    session: Session, inbox: Inbox, matches: list[ArticleSummary]
) -> list[SearchGroup]:
    """One indexed metadata read, no ancestor walks or grouped-result cache."""
    if not matches:
        return []
    metadata = {
        row.id: row
        for row in session.execute(
            text(SEARCH_METADATA_SQL).bindparams(bindparam("ids", expanding=True)),
            {"inbox_id": inbox.id, "ids": [a.id for a in matches]},
        )
    }
    groups = {}
    members = {}
    for match in matches:
        row = metadata.get(match.id)
        if row is None:
            logger.warning(
                "search missing membership inbox_id=%s article_id=%s",
                inbox.id,
                match.id,
            )
            continue
        root_id = row.root_id if row.root_date is not None else None
        if root_id is None:
            logger.warning(
                "search unusable root inbox_id=%s article_id=%s root_id=%s",
                inbox.id,
                match.id,
                row.thread_root_id,
            )
        if root_id is None and row.message_date is None:
            logger.warning(
                "search unaddressable message inbox_id=%s article_id=%s",
                inbox.id,
                match.id,
            )
            continue
        # A fallback singleton must never absorb a different message's replies.
        key = ("root", root_id) if root_id is not None else ("message", match.id)
        if key not in groups:
            groups[key] = SearchGroup(
                match,
                root_id,
                coerce_dt(row.root_date if root_id is not None else row.message_date),
            )
            members[key] = []
        group = groups[key]
        group.count += 1
        group.authors.append(match.author)
        members[key].append(row)
    for key, group in groups.items():
        representative = metadata[group.representative.id]
        posting = _posting(representative)
        if posting is not None:
            group.version = representative.patch_series_version
            group.positions = sorted(
                {
                    row.patch_series_position
                    for row in members[key]
                    if _posting(row) == posting
                    and row.patch_series_position
                    != representative.patch_series_position
                }
            )
    return list(groups.values())
