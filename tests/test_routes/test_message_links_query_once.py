"""The message page reads the article's inbox links once.

The redirect, the ETag and the render all use one `all_links` read.
Merging #650 and #653 left a second identical SELECT behind, which
every message page, 304s included, paid for.
"""

from tests.test_routes._helpers import _ingest_one_article


def test_message_page_reads_inbox_links_once(client, tmp_path):
    from sqlalchemy import event

    from mimir.extensions import engine

    _, url = _ingest_one_article(tmp_path, "alpha", "links-once@example.com")

    link_selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, parameters, context, executemany):
        if (
            "FROM inboxes JOIN article_lists" in statement
            and "article_lists.article_id = ?" in statement
        ):
            link_selects.append(statement)

    try:
        resp = client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", _grab)

    assert resp.status_code == 200
    assert len(link_selects) == 1, (
        f"inbox-links SELECT ran {len(link_selects)} times:\n"
        + "\n---\n".join(link_selects)
    )
