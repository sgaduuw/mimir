"""Cross-inbox maintainer feed identity, discovery and rolling-window contracts."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

from mimir.models import Article, ArticleList, ArticleTrailer, Inbox, Mailmap
from tests._dom import parse
from tests.test_routes.test_maintainers import _add_subsystem

NS = {"a": "http://www.w3.org/2005/Atom"}
ADDRESS = "foo@bar.org"
PATH = "/maintainers/foo@bar.org/feed.atom"


def _maintainer(session, address=ADDRESS, name="Foo Maintainer"):
    _add_subsystem(session, "FEED OWNER", "Maintained", [("M", name, address)])


def _message(session, title, author, *, inboxes=("alpha",), age=0, canonical=None):
    article = Article(
        message_id=title + "@feed-test",
        subject=title,
        subject_normalized=title,
        author=author,
        date=datetime.now(UTC) - timedelta(seconds=age),
    )
    session.add(article)
    session.flush()
    for name in inboxes:
        inbox = session.scalar(select(Inbox).where(Inbox.name == name))
        session.add(
            ArticleList(
                article_id=article.id,
                inbox_id=inbox.id,
                epoch="0.git",
                commit_sha="a" * 40,
                thread_root_id=article.id,
            )
        )
        if name == canonical:
            article.canonical_inbox_id = inbox.id
    session.commit()
    return article


def _entries(response):
    assert response.status_code == 200
    assert response.mimetype == "application/atom+xml"
    return ET.fromstring(response.data).findall("a:entry", NS)


def test_maintainer_feed_exact_alias_identity_crossposts_and_links(
    client, session, frozen_clock
):
    _maintainer(session)
    session.add(Mailmap(old_address="old@bar.org", address=ADDRESS))
    session.commit()
    cross = _message(
        session,
        "cross",
        "Foo <FOO@BAR.ORG>",
        inboxes=("alpha", "beta"),
        canonical="beta",
    )
    _message(session, "alias", "Old <old@bar.org>", inboxes=("beta",), age=1)
    _message(session, "bare", ADDRESS, age=2)
    for index, author in enumerate(
        ("xfoo@bar.org", "foo@bar.org.evil", '"foo@bar.org" <someone@else.org>')
    ):
        _message(session, f"imposter-{index}", author)
    _message(session, "unconfigured", ADDRESS, inboxes=())
    _message(session, "too-old", ADDRESS, age=5 * 86400 + 1)
    reviewed = _message(session, "review-only", "Someone <someone@else.org>")
    session.add(
        ArticleTrailer(
            article_id=reviewed.id,
            role="Reviewed-by",
            name="Foo",
            address=ADDRESS,
            address_normalized=ADDRESS,
        )
    )
    session.commit()
    response = client.get(PATH)
    entries = _entries(response)
    assert [e.findtext("a:title", namespaces=NS) for e in entries] == [
        "cross",
        "alias",
        "bare",
    ]
    root = ET.fromstring(response.data)
    assert root.findtext("a:id", namespaces=NS) == "http://localhost" + PATH
    assert (
        root.findtext("a:title", namespaces=NS)
        == "Foo Maintainer | Maintainers | mimir"
    )
    assert (
        root.find("a:link[@rel='self']", NS).attrib["href"] == "http://localhost" + PATH
    )
    assert (
        root.find("a:link[@rel='alternate']", NS).attrib["href"]
        == "http://localhost/maintainers/" + ADDRESS
    )
    expected_link = f"http://localhost/beta/2024/06/{cross.id}/t#m{cross.id}"
    assert entries[0].find("a:link", NS).attrib["href"] == expected_link
    assert entries[0].findtext("a:id", namespaces=NS).endswith(f":beta/{cross.id}")
    assert response.cache_control.public
    assert 0 < response.cache_control.max_age <= 300
    assert response.headers.get("ETag") and "Last-Modified" not in response.headers


def test_maintainer_feed_filters_cached_age_out_and_http_deadline(
    client, session, frozen_clock
):
    _maintainer(session)
    _message(session, "edge", ADDRESS, age=5 * 86400 - 30)
    _message(session, "recent", ADDRESS)
    assert len(_entries(client.get(PATH))) == 2
    frozen_clock.tick(60)
    second = client.get(PATH)
    assert [e.findtext("a:title", namespaces=NS) for e in _entries(second)] == [
        "recent"
    ]
    assert second.cache_control.max_age == 240


@pytest.mark.parametrize(
    "address", ["nobody@bar.org", "bad/addr@bar.org", "bad!addr@bar.org", "a"]
)
def test_unknown_or_unaddressable_maintainer_feed_404(client, session, address):
    if address != "nobody@bar.org":
        _maintainer(session, address)
    assert client.get("/maintainers/" + address + "/feed.atom").status_code == 404


def test_maintainer_feed_redirects_only_retired_identity(client, session):
    _maintainer(session)
    session.add(Mailmap(old_address="old@bar.org", address=ADDRESS))
    session.commit()
    response = client.get("/maintainers/OLD@BAR.ORG/feed.atom")
    assert response.status_code == 301
    assert response.headers["Location"] == PATH
    assert client.get(PATH).status_code == 200
    assert client.get("/maintainers/FOO@BAR.ORG/feed.atom").status_code == 200


def test_sitemap_profiles_discover_working_feeds_without_listing_feeds(client, session):
    from tests.test_routes._helpers import _clear_sitemap_cache

    for address, name in [
        ("first.last@bar.org", "First Last"),
        ("foo+tag@bar.org", "Foo"),
        ("some_one@bar.org", "private@name.org"),
        ("Case@Bar.org", ""),
    ]:
        _maintainer(session, address, name)
    _clear_sitemap_cache()
    response = client.get("/sitemap-maintainers.xml")
    assert response.status_code == 200
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [n.text for n in ET.fromstring(response.data).findall("s:url/s:loc", ns)]
    assert len(locs) == 4
    for loc in locs:
        path = urlsplit(loc).path
        profile = parse(client.get(path).get_data(as_text=True))
        discovery = profile.find("head").find(
            "link", rel="alternate", type="application/atom+xml"
        )
        visible = profile.find("header", cls="maintainer-heading").find_all("a")
        assert len(visible) == 1
        assert visible[0].text() == "Atom feed"
        assert (
            discovery.attrs["href"] == visible[0].attrs["href"] == path + "/feed.atom"
        )
        assert discovery.attrs["title"] == profile.find("h1").text()
        feed = client.get(discovery.attrs["href"])
        _entries(feed)
        root = ET.fromstring(feed.data)
        assert (
            root.findtext("a:title", namespaces=NS)
            == profile.find("h1").text() + " | Maintainers | mimir"
        )
    for endpoint in (
        "/sitemap-maintainers.xml",
        "/meta-sitemap.xml",
        "/sitemap.xml",
        "/alpha/sitemap.xml",
    ):
        xml = client.get(endpoint)
        assert xml.status_code == 200
        values = [
            node.text
            for node in ET.fromstring(xml.data).iter()
            if node.tag.endswith("loc")
        ]
        assert all(not value.endswith("/feed.atom") for value in values)


@pytest.mark.parametrize(
    "sender",
    ["foo (old) @bar.org", "foo @bar.org", "foo@bar .org", "foo\t@bar.org"],
)
def test_maintainer_feed_matches_parsed_noncontiguous_addresses(
    client, session, frozen_clock, sender
):
    from email.utils import parseaddr

    assert parseaddr(sender)[1].lower() == ADDRESS
    _maintainer(session)
    _message(session, "unusual-from", sender)
    assert [
        e.findtext("a:title", namespaces=NS) for e in _entries(client.get(PATH))
    ] == ["unusual-from"]


def test_maintainer_feed_filters_exact_identity_before_cap_and_uses_date_index(
    session, seeded_db, frozen_clock
):
    from mimir.maintainer_feed import maintainer_feed_articles
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    valid = [_message(session, f"valid-{i}", ADDRESS, age=20 + i) for i in range(3)]
    for i in range(5):
        _message(session, f"newer-spoof-{i}", f'"{ADDRESS}" <other@else.org>', age=i)
    floor = datetime.now(UTC) - timedelta(days=5)
    observed = []
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as plan_session:
        plan = plan_of(
            plan_session,
            lambda: observed.extend(
                maintainer_feed_articles(plan_session, [ADDRESS], floor, 2)
            ),
            "INDEXED BY ix_articles_date",
            required_sql=("EXISTS",),
        )
    assert [row.id for row in observed] == [row.id for row in valid[:2]]
    assert "ix_articles_date" in plan
    assert "SEARCH al" in plan


def test_maintainer_feed_listing_cache_follows_mailmap_version(
    client, session, frozen_clock
):
    from mimir.models import MainlineState

    _maintainer(session)
    session.merge(MainlineState(tree_name="linus", last_commit_sha="rules:a"))
    session.commit()
    _message(session, "old-alias", "old@bar.org")
    assert _entries(client.get(PATH)) == []
    session.add(Mailmap(old_address="old@bar.org", address=ADDRESS))
    session.merge(MainlineState(tree_name="linus", last_commit_sha="rules:b"))
    session.commit()
    assert [
        e.findtext("a:title", namespaces=NS) for e in _entries(client.get(PATH))
    ] == ["old-alias"]


def test_maintainer_feed_warm_links_and_profile_removal(
    client, session, frozen_clock, monkeypatch
):
    from mimir.models import SubsystemMaintainer
    from mimir.web import urls

    _maintainer(session)
    _message(session, "warm", ADDRESS)
    first = _entries(client.get(PATH))
    assert len(first) == 1

    def forbidden(*args, **kwargs):
        pytest.fail("Warm maintainer feed recomputed canonical links")

    monkeypatch.setattr(urls, "_canonical_destinations_for", forbidden)
    assert ET.tostring(_entries(client.get(PATH))[0]) == ET.tostring(first[0])
    owner = session.scalar(
        select(SubsystemMaintainer).where(SubsystemMaintainer.address == ADDRESS)
    )
    owner.role = "R"
    session.commit()
    assert client.get(PATH).status_code == 404


def test_maintainer_feed_unicode_alias_and_literal_wildcard_address(
    client, session, frozen_clock
):
    _maintainer(session, "percent%_alias@bar.org")
    _message(session, "literal", "percent%_alias@bar.org")
    _message(session, "wildcard-imposter", "percentZZalias@bar.org")
    assert [
        e.findtext("a:title", namespaces=NS)
        for e in _entries(client.get("/maintainers/percent%25_alias@bar.org/feed.atom"))
    ] == ["literal"]
    _maintainer(session)
    session.add(Mailmap(old_address="älias@bar.org", address=ADDRESS))
    session.commit()
    _message(session, "unicode-alias", "ÄLIAS@BAR.ORG")
    assert [
        e.findtext("a:title", namespaces=NS) for e in _entries(client.get(PATH))
    ] == ["unicode-alias"]


def test_maintainer_feed_default_cap_is_newest_thousand(client, session, frozen_clock):
    _maintainer(session)
    inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    articles = [
        Article(
            message_id=f"cap-{i}@feed-test",
            subject=f"cap-{i}",
            author=ADDRESS,
            date=datetime.now(UTC),
        )
        for i in range(1002)
    ]
    session.add_all(articles)
    session.flush()
    session.add_all(
        [
            ArticleList(
                article_id=a.id, inbox_id=inbox.id, epoch="0.git", commit_sha="a" * 40
            )
            for a in articles
        ]
    )
    session.commit()
    entries = _entries(client.get(PATH))
    assert len(entries) == 1000
    assert [entry.findtext("a:title", namespaces=NS) for entry in entries] == [
        f"cap-{i}" for i in range(1001, 1, -1)
    ]


@pytest.mark.parametrize("sender", ["K@bar.org", "k@bar.orK"])
def test_maintainer_feed_keeps_unicode_case_equivalent_sender(
    client, session, frozen_clock, sender
):
    from email.utils import parseaddr

    address = parseaddr(sender)[1].lower()
    assert address in {"k@bar.org", "k@bar.ork"}
    assert address.isascii() and not sender.isascii()
    _maintainer(session, address)
    _message(session, "unicode-case", sender)
    response = client.get("/maintainers/" + address + "/feed.atom")
    assert [e.findtext("a:title", namespaces=NS) for e in _entries(response)] == [
        "unicode-case"
    ]


def test_live_maintainer_keeps_profile_and_feed_despite_mailmap_mapping(
    client, session
):
    _maintainer(session, "old@bar.org", "Old Maintainer")
    _maintainer(session, "current@bar.org", "Current Maintainer")
    session.add(Mailmap(old_address="old@bar.org", address="current@bar.org"))
    session.commit()
    profile = client.get("/maintainers/old@bar.org")
    assert profile.status_code == 200
    canonical = parse(profile.get_data(as_text=True)).find("link", rel="canonical")
    assert canonical.attrs["href"] == "http://localhost/maintainers/old@bar.org"
    response = client.get("/maintainers/old@bar.org/feed.atom")
    _entries(response)
    assert (
        ET.fromstring(response.data).findtext("a:id", namespaces=NS)
        == "http://localhost/maintainers/old@bar.org/feed.atom"
    )
