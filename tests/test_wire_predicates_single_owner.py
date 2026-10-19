"""The broker wire and the admin-side validators accept the same inputs.

Whoever builds an identifier and whoever accepts it share one predicate
(AGENTS.md). The wire validators in `mimir.broker.protocol` once held
copies of the inbox-name and robots predicates; a copy that drifts
makes the CLI accept what the broker refuses, or the reverse. Each
table row is fed to both sides and both must give the expected answer.

Cases avoid leading/trailing whitespace and uppercase content-signal
tokens: the admin validators deliberately strip and lowercase before
checking, the wire deliberately does not, and that normalisation is not
the predicate under test.
"""

import pytest
from pydantic import ValidationError

from mimir.broker.protocol import InboxCreateRequest, RobotsAddRequest
from mimir.inboxes import InboxValidationError, validate_name
from mimir.robots import (
    RobotsValidationError,
    validate_content_signals,
    validate_disallow_path,
    validate_user_agent,
)


def _accepts(fn, *args) -> bool:
    try:
        fn(*args)
    except ValidationError, InboxValidationError, RobotsValidationError:
        return False
    return True


def _wire_inbox(name):
    return InboxCreateRequest(name=name, mirror_path="/m", upstream_url="https://x")


def _wire_ua(ua):
    return RobotsAddRequest(user_agent=ua)


def _wire_path(path):
    return RobotsAddRequest(user_agent="*", disallow=[path])


def _wire_signals(signals):
    return RobotsAddRequest(user_agent="*", content_signals=signals)


INBOX_NAMES = [
    ("lkml", True),
    ("a", True),
    ("a-b", True),
    ("a" * 64, True),
    ("a" * 65, False),
    ("", False),
    ("-x", False),
    ("x-", False),
    ("LKML", False),
    ("a b", False),
    ("café", False),
    ("../etc", False),
    ("a_b", False),
]

USER_AGENTS = [
    ("*", True),
    ("Googlebot", True),
    ("a" * 64, True),
    ("a" * 65, False),
    ("", False),
    ("a b", False),
    ("a\tb", False),
    ("boté", False),
    ("bot\x7f", False),
]

DISALLOW_PATHS = [
    ("/", True),
    ("/search", True),
    ("*.pdf", True),
    ("/" + "a" * 255, True),
    ("/" + "a" * 256, False),
    ("search", False),
    ("", False),
    ("/a b", False),
    ("/café", False),
]

CONTENT_SIGNALS = [
    ({"search": "yes"}, True),
    ({"ai-train": "no", "ai-input": "yes"}, True),
    ({"bogus": "yes"}, False),
    ({"search": "maybe"}, False),
    ({"search": ""}, False),
]


@pytest.mark.parametrize(("name", "ok"), INBOX_NAMES)
def test_inbox_name_wire_and_admin_agree(name, ok):
    assert _accepts(_wire_inbox, name) is ok
    assert _accepts(validate_name, name) is ok


@pytest.mark.parametrize(("ua", "ok"), USER_AGENTS)
def test_robots_user_agent_wire_and_admin_agree(ua, ok):
    assert _accepts(_wire_ua, ua) is ok
    assert _accepts(validate_user_agent, ua) is ok


@pytest.mark.parametrize(("path", "ok"), DISALLOW_PATHS)
def test_robots_disallow_path_wire_and_admin_agree(path, ok):
    assert _accepts(_wire_path, path) is ok
    assert _accepts(validate_disallow_path, path) is ok


@pytest.mark.parametrize(("signals", "ok"), CONTENT_SIGNALS)
def test_robots_content_signals_wire_and_admin_agree(signals, ok):
    assert _accepts(_wire_signals, signals) is ok
    assert _accepts(validate_content_signals, signals) is ok
