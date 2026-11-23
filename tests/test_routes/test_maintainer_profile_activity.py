"""Profile activity reuses a complete feed snapshot without cold computation."""

from xml.etree import ElementTree as ET

import pytest

from tests._dom import parse
from tests.test_routes._helpers import _json_ld_blocks
from tests.test_routes.test_maintainer_feeds import (
    ADDRESS,
    NS,
    PATH,
    _maintainer,
    _message,
)

PROFILE = "/maintainers/" + ADDRESS


def _forbid_feed_work(monkeypatch):
    from mimir.seo import feed_data
    from mimir.web import urls

    def forbidden(*args, **kwargs):
        pytest.fail("Profile attempted cold feed computation")

    monkeypatch.setattr(feed_data, "maintainer_feed_articles", forbidden)
    monkeypatch.setattr(urls, "_canonical_destinations_for", forbidden)


def test_profile_uses_feed_urls_and_dates_only_rendered_activity(
    client, session, frozen_clock, monkeypatch
):
    _maintainer(session)
    article = _message(session, "recent-profile", ADDRESS, inboxes=("beta",))
    feed = client.get(PATH)
    assert feed.status_code == 200
    entry = ET.fromstring(feed.data).find("a:entry", NS)
    expected = entry.find("a:link", NS).attrib["href"]
    _forbid_feed_work(monkeypatch)
    response = client.get(PROFILE)
    assert response.status_code == 200
    dom = parse(response.get_data(as_text=True))
    recent = dom.find("section", id="recent-messages")
    assert recent.find("a").attrs["href"] == expected.removeprefix("http://localhost")
    assert recent.find("a").text() == "recent-profile"
    assert "beta" in recent.text()
    assert recent.find("time").attrs["datetime"].startswith(article.date.isoformat())
    payload = _json_ld_blocks(response.get_data(as_text=True))[0]
    assert payload["dateModified"] == recent.find("time").attrs["datetime"]
    assert "email" not in payload["mainEntity"]


def test_profile_distinguishes_cold_activity_from_cached_empty_feed(
    client, session, monkeypatch, frozen_clock
):
    _maintainer(session)
    with monkeypatch.context() as cold:
        _forbid_feed_work(cold)
        response = client.get(PROFILE)
    assert response.status_code == 200
    assert not parse(response.get_data(as_text=True)).find_all(
        "section", id="recent-messages"
    )
    assert "dateModified" not in _json_ld_blocks(response.get_data(as_text=True))[0]
    assert client.get(PATH).status_code == 200
    _forbid_feed_work(monkeypatch)
    response = client.get(PROFILE)
    recent = parse(response.get_data(as_text=True)).find(
        "section", id="recent-messages"
    )
    assert "No messages in the last 5 days" in recent.text()
    assert "dateModified" not in _json_ld_blocks(response.get_data(as_text=True))[0]


def test_profile_hides_expired_links_even_when_listing_remains_warm(
    client, session, frozen_clock, monkeypatch
):
    from mimir import cache
    from mimir.dashboard import LISTING_CACHE_TTL_SEC

    _maintainer(session)
    _message(session, "expires", ADDRESS)
    with cache.warm_window(7200):
        assert client.get(PATH).status_code == 200
    assert parse(client.get(PROFILE).get_data(as_text=True)).find(
        "section", id="recent-messages"
    )
    frozen_clock.tick(LISTING_CACHE_TTL_SEC + 1)
    _forbid_feed_work(monkeypatch)
    response = client.get(PROFILE)
    assert not parse(response.get_data(as_text=True)).find_all(
        "section", id="recent-messages"
    )
    assert "dateModified" not in _json_ld_blocks(response.get_data(as_text=True))[0]
    assert response.headers["Cache-Control"] == "public, max-age=600"


def test_profile_limits_cached_activity_and_keeps_email_out_of_title(
    client, session, frozen_clock, monkeypatch
):
    _maintainer(session, name="private@elsewhere.org")
    for index in range(17):
        _message(session, f"activity-{index}", ADDRESS, age=index)
    assert client.get(PATH).status_code == 200
    _forbid_feed_work(monkeypatch)
    response = client.get(PROFILE)
    dom = parse(response.get_data(as_text=True))
    recent = dom.find("section", id="recent-messages")
    assert [link.text() for link in recent.find_all("a")] == [
        f"activity-{i}" for i in range(15)
    ]
    title = dom.find("title").text()
    assert "Linux kernel maintainer" in title and "@" not in title
    description = dom.find("meta", name="description").attrs["content"]
    assert len(description) <= 160 and "feed owner" in description


def test_profile_hides_activity_after_canonical_policy_change(
    client, session, frozen_clock, monkeypatch
):
    from mimir.config import settings

    _maintainer(session)
    _message(session, "policy-change", ADDRESS)
    assert client.get(PATH).status_code == 200
    assert parse(client.get(PROFILE).get_data(as_text=True)).find(
        "section", id="recent-messages"
    )
    monkeypatch.setattr(
        settings, "thread_view_render_cap", settings.thread_view_render_cap + 1
    )
    _forbid_feed_work(monkeypatch)
    response = client.get(PROFILE)
    assert not parse(response.get_data(as_text=True)).find_all(
        "section", id="recent-messages"
    )
    assert "dateModified" not in _json_ld_blocks(response.get_data(as_text=True))[0]
