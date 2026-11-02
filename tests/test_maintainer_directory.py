"""Tests for mimir/maintainer_directory.py: cross-inbox maintainer
directory read helpers backing the maintainer index + profile pages."""

from sqlalchemy import select

from mimir import cache
from mimir.maintainer_directory import all_maintainers, maintainer_profile
from mimir.models import Article, ArticleTrailer, Subsystem, SubsystemMaintainer


def _add_subsystem(session, name, status, maintainers):
    """Insert a Subsystem + its M:/R: maintainer rows. `maintainers`
    is a list of (role, name, address) tuples; addresses are passed
    verbatim (not lowercased) to mirror how MAINTAINERS entries
    actually look."""
    sub = Subsystem(name=name, status=status)
    for role, mname, addr in maintainers:
        sub.maintainers.append(SubsystemMaintainer(role=role, name=mname, address=addr))
    session.add(sub)
    session.flush()
    return sub


def _add_trailer(session, message_id, address, role="Reviewed-by", name=""):
    """Attach one ArticleTrailer to an already-seeded conftest
    article (art1..art4), keyed by the article's real message_id."""
    article = session.execute(
        select(Article).where(Article.message_id == message_id)
    ).scalar_one()
    session.add(
        ArticleTrailer(
            article_id=article.id,
            role=role,
            name=name,
            address=address,
            address_normalized=address.lower(),
        )
    )
    session.flush()


def test_all_maintainers_collapses_case_variants_role_and_orders_by_address(session):
    _add_subsystem(
        session,
        "ADVANTECH",
        "Maintained",
        maintainers=[
            ("M", "Andrea Ho", "Andrea.Ho@advantech.com.tw"),
            ("M", "", "andrea.ho@advantech.com.tw"),
            ("R", "Bob Reviewer", "bob@example.com"),  # role R, excluded
            ("M", "Zed", "zed@example.com"),
        ],
    )
    session.commit()

    result = all_maintainers(session, force=True)

    assert result == [
        ("andrea.ho@advantech.com.tw", "Andrea Ho"),
        ("zed@example.com", "Zed"),
    ]


def test_all_maintainers_skips_empty_address(session):
    _add_subsystem(session, "NOADDR", "Maintained", maintainers=[("M", "Nobody", "")])
    session.commit()

    assert all_maintainers(session, force=True) == []


def test_all_maintainers_cache_round_trips_tuples(session):
    """Populate the cache, then read it back without force; the
    decoded value must still be a list of real tuples (JSON turns
    tuples into lists on the wire, mimir.cache's generic tuple tag
    is what recovers them)."""
    _add_subsystem(
        session, "BCACHEFS", "Supported", maintainers=[("M", "Kent", "kent@kernel.org")]
    )
    session.commit()

    populated = all_maintainers(session, force=True)
    cached = all_maintainers(session, force=False)

    assert cached == populated == [("kent@kernel.org", "Kent")]
    assert all(isinstance(pair, tuple) for pair in cached)


def test_maintainer_profile_returns_full_profile(session):
    _add_subsystem(
        session,
        "BCACHEFS",
        "Supported",
        maintainers=[("M", "Alice", "Alice@Kernel.org")],
    )
    _add_subsystem(
        session,
        "ZFS",
        "Maintained",
        maintainers=[("M", "", "alice@kernel.org")],
    )
    _add_trailer(session, "art1@example.com", "alice@kernel.org")  # alpha
    _add_trailer(session, "art2@example.com", "alice@kernel.org")  # beta
    session.commit()

    profile = maintainer_profile(session, "alice@kernel.org", force=True)

    assert profile is not None
    assert profile.address == "alice@kernel.org"
    assert profile.name == "Alice"
    assert profile.subsystems == [("BCACHEFS", "Supported"), ("ZFS", "Maintained")]
    assert profile.review_inboxes == ["alpha", "beta"]


def test_maintainer_profile_empty_review_inboxes(session):
    _add_subsystem(
        session,
        "BCACHEFS",
        "Supported",
        maintainers=[("M", "Carol", "carol@kernel.org")],
    )
    session.commit()

    profile = maintainer_profile(session, "carol@kernel.org", force=True)

    assert profile is not None
    assert profile.review_inboxes == []


def test_maintainer_profile_unknown_address_returns_none_and_does_not_cache(session):
    assert maintainer_profile(session, "nobody@example.com") is None
    # A second call must return None cleanly too, not a poisoned
    # cache entry from the first miss.
    assert maintainer_profile(session, "nobody@example.com") is None
    assert cache.get("maintainer:profile:nobody@example.com") is None


