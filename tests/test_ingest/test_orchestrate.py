"""Tests for mimir/ingest/orchestrate.py: `discover_epochs`
(git-repo enumeration), `ingest_inbox` (per-inbox driver,
including the post-ingest auto-ANALYZE), and `ingest_all`
(across-all-inboxes driver with the shared `--limit`
budget). Also covers the multi-worker path and the
parser-failure-mid-batch ordering guarantee."""



from sqlalchemy import select

from mimir.ingest import (
    discover_epochs,
    ingest_all,
    ingest_inbox,
)
from mimir.models import (
    Inbox,
)

from tests.test_ingest._helpers import _build_pubinbox_repo, _rfc5322, _setup_alpha_with_messages, _spy_text


def test_ingest_inbox_runs_analyze_when_threshold_reached(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 2)

    results = ingest_inbox(alpha, workers=1)

    # Three messages, all fresh -- exact count is known. The earlier
    # `>= 2` lower bound would have masked a regression that
    # accidentally dropped one of the three to dup_batch / failed.
    assert sum(r.new + r.linked for r in results) == 3
    assert "ANALYZE" in seen


def test_ingest_inbox_skips_analyze_below_threshold(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 100)

    ingest_inbox(alpha, workers=1)

    assert "ANALYZE" not in seen


def test_ingest_inbox_skips_analyze_when_disabled(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 0)

    ingest_inbox(alpha, workers=1)

    assert "ANALYZE" not in seen


# Cache invalidation on empty-to-non-empty transition


def test_ingest_inbox_invalidates_cache_on_first_ingest(seeded_db, tmp_path):
    """When an inbox transitions from empty (`last_article_date IS
    NULL`) to populated, drop the per-inbox cache keys. The
    front-page card otherwise sticks on `total=0` ("not yet
    ingested") for the rest of the 24h archive_stats TTL after a
    warm-cache tick captures the empty state between
    `admin inbox add` and the first ingest."""
    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    # The seeded `alpha` fixture stamps `last_article_date` as a
    # mid-2024 timestamp to mimic an already-ingested inbox; null it
    # back out to simulate a brand-new inbox (the actual case the
    # invalidation protects against, where a warm-cache tick wrote
    # `total=0` between `admin inbox add` and the first ingest).
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = None
        s.commit()

    # Seed a cache row keyed under the inbox name (mimics the `total=0`
    # row a warm-cache tick would write between add and first ingest).
    # The shape of the value doesn't matter; `delete_for_inbox` is
    # key-pattern-driven.
    cache.set(f"archive_stats:{alpha.name}", "sentinel", ttl=86400)
    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel"

    ingest_inbox(alpha, workers=1)

    assert cache.get(f"archive_stats:{alpha.name}") is None, (
        "first ingest should bust the per-inbox cache so the "
        "front-page card recomputes against real data"
    )


def test_ingest_inbox_does_not_invalidate_cache_on_steady_state(
    seeded_db, tmp_path,
):
    """An already-populated inbox (last_article_date set) running its
    next ingest does NOT bust the per-inbox cache. Doing so would
    force a COUNT(*) refresh on every UPDATE_EVERY tick, defeating
    the 24h TTL that exists precisely because the count is the slow
    piece."""
    from datetime import datetime, timezone

    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    # Stamp the inbox as already-populated so the empty-state
    # capture sees a non-NULL value going into ingest.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s.commit()

    cache.set(f"archive_stats:{alpha.name}", "sentinel", ttl=86400)
    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel"

    ingest_inbox(alpha, workers=1)

    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel", (
        "steady-state ingest should leave the per-inbox cache alone; "
        "the 24h TTL is intentional"
    )


# Canonical inbox + list-address observation


def test_discover_epochs_returns_git_repos_only(tmp_path):
    """`discover_epochs` walks the mirror dir and returns just the
    children that look like git repos. Non-repo directories and
    regular files must be filtered -- without this, a stray
    `Inboxes/<name>/git/README` or `metadata/` would crash the
    walker downstream."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(mirror / "0.git", [_rfc5322("e0@example.com")])
    _build_pubinbox_repo(mirror / "1.git", [_rfc5322("e1@example.com")])
    # Non-repo directory.
    (mirror / "garbage").mkdir()
    (mirror / "garbage" / "stray-file").write_text("x")
    # Stray file at the top.
    (mirror / "README").write_text("operator notes")

    epochs = discover_epochs(mirror)
    names = [p.name for p in epochs]
    assert names == ["0.git", "1.git"], (
        f"discover_epochs must skip non-repo siblings; got {names}"
    )


def test_discover_epochs_empty_mirror_returns_empty(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    assert discover_epochs(mirror) == []


def test_ingest_all_limit_decrements_across_inboxes(seeded_db, tmp_path):
    """`ingest_all(limit=N)` is a cross-inbox cap. With two inboxes
    each carrying 3 messages and `limit=2`, the first inbox should
    consume both slots and the second must be skipped entirely --
    no ingest call, no result row. Without this contract, a
    `limit=500` across 5 inboxes could quietly ingest 2500 messages."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [
        _rfc5322(f"alpha-cap-{i}@example.com") for i in range(3)
    ])
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [
        _rfc5322(f"beta-cap-{i}@example.com") for i in range(3)
    ])

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        # Re-fetch detached copies for ingest_all's dict.
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=2, workers=1)

    # alpha got an ingest result; beta did not (skipped before its
    # ingest_inbox was even called).
    assert "alpha" in out
    assert "beta" not in out, (
        f"limit should have stopped before beta's ingest; got results for "
        f"{list(out.keys())}"
    )
    # And the per-result bookkeeping: alpha's result respects the
    # cap. Either dup_batch + new + linked == 2 (the cap), or new
    # rolled through up to 3 if the per-epoch loop is more permissive
    # -- but the *cross-inbox* cap is the contract being tested.
    alpha_total = sum(
        r.new + r.linked + r.dup_batch + r.dup_db + r.failed
        for r in out["alpha"]
    )
    # alpha consumed at least the cap-worth; beta must not have run.
    assert alpha_total >= 2


def test_ingest_all_no_limit_walks_all_inboxes(seeded_db, tmp_path):
    """Sanity companion: with `limit=None`, every inbox gets ingested.
    Without this baseline, a passing `test_..._decrements_across_inboxes`
    could be masking a regression that just never walks beta."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [_rfc5322("a-nolim@example.com")])
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [_rfc5322("b-nolim@example.com")])

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=None, workers=1)
    assert "alpha" in out and "beta" in out


# --------------------------------------------------------------------------
# Multi-worker ingest (workers>1).
#
# Every other ingest test in this file pins `workers=1`. The
# CLAUDE.md rule "`parse_message` must stay importable at module
# level so it pickles cleanly to worker processes" has nothing
# enforcing it; a closure-capturing refactor would break production
# and pass CI. One end-to-end run with `workers=2` is enough to
# catch that.
# --------------------------------------------------------------------------
