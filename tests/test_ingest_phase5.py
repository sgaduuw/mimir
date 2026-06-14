"""Phase 5 tests: broker admin RPC handlers migrate from inline
direct-SQLite writes (via service-layer SessionLocal/write_transaction)
to WriteOp dispatch through the active WriterThread.

Pins the structural contract for Phase 5 of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 5 PR audit is easy.

Contract-test shape: capture all WriteOp submits to the active
writer via a recording wrapper around `_context.get_active_writer()`,
run the service function, assert at least one submit happened
(i.e. the dispatch path was taken, not the legacy SessionLocal
path).
"""


def _writer_submit_recorder():
    """Helper: monkeypatch `writer.submit` on the active broker writer
    to record every WriteOp passed through. The wrapper preserves
    submit's real behaviour (the writer actually executes the WriteOp)
    so DB state assertions still work; the list just records the
    dispatch happened.

    Returns (writer, submits_list). Caller is responsible for restoring
    `writer.submit` after the test if needed; pytest's test isolation
    typically makes this fine since the per-test broker_active fixture
    rebuilds the writer."""
    from mimir.broker._context import get_active_writer

    writer = get_active_writer()
    submits = []
    original_submit = writer.submit

    def _recording_submit(op):
        submits.append(op)
        return original_submit(op)

    writer.submit = _recording_submit
    return writer, submits


def test_add_rule_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: `mimir.robots.add_rule` dispatches via the
    active WriterThread when broker context is set, not via the
    legacy direct `SessionLocal()` path."""
    from sqlalchemy import select

    from mimir.models import RobotsRule
    from mimir.robots import add_rule

    _, submits = _writer_submit_recorder()
    add_rule(
        user_agent="phase5-test-agent",
        disallow=["/test"],
        crawl_delay=None,
        content_signals=None,
    )

    assert len(submits) >= 1, "add_rule must dispatch at least one WriteOp"
    assert submits[0].label.startswith("robots:add:"), (
        "WriteOp label preserves the legacy write_transaction label shape"
    )

    # End-to-end: the row landed.
    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "phase5-test-agent")
        ).scalar_one()
    assert row.user_agent == "phase5-test-agent"


def test_add_rule_falls_back_to_direct_when_no_context(seeded_db):
    """When called outside the broker context, `add_rule` must fall
    back to the legacy direct `SessionLocal()` path (write_transaction
    was removed in Phase 6b)."""
    from sqlalchemy import select

    from mimir.broker import _context
    from mimir.models import RobotsRule
    from mimir.robots import add_rule

    # Temporarily clear the active broker context so no writer is available.
    saved_pool = _context._active_pool
    saved_writer = _context._active_writer
    _context.clear_active()
    try:
        add_rule(
            user_agent="phase5-fallback-agent",
            disallow=["/fallback"],
            crawl_delay=None,
            content_signals=None,
        )
    finally:
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "phase5-fallback-agent")
        ).scalar_one()
    assert row.user_agent == "phase5-fallback-agent"


def test_update_rule_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: update_rule dispatches via the writer."""
    from sqlalchemy import insert, select

    from mimir.models import RobotsRule
    from mimir.robots import update_rule

    with seeded_db() as s:
        s.execute(
            insert(RobotsRule).values(
                user_agent="phase5-update-target",
                disallow_paths=["/old"],
                crawl_delay=None,
                content_signals=None,
            )
        )
        s.commit()

    _, submits = _writer_submit_recorder()
    update_rule(
        user_agent="phase5-update-target",
        add_disallow=["/new"],
        remove_disallow=["/old"],
        crawl_delay=10,
    )

    assert len(submits) >= 1
    assert submits[0].label.startswith("robots:update:")

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "phase5-update-target")
        ).scalar_one()
    assert row.disallow_paths == ["/new"]
    assert row.crawl_delay == 10


def test_remove_rule_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: remove_rule dispatches via the writer."""
    from sqlalchemy import insert, select

    from mimir.models import RobotsRule
    from mimir.robots import remove_rule

    with seeded_db() as s:
        s.execute(
            insert(RobotsRule).values(
                user_agent="phase5-remove-target",
                disallow_paths=["/x"],
                crawl_delay=None,
                content_signals=None,
            )
        )
        s.commit()

    _, submits = _writer_submit_recorder()
    remove_rule(user_agent="phase5-remove-target")

    assert len(submits) >= 1
    assert submits[0].label.startswith("robots:remove:")

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "phase5-remove-target")
        ).one_or_none()
    assert row is None


def test_reset_rules_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: reset_rules dispatches via the writer.
    After reset, only the default `*` row should remain."""
    from sqlalchemy import insert, select

    from mimir.models import RobotsRule
    from mimir.robots import reset_rules

    with seeded_db() as s:
        s.execute(
            insert(RobotsRule).values(
                user_agent="phase5-reset-survivor-1",
                disallow_paths=["/y"],
                crawl_delay=None,
                content_signals=None,
            )
        )
        s.execute(
            insert(RobotsRule).values(
                user_agent="phase5-reset-survivor-2",
                disallow_paths=["/z"],
                crawl_delay=None,
                content_signals=None,
            )
        )
        s.commit()

    _, submits = _writer_submit_recorder()
    reset_rules()

    assert len(submits) >= 1
    assert submits[0].label == "robots:reset"

    with seeded_db() as s:
        remaining = s.execute(select(RobotsRule.user_agent)).scalars().all()
    # The two custom rules are gone; only the default star row remains.
    assert "phase5-reset-survivor-1" not in remaining
    assert "phase5-reset-survivor-2" not in remaining
    assert "*" in remaining, "reset_rules must reseed the default star rule"


