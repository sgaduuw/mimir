"""Tests for mimir/web/routes/timeviews.py: `/<inbox>/today`,
`/<inbox>/yesterday`, `/<inbox>/since/<date>`, year and
month archive routes, and the date-shaped 404 guards."""

from tests.test_routes._helpers import _title_of


def test_inbox_today_route_renders_today_label(client, inbox_name, frozen_clock):
    """`/today` heading must reference the current date (UTC). A
    template change that hardcoded a static label would pass the
    smoke status check but mislabel every daily view."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/today").data.decode()
    assert today in body, f"today's date {today!r} missing from /today body"


def test_inbox_yesterday_route_renders_yesterday_label(
    client, inbox_name, frozen_clock
):
    from datetime import datetime, timedelta, timezone

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/yesterday").data.decode()
    assert yesterday in body


def test_inbox_since_smoke_recent(client, inbox_name, frozen_clock):
    """`/since/<recent-date>` resolves and renders without the cap
    notice; the seeded archive is older than 90d so the body just
    says 'no messages in this window' but the route still 200s."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{recent}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" not in body
    assert recent in body


def test_inbox_since_caps_window_with_notice(client, inbox_name, frozen_clock):
    """A since-date older than 90 days clamps the window to the
    90-day floor and the template surfaces a notice so the operator
    sees why the window starts where it does."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{old}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" in body
    # The effective-since date (today - 90d) appears in the notice.
    floor = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    assert floor in body


def test_inbox_since_malformed_date_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/since/not-a-date").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-13-01").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-02-30").status_code == 404


def test_inbox_since_future_date_404(client, inbox_name, frozen_clock):
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
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
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
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
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    now = datetime.now(timezone.utc)
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
    from mimir.web import _year_decade_groups

    out = _year_decade_groups(1996, 2026)
    decades = [decade for decade, _ in out]
    assert decades == [2020, 2010, 2000, 1990]
    # Each list is descending within the decade.
    assert out[0][1] == [2026, 2025, 2024, 2023, 2022, 2021, 2020]
    assert out[3][1] == [1999, 1998, 1997, 1996]


def test_year_decade_groups_single_decade():
    from mimir.web import _year_decade_groups

    out = _year_decade_groups(2024, 2026)
    assert out == [(2020, [2026, 2025, 2024])]


def test_year_decade_groups_single_year():
    from mimir.web import _year_decade_groups

    assert _year_decade_groups(2025, 2025) == [(2020, [2025])]


def test_year_decade_groups_handles_decade_boundary():
    """1999 → 2010 spans 3 decades; each gets exactly the years that
    belong to it (no off-by-one wrap)."""
    from mimir.web import _year_decade_groups

    out = _year_decade_groups(1999, 2010)
    assert out == [
        (2010, [2010]),
        (2000, [2009, 2008, 2007, 2006, 2005, 2004, 2003, 2002, 2001, 2000]),
        (1990, [1999]),
    ]


def test_year_decade_groups_returns_empty_when_inverted():
    from mimir.web import _year_decade_groups

    assert _year_decade_groups(2026, 1996) == []
