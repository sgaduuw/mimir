"""The message page's bounded window onto a long thread (#798).

A thread longer than `thread_view_render_cap` renders the path from the
root, the nearest siblings, the first replies and gap rows that link to
the thread view, instead of every message. Shorter threads render whole,
as before.
"""

import re

from sqlalchemy import delete, event, select

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, ArticleList, Inbox
from mimir.threading import thread_page_of
from mimir.web.urls import thread_page_url
from tests.test_routes._helpers import (
    THREAD_SHAPES,
    build_thread,
    seed_thread_shape,
)

# The window's shape, restated rather than imported: these numbers are
# the contract the page makes, so a change to them should fail here.
WINDOW_PATH_MAX = 20
WINDOW_SIBLINGS = 5
WINDOW_CHILDREN = 10


def _tree(html: str) -> list[tuple]:
    """The thread list as rows: ("node", depth, article_id, hidden replies),
    ("gap", depth, count, href, noun), ("more", None, total, href)."""
    box = re.search(r'<ul class="thread-list">(.*?)</ul>', html, re.DOTALL)
    assert box, "no thread list rendered"
    rows = []
    for m in re.finditer(r"<li\b([^>]*)>(.*?)</li>", box.group(1), re.DOTALL):
        attrs, inner = m.groups()
        depth = re.search(r'data-depth="(\d+)"', attrs)
        depth = int(depth.group(1)) if depth else None
        href = re.search(r'href="([^"]*)"', inner)
        href = href.group(1) if href else None
        if "thread-gap" in attrs:
            m = re.search(r"(\d+) more (messages?|repl(?:y|ies))\b", inner)
            rows.append(("gap", depth, int(m.group(1)), href, m.group(2)))
        elif "thread-all" in attrs:
            total = int(re.search(r"View all (\d+) messages", inner).group(1))
            rows.append(("more", None, total, href))
        elif "data-article-id" in attrs:
            art_id = int(re.search(r'data-article-id="(\d+)"', attrs).group(1))
            replies = re.search(r"\+(\d+) repl(?:y|ies)\b", inner)
            rows.append(
                ("node", depth, art_id, int(replies.group(1)) if replies else 0)
            )
        else:
            rows.append(("other", depth, None, href))
    return rows


def _node_ids(rows):
    return [r[2] for r in rows if r[0] == "node"]


def _gaps(rows):
    return [r for r in rows if r[0] == "gap"]


def _expected_more_url(root_id, article_id, inbox_name="alpha"):
    """The thread-view page holding the article, from the same helpers the
    thread view builds its own URLs with."""
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        root = s.get(Article, root_id)
        page = thread_page_of(
            s,
            inbox.id,
            root_id,
            s.get(Article, article_id),
            settings.thread_view_render_cap,
        )
        return thread_page_url(root_id, root.date, inbox_name, page)


def test_thread_at_the_cap_renders_whole_and_one_more_windows(
    client, tmp_path, monkeypatch
):
    """The threshold is the thread view's page size: a thread that fits
    on one thread page is shown whole, one message more is windowed."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 12)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    whole = build_thread(tmp_path / "a", "alpha", shape="fan_out", size=12)
    rows = _tree(client.get(whole["m0"][1]).get_data(as_text=True))
    assert _node_ids(rows) == [whole[f"m{i}"][0] for i in range(12)]
    assert not _gaps(rows)
    assert not [r for r in rows if r[0] == "more"]

    windowed = build_thread(tmp_path / "b", "beta", shape="fan_out", size=13)
    rows = _tree(client.get(windowed["m0"][1]).get_data(as_text=True))
    # Root plus its first 10 replies, then one gap for the other 2.
    assert len(_node_ids(rows)) == 1 + WINDOW_CHILDREN
    assert [g[2] for g in _gaps(rows)] == [12 - WINDOW_CHILDREN]
    assert [r[2] for r in rows if r[0] == "more"] == [13]


def test_window_shows_nearest_siblings_with_exact_gap_counts(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="fan_out", size=40)
    ids = [seeded[f"m{i}"][0] for i in range(40)]
    art_id, url = seeded["m20"]

    rows = _tree(client.get(url).get_data(as_text=True))

    expected = (
        [("node", 0, ids[0], 0), ("gap", 1, 19 - WINDOW_SIBLINGS)]
        + [("node", 1, i, 0) for i in ids[20 - WINDOW_SIBLINGS : 20]]
        + [("node", 1, art_id, 0)]
        + [("node", 1, i, 0) for i in ids[21 : 21 + WINDOW_SIBLINGS]]
        + [("gap", 1, 19 - WINDOW_SIBLINGS)]
    )
    # Every reply to the root is shown or counted by a gap, so the root
    # carries no "+replies" marker.
    assert [r[:4] if r[0] == "node" else r[:3] for r in rows[:-1]] == expected
    assert rows[-1][:3] == ("more", None, 40)
    # A sibling gap counts replies to the parent, not their subtrees.
    assert [g[4] for g in _gaps(rows)] == ["replies", "replies"]


def test_window_counts_only_this_inbox(client, tmp_path, monkeypatch):
    """Threading is inbox-scoped. Replies that exist only in the other
    inbox of a cross-post are neither shown nor counted here."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(
        tmp_path, "alpha", shape="fan_out", size=20, cross_post_to="beta"
    )
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            delete(ArticleList).where(
                ArticleList.inbox_id == alpha.id,
                ArticleList.article_id.in_([seeded[f"m{i}"][0] for i in range(15, 20)]),
            )
        )
        s.commit()

    rows = _tree(client.get(seeded["m0"][1]).get_data(as_text=True))

    assert _node_ids(rows) == [seeded[f"m{i}"][0] for i in range(11)]
    assert [g[2] for g in _gaps(rows)] == [4]
    assert rows[0][3] == 0
    assert rows[-1][:3] == ("more", None, 15)


