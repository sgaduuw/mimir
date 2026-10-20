"""Tests for mimir/web/routes/timeviews.py: `/<inbox>/today`,
`/<inbox>/yesterday`, `/<inbox>/since/<date>`, year and
month archive routes, and the date-shaped 404 guards."""

from datetime import UTC

from tests.test_routes._helpers import _title_of


def test_inbox_today_route_renders_today_label(client, inbox_name, frozen_clock):
    """`/today` heading must reference the current date (UTC). A
    template change that hardcoded a static label would pass the
    smoke status check but mislabel every daily view."""
    from datetime import datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/today").data.decode()
    assert today in body, f"today's date {today!r} missing from /today body"


def test_inbox_yesterday_route_renders_yesterday_label(
    client, inbox_name, frozen_clock
):
    from datetime import datetime, timedelta

    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/yesterday").data.decode()
    assert yesterday in body


def test_inbox_since_smoke_recent(client, inbox_name, frozen_clock):
    """`/since/<recent-date>` resolves and renders without the cap
    notice; the seeded archive is older than 90d so the body just
    says 'no messages in this window' but the route still 200s."""
    from datetime import datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{recent}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" not in body
    assert recent in body


def test_inbox_since_caps_window_with_notice(client, inbox_name, frozen_clock):
    """A since-date older than 90 days clamps the window to the
    90-day floor and the template surfaces a notice so the operator
    sees why the window starts where it does."""
    from datetime import datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{old}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" in body
    # The effective-since date (today - 90d) appears in the notice.
    floor = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
    assert floor in body


def test_inbox_since_malformed_date_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/since/not-a-date").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-13-01").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-02-30").status_code == 404


def test_inbox_since_future_date_404(client, inbox_name, frozen_clock):
    from datetime import datetime, timedelta

    future = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
    assert client.get(f"/{inbox_name}/since/{future}").status_code == 404


def test_inbox_year_archive_renders_year_label(client, inbox_name):
    body = client.get(f"/{inbox_name}/2024/").data.decode()
    # Page title block lifts the year into <title>.
    assert "<title>2024 | " in body
    # The h2 heading carries the "archive · 2024" pattern (template-
    # stable across nested <small>/<em> wrappers around the year).
    assert "archive · 2024" in body


