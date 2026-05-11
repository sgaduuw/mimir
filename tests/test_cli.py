"""Click-level surface for the `admin inbox trackers` group plus the
tracker-aware columns in `admin inbox list`. Service-layer behaviour
is covered by `tests/test_inboxes.py`; these tests pin the CLI
shape — argument parsing, exit codes, output strings — so a future
refactor of the click wiring doesn't silently regress operator UX.
"""
from click.testing import CliRunner

from mimir.cli import (
    admin_inbox_list_command,
    admin_inbox_trackers_add_command,
    admin_inbox_trackers_clear_command,
    admin_inbox_trackers_remove_command,
    admin_inbox_trackers_set_command,
    admin_inbox_trackers_show_command,
    update_command,
    warm_cache_command,
)
from mimir.inboxes import get_inbox, set_tracked_authors


def test_trackers_show_no_trackers(seeded_db):
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["alpha"])
    assert result.exit_code == 0
    assert "no trackers configured" in result.output


def test_trackers_show_with_trackers(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["alpha"])
    assert result.exit_code == 0
    assert "2 tracker(s)" in result.output
    assert "Linus" in result.output
    assert "torvalds@" in result.output
    assert "Greg" in result.output


def test_trackers_show_unknown_inbox(seeded_db):
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["nope"])
    assert result.exit_code != 0
    assert "no inbox" in result.output


