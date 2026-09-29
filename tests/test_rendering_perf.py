"""Cost guards for the body renderer.

In their own file, outside `tests/test_rendering.py`, because the
mutation battery scores mutants on that file's results. A wall-clock
assertion inside the scored set turns a timing flake into a reported
KILL, which is the false kill that closes an investigation rather
than the false survival that merely wastes one.

Written by the reviewer that measured the amplification these bound.
"""

import time

import pytest

from mimir.rendering import render_body
from mimir.rendering.body import MAX_QUOTE_DEPTH


def _amplifier_body(target_bytes: int) -> str:
    """Worst measured shape: independent single-line quote blocks at
    one level below the cap, separated by blank lines so each is its
    own block and each pays the full `<details>` chain."""
    group = ">" * (MAX_QUOTE_DEPTH - 1) + "x\n\n"
    n = max(1, target_bytes // len(group))
    return group * n


@pytest.mark.parametrize("size_kb", [64, 256])
def test_render_body_stays_sublinear_in_wall_time_per_kib(size_kb):
    """Cost per input byte must not scale with the cap. Measured on
    this branch at ~2.3 us per input byte (a 1 MB body costs ~2.4 s of
    CPU on one gunicorn worker, and the message body is re-derived
    from the mirror on every read, so nothing caches the result)."""
    body = _amplifier_body(size_kb * 1024)
    render_body(">" * 4 + "warm\n")  # warm imports
    t0 = time.perf_counter()
    render_body(body)
    micros_per_byte = (time.perf_counter() - t0) * 1e6 / len(body)
    assert micros_per_byte <= 0.3, (
        f"{micros_per_byte:.2f} us per input byte at {size_kb} KiB"
    )
