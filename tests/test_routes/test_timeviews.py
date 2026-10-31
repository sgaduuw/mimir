"""Tests for mimir/web/routes/timeviews.py: `/<inbox>/today`,
`/<inbox>/yesterday`, `/<inbox>/since/<date>`, year and
month archive routes, and the date-shaped 404 guards."""

import re
from datetime import UTC

from freezegun import freeze_time

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
    assert "<h1>2024</h1>" in body


def test_year_archive_has_inbox_breadcrumb(client, inbox_name):
    body = client.get(f"/{inbox_name}/2024/").data.decode()
    # Pico's breadcrumb: its selector is the lowercase label over `ul > li`,
    # and anything else falls back to its plain nav, which pushes the year
    # to the far right.
    assert (
        f'<nav aria-label="breadcrumb"><ul><li><a href="/{inbox_name}/">'
        f"{inbox_name}</a></li><li>2024</li></ul></nav>"
    ) in body


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


# --- year archive rows (#998) ---

_MONTH_HREF_RE = re.compile(r'href="(/alpha/2024/\d\d/)"')


def _year_rows(body: str) -> list[str]:
    return re.findall(r'<li class="year-row[^"]*">.*?</li>', body, re.DOTALL)


def test_year_archive_is_twelve_rows(client, frozen_clock):
    body = client.get("/alpha/2024/").data.decode()
    assert len(_year_rows(body)) == 12


def test_year_archive_empty_months_are_not_links(client, frozen_clock):
    """alpha seeds Jan (2) and Mar (1) of 2024; every other month is
    empty and must render as plain text."""
    body = client.get("/alpha/2024/").data.decode()
    assert _MONTH_HREF_RE.findall(body) == ["/alpha/2024/01/", "/alpha/2024/03/"]


def test_year_archive_bar_is_proportional_to_busiest_month(client, frozen_clock):
    body = client.get("/alpha/2024/").data.decode()
    widths = [float(w) for w in re.findall(r'<rect[^>]*\bwidth="([\d.]+)"', body)]
    # Jan 2 is the busiest month, Mar 1 is half of it; empty rows
    # draw no bar.
    assert widths == [100.0, 50.0]


def test_year_archive_bar_is_an_svg_with_a_text_equivalent(client, frozen_clock):
    """The CSP forbids `style=`, so the bar is an SVG whose width is a
    `rect` attribute; the aria-label is its text equivalent (#988)."""
    body = client.get("/alpha/2024/").data.decode()
    svgs = re.findall(r'<svg class="bar-meter".*?</svg>', body, re.DOTALL)
    assert len(svgs) == 2
    assert 'aria-label="100% of the busiest month"' in svgs[0]
    assert 'aria-label="50% of the busiest month"' in svgs[1]
    for svg in svgs:
        assert 'viewBox="0 0 100 6"' in svg
        assert 'role="img"' in svg
        assert 'fill="currentColor"' in svg
        assert "style=" not in svg


def test_year_archive_bar_width_is_rounded(client, frozen_clock, tmp_path):
    """The bar width is rounded to one decimal, not a long float (#988).
    alpha seeds Jan (2) and Mar (1) of 2024; one more January message
    makes Mar a third of the busiest month."""
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(
        tmp_path,
        "alpha",
        [("jan-extra@x", None)],
        date_for={"jan-extra@x": "Mon, 15 Jan 2024 12:00:00 +0000"},
    )
    body = client.get("/alpha/2024/").data.decode()
    widths = re.findall(r'<rect[^>]*\bwidth="([^"]+)"', body)
    assert widths == ["100.0", "33.3"]


def test_year_archive_empty_month_label_follows_the_month(client):
    """ "not yet" for the current month and later ones, "none" for every
    other empty month, in any year."""
    with freeze_time("2024-06-15 12:00:00"):
        current = client.get("/alpha/2024/").data.decode()
    with freeze_time("2026-06-15 12:00:00"):
        past = client.get("/alpha/2024/").data.decode()
    label = lambda body: re.findall(  # noqa: E731
        r'<span class="count count--none">([^<]*)</span>', body
    )
    # Empty in 2024: Feb, Apr, May are over; Jun (the current month)
    # and Jul-Dec are still to come.
    assert label(current) == ["none"] * 3 + ["not yet"] * 7
    assert label(past) == ["none"] * 10


def test_year_archive_hides_next_year_without_data(client, frozen_clock):
    # 2025 has no articles: no link forward from 2024 ...
    assert 'href="/alpha/2025/"' not in client.get("/alpha/2024/").data.decode()
    # ... but 2023 still links to 2024, which has data, and back to 2022.
    body = client.get("/alpha/2023/").data.decode()
    assert 'href="/alpha/2024/"' in body
    assert 'href="/alpha/2022/"' in body


def test_daily_reply_count_links_to_the_thread_view(client, tmp_path, frozen_clock):
    """The "N replies today" link on `/<inbox>/today` is the thread's `/t`,
    the same URL the thread page names as canonical (#1016)."""
    from datetime import datetime, timedelta

    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from mimir.web.urls import _msg_url
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("dy-root@x", None), ("dy-reply@x", "dy-root@x")]
    )
    now = datetime.now(UTC)
    with SessionLocal() as s:
        for mid, ago in (("dy-root@x", 2), ("dy-reply@x", 1)):
            s.execute(
                update(Article)
                .where(Article.id == seeded[mid][0])
                .values(date=now - timedelta(hours=ago))
            )
        s.commit()
        msg_path = _msg_url(s.get(Article, seeded["dy-root@x"][0]), "alpha")
    body = client.get("/alpha/today").get_data(as_text=True)
    canonical = client.get(msg_path + "/t").get_data(as_text=True)
    assert f'<link rel="canonical" href="http://localhost{msg_path}/t">' in canonical
    assert f'<a href="{msg_path}/t">1 reply today</a>' in body