def test_window_marks_shown_messages_that_have_hidden_replies(
    client, tmp_path, monkeypatch
):
    """`bushy` gives every message two replies. A sibling shown without
    its replies says how many there are and links to its own page."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="bushy", size=60)
    by_id = {aid: (mid, url) for mid, (aid, url) in seeded.items()}
    art_id, url = seeded["m5"]  # parent m2, sibling m6, replies m11 and m12

    html = client.get(url).get_data(as_text=True)
    rows = _tree(html)
    nodes = {r[2]: r for r in rows if r[0] == "node"}

    assert [by_id[i][0] for i in _node_ids(rows)] == [
        "m0",
        "m2",
        "m5",
        "m11",
        "m12",
        "m6",
    ]
    # m0's other reply (m1) is not shown; m6's two replies are not shown.
    assert nodes[seeded["m0"][0]][3] == 1
    assert nodes[seeded["m6"][0]][3] == 2
    assert nodes[seeded["m11"][0]][3] == 2
    assert nodes[seeded["m2"][0]][3] == 0
    assert nodes[art_id][3] == 0
    m6_url = seeded["m6"][1]
    assert re.search(
        rf'<a class="thread-replies" href="{re.escape(m6_url)}">\+2 replies</a>', html
    )


def test_window_path_keeps_the_root_and_the_nearest_ancestors(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=30)
    ids = [seeded[f"m{i}"][0] for i in range(30)]

    rows = _tree(client.get(seeded["m29"][1]).get_data(as_text=True))

    keep = WINDOW_PATH_MAX - 1
    # The root's only reply is behind the path gap, so no marker.
    assert rows[0] == ("node", 0, ids[0], 0)
    assert rows[1][:3] == ("gap", 1, 29 - WINDOW_PATH_MAX)
    # A path gap counts the ancestors it stands for, exactly.
    assert rows[1][4] == "messages"
    shown = [r for r in rows[2:] if r[0] == "node"]
    assert [r[2] for r in shown] == ids[29 - keep : 30]
    # True depths, clamped by the template's 20-step ladder.
    assert [r[1] for r in shown] == [min(20, d) for d in range(29 - keep, 30)]

    # Exactly WINDOW_PATH_MAX ancestors fit without a gap.
    rows = _tree(client.get(seeded[f"m{WINDOW_PATH_MAX}"][1]).get_data(as_text=True))
    assert _node_ids(rows)[: WINDOW_PATH_MAX + 1] == ids[: WINDOW_PATH_MAX + 1]
    assert not [g for g in _gaps(rows) if g[1] == 1]


def test_window_on_a_cyclic_thread_stops_the_walk_at_the_root(
    client, tmp_path, monkeypatch
):
    """`thread_parent` comes from sender-controlled headers. The walk up
    has to stop at the root rather than circle the cycle to MAX_DEPTH,
    which rendered a path gap of about a thousand."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    edges = [("c0", "c2"), ("c1", "c0"), ("c2", "c1")] + [
        (f"r{i}", "c2") for i in range(10)
    ]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    art_id, url = seeded["r5"]
    with SessionLocal() as s:
        roots = set(
            s.execute(
                select(ArticleList.thread_root_id).where(
                    ArticleList.article_id.in_([v[0] for v in seeded.values()])
                )
            ).scalars()
        )
    assert len(roots) == 1 and None not in roots  # precondition: one rooted thread

    rows = _tree(client.get(url).get_data(as_text=True))

    # Windowed (a fallback to the full walk would hide the defect), and
    # the path is the few messages up to the root.
    assert [r[2] for r in rows if r[0] == "more"] == [13]
    assert len(rows) < 30, rows
    assert all(g[2] < 20 for g in _gaps(rows)), rows
    assert art_id in _node_ids(rows)