def test_inbox_month_archive_renders_month_label(client, inbox_name):
    body = client.get(f"/{inbox_name}/2024/05/").data.decode()
    # "May 2024" or similar, title block has at least the year + month
    # in some recognisable form. Pin "2024" + a month-name fragment.
    assert "2024" in body
    assert any(
        m in body
        for m in (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    ), "month label missing"


def test_year_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/1990/").status_code == 404
    # _max_archive_year() = current_year + 1; pick something solidly above.
    assert client.get(f"/{inbox_name}/2999/").status_code == 404


def test_month_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/2024/0/").status_code == 404
    assert client.get(f"/{inbox_name}/2024/13/").status_code == 404


def test_year_archive_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/2024/").data.decode())
    assert title == f"2024 | {inbox_name} | mimir"


def test_month_archive_title(client, inbox_name):
    # Month label format is "Month YYYY", just sanity-check the
    # separator + scope tokens are present.
    title = _title_of(client.get(f"/{inbox_name}/2024/05/").data.decode())
    assert title.endswith(f" | {inbox_name} | mimir")
    assert "2024" in title


def test_daily_today_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/today").data.decode())
    assert " | " in title
    assert title.endswith(f" | {inbox_name} | mimir")


def test_since_title_shape(client, inbox_name, frozen_clock):
    from datetime import datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d")
    title = _title_of(client.get(f"/{inbox_name}/since/{recent}").data.decode())
    assert title.endswith(f" | {inbox_name} | mimir")
    assert recent in title


def test_daily_view_counts_messages_in_window(client, frozen_clock):
    """Pin the count-by-window behaviour on `_daily_view`: an
    article sent today is counted, one sent yesterday is not.
    Audit (2026-05-15) flagged the previous
    `Article.date >= start.strftime(...)` form as brittle on
    SQLA 2.x typing; the datetime-comparison form keeps the
    column's DateTime type live across the round-trip."""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # Seed two articles: one today, one yesterday. The /today
        # view should see the today one only.
        today_art = Article(
            message_id="today-win@example.com",
            subject="today",
            author="a",
            date=now,
            thread_parent=None,
            subject_normalized="today",
        )
        yest_art = Article(
            message_id="yest-win@example.com",
            subject="yesterday",
            author="a",
            date=yesterday,
            thread_parent=None,
            subject_normalized="yesterday",
        )
        s.add_all([today_art, yest_art])
        s.flush()
        s.add_all(
            [
                ArticleList(
                    article_id=today_art.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="aa" * 20,
                ),
                ArticleList(
                    article_id=yest_art.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="bb" * 20,
                ),
            ]
        )
        s.commit()

    r = client.get("/alpha/today")
    assert r.status_code == 200
    body = r.data.decode()
    # The total appears as "N messages across …"; pin the digit
    # without coupling to surrounding template prose.
    import re

    m = re.search(r"(\d+)\s+messages? across", body)
    assert m is not None, f"didn't find message count in rendered body:\n{body[:400]}"
    count = int(m.group(1))
    assert count >= 1, (
        "today's article should be inside the window; the strftime-"
        f"to-datetime change must keep the count correct (got {count})"
    )
    # Yesterday's article must NOT be counted in the /today view.
    # If the SQL comparison silently broke and counted everything,
    # this would be 2+.
    assert count == 1, (
        f"only today's seeded article should match /today; got count={count}"
    )


def test_year_decade_groups_groups_by_decade():
    from mimir.web.urls import _year_decade_groups

    out = _year_decade_groups(1996, 2026)
    decades = [decade for decade, _ in out]
    assert decades == [2020, 2010, 2000, 1990]
    # Each list is descending within the decade.
    assert out[0][1] == [2026, 2025, 2024, 2023, 2022, 2021, 2020]
    assert out[3][1] == [1999, 1998, 1997, 1996]


def test_year_decade_groups_single_decade():
    from mimir.web.urls import _year_decade_groups

    out = _year_decade_groups(2024, 2026)
    assert out == [(2020, [2026, 2025, 2024])]


def test_year_decade_groups_single_year():
    from mimir.web.urls import _year_decade_groups

    assert _year_decade_groups(2025, 2025) == [(2020, [2025])]


def test_year_decade_groups_handles_decade_boundary():
    """1999 → 2010 spans 3 decades; each gets exactly the years that
    belong to it (no off-by-one wrap)."""
    from mimir.web.urls import _year_decade_groups

    out = _year_decade_groups(1999, 2010)
    assert out == [
        (2010, [2010]),
        (2000, [2009, 2008, 2007, 2006, 2005, 2004, 2003, 2002, 2001, 2000]),
        (1990, [1999]),
    ]


def test_year_decade_groups_returns_empty_when_inverted():
    from mimir.web.urls import _year_decade_groups

    assert _year_decade_groups(2026, 1996) == []


def _seed_since_threads(n):
    """`n` single-message threads in the primary inbox, one per hour
    ending an hour before the frozen clock; thread `i` is newer than
    thread `i - 1`. Returns their message ids, oldest first."""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from tests.conftest import TEST_INBOX_PRIMARY

    now = datetime.now(UTC)
    ids = []
    with SessionLocal() as s:
        inbox = s.execute(
            select(Inbox).where(Inbox.name == TEST_INBOX_PRIMARY)
        ).scalar_one()
        for i in range(n):
            mid = f"since-cap-{i}@example.com"
            art = Article(
                message_id=mid,
                subject=f"since cap {i}",
                author="X",
                date=now - timedelta(hours=n - i),
                thread_parent=None,
                subject_normalized=f"since cap {i}",
            )
            s.add(art)
            s.flush()
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha=f"{i:040x}",
                )
            )
            ids.append(mid)
        s.commit()
    return ids


