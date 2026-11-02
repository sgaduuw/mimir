"""Inbox presentation uses cached inputs and fetches status only for active roots."""

import re
import signal
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.dashboard import ArchiveStats, ArticleSummary, DailyVolume
from mimir.threading import ActiveThread


@pytest.fixture
def dashboard_data(monkeypatch):
    from mimir.web.routes import dashboards

    when = datetime(2024, 6, 15, 11, tzinfo=UTC)
    active = [
        ActiveThread(
            101,
            "alpha",
            "active@x",
            "active subject",
            "Alice <private@x>",
            when,
            4,
            3,
            when,
        )
    ]
    message = ArticleSummary(102, "recent subject", "private@x", when)
    data = dict(
        active_threads=active,
        latest_pull_requests=[
            ArticleSummary(103, "[git pull] fixes", "private@x <private@x>", when)
        ],
        latest_stable_releases=[
            ArticleSummary(104, "Linux 6.12-rc3", "Bob <private@x>", when)
        ],
        this_day_in_history=[ArticleSummary(105, "history subject", None, when)],
        archive_stats=ArchiveStats(12345, 2, datetime(2001, 1, 2, tzinfo=UTC), when),
        daily_volume=DailyVolume([(when.date(), 7), (when.date(), 9)], 9),
        most_active_subsystems_in_inbox=[
            SimpleNamespace(name="NETWORKING", inbox_name="alpha", message_count=8),
            SimpleNamespace(name="FILESYSTEMS", inbox_name="alpha", message_count=2),
        ],
    )
    for name, value in data.items():
        monkeypatch.setattr(dashboards, name, Mock(return_value=value))
    monkeypatch.setattr(
        dashboards, "_fetch_recent", Mock(return_value=([message], True))
    )
    monkeypatch.setattr(dashboards, "author_recent", Mock(return_value=[message]))
    monkeypatch.setattr(dashboards, "_json_ld_inbox", Mock(return_value=None))
    status = Mock(return_value={})
    monkeypatch.setattr(dashboards, "lifecycle_status_for_articles", status)
    return dashboards, data, status


def test_dashboard_cached_header_layout_and_status_scope(client, dashboard_data):
    dashboards, data, status = dashboard_data
    body = client.get("/alpha/").get_data(as_text=True)
    assert "12,345 messages" in body
    assert "Since 2001-01-02" in body
    assert "16 in the last 30 days" in body
    assert 'class="inbox-main"' in body and '<aside class="inbox-sidebar"' in body
    assert body.index("Recent messages") < body.index("Latest pull requests")
    assert 'href="/alpha/today"' in body
    assert 'aria-label="3 replies in the last 7 days"' in body
    assert 'class="listing-row"' in body
    assert status.call_args.args[1] == [101]
    assert (
        dashboards.most_active_subsystems_in_inbox.call_args.kwargs["compute_on_miss"]
        is False
    )
    assert 'width="100.0"' in body and 'width="25.0"' in body
    assert 'href="/alpha/subsystem/"' in body
    assert "private@x" not in body
    assert "unknown sender" in body
    assert ">fixes</a>" in body
    assert ">6.12-rc3<" in body


def test_dashboard_empty_cached_inputs(client, dashboard_data):
    dashboards, _, _ = dashboard_data
    dashboards.archive_stats.return_value = ArchiveStats(0, 0, None, None)
    dashboards.daily_volume.return_value = DailyVolume([], 0)
    dashboards.most_active_subsystems_in_inbox.return_value = []
    body = client.get("/alpha/").get_data(as_text=True)
    assert "0 messages" in body and "0 in the last 30 days" in body
    assert "No messages yet" in body
    assert "Since " not in body
    assert 'href="/alpha/subsystem/"' in body


def test_recent_fragment_is_badge_free_and_does_not_fetch_status(client, monkeypatch):
    from mimir.web.routes import api

    def forbidden(*args, **kwargs):
        pytest.fail("recent fragments must not compute lifecycle status")

    monkeypatch.setattr(
        "mimir.lifecycle_status.lifecycle_status_for_articles", forbidden
    )
    monkeypatch.setattr(api, "lifecycle_status_for_articles", forbidden, raising=False)
    body = client.get("/api/alpha/recent?offset=0").get_data(as_text=True)
    assert 'class="recent-row"' in body
    assert "badge" not in body
    assert "unknown sender" not in body  # seeded authors have names
    assert "2024-03-01 12:00" in body


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[GIT PULL] fixes", "fixes"),
        ("[git pull] fixes", "fixes"),
        ("[GıT PULL] fixes", "[GıT PULL] fixes"),
        ("[GIT PULL]  ", "[GIT PULL]  "),
        (None, "(no subject)"),
    ],
)
def test_pull_label_uses_shared_ascii_predicate(subject, expected):
    from mimir.web.filters import _pull_label

    assert _pull_label(subject) == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Linux 6.12.1", "6.12.1"),
        ("Linux 6.13-rc2", "6.13-rc2"),
        ("Unexpected release announcement", "Unexpected release announcement"),
        (None, "(no subject)"),
    ],
)
def test_release_label_falls_back_to_readable_subject(subject, expected):
    from mimir.web.filters import _release_label

    assert _release_label(subject) == expected


