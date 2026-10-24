"""Per-worker token buckets that throttle declared crawlers (#761).

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
charged against every bucket it needs, and only when ALL have a token.
A refused request charges none, so a crawler that hits a wall does not
also drain the share left for its neighbours.

Multi-worker choice and its error bound: gunicorn runs sync workers,
one request per process, so a concurrency cap degenerates and a shared
counter would put an RPC on every crawler request. Each worker instead
holds its own float-token buckets refilling continuously at
`budget / web_workers` per minute (global 120, 8 workers, Applebot 10%:
12 a minute overall, 1.5 per worker). Over any window a bucket admits at
most that window's share of its rate plus its capacity, and capacity is
`BURST_SECONDS` (10) seconds of rate, or one token if that is more. So a
worker can burst ten seconds' worth at start and after an idle spell, and
a share below one request every ten seconds still admits one request
every `60 / rate` seconds instead of never. Summed over workers the
bound is the window's share of the budget plus ten seconds' worth. Per-
worker buckets under-admit when crawler traffic lands unevenly on the
workers (one worker's idle tokens cannot serve another's backlog).

Memory bound: the token is attacker-chosen text, so buckets for unlisted
crawlers are capped at `MAX_UNLISTED_BUCKETS`. A bucket that has been
idle for ten minutes and has refilled holds no state and is dropped. At
the cap, a further new token shares ONE overflow bucket at the default
percentage, so a flood of invented crawler names is throttled together
instead of each getting a fresh bucket.

No database, blob or render work happens on any path through here.
"""

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence

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
        "web.meta_sitemap",
        "web.maintainers_sitemap",
        "web.series_diff_sitemap",
        "web.inbox_sitemap",
        "web.archive_sitemap",
        "web.healthz",
        "web.readyz",
    }
)

_LOG_EVERY_SEC = 60

# A bucket holds this many seconds of its rate. A full minute (the
# textbook choice) lets a worker admit a whole minute's budget at once
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


class _Bucket:
    def __init__(self, per_minute: float, now: float):
        self.rate = per_minute / 60  # tokens per second
        self.capacity = max(per_minute * BURST_SECONDS / 60, 1.0)
        self.tokens = self.capacity
        self.stamp = now
        self.last_used = now
        self.deferred = 0  # since the last log line
        self.last_log: float | None = None

    def refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def has_token(self) -> bool:
        return self.tokens >= 1


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
    ):
        self.retry_after_sec = retry_after_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._workers = web_workers
        self._total = total_per_minute
        self._default_pct = default_ua_pct
        now = clock()
        self._global = _Bucket(total_per_minute / web_workers, now)
        # Dict order is match order: the first configured crawler found
        # in the User-Agent wins.
        self._listed = {
            name: (name.lower(), self._bucket(pct, now))
            for name, pct in ua_percent.items()
        }
        if markers is None:
            markers = Settings.model_fields["crawler_ua_markers"].default
        alternatives = "|".join(re.escape(m) for m in markers)
        self._token = (
            re.compile(rf"\b((?:[A-Za-z][\w.-]*)?(?:{alternatives}))/", re.IGNORECASE)
            if alternatives
            else None
        )
        self._unlisted: dict[str, _Bucket] = {}
        self._overflow = self._bucket(default_ua_pct, now)
        self._pool = self._bucket(unlisted_pool_pct, now)
        self._last_sweep = now

    def _bucket(self, pct: int, now: float) -> _Bucket:
        return _Bucket(self._total * pct / 100 / self._workers, now)

    @classmethod
    def from_settings(cls, s: Settings) -> CrawlerBudget:
        return cls(
            s.crawler_budget_per_minute,
            s.effective_crawler_ua_budgets,
            s.web_workers,
            s.crawler_retry_after_sec,
            markers=s.crawler_ua_markers,
            default_ua_pct=s.crawler_default_ua_pct,
            unlisted_pool_pct=s.crawler_unlisted_pool_pct,
        )

    def crawler_key(self, user_agent: str | None) -> str | None:
        """The bucket key for a crawler request, None for anything else:
        the dict key of a listed crawler, else the lowercased product
        token. Total 0 is the master switch, so nothing is a crawler."""
        if self._total <= 0 or not user_agent:
            return None
        ua = user_agent.lower()
        for name, (needle, _) in self._listed.items():
            if needle in ua:
                return name
        match = self._token.search(user_agent) if self._token else None
        return match.group(1).lower() if match else None

    def check(self, user_agent: str | None) -> str | None:
        """Return the crawler's bucket key when the request must be
        refused; None when it passes (not a crawler, or admitted and
        charged)."""
        key = self.crawler_key(user_agent)
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if key is None:
                return None
            own = self._bucket_for(key, now)
            own.last_used = now
            buckets = [own, self._global]
            if key not in self._listed:
                buckets.append(self._pool)
            for b in buckets:
                b.refill(now)
            if not all(b.has_token() for b in buckets):
                label = OVERFLOW_KEY if own is self._overflow else key
                self._note_deferred(label, own, now)
                return label
            for b in buckets:
                b.tokens -= 1
        return None

    def _bucket_for(self, key: str, now: float) -> _Bucket:
        # Caller holds the lock.
        if key in self._listed:
            return self._listed[key][1]
        bucket = self._unlisted.get(key)
        if bucket is None:
            if len(self._unlisted) >= MAX_UNLISTED_BUCKETS:
                return self._overflow
            bucket = self._unlisted[key] = self._bucket(self._default_pct, now)
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

    def _note_deferred(self, key: str, bucket: _Bucket, now: float) -> None:
        # Caller holds the lock. First deferral logs at once so an
        # operator sees the throttle start; then at most once a minute
        # per bucket, with the count since the previous line.
        bucket.deferred += 1
        if bucket.last_log is None or now - bucket.last_log >= _LOG_EVERY_SEC:
            logger.warning(
                "crawler budget: crawler=%s deferred=%d pid=%d",
                key,
                bucket.deferred,
                os.getpid(),
            )
            bucket.deferred = 0
            bucket.last_log = now

    def _find(self, key: str) -> _Bucket:
        return self._listed[key][1] if key in self._listed else self._unlisted[key]

    def ua_tokens(self, key: str) -> float:
        return self._find(key).tokens

    def ua_per_worker_per_minute(self, key: str) -> float:
        return self._find(key).rate * 60

    def unlisted_count(self) -> int:
        return len(self._unlisted)

    def pool_tokens(self) -> float:
        return self._pool.tokens

    def global_tokens(self) -> float:
        return self._global.tokens
