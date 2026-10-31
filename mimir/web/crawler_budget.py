"""Token buckets that throttle declared crawlers (#761, #1026).

One well-behaved crawler can take most of the web tier, and a CDN rate
limit cannot tell it from readers (it counts per IP, and the crawl is
spread over thousands of IPs). The origin is the one place that sees
the whole crawl, so the budget lives here: a configured crawler over
budget gets `429` + `Retry-After`, which search crawlers read as "slow
down". Humans are never gated.

The global budget (`CRAWLER_BUDGET_PER_MINUTE`) is the master switch:
0 gates nothing. A request is a crawler when its User-Agent contains a
listed name (case-insensitive substring, first listed wins) or carries
a product token such as `Googlebot/2.1`, `GPTBot/1.0` or `curl/8.5.0`: a
name ending in one of the configured markers (by default `bot`,
`crawler`, `spider`, `curl`, `wget`, `python-requests`) followed by a
slash. A token that is just the marker (`bot/1.0`, `curl/8.5.0`) counts
too, on purpose. The slash is required so a browser on a device called
`CUBOT X30` is never gated. Script tools are markers, so an operator's
curl smoke check is a crawler once the global budget is set.

A listed crawler holds its own percentage of the global budget,
`global * pct / 100`; any other recognised crawler holds the default
percentage, in a bucket keyed by its lowercased token, and also draws on
one shared pool bucket (`crawler_unlisted_pool_pct` of the global), so
a flood of invented crawler names cannot drain the global bucket and
starve real crawlers. Listed crawlers never touch the pool. A request is
charged against every bucket it needs, and only when ALL admit it.
A refused request charges none, so a crawler that hits a wall does not
also drain the share left for its neighbours.

A crawler's share is also its priority on the global bucket (#1026): a
crawler holding `pct` may take a global token only while the bucket is
more than `(100 - pct)%` full. Googlebot at 80% can draw it down to
20%; Applebot at 10% is served only from the top tenth, i.e. when nobody
ranked above it is using the bucket. Without this the global bucket was
first come, first served, and a busy low-share crawler took the token a
search crawler needed: Googlebot got 429s from a full own bucket. The
share still caps the crawler's own rate, so shares need not add up to
100 (the defaults add up to 260): they are ranks plus caps, not
reservations. Unlisted crawlers rank at the default percentage (with
the defaults, level with the 10% listed crawlers) and the
impossible-headers bucket at its own 1%, below everything.

Shared across workers, and its error bound: gunicorn runs sync workers,
one request per process, so a concurrency cap degenerates and a counter
in the broker would put an RPC on every crawler request. The fixed
buckets (global, pool, overflow, impossible-headers and one per listed
crawler) instead live in one small file mapped into every worker
(`/dev/shm` where it exists), each a `(tokens, stamp)` pair updated
under a POSIX record lock (`lockf`). The file is named after the gunicorn master's pid and
a hash of the budget layout, so a config change never reads another
layout's slots; the first worker in fills the buckets, and the kernel
drops a dead worker's lock. If the file cannot be created, the gate
logs a warning and falls back to per-worker buckets rather than fail
the import. These buckets therefore hold the whole
server's share (global 120, Applebot 10%: 12 a minute), however many
workers there are. Before #1026 they were per worker at `budget /
web_workers`, which at production settings left each worker's global
bucket about one token: nothing to rank, and nothing for one worker to
lend another. The clock is read again once the file lock is held, and a
stamp never moves back, so time a worker spent waiting is never
credited twice. Over any window a bucket admits at most that window's
share of its rate plus its capacity, and capacity is `BURST_SECONDS`
(10) seconds of rate, or one token if that is more, so a share below one
request every ten seconds still admits one request every `60 / rate`
seconds instead of never. Each web instance (container) holds its own
file, so instances cannot see each other: split the budget across them.

Memory bound: the token is attacker-chosen text, so buckets for unlisted
crawlers stay in each worker (a fixed shared layout cannot hold them, so
each holds `budget * pct / 100 / web_workers`) and are capped at
`MAX_UNLISTED_BUCKETS`. A bucket that has been
idle for ten minutes and has refilled holds no state and is dropped. At
the cap, a further new token shares ONE overflow bucket at the default
percentage, so a flood of invented crawler names is throttled together
instead of each getting a fresh bucket.

A request that did not identify a crawler in its User-Agent but sends
`Sec-Fetch-Site: none` with a `Referer` (#903) is held in ONE small shared
bucket, `crawler_impossible_headers_pct` of the global. `none` marks a
user-initiated navigation (typed URL, bookmark), and a browser sends no
Referer on one, so the pair is a client faking a click. It charges the
global bucket like a listed crawler, not the unlisted pool. A declared
crawler keeps its own bucket. Its log line says the match was on request
headers, not the User-Agent.

No database, blob or render work happens on any path through here.
"""

