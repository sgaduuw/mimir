"""Click-level surface for every command registered in `mimir.cli`.

Service-layer behaviour is covered by `tests/test_inboxes.py`,
`tests/test_ingest.py`, and `tests/test_sync.py`; these tests pin
the CLI shape — argument parsing, exit codes, output strings,
ClickException branches — so a future refactor of the click wiring
doesn't silently regress operator UX.

For commands that have to read real blobs (`show`, `reindex`,
`ingest`), the test builds a tiny public-inbox v2 repo into
`tmp_path` via `_build_pubinbox_repo` and re-points the seeded
inbox row at it.
"""
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import (
    admin_canonicals_backfill_command,
    admin_failures_list_command,
    admin_failures_replay_command,
    admin_inbox_add_command,
    admin_inbox_list_command,
    admin_inbox_remove_command,
    admin_inbox_show_command,
    admin_inbox_trackers_add_command,
    admin_inbox_trackers_clear_command,
    admin_inbox_trackers_remove_command,
    admin_inbox_trackers_set_command,
    admin_inbox_trackers_show_command,
    admin_inbox_update_command,
    analyze_command,
    dev_seed_thread_command,
    ingest_command,
    init_db_command,
    reindex_command,
    show_command,
    update_command,
    vacuum_command,
    warm_cache_command,
)
from mimir.inboxes import get_inbox, set_tracked_authors


def test_configure_logging_actually_changes_level_on_re_invocation():
    """`logging.basicConfig` is a no-op after the first call. The CLI
    used to call it on every invocation, so a quiet first run pinned
    the level for the whole process and subsequent `-vv` runs silently
    kept WARNING. Pin the level-flip contract: two calls with
    different verbose values must produce different effective levels.
    Audit (2026-05-15)."""
    import logging

    from mimir.cli import _configure_logging

    root = logging.getLogger()
    original_level = root.level
    try:
        _configure_logging(0)  # WARNING
        first = root.level
        _configure_logging(2)  # DEBUG
        second = root.level

        assert first == logging.WARNING
        assert second == logging.DEBUG
        assert first != second, (
            "second _configure_logging call didn't change the root "
            "level -- basicConfig idempotency regressed"
        )
    finally:
        root.setLevel(original_level)


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


def test_warm_cache_workers_one_serial_path(seeded_db):
    """`--workers 1` forces the single-threaded path that bypasses the
    thread pool. Behavior must match the parallel path: every key
    still gets warmed, summary line still emits."""
    result = CliRunner().invoke(warm_cache_command, ["--workers", "1", "-v"])
    assert result.exit_code == 0
    per_key_lines = [
        line for line in result.output.splitlines()
        if line.endswith(" ms") and "ms total" not in line
    ]
    assert len(per_key_lines) >= 5, result.output
    assert "warm-cache:" in result.output and "ms total" in result.output


