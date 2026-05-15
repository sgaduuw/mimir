"""Unit + integration tests for the dynamic MAINTAINERS-derived
allowlist. The redaction-filter wiring (`_safe_from_filter`,
`_redact_trailer_address`, `_is_allowlisted_address_filter`) is
exercised through the web tier in `tests/test_routes.py`; this
file pins the helper itself + the cache invalidation hook."""
from sqlalchemy import select

from mimir import cache, maintainer_allowlist
from mimir.extensions import SessionLocal
from mimir.models import Subsystem, SubsystemMaintainer


def _add_maintainer(session, sub_name, role, name, address):
    """Append one M:/R: row to a subsystem (creating the subsystem
    if absent). Returns the SubsystemMaintainer row."""
    sub = session.execute(
        select(Subsystem).where(Subsystem.name == sub_name)
    ).scalar_one_or_none()
    if sub is None:
        sub = Subsystem(name=sub_name, status="Maintained")
        session.add(sub)
        session.flush()
    m = SubsystemMaintainer(
        subsystem_id=sub.id, role=role, name=name, address=address,
    )
    session.add(m)
    session.flush()
    return m


def test_empty_when_no_maintainers_indexed(seeded_db):
    """A fresh deploy without `update-mainline` having run yet has
    an empty `subsystem_maintainers` table → empty frozenset → only
    the static allowlist applies. Pins the documented graceful-
    degradation contract."""
    maintainer_allowlist.invalidate()
    out = maintainer_allowlist.maintainer_addresses()
    assert out == frozenset()


def test_collects_m_and_r_roles(seeded_db):
    """Both maintainer and reviewer roles contribute to the set —
    the helper doesn't filter by role."""
    with seeded_db() as s:
        _add_maintainer(s, "BCACHEFS", "M", "Kent", "kent@example.com")
        _add_maintainer(s, "BCACHEFS", "R", "Reviewer A", "ra@example.com")
        s.commit()
    maintainer_allowlist.invalidate()
    out = maintainer_allowlist.maintainer_addresses()
    assert "kent@example.com" in out
    assert "ra@example.com" in out


def test_addresses_lowercased(seeded_db):
    """Cached set carries lowercased addresses so the filter check
    can do a single `addr.lower() in set` comparison without per-
    entry case folding at lookup time."""
    with seeded_db() as s:
        _add_maintainer(s, "BCACHEFS", "M", "Mixed", "Mixed@KerneL.OrG")
        s.commit()
    maintainer_allowlist.invalidate()
    out = maintainer_allowlist.maintainer_addresses()
    assert "mixed@kernel.org" in out
    assert "Mixed@KerneL.OrG" not in out


def test_deduplicates_addresses_appearing_in_multiple_subsystems(seeded_db):
    """The same address listed under multiple subsystems lands in
    the set once. SELECT DISTINCT on the address column."""
    with seeded_db() as s:
        _add_maintainer(s, "SUB-A", "M", "Same", "same@example.com")
        _add_maintainer(s, "SUB-B", "R", "Same", "same@example.com")
        s.commit()
    maintainer_allowlist.invalidate()
    out = maintainer_allowlist.maintainer_addresses()
    assert sum(1 for a in out if a == "same@example.com") == 1


def test_invalidate_drops_cached_set(seeded_db):
    """Calling `invalidate()` after MAINTAINERS data changed makes
    the next read see the new state. Cross-process safety relies on
    this hook firing from the scheduler sidecar after the
    `update-mainline` reload commits."""
    with seeded_db() as s:
        _add_maintainer(s, "BCACHEFS", "M", "Kent", "kent@example.com")
        s.commit()
    maintainer_allowlist.invalidate()
    first = maintainer_allowlist.maintainer_addresses()
    assert "kent@example.com" in first

    # Drop the row (simulating MAINTAINERS reload that no longer
    # lists Kent), invalidate, re-read.
    with seeded_db() as s:
        s.execute(SubsystemMaintainer.__table__.delete())
        s.commit()
    # Without invalidate(), the cached value would still hold
    # kent@example.com — pin that behaviour first to make the test
    # specifically about invalidation.
    stale = maintainer_allowlist.maintainer_addresses()
    assert "kent@example.com" in stale, "cache should still be warm"

    maintainer_allowlist.invalidate()
    fresh = maintainer_allowlist.maintainer_addresses()
    assert "kent@example.com" not in fresh


def test_cache_delete_clears_one_key():
    """Pin the new `cache.delete(key)` helper used by the
    invalidation hook. Set, confirm read, delete, confirm gone."""
    cache.set("ma_test_key", ["a"], ttl=60)
    with SessionLocal() as s:
        assert cache.get_or_compute(
            s, "ma_test_key", 60, lambda: ["recompute"],
        ) == ["a"]
    deleted = cache.delete("ma_test_key")
    assert deleted == 1
    with SessionLocal() as s:
        # Cache miss now → fn() runs.
        assert cache.get_or_compute(
            s, "ma_test_key", 60, lambda: ["recompute"],
        ) == ["recompute"]
    # Belt + braces: a delete on an absent key returns 0.
    assert cache.delete("ma_nonexistent_key") == 0
    cache.delete("ma_test_key")  # cleanup
