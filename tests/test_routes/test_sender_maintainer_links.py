"""Sender links share current maintainer membership and existing privacy rules."""

import pytest
from sqlalchemy import select

from mimir import maintainer_allowlist, maintainer_directory
from mimir.config import settings
from mimir.maintainer_directory import maintainer_path
from mimir.models import MainlineState, SubsystemMaintainer
from tests._dom import parse
from tests.test_routes._helpers import _ingest_one_article, _seed_three_message_thread
from tests.test_routes.test_maintainers import _add_subsystem

ADDRESS = "sender@example.org"
AUTHOR = "Sender Name <sender@example.org>"


def _sender_node(response, surface):
    assert response.status_code == 200
    dom = parse(response.get_data(as_text=True))
    if surface == "message":
        return dom.find("header", cls="mail-header").find("dl").find_all("dd")[0]
    return dom.find("p", cls="card-byline").find("strong")


@pytest.mark.parametrize("surface", ["message", "thread"])
@pytest.mark.parametrize("role", ["M", "R", None])
def test_sender_profile_link_keeps_role_gate_and_visible_text(
    client, session, tmp_path, monkeypatch, surface, role
):
    monkeypatch.setattr(settings, "email_allowlist", [])
    if role:
        _add_subsystem(
            session, "SENDERS", "Maintained", [(role, "Sender Name", ADDRESS)]
        )
    maintainer_allowlist.invalidate()
    _aid, url = _ingest_one_article(tmp_path, "alpha", "sender-link@x", author=AUTHOR)
    target = url if surface == "message" else url + "/t"
    node = _sender_node(client.get(target), surface)
    expected = "Sender Name"
    if surface == "message":
        expected += f" <{ADDRESS if role else 'hidden'}>"
    assert node.text() == expected
    links = node.find_all("a")
    assert len(links) == (1 if role == "M" else 0)
    if role == "M":
        assert links[0].attrs["href"] == maintainer_path(ADDRESS)
        profile = client.get(links[0].attrs["href"])
        assert profile.status_code == 200
        canonical = parse(profile.get_data(as_text=True)).find("link", rel="canonical")
        assert canonical.attrs["href"] == "http://localhost" + links[0].attrs["href"]


def test_thread_sender_membership_set_loads_once_per_request(
    client, session, tmp_path, monkeypatch
):
    _add_subsystem(session, "SENDERS", "Maintained", [("M", "Sender Name", ADDRESS)])
    maintainer_allowlist.invalidate()
    messages = _seed_three_message_thread(tmp_path, "alpha", authors=(AUTHOR,) * 3)
    calls = []
    original = maintainer_directory.all_maintainers

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(maintainer_directory, "all_maintainers", counted)
    response = client.get(messages["root"][1] + "/t")
    assert response.status_code == 200
    bylines = parse(response.get_data(as_text=True)).find_all("p", cls="card-byline")
    assert len(bylines) == 3
    assert [row.find("strong").find("a").attrs["href"] for row in bylines] == [
        maintainer_path(ADDRESS)
    ] * 3
    assert len(calls) == 1


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_sender_role_reload_invalidates_cached_conditional_page(
    client, session, tmp_path, surface
):
    _add_subsystem(session, "SENDERS", "Maintained", [("M", "Sender Name", ADDRESS)])
    session.merge(MainlineState(tree_name="linus", last_commit_sha="sender-before"))
    session.commit()
    maintainer_allowlist.invalidate()
    _aid, url = _ingest_one_article(tmp_path, "alpha", "sender-reload@x", author=AUTHOR)
    target = url if surface == "message" else url + "/t"
    first = client.get(target)
    assert len(_sender_node(first, surface).find_all("a")) == 1
    owner = session.scalar(
        select(SubsystemMaintainer).where(SubsystemMaintainer.address == ADDRESS)
    )
    owner.role = "R"
    session.merge(MainlineState(tree_name="linus", last_commit_sha="sender-after"))
    session.commit()
    maintainer_allowlist.invalidate()
    second = client.get(target, headers={"If-None-Match": first.headers["ETag"]})
    assert second.status_code == 200
    assert second.headers["ETag"] != first.headers["ETag"]
    assert _sender_node(second, surface).text() == _sender_node(first, surface).text()
    assert not _sender_node(second, surface).find_all("a")
