"""#798 review: a windowed page's validator moves when a reply lands below
the active message, or a sibling lands, without moving the max date."""

import pytest
from sqlalchemy import func, select

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from tests.test_routes._helpers import build_thread


@pytest.mark.parametrize("parent_key", ["m20", "m0"])
def test_windowed_etag_moves_on_an_old_dated_arrival(
    client, tmp_path, monkeypatch, parent_key
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    seeded = build_thread(tmp_path, "alpha", shape="fan_out", size=40, dates="spread")
    _, url = seeded["m20"]
    first = client.get(url)
    assert 'class="thread-all"' in first.get_data(as_text=True)  # windowed
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        root_id = seeded["m0"][0]
        max_before = s.execute(select(func.max(Article.date))).scalar()
        parent = s.get(Article, seeded[parent_key][0])
        new = Article(
            message_id="late@x",
            subject="late",
            subject_normalized="late",
            author="z@example.com",
            date=s.get(Article, seeded["m21"][0]).date,  # older than max
            thread_parent=parent.message_id,
        )
        s.add(new)
        s.flush()
        s.add(
            ArticleList(
                article_id=new.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha="e" * 40,
                thread_root_id=root_id,
            )
        )
        s.commit()
        assert s.execute(select(func.max(Article.date))).scalar() == max_before
    second = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert second.status_code == 200
