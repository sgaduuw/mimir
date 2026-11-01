"""Regression test for the #798 review: the same-subject orphans query on a
windowed page must stay driven by `ix_articles_subject_normalized`.

Excluding the thread through `article_lists.thread_root_id` on the JOINed
row adds a second predicate on `article_lists`, and without
`sqlite_stat1` the planner then drives the query from
`ix_article_lists_inbox_id`: every row of the inbox, per page view.
"""

import re

from sqlalchemy import event

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import ArticleFile
from tests.test_routes._helpers import _metadata_url, build_thread


def test_windowed_orphans_query_is_driven_by_the_subject_index(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="off_list", size=30)
    with SessionLocal() as s:
        # A patch thread, so the orphans query runs.
        s.add(ArticleFile(article_id=seeded["m29"][0], path="mm/slab.c"))
        s.commit()
        engine = s.get_bind()
    url = seeded["m0"][1]
    reader = client.get(url)
    assert reader.status_code == 200
    reader_html = reader.get_data(as_text=True)
    # This chain window can omit grandchildren without a visible gap row.
    visible = re.findall(r'<li\b[^>]*\bdata-article-id="(\d+)"', reader_html)
    assert seeded["m0"][0] in {int(aid) for aid in visible}
    assert 0 < len(visible) < 30
    assert "View all 30 messages" in reader_html

    statements = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", grab)
    try:
        response = client.get(_metadata_url(url))
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert response.status_code == 200

    orphans = [
        (stmt, params)
        for stmt, params in statements
        if "articles.subject_normalized = ?" in stmt and "article_lists" in stmt
    ]
    assert len(orphans) == 1, [s for s, _ in statements]
    stmt, params = orphans[0]
    with SessionLocal() as s:
        raw = s.connection().connection.driver_connection
        plan = "\n".join(
            row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + stmt, params)
        )
    assert "ix_articles_subject_normalized" in plan, plan
    assert "ix_article_lists_inbox_id" not in plan, plan
