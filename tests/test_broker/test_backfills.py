"""Phase 2.2 backfill RPCs with cooperative scheduling.

Each of the four backfills routes through the broker as a chain of
chunked RPCs: the handler runs for at most
`Settings.broker_backfill_chunk_seconds` (default 10 s) before
returning `partial=True, continuation=<last id>`; the CLI loops with
a follow-up RPC until `partial=False`. This file covers:

- `walk_articles` time-budget mechanics (partial/continuation
  surface, single chunk inside the budget completes, exceeding the
  budget bails at the next batch boundary).
- The `BackfillResult.merge` shape on each of the four backfill
  flavours (counter sums; carry-forward of `other`'s partial /
  continuation).
- The broker handler returns `Reply.result = {counters, partial,
  continuation}` and the `BrokerClient.backfill_*` methods
  reconstruct a typed `BackfillResult` from it.
- The `_broker_backfill_loop` helper aggregates a multi-chunk
  result and converges on `partial=False` at the natural end.

The patch-series backfill is the cheapest (no blob fetch, only
subject/author/thread_parent reads) so it's the main vehicle for
the dispatch / merge / loop tests; the other three are covered
structurally by the protocol + handler registration smoke and the
typed-result reconstruction on the client.
"""

from __future__ import annotations

from sqlalchemy import select

from mimir.broker.handlers import dispatch
from mimir.broker.protocol import (
    BackfillArticleFilesRequest,
    BackfillArticleTrailersRequest,
    BackfillCanonicalsRequest,
    BackfillPatchSeriesRequest,
)
from mimir.extensions import SessionLocal
from mimir.models import Article


def _line(req) -> bytes:
    return req.model_dump_json().encode("utf-8")


def _seed_extra_articles(n: int) -> list[int]:
    """Add `n` articles to the seeded DB with `[PATCH 0/N]` subjects
    so the patch-series walker has cover-letter candidates to chew
    on. Returns the new article IDs in insertion order."""
    from datetime import datetime, timezone

    from mimir.models import ArticleList, Inbox

    ids: list[int] = []
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i in range(n):
            art = Article(
                message_id=f"coop-{i}@example.com",
                subject=f"[PATCH 0/{n}] coop test {i}",
                author=f"Author{i} <a{i}@example.com>",
                date=datetime(2025, 1, 1 + i, 12, 0, tzinfo=timezone.utc),
                thread_parent=None,
                subject_normalized=f"coop test {i}",
            )
            s.add(art)
            s.flush()
            ids.append(art.id)
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha=f"{i:040d}",
                )
            )
        s.commit()
    return ids


# ----- walk_articles time budget -----------------------------------------


def test_walk_articles_returns_partial_when_deadline_expires(seeded_db, broker_active):
    """`max_seconds=0` makes the deadline fire at the first batch
    boundary; the walker returns `(partial=True, continuation=<last
    id processed>)` after committing what it touched. Use
    `batch_size=2` so the seeded 4-article corpus splits into two
    batches and we can prove the walker stopped at the FIRST
    boundary (not after processing everything).

    Phase 3c: `process_one` returns a `(bucket, payload)` tuple;
    `flush_batch` is required. A no-op `flush_batch` is sufficient
    here since we're testing the time-budget mechanics, not writes."""
    from concurrent.futures import Future

    from mimir._backfill import walk_articles
    from mimir.patches import BackfillResult

    result = BackfillResult()

    def process(session, article, reprocess):
        return ("no_diff", None)

    def flush_batch(writer, payloads):
        f = Future()
        f.set_result(None)
        return f

    partial, cont = walk_articles(
        result,
        process,
        flush_batch,
        batch_size=2,
        label="test_walk_partial",
        max_seconds=0.0,
    )
    assert partial is True
    assert cont is not None
    # We processed exactly one batch (2 articles) and bailed.
    assert result.examined == 2


def test_walk_articles_returns_full_completion_without_deadline(
    seeded_db, broker_active
):
    """No `max_seconds` set: the walker runs to natural end and
    returns `(partial=False, continuation=None)`.

    Phase 3c: `process_one` returns a `(bucket, payload)` tuple;
    `flush_batch` is required."""
    from concurrent.futures import Future

    from mimir._backfill import walk_articles
    from mimir.patches import BackfillResult

    result = BackfillResult()

    def flush_batch(writer, payloads):
        f = Future()
        f.set_result(None)
        return f

    partial, cont = walk_articles(
        result,
        lambda _s, _a, _r: ("no_diff", None),
        flush_batch,
        batch_size=2,
        label="test_walk_full",
    )
    assert partial is False
    assert cont is None
    assert result.examined == 4  # all seeded articles


