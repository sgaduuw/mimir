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

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mimir.config import _resolve_tree_label, settings
from mimir.models import (
    Article,
    ArticleList,
    ArticleTrailer,
    MainlineCommit,
    MainlineState,
)
from mimir.patch_series import same_posting, same_series
from mimir.patches import article_has_files
from mimir.subsystems import article_paths


def _digest(values) -> str:
    """Short digest of NUL-joined strings, so {"ab"} and {"a", "b"}
    cannot collide."""
    return hashlib.blake2s("\0".join(values).encode(), digest_size=4).hexdigest()


def allowlist_tag() -> str:
    """The static half of the address allowlist, `Settings.email_allowlist`,
    as a validator input (#900). It is an env knob: changing it restarts
    the same image without moving `mimir.__version__`, and removing a
    token must reach every cache holding a page that showed the address.
    The MAINTAINERS half moves with `rules_version` below."""
    return _digest(settings.email_allowlist)


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
    # The landing rows themselves, not a summary of them. Since #673
    # one commit has a row per tree, and the page renders WHICH tree
    # (the pill), which sha and which date (tooltip and the "Queued in
    # X as <sha> on <date>" sentence, from the earliest non-Linus row).
    # A count-and-max tag missed {linux-next} -> {linus} at one sha and
    # date, and a tree list plus max still missed a rebased row below
    # the max. Same argument as `files_tag`: the truthful input is the
    # values. A handful of rows per article. The label is the one the
    # page prints ("Queued in <label>"): `TREES__<name>__DISPLAY_NAME`
    # changes on a restart that keeps the version.
    #
    # A cover's pill rolls up every patch of its posting (#938), so for a
    # cover the landings and the trailers are the posting's, in the same
    # two statements. A re-post's rows come along, which only over-
    # invalidates: the pill counts the slot's patch.
    article = session.get(Article, article_id)
    posting = (
        select(Article.id, Article.message_id)
        .where(same_posting(article), Article.patch_series_position > 0)
        .subquery()
        if article is not None
        and article.patch_series_key
        and article.patch_series_version
        and not article.patch_series_position
        else None
    )
    landed = MainlineCommit.message_id == message_id
    reviewed = ArticleTrailer.article_id == article_id
    if posting is not None:
        landed = or_(
            landed, MainlineCommit.message_id.in_(select(posting.c.message_id))
        )
        reviewed = or_(reviewed, ArticleTrailer.article_id.in_(select(posting.c.id)))
    landing_rows = session.execute(
        select(
            MainlineCommit.tree_name,
            MainlineCommit.commit_sha,
            MainlineCommit.committed_at,
        ).where(landed)
    ).all()
    landings_tag = ";".join(
        sorted(
            f"{r.tree_name},{_resolve_tree_label(r.tree_name)},"
            f"{r.commit_sha},{r.committed_at.isoformat()}"
            for r in landing_rows
        )
    )
    # Trailer values, not a count: `backfill-article-trailers
    # --reprocess` can rewrite a name or address at the same count, and
    # the pill shows names (tooltip) and maintainer matches by address.
    # In id order, which is the tooltip's order.
    trailer_rows = session.execute(
        select(
            ArticleTrailer.role,
            ArticleTrailer.name,
            ArticleTrailer.address_normalized,
        )
        .where(reviewed)
        .order_by(ArticleTrailer.id)
    ).all()
    trailers_tag = _digest(repr(tuple(r)) for r in trailer_rows)
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
    # themselves are the truthful input, read through the same
    # `article_paths` the page renders from: for a cover that is every
    # patch of its posting, with the position, since the series totals
    # count patches as well as files (#844), and the line counts they
    # add (#939). `get` is the route's own loaded row, so it costs no
    # statement.
    files_tag = _digest(repr(row) for row in article_paths(session, article))
    # Supersedance and the revision count come from SIBLING articles
    # in this patch's series (key and head), so they live in none of the reads
    # above. Posting a v2 flips this article's badge to SUPERSEDED and
    # rewrites its synthesis prose to "revision 1 of 2", without adding
    # a message to its thread. Posting a v2 is the most routine patch
    # workflow on the list, and "is this the current revision" is
    # squarely in the query family these surfaces exist to answer.
    series = session.execute(
        select(
            Article.patch_series_key,
            Article.patch_series_position,
            Article.patch_series_head_id,
        ).where(Article.id == article_id)
    ).one_or_none()
    # The revisions' values, not a count and a text max: the pill ranks
    # by revision number, and `backfill-patch-series --reprocess` can
    # renumber a version in place at the same count.
    series_tag = ""
    if series is not None:
        # A keyless row's own columns still decide the patch state card
        # (#838), and a reprocess can clear its position alone.
        series_tag = repr(tuple(series))
    if series is not None and series[0]:
        # Plus each revision's canonical inbox and memberships: the fold
        # links every revision (and the diff's `to` slot) through
        # `_canonical_url_for`, so a sibling cross-posted later moves a
        # link without moving anything else here (#687). Plus whether
        # each has a diff: a slot's patch is its lowest-id row with one
        # (`patch_series.slot_rank`, #861), so a backfill that gives a
        # row its diff moves the fold, the diff link and the state card.
        revision_filter = [same_series(series[0], series[2])]
        if posting is None:
            revision_filter.append(
                func.coalesce(Article.patch_series_position, 0) == (series[1] or 0)
            )
        # else: a cover's pill rolls up its posting (#938) and reads each
        # patch's own newer revision, so a cover takes every position of
        # the series, not only its own.
        revisions = session.execute(
            select(
                Article.id,
                Article.patch_series_version,
                Article.canonical_inbox_id,
                ArticleList.inbox_id,
                article_has_files(),
            )
            .outerjoin(ArticleList, ArticleList.article_id == Article.id)
            .where(*revision_filter)
            .order_by(Article.id, ArticleList.inbox_id)
        ).all()
        # The identity leads, because the fold's diff links spell it
        # (`s=`, #816): the chain backfill can move a whole series under
        # a new head, which keeps the same revision rows and changes
        # every link.
        series_tag = _digest(repr(tuple(r)) for r in [series, *revisions])
    return (
        f"{landings_tag}|{trailers_tag}|{rules_version or ''}|{series_tag}|{files_tag}|"
        f"{allowlist_tag()}"
    )
