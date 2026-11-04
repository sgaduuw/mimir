"""Presentation contracts shared by archive pages and the message overview (#1081)."""

from datetime import UTC, datetime

import pytest
from flask import render_template
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.ingest import ingest_epoch
from mimir.models import Article, Inbox
from tests._dom import parse
from tests.test_routes._helpers import _build_pubinbox_epoch


@pytest.mark.parametrize("template", ["daily.html", "since.html"])
@pytest.mark.parametrize("selected", ["all", "patches"])
def test_window_count_prose_has_thousands_separators(client, template, selected):
    with client.application.test_request_context("/alpha/today"):
        body = render_template(
            template,
            inbox_name="alpha",
            current_inbox="alpha",
            heading="Today",
            day=datetime(2026, 11, 4, tzinfo=UTC),
            since=datetime(2026, 11, 1, tzinfo=UTC),
            total_messages=3567,
            matching_threads=1249,
            selected_filter=selected,
            threads=[],
            first_thread=1,
            last_thread=100,
            filter_links=[],
            capped=False,
            truncated=False,
        )
    text = parse(body).text()
    assert "3,567 messages" in text
    assert ("1,249 threads" if selected == "all" else "1,249 matching threads") in text


def test_thread_load_more_keeps_large_counts_and_native_htmx_links(client):
    with client.application.test_request_context("/alpha/2026/11/1/t"):
        body = render_template(
            "_thread_messages.html",
            messages=[],
            next_page_url="/alpha/2026/11/1/t?page=2",
            next_kind_urls={},
            remaining=1234,
            page_size=100,
        )
    link = parse(body).find("a")
    assert link.text() == "Next 100 of 1,234 remaining"
    assert link.attrs["href"] == link.attrs["hx-get"]
    assert link.attrs["hx-swap"] == "outerHTML"


def test_midword_highlight_preserves_literal_text_and_escaping(client):
    with client.application.test_request_context("/"):
        body = render_template(
            "search.html",
            inbox_name="alpha",
            current_inbox="alpha",
            query="folio",
            min_len=2,
            max_len=200,
            matching_count=1,
            results=[],
        )
        highlighted = client.application.jinja_env.filters["highlight_match"](
            "memfd_pin_folios()<x>", "folio"
        )
    assert str(highlighted) == "memfd_pin_<mark>folio</mark>s()&lt;x&gt;"
    assert parse(str(highlighted)).text() == "memfd_pin_folios()<x>"
    assert "search-field" in body


def _overview_seed(tmp_path):
    messages = [
        (
            "root@polish",
            "[PATCH] Original topic",
            None,
            b"root",
            "Root Name <root@private.example>",
        ),
        (
            "re@polish",
            "Re: [PATCH] Original topic",
            "root@polish",
            b"reply",
            "Reply Name <reply@private.example>",
        ),
        (
            "aw@polish",
            "AW: Re: [PATCH] Original topic",
            "re@polish",
            b"reply",
            "address-only@kernel.org",
        ),
        (
            "sv@polish",
            "SV: [PATCH] Original topic",
            "root@polish",
            b"reply",
            "hidden-only@private.example",
        ),
        (
            "changed@polish",
            "A different topic",
            "root@polish",
            b"changed",
            "Changed Name <changed@private.example>",
        ),
    ]
    epoch = tmp_path / "0.git"
    _build_pubinbox_epoch(epoch, messages)
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        inbox.mirror_path = str(tmp_path)
        session.commit()
        ingest_epoch(session, inbox, "0.git", epoch, workers=1)
        session.commit()
        rows = session.scalars(
            select(Article).where(Article.message_id.in_([m[0] for m in messages]))
        ).all()
        assert len(rows) == 5
        return {
            a.message_id: (a.id, f"/alpha/{a.date.year}/{a.date.month:02d}/{a.id}")
            for a in rows
        }


def test_message_overview_only_names_changed_subject_and_safe_sender(client, tmp_path):
    seeded = _overview_seed(tmp_path)
    # Viewing the changed reply must still compare every row with the ROOT topic.
    response = client.get(seeded["changed@polish"][1])
    assert response.status_code == 200
    page = parse(response.get_data(as_text=True))
    tree = page.find("ul", cls="thread-list")
    rows = {
        int(n.attrs["data-article-id"]): n
        for n in tree.find_all("li")
        if "data-article-id" in n.attrs
    }
    assert len(rows) == 5
    expected = {
        "root@polish": "Root Name",
        "re@polish": "Reply Name",
        "aw@polish": "address-only@kernel.org",
        "sv@polish": "unknown sender",
        "changed@polish": "Changed Name",
    }
    for mid, sender in expected.items():
        aid, url = seeded[mid]
        row = rows[aid]
        assert sender in row.text()
        assert "private.example" not in row.text()
        assert ("A different topic" in row.text()) == (mid == "changed@polish")
        assert "Original topic" not in row.text()
        link = row.find_all("a")[0]
        assert link.attrs["href"] == url and link.attrs["hx-get"] == url
        assert link.attrs["hx-target"] == "#msg"
        assert link.attrs["hx-swap"] == "outerHTML show:#msg:top"
        assert link.attrs["hx-push-url"] == "true"
        assert ("is-active" in row.classes) == (mid == "changed@polish")
    # The existing swap replaces the article only, retaining the already-rendered tree.
    fragment = parse(
        client.get(seeded["re@polish"][1], headers={"HX-Request": "true"}).get_data(
            as_text=True
        )
    )
    assert len(fragment.find_all("article", id="msg")) == 1
    assert fragment.find_all("ul", cls="thread-list") == []