@pytest.mark.parametrize("counts", [[0, 0], [None, 2], [None, None]])
def test_subsystem_bars_handle_empty_counts(client, dashboard_data, counts):
    dashboards, data, _ = dashboard_data
    for row, count in zip(data["most_active_subsystems_in_inbox"], counts, strict=True):
        row.message_count = count
    body = client.get("/alpha/").get_data(as_text=True)
    assert 'href="/alpha/subsystem/"' in body
    widths = [
        float(width)
        for width in re.findall(r'<rect class="bar-added" width="([^"]+)"', body)
    ]
    assert 0 in widths
    assert "None messages" not in body


def test_configured_trackers_keep_order_links_and_empty_states(client, dashboard_data):
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    dashboards, _, _ = dashboard_data
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        inbox.tracked_authors = {
            "Third label": "third",
            "First label": "first",
            "Extra label": "extra",
            "Empty label": "empty",
        }
        session.commit()
    dashboards.author_recent.side_effect = [
        [ArticleSummary(110 + i, f"tracker {i}", "anonymous@example.com", None)]
        for i in range(3)
    ] + [[]]
    body = client.get("/alpha/").get_data(as_text=True)
    positions = [
        body.index(f"Latest from {label}")
        for label in ["Third label", "First label", "Extra label", "Empty label"]
    ]
    assert positions == sorted(positions)
    assert body.count("No recent messages.") == 1
    for substr in ["third", "first", "extra", "empty"]:
        assert f'href="/alpha/author/{substr}"' in body
        assert f'href="/alpha/author/{substr}/feed.atom"' in body
    assert "anonymous@example.com" not in body


def test_recent_initial_and_htmx_pages_share_rows_offsets_and_exhaustion(
    client, dashboard_data, monkeypatch
):
    from mimir.web.routes import api

    dashboards, _, _ = dashboard_data
    message = ArticleSummary(120, "subject " * 40, "hidden@example.com", None)
    dashboards._fetch_recent.return_value = ([message] * 10, True)
    fetch = Mock(side_effect=[([message] * 10, True), ([message], False), ([], False)])
    monkeypatch.setattr(api, "_fetch_recent", fetch)
    initial = client.get("/alpha/").get_data(as_text=True)
    fragment = client.get("/api/alpha/recent?offset=10").get_data(as_text=True)
    final = client.get("/api/alpha/recent?offset=20").get_data(as_text=True)
    exhausted = client.get("/api/alpha/recent?offset=30").get_data(as_text=True)
    assert (
        initial.count('class="recent-row"')
        == fragment.count('class="recent-row"')
        == 10
    )
    initial_row = initial.split('<li class="recent-row">', 1)[1].split("</li>", 1)[0]
    fragment_row = fragment.split('<li class="recent-row">', 1)[1].split("</li>", 1)[0]
    assert initial_row == fragment_row
    assert 'offset=10"' in initial and 'offset=20"' in fragment
    assert 'hx-target="closest li"' in fragment and 'hx-swap="outerHTML"' in fragment
    assert final.count('class="recent-row"') == 1 and "Load more" not in final
    assert not exhausted.strip()
    assert [call.args[2:] for call in fetch.call_args_list] == [
        (10, 10),
        (20, 10),
        (30, 10),
    ]
    assert "badge" not in fragment and "hidden@example.com" not in fragment
    assert "Unknown date" in fragment


def test_release_label_cannot_stall_on_hostile_mail_subject():
    from mimir.web.filters import _release_label

    # This subject passes the announcement query's Linux [0-9]* GLOB.
    subject = "Linux 6.1" + "a" * 100_000 + "_"

    def timed_out(signum, frame):
        pytest.fail("release label exceeded two seconds on hostile mail text")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        assert _release_label(subject) == subject
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_populated_undated_archive_does_not_claim_empty(client):
    from sqlalchemy import select, update

    from mimir.dashboard import archive_stats
    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    with SessionLocal() as session:
        session.execute(update(Article).values(date=None))
        session.execute(update(Inbox).values(last_article_date=None))
        session.commit()
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        stats = archive_stats(session, inbox, force=True)
        assert stats.total > 0 and stats.last_date is None
    response = client.get("/alpha/")
    assert response.status_code == 200
    header = (
        response.get_data(as_text=True)
        .split('<header class="inbox-header">', 1)[1]
        .split("</header>", 1)[0]
    )
    assert "No messages yet" not in header
    assert "Latest date unknown" in header
