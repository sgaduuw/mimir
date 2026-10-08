"""Cost guards for the body renderer.

In their own file, outside `tests/test_rendering.py`, because the
mutation battery scores mutants on that file's results. A wall-clock
assertion inside the scored set turns a timing flake into a reported
KILL, which is the false kill that closes an investigation rather
than the false survival that merely wastes one.

Written by the reviewer that measured the amplification these bound.
"""

import timeit

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
    from the mirror on every read, so nothing caches the result).

    Times the fastest of five calls, not one: load and first-call
    effects only add time, so the minimum is the stable estimate. A
    single call failed on the free-threaded CI job (0.40 to 0.67 us per
    byte at 256 KiB, 2026-10-08) while the same code measures ~0.08 to
    0.11 on a laptop (3.14.8, GIL and free-threaded, 2026-10-08). The
    0.3 limit sits well above that and well below the 2.3 being caught.
    """
    body = _amplifier_body(size_kb * 1024)
    render_body(">" * 4 + "warm\n")  # warm imports
    micros_per_byte = (
        min(timeit.repeat(lambda: render_body(body), number=1, repeat=5))
        * 1e6
        / len(body)
    )
    assert micros_per_byte <= 0.3, (
        f"{micros_per_byte:.2f} us per input byte at {size_kb} KiB"
    )
