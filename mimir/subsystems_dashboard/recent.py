"""Recent versioned postings, bounded after grouping and before previews."""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import bindparam, select, text

from mimir import cache
from mimir.canonical import fallback_canonical_name
from mimir.datetime_utils import coerce_dt
from mimir.models import ArticleList, Inbox
from mimir.patch_series import parse_cover_letter, same_series_sql, series_head_sql
from mimir.patch_subjects import _is_patch_subject, _is_reply_to_patch
from mimir.subsystems import SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC
from mimir.subsystems_dashboard._path_filter import _subsystem_path_filter_sql

RECENT_POSTINGS_LIMIT = 30
RECENT_PREVIEW_LIMIT = 30


@dataclass
class PostingArticle:
    article_id: int
    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    inbox_name: str | None
    position: int | None


@dataclass
class RecentPosting:
    representative: PostingArticle
    version: str | None
    matching_count: int
    children: list[PostingArticle]
    cover: PostingArticle | None = None

    @property
    def title(self):
        parsed = parse_cover_letter(self.cover.subject) if self.cover else None
        return parsed.title if parsed else self.representative.subject

    @property
    def status_id(self):
        if self.cover:
            return self.cover.article_id
        return None if self.children else self.representative.article_id


def _canonical_patch(subject, position, key):
    article = SimpleNamespace(
        subject=subject, patch_series_position=position, patch_series_key=key
    )
    return _is_patch_subject(subject) and not _is_reply_to_patch(article)


def recent_postings_sql(subsystem):
    """The actual group/member statement, also used by read-only measurements.

    The SQLite function calls the existing Python subject predicates, keeping
    reply rejection before LIMIT without a second mail parser. CASE gates
    parsing on indexed inbox membership: a plain join lets SQLite invoke the
    Python parser before rejecting another inbox's path-matched article.
    The shared UNION of path-index seeks remains the candidate driver.
    """
    path_filter = _subsystem_path_filter_sql(subsystem, prefix="rps")
    if path_filter is None:
        return None
    path_sql, params = path_filter
    head = series_head_sql("a")
    identity = "series_key, series_head, version, singleton"
    return text(f"""
        WITH candidates AS MATERIALIZED (
            SELECT a.id, a.date, a.patch_series_position AS position,
                   a.patch_series_key AS series_key,
                   {head} AS series_head,
                   a.patch_series_version AS version,
                   CASE WHEN a.patch_series_key IS NOT NULL
                              AND a.patch_series_version IS NOT NULL
                              AND a.patch_series_position > 0
                        THEN 0 ELSE a.id END AS singleton
              FROM articles a
             WHERE a.id IN ({path_sql})
               AND COALESCE(a.patch_series_position, 1) != 0
               AND CASE WHEN EXISTS (
                       SELECT 1 FROM article_lists al
                        WHERE al.article_id = a.id AND al.inbox_id = :inbox_id
                   ) THEN canonical_patch(a.subject, a.patch_series_position,
                                          a.patch_series_key)
                   ELSE 0 END
        ), ranked_groups AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY {identity} ORDER BY date DESC, id DESC
            ) AS group_rank FROM candidates
        ), selected AS MATERIALIZED (
            SELECT * FROM ranked_groups WHERE group_rank = 1
             ORDER BY date DESC, id DESC LIMIT :limit
        ), slots AS (
            SELECT g.id AS representative_id, g.date AS newest_date,
                   g.version, g.singleton, c.id, c.position,
                   ROW_NUMBER() OVER (
                       PARTITION BY g.id, c.position ORDER BY c.id
                   ) AS slot_rank
              FROM selected g JOIN candidates c
                ON c.singleton = g.singleton
               AND c.series_key IS g.series_key
               AND c.series_head = g.series_head
               AND c.version IS g.version
        ), preview AS (
            SELECT *, COUNT(*) OVER (PARTITION BY representative_id) AS matching_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY representative_id ORDER BY position, id
                   ) AS preview_rank
              FROM slots WHERE slot_rank = 1
        )
        SELECT p.*, a.message_id, a.subject, a.author, a.date,
               a.canonical_inbox_id
          FROM preview p JOIN articles a ON a.id = p.id
         WHERE preview_rank <= :preview_limit
         ORDER BY newest_date DESC, representative_id DESC, preview_rank
    """), params


