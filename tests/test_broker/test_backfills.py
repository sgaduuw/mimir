"""Phase 2.2 backfill RPCs with cooperative scheduling.

Each of the four backfills routes through the broker as a chain of
chunked RPCs: the handler runs for at most
`Settings.broker_backfill_chunk_seconds` (default 10 s) before
returning `partial=True`; the CLI loops with a follow-up RPC until
`partial=False`. The broker keeps the run's cursor; resuming across
sessions is covered in `tests/test_backfill_runs.py`. This file covers:

- `walk_articles` time-budget mechanics (partial surface, single
  chunk inside the budget completes, exceeding the budget bails at
  the next batch boundary, the walk starts below the run's cursor).
- The `BackfillResult.merge` shape on each of the four backfill
  flavours (counter sums; carry-forward of `other`'s partial).
- The broker handler returns `Reply.result = {counters, partial,
  run}` and the `BrokerClient.backfill_*` methods reconstruct a
  typed `BackfillResult` from it.
- The `_broker_backfill_loop` helper aggregates a multi-chunk
  result and converges on `partial=False` at the natural end.

The patch-series backfill is the cheapest (no blob fetch, only
subject/author/thread_parent reads) so it's the main vehicle for
the dispatch / merge / loop tests; the other three are covered
structurally by the protocol + handler registration smoke and the
typed-result reconstruction on the client.
"""

from __future__ import annotations

from datetime import UTC

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
    from datetime import datetime

    from mimir.models import ArticleList, Inbox

    ids: list[int] = []
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i in range(n):
            art = Article(
                message_id=f"coop-{i}@example.com",
                subject=f"[PATCH 0/{n}] coop test {i}",
                author=f"Author{i} <a{i}@example.com>",
                date=datetime(2025, 1, 1 + i, 12, 0, tzinfo=UTC),
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
    boundary; the walker returns `partial=True` after committing what
    it touched. Use
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

    def flush_batch(writer, payloads, after=None):
        f = Future()
        f.set_result(None)
        return f

    partial = walk_articles(
        result,
        process,
        flush_batch,
        batch_size=2,
        label="test_walk_partial",
        max_seconds=0.0,
    )
    assert partial is True
    # We processed exactly one batch (2 articles) and bailed.
    assert result.examined == 2


def test_walk_articles_returns_full_completion_without_deadline(
    seeded_db, broker_active
):
    """No `max_seconds` set: the walker runs to natural end and
    returns `partial=False`.

    Phase 3c: `process_one` returns a `(bucket, payload)` tuple;
    `flush_batch` is required."""
    from concurrent.futures import Future

    from mimir._backfill import walk_articles
    from mimir.patches import BackfillResult

    result = BackfillResult()

    def flush_batch(writer, payloads, after=None):
        f = Future()
        f.set_result(None)
        return f

    partial = walk_articles(
        result,
        lambda _s, _a, _r: ("no_diff", None),
        flush_batch,
        batch_size=2,
        label="test_walk_full",
    )
    assert partial is False
    assert result.examined == 4  # all seeded articles


def test_walk_articles_starts_below_the_run_cursor(seeded_db, broker_active):
    """A run's cursor is the lowest id already processed, so the walk
    starts at `Article.id < cursor`. Seeded ids are consecutive; a
    cursor of `min_id + 1` leaves exactly the oldest article."""
    from concurrent.futures import Future
    from datetime import datetime

    from mimir._backfill import walk_articles
    from mimir.backfill_runs import Run
    from mimir.patches import BackfillResult

    def flush_batch(writer, payloads, after=None):
        f = Future()
        f.set_result(None)
        return f

    with SessionLocal() as s:
        min_id = s.execute(select(Article.id).order_by(Article.id)).scalars().first()
    seen: list[int] = []

    def process(_s, article, _r):
        seen.append(article.id)
        return ("no_diff", None)

    result = BackfillResult()
    partial = walk_articles(
        result,
        process,
        flush_batch,
        batch_size=2,
        label="test_resume",
        run=Run(
            id=0,
            started_at=datetime(2026, 1, 1),
            cursor=min_id + 1,
            examined=0,
            finished=False,
            updated_at=datetime(2026, 1, 1),
        ),
    )
    assert partial is False
    assert seen == [min_id]


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
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.indexed == 15
    assert m.no_diff == 8
    assert m.skipped == 5
    assert m.failed == 2
    # partial carries the LATEST chunk's value forward, not summed.
    assert m.partial is True


def test_trailers_backfill_result_merge_sums_counters():
    from mimir.trailers import BackfillResult

    a = BackfillResult(examined=10, indexed=5, no_trailers=4, skipped=1)
    b = BackfillResult(
        examined=20,
        indexed=8,
        no_trailers=10,
        skipped=2,
        partial=False,
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.indexed == 13
    assert m.no_trailers == 14
    assert m.skipped == 3
    assert m.partial is False


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
    )
    m = a.merge(b)
    assert m.examined == 10
    assert m.indexed == 3
    assert m.in_series_indexed == 3
    assert m.in_series_orphan == 1
    assert m.not_cover == 3
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
    )
    m = a.merge(b)
    assert m.examined == 30
    assert m.resolved == 12
    assert m.unresolved == 15
    assert m.skipped == 3
    assert m.partial is False


