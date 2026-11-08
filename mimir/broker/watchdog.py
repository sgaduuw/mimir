"""Stuck-job watchdog: report broker jobs that outlive their caller.

A worker logs an RPC only once it has finished (`_worker_loop`'s
slow-RPC WARNING), so a job that never finishes never logs. The
client's timeout ends the caller's wait and not the job. On
2026-11-08 two warm workers hung for six hours while one held a read
transaction that pinned the WAL. The only sign was one scheduler line
that did not say which request had hung (#1123).

Each worker records the job it is running in an `InFlightJobs`
registry. A watchdog thread wakes every `POLL_SEC` and logs a WARNING
for each job that has run past its op's client timeout. That is the
point where the caller has given up and nobody else will report it.
The WARNING repeats every `broker_stuck_job_warn_interval_sec` while
the job runs, so a job stuck for hours keeps showing up in the log
and is not a single line scrolled away.

The threshold is the op's own `timeout` ClassVar, the same value
`BrokerClient.call` waits for, so "the caller timed out" and "the
broker reports it" are one predicate. Requests are named with
`protocol.describe_request`, the function the client's timeout error
uses, so the two log lines can be matched.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass

from mimir.broker.handlers import _DISPATCH
from mimir.broker.protocol import _BrokerRequest, describe_request

logger = logging.getLogger(__name__)

# How often the watchdog looks. Detection lags a threshold crossing
# by at most this much; with thresholds of minutes, seconds are noise.
POLL_SEC = 5.0


@dataclass
class _Job:
    worker_tag: str
    line: bytes
    started: float
    # When the last WARNING for this job went out; None until the first.
    warned_at: float | None = None
    # Filled by `_identify` once the job outlives the shortest op
    # timeout. Not at `start`: nearly every job finishes in
    # milliseconds, and a `cache_set` line can be megabytes, so
    # parsing each one a second time (the reader already did) would
    # tax every request to watch the rare one that hangs.
    threshold: float | None = None
    description: str | None = None


def _identify(job: _Job) -> None:
    """Parse the job's wire line once for its op's client timeout and
    its name. A line that is not a JSON object with a known op gets
    the base timeout, and its leading bytes stand in for a name, so
    a malformed request is still watched."""
    try:
        fields = json.loads(job.line)
    except ValueError:
        fields = None
    if not isinstance(fields, dict):
        job.threshold = _BrokerRequest.timeout
        job.description = repr(job.line[:80])
        return
    entry = _DISPATCH.get(fields.get("op"))
    job.threshold = (entry[0] if entry is not None else _BrokerRequest).timeout
    job.description = describe_request(fields)


class InFlightJobs:
    """What each broker worker thread is running right now, keyed by
    thread name. Workers call `start` before dispatch and `finish`
    after; the watchdog calls `check`. One lock, held for dict
    operations only, never across a dispatch or a log call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}

    def start(self, worker_tag: str, line: bytes) -> None:
        job = _Job(worker_tag=worker_tag, line=line, started=time.monotonic())
        with self._lock:
            self._jobs[threading.current_thread().name] = job

    def finish(self) -> None:
        with self._lock:
            self._jobs.pop(threading.current_thread().name, None)

    def check(self, repeat_sec: float, now: float | None = None) -> int:
        """Log a WARNING for each job past its threshold that has not
        been warned about in the last `repeat_sec`. Returns how many
        WARNINGs it logged. `now` is injectable so a test can step the
        clock instead of sleeping through a 300 s threshold."""
        if now is None:
            now = time.monotonic()
        # No op waits less than this, so a younger job cannot be due
        # and is skipped without being parsed. Read per check rather
        # than once, so it follows the dispatch table as it is.
        floor = min(
            _BrokerRequest.timeout, *(model.timeout for model, _h in _DISPATCH.values())
        )
        with self._lock:
            old = [
                (name, job)
                for name, job in self._jobs.items()
                if now - job.started >= floor
            ]
        due: list[tuple[str, _Job]] = []
        for thread_name, job in old:
            # Only this thread writes `threshold`, `description` and
            # `warned_at`; workers only add and remove whole jobs.
            if job.threshold is None:
                _identify(job)
            if now - job.started < job.threshold:
                continue
            if job.warned_at is not None and now - job.warned_at < repeat_sec:
                continue
            job.warned_at = now
            due.append((thread_name, job))
        for thread_name, job in due:
            logger.warning(
                "broker stuck job [%s] on %s: running %.0fs, past its %.0fs "
                "client timeout: %s",
                job.worker_tag,
                thread_name,
                now - job.started,
                job.threshold,
                job.description,
            )
        return len(due)

    def run(self, stop_event: threading.Event, repeat_sec: float) -> None:
        """Watchdog thread body: `check` every `POLL_SEC` until stop.
        A failure in one check is logged and the loop continues; a
        dead watchdog would bring back the silence it exists to end."""
        while not stop_event.wait(POLL_SEC):
            try:
                self.check(repeat_sec)
            except Exception:
                logger.exception("broker watchdog: check failed, continuing")