def test_warm_cache_parallel_propagates_refresh_window(seeded_db):
    """Worker threads must inherit the `refresh_window` contextvar
    via `copy_context()`. If they don't, a fresh-but-near-expiry
    cache row would *not* recompute under load — silently undoing
    Phase 2's TTL-aware refresh once Phase 3 fans out across workers.

    Pre-seed a near-expiry row for one warm target's key, then run
    warm-cache. The row's `expires_at` must move forward (recompute
    happened) instead of staying near-now (skipped)."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    # active_threads target for alpha; key matches dashboard helper.
    key = "active_threads:alpha:7:10"
    near_expiry = int(datetime.now(timezone.utc).timestamp()) + 30  # 30 s left
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.add(CacheEntry(key=_ns(key), value="[]", expires_at=near_expiry))
        s.commit()

    result = CliRunner().invoke(warm_cache_command, ["--workers", "4"])
    assert result.exit_code == 0

    with SessionLocal() as s:
        row = s.execute(
            select(CacheEntry).where(CacheEntry.key == _ns(key))
        ).scalar_one()
    # If the worker thread had no refresh_window, the still-live
    # 30s-remaining row would have short-circuited and `expires_at`
    # would still be near `near_expiry`. Successful refresh pushes
    # it out by the full TTL.
    assert row.expires_at > near_expiry + 60, (
        f"expected refresh to extend expires_at well past {near_expiry}, "
        f"got {row.expires_at}; refresh_window contextvar may not have "
        f"propagated to worker threads"
    )


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
    """-v restores the per-key timings on top of the summary line.

    Structural: each seeded inbox must get *multiple* per-key timing
    lines (one per cached helper -- archive_stats, active_threads,
    daily_volume, etc.). Pinning the literal label names ties the
    test to incidental cache-key strings; asserting "each inbox got
    several timings" catches the only regression that matters --
    warm-cache stopped iterating an inbox -- without flagging on a
    benign rename."""
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    per_key_lines = [
        line for line in result.output.splitlines()
        if line.endswith(" ms") and "ms total" not in line
    ]
    alpha_lines = [line for line in per_key_lines if line.startswith("alpha ")]
    beta_lines = [line for line in per_key_lines if line.startswith("beta ")]
    # Lower bound, not exact: more helpers may join the warm-cache
    # rotation; fewer means an inbox stopped being iterated.
    assert len(alpha_lines) >= 5, (
        f"alpha got only {len(alpha_lines)} per-key lines: {alpha_lines}"
    )
    assert len(beta_lines) >= 5, (
        f"beta got only {len(beta_lines)} per-key lines: {beta_lines}"
    )
    # Summary line is still there.
    assert "warm-cache:" in result.output and "ms total" in result.output


def test_warm_cache_includes_subsystem_dashboard_targets(seeded_db):
    """Per-subsystem dashboard helpers (recent_articles_in_subsystem +
    three siblings) drive the slowest cold-load page. warm-cache must
    iterate the top-N subsystems per inbox so the second visitor lands
    on a warmed cache."""
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    assert "alpha subsystem dashboards (top 20)" in result.output
    assert "beta subsystem dashboards (top 20)" in result.output


def test_warm_cache_subsystem_dashboards_populate_cache(seeded_db):
    """When an inbox has at least one most-active subsystem, the warm
    target writes cache rows for all four per-subsystem helpers. A
    silent regression in the helper composition (one helper dropped,
    wrong argument shape) wouldn't fail the timing-line assertion
    above but would leave a cold page; this asserts the actual rows."""
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import (
        Article, ArticleFile, ArticleList, CacheEntry, Inbox, Subsystem,
        SubsystemPath,
    )

    # Seed: one subsystem with one matching article, in 'alpha'.
    with SessionLocal() as s:
        sub = Subsystem(name="BCACHEFS", status="Supported")
        s.add(sub)
        s.flush()
        s.add(SubsystemPath(subsystem_id=sub.id, glob="fs/bcachefs/", is_exclude=False))
        art = Article(
            message_id="warm-sub@x", subject="patch", author="A",
            date=datetime(2026, 5, 14, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="patch",
        )
        s.add(art)
        s.flush()
        alpha = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id, epoch="0.git",
            commit_sha="ab" * 20,
        ))
        s.commit()
        sub_id = sub.id

    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0

    # All four per-subsystem dashboard helpers must have cached a row
    # for this subsystem.
    expected_keys = [
        f"recent_articles_in_subsystem:alpha:{sub_id}:30",
        f"active_threads_in_subsystem:alpha:{sub_id}:7:10",
        f"daily_volume_in_subsystem:alpha:{sub_id}:30",
        f"active_reviewers_in_subsystem:alpha:{sub_id}:30:10",
    ]
    from mimir.cache import _ns
    with SessionLocal() as s:
        present = {
            row.key for row in s.execute(
                sa_select(CacheEntry).where(
                    CacheEntry.key.in_([_ns(k) for k in expected_keys])
                )
            ).scalars().all()
        }
    for k in expected_keys:
        assert _ns(k) in present, (
            f"warm-cache did not populate {k}; per-subsystem dashboard "
            f"will be cold on first visit"
        )


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


# `dev-seed-thread` builds a synthetic multi-message thread into a bare
# git repo under <mirror-root>/<inbox>/git/, then ingests it. The CLI is
# dev-only but the contract still needs pinning so a refactor of the
# synth-thread shape (depth, in_reply_to chain, author dedup) doesn't
# silently produce something that looks fine via `flask run` but breaks
# real ingest paths the next time it's exercised.


def test_dev_seed_thread_creates_inbox_and_articles(seeded_db, tmp_path):
    """First invocation: creates the inbox, ingests N messages, prints a
    URL pointing at a real article. Hermetic via --mirror-root."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    mirror_root = tmp_path / "Inboxes"
    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox", "dev-thread-test",
            "--messages", "5",
            "--mirror-root", str(mirror_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created inbox 'dev-thread-test'" in result.output
    assert "new=5" in result.output  # all 5 are fresh
    assert "navigate to: http://" in result.output

    # Mirror layout on disk matches the documented shape.
    assert (mirror_root / "dev-thread-test" / "git" / "0.git").is_dir()

    # DB rows match.
    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-test")
        ).scalar_one()
        assert ix.mirror_path == str(mirror_root / "dev-thread-test" / "git")
        rows = s.execute(
            select(Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
        ).all()
        assert len(rows) == 5


def test_dev_seed_thread_forms_a_real_thread(seeded_db, tmp_path):
    """Every reply must reference an in-archive parent so the
    recursive-CTE walk-up (`find_thread_root`) terminates and renders
    a tree. Without this guarantee, the dev-seeded inbox would render
    every message as its own root, defeating the whole point of the
    helper (which is to give the thread-fold UI something to display)."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox", "dev-thread-shape",
            "--messages", "6",
            "--mirror-root", str(tmp_path / "Inboxes"),
        ],
    )
    assert result.exit_code == 0, result.output

    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-shape")
        ).scalar_one()
        articles = list(s.execute(
            select(Article)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
            .order_by(Article.date.asc())
        ).scalars())

    assert len(articles) == 6
    # First article is the root: no thread_parent.
    assert articles[0].thread_parent is None, (
        "first seeded message must be the thread root"
    )
    # All replies reference a parent message-id that exists in the same
    # set of seeded articles -- no off-list ancestors.
    seeded_ids = {a.message_id for a in articles}
    for a in articles[1:]:
        assert a.thread_parent is not None
        assert a.thread_parent in seeded_ids, (
            f"reply {a.message_id} references off-list parent "
            f"{a.thread_parent}; dev-seed should keep the thread closed"
        )


def test_dev_seed_thread_idempotent_appends_on_rerun(seeded_db, tmp_path):
    """Re-running against an existing inbox doesn't recreate or
    error out; it appends fresh messages to the same repo. The
    CLI's `using existing inbox` log line is the operator-visible
    signal that the second run took the append path."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    args = [
        "--inbox", "dev-thread-idempotent",
        "--messages", "3",
        "--mirror-root", str(tmp_path / "Inboxes"),
    ]
    first = CliRunner().invoke(dev_seed_thread_command, args)
    assert first.exit_code == 0, first.output
    assert "created inbox 'dev-thread-idempotent'" in first.output

    second = CliRunner().invoke(dev_seed_thread_command, args)
    assert second.exit_code == 0, second.output
    assert "using existing inbox 'dev-thread-idempotent'" in second.output

    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "dev-thread-idempotent")
        ).scalar_one()
        rows = s.execute(
            select(Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == ix.id)
        ).all()
    # Each run adds 3; two runs => 6 total.
    assert len(rows) == 6