import fcntl
import hashlib
import logging
import mmap
import os
import re
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from mimir.config import Settings

logger = logging.getLogger(__name__)

# Endpoint names, not paths, so a route rename cannot silently un-exempt
# one (a test asserts each name exists in the url_map). Cheap or
# structural routes a crawler needs to behave well: robots, sitemaps,
# probes, static files. Conditional requests are NOT exempt on purpose.
EXEMPT_ENDPOINTS = frozenset(
    {
        "static",
        "web.robots_txt",
        "web.sitemap",
        "web.messages_sitemap",
        "web.meta_sitemap",
        "web.maintainers_sitemap",
        "web.series_diff_sitemap",
        "web.inbox_sitemap",
        "web.archive_sitemap",
        "web.archive_messages_sitemap",
        "web.healthz",
        "web.readyz",
    }
)

_LOG_EVERY_SEC = 60

# A bucket holds this many seconds of its rate. A full minute (the
# textbook choice) lets a bucket admit a whole minute's budget at once
# at start and after any idle minute, so a 60 s window could see twice
# the budget (measured: 239 for a budget of 120). Ten seconds keeps a
# crawler's natural bursts while bounding that to ten seconds' worth.
BURST_SECONDS = 10

# Distinct unlisted crawler tokens held at once. Each bucket is a few
# floats, so this bounds memory at a trivial size while being far above
# the handful of real crawlers a site sees.
MAX_UNLISTED_BUCKETS = 256
# A bucket idle this long (and refilled) carries no state: drop it.
_IDLE_DROP_SEC = 600
OVERFLOW_KEY = "(other crawlers)"
IMPOSSIBLE_KEY = "(impossible headers)"

# Shared memory layout: a header marking the slots initialised, then
# one (tokens, stamp) pair per fixed bucket.
_HEADER = struct.Struct("d")
_SLOT = struct.Struct("dd")
_READY = 1026.0
# Which bucket refused a request, in the order they are checked.
REFUSALS = ("own", "global", "pool")


def impossible_headers(sec_fetch_site: str | None, referer: str | None) -> bool:
    """`Sec-Fetch-Site: none` (a user-initiated navigation) with a Referer."""
    return bool(referer) and (sec_fetch_site or "").lower() == "none"


def shared_dir() -> str:
    # tmpfs where there is one, so the file never touches a disk.
    return "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()