def test_maintainer_profile_only_role_r_is_not_a_maintainer(session):
    """A reviewer-only address (role R, no M: entry) is not a known
    maintainer and must 404-shape as None."""
    _add_subsystem(
        session,
        "BCACHEFS",
        "Supported",
        maintainers=[("R", "Dave", "dave@kernel.org")],
    )
    session.commit()

    assert maintainer_profile(session, "dave@kernel.org") is None


def test_maintainer_profile_review_inboxes_seek_the_trailer_index(seeded_db):
    """Same shape as #831's reviewer query: a trailer lookup by address
    alone cannot use `ix_article_trailers_role_addr` and scans every
    trailer. Under production's statistics it must seek the index."""
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with seeded_db() as s:
        _add_subsystem(
            s, "BCACHEFS", "Supported", maintainers=[("M", "Kent", "kent@kernel.org")]
        )
        s.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        plan = plan_of(
            s,
            lambda: maintainer_profile(s, "kent@kernel.org", force=True),
            "article_trailers",
        )
    # The role equality, not a skip-scan (ANY(role)) the stats allow.
    assert "ix_article_trailers_role_addr (role=? AND address_normalized=?)" in plan, (
        plan
    )


def test_review_counts_keep_trailer_roles_and_cross_post_memberships(session):
    from mimir.maintainer_directory import maintainer_review_counts_query

    _add_trailer(session, "art3@example.com", "Alice@Kernel.org")
    _add_trailer(session, "art3@example.com", "alice@kernel.org", role="Tested-by")
    _add_trailer(session, "art2@example.com", "alice@kernel.org", role="Acked-by")
    _add_trailer(session, "art3@example.com", "bob@kernel.org")
    _add_trailer(session, "art3@example.com", "alice@kernel.org", role="Signed-off-by")
    session.commit()

    assert maintainer_review_counts_query(session, "alice@kernel.org") == {
        "alpha": 2,
        "beta": 3,
    }
    assert maintainer_review_counts_query(session, "nobody@kernel.org") == {}


def test_review_counts_seek_the_trailer_index(seeded_db):
    from mimir.maintainer_directory import maintainer_review_counts_query
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        plan = plan_of(
            session,
            lambda: maintainer_review_counts_query(session, "alice@kernel.org"),
            "article_trailers",
        )
    assert "ix_article_trailers_role_addr (role=? AND address_normalized=?)" in plan, (
        plan
    )


def test_review_count_warmer_filters_owners_and_refreshes_existing_keys(
    session, monkeypatch
):
    from mimir import maintainer_directory as directory

    _add_subsystem(
        session,
        "FILES",
        "Maintained",
        [
            ("M", "Alice", "Alice@Kernel.org"),
            ("M", "Alice", "alice@kernel.org"),
            ("M", "Empty", "empty@kernel.org"),
            ("M", "Unsupported", "bad/address@kernel.org"),
            ("R", "Reviewer", "reviewer@kernel.org"),
        ],
    )
    _add_trailer(session, "art1@example.com", "alice@kernel.org")
    session.commit()
    assert directory.cached_maintainer_review_counts("alice@kernel.org") is None
    directory.warm_maintainer_review_counts(session)
    assert directory.cached_maintainer_review_counts("ALICE@KERNEL.ORG") == {"alpha": 1}
    assert directory.cached_maintainer_review_counts("empty@kernel.org") == {}
    keys = [
        key for key in cache.keys() if key.startswith("maintainer:review_counts_v1:")
    ]
    assert len(keys) == 2

    original = directory.maintainer_review_counts_query
    calls = []

    def counted(s, address):
        calls.append(address)
        return original(s, address)

    monkeypatch.setattr(directory, "maintainer_review_counts_query", counted)
    directory.warm_maintainer_review_counts(session)
    assert calls == []
    _add_trailer(session, "art2@example.com", "alice@kernel.org")
    session.commit()
    _add_subsystem(session, "NEW", "Maintained", [("M", "New", "new@kernel.org")])
    session.commit()
    assert directory.cached_maintainer_review_counts("new@kernel.org") is None
    # Existing slow-warmer context refreshes near expiry without a new policy.
    now = cache._now()
    monkeypatch.setattr(cache, "_now", lambda: now + 3599)
    with cache.refresh_window(450):
        directory.warm_maintainer_review_counts(session)
    assert calls == ["alice@kernel.org", "empty@kernel.org", "new@kernel.org"]
    assert directory.cached_maintainer_review_counts("new@kernel.org") == {}
    assert directory.cached_maintainer_review_counts("alice@kernel.org") == {
        "alpha": 1,
        "beta": 1,
    }