# ----- broker handlers ---------------------------------------------------


def test_dispatch_backfill_patch_series_returns_counters_partial_run(
    seeded_db,
):
    """The patch-series backfill handler runs the full seeded corpus
    cheaply (no blob fetch). Reply.result must carry the three
    keys the CLI loop reads."""
    reply = dispatch(
        _line(BackfillPatchSeriesRequest(rpc_id=1, limit=4, start="resume"))
    )
    assert reply.ok is True, reply.error
    assert reply.result is not None
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    # Natural completion at limit=4 (==corpus): partial False.
    assert reply.result["partial"] is False
    run = reply.result["run"]
    # A fresh run: no cursor before this chunk, and its examined count
    # is this chunk's.
    assert run["cursor"] is None
    assert run["examined"] == 4
    assert run["started_at"]


def test_dispatch_backfill_article_files_reply_shape(seeded_db):
    """Article-files backfill needs the mirror to fetch blobs; the
    seeded fixture has no mirrors so every article falls into
    `skipped`. We don't care about the counters here, just that the
    reply shape is well-formed and the handler doesn't crash on
    unreachable mirrors (the existing `_process_one` catches
    `MessageNotFound` / `KeyError` and reports `skipped`)."""
    reply = dispatch(
        _line(BackfillArticleFilesRequest(rpc_id=1, limit=4, start="resume"))
    )
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
    reply = dispatch(
        _line(BackfillArticleTrailersRequest(rpc_id=1, limit=4, start="resume"))
    )
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    assert counters["skipped"] == 4


def test_dispatch_backfill_canonicals_reply_shape(seeded_db):
    """Canonicals walker iterates the seeded corpus; the seeded
    articles have no canonical_inbox set yet, so the default
    (non-reprocess) path scans all 4. They all end up `skipped`
    because the mirror isn't real."""
    reply = dispatch(_line(BackfillCanonicalsRequest(rpc_id=1, start="resume")))
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 4
    assert counters["skipped"] == 4


def test_dispatch_backfill_canonicals_with_inbox_filter(seeded_db):
    """`inbox_filter='alpha'` restricts the walk to articles linked
    to alpha. The seeded fixture has 3 articles linked to alpha
    (art1, art3 via cross-post, art4); art2 is beta-only."""
    reply = dispatch(
        _line(BackfillCanonicalsRequest(rpc_id=1, inbox_filter="alpha", start="resume"))
    )
    assert reply.ok is True, reply.error
    counters = reply.result["counters"]
    assert counters["examined"] == 3


# ----- BrokerClient round-trip ------------------------------------------


def test_broker_client_backfill_returns_typed_result(seeded_db):
    """`BrokerClient.backfill` reconstructs a typed
    `BackfillResult` from the broker reply, plus the run info.
    End-to-end via a real broker on a tmp socket so the framing +
    JSONL + reply-shape round-trip is exercised."""
    from mimir.broker.client import BrokerClient
    from mimir.broker.protocol import BackfillPatchSeriesRequest
    from mimir.patch_series import BackfillResult
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("backfill-typed")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result, run = c.backfill(
                BackfillPatchSeriesRequest(limit=4, start="resume"),
                BackfillResult,
            )
        finally:
            c.close()
    assert isinstance(result, BackfillResult)
    assert result.examined == 4
    assert result.partial is False
    assert run["examined"] == 4


