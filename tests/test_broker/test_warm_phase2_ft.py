"""Phase 2 free-threading stress test: N concurrent warm_inbox
RPCs against a running broker, verify all complete cleanly with
the active read pool + writer in the loop. Runs under both
Python 3.14 (test job) and 3.14t (test-ft job) via the existing
CI matrix from 2.9.0."""

import threading
from concurrent.futures import ThreadPoolExecutor, wait

from mimir.broker import _context
from mimir.broker.handlers.warm import handle_warm_inbox
from mimir.broker.pools import ReadSessionPool
from mimir.broker.protocol import WarmInboxRequest
from mimir.broker.writes import WriterThread


def test_phase2_concurrent_warm_inbox_no_deadlock_or_race(seeded_db):
    """N threads each call handle_warm_inbox concurrently. With the
    active read pool + writer registered, all should complete
    successfully. Pin: no deadlock, no exception, no future left
    unresolved."""
    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        _context.set_active(pool, writer)
        N = 8
        req = WarmInboxRequest(inbox_name="alpha", targets=None)

        barrier = threading.Barrier(N)

        def worker():
            barrier.wait(timeout=5)
            return handle_warm_inbox(req)

        with ThreadPoolExecutor(max_workers=N) as ex:
            futures = [ex.submit(worker) for _ in range(N)]
            done, not_done = wait(futures, timeout=30)
            assert not not_done, f"{len(not_done)} workers didn't finish"
            for f in done:
                reply = f.result()
                assert reply.ok is True
    finally:
        _context.clear_active()
        writer.stop(timeout=10)
        pool.close()
