"""Every inbox, robots and replay mutator writes through the broker's
WriterThread and raises when none is active (#707). The single-writer
invariant means a process without a writer must not fall back to its
own SessionLocal write."""

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
