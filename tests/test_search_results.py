"""Bounded search grouping and literal, inert subject highlighting."""

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from time import monotonic

import pytest
from sqlalchemy import event, select

from mimir.dashboard import ArticleSummary
from mimir.models import Article, ArticleList, Inbox
from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats


def _matches(session, specs):
    inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    matches = []
    for n, (subject, root, key, head, version, position) in enumerate(specs):
        article = Article(
            message_id=f"group-{n}@test",
            subject=subject,
            author=f"Sender {n % 2} <private{n}@example.net>",
            date=datetime(2024, 3, 1, tzinfo=UTC) - timedelta(hours=n),
            patch_series_key=key,
            patch_series_head_id=head,
            patch_series_version=version,
            patch_series_position=position,
        )
        session.add(article)
        session.flush()
        session.add(
            ArticleList(
                article_id=article.id,
                inbox_id=inbox.id,
                epoch="0.git",
                commit_sha="a" * 40,
                thread_root_id=matches[root].id if root is not None else None,
            )
        )
        matches.append(
            ArticleSummary(article.id, subject, article.author, article.date)
        )
    session.commit()
    return inbox, matches


def test_groups_follow_roots_and_newest_match_not_subject(seeded_db):
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("new title", None, None, None, None, None),
                ("old title", 0, None, None, None, None),
                ("new title", None, None, None, None, None),
            ],
        )
        s.get(ArticleList, (matches[0].id, inbox.id)).thread_root_id = matches[0].id
        s.get(ArticleList, (matches[2].id, inbox.id)).thread_root_id = matches[2].id
        s.commit()
        groups = group_search_results(s, inbox, matches)
        assert [g.representative.id for g in groups] == [matches[0].id, matches[2].id]
        assert [g.count for g in groups] == [2, 1]
        assert groups[0].root_id == matches[0].id
        assert groups[0].authors == [m.author for m in matches[:2]]
        # The root need not itself match.
        only_reply = group_search_results(s, inbox, matches[1:2])[0]
        assert only_reply.root_id == matches[0].id
        assert only_reply.representative == matches[1]


def test_missing_membership_and_unusable_roots_are_not_guessed(seeded_db, caplog):
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("null", None, None, None, None, None),
                ("bad root", 0, None, None, None, None),
                ("stale", 0, None, None, None, None),
            ],
        )
        s.delete(s.get(ArticleList, (matches[2].id, inbox.id)))
        s.commit()
        groups = group_search_results(s, inbox, matches)
        assert len(groups) == 2
        assert all(g.root_id is None and g.count == 1 for g in groups)
        assert all(str(m.id) in caplog.text for m in matches)


def test_positions_are_representatives_exact_posting_only(seeded_db):
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("[PATCH v2 2/9] newest", None, "k", None, "v2", 2),
                ("[PATCH v2 4/9] same", 0, "k", 0, "v2", 4),
                ("[PATCH v2 4/9] resend", 0, "k", None, "v2", 4),
                ("[PATCH v1 3/9] old", 0, "k", None, "v1", 3),
                ("[PATCH v2 5/9] split", 0, "k", 99, "v2", 5),
                ("Re: [PATCH v2 6/9] reply", 0, "k", None, "v2", 6),
                ("[PATCH v2 0/9] cover", 0, "k", None, "v2", 0),
            ],
        )
        s.get(ArticleList, (matches[0].id, inbox.id)).thread_root_id = matches[0].id
        s.commit()
        group = group_search_results(s, inbox, matches)[0]
        assert group.positions == [4]
        assert group.version == "v2"
        assert group_search_results(s, inbox, matches[5:])[0].positions == []


def test_metadata_is_one_indexed_nonrecursive_batch(seeded_db):
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        inbox, matches = _matches(s, [("term", None, None, None, None, None)] * 100)
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(s.get_bind(), "before_cursor_execute", capture)
        try:
            group_search_results(s, inbox, matches)
        finally:
            event.remove(s.get_bind(), "before_cursor_execute", capture)
        assert len(statements) == 1
        assert "RECURSIVE" not in statements[0].upper()
        plan = plan_of(
            s, lambda: group_search_results(s, inbox, matches), "search_metadata"
        )
        assert "SCAN" not in plan
        assert "sqlite_autoindex_article_lists_1" in plan
        assert "INTEGER PRIMARY KEY" in plan


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize(
    "subject,query,highlight",
    [
        ('<script x="bad">Hello & hi</script>', "hello", "Hello"),
        ("a &amp; b", "&amp;", "&amp;"),
        ("100%_ [a.*]", "%_", "%_"),
        ("100%_ [a.*]", "[a.*]", "[a.*]"),
        ("İ X HELLO hello", "hello", "HELLO"),
        ("K kelvin", "k", "k"),
        ("subject", "sender", None),
        ("<b>blank</b>", "", None),
    ],
)
def test_literal_highlight_escapes_every_piece(subject, query, highlight):
    from mimir.web.filters import _highlight_match_filter

    rendered = str(_highlight_match_filter(subject, query))
    parser = Tags()
    parser.feed(rendered)
    assert "".join(parser.text) == subject
    assert parser.tags == ([("mark", [])] if highlight is not None else [])
    if highlight is not None:
        assert "<mark>" in rendered
        marked = rendered.split("<mark>")[1].split("</mark>")[0]
        inner = Tags()
        inner.feed(marked)
        assert "".join(inner.text) == highlight