def test_trackers_set_replaces_dict(seeded_db):
    set_tracked_authors("alpha", {"old": "stale@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command,
        ["alpha", "Linus=torvalds@", "Greg=gregkh@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {
        "Linus": "torvalds@", "Greg": "gregkh@",
    }


def test_trackers_set_rejects_malformed_pair(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command, ["alpha", "missing-equals"],
    )
    assert result.exit_code != 0
    assert "LABEL=SUBSTRING" in result.output


def test_trackers_set_keeps_value_with_embedded_equals(seeded_db):
    """A value containing `=` should survive the split-on-first-`=`."""
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command,
        ["alpha", "Weird=foo=bar@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Weird": "foo=bar@"}


def test_trackers_add_initializes_null(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_trackers_add_command,
        ["alpha", "Linus", "torvalds@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_trackers_add_appends(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_add_command,
        ["alpha", "Greg", "gregkh@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {
        "Linus": "torvalds@", "Greg": "gregkh@",
    }


def test_trackers_remove_drops_label(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_remove_command, ["alpha", "Greg"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_trackers_remove_missing_label_fails(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_remove_command, ["alpha", "Greg"],
    )
    assert result.exit_code != 0
    assert "no tracker labelled" in result.output


def test_trackers_clear_writes_null(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_clear_command, ["alpha"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors is None


def test_admin_inbox_list_shows_tracker_count(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(admin_inbox_list_command, [])
    assert result.exit_code == 0
    # alpha has two trackers, beta has none.
    lines = result.output.strip().splitlines()
    alpha_line = next(line for line in lines if " alpha " in line)
    beta_line = next(line for line in lines if " beta " in line)
    assert "trackers=2" in alpha_line
    assert "trackers=none" in beta_line


def test_warm_cache_default_emits_only_summary(seeded_db):
    """Default verbosity collapses per-key timings into one summary
    line so the scheduler log doesn't scale with inbox count."""
    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert any(line.startswith("warm-cache:") and "ms total" in line for line in lines), result.output
    # No per-key timing lines (those end with "<n> ms" without "total").
    per_key = [
        line for line in lines
        if line.endswith(" ms") and "ms total" not in line
    ]
    assert per_key == [], f"unexpected per-key lines at default verbosity: {per_key}"


def test_warm_cache_verbose_keeps_per_key_timings(seeded_db):
    """-v restores the per-key timings on top of the summary line."""
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    # Each seeded inbox (alpha, beta) gets at least one per-key line
    # under -v. Pick a label that's stable across builds. The DB also
    # has bootstrap-time `Settings.inboxes` entries (lkml etc.) at this
    # point — fine, we don't assert on the inbox count.
    assert "alpha archive_stats" in result.output
    assert "beta archive_stats" in result.output
    # Summary line is still there.
    assert "warm-cache:" in result.output and "ms total" in result.output


def test_warm_cache_includes_atom_feed_sources(seeded_db):
    """The atom routes use `recent_articles(limit=FEED_ENTRY_LIMIT)`
    and `author_recent(..., limit=FEED_ENTRY_LIMIT)` — a different
    cache key from the dashboard's `limit=5/10` calls. Warm both so
    the first feed poll per hour returns a cache-hit too."""
    from mimir.inboxes import set_tracked_authors
    set_tracked_authors("alpha", {"Examples": "example.com"})
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    # Recent feed flavour for each seeded inbox.
    assert "alpha recent_articles (50)" in result.output
    assert "beta recent_articles (50)" in result.output
    # Tracker tile + feed flavour distinct lines.
    assert "alpha tracker:Examples" in result.output
    assert "alpha tracker:Examples (feed)" in result.output


def test_warm_cache_skips_sitemap_when_site_base_url_unset(
    seeded_db, monkeypatch
):
    """Without SITE_BASE_URL, sitemap renders rely on `request.url_root`
    which isn't available from the CLI. Warm-cache skips them rather
    than poison the cache with relative-looking URLs."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "site_base_url", "")
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    assert "sitemap:index" not in result.output
    assert "sitemap:meta" not in result.output
    assert "sitemap:inbox:" not in result.output


def test_warm_cache_includes_sitemap_when_site_base_url_set(
    seeded_db, monkeypatch
):
    """With SITE_BASE_URL set, warm-cache pre-renders the three
    sitemap surfaces — index, meta, and per-inbox — so the first
    crawler hit per hour gets a cache-hit."""
    from sqlalchemy import delete
    from mimir import cache
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    # Clean slate so we can assert the entries exist post-run.
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()

    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    assert "sitemap:index" in result.output
    assert "sitemap:meta" in result.output
    assert "sitemap:inbox:alpha" in result.output
    assert "sitemap:inbox:beta" in result.output

    # Cache rows actually landed and decode to well-formed XML with
    # the right schema-namespaced root element. A "<?xml" prefix
    # check alone would pass on a malformed document.
    import xml.etree.ElementTree as ET
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    expected_root = {
        "sitemap:index": "{%s}sitemapindex" % ns,
        "sitemap:meta": "{%s}urlset" % ns,
        "sitemap:inbox:alpha": "{%s}urlset" % ns,
        "sitemap:inbox:beta": "{%s}urlset" % ns,
    }
    for key, expected_tag in expected_root.items():
        body = cache.get(key)
        assert body is not None, f"missing cache row for {key}"
        assert "example.test" in body, (
            f"cache row for {key} doesn't carry the SITE_BASE_URL prefix; "
            f"warm-cache may have run with the wrong base"
        )
        root = ET.fromstring(body)
        assert root.tag == expected_tag, (
            f"cache row for {key} has wrong root element: {root.tag!r} "
            f"(expected {expected_tag!r})"
        )


def test_warm_cache_sitemap_helpers_force_recompute(seeded_db):
    """Passing force=True to the sitemap helpers must overwrite the
    cached value, even when a live (unexpired) row exists. Without
    this, warm-cache after a fresh ingest wouldn't see the new
    article URLs until the 1h TTL elapsed."""
    from sqlalchemy import select
    from mimir import cache
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.web import inbox_sitemap_xml

    cache.set("sitemap:inbox:alpha", "STALE", ttl=3600)
    assert cache.get("sitemap:inbox:alpha") == "STALE"
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        body = inbox_sitemap_xml(s, alpha, "https://example.test", force=True)
    assert "<?xml" in body
    assert cache.get("sitemap:inbox:alpha") == body


def test_update_default_silent_on_no_op(seeded_db, monkeypatch):
    """No-op ticks (no upstream changes, no new commits to ingest)
    must not emit per-inbox / per-epoch lines at default verbosity —
    that's what makes the scheduler log readable as inbox count grows."""
    from mimir import cli, sync as sync_mod
    from mimir.ingest import IngestResult

    def _fake_sync(*_a, **_kw):
        return sync_mod.SyncResult(cloned=[], fetched=[], failed=[])
    def _fake_ingest_all(inboxes, workers):
        return {
            name: [IngestResult(
                epoch="0.git", new=0, linked=0, dup_batch=3,
                dup_db=2, failed=0, last_commit_sha="aa" * 20,
            )]
            for name in inboxes
        }
    monkeypatch.setattr(cli, "sync_epochs", _fake_sync)
    monkeypatch.setattr(cli, "ingest_all", _fake_ingest_all)

    result = CliRunner().invoke(update_command, [])
    assert result.exit_code == 0
    assert "sync:" not in result.output, result.output
    assert "/0.git:" not in result.output, result.output


def test_update_verbose_prints_no_op_lines(seeded_db, monkeypatch):
    """-v restores per-inbox / per-epoch lines even when nothing changed."""
    from mimir import cli, sync as sync_mod
    from mimir.ingest import IngestResult

    monkeypatch.setattr(
        cli, "sync_epochs",
        lambda *_a, **_kw: sync_mod.SyncResult(cloned=[], fetched=[], failed=[]),
    )
    monkeypatch.setattr(
        cli, "ingest_all",
        lambda inboxes, workers: {
            name: [IngestResult(
                epoch="0.git", new=0, linked=0, dup_batch=1,
                dup_db=1, failed=0, last_commit_sha="bb" * 20,
            )]
            for name in inboxes
        },
    )

    result = CliRunner().invoke(update_command, ["-v"])
    assert result.exit_code == 0
    assert "sync: cloned=[] fetched=[] failed=[]" in result.output
    assert "/0.git: new=0 linked=0" in result.output
