"""Listing presentation contracts; real widths are checked by the browser test."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader

from tests._dom import parse


@pytest.fixture
def listing_page(client):
    app = client.application
    env = app.jinja_env.overlay(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                app.jinja_loader,
            ]
        )
    )
    when = datetime(2026, 11, 3, 21, 2, 3, tzinfo=UTC)
    row = SimpleNamespace(
        id=12,
        article_id=12,
        subject="Example patch",
        author="Sender <private@example.net>",
        date=when,
        last_activity=when,
        inbox_name="alpha",
        role="Reported-and-tested-by",
        reply_count=123456,
    )

    def render(template, **extra):
        context = dict(
            inbox_name="alpha",
            sub="Sender",
            address="reviewer@kernel.org",
            results=[row],
            entries=[row],
            threads=[row],
            role_counts={row.role: 1},
            total=1,
            truncated=False,
            lifecycle_status_by_id={},
        )
        context.update(extra)
        with app.test_request_context("/"):
            return parse(env.get_template(template).render(**context))

    return render


@pytest.mark.parametrize("template", ["author.html", "reviewer.html"])
def test_multiday_rows_show_a_short_date_and_keep_the_full_timestamp(
    listing_page, template
):
    page = listing_page(template)
    when = page.find("tr", cls="listing-row").find("time")
    assert when.text() == "2026-11-03"
    assert when.attrs["datetime"] == "2026-11-03T21:02:03+00:00"
    assert when.attrs["title"] == "2026-11-03 21:02:03 +0000"


def test_author_listing_has_two_columns_not_an_empty_details_column(listing_page):
    table = listing_page("author.html").find("table", cls="listing-table")
    headers = table.find("thead").find_all("th")
    cells = table.find("tr", cls="listing-row").find_all("td")
    assert [h.text() for h in headers] == ["Last", "Message"]
    assert len(cells) == 2
    assert "listing-main" in headers[-1].classes
    assert "listing-main" in cells[-1].classes


def test_count_and_day_headers_have_distinct_column_semantics(listing_page):
    table = listing_page("_listing_threads.html", group_days=True).find("table")
    count = table.find("thead").find("th", cls="listing-count")
    assert count.text() == "Replies"
    day = table.find("tr", cls="listing-day").find("th")
    assert day.text() == "Tuesday 3 November"
    assert day.attrs == {"colspan": "3", "scope": "rowgroup"}
    row = table.find("tr", cls="listing-row")
    assert row.find("time").text() == "21:02"
    assert row.find("td", cls="listing-count").text() == "123456"


@pytest.mark.parametrize("count,label", [(1, "1 match"), (2, "2 matches")])
def test_search_row_uses_a_short_unambiguous_count(client, count, label):
    from flask import render_template

    with client.application.test_request_context("/"):
        page = parse(
            render_template(
                "_listing_row.html",
                row_id=1,
                row_time=None,
                row_url="/alpha/2026/11/1/t",
                row_subject="Search subject",
                row_matches=count,
            )
        )
    assert page.find("td", cls="listing-count").text() == label


@pytest.mark.parametrize(
    "author,expected",
    [
        ("public@kernel.org", "public@kernel.org"),
        ("Person <public@kernel.org>", "Person"),
        ("Person <private@example.net>", "Person"),
        ("private@example.net", "unknown sender"),
        ('"private@example.net via list" <private@example.net>', "unknown sender"),
        ('"private@example.net" <public@kernel.org>', "public@kernel.org"),
        (None, "unknown sender"),
    ],
)
def test_listing_sender_uses_the_existing_display_allowlist(client, author, expected):
    from mimir.web.filters import _listing_sender_filter

    assert _listing_sender_filter(author) == expected


def test_listing_sender_can_fall_back_to_a_maintainers_address(client, session):
    from mimir import maintainer_allowlist
    from mimir.models import Subsystem, SubsystemMaintainer
    from mimir.web.filters import _listing_name_filter, _listing_sender_filter

    address = "contributor@example.net"
    assert _listing_sender_filter(address) == "unknown sender"
    sub = Subsystem(name="LISTING TEST")
    session.add(sub)
    session.flush()
    session.add(
        SubsystemMaintainer(
            subsystem_id=sub.id, role="M", name="Contributor", address=address
        )
    )
    session.commit()
    maintainer_allowlist.invalidate()
    assert _listing_sender_filter(address) == address
    # Parsed trailer names still have no address fallback.
    assert _listing_name_filter(address) == "unknown sender"
