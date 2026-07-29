"""Validator inputs for the DERIVED state a rendered page shows about
an article: the state that lives in neither the message body nor the
thread's shape, and so moves without touching any input the page's
other ETag components cover.

Both the message page and the thread view render the same four
derived surfaces (subsystem attribution, lifecycle badge, review
roll-up, revision/supersedance line), and both are served
`public, no-cache`, so a validator that misses one of them leaves the
edge and every crawler holding a page whose badges the source data
has already contradicted. This module is that validator, shared, so
the two surfaces cannot drift apart again.
"""

import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir.models import (
    Article,
    ArticleFile,
    ArticleTrailer,
    MainlineCommit,
    MainlineState,
)


def render_state_tag(session: Session, article_id: int, message_id: str) -> str:
    """Everything a page renders ABOUT `article_id` that no message
    carries and no thread-shape signal covers.

    Indexed reads rather than a `lifecycle_status_for_articles` probe,
    for two reasons. This has to run BEFORE the 304 short-circuit (it
    feeds the validator), and that helper computes a recursive CTE and
    then WRITES a cache row, which in the web tier is a broker RPC: the
    cheap path would have acquired a query, a CTE and a write on the
    single-writer broker, on exactly the URLs the sitemap aims crawlers
    at. And it only covers lifecycle, while these pages also render the
    subsystem line, which moves on any `update-mainline` MAINTAINERS
    reparse without touching a node count or a thread's max date.

    Cost, measured 2026-07-29 against the production corpus (17,334,085
    articles), sampling 200 patch-shaped articles (the expensive shape:
    they carry `article_files` rows and trigger the sixth, series,
    query): **0.752 ms per call**. Every read is an index seek, so this
    is B-tree-depth-bound rather than corpus-size-bound, but re-measure
    rather than trusting the figure; the archive only grows. It is paid
    on every request to both surfaces, including the 304s, which is the
    crawler path.
    """
    landings = session.execute(
        select(
            func.count(MainlineCommit.commit_sha),
            func.max(MainlineCommit.committed_at),
        ).where(MainlineCommit.message_id == message_id)
    ).one()
    trailers = session.scalar(
        select(func.count(ArticleTrailer.id)).where(
            ArticleTrailer.article_id == article_id
        )
    )
    # `MainlineState.last_commit_sha` is the object id of the
    # MAINTAINERS BLOB at the last load, so it versions the whole
    # subsystem-RULE snapshot in one scalar instead of re-deriving this
    # article's matches. Load-bearing that it is the blob and not the
    # tree HEAD: HEAD moves on every push to Linus's tree, which would
    # rotate the validator of every page in the archive several times a
    # day for a rendered output that is almost always identical. See
    # `MainlineState` and `mainline.load_maintainers`.
    rules_version = session.scalar(
        select(MainlineState.last_commit_sha).where(MainlineState.tree_name == "linus")
    )
    # The rules are only half of the subsystem attribution: it is rules
    # x `article_files`, so the ARTICLE side needs its own input. Not a
    # `count(*)`: `backfill-article-files --reprocess` (or a re-ingest
    # after a parser fix) can rewrite a path in place, which keeps the
    # count and changes which subsystems claim the article. The paths
    # themselves are the truthful input, and they cost the same to read
    # (a range scan on the `(article_id, path)` PK, ~3 rows per article,
    # already ordered by that PK so the ORDER BY is free). NUL-joined so
    # {"ab"} and {"a", "b"} cannot collide.
    paths = session.scalars(
        select(ArticleFile.path)
        .where(ArticleFile.article_id == article_id)
        .order_by(ArticleFile.path)
    ).all()
    files_tag = hashlib.blake2s("\0".join(paths).encode(), digest_size=4).hexdigest()
    # Supersedance and the revision count come from SIBLING articles
    # sharing this patch's series key, so they live in none of the reads
    # above. Posting a v2 flips this article's badge to SUPERSEDED and
    # rewrites its synthesis prose to "revision 1 of 2", without adding
    # a message to its thread. Posting a v2 is the most routine patch
    # workflow on the list, and "is this the current revision" is
    # squarely in the query family these surfaces exist to answer.
    series = session.execute(
        select(Article.patch_series_key, Article.patch_series_position).where(
            Article.id == article_id
        )
    ).one_or_none()
    series_tag = ""
    if series is not None and series[0]:
        count, newest = session.execute(
            select(
                func.count(Article.id),
                func.max(Article.patch_series_version),
            ).where(
                Article.patch_series_key == series[0],
                func.coalesce(Article.patch_series_position, 0) == (series[1] or 0),
            )
        ).one()
        series_tag = f"{count}|{newest or ''}"
    return (
        f"{landings[0]}|{landings[1] or ''}|{trailers or 0}|"
        f"{rules_version or ''}|{series_tag}|{files_tag}"
    )