def recent_postings_query(session, inbox, subsystem, limit=30, preview_limit=30):
    """Read the bounded dataset without cache writes or lifecycle work."""
    built = recent_postings_sql(subsystem)
    if built is None:
        return []
    if limit < 1 or preview_limit < 1:
        raise ValueError("Posting and preview limits must be positive")
    session.connection().connection.driver_connection.create_function(
        "canonical_patch", 3, _canonical_patch, deterministic=True
    )
    statement, params = built
    rows = session.execute(
        statement,
        {
            **params,
            "inbox_id": inbox.id,
            "limit": limit,
            "preview_limit": preview_limit,
        },
    ).all()
    if not rows:
        return []
    ids = list(dict.fromkeys(r.representative_id for r in rows))
    # Cover selection spans the full posting, not only matching paths/inboxes.
    covers = session.execute(
        text(f"""
        SELECT g.id AS representative_id, c.id, c.message_id, c.subject,
               c.author, c.date, c.canonical_inbox_id, c.patch_series_position AS position
          FROM articles g JOIN articles c ON c.id = (
              SELECT p.id FROM articles p
               WHERE {same_series_sql("p", "g")}
                 AND p.patch_series_version = g.patch_series_version
                 AND COALESCE(p.patch_series_position, 0) = 0
               ORDER BY NOT EXISTS (SELECT 1 FROM article_files f
                                     WHERE f.article_id = p.id), p.id
               LIMIT 1
          )
         WHERE g.id IN :ids AND g.patch_series_position > 0
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    # The latest matching copy may be a resend outside the first 30 positions.
    representatives = session.execute(
        text("""
        SELECT id, message_id, subject, author, date, canonical_inbox_id,
               patch_series_position AS position FROM articles WHERE id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    all_rows = [*rows, *covers, *representatives]
    links = defaultdict(list)
    for aid, iid, name in session.execute(
        select(ArticleList.article_id, Inbox.id, Inbox.name)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_({r.id for r in all_rows}))
    ):
        links[aid].append((iid, name))

    def article(row):
        return PostingArticle(
            row.id,
            row.message_id,
            row.subject,
            row.author,
            coerce_dt(row.date),
            fallback_canonical_name(row.canonical_inbox_id, links[row.id]),
            row.position,
        )

    representative_by_id = {r.id: article(r) for r in representatives}
    cover_by_id = {r.representative_id: article(r) for r in covers}
    postings = {}
    for row in rows:
        posting = postings.setdefault(
            row.representative_id,
            RecentPosting(
                representative_by_id[row.representative_id],
                row.version if not row.singleton else None,
                row.matching_count,
                [],
                cover_by_id.get(row.representative_id),
            ),
        )
        if not row.singleton:
            posting.children.append(article(row))
    return list(postings.values())


def pack_postings(postings):
    def article(row):
        return [
            row.article_id,
            row.message_id,
            row.subject,
            row.author,
            row.date.isoformat() if row.date else None,
            row.inbox_name,
            row.position,
        ]

    return [
        [
            article(p.representative),
            p.version,
            p.matching_count,
            [article(c) for c in p.children],
            article(p.cover) if p.cover else None,
        ]
        for p in postings
    ]


def unpack_postings(payload):
    def article(row):
        return PostingArticle(*row[:4], coerce_dt(row[4]), *row[5:])

    return [
        RecentPosting(
            article(p[0]),
            p[1],
            p[2],
            [article(c) for c in p[3]],
            article(p[4]) if p[4] else None,
        )
        for p in payload
    ]


def recent_postings_in_subsystem(
    session,
    inbox,
    subsystem,
    limit=RECENT_POSTINGS_LIMIT,
    preview_limit=RECENT_PREVIEW_LIMIT,
    *,
    force=False,
):
    payload = cache.get_or_compute(
        session,
        f"recent_postings_v1:{inbox.name}:{subsystem.id}:{limit}:{preview_limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        lambda: pack_postings(
            recent_postings_query(session, inbox, subsystem, limit, preview_limit)
        ),
        force=force,
    )
    return unpack_postings(payload)


def warm_postings(postings):
    """Best-effort web cache refusal must remain an observable warm failure."""
    if (
        len(json.dumps(pack_postings(postings), separators=(",", ":")))
        > cache.MAX_CACHE_VALUE_BYTES
    ):
        raise ValueError("Subsystem postings exceed cache value limit")
