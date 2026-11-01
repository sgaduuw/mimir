"""The per-message metadata shown from the thread's disclosures."""

import hashlib
import logging

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

import mimir
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.models import Article, ArticleFile, ArticleList
from mimir.patch_state import patch_state_for_article
from mimir.related import is_bot_sender, related_discussions
from mimir.subsystems import related_patches, subsystems_for_article
from mimir.threading import (
    ThreadNode,
    dedupe_thread,
    find_thread_root,
    get_thread,
    thread_aggregates,
    thread_is_materialised,
    thread_window,
)
from mimir.web.routes._validators import render_state_tag

logger = logging.getLogger(__name__)


def message_metadata(session, inbox, article):
    """Load metadata only when requested, never once per thread card.

    Related lists keep their existing cached semantics. The response has no
    HTTP cache; state cards use a fresh state/thread digest so their own
    cache cannot hide a landing, attribution or thread change.
    """
    root_msgid = (
        find_thread_root(session, inbox, article.message_id) or article.message_id
    )
    root = session.scalar(select(Article).where(Article.message_id == root_msgid))
    rooted = thread_is_materialised(session, inbox.id, root.id)
    agg = thread_aggregates(session, inbox, root.id) if rooted else None
    window = thread_window(session, inbox, root, article) if agg is not None else None
    thread = (
        window.rows
        if window is not None
        else dedupe_thread(get_thread(session, inbox, root_msgid))
    )
    thread_nodes = [n for n in thread if isinstance(n, ThreadNode)]
    thread_ids = {n.id for n in thread_nodes}
    thread_max_date = (
        agg.last_activity
        if agg is not None
        else max((n.date for n in thread_nodes if n.date), default=article.date)
    )
    parent_off_list = None
    if root.thread_parent:
        parent_in_inbox = session.scalar(
            select(Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.message_id == root.thread_parent,
            )
        )
        if parent_in_inbox is None:
            parent_off_list = root.thread_parent
    # Non-patch gate for the related-discussions panel (#71):
    # a thread counts as a patch thread when ANY of its articles
    # touches files. Rides the (article_id, path) PK. A window shows
    # part of the thread, so it asks the column about all of it.
    if window is not None:
        patch_probe = (
            select(ArticleFile.article_id)
            .join(ArticleList, ArticleList.article_id == ArticleFile.article_id)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.thread_root_id == root.id,
            )
        )
    else:
        patch_probe = select(ArticleFile.article_id).where(
            ArticleFile.article_id.in_(list(thread_ids))
        )
    is_patch_thread = session.execute(patch_probe.limit(1)).first() is not None

    # A bot-authored root (syzbot, kernel test robot, tip-bot)
    # shows no panel: the non-patch population is dominated by
    # bot reports whose related matches are low-value (#71).
    # Gated before compute like is_patch_thread so suppressed
    # roots don't enter the empty-rate metric.
    is_bot_root = is_bot_sender(root.author)

    related_threads: list = []
    if not is_patch_thread and not is_bot_root:
        # Best-effort: a panel bug must never 500 a message view.
        # ERROR (not warning) so a broken scorer stays loud.
        try:
            related_threads = related_discussions(
                session,
                inbox,
                root_id=root.id,
                # A window's ids are enough: candidates are filtered
                # by these only after the SQL limit, and any from the
                # rest of the thread collapse onto `root_id`, which
                # the panel drops.
                thread_article_ids=thread_ids,
                thread_authors=(
                    set(agg.authors)
                    if window is not None
                    else {n.author for n in thread_nodes if n.author}
                ),
            )
        except Exception:
            logger.exception(
                "related-discussions failed for %s/%s",
                inbox.name,
                article.id,
            )

    # Same-subject orphans within this inbox.
    related: list[Article] = []
    if parent_off_list and article.subject_normalized and is_patch_thread:
        # Excluded through the column for a window, which shows only
        # part of the thread; an IN list of every member's
        # Message-ID was also one bound parameter per message.
        # NOT EXISTS on its own alias, not a second predicate on the
        # joined row: that one drives the plan from
        # `ix_article_lists_inbox_id`, the whole inbox, instead of
        # the subject index. The alias also stops `exists()`
        # correlating to the outer `article_lists`.
        member = aliased(ArticleList)
        not_in_thread = (
            ~exists().where(
                member.article_id == Article.id,
                member.inbox_id == inbox.id,
                member.thread_root_id == root.id,
            )
            if window is not None
            else Article.message_id.not_in({n.message_id for n in thread_nodes})
        )
        related = list(
            session.execute(
                select(Article)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    ArticleList.inbox_id == inbox.id,
                    Article.subject_normalized == article.subject_normalized,
                    not_in_thread,
                )
                .order_by(Article.date.desc().nulls_last())
                .limit(5)
            ).scalars()
        )

    subsystem_hits = subsystems_for_article(session, article)
    state = render_state_tag(session, article.id, article.message_id)
    state_key = hashlib.blake2s(
        f"{mimir.__version__}|{inbox.id}|{state}|{thread_max_date}|{agg.total if agg else len(thread_nodes)}".encode(),
        digest_size=8,
    ).hexdigest()
    patch_state = patch_state_for_article(
        session,
        article,
        thread_dates=[thread_max_date],
        subsystem_ids=[s.id for s in subsystem_hits],
        inbox_name=inbox.name,
        state_key=state_key,
    )
    lifecycle_status = lifecycle_status_for_articles(
        session,
        [article.id],
        inbox.id,
        state_key=state_key,
    ).get(article.id)
    return dict(
        article=article,
        current_inbox=inbox.name,
        inbox_name=inbox.name,
        subsystem_hits=subsystem_hits,
        patch_state=patch_state,
        lifecycle_status=lifecycle_status,
        related=related,
        related_threads=related_threads,
        related_patches=related_patches(session, article.id),
    )
