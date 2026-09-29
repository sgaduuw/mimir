"""Complexity guard for the `format=flowed` decoder.

Deliberately in its OWN file, outside `tests/test_flowed.py`, because
the mutation battery scores mutants on the pass/fail counts of that
file. A wall-clock test inside the scored set turns a timing flake
into a reported kill, which is the false-kill that closes an
investigation rather than the false-survival that merely wastes one.
"""

import time

from mimir.flowed import unflow


def test_unflow_is_linear_in_line_count():
    """`pending += ... + core` accumulates into a variable that the
    nested `flush()` closes over, so it is a cell (STORE_DEREF) and
    CPython's in-place string-concatenation specialisation, which
    only applies to STORE_FAST locals, does not fire. The join is
    therefore O(n^2) in the size of a soft-wrapped paragraph.

    `parse_message` runs on the read path for every message view, and
    caps the raw message at 50 MB, so a single crafted flowed body of
    a few MB pins a gunicorn worker to its 60 s timeout on every
    request.

    Scale-invariant assertion (a ratio, not a wall-clock budget) so
    this does not become a flaky machine-speed test. Linear work
    doubles; measured on this branch the ratio is ~3.4.
    """

    def build(n: int) -> str:
        return "\n".join(f"word{i:06d} " for i in range(n)) + "\nend"

    def best_of(n: int, rounds: int = 3) -> float:
        text = build(n)
        unflow(text)  # warm
        return min(_timed(text) for _ in range(rounds))

    def _timed(text: str) -> float:
        start = time.perf_counter()
        unflow(text)
        return time.perf_counter() - start

    small = best_of(30_000)
    large = best_of(60_000)
    # Precondition: the small run is long enough that timer noise is
    # not what the ratio measures.
    assert small > 0.005, f"baseline too fast to compare: {small}s"
    assert large / small < 2.6, (
        f"doubling the input multiplied the work by {large / small:.2f}x "
        f"({small * 1000:.1f}ms -> {large * 1000:.1f}ms); expected ~2x"
    )