def test_walk_articles_resumes_from_start_cursor(seeded_db, broker_active):
    """`start_cursor=N` makes the walker pick up at `Article.id < N`
    on its first batch. Pair with `max_seconds=0` to confirm chunked
    resume reaches every article across two chunks.

    Phase 3c: `process_one` returns a `(bucket, payload)` tuple;
    `flush_batch` is required."""
    from concurrent.futures import Future

    from mimir._backfill import walk_articles
    from mimir.patches import BackfillResult

    def flush_batch(writer, payloads):
        f = Future()
        f.set_result(None)
        return f

    # Chunk 1: deadline=0 forces a one-batch bail.
    result1 = BackfillResult()
    p1, c1 = walk_articles(
        result1,
        lambda _s, _a, _r: ("no_diff", None),
        flush_batch,
        batch_size=2,
        label="test_resume_1",
        max_seconds=0.0,
    )
    assert p1 is True and c1 is not None

    # Chunk 2: continue from where chunk 1 stopped. No deadline this
    # time, so we run to the end. Across the two chunks every
    # seeded article is examined exactly once.
    result2 = BackfillResult()
    p2, c2 = walk_articles(
        result2,
        lambda _s, _a, _r: ("no_diff", None),
        flush_batch,
        batch_size=2,
        label="test_resume_2",
        start_cursor=c1,
    )
    assert p2 is False and c2 is None
    assert result1.examined + result2.examined == 4


# ----- BackfillResult.merge ---------------------------------------------