def test_dev_seed_thread_message_url_is_reachable(client, seeded_db, tmp_path):
    """The printed URL must resolve to a 200 against the running app.
    Extracts the URL from CLI output and hits it; if dev-seed has
    silently drifted (wrong path format, missing blob, etc.) this
    catches it."""
    import re

    result = CliRunner().invoke(
        dev_seed_thread_command,
        [
            "--inbox", "dev-thread-routable",
            "--messages", "4",
            "--mirror-root", str(tmp_path / "Inboxes"),
        ],
    )
    assert result.exit_code == 0, result.output
    m = re.search(r"navigate to: http://[^/]+(/\S+)", result.output)
    assert m is not None, f"no URL in output: {result.output}"
    url_path = m.group(1)
    r = client.get(url_path)
    assert r.status_code == 200, (
        f"dev-seed URL {url_path} returned {r.status_code}; "
        f"the seed helper has likely drifted from the route shape"
    )


# Shared helpers for the blob-touching commands (ingest / reindex / show).
# A local copy beats importing private helpers across test files; if the
# public-inbox v2 layout ever changes, both files update together but
# neither has to depend on the other.


def _rfc5322_msg(msgid: str, *, subject: str = "t", body: bytes = b"hello") -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@example.com\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n"
        + body
    )


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Build a bare public-inbox v2 epoch repo: one commit per
    message, each tree carries an `m` blob with raw RFC 5322 bytes."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent = None
    for i, raw in enumerate(messages):
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.parents = [parent] if parent else []
        c.author = c.committer = b"test <t@x>"
        c.commit_time = c.author_time = 1704067200 + i
        c.commit_timezone = c.author_timezone = 0
        c.encoding = b"UTF-8"
        c.message = f"add msg {i}".encode()
        repo.object_store.add_object(c)
        parent = c.id
    if parent is not None:
        repo.refs[b"HEAD"] = parent
    return repo_path


