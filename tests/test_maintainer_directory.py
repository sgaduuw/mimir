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