def test_patches_backfill_result_merge_sums_counters():
    from mimir.patches import BackfillResult

    a = BackfillResult(examined=10, indexed=5, no_diff=3, skipped=1, failed=1)
    b = BackfillResult(
        examined=20,
        indexed=10,
        no_diff=5,
        skipped=4,
        failed=1,
        partial=True,
        continuation=99,
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.indexed == 15
    assert m.no_diff == 8
    assert m.skipped == 5
    assert m.failed == 2
    # partial / continuation carry the LATEST chunk's values forward,
    # not summed.
    assert m.partial is True
    assert m.continuation == 99


def test_trailers_backfill_result_merge_sums_counters():
    from mimir.trailers import BackfillResult

    a = BackfillResult(examined=10, indexed=5, no_trailers=4, skipped=1)
    b = BackfillResult(
        examined=20,
        indexed=8,
        no_trailers=10,
        skipped=2,
        partial=False,
        continuation=None,
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.indexed == 13
    assert m.no_trailers == 14
    assert m.skipped == 3
    assert m.partial is False
    assert m.continuation is None


def test_patch_series_backfill_result_merge_sums_counters():
    from mimir.patch_series import BackfillResult

    a = BackfillResult(
        examined=5,
        indexed=2,
        in_series_indexed=1,
        in_series_orphan=0,
        not_cover=2,
        skipped=0,
    )
    b = BackfillResult(
        examined=5,
        indexed=1,
        in_series_indexed=2,
        in_series_orphan=1,
        not_cover=1,
        skipped=0,
        partial=True,
        continuation=42,
    )
    m = a.merge(b)
    assert m.examined == 10
    assert m.indexed == 3
    assert m.in_series_indexed == 3
    assert m.in_series_orphan == 1
    assert m.not_cover == 3
    assert m.continuation == 42
    assert m.partial is True


def test_canonicals_backfill_result_merge_sums_counters():
    from mimir.ingest.backfill import BackfillResult

    a = BackfillResult(examined=10, resolved=4, unresolved=5, skipped=1)
    b = BackfillResult(
        examined=20,
        resolved=8,
        unresolved=10,
        skipped=2,
        partial=False,
        continuation=None,
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.resolved == 12
    assert m.unresolved == 15
    assert m.skipped == 3
    assert m.partial is False
    assert m.continuation is None


# ----- broker handlers ---------------------------------------------------


def test_dispatch_backfill_patch_series_returns_counters_partial_continuation(
    seeded_db,
):
    """The patch-series backfill handler runs the full seeded corpus
    cheaply (no blob fetch). Reply.result must carry the three
    keys the CLI loop reads."""
    reply = dispatch(_line(BackfillPatchSeriesRequest(limit=4)))
    assert reply.ok is True, reply.error
    assert reply.result is not None
    assert "counters" in reply.result
    assert "partial" in reply.result
    assert "continuation" in reply.result
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    # Natural completion at limit=4 (==corpus): partial False.
    assert reply.result["partial"] is False
    assert reply.result["continuation"] is None


def test_dispatch_backfill_patch_series_passes_continuation_through(seeded_db):
    """`start_cursor=<id>` makes the handler skip articles with id
    >= cursor. Seeded ids are 1..4; resume at cursor=2 examines
    only id=1."""
    with SessionLocal() as s:
        min_id = s.execute(select(Article.id).order_by(Article.id)).scalars().first()
    reply = dispatch(
        _line(
            BackfillPatchSeriesRequest(continuation=min_id + 1),
        )
    )
    assert reply.ok is True
    counters = reply.result["counters"]
    # cursor=min_id+1 → only the id=min_id row matches.
    assert counters["examined"] == 1


def test_dispatch_backfill_article_files_reply_shape(seeded_db):
    """Article-files backfill needs the mirror to fetch blobs; the
    seeded fixture has no mirrors so every article falls into
    `skipped`. We don't care about the counters here, just that the
    reply shape is well-formed and the handler doesn't crash on
    unreachable mirrors (the existing `_process_one` catches
    `MessageNotFound` / `KeyError` and reports `skipped`)."""
    reply = dispatch(_line(BackfillArticleFilesRequest(limit=4)))
    assert reply.ok is True, reply.error
    assert reply.result is not None
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    # No blobs to read; every article skips. The point of the test
    # is the contract, not the counter value, but the bucket is a
    # convenient pin against the handler accidentally turning
    # MessageNotFound into `failed`.
    assert counters["skipped"] == 4


def test_dispatch_backfill_article_trailers_reply_shape(seeded_db):
    reply = dispatch(_line(BackfillArticleTrailersRequest(limit=4)))
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    assert counters["skipped"] == 4


def test_dispatch_backfill_canonicals_reply_shape(seeded_db):
    """Canonicals walker iterates the seeded corpus; the seeded
    articles have no canonical_inbox set yet, so the default
    (non-reprocess) path scans all 4. They all end up `skipped`
    because the mirror isn't real."""
    reply = dispatch(_line(BackfillCanonicalsRequest()))
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    assert counters["skipped"] == 4


def test_dispatch_backfill_canonicals_with_inbox_filter(seeded_db):
    """`inbox_filter='alpha'` restricts the walk to articles linked
    to alpha. The seeded fixture has 3 articles linked to alpha
    (art1, art3 via cross-post, art4); art2 is beta-only."""
    reply = dispatch(_line(BackfillCanonicalsRequest(inbox_filter="alpha")))
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 3


# ----- BrokerClient round-trip ------------------------------------------


def test_broker_client_backfill_returns_typed_result(seeded_db):
    """`BrokerClient.backfill_patch_series` reconstructs a typed
    `BackfillResult` from the broker reply, with `partial` /
    `continuation` populated. End-to-end via a real broker on a
    tmp socket so the framing + JSONL + reply-shape round-trip is
    exercised."""
    from mimir.broker.client import BrokerClient
    from mimir.patch_series import BackfillResult
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("backfill-typed")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.backfill_patch_series(limit=4)
        finally:
            c.close()
    assert isinstance(result, BackfillResult)
    assert result.examined == 4
    assert result.partial is False
    assert result.continuation is None


def test_broker_client_backfill_canonicals_inbox_filter(seeded_db):
    """`inbox_filter` flows through the BrokerClient → request →
    handler chain."""
    from mimir.broker.client import BrokerClient
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("backfill-canonicals-filter")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.backfill_canonicals(inbox_filter="alpha")
        finally:
            c.close()
    assert result.examined == 3  # 3 alpha-linked seeded articles


# ----- _broker_backfill_loop convergence --------------------------------


def test_broker_backfill_loop_converges_across_chunks(seeded_db):
    """Stub a `client.backfill_*`-shaped call that returns partial
    chunks N-2 times before a final partial=False chunk. The loop
    must aggregate counter sums and stop on partial=False."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.patches import BackfillResult

    # Pre-canned chunks: two partials then a finisher.
    chunks = [
        BackfillResult(examined=2, indexed=1, partial=True, continuation=99),
        BackfillResult(examined=2, indexed=1, partial=True, continuation=42),
        BackfillResult(examined=1, indexed=0, partial=False, continuation=None),
    ]
    calls: list[dict] = []

    def fake_call(*, limit, reprocess, continuation):
        calls.append(
            {"limit": limit, "reprocess": reprocess, "continuation": continuation}
        )
        return chunks.pop(0)

    result = _broker_backfill_loop(
        fake_call,
        limit=None,
        reprocess=False,
    )
    assert result.examined == 5  # 2 + 2 + 1
    assert result.indexed == 2  # 1 + 1 + 0
    assert result.partial is False
    assert result.continuation is None
    # Three RPCs: continuation None → 99 → 42.
    assert [c["continuation"] for c in calls] == [None, 99, 42]


def test_broker_backfill_loop_respects_limit_across_chunks(seeded_db):
    """`limit=5` across chunks of 2/2/2 stops after consuming
    examined=5: the third call's `limit=` arg goes through as 1
    (5 − 2 − 2), but the loop ALSO terminates after that chunk
    even if it claims partial=True (we've hit our cap).

    The aggregation rule: each iteration subtracts `chunk.examined`
    from the remaining cap. The handler is trusted to respect the
    per-chunk limit it was given; the loop short-circuits any time
    the cap reaches zero."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.patches import BackfillResult

    chunks = [
        BackfillResult(examined=2, indexed=1, partial=True, continuation=99),
        BackfillResult(examined=2, indexed=1, partial=True, continuation=42),
        BackfillResult(examined=1, indexed=0, partial=True, continuation=7),
    ]
    seen_limits: list[int | None] = []

    def fake_call(*, limit, reprocess, continuation):
        seen_limits.append(limit)
        return chunks.pop(0)

    result = _broker_backfill_loop(
        fake_call,
        limit=5,
        reprocess=False,
    )
    # 2+2+1 == 5; loop stops there even though chunk 3 said partial=True.
    assert result.examined == 5
    # Per-chunk limits: 5, 3, 1.
    assert seen_limits == [5, 3, 1]


def test_broker_backfill_loop_passes_extra_kwargs(seeded_db):
    """Canonicals' `inbox_filter` rides on `extra=` in the loop
    helper. Verify it flows through to the bound client method on
    every iteration."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.ingest.backfill import BackfillResult

    seen_extras: list[dict] = []

    def fake_call(*, limit, reprocess, continuation, inbox_filter):
        seen_extras.append({"inbox_filter": inbox_filter})
        return BackfillResult(examined=1, resolved=1)

    result = _broker_backfill_loop(
        fake_call,
        limit=None,
        reprocess=False,
        extra={"inbox_filter": "alpha"},
    )
    assert result.examined == 1
    assert seen_extras == [{"inbox_filter": "alpha"}]


# ----- end-to-end chunked resume via the broker -------------------------


def test_chunked_resume_via_broker_covers_full_corpus(seeded_db, monkeypatch):
    """Plant a backfill_patch_series RPC chain with a tiny chunk
    budget (1 ms) so the broker handler returns partial=True after
    each batch; the CLI loop resumes via continuation. Pin: across
    the chain, every seeded article gets `examined` exactly once.

    Uses `_broker_backfill_loop` directly against a real broker so
    the chunking is verified end-to-end through the protocol +
    handler + walker.
    """
    from mimir.broker.client import BrokerClient
    from mimir.cli._common import _broker_backfill_loop
    from mimir.config import settings
    from tests.test_broker._helpers import broker_running, short_socket_path

    # Add 6 more cover-letter-shaped articles so the seeded corpus
    # has 10 rows total. With the chunk-seconds dial cranked to
    # ~0.001 s, each broker chunk processes at most a tiny number
    # of articles before the deadline forces a partial reply.
    _seed_extra_articles(6)
    # Make the broker bail very fast so we get multiple chunks.
    # We can't quite use 0 because that races with the batch-size
    # internal loop; a few milliseconds is enough to process one
    # batch then exit.
    monkeypatch.setattr(settings, "broker_backfill_chunk_seconds", 0)

    sp = short_socket_path("backfill-resume")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = _broker_backfill_loop(
                c.backfill_patch_series,
                limit=None,
                reprocess=False,
            )
        finally:
            c.close()

    # 10 articles (4 seeded + 6 added) all examined across the
    # multi-chunk walk; converged on a clean finish.
    assert result.examined == 10
    assert result.partial is False
    assert result.continuation is None