def _repoint_inbox(name: str, mirror_dir: Path) -> None:
    """Repoint a seeded Inbox row's mirror_path at the given tmp dir
    so the read path resolves blobs against a real repo."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
        ix.mirror_path = str(mirror_dir)
        s.commit()


# `ingest` -- batch ingest CLI.


def test_ingest_command_runs_and_prints_per_epoch_line(seeded_db, tmp_path):
    """`flask --app mimir ingest` walks every configured inbox's mirror
    and emits one summary line per epoch. Pin both the side-effect
    (article row count) and the visible contract (output shape).

    Use epoch 2.git so the seeded fixture's epoch=0.git ArticleList
    rows don't interfere with our assertions."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg("cli-ingest-1@example.com"),
        _rfc5322_msg("cli-ingest-2@example.com"),
    ])
    _repoint_inbox("alpha", mirror)

    result = CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    assert result.exit_code == 0, result.output
    # Per-epoch line shape: `<inbox>/<epoch>: new=N linked=N ...`
    assert "alpha/2.git:" in result.output
    assert "new=2" in result.output

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ids = s.execute(
            select(Article.message_id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == alpha.id, ArticleList.epoch == "2.git")
        ).scalars().all()
    assert "cli-ingest-1@example.com" in ids
    assert "cli-ingest-2@example.com" in ids


def test_ingest_command_unknown_inbox_clickexception():
    """The `_select_inboxes` helper raises ClickException on an
    unknown name; the CLI must surface that as a non-zero exit."""
    result = CliRunner().invoke(ingest_command, ["--inbox", "no-such-inbox"])
    assert result.exit_code != 0
    assert "unknown inbox" in result.output


# `reindex` -- single-epoch rewind, with and without --from-scratch.


