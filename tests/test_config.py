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

import os

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


def test_settings_trees_seeds_curated_defaults_on_fresh_deploy(monkeypatch):
    """When neither TREES__* nor legacy MAINLINE_TREE_URL/PATH is set,
    the validator seeds the full 7-tree curated default set."""
    for k in list(os.environ):
        if k.upper().startswith("TREES__") or k.startswith("MAINLINE_TREE"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from mimir.config import Settings

    s = Settings()
    assert set(s.trees.keys()) == {
        "linus",
        "linux-next",
        "net-next",
        "tip",
        "pci",
        "mm",
        "bpf-next",
    }
    assert s.trees["linus"].walk_every_seconds == 1500  # 25 min
    assert s.trees["linux-next"].rebases is True
    assert s.trees["mm"].branch == "mm-stable"


def test_settings_trees_legacy_env_seeds_only_linus(monkeypatch):
    """When MAINLINE_TREE_URL / MAINLINE_TREE_PATH are set and
    TREES__* is unset, only the linus entry is seeded from
    the legacy scalars. The curated extras stay opted out."""
    for k in list(os.environ):
        if k.upper().startswith("TREES__"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("MAINLINE_TREE_URL", "https://example.com/legacy.git")
    monkeypatch.setenv("MAINLINE_TREE_PATH", "Mainline/legacy.git")
    from mimir.config import Settings

    s = Settings()
    assert set(s.trees.keys()) == {"linus"}
    assert s.trees["linus"].url == "https://example.com/legacy.git"
    assert s.trees["linus"].walk_every_seconds == 1500


def test_settings_trees_env_override_wins(monkeypatch):
    """When TREES__* env vars are set, take env as-is. Curated defaults
    are NOT merged in. Slug `bcachefs` (no underscore) passes through
    unchanged (the normaliser only converts underscore-to-dash for
    slugs that were env-encoded with underscores in place of dashes)."""
    for k in list(os.environ):
        if k.upper().startswith("TREES__") or k.startswith("MAINLINE_TREE"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv(
        "TREES__bcachefs__URL",
        "https://example.com/bcachefs.git",
    )
    monkeypatch.setenv("TREES__bcachefs__PATH", "trees/bcachefs.git")
    from mimir.config import Settings

    s = Settings()
    assert set(s.trees.keys()) == {"bcachefs"}


def test_settings_has_broker_read_pool_size_default():
    """Default to os.cpu_count() so the pool sizes itself to the host by
    default. Operators override via BROKER_READ_POOL_SIZE."""
    s = Settings(secret_key="test-secret-key-not-real-12345678")
    assert s.broker_read_pool_size == (os.cpu_count() or 4)


def test_settings_has_broker_writer_queue_depth_default():
    """Default 256 absorbs ~2-3 s of burst at typical write rates per the
    Phase 1 spec. Operators override via BROKER_WRITER_QUEUE_DEPTH."""
    s = Settings(secret_key="test-secret-key-not-real-12345678")
    assert s.broker_writer_queue_depth == 256


def test_settings_broker_writer_queue_depth_env_override(monkeypatch):
    monkeypatch.setenv("BROKER_WRITER_QUEUE_DEPTH", "1024")
    s = Settings(secret_key="test-secret-key-not-real-12345678")
    assert s.broker_writer_queue_depth == 1024


def test_settings_has_mainline_commit_batch_size_default():
    """Default 100 matches the Phase 3 spec walkthrough's batch
    size. Operators tune via MAINLINE_COMMIT_BATCH_SIZE."""
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.mainline_commit_batch_size == 100


def test_settings_mainline_commit_batch_size_env_override(monkeypatch):
    monkeypatch.setenv("MAINLINE_COMMIT_BATCH_SIZE", "50")
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.mainline_commit_batch_size == 50


def test_settings_has_ingest_batch_flush_seconds_default():
    """Default 0.5 s preserves the pre-Phase-3b COMMIT_EVERY_SECONDS value.
    Operators tune via INGEST_BATCH_FLUSH_SECONDS."""
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.ingest_batch_flush_seconds == 0.5


def test_settings_ingest_batch_flush_seconds_env_override(monkeypatch):
    monkeypatch.setenv("INGEST_BATCH_FLUSH_SECONDS", "2.5")
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.ingest_batch_flush_seconds == 2.5


def test_settings_has_related_discussions_window_days_default():
    """Default 365 bounds the related-discussions candidate walk to one year
    of the date index; a silently changed default would change both panel
    recall and cold-compute cost (#71)."""
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.related_discussions_window_days == 365


def test_settings_related_discussions_window_days_env_override(monkeypatch):
    monkeypatch.setenv("RELATED_DISCUSSIONS_WINDOW_DAYS", "90")
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.related_discussions_window_days == 90


def test_settings_has_related_discussions_bot_senders_default():
    """Default denylist carries the three high-volume kernel bots
    (syzbot, kernel test robot, tip-bot); their threads are excluded
    from related-discussions candidates and suppress the panel on
    bot-authored roots (#71 follow-up)."""
    from mimir.config import Settings

    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.related_discussions_bot_senders == [
        "syzkaller.appspotmail.com",
        "lkp@intel.com",
        "tip-bot2@linutronix.de",
    ]


def test_settings_related_discussions_bot_senders_env_override(monkeypatch):
    from mimir.config import Settings

    monkeypatch.setenv("RELATED_DISCUSSIONS_BOT_SENDERS", "a@x.test,b@y.test")
    s = Settings(SECRET_KEY="test-secret-key-not-real-12345678")
    assert s.related_discussions_bot_senders == ["a@x.test", "b@y.test"]