def test_window_falls_back_to_the_whole_thread_when_the_chain_disagrees(
    client, tmp_path, monkeypatch, caplog
):
    """The column says m7 is in the thread, but its parent chain no longer
    reaches the root. The page renders the thread whole and says why."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=8)
    art_id, url = seeded["m7"]
    with SessionLocal() as s:
        s.get(Article, art_id).thread_parent = "absent@elsewhere"
        s.commit()

    with caplog.at_level("WARNING", logger="mimir.web.routes.message"):
        rows = _tree(client.get(url).get_data(as_text=True))

    assert not [r for r in rows if r[0] in ("gap", "more")]
    hits = [
        r.getMessage()
        for r in caplog.records
        if "does not reach the materialised root" in r.getMessage()
    ]
    assert hits == [
        f"message: alpha/{art_id} rendered whole; the walk up does "
        f"not reach the materialised root {seeded['m0'][0]}"
    ]


def test_gap_and_footer_links_name_the_thread_page_holding_the_message(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="fan_out", size=40, dates="spread")
    root_id = seeded["m0"][0]
    art_id, url = seeded["m20"]

    rows = _tree(client.get(url).get_data(as_text=True))

    expected = _expected_more_url(root_id, art_id)
    assert expected.endswith("/t/5")  # precondition: not page 1
    links = [r[3] for r in rows if r[0] in ("gap", "more")]
    assert len(links) == 3
    assert set(links) == {expected}


def test_window_per_request_work_does_not_grow_with_the_thread(
    client, tmp_path, monkeypatch
):
    """Same statements and same rows for a 200 and a 2,000 message
    thread. Measured on the second request: the first one primes the
    caches every page shares (lifecycle pill, related panels, patch
    card), the way a crawler meets them. The pill's own walk on a miss
    is deliberately outside this test."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 75)
    measured = {}
    for size, inbox in ((200, "alpha"), (2000, "beta")):
        (tmp_path / inbox).mkdir()
        # Prefixed Message-IDs: `build_thread` reuses `m0`, `m1`, ... and
        # the second thread would cross-post the first one's messages.
        edges = [
            (f"{inbox}-{mid}", parent and f"{inbox}-{parent}")
            for mid, parent in THREAD_SHAPES["bushy"](size)
        ]
        seeded = seed_thread_shape(tmp_path / inbox, inbox, edges)
        # m50 has the same parent, siblings and replies at both sizes;
        # only its grandchildren differ.
        _, url = seeded[f"{inbox}-m50"]
        assert client.get(url).status_code == 200
        statements = []
        with SessionLocal() as s:
            engine = s.get_bind()

        def grab(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))

        event.listen(engine, "before_cursor_execute", grab)
        try:
            resp = client.get(url)
        finally:
            event.remove(engine, "before_cursor_execute", grab)
        assert resp.status_code == 200
        measured[size] = (statements, _tree(resp.get_data(as_text=True)))

    small, large = measured[200], measured[2000]
    assert len(small[0]) == len(large[0]), (len(small[0]), len(large[0]))
    assert len(small[1]) == len(large[1])
    assert len(large[1]) < 40
    for statement, _ in large[0]:
        # `get_thread`'s walk down from the root.
        assert "JOIN thread t ON a.thread_parent = t.message_id" not in statement

    replies = [
        (stmt, params)
        for stmt, params in large[0]
        if "articles.thread_parent = " in stmt and "LIMIT" in stmt
    ]
    assert len(replies) == 3  # earlier siblings, later siblings, children
    with SessionLocal() as s:
        raw = s.connection().connection.driver_connection
        for stmt, params in replies:
            plan = "\n".join(
                row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + stmt, params)
            )
            assert "ix_articles_thread_parent" in plan, plan