class _Shared:
    """The fixed buckets' state in one mmap. With a path, every process
    opening the same file shares it; without, it is private to this
    instance (tests, and the gate when it is off)."""

    def __init__(self, slots: int, path: str | None):
        size = _HEADER.size + slots * _SLOT.size
        self._fd: int | None = None
        self.mem: mmap.mmap | bytearray
        if path is None:
            self.mem = bytearray(size)
            return
        # O_NOFOLLOW: the name is predictable, so never write through a
        # symlink someone else planted there.
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.ftruncate(self._fd, size)  # zero-filled on create, a no-op after
            self.mem = mmap.mmap(self._fd, size)
        except OSError:
            os.close(self._fd)
            raise

    @contextmanager
    def lock(self) -> Iterator[None]:
        # A POSIX record lock, not flock: flock belongs to the open file
        # description, which a fork shares, so a gate built before the
        # workers fork (gunicorn --preload) would have every worker
        # "holding" it at once. lockf is per process and not inherited.
        # It does not exclude threads of one process, which is why
        # callers also hold a threading.Lock. Closing ANY fd on the file
        # in this process drops it, and mmap holds a dup of ours, so
        # there must be one gate per path per process (production builds
        # exactly one, at import). The kernel drops it when a process
        # dies, so a killed worker cannot wedge the rest.
        if self._fd is None:
            yield
            return
        fcntl.lockf(self._fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.lockf(self._fd, fcntl.LOCK_UN)

    def ready(self) -> bool:
        return _HEADER.unpack_from(self.mem, 0)[0] == _READY

    def mark_ready(self) -> None:
        _HEADER.pack_into(self.mem, 0, _READY)


class _Bucket:
    def __init__(
        self,
        per_minute: float,
        now: float,
        mem: mmap.mmap | bytearray | None = None,
        slot: int = 0,
    ):
        self.rate = per_minute / 60  # tokens per second
        self.capacity = max(per_minute * BURST_SECONDS / 60, 1.0)
        # An unlisted bucket gets a private buffer of its own; the same
        # pack/unpack then serves both kinds.
        self._mem = bytearray(_HEADER.size + _SLOT.size) if mem is None else mem
        self._off = _HEADER.size + slot * _SLOT.size
        if mem is None:
            self.fill(now)
        self.last_used = now
        self.deferred = dict.fromkeys(REFUSALS, 0)  # since the last log line
        self.last_log: float | None = None

    def _get(self) -> tuple[float, float]:
        return _SLOT.unpack_from(self._mem, self._off)

    def _set(self, tokens: float, stamp: float) -> None:
        _SLOT.pack_into(self._mem, self._off, tokens, stamp)

    @property
    def tokens(self) -> float:
        return self._get()[0]

    def fill(self, now: float) -> None:
        self._set(self.capacity, now)

    def take(self) -> None:
        tokens, stamp = self._get()
        self._set(tokens - 1, stamp)

    def refill(self, now: float) -> None:
        tokens, stamp = self._get()
        # A stamp never moves back: a clock read that lands behind one
        # another worker already wrote would credit that interval twice.
        gained = max(now - stamp, 0) * self.rate
        self._set(min(self.capacity, tokens + gained), max(now, stamp))

    def has_token(self) -> bool:
        return self.tokens >= 1

    def admits_at(self, pct: int) -> bool:
        """A token for a crawler holding `pct` of the budget: only while
        the bucket is more than (100 - pct)% full, so a higher share
        draws it lower and a low share is served from what is left. A
        full bucket admits anyone, so a lone crawler is never starved."""
        tokens = self.tokens
        return tokens >= 1 and tokens > self.capacity * (1 - pct / 100)


class CrawlerBudget:
    def __init__(
        self,
        total_per_minute: int,
        ua_percent: dict[str, int],
        web_workers: int,
        retry_after_sec: int,
        clock: Callable[[], float] = time.monotonic,
        markers: Sequence[str] | None = None,
        default_ua_pct: int = 10,
        unlisted_pool_pct: int = 30,
        impossible_headers_pct: int = 1,
        shared_path: str | None = None,
        split: int = 1,
    ):
        self.retry_after_sec = retry_after_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._workers = web_workers
        self._total = total_per_minute
        self._default_pct = default_ua_pct
        self._impossible_pct = impossible_headers_pct
        # The fixed buckets hold the instance's share divided by this:
        # 1 when shared, the worker count when each worker keeps its own.
        self._split = split
        now = clock()
        use_impossible = total_per_minute > 0 and impossible_headers_pct > 0
        # global, pool, overflow, one per listed crawler, impossible.
        slots = 3 + len(ua_percent) + use_impossible
        self._shared = _Shared(slots, shared_path)
        mem = self._shared.mem
        self._global = _Bucket(total_per_minute / split, now, mem, 0)
        self._pool = self._bucket(unlisted_pool_pct, now, 1)
        self._overflow = self._bucket(default_ua_pct, now, 2)
        # Dict order is match order: the first configured crawler found
        # in the User-Agent wins.
        self._listed = {
            name: (name.lower(), self._bucket(pct, now, 3 + i), pct)
            for i, (name, pct) in enumerate(ua_percent.items())
        }
        self._impossible = (
            self._bucket(impossible_headers_pct, now, slots - 1)
            if use_impossible
            else None
        )
        fixed = [self._global, self._pool, self._overflow]
        fixed += [b for _, b, _ in self._listed.values()]
        if self._impossible:
            fixed.append(self._impossible)
        with self._shared.lock():
            # The first worker in starts every bucket full; the rest
            # find them already running.
            if not self._shared.ready():
                for b in fixed:
                    b.fill(now)
                self._shared.mark_ready()
        if markers is None:
            markers = Settings.model_fields["crawler_ua_markers"].default
        alternatives = "|".join(re.escape(m) for m in markers)
        self._token = (
            re.compile(rf"\b((?:[A-Za-z][\w.-]*)?(?:{alternatives}))/", re.IGNORECASE)
            if alternatives
            else None
        )
        self._unlisted: dict[str, _Bucket] = {}
        self._last_sweep = now

    def _bucket(self, pct: int, now: float, slot: int) -> _Bucket:
        # A fixed bucket in shared memory, at its share of the whole
        # server's budget: every worker draws on the same tokens.
        per_minute = self._total * pct / 100 / self._split
        return _Bucket(per_minute, now, self._shared.mem, slot)

    @classmethod
    def from_settings(cls, s: Settings) -> CrawlerBudget:
        uas = s.effective_crawler_ua_budgets
        layout = (
            s.crawler_budget_per_minute,
            tuple(uas.items()),
            s.crawler_default_ua_pct,
            s.crawler_unlisted_pool_pct,
            s.crawler_impossible_headers_pct,
        )
        path = None
        if s.crawler_budget_per_minute > 0:
            # One file per gunicorn master (each worker's parent), per pid
            # namespace (containers in one pod share /dev/shm, and their
            # masters can have the same pid) and per layout, so a changed
            # config never reads another's slots.
            digest = hashlib.sha256(repr(layout).encode()).hexdigest()[:12]
            try:
                pidns = os.stat("/proc/self/ns/pid").st_ino
            except OSError:
                pidns = 0
            path = os.path.join(
                shared_dir(),
                f"mimir-crawler-budget-{pidns}-{os.getppid()}-{digest}",
            )

        def build(shared_path: str | None, split: int = 1) -> CrawlerBudget:
            return cls(
                s.crawler_budget_per_minute,
                uas,
                s.web_workers,
                s.crawler_retry_after_sec,
                markers=s.crawler_ua_markers,
                default_ua_pct=s.crawler_default_ua_pct,
                unlisted_pool_pct=s.crawler_unlisted_pool_pct,
                impossible_headers_pct=s.crawler_impossible_headers_pct,
                shared_path=shared_path,
                split=split,
            )

        try:
            return build(path)
        except OSError as exc:
            # The gate is built at import in every process, so a missing
            # or read-only directory must not stop the app from booting.
            # Per-worker buckets at budget / WORKERS are the pre-#1026
            # behaviour; a whole share in every worker would multiply the
            # budget by the worker count.
            logger.warning(
                "crawler budget: cannot share buckets through %s (%s); "
                "each worker keeps its own at budget / %d",
                path,
                exc,
                s.web_workers,
            )
            return build(None, split=s.web_workers)

    def crawler_key(self, user_agent: str | None) -> str | None:
        """The bucket key for a crawler request, None for anything else:
        the dict key of a listed crawler, else the lowercased product
        token. Total 0 is the master switch, so nothing is a crawler."""
        if self._total <= 0 or not user_agent:
            return None
        ua = user_agent.lower()
        for name, (needle, _, _) in self._listed.items():
            if needle in ua:
                return name
        match = self._token.search(user_agent) if self._token else None
        return match.group(1).lower() if match else None

    def check(
        self,
        user_agent: str | None,
        sec_fetch_site: str | None = None,
        referer: str | None = None,
    ) -> str | None:
        """Return the bucket key when the request must be refused; None
        when it passes (not gated, or admitted and charged)."""
        key = self.crawler_key(user_agent)
        if (
            key is None
            and self._impossible
            and impossible_headers(sec_fetch_site, referer)
        ):
            key = IMPOSSIBLE_KEY
        with self._lock:
            now = self._clock()
            # Unlisted buckets are this process's own, so the sweep needs
            # no cross-process lock, and readers never take it.
            self._sweep(now)
            if key is None:
                return None
            with self._shared.lock():
                # Read the clock again once the lock is ours: the wait for
                # another worker can be longer than the gap between stamps.
                return self._charge(key, self._clock())

    def _charge(self, key: str, now: float) -> str | None:
        # Caller holds both locks.
        if key == IMPOSSIBLE_KEY:
            own, pct, pool = self._impossible, self._impossible_pct, None
        elif key in self._listed:
            _, own, pct = self._listed[key]
            pool = None
        else:
            own, pct, pool = self._bucket_for(key, now), self._default_pct, self._pool
        own.last_used = now
        for b in (own, self._global, pool):
            if b is not None:
                b.refill(now)
        if not own.has_token():
            refused = "own"
        elif not self._global.admits_at(pct):
            refused = "global"
        elif pool is not None and not pool.has_token():
            refused = "pool"
        else:
            for b in (own, self._global, pool):
                if b is not None:
                    b.take()
            return None
        label = OVERFLOW_KEY if own is self._overflow else key
        self._note_deferred(label, own, now, refused)
        return label

    def _bucket_for(self, key: str, now: float) -> _Bucket:
        # Caller holds the lock. Unlisted buckets stay per process: the
        # keys are attacker-chosen text, which a fixed shared layout
        # cannot hold.
        # ponytail: per-process unlisted buckets each hold budget/workers;
        # they also draw on the shared pool and global, so the server
        # totals still hold. A shared hash table if that ever matters.
        bucket = self._unlisted.get(key)
        if bucket is None:
            if len(self._unlisted) >= MAX_UNLISTED_BUCKETS:
                return self._overflow
            per_minute = self._total * self._default_pct / 100 / self._workers
            bucket = self._unlisted[key] = _Bucket(per_minute, now)
        return bucket

    def _sweep(self, now: float) -> None:
        # Caller holds the lock. At most once a minute: a few hundred
        # buckets at most, so a plain scan is fine.
        if now - self._last_sweep < _LOG_EVERY_SEC:
            return
        self._last_sweep = now
        for key, b in list(self._unlisted.items()):
            if now - b.last_used >= _IDLE_DROP_SEC:
                b.refill(now)
                if b.tokens >= b.capacity:
                    del self._unlisted[key]

    def _note_deferred(
        self, key: str, bucket: _Bucket, now: float, refused: str
    ) -> None:
        # Caller holds the lock. First deferral logs at once so an
        # operator sees the throttle start; then at most once a minute
        # per bucket and worker, with the count since the previous line
        # split by the bucket that refused.
        bucket.deferred[refused] += 1
        if bucket.last_log is not None and now - bucket.last_log < _LOG_EVERY_SEC:
            return
        counts = bucket.deferred
        what = (
            'matched on request headers, not User-Agent: rule="Sec-Fetch-Site: '
            'none with Referer"'
            if key == IMPOSSIBLE_KEY
            else f"crawler={key}"
        )
        logger.warning(
            "crawler budget: %s deferred=%d %s pid=%d",
            what,
            sum(counts.values()),
            " ".join(f"{name}={counts[name]}" for name in REFUSALS),
            os.getpid(),
        )
        bucket.deferred = dict.fromkeys(REFUSALS, 0)
        bucket.last_log = now

    def _find(self, key: str) -> _Bucket:
        return self._listed[key][1] if key in self._listed else self._unlisted[key]

    def ua_tokens(self, key: str) -> float:
        return self._find(key).tokens

    def ua_per_minute(self, key: str) -> float:
        return self._find(key).rate * 60

    def impossible_per_minute(self) -> float:
        return self._impossible.rate * 60 if self._impossible else 0.0

    def unlisted_count(self) -> int:
        return len(self._unlisted)

    def pool_tokens(self) -> float:
        return self._pool.tokens

    def global_tokens(self) -> float:
        return self._global.tokens
