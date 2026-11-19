"""Subprocess body for test_pool_lockstep_canonical_patch_does_not_deadlock
(#1221). Exit 0 = all rounds ran, 3 = a round stalled, 4 = the workers
stopped early. Two threads check out pool sessions in lockstep and run a
fresh SQL text each round through the `canonical_patch` UDF."""

import os
import sqlite3
import sys
import threading
import time

from sqlalchemy import text

from mimir.broker.pools import ReadSessionPool
from mimir.patch_subjects import canonical_patch as _canonical_patch

db, rounds = sys.argv[1], int(sys.argv[2])
c = sqlite3.connect(db)
c.execute("CREATE TABLE a(id INTEGER PRIMARY KEY, s TEXT, p INT, k TEXT)")
c.executemany(
    "INSERT INTO a(s,p,k) VALUES (?,?,?)",
    ((f"[PATCH {i % 7}/7] x {i}", i % 8, f"k{i % 500}") for i in range(3000)),
)
c.commit()
c.close()

pool = ReadSessionPool(f"sqlite:///{db}", pool_size=2)
barrier = threading.Barrier(2)
progress = [0]


def worker():
    for r in range(rounds):
        barrier.wait()
        with pool.session() as s:
            s.connection().connection.driver_connection.create_function(
                "canonical_patch", 3, _canonical_patch, deterministic=True
            )
            s.execute(text(f"SELECT count(*), {r} FROM a WHERE canonical_patch(s,p,k)"))
        barrier.wait()
        progress[0] = r + 1


threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
for t in threads:
    t.start()
last, changed = -1, time.monotonic()
while any(t.is_alive() for t in threads):
    time.sleep(0.1)
    if progress[0] != last:
        last, changed = progress[0], time.monotonic()
    elif time.monotonic() - changed > 3:
        os._exit(3)
sys.exit(0 if progress[0] == rounds else 4)