def test_reindex_default_rewinds_state_and_redrives(seeded_db, tmp_path):
    """Default reindex: doesn't delete ArticleList rows, but clears
    the IngestState cursor so the next walk starts at the beginning.
    The actual messages are dup_db skips because they're already in
    the DB; the operator-visible signal is the summary line."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox, IngestState

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg("reindex-1@example.com"),
        _rfc5322_msg("reindex-2@example.com"),
    ])
    _repoint_inbox("alpha", mirror)

    # First ingest seeds the cursor and the rows.
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        state = s.get(IngestState, (alpha.id, "2.git"))
    assert state is not None
    assert state.last_commit_sha is not None

    # Reindex without --from-scratch: cursor goes back to None, the
    # re-walk redrives the same commits, which are dup_db this time.
    result = CliRunner().invoke(reindex_command, ["alpha", "2.git"])
    assert result.exit_code == 0, result.output
    assert "alpha/2.git:" in result.output
    assert "dup_db=2" in result.output, result.output
    assert "deleted" not in result.output  # only --from-scratch logs that


def test_reindex_from_scratch_deletes_existing_links(seeded_db, tmp_path):
    """`--from-scratch` drops the per-inbox ArticleList rows for the
    epoch before re-walking, so the messages re-ingest as `linked`
    (the Article rows survive because they may be cross-posted)."""
    from sqlalchemy import func, select
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg("scratch-1@example.com"),
        _rfc5322_msg("scratch-2@example.com"),
    ])
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        before = s.execute(
            select(func.count()).select_from(ArticleList)
            .where(ArticleList.inbox_id == alpha.id, ArticleList.epoch == "2.git")
        ).scalar_one()
    assert before == 2

    result = CliRunner().invoke(reindex_command, ["alpha", "2.git", "--from-scratch"])
    assert result.exit_code == 0, result.output
    assert "deleted 2 existing inbox-links for alpha/2.git" in result.output
    # The re-walk re-adds them via the linked-bucket path.
    assert "linked=2" in result.output, result.output


def test_reindex_missing_epoch_repo_clickexception(seeded_db, tmp_path):
    """Reindex against an epoch directory that doesn't exist on disk
    must raise a clean ClickException, not crash inside dulwich."""
    _repoint_inbox("alpha", tmp_path / "alpha-mirror")  # dir doesn't exist
    result = CliRunner().invoke(reindex_command, ["alpha", "0.git"])
    assert result.exit_code != 0
    assert "epoch repo not found" in result.output


# `show` -- one-article inspect, with the three ClickException branches.


def _ingest_one_for_show(tmp_path) -> tuple[str, Path]:
    """Build a single-message epoch and ingest it, returning the
    message-id and the mirror dir. Tests then drive `show` against
    the seeded article. Uses epoch 2.git to avoid colliding with
    seeded fixture rows at 0.git."""
    msgid = "show-msg@example.com"
    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg(msgid, subject="show test subject", body=b"hello show body"),
    ])
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    return msgid, mirror


def test_show_prints_db_row_and_parsed_blob(seeded_db, tmp_path):
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid])
    assert result.exit_code == 0, result.output
    # DB-side section
    assert "--- DB row ---" in result.output
    assert "linked inboxes:" in result.output
    assert "alpha/2.git@" in result.output
    # Parsed-blob section
    assert "--- parsed blob ---" in result.output
    assert f"Message-ID: {msgid}" in result.output
    assert "show test subject" in result.output
    assert "hello show body" in result.output


def test_show_no_body_suppresses_body(seeded_db, tmp_path):
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid, "--no-body"])
    assert result.exit_code == 0, result.output
    assert "show test subject" in result.output  # headers still shown
    assert "hello show body" not in result.output


def test_show_body_chars_truncates_with_marker(seeded_db, tmp_path):
    """When `--body-chars` is shorter than the body, the output is
    truncated and a `... (N more chars truncated; ...)` line
    explains it."""
    long_body = b"x" * 200
    msgid = "show-trunc@example.com"
    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg(msgid, body=long_body),
    ])
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])

    result = CliRunner().invoke(show_command, [msgid, "--body-chars", "50"])
    assert result.exit_code == 0, result.output
    assert "more chars truncated" in result.output
    # 150 chars were dropped; the marker spells it out.
    assert "150" in result.output


def test_show_unknown_message_id_clickexception(seeded_db):
    result = CliRunner().invoke(show_command, ["nope@nowhere.invalid"])
    assert result.exit_code != 0
    assert "no article" in result.output


def test_show_inbox_filter_rejects_unlinked(seeded_db, tmp_path):
    """`--inbox <name>` restricts the blob read to that inbox; if the
    article isn't linked there, a ClickException lists where it
    actually lives instead of silently falling back."""
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid, "--inbox", "beta"])
    assert result.exit_code != 0
    assert "not linked to inbox 'beta'" in result.output
    # The error includes the list of inboxes that DO have it.
    assert "alpha" in result.output


# `vacuum` / `analyze` -- DB-maintenance commands.


def test_analyze_command_runs(seeded_db):
    """`ANALYZE` is a no-op-shaped command on tiny DBs, but the CLI's
    contract is: runs, returns 0, prints the timing line."""
    result = CliRunner().invoke(analyze_command, [])
    assert result.exit_code == 0, result.output
    assert "ANALYZE complete" in result.output


def test_vacuum_command_runs_and_reports_sizes(seeded_db):
    """`vacuum` rebuilds the DB and checkpoints the WAL. On a tiny
    test DB the reclamation is negligible, but the before/after/
    reclaimed lines must all emit and the DB must still be openable
    afterwards."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    result = CliRunner().invoke(vacuum_command, [])
    assert result.exit_code == 0, result.output
    assert "before:" in result.output
    assert "after:" in result.output
    assert "reclaimed" in result.output

    # The DB is still usable after the engine.dispose() inside vacuum.
    with SessionLocal() as s:
        names = s.execute(select(Inbox.name)).scalars().all()
    assert "alpha" in names