def test_highlight_hostile_subject_is_bounded():
    from mimir.web.filters import _highlight_match_filter

    start = monotonic()
    _highlight_match_filter("[" * 300_000 + "İ HELLO", "[" * 79 + "x")
    assert monotonic() - start < 3


def test_crosspost_has_independent_inbox_root(seeded_db):
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        alpha, matches = _matches(
            s,
            [
                ("root", None, None, None, None, None),
                ("reply", 0, None, None, None, None),
            ],
        )
        s.get(ArticleList, (matches[0].id, alpha.id)).thread_root_id = matches[0].id
        beta = s.scalar(select(Inbox).where(Inbox.name == "beta"))
        s.add(
            ArticleList(
                article_id=matches[1].id,
                inbox_id=beta.id,
                epoch="0.git",
                commit_sha="b" * 40,
                thread_root_id=matches[1].id,
            )
        )
        s.commit()
        assert group_search_results(s, alpha, matches[1:])[0].root_id == matches[0].id
        assert group_search_results(s, beta, matches[1:])[0].root_id == matches[1].id
        s.get(ArticleList, (matches[1].id, beta.id)).thread_root_id = matches[0].id
        s.commit()
        assert group_search_results(s, beta, matches[1:])[0].root_id is None


def test_warm_message_cache_still_sees_membership_repairs(client, seeded_db):
    from mimir.dashboard import search_articles
    from mimir.search_results import group_search_results

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("repair-term newest", None, None, None, None, None),
                ("repair-term older", None, None, None, None, None),
            ],
        )
        for m in matches:
            s.get(ArticleList, (m.id, inbox.id)).thread_root_id = m.id
        s.commit()
        cached = search_articles(s, inbox, "repair-term")
        assert len(cached) == 2
        assert len(group_search_results(s, inbox, cached)) == 2
    first = client.get("/alpha/search?q=repair-term")
    assert first.get_data(as_text=True).count('class="listing-row"') == 2
    with seeded_db() as s:
        s.get(ArticleList, (matches[0].id, inbox.id)).thread_root_id = matches[1].id
        s.commit()
        assert search_articles(s, inbox, "repair-term") == cached
    second = client.get("/alpha/search?q=repair-term")
    assert second.get_data(as_text=True).count('class="listing-row"') == 1
    assert "2 matching messages" in second.get_data(as_text=True)
    assert first.headers["Cache-Control"] == second.headers["Cache-Control"]
    assert "ETag" not in second.headers


def test_route_cap_precedes_grouping_and_badges_use_representative(
    client, seeded_db, monkeypatch
):
    from mimir.web.routes import search
    from mimir.web.urls import thread_page_url

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                (f"cap-term {n}", 0 if n else None, None, None, None, None)
                for n in range(101)
            ],
        )
        s.get(ArticleList, (matches[0].id, inbox.id)).thread_root_id = matches[0].id
        s.commit()
    calls = []
    original = search.lifecycle_status_for_articles

    def lifecycle(session, ids, inbox_id):
        calls.append(ids)
        return original(session, ids, inbox_id)

    monkeypatch.setattr(search, "lifecycle_status_for_articles", lifecycle)
    body = client.get("/alpha/search?q=cap-term").get_data(as_text=True)
    assert body.count('class="listing-row"') == 1
    assert "The 100 newest matching messages" in body
    assert "100 matching messages" in body
    assert "refine the query" in body
    assert calls == [[matches[0].id]]
    assert (
        body.count(
            f'href="{thread_page_url(matches[0].id, matches[0].date, inbox.name)}"'
        )
        == 2
    )
    visible = Tags()
    visible.feed(body)
    assert "cap-term 0" in "".join(visible.text)
    assert "cap-term 100" not in "".join(visible.text)


