"""The front page `/` (#994): header figures, busiest inboxes, active
subsystems, and one line per inbox.

The 30-day message total is a deduplicated count of `articles`, cached and
warmed (never computed on the request), so the tests seed a cross-post on
purpose: summing per-inbox counts gives a different number.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.models import Article, ArticleList, Inbox
from tests._dom import parse


def _inbox(s, name):
    return s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


def _post(s, msgid, inboxes, *, hours_ago):
    """One article, linked into each of `inboxes` (a cross-post when more
    than one)."""
    art = Article(
        message_id=msgid,
        subject=f"[PATCH] {msgid}",
        author="Ann Author <ann@example.com>",
        date=datetime.now(UTC) - timedelta(hours=hours_ago),
        lists=[
            ArticleList(inbox_id=_inbox(s, n).id, epoch="0.git", commit_sha="f" * 40)
            for n in inboxes
        ],
    )
    s.add(art)
    s.flush()
    return art


def _warm_total():
    from mimir.dashboard import recent_message_total
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        return recent_message_total(s, force=True)


def _warm_volumes():
    from mimir.dashboard import daily_volume
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        for inbox in s.scalars(select(Inbox)).all():
            daily_volume(s, inbox, force=True)


def _page(client):
    return parse(client.get("/").get_data(as_text=True))


def _figures(page):
    return {
        d.attrs["data-figure"]: d.text()
        for d in page.find_all("dd")
        if "data-figure" in d.attrs
    }


@pytest.fixture
def recent(seeded_db):
    """A cross-post (alpha and beta, 2 hours old), one alpha-only post
    3 days old, and one beta-only post 40 days old, which is outside the
    window. Per-inbox 30-day counts: alpha 2, beta 1. Deduplicated: 2."""
    with seeded_db() as s:
        _post(s, "cross@x", ["alpha", "beta"], hours_ago=2)
        _post(s, "alpha-only@x", ["alpha"], hours_ago=72)
        _post(s, "old@x", ["beta"], hours_ago=40 * 24)
        _inbox(s, "alpha").last_article_date = datetime.now(UTC) - timedelta(hours=2)
        _inbox(s, "beta").last_article_date = datetime.now(UTC) - timedelta(hours=30)
        s.commit()


def test_the_total_counts_a_cross_post_once(client, recent):
    # The fixture's precondition: the per-inbox sum would say 3.
    assert _warm_total() == 2
    figures = _figures(_page(client))
    assert figures["messages-30d"] == "2"


def test_inbox_and_24_hour_counts(client, recent):
    _warm_total()
    figures = _figures(_page(client))
    assert figures["inboxes"] == "2"
    assert figures["active-24h"] == "1"  # alpha 2 h ago, beta 30 h ago


def test_a_cold_cache_omits_the_total_and_never_runs_the_count(client, recent):
    """The count runs in the warm-cache tier, not on a request: the gateway
    times out a request that waits for it (the 1.36.0 shape)."""
    from sqlalchemy import event

    from mimir.extensions import engine

    statements = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    try:
        page = _page(client)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    figures = _figures(page)
    assert "messages-30d" not in figures
    assert figures["inboxes"] == "2"
    assert statements, "the request ran no statements: nothing was captured"
    assert [s for s in statements if "FROM articles WHERE date" in s] == []


def test_the_count_walks_the_date_index_under_production_statistics(client, recent):
    from mimir.dashboard import recent_message_total
    from mimir.extensions import SessionLocal
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(SessionLocal, PRODUCTION_STATS), SessionLocal() as s:
        plan = plan_of(
            s,
            lambda: recent_message_total(s, force=True),
            "FROM articles WHERE date",
        )
    assert "USING COVERING INDEX ix_articles_date (date>? AND date<?)" in plan, plan
    assert "SCAN" not in plan, plan


def test_the_window_matches_the_daily_volume_window(seeded_db):
    """The header total and the per-inbox cards describe one window: 30
    UTC days ending today. A message dated tomorrow is in neither."""
    with seeded_db() as s:
        _post(s, "future@x", ["alpha"], hours_ago=-30)
        _post(s, "today@x", ["alpha"], hours_ago=1)
        s.commit()
    assert _warm_total() == 1


def test_busiest_cards_lead_with_the_thirty_day_count(client, recent):
    _warm_total()
    _warm_volumes()
    page = _page(client)
    cards = page.find_all("a", cls="front-card")
    assert [c.attrs["href"] for c in cards] == ["/alpha/", "/beta/"]
    assert [c.find("p", cls="count").text() for c in cards] == ["2", "1"]
    # Each carries a sparkline; the totals line reads from archive_stats.
    assert all(len(c.find_all("svg", cls="front-card-spark")) == 1 for c in cards)
    assert "since 2024" in cards[0].text()


def test_only_six_inboxes_are_busiest(client, seeded_db):
    with seeded_db() as s:
        for n in range(8):
            s.add(
                Inbox(name=f"list-{n}", mirror_path="/x", upstream_url=f"https://x/{n}")
            )
        s.flush()
        for n in range(8):
            for m in range(n + 1):
                _post(s, f"l{n}m{m}@x", [f"list-{n}"], hours_ago=5)
        s.commit()
    _warm_volumes()
    cards = _page(client).find_all("a", cls="front-card")
    assert [c.attrs["href"] for c in cards] == [
        f"/list-{n}/" for n in (7, 6, 5, 4, 3, 2)
    ]


def test_the_inbox_list_has_every_inbox_once_and_no_sparklines(client, recent):
    _warm_total()
    _warm_volumes()
    page = _page(client)
    listing = page.find("ul", id="inbox-list")
    rows = listing.find_all("li")
    links = [li.find("a", cls="inbox-link").attrs["href"] for li in rows]
    assert links == ["/alpha/", "/beta/"]
    assert [li.attrs["data-active"] for li in rows] == ["2", "1"]
    assert [li.attrs["data-order"] for li in rows] == ["0", "1"]
    assert listing.find_all("svg") == []
    # Sparklines are only the busiest cards': nothing per inbox in the list.
    assert len(page.find_all("svg", cls="front-card-spark")) == 2


def test_pinned_inboxes_stay_first_and_are_marked(client, recent, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "pinned_inboxes", ["beta"])
    rows = _page(client).find("ul", id="inbox-list").find_all("li")
    assert [li.find("a", cls="inbox-link").attrs["href"] for li in rows] == [
        "/beta/",
        "/alpha/",
    ]
    marks = [li.find_all("span", cls="inbox-pinned") for li in rows]
    assert [len(m) for m in marks] == [1, 0]
    assert marks[0][0].text() == "pinned"
    # No pin, no mark.
    monkeypatch.setattr(settings, "pinned_inboxes", [])
    rows = _page(client).find("ul", id="inbox-list").find_all("li")
    assert all(not li.find_all("span", cls="inbox-pinned") for li in rows)


def test_each_row_shows_a_short_total_and_when_it_last_moved(client, recent):
    rows = _page(client).find("ul", id="inbox-list").find_all("li")
    alpha = rows[0].find("span", cls="inbox-meta").text()
    beta = rows[1].find("span", cls="inbox-meta").text()
    assert alpha.endswith("2h ago")
    assert beta.endswith("1d ago")  # 30 hours
    assert alpha.split(" · ")[0] == "5"  # art1, art3, art4 plus the two posts above


def test_the_lookup_form_is_on_the_page_once(client, seeded_db):
    page = _page(client)
    forms = page.find_all("form", cls="message-id-form")
    assert len(forms) == 1
    assert forms[0].attrs["action"] == "/m/"
    assert forms[0].attrs["method"] == "get"
    assert forms[0].find("input").attrs["name"] == "id"
    assert page.find_all(id="message-id-input").__len__() == 1


def test_controls_ship_hidden_and_the_script_is_versioned(client, seeded_db):
    from urllib.parse import parse_qs, urlsplit

    from mimir import __version__

    page = _page(client)
    controls = page.find("div", cls="front-controls")
    assert "hidden" in controls.attrs and "data-filter-controls" in controls.attrs
    assert controls.find("input").attrs["data-filter-input"] == "inbox-list"
    assert [b.attrs["data-sort-button"] for b in controls.find_all("button")] == [
        "default",
        "active",
    ]
    scripts = [
        s.attrs["src"]
        for s in page.find_all("script")
        if s.attrs.get("src", "").split("?")[0].endswith("/list-tools.js")
    ]
    assert len(scripts) == 1
    assert parse_qs(urlsplit(scripts[0]).query) == {"v": [__version__]}


def test_the_tagline_names_no_scale_figure(client, seeded_db):
    tagline = _page(client).find("p", cls="front-tagline").text()
    assert tagline == (
        "Linux kernel mailing list archives, with cross-list deduplication, "
        "subsystem dashboards, patch-series timelines and reviewer activity."
    )


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1k"),
        (1500, "1.5k"),
        (9949, "9.9k"),
        (10_000, "10k"),
        (160_447, "160k"),
        (999_499, "999k"),
        (999_500, "1M"),
        (1_000_000, "1M"),
        (6_609_675, "6.6M"),
        (25_400_000, "25M"),
    ],
)
def test_short_count(client, n, text):
    env = client.application.jinja_env
    assert env.from_string("{{ n|short_count }}").render(n=n) == text


def test_index_uses_retained_volume_until_the_warmer_refreshes_it(
    client, recent, seeded_db, monkeypatch
):
    from sqlalchemy import event

    from mimir import cache
    from mimir.dashboard import DAILY_VOLUME_CACHE_TTL_SEC, archive_stats, daily_volume
    from mimir.extensions import engine

    now = cache._now()
    monkeypatch.setattr(cache, "_now", lambda: now)
    warm_window = 1200
    with seeded_db() as s, cache.warm_window(warm_window):
        alpha = _inbox(s, "alpha")
        for inbox in (alpha, _inbox(s, "beta")):
            archive_stats(s, inbox, force=True)
        assert sum(n for _, n in daily_volume(s, alpha, force=True).days) == 2

    with seeded_db() as s:
        _post(s, "new@x", ["alpha"], hours_ago=1)
        s.commit()

    with seeded_db() as s, cache.warm_window(warm_window):
        early = daily_volume(s, _inbox(s, "alpha"))
    assert sum(n for _, n in early.days) == 2

    now += DAILY_VOLUME_CACHE_TTL_SEC + warm_window + 1
    from mimir.broker import _context

    cache.purge_expired_via_writer(_context.get_active_writer()).result(timeout=10)

    statements = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with monkeypatch.context() as scoped:

        def unexpected_write(*args, **kwargs):
            pytest.fail("the index attempted a cache write")

        scoped.setattr(cache, "set", unexpected_write)
        event.listen(engine, "before_cursor_execute", grab)
        try:
            page = _page(client)
        finally:
            event.remove(engine, "before_cursor_execute", grab)

    assert statements
    assert not any("SELECT date(a.date) AS day" in sql for sql in statements)
    cards = page.find_all("a", cls="front-card")
    assert [card.attrs["href"] for card in cards] == ["/alpha/"]
    assert cards[0].find("p", cls="count").text() == "2"
    rows = page.find("ul", id="inbox-list").find_all("li")
    assert "data-active" not in rows[1].attrs

    with seeded_db() as s, cache.warm_window(warm_window):
        daily_volume(s, _inbox(s, "alpha"))

    cards = _page(client).find_all("a", cls="front-card")
    assert cards[0].find("p", cls="count").text() == "3"