def test_etag_changes_when_a_thread_loses_a_message_but_keeps_its_date(
    client, tmp_path, monkeypatch
):
    """The page shows the thread's message count, so the count belongs in
    the validator. A purge of an older message leaves the newest date in
    place, which was the only thread input the ETag carried."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    for size, inbox in ((4, "alpha"), (12, "beta")):
        (tmp_path / inbox).mkdir()
        seeded = build_thread(tmp_path / inbox, inbox, shape="fan_out", size=size)
        _, url = seeded["m0"]
        first = client.get(url)
        assert first.status_code == 200
        assert f"Thread ({size} messages)" in first.get_data(as_text=True)

        with SessionLocal() as s:
            ix = s.execute(select(Inbox).where(Inbox.name == inbox)).scalar_one()
            s.execute(
                delete(ArticleList).where(
                    ArticleList.inbox_id == ix.id,
                    ArticleList.article_id == seeded["m2"][0],
                )
            )
            s.commit()

        second = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        assert second.status_code == 200, f"stale count served as 304 ({size})"
        assert f"Thread ({size - 1} messages)" in second.get_data(as_text=True)


def test_dateless_reply_is_not_taken_for_the_root(client, tmp_path):
    """`get_thread` sorts a dateless reply and its subtree before the
    root, so the first node is not the root. The fold pin is keyed by
    the root id, so the page must name the real one."""
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=7, dates="dateless")
    root_id = seeded["m0"][0]
    html = client.get(seeded["m1"][1]).get_data(as_text=True)
    rows = _tree(html)
    assert rows[0][2] != root_id  # precondition: the walk does not start at the root

    assert re.search(r'<html[^>]* data-thread-root-id="(\d+)"', html).group(1) == str(
        root_id
    )
    assert re.search(
        r'<section class="thread-context"\s+data-thread-root-id="(\d+)"', html
    ).group(1) == str(root_id)


def test_cross_post_gate_does_not_walk_the_canonical_inbox_thread(
    client, tmp_path, monkeypatch
):
    """The canonical inbox's copy of the thread is consulted through the
    column only. The canonical itself is unchanged: the thread page that
    holds this message in the canonical inbox."""
    import mimir.web.routes.message as message_route

    seeded = build_thread(
        tmp_path, "alpha", shape="chain", size=6, cross_post_to="beta"
    )
    root_id = seeded["m0"][0]
    art_id, url = seeded["m3"]
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        s.get(Article, art_id).canonical_inbox_id = beta.id
        s.commit()

    walked = []
    real = message_route.get_thread

    def spy(session, inbox, root_msgid):
        walked.append(inbox.name)
        return real(session, inbox, root_msgid)

    monkeypatch.setattr(message_route, "get_thread", spy)
    resp = client.get(url)
    assert resp.status_code == 200
    canonical = re.search(
        r'<link rel="canonical" href="([^"]+)"', resp.get_data(as_text=True)
    )
    expected = _expected_more_url(root_id, art_id, "beta")
    assert expected.startswith("/beta/")  # precondition: another inbox
    assert canonical.group(1).endswith(expected)
    assert walked == ["alpha"]


def test_cross_post_canonical_needs_the_message_own_row_to_carry_the_root(
    client, tmp_path
):
    """The canonical inbox's row for this message names a root that is not
    in that inbox (what a `reindex --from-scratch` can leave). The thread
    there still passes `thread_is_materialised`, which only looks for
    NULL rows, but `thread_page_of` ranks over the column and would not
    count this message, so the canonical must not name a thread page."""
    seeded = build_thread(
        tmp_path, "alpha", shape="chain", size=6, cross_post_to="beta"
    )
    art_id, url = seeded["m3"]
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        stray = Article(
            message_id="stray@x", subject="stray", subject_normalized="stray"
        )
        s.add(stray)
        s.flush()
        s.get(Article, art_id).canonical_inbox_id = beta.id
        s.get(ArticleList, (art_id, beta.id)).thread_root_id = stray.id
        s.commit()

    resp = client.get(url)

    assert resp.status_code == 200
    canonical = re.search(
        r'<link rel="canonical" href="([^"]+)"', resp.get_data(as_text=True)
    ).group(1)
    art_date = re.search(r"/alpha/(\d+/\d+)/", url).group(1)
    assert canonical.endswith(f"/beta/{art_date}/{art_id}"), canonical


def test_windowed_patch_thread_is_detected_from_the_whole_thread(
    client, tmp_path, monkeypatch
):
    """Only an elided message touches files. The thread is still a patch
    thread, so the related-discussions panel is not computed."""
    import mimir.web.routes.message as message_route

    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="fan_out", size=40)
    with SessionLocal() as s:
        s.add(ArticleFile(article_id=seeded["m39"][0], path="kernel/sched/core.c"))
        s.commit()
    calls = []
    monkeypatch.setattr(
        message_route,
        "related_discussions",
        lambda *a, **kw: calls.append(kw) or [],
    )
    rows = _tree(client.get(seeded["m0"][1]).get_data(as_text=True))
    assert seeded["m39"][0] not in _node_ids(rows)  # precondition: elided
    assert calls == []

    with SessionLocal() as s:
        s.execute(delete(ArticleFile))
        s.commit()
    client.get(seeded["m0"][1])
    assert len(calls) == 1  # the gate can open: it is not stuck shut


def test_windowed_orphans_exclude_the_whole_thread_through_the_column(
    client, tmp_path, monkeypatch
):
    """Same-subject orphans under an off-list root: thread members never
    appear, even ones the window does not show, and an outside article
    with the same subject does."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="off_list", size=30)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ArticleFile(article_id=seeded["m29"][0], path="mm/slab.c"))
        stranger = Article(
            message_id="stranger@elsewhere",
            subject="shape msg",
            subject_normalized="shape msg",
            author="z@example.com",
            date=s.get(Article, seeded["m0"][0]).date,
        )
        s.add(stranger)
        s.flush()
        s.add(
            ArticleList(
                article_id=stranger.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha="f" * 40,
                thread_root_id=stranger.id,
            )
        )
        s.commit()
        stranger_id = stranger.id

    html = client.get(seeded["m0"][1]).get_data(as_text=True)
    related = re.search(r"<h4>Possibly related.*?</ul>", html, re.DOTALL)
    assert related, "orphans panel missing"
    linked = {
        int(i) for i in re.findall(r'href="/alpha/\d+/\d+/(\d+)"', related.group(0))
    }
    assert linked == {stranger_id}