def test_search_row_privacy_destination_positions_and_badge(client, seeded_db):
    from mimir.models import MainlineCommit
    from mimir.web.urls import thread_page_url

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("[PATCH v2 2/9] find-term newest", None, "k", None, "v2", 2),
                ("[PATCH v2 4/9] find-term older", 0, "k", None, "v2", 4),
                ("[PATCH v2 5/9] find-term oldest", 0, "k", None, "v2", 5),
            ],
        )
        # The destination is an older nonmatching root, while badge identity
        # and the displayed subject belong to the newest matching patch.
        root = s.scalar(select(Article).where(Article.message_id == "art1@example.com"))
        for match in matches:
            s.get(ArticleList, (match.id, inbox.id)).thread_root_id = root.id
        s.get(Article, matches[2].id).author = matches[0].author
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id="group-0@test",
                tree_name="linus",
                committed_at=datetime(2024, 3, 2, tzinfo=UTC),
            )
        )
        s.commit()
        target = thread_page_url(root.id, root.date, inbox.name)
    body = client.get("/alpha/search?q=find-term").get_data(as_text=True)
    assert body.count('class="listing-row"') == 1
    assert body.count(f'href="{target}"') == 2
    assert "badge-lifecycle-landed" in body
    assert "badge-activity-" not in body
    assert "Also matching v2 patches 4, 5" in body
    assert "private" not in body
    visible = Tags()
    visible.feed(body)
    assert "Sender 0, Sender 1" in "".join(visible.text)
    assert sum(tag == "mark" for tag, attrs in visible.tags) == 1
    assert 'for="search-query"' in body
    assert 'id="search-query"' in body
    assert 'rel="canonical" href="http://localhost/alpha/search"' in body
    assert "?q=find-term" not in body.split('rel="canonical"')[1].split(">")[0]


def test_fallback_singletons_use_current_message_url(client, seeded_db):
    from mimir.web.urls import _msg_path

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("fallback-term one", None, None, None, None, None),
                ("fallback-term two", 0, None, None, None, None),
            ],
        )
    body = client.get("/alpha/search?q=fallback-term").get_data(as_text=True)
    assert body.count('class="listing-row"') == 2
    assert body.count("1 matching message</a>") == 2
    for match in matches:
        assert body.count(f'href="{_msg_path(match.id, match.date, inbox.name)}"') == 2
    assert '/t"' not in body


@pytest.mark.parametrize("query", ["", "x", "not-found-term"])
def test_no_matches_skip_metadata_and_lifecycle_reads(client, monkeypatch, query):
    from mimir.web.routes import search

    # The grouping helper and lifecycle batch accept the empty result without SQL.
    calls = []
    original = search.group_search_results

    def group(session, inbox, matches):
        calls.append(matches)
        return original(session, inbox, matches)

    monkeypatch.setattr(search, "group_search_results", group)
    response = client.get("/alpha/search", query_string={"q": query})
    assert response.status_code == 200
    assert calls == [[]]
    assert 'class="listing-row"' not in response.get_data(as_text=True)


def test_stale_cached_membership_is_omitted_from_displayed_match_count(
    client, seeded_db
):
    from mimir.dashboard import search_articles

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("stale-term one", None, None, None, None, None),
                ("stale-term two", None, None, None, None, None),
            ],
        )
        assert len(search_articles(s, inbox, "stale-term")) == 2
        s.delete(s.get(ArticleList, (matches[0].id, inbox.id)))
        s.commit()
    body = client.get("/alpha/search?q=stale-term").get_data(as_text=True)
    assert body.count('class="listing-row"') == 1
    assert "The 1 newest matching message," in body
    assert "The 2 newest matching messages," not in body


@pytest.mark.parametrize("dated_match", [True, False])
def test_undated_root_never_emits_unaddressable_destination(
    client, seeded_db, caplog, dated_match
):
    from mimir.search_results import group_search_results
    from mimir.web.urls import _msg_path, thread_page_url

    with seeded_db() as s:
        inbox, matches = _matches(
            s,
            [
                ("root", None, None, None, None, None),
                ("date-term reply", 0, None, None, None, None),
            ],
        )
        root = s.get(Article, matches[0].id)
        root.date = None
        s.get(ArticleList, (root.id, inbox.id)).thread_root_id = root.id
        if not dated_match:
            s.get(Article, matches[1].id).date = None
        s.commit()
        assert root.date is None
        broken_url = thread_page_url(root.id, None, inbox.name)
        groups = group_search_results(s, inbox, matches[1:])
    assert client.get(broken_url).status_code == 404
    body = client.get("/alpha/search?q=date-term").get_data(as_text=True)
    assert f'href="{broken_url}"' not in body
    if dated_match:
        assert len(groups) == 1 and groups[0].root_id is None
        target = _msg_path(matches[1].id, matches[1].date, inbox.name)
        assert body.count(f'href="{target}"') == 2
    else:
        assert groups == []
        assert 'class="listing-row"' not in body
    assert str(matches[1].id) in caplog.text
