"""Every inbox, robots and replay mutator writes through the broker's
WriterThread and raises when none is active (#707). The single-writer
invariant means a process without a writer must not fall back to its
own SessionLocal write."""

from datetime import UTC

import pytest
from sqlalchemy import select

from mimir.broker import _context
from mimir.inboxes import (
    create_inbox,
    delete_inbox,
    set_tracked_authors,
    update_inbox,
)
from mimir.ingest.replay import replay_failures
from mimir.models import Inbox, RobotsRule
from mimir.robots import add_rule, remove_rule, reset_rules, update_rule

MUTATORS = {
    "create_inbox": lambda: create_inbox(
        "fresh", "/tmp/fresh", "https://example.invalid/fresh"
    ),
    "update_inbox": lambda: update_inbox("alpha", mirror_path="/tmp/moved"),
    "delete_inbox": lambda: delete_inbox("alpha"),
    "set_tracked_authors": lambda: set_tracked_authors("alpha", None),
    "add_rule": lambda: add_rule("fresh-bot", disallow=["/x"]),
    "update_rule": lambda: update_rule("tester-bot", add_disallow=["/y"]),
    "remove_rule": lambda: remove_rule("tester-bot"),
    "reset_rules": reset_rules,
    "replay_failures": lambda: replay_failures(Inbox(id=1, name="alpha")),
}


@pytest.mark.parametrize("name", MUTATORS)
def test_mutator_without_a_writer_raises(seeded_db, monkeypatch, name):
    with seeded_db() as s:
        s.add(RobotsRule(user_agent="tester-bot", disallow_paths=[]))
        s.commit()
        # Precondition: the legacy path would have found its target,
        # so a green run means "refused", not "nothing to do".
        assert s.execute(select(Inbox).where(Inbox.name == "alpha")).first()

    monkeypatch.setattr(_context, "_active_pool", None)
    monkeypatch.setattr(_context, "_active_writer", None)

    with pytest.raises(RuntimeError, match="No active broker"):
        MUTATORS[name]()


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
        from datetime import datetime

        now = datetime.now(UTC)
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
