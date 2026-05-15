"""Validation contracts on `mimir.config.Settings`.

The module-level `settings = Settings()` instance is built at import
time, so a misconfigured `.env` fails the app before it can serve a
request, which is the right posture for a long-running service. The
constraint that protects production from accidentally shipping a
weak / empty Flask `secret_key` lives in a pydantic `Field(min_length=...)`
declaration, with no separate test covering it. A future refactor
that dropped the constraint (e.g. moving `secret_key` to `Optional`
or relaxing the length) would silently let `flask --app mimir run`
boot with a short or blank secret. Pin the constraint directly.
"""
import pytest
from pydantic import ValidationError

from mimir.config import Settings


def test_settings_rejects_short_secret_key():
    """`Settings.secret_key` carries `Field(min_length=16)`. A value
    below that length must raise `ValidationError` at construction
    time, pydantic-settings doesn't print a separate warning, it
    just throws, so the import-time failure is what protects the
    deploy from a weak session-signing key."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(secret_key="short")
    # The error must specifically reference the length constraint;
    # a generic raise would mean some other validator caught it,
    # not the one we care about.
    err_str = str(exc_info.value)
    assert "secret_key" in err_str
    assert "16" in err_str or "at least" in err_str.lower()


def test_settings_accepts_exactly_min_length_secret_key():
    """The boundary: 16 chars is fine, 15 isn't. Pin the exact
    threshold so a regression that bumped `min_length` to 17 (or
    dropped it to 8) gets caught."""
    s = Settings(secret_key="a" * 16)
    assert s.secret_key == "a" * 16

    with pytest.raises(ValidationError):
        Settings(secret_key="a" * 15)