# `admin failures list` / `replay` -- CLI wrappers around the service
# layer (which has its own coverage in test_ingest.py).


def _seed_parse_failure(inbox_name: str = "alpha") -> None:
    """Insert one ParseFailure row tied to the seeded inbox."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox, ParseFailure
    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == inbox_name)
        ).scalar_one()
        now = datetime.now(timezone.utc)
        s.add(ParseFailure(
            inbox_id=ix.id,
            epoch="0.git",
            commit_sha="ab" * 20,
            error_class="MessageTooLarge",
            error_message="message exceeds cap",
            first_seen=now,
            last_attempt=now,
            attempts=1,
        ))
        s.commit()


def test_admin_failures_list_empty_says_so(seeded_db):
    result = CliRunner().invoke(admin_failures_list_command, [])
    assert result.exit_code == 0
    assert "no parse failures" in result.output


def test_admin_failures_list_prints_seeded_row_and_total(seeded_db):
    _seed_parse_failure()
    result = CliRunner().invoke(admin_failures_list_command, [])
    assert result.exit_code == 0, result.output
    assert "alpha/0.git@" in result.output  # `<inbox>/<epoch>@<sha[:12]>`
    assert "MessageTooLarge" in result.output
    assert "message exceeds cap" in result.output
    assert "total: 1" in result.output


def test_admin_failures_list_epoch_requires_inbox(seeded_db):
    """`--epoch` without `--inbox` is rejected up-front; the filter
    only makes sense scoped to an inbox."""
    result = CliRunner().invoke(admin_failures_list_command, ["--epoch", "0.git"])
    assert result.exit_code != 0
    assert "--epoch requires --inbox" in result.output


def test_admin_failures_replay_unknown_inbox_clickexception(seeded_db):
    result = CliRunner().invoke(admin_failures_replay_command, ["no-such-inbox"])
    assert result.exit_code != 0
    # InboxNotFound is the underlying type; the CLI surfaces its str().
    assert "no-such-inbox" in result.output


def test_admin_failures_replay_happy_path_recovers_and_clears(
    seeded_db, tmp_path, monkeypatch,
):
    """The replay happy path: stage a failure for a real parseable
    blob, invoke the CLI, assert the summary line reports
    `recovered=1` and the row is gone. Only `unknown_inbox` was
    covered before; the success branch — the one operators actually
    invoke after a parser fix — was unreached.

    Mirror the test_ingest replay setup but drive it through the
    Click runner so the CLI argument parsing + ClickException
    boundary + output-shape are all exercised end-to-end."""
    import datetime as _dt
    import mimir.parser
    from sqlalchemy import func, select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox, ParseFailure

    # Build a real repo so replay_failures can fetch the blob.
    mirror_root = tmp_path / "replay-cli-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322_msg("cli-replay@example.com", body=b"x" * 500),
    ])
    _repoint_inbox("alpha", mirror_root)

    # Stage a failure for the real commit_sha so replay finds work.
    repo = Repo(str(mirror_root / "0.git"))
    sha = repo.head().decode()
    now = _dt.datetime.now(_dt.timezone.utc)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ParseFailure(
            inbox_id=ix.id,
            epoch="0.git",
            commit_sha=sha,
            error_class="MessageTooLarge",
            error_message="prior cap was too tight",
            first_seen=now,
            last_attempt=now,
            attempts=1,
        ))
        s.commit()

    # Ensure parser MAX_RAW_MESSAGE_BYTES doesn't reject the blob.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)

    result = CliRunner().invoke(admin_failures_replay_command, ["alpha"])
    assert result.exit_code == 0, result.output
    assert "alpha: attempted=1 recovered=1 still_failed=0 skipped=0" in result.output

    # The failure row is gone.
    with SessionLocal() as s:
        remaining = s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.commit_sha == sha)
        ).scalar_one()
    assert remaining == 0


# `init-db` -- bootstrap helper; documented in CLAUDE.md as a quick
# local-dev path that the operator falls back to when alembic isn't
# applicable. Untested historically because alembic is the real
# migration story.


def test_init_db_command_runs_and_creates_schema(tmp_path, monkeypatch):
    """`flask --app mimir init-db` calls `Base.metadata.create_all`
    against the configured engine. Hard to test in-place (the
    engine is module-global and already migrated), so point it at
    a throwaway sqlite file via the engine's URL.

    Pin two things:
    1. Exit code 0 and "schema created" output (operator UX).
    2. The expected tables actually exist on the fresh file
       afterwards. A regression that swapped `create_all` for a
       no-op would still print "schema created"; the table check
       catches it.
    """
    import sqlalchemy
    from mimir.extensions import Base
    from mimir import extensions as ext_module
    from mimir import cli as cli_module

    fresh_db = tmp_path / "fresh-init-db.sqlite"
    fresh_url = f"sqlite:///{fresh_db}"
    fresh_engine = sqlalchemy.create_engine(fresh_url, future=True)

    # The CLI module captured `engine` at import time, so monkeypatching
    # `mimir.extensions.engine` doesn't reach it. Patch the cli module's
    # own bound name too.
    monkeypatch.setattr(ext_module, "engine", fresh_engine)
    monkeypatch.setattr(cli_module, "engine", fresh_engine)

    result = CliRunner().invoke(init_db_command, [])
    assert result.exit_code == 0, result.output
    assert "schema created" in result.output

    # Tables exist on the fresh DB.
    inspector = sqlalchemy.inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    expected = {t.name for t in Base.metadata.tables.values()}
    missing = expected - tables
    assert not missing, (
        f"init-db should have created every ORM table; missing {missing}"
    )


# `admin canonicals backfill` -- no failure shape to assert; just
# pin that the wrapper runs, exits 0, and emits the summary line.


def test_admin_canonicals_backfill_runs_and_prints_summary(seeded_db):
    result = CliRunner().invoke(admin_canonicals_backfill_command, [])
    assert result.exit_code == 0, result.output
    # Seeded DB has 4 articles, but none with list-shaped To/Cc -- so
    # examined>0, resolved=0, unresolved>0. We only pin the line
    # shape; counts depend on dashboard helpers and bootstrap state.
    assert "backfill complete:" in result.output
    assert "examined=" in result.output
    assert "resolved=" in result.output


# `admin inbox add / update / remove / show` -- service layer is
# covered by test_inboxes.py; here we pin the click wiring (option
# parsing, ClickException -> exit-nonzero, output shape).


def test_admin_inbox_add_creates_inbox(seeded_db):
    result = CliRunner().invoke(admin_inbox_add_command, [
        "gamma",
        "--mirror-path", "/tmp/gamma",
        "--upstream-url", "https://example.com/gamma",
    ])
    assert result.exit_code == 0, result.output
    assert "created inbox 'gamma'" in result.output
    # Confirm it landed in the DB.
    assert get_inbox("gamma").upstream_url == "https://example.com/gamma"


def test_admin_inbox_add_invalid_url_clickexception(seeded_db):
    """`upstream_url` must be `https://...`; the validator raises
    InboxValidationError, which the CLI surfaces as a ClickException."""
    result = CliRunner().invoke(admin_inbox_add_command, [
        "gamma",
        "--mirror-path", "/tmp/gamma",
        "--upstream-url", "not-a-url",
    ])
    assert result.exit_code != 0


def test_admin_inbox_update_no_args_clickexception(seeded_db):
    """Update must specify at least one field to change; calling with
    none is a user error."""
    result = CliRunner().invoke(admin_inbox_update_command, ["alpha"])
    assert result.exit_code != 0
    assert "nothing to update" in result.output


def test_admin_inbox_update_changes_mirror_path(seeded_db):
    result = CliRunner().invoke(admin_inbox_update_command, [
        "alpha", "--mirror-path", "/tmp/alpha-new",
    ])
    assert result.exit_code == 0, result.output
    assert "updated inbox 'alpha'" in result.output
    assert get_inbox("alpha").mirror_path == "/tmp/alpha-new"


def test_admin_inbox_show_prints_fields_and_states(seeded_db):
    """`admin inbox show <name>` prints the configured fields plus
    the per-epoch ingest cursor state. The seeded alpha has linked
    articles but no IngestState row -- exercise both branches in
    one test by checking the "none -- never ingested" line."""
    result = CliRunner().invoke(admin_inbox_show_command, ["alpha"])
    assert result.exit_code == 0, result.output
    assert "name:" in result.output and "alpha" in result.output
    assert "mirror_path:" in result.output
    assert "upstream_url:" in result.output
    # Seeded fixture has 3 ArticleList rows on alpha (art1, art3, art4).
    assert "linked articles: 3" in result.output
    # No IngestState rows yet.
    assert "never ingested" in result.output


def test_admin_inbox_show_unknown_clickexception(seeded_db):
    result = CliRunner().invoke(admin_inbox_show_command, ["no-such-inbox"])
    assert result.exit_code != 0


def test_admin_inbox_remove_yes_drops_inbox_and_orphans(seeded_db):
    """With `--yes`, the prompt is skipped; the inbox row + its
    ArticleList rows go away. Orphan articles (those left with no
    remaining inbox links) also go by default."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    # Pre-state: alpha exists, art1+art4 are alpha-only.
    with SessionLocal() as s:
        assert s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one_or_none() is not None
        ids_before = set(s.execute(select(Article.message_id)).scalars().all())
    assert {"art1@example.com", "art4@example.com"} <= ids_before

    result = CliRunner().invoke(admin_inbox_remove_command, ["alpha", "--yes"])
    assert result.exit_code == 0, result.output
    assert "removed inbox 'alpha'" in result.output
    assert "article_lists rows deleted:" in result.output

    with SessionLocal() as s:
        assert s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one_or_none() is None
        ids_after = set(s.execute(select(Article.message_id)).scalars().all())
    # art1 and art4 were alpha-only -> gone. art3 was cross-posted
    # with beta -> survives. art2 is beta-only -> survives.
    assert "art1@example.com" not in ids_after
    assert "art4@example.com" not in ids_after
    assert "art2@example.com" in ids_after
    assert "art3@example.com" in ids_after


