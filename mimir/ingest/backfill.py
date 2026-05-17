"""Historical re-walk to record per-inbox address observations and
resolve `canonical_inbox_id` from each article's original To/Cc
headers.

Idempotent + resumable: by default skips articles that already have
a canonical set. Mid-walk re-runs `_maybe_promote_list_address`
periodically so that auto-promotion fires after the first ~50 newest
messages per inbox accumulate observations, and the bulk of the
walk resolves canonicals against a settled list_address map.

Used by the `admin canonicals backfill` CLI command.
"""
import logging
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.canonical import extract_list_addresses, pick_canonical_inbox_id
from mimir.datetime_utils import aware_utc
from mimir.extensions import SessionLocal
from mimir.ingest.epoch import (
    _flush_observations,
    _maybe_promote_list_address,
)
from mimir.models import Article, ArticleList, Inbox
from mimir.store import MessageNotFound, read_message

logger = logging.getLogger(__name__)


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_canonicals`. Each examined article
    lands in exactly one of resolved/unresolved/skipped (resolved =
    `canonical_inbox_id` was set or updated; unresolved = parsed cleanly
    but no list address matched; skipped = blob couldn't be read)."""
    examined: int = 0
    resolved: int = 0
    unresolved: int = 0
    skipped: int = 0


def backfill_canonicals(
    inbox_filter: str | None = None,
    limit: int | None = None,
    reprocess: bool = False,
    promote_every: int = 200,
    progress_every: int = 1000,
    progress: callable = None,  # type: ignore[valid-type]
) -> BackfillResult:
    """Walk historical articles newest-first, record per-inbox address
    observations and resolve `canonical_inbox_id` from the original
    To/Cc headers. Idempotent + resumable: by default skips articles
    that already have a canonical set (the `WHERE canonical_inbox_id IS
    NULL` filter naturally picks up where a previous run left off).

    Mid-walk, every `promote_every` articles we re-run
    `_maybe_promote_list_address` for inboxes still at NULL, that way
    auto-promotion fires early in the pass (after the first ~50 newest
    messages per inbox accumulate observations) and the bulk of the
    walk resolves canonicals against a settled list_address map.

    `--reprocess` re-examines articles whose canonical is already set
    (use after operator updates a list_address or when chasing the
    bootstrap region from an earlier pass). `--inbox` restricts the
    walk to articles linked to a single inbox. `--limit` caps the
    session for "do an hour's worth tonight."
    """
    out = BackfillResult()
    address_to_inbox_id: dict[str, int] = {}
    pending_obs: dict[int, dict[str, tuple[int, datetime]]] = {}
    inbox_cache: dict[int, Inbox] = {}

    def refresh_address_map(session: Session) -> None:
        nonlocal address_to_inbox_id
        address_to_inbox_id = dict(session.execute(
            select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
        ).all())

    def get_inbox(session: Session, inbox_id: int) -> Inbox:
        ix = inbox_cache.get(inbox_id)
        if ix is None:
            ix = session.get(Inbox, inbox_id)
            if ix is None:
                raise RuntimeError(f"missing inbox row for id={inbox_id}")
            inbox_cache[inbox_id] = ix
        return ix

    def flush_pending(session: Session) -> None:
        for inbox_id, obs in pending_obs.items():
            if obs:
                _flush_observations(session, inbox_id, obs)
        pending_obs.clear()

    def maybe_promote_all(session: Session) -> bool:
        """Run promotion for every inbox that's still NULL. Returns True
        if any inbox got promoted (caller refreshes the address map)."""
        promoted = False
        for ix in session.execute(
            select(Inbox).where(Inbox.list_address.is_(None))
        ).scalars():
            if _maybe_promote_list_address(session, ix.id) is not None:
                promoted = True
        return promoted

    with SessionLocal() as session:
        refresh_address_map(session)

        # Build the article query. Newest-first so the most-indexed
        # articles get correct canonicals first AND auto-promotion has
        # the freshest observations to work with.
        q = select(Article).order_by(Article.date.desc().nullslast())
        if not reprocess:
            q = q.where(Article.canonical_inbox_id.is_(None))
        if inbox_filter is not None:
            q = q.join(ArticleList, ArticleList.article_id == Article.id) \
                 .join(Inbox, Inbox.id == ArticleList.inbox_id) \
                 .where(Inbox.name == inbox_filter)
        if limit is not None:
            q = q.limit(limit)

        # Stream the eligible-articles result rather than materialising
        # all rows. `--reprocess` and the unfiltered-inbox path can hit
        # the full 6M-row prod corpus; materialising peaks at multi-GB.
        # `yield_per` rides on top of the existing `promote_every`
        # commit cadence; commits expire the iterated articles, which
        # we never re-touch (each iteration only reads the freshly-
        # yielded one).
        articles = session.execute(
            q.execution_options(stream_results=True, yield_per=1000)
        ).scalars()
        for article in articles:
            out.examined += 1

            # Pick a linked inbox to read the blob from. Cross-posts
            # have one ArticleList row per inbox, all pointing at the
            # same logical message; first try the lowest-id inbox.
            links = list(session.execute(
                select(ArticleList.inbox_id)
                .where(ArticleList.article_id == article.id)
                .order_by(ArticleList.inbox_id)
            ).scalars())

            parsed = None
            for inbox_id in links:
                ix = get_inbox(session, inbox_id)
                try:
                    parsed = read_message(session, ix, article.message_id)
                    break
                except MessageNotFound as exc:
                    logger.debug(
                        "backfill: %s blob not in %s: %s",
                        article.message_id, ix.name, exc,
                    )
                    continue
                except Exception as exc:
                    logger.warning(
                        "backfill: parse failed for %s in %s: %r",
                        article.message_id, ix.name, exc,
                    )
                    continue
            if parsed is None:
                out.skipped += 1
                continue

            list_addrs = extract_list_addresses(parsed.headers)
            if list_addrs:
                obs_time = aware_utc(
                    parsed.date or article.date or datetime.now(timezone.utc)
                )
                for inbox_id in links:
                    bucket = pending_obs.setdefault(inbox_id, {})
                    for addr in list_addrs:
                        prev = bucket.get(addr)
                        if prev is None:
                            bucket[addr] = (1, obs_time)
                        else:
                            cnt, ts = prev
                            bucket[addr] = (cnt + 1, max(ts, obs_time))

            new_canonical = pick_canonical_inbox_id(list_addrs, address_to_inbox_id)
            if new_canonical != article.canonical_inbox_id:
                article.canonical_inbox_id = new_canonical
                out.resolved += 1
            else:
                out.unresolved += 1

            if out.examined % promote_every == 0:
                flush_pending(session)
                session.commit()
                if maybe_promote_all(session):
                    refresh_address_map(session)
                    session.commit()

            if progress is not None and out.examined % progress_every == 0:
                progress(out)

        flush_pending(session)
        maybe_promote_all(session)
        session.commit()

    return out