def test_window_counts_the_thread_page_once(client, tmp_path, monkeypatch):
    """The canonical tag and the window's gap links both want
    `thread_page_of` for the same inbox, root and article. One count
    serves both, on warm requests too."""
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="fan_out", size=20)
    _, url = seeded["m10"]
    assert client.get(url).status_code == 200
    statements = []
    with SessionLocal() as s:
        engine = s.get_bind()

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", grab)
    try:
        resp = client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert resp.status_code == 200
    assert "View all 20 messages" in resp.get_data(as_text=True)  # it is windowed
    page_counts = [
        st
        for st in statements
        if st.startswith("SELECT count(*)")
        and "article_lists.thread_root_id = ?" in st
        and "articles.id != ?" in st
    ]
    assert len(page_counts) == 1, page_counts


def _page_url(root_id, article_id, inbox_name):
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        page = thread_page_of(
            s,
            inbox.id,
            root_id,
            s.get(Article, article_id),
            settings.thread_view_render_cap,
        )
        return thread_page_url(root_id, s.get(Article, root_id).date, inbox_name, page)


def test_cross_posted_window_links_this_inbox_page_not_the_canonical_one(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(
        tmp_path,
        "alpha",
        shape="fan_out",
        size=40,
        dates="spread",
        cross_post_to="beta",
    )
    root_id = seeded["m0"][0]
    art_id, url = seeded["m20"]
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        s.get(Article, art_id).canonical_inbox_id = beta.id
        # Beta lacks the earlier replies, so m20 sits on another page there.
        s.execute(
            delete(ArticleList).where(
                ArticleList.inbox_id == beta.id,
                ArticleList.article_id.in_([seeded[f"m{i}"][0] for i in range(1, 16)]),
            )
        )
        s.commit()
    alpha_page = _page_url(root_id, art_id, "alpha")
    beta_page = _page_url(root_id, art_id, "beta")
    assert alpha_page.rsplit("/", 1)[-1] != beta_page.rsplit("/", 1)[-1]  # precondition

    html = client.get(url).get_data(as_text=True)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
    assert canonical.endswith(beta_page)  # precondition: canonical is beta's page
    assert "View all 40 messages" in html  # precondition: windowed in alpha
    gap_links = set(re.findall(r'href="(/alpha/[^"]*/t(?:/\d+)?)"', html))
    assert gap_links == {alpha_page}, (gap_links, alpha_page, beta_page)