def test_admin_inbox_remove_keep_orphans_preserves_articles(seeded_db):
    """`--keep-orphan-articles` leaves Article rows with no remaining
    links intact (article_lists rows go either way)."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    result = CliRunner().invoke(admin_inbox_remove_command, [
        "alpha", "--yes", "--keep-orphan-articles",
    ])
    assert result.exit_code == 0, result.output
    # Orphan-deleted line is suppressed under --keep-orphan-articles.
    assert "orphan articles deleted" not in result.output

    with SessionLocal() as s:
        ids = set(s.execute(select(Article.message_id)).scalars().all())
    # All four seed articles survive: alpha-only ones are now orphans
    # but still present.
    assert "art1@example.com" in ids
    assert "art4@example.com" in ids
    assert "art2@example.com" in ids
    assert "art3@example.com" in ids


# Structural / wire-up assertions.
#
# The shape of `register_cli` is load-bearing — Flask only exposes the
# subcommands explicitly added there. A new top-level `@click.command`
# decorator at module scope that doesn't get wired in is invisible to
# the operator and to CI (every CLI test invokes its target directly,
# not via `flask --app mimir <name>`), so the regression slips through.


def test_register_cli_attaches_every_module_level_command():
    """Every `@click.command`/`@click.group`-decorated callable at
    `mimir.cli` module scope must be reachable through `app.cli`
    after `register_cli(app)` runs — either added directly to
    `app.cli` or attached to a registered group (`admin`,
    `admin inbox`, `admin inbox trackers`, `admin failures`,
    `admin canonicals`).

    A regression here is silent: the command still imports clean,
    every direct-invocation test in this file still passes, but
    `flask --app mimir <name>` returns "No such command." Pin the
    surface by traversing the registered command tree and asserting
    no module-level Command falls outside it."""
    import click
    from flask import Flask

    import mimir.cli as cli_mod

    app = Flask(__name__)
    cli_mod.register_cli(app)

    def reachable(commands: dict) -> set:
        out = set()
        for cmd in commands.values():
            out.add(cmd)
            if isinstance(cmd, click.Group):
                out.update(reachable(cmd.commands))
        return out

    attached = reachable(app.cli.commands)

    declared = {
        v for v in vars(cli_mod).values() if isinstance(v, click.Command)
    }

    missing = declared - attached
    assert not missing, (
        "module-level click.Command(s) not reachable via `app.cli` after "
        f"`register_cli(app)`: {sorted(c.name for c in missing)}. "
        "Either add to register_cli() or attach to an existing subgroup."
    )
