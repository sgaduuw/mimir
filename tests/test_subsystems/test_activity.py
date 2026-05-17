"""Tests for mimir/subsystems_dashboard/activity.py: cross-
inbox 'most active subsystems' aggregations and the
carrying of top-maintainer / status / sparkline metadata."""



from sqlalchemy import select

from mimir.models import (
    Inbox,
)
from mimir.subsystems_dashboard import (
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
)

from tests.test_subsystems._helpers import _add_recent_thread_root, _add_subsystem


def test_most_active_subsystems_in_inbox_counts_and_sorts(seeded_db):
    """Two subsystems with different recent message counts surface
    in descending order. Subsystem with no recent activity is
    excluded entirely."""
    with seeded_db() as s:
        hot = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        warm = _add_subsystem(s, "BTRFS-MAIN", "Maintained", files=["fs/btrfs/"])
        _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        # 3 hot, 1 warm, 0 dormant.
        _add_recent_thread_root(s, "h1@x", ["fs/bcachefs/a.c"])
        _add_recent_thread_root(s, "h2@x", ["fs/bcachefs/b.c"])
        _add_recent_thread_root(s, "h3@x", ["fs/bcachefs/c.c"])
        _add_recent_thread_root(s, "w1@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(
            s, alpha, days=7, limit=10, force=True,
        )
    names = [a.name for a in out]
    counts = [a.message_count for a in out]
    assert names == ["BCACHEFS", "BTRFS-MAIN"]
    assert counts == [3, 1]
    # `DORMANT` doesn't appear at all (no in-window articles).
    assert hot.id in {a.id for a in out}
    assert warm.id in {a.id for a in out}


def test_most_active_subsystems_in_inbox_inbox_scoped(seeded_db):
    """A subsystem's count on `alpha` doesn't bleed into `beta`'s
    list."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "alpha-1@x", ["fs/bcachefs/a.c"],
                                inbox_name="alpha")
        _add_recent_thread_root(s, "alpha-2@x", ["fs/bcachefs/b.c"],
                                inbox_name="alpha")
        _add_recent_thread_root(s, "beta-1@x", ["fs/bcachefs/c.c"],
                                inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_a = most_active_subsystems_in_inbox(s, alpha, force=True)
        out_b = most_active_subsystems_in_inbox(s, beta, force=True)
    assert out_a[0].message_count == 2
    assert out_a[0].inbox_name == "alpha"
    assert out_b[0].message_count == 1
    assert out_b[0].inbox_name == "beta"


def test_most_active_subsystems_in_inbox_empty_when_no_supported_globs(
    seeded_db,
):
    """Wildcard-only F: rules → no supported globs → not included.
    Matches the documented contract of the other path-filtered
    helpers."""
    with seeded_db() as s:
        _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_recent_thread_root(s, "wild@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out == []


def test_most_active_subsystems_global_aggregates_across_inboxes(
    seeded_db,
):
    """A subsystem active in both inboxes shows once with a total
    count summed across them, attributed to the busiest inbox."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        # alpha: 3 messages
        _add_recent_thread_root(s, "a1@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "a2@x", ["fs/bcachefs/b.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "a3@x", ["fs/bcachefs/c.c"], inbox_name="alpha")
        # beta: 1 message
        _add_recent_thread_root(s, "b1@x", ["fs/bcachefs/d.c"], inbox_name="beta")
        s.commit()
        out = most_active_subsystems_global(s, days=7, limit=10, force=True)
    assert len(out) == 1
    assert out[0].name == "BCACHEFS"
    # Total = 3 + 1 = 4. Attribution = alpha (3 > 1).
    assert out[0].message_count == 4
    assert out[0].inbox_name == "alpha"


def test_most_active_subsystems_global_force_propagates_to_inner(
    seeded_db, monkeypatch,
):
    """The outer `most_active_subsystems_global` wraps its compute
    in `cache.get_or_compute`, and the inner per-inbox helper has
    its own cache. Without `force=force` plumbed through, a
    `warm-cache --force` (or any caller passing `force=True`)
    recomputes the global aggregator from stale per-inbox rows.
    Audit (2026-05-15) flagged it: outer bypasses, inner silently
    doesn't.

    The test patches the inner helper to record the `force` kwarg
    every call gets, then drives the public surface with
    `force=True` and asserts every inner invocation saw `True`."""
    from mimir.subsystems_dashboard import activity as subs_mod

    seen_force: list[bool] = []
    real_inner = subs_mod._most_active_subsystems_in_inbox_full

    def _spy(session, inbox, *, days=7, force=False):
        seen_force.append(force)
        return real_inner(session, inbox, days=days, force=force)

    monkeypatch.setattr(
        subs_mod, "_most_active_subsystems_in_inbox_full", _spy,
    )

    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "force-a@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        s.commit()
        most_active_subsystems_global(s, days=7, limit=10, force=True)

    assert seen_force, "inner helper was never called, test setup broke"
    assert all(seen_force), (
        f"force=True must propagate through to the per-inbox helper; "
        f"got {seen_force!r}"
    )


def test_most_active_subsystems_global_alphabetical_tiebreak_on_inbox(
    seeded_db,
):
    """Equal per-inbox counts → attribute to the alphabetically-
    earlier inbox name. Deterministic ordering matters for stable
    URL generation."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "a-tie@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "b-tie@x", ["fs/bcachefs/b.c"], inbox_name="beta")
        s.commit()
        out = most_active_subsystems_global(s, days=7, limit=10, force=True)
    assert len(out) == 1
    # alpha < beta alphabetically, equal per-inbox count of 1.
    assert out[0].inbox_name == "alpha"


def test_most_active_subsystems_carries_top_maintainer_and_status(
    seeded_db,
):
    """SubsystemActivity rows pick up the first M: maintainer's
    name + the subsystem's status. The R: rows don't contribute
    to the "maintained by" decoration (that framing is M:-only)."""
    with seeded_db() as s:
        _add_subsystem(
            s, "BCACHEFS", "Supported",
            files=["fs/bcachefs/"],
            maintainers=[
                ("M", "Kent Overstreet", "kent@kernel.org"),
                ("R", "Brian Foster", "bfoster@redhat.com"),
            ],
        )
        _add_recent_thread_root(s, "bch@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert len(out) == 1
    row = out[0]
    assert row.maintainer_name == "Kent Overstreet"
    assert row.multiple_maintainers is False  # only one M: row
    assert row.status == "Supported"


def test_most_active_subsystems_marks_multiple_maintainers(seeded_db):
    """Multiple M: rows → multiple_maintainers True so the card
    can render the "et al." suffix."""
    with seeded_db() as s:
        _add_subsystem(
            s, "NET", "Maintained",
            files=["net/"],
            maintainers=[
                ("M", "Jakub Kicinski", "kuba@kernel.org"),
                ("M", "Paolo Abeni", "pabeni@redhat.com"),
            ],
        )
        _add_recent_thread_root(s, "net@x", ["net/core/dev.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out[0].multiple_maintainers is True
    # First M: by id order wins the display slot.
    assert out[0].maintainer_name == "Jakub Kicinski"


def test_most_active_subsystems_carries_sparkline(seeded_db):
    """7-day daily-volume sparkline rides on the SubsystemActivity
    row so the front-page card can render it without an extra
    helper call per card."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "spark@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out[0].spark is not None
    assert len(out[0].spark.days) == 7  # 7-day series