def test_replay_failures_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: `mimir.ingest.replay.replay_failures` dispatches
    via the active WriterThread when broker context is set. The function
    only writes when there are actual failures to process; seed one
    ParseFailure row first so the loop runs."""
    from sqlalchemy import insert, select

    from mimir.ingest.replay import replay_failures
    from mimir.models import Inbox, ParseFailure

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        s.execute(
            insert(ParseFailure).values(
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="0" * 40,
                error_class="TestError",
                error_message="phase5 contract test seed",
                first_seen=now,
                last_attempt=now,
                attempts=1,
            )
        )
        s.commit()
        # Detach the inbox so replay_failures' session.merge() works as
        # in production (the broker handler passes a freshly-fetched
        # Inbox).
        s.expunge(alpha)

    _, submits = _writer_submit_recorder()
    # replay_failures with default kwargs will read the seeded
    # ParseFailure, fail to fetch its blob (the commit_sha doesn't
    # exist in the mirror), bump the last_seen_at + seen_count, and
    # commit. The dispatch path must fire at least one WriteOp.
    replay_failures(alpha, limit=10)

    assert len(submits) >= 1, (
        "replay_failures must dispatch at least one WriteOp when "
        "broker context is active"
    )
    assert submits[0].label == "failures_replay", (
        "WriteOp label should be 'failures_replay'"
    )


def test_set_tracked_authors_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: set_tracked_authors dispatches via the writer."""
    from mimir.inboxes import set_tracked_authors

    _, submits = _writer_submit_recorder()
    inbox = set_tracked_authors(name="alpha", authors={"phase5": "phase5@test"})

    assert len(submits) >= 1, "set_tracked_authors must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:set_tracked_authors:")
    assert inbox.tracked_authors == {"phase5": "phase5@test"}


def test_add_tracked_author_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: add_tracked_author dispatches via the writer."""
    from mimir.inboxes import add_tracked_author

    _, submits = _writer_submit_recorder()
    inbox = add_tracked_author(name="alpha", label="phase5", substring="phase5@test")

    assert len(submits) >= 1, "add_tracked_author must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:add_tracked_author:")
    assert inbox.tracked_authors is not None
    assert inbox.tracked_authors.get("phase5") == "phase5@test"


def test_remove_tracked_author_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: remove_tracked_author dispatches via the writer."""
    from mimir.inboxes import remove_tracked_author, set_tracked_authors

    # Seed a tracker entry so removal has something to remove.
    set_tracked_authors(name="alpha", authors={"to-remove": "remove@test"})

    _, submits = _writer_submit_recorder()
    inbox = remove_tracked_author(name="alpha", label="to-remove")

    assert len(submits) >= 1, "remove_tracked_author must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:remove_tracked_author:")
    assert inbox.tracked_authors is None or "to-remove" not in inbox.tracked_authors


def test_clear_tracked_authors_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: clear_tracked_authors dispatches via the writer.
    It delegates to set_tracked_authors which was migrated first; this
    test pins that the delegation chain still takes the writer path."""
    from mimir.inboxes import clear_tracked_authors, set_tracked_authors

    # Seed a tracker entry so clearing has something to clear.
    set_tracked_authors(name="alpha", authors={"to-clear": "clear@test"})

    _, submits = _writer_submit_recorder()
    inbox = clear_tracked_authors(name="alpha")

    assert len(submits) >= 1, "clear_tracked_authors must dispatch at least one WriteOp"
    # clear_tracked_authors delegates to set_tracked_authors
    assert submits[0].label.startswith("inbox:set_tracked_authors:")
    assert inbox.tracked_authors is None


def test_create_inbox_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: create_inbox dispatches via the writer."""
    from mimir.inboxes import create_inbox

    _, submits = _writer_submit_recorder()
    inbox = create_inbox(
        name="phase5-new",
        mirror_path="/tmp/phase5-new",
        upstream_url="https://example.com/phase5-new",
    )

    assert len(submits) >= 1, "create_inbox must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:create:")
    assert inbox.name == "phase5-new"
    assert inbox.mirror_path == "/tmp/phase5-new"


def test_update_inbox_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: update_inbox dispatches via the writer."""
    from mimir.inboxes import update_inbox

    _, submits = _writer_submit_recorder()
    inbox = update_inbox(name="alpha", mirror_path="/tmp/alpha-updated")

    assert len(submits) >= 1, "update_inbox must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:update:")
    assert inbox.mirror_path == "/tmp/alpha-updated"


def test_delete_inbox_dispatches_via_writer(seeded_db, broker_active):
    """Phase 5 contract: delete_inbox dispatches via the writer.
    Uses the beta inbox so the primary alpha inbox is preserved for
    other tests in the session."""
    from mimir.inboxes import delete_inbox

    _, submits = _writer_submit_recorder()
    report = delete_inbox(name="beta", keep_orphan_articles=True)

    assert len(submits) >= 1, "delete_inbox must dispatch at least one WriteOp"
    assert submits[0].label.startswith("inbox:delete:")
    assert report.name == "beta"