def test_broker_client_backfill_canonicals_inbox_filter(seeded_db):
    """`inbox_filter` flows through the BrokerClient → request →
    handler chain."""
    from mimir.broker.client import BrokerClient
    from mimir.broker.protocol import BackfillCanonicalsRequest
    from mimir.ingest.backfill import BackfillResult
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("backfill-canonicals-filter")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result, _run = c.backfill(
                BackfillCanonicalsRequest(inbox_filter="alpha", start="resume"),
                BackfillResult,
            )
        finally:
            c.close()
    assert result.examined == 3  # 3 alpha-linked seeded articles


# ----- _broker_backfill_loop convergence --------------------------------


_RUN = {
    "started_at": "2026-10-11 00:00:00",
    "cursor": None,
    "examined": 0,
    "finished": False,
}


def test_broker_backfill_loop_converges_across_chunks(seeded_db):
    """Stub a chunk call that returns partial
    chunks N-2 times before a final partial=False chunk. The loop
    must aggregate counter sums, stop on partial=False, and send
    `start` on the first RPC only."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.patches import BackfillResult

    # Pre-canned chunks: two partials then a finisher.
    chunks = [
        BackfillResult(examined=2, indexed=1, partial=True),
        BackfillResult(examined=2, indexed=1, partial=True),
        BackfillResult(examined=1, indexed=0, partial=False),
    ]
    calls: list[dict] = []

    def fake_call(*, limit, reprocess, start):
        calls.append({"limit": limit, "reprocess": reprocess, "start": start})
        return chunks.pop(0), {**_RUN, "examined": 9}

    result, run = _broker_backfill_loop(
        fake_call,
        limit=None,
        reprocess=False,
        restart=True,
    )
    assert result.examined == 5  # 2 + 2 + 1
    assert result.indexed == 2  # 1 + 1 + 0
    assert result.partial is False
    assert run["examined"] == 9
    assert [c["start"] for c in calls] == ["restart", None, None]


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
        BackfillResult(examined=2, indexed=1, partial=True),
        BackfillResult(examined=2, indexed=1, partial=True),
        BackfillResult(examined=1, indexed=0, partial=True),
    ]
    seen_limits: list[int | None] = []

    def fake_call(*, limit, reprocess, start):
        seen_limits.append(limit)
        return chunks.pop(0), _RUN

    result, _run = _broker_backfill_loop(
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
    helper. Verify it flows through to the chunk call on every
    iteration."""
    from mimir.cli._common import _broker_backfill_loop
    from mimir.ingest.backfill import BackfillResult

    seen_extras: list[dict] = []

    def fake_call(*, limit, reprocess, start, inbox_filter):
        seen_extras.append({"inbox_filter": inbox_filter})
        return BackfillResult(examined=1, resolved=1), _RUN

    result, _run = _broker_backfill_loop(
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
    each batch; each chunk resumes below the run's cursor. Pin: across
    the chain, every seeded article gets `examined` exactly once.

    Uses `_broker_backfill_loop` directly against a real broker so
    the chunking is verified end-to-end through the protocol +
    handler + walker.
    """
    from mimir.broker.client import BrokerClient
    from mimir.broker.protocol import BackfillPatchSeriesRequest
    from mimir.cli._common import _broker_backfill_loop
    from mimir.config import settings
    from mimir.patch_series import BackfillResult
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
            result, run = _broker_backfill_loop(
                lambda **kw: c.backfill(
                    BackfillPatchSeriesRequest(**kw), BackfillResult
                ),
                limit=None,
                reprocess=False,
            )
        finally:
            c.close()

    # 10 articles (4 seeded + 6 added) all examined across the
    # multi-chunk walk; converged on a clean finish.
    assert result.examined == 10
    assert result.partial is False
    assert run["examined"] == 10