def _shrink_since_cap(monkeypatch, cap):
    # The route and the helper each bound the name at import time.
    monkeypatch.setattr("mimir.threading.THREADS_SINCE_MAX_THREADS", cap)
    monkeypatch.setattr("mimir.web.routes.timeviews.THREADS_SINCE_MAX_THREADS", cap)


def test_since_lists_only_the_cap_newest_first_with_notice(
    client, inbox_name, frozen_clock, monkeypatch
):
    """More threads than the cap: render exactly the cap, most
    recently active first, and say the list was cut."""
    from datetime import datetime, timedelta

    _shrink_since_cap(monkeypatch, 3)
    ids = _seed_since_threads(5)
    recent = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/since/{recent}").data.decode()
    shown = [mid for n, mid in enumerate(ids) if f"since cap {n}<" in body]
    assert shown == ids[2:], "only the 3 newest threads belong on the page"
    assert (
        body.index("since cap 4<")
        < body.index("since cap 3<")
        < body.index("since cap 2<")
    )
    assert "Showing the 3 most recently active threads" in body
    # A later date only drops older threads, so the notice must not
    # suggest one would show the rest (#799 review).
    assert "later date" not in body
    assert "5 messages in this window" in body


def test_since_exactly_the_cap_has_no_notice(
    client, inbox_name, frozen_clock, monkeypatch
):
    from datetime import datetime, timedelta

    _shrink_since_cap(monkeypatch, 3)
    _seed_since_threads(3)
    recent = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/since/{recent}").data.decode()
    assert "most recently active threads" not in body
    assert "3 messages across" in body


def test_since_pre_floor_dates_share_one_compute(
    client, inbox_name, frozen_clock, monkeypatch
):
    """Two different dates before the 90-day floor are the same
    window: one cache key, one compute."""
    from datetime import datetime, timedelta

    from mimir import threading as mimir_threading

    calls = []
    real = mimir_threading._active_threads_query

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mimir_threading, "_active_threads_query", counting)
    now = datetime.now(UTC)
    for days in (200, 365):
        old = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        assert client.get(f"/{inbox_name}/since/{old}").status_code == 200
    assert len(calls) == 1


def test_since_lifecycle_gets_at_most_the_cap_ids(
    client, inbox_name, frozen_clock, monkeypatch
):
    from datetime import datetime, timedelta

    seen = []

    def spy(session, article_ids, inbox_id, **kwargs):
        seen.append(list(article_ids))
        return {}

    _shrink_since_cap(monkeypatch, 3)
    monkeypatch.setattr("mimir.web.routes.timeviews.lifecycle_status_for_articles", spy)
    _seed_since_threads(5)
    recent = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    assert client.get(f"/{inbox_name}/since/{recent}").status_code == 200
    assert [len(ids) for ids in seen] == [3]


def test_since_total_uses_the_same_window_start_as_the_threads(
    client, inbox_name, frozen_clock
):
    """Capped window: the thread list starts at `now - 90 days`, so
    the total must too. Counting from that day's midnight would add a
    message the list cannot show (#801)."""
    import re
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.threading import THREADS_SINCE_MAX_DAYS

    now = datetime.now(UTC)
    # FROZEN_NOW is mid-day, so two hours before the exact floor is
    # still after that day's midnight.
    dates = {
        "inside": now - timedelta(hours=1),
        "before-floor": now - timedelta(days=THREADS_SINCE_MAX_DAYS, hours=2),
    }
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        for i, (label, when) in enumerate(dates.items()):
            art = Article(
                message_id=f"window-{label}@example.com",
                subject=label,
                author="X",
                date=when,
                thread_parent=None,
                subject_normalized=label,
            )
            s.add(art)
            s.flush()
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha=f"{i:040x}",
                )
            )
        s.commit()
    body = client.get(f"/{inbox_name}/since/2000-01-01").data.decode()
    assert re.search(r"\b1 message\s*across", body), body[:600]
