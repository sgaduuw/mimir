"""Unit tests for `mimir.datetime_utils.aware_utc`.

Pins the contract that callers across ingest, patch_state, and
web/urls all rely on: naive input → aware UTC; aware input → no-op.
The three pre-centralisation copies had small wording differences
in their docstrings; this file is the single source of behaviour
truth now.
"""

from datetime import datetime, timezone, timedelta

import pytest

from mimir.datetime_utils import aware_utc


def test_aware_utc_replaces_missing_tzinfo_with_utc():
    naive = datetime(2024, 6, 1, 12, 0, 0)
    out = aware_utc(naive)
    assert out.tzinfo == timezone.utc
    # Wall-clock fields are untouched -- the helper attaches UTC,
    # it does NOT shift the value (since "no TZ" by convention is
    # already taken to mean UTC across this codebase).
    assert out.replace(tzinfo=None) == naive


def test_aware_utc_is_noop_on_already_aware_datetime():
    aware = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    out = aware_utc(aware)
    assert out is aware


def test_aware_utc_preserves_non_utc_tzinfo():
    """An already-tz-aware datetime in a non-UTC zone is returned
    unchanged: the helper's job is to fix NAIVE inputs, not to
    coerce other zones into UTC. Callers that need a UTC value
    pass it through `.astimezone(timezone.utc)` themselves."""
    pacific = timezone(timedelta(hours=-8))
    aware = datetime(2024, 6, 1, 12, 0, 0, tzinfo=pacific)
    out = aware_utc(aware)
    assert out is aware
    assert out.utcoffset() == timedelta(hours=-8)


def test_aware_utc_raises_attributeerror_on_none():
    """The helper signature is `(datetime) -> datetime`. Passing
    None would mask a missing-data bug at the call site; the
    three pre-centralisation copies all expected non-None inputs
    and callers handled None explicitly (`x if x else None`)."""
    with pytest.raises(AttributeError):
        aware_utc(None)  # type: ignore[arg-type]
