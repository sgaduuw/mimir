"""Historical re-walk to record per-inbox address observations and
resolve `canonical_inbox_id` from each article's original To/Cc
headers.

Idempotent + resumable: by default skips articles that already have
a canonical set. Mid-walk re-runs `_maybe_promote_list_address`
periodically so that auto-promotion fires after the first ~50 newest
messages per inbox accumulate observations, and the bulk of the
walk resolves canonicals against a settled list_address map.

Used by the `admin canonicals backfill` CLI command.

Walks `articles` by id-desc rather than date-desc as the patch-
metadata backfills do (see `mimir._backfill.walk_articles`). The
practical effect on production data is negligible (articles are
ingested in commit order so id-desc ≈ date-desc); the win is a
clean integer cursor for broker-side cooperative scheduling.
NULL-date rows, which `nullslast()` used to bucket at the very end,
now fall in their natural id position. They were rare and carry
no list-address signal anyway.
"""
import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.canonical import extract_list_addresses, pick_canonical_inbox_id
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.extensions import SessionLocal, write_transaction
from mimir.ingest.epoch import (
    _flush_observations,
    _maybe_promote_list_address,
)
from mimir.models import Article, ArticleList, Inbox
from mimir.store import MessageNotFound, read_message

logger = logging.getLogger(__name__)


# Batch size for the id-cursor pagination. Matches `walk_articles`.
_BATCH = 1000


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_canonicals`. Each examined article
    lands in exactly one of resolved/unresolved/skipped (resolved =
    `canonical_inbox_id` was set or updated; unresolved = parsed cleanly
    but no list address matched; skipped = blob couldn't be read).

    `partial` + `continuation` carry the cooperative-scheduling handoff
    between broker chunks (Phase 2.2). Direct (non-broker) callers
    always see `partial=False, continuation=None`."""
    examined: int = 0
    resolved: int = 0
    unresolved: int = 0
    skipped: int = 0
    partial: bool = False
    continuation: int | None = None

    def merge(self, other: "BackfillResult") -> "BackfillResult":
        """Sum counters with `other`, carrying `other`'s
        `partial`/`continuation` forward."""
        return BackfillResult(
            examined=self.examined + other.examined,
            resolved=self.resolved + other.resolved,
            unresolved=self.unresolved + other.unresolved,
            skipped=self.skipped + other.skipped,
            partial=other.partial,
            continuation=other.continuation,
        )


def backfill_canonicals(
    inbox_filter: str | None = None,
    limit: int | None = None,
    reprocess: bool = False,
    promote_every: int = 200,
    progress_every: int = 1000,
    progress: callable = None,  # type: ignore[valid-type]
    *,
    max_seconds: float | None = None,
    start_cursor: int | None = None,
) -> BackfillResult:
    """Walk historical articles newest-first (id-desc), record per-
    inbox address observations and resolve `canonical_inbox_id` from
    the original To/Cc headers. Idempotent + resumable: by default
    skips articles that already have a canonical set (the
    `WHERE canonical_inbox_id IS NULL` filter naturally picks up where
    a previous run left off).

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

    `max_seconds` + `start_cursor` enable broker-side cooperative
    scheduling (Phase 2.2). When `max_seconds` is set, the call returns
    `partial=True, continuation=<last Article.id>` after the deadline
    elapses; the broker handler relays that to its caller which loops
    with a follow-up RPC carrying `start_cursor=<last>`. Direct
    callers leave both None and get the historical full-walk behaviour.
    """
    out = BackfillResult()
    address_to_inbox_id: dict[str, int] = {}
    demoted_inbox_ids: frozenset[int] = frozenset()
    pending_obs: dict[int, dict[str, tuple[int, datetime]]] = {}
    inbox_cache: dict[int, Inbox] = {}

    def refresh_address_map(session: Session) -> None:
        nonlocal address_to_inbox_id, demoted_inbox_ids
        address_to_inbox_id = dict(session.execute(
            select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
        ).all())
        demoted_inbox_ids = frozenset(session.execute(
            select(Inbox.id).where(Inbox.name.in_(settings.canonical_demoted_inboxes))
        ).scalars())

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

    # BEGIN IMMEDIATE for every transaction in this block, avoids
    # SQLITE_BUSY_SNAPSHOT when gunicorn cache.set commits a write
    # between the backfill's read of the next article batch and its
    # write of canonical_inbox_id / inbox_address_observations.
    label = (
        f"backfill_canonicals:{inbox_filter}"
        if inbox_filter else "backfill_canonicals"
    )
    deadline: float | None = (
        time.monotonic() + max_seconds if max_seconds is not None else None
    )
    last_processed: int | None = None

    with write_transaction(label), SessionLocal() as session:
        refresh_address_map(session)

        # Batched id-cursor pagination. id-desc rather than date-desc
        # so the continuation cursor is a single integer; production
        # ingest-order is date-monotonic so this approximates the
        # "newest first" semantic within rounding error.
        cursor: int | None = start_cursor
        examined_total = 0

        while True:
            q = select(Article).order_by(Article.id.desc()).limit(_BATCH)
            if not reprocess:
                q = q.where(Article.canonical_inbox_id.is_(None))
            if inbox_filter is not None:
                q = q.join(ArticleList, ArticleList.article_id == Article.id) \
                     .join(Inbox, Inbox.id == ArticleList.inbox_id) \
                     .where(Inbox.name == inbox_filter)
            if cursor is not None:
                q = q.where(Article.id < cursor)
            batch = list(session.execute(q).scalars())
            if not batch:
                break

            limit_reached = False
            for article in batch:
                cursor = article.id
                examined_total += 1
                if limit is not None and examined_total > limit:
                    limit_reached = True
                    break
                out.examined += 1

                # Pick a linked inbox to read the blob from. Cross-posts
                # have one ArticleList row per inbox, all pointing at
                # the same logical message; first try the lowest-id
                # inbox.
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
                    last_processed = article.id
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

                new_canonical = pick_canonical_inbox_id(
                    list_addrs, address_to_inbox_id, demoted_inbox_ids,
                )
                if new_canonical != article.canonical_inbox_id:
                    article.canonical_inbox_id = new_canonical
                    out.resolved += 1
                else:
                    out.unresolved += 1

                last_processed = article.id

                if out.examined % promote_every == 0:
                    flush_pending(session)
                    session.commit()
                    if maybe_promote_all(session):
                        refresh_address_map(session)
                        session.commit()

                if progress is not None and out.examined % progress_every == 0:
                    progress(out)

            # Batch boundary: flush any in-flight per-batch state and
            # commit so partial progress survives a time-budget exit.
            flush_pending(session)
            session.commit()

            if limit_reached:
                break

            # Cooperative-scheduling deadline. Checked after the commit
            # so the continuation cursor points at fully-persisted
            # work.
            if deadline is not None and time.monotonic() >= deadline:
                out.partial = True
                out.continuation = last_processed
                return out

        # Walk completed naturally (no time budget hit). One final
        # promotion sweep mirrors the pre-2.2 behaviour.
        flush_pending(session)
        maybe_promote_all(session)
        session.commit()

    return out
