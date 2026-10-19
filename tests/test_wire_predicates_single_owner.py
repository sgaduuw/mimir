"""The broker wire uses the canonical predicates, not copies.

A copy, however equal today, can drift (AGENTS.md: whoever builds an
identifier and whoever accepts it share one predicate). Identity is
the property that rules drift out.
"""

from mimir import _inbox_name, robots
from mimir.broker import protocol


def test_wire_binds_the_canonical_predicates():
    assert protocol.INBOX_NAME_RE is _inbox_name.INBOX_NAME_RE
    assert protocol.UA_RE is robots.UA_RE
    assert protocol.PATH_RE is robots.PATH_RE
    assert protocol.CONTENT_SIGNAL_KEYS is robots.CONTENT_SIGNAL_KEYS
    assert protocol.CONTENT_SIGNAL_VALUES is robots.CONTENT_SIGNAL_VALUES
