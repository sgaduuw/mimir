"""The inbox dashboard's `ItemList` (#1003): each active thread is named
by the same URL its own message page names as canonical. A thread the
column cannot rank keeps its message URL there, single-message or not.
Started from Granny's #1003 review test."""

import json
import re
from datetime import UTC, datetime, timedelta


def _canonical(html: str) -> str | None:
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return m.group(1) if m else None


def _item_list_urls(html: str) -> list[str]:
    for block in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    ):
        data = json.loads(block)
        for node in data.get("@graph", [data]):
            entity = node.get("mainEntity") or {}
            if entity.get("@type") == "ItemList":
                return [i["url"] for i in entity["itemListElement"]]
    return []


def _make_recent(root_id: int) -> str:
    """Date the root an hour ago, so it is an active thread. Returns the
    root's message path."""
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from mimir.web.urls import _msg_url

    with SessionLocal() as s:
        s.execute(
            update(Article)
            .where(Article.id == root_id)
            .values(date=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()
        return _msg_url(s.get(Article, root_id), "alpha")


def _unroot_and_make_recent(root_id: int, unrooted_id: int) -> str:
    """`_make_recent`, and leave `unrooted_id`'s row with no
    `thread_root_id`, as a `reindex --from-scratch` or a stalled backfill
    does. Returns the root's message path."""
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    with SessionLocal() as s:
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id == unrooted_id)
            .values(thread_root_id=None)
        )
        s.commit()
    return _make_recent(root_id)


def _listed_and_canonical(client, root_id: int, msg_path: str):
    canonical = _canonical(client.get(msg_path).get_data(as_text=True))
    listed = [
        u
        for u in _item_list_urls(client.get("/alpha/").get_data(as_text=True))
        if re.search(rf"/{root_id}(/t)?$", u)
    ]
    return listed, canonical


def test_inbox_item_list_follows_the_message_canonical_for_an_unrooted_singleton(
    client, tmp_path
):
    from tests.test_routes._helpers import seed_thread_shape

    solo_id, _ = seed_thread_shape(tmp_path, "alpha", [("unrooted@x", None)])[
        "unrooted@x"
    ]
    msg_path = _unroot_and_make_recent(solo_id, solo_id)
    listed, canonical = _listed_and_canonical(client, solo_id, msg_path)
    assert canonical == f"http://localhost{msg_path}"  # precondition: no /t claim
    assert listed == [canonical], listed


def test_inbox_item_list_follows_the_message_canonical_mid_backfill(client, tmp_path):
    """A thread with a reply the backfill has not reached: unrankable, so
    its root keeps the message URL everywhere, the `ItemList` included."""
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("mb-root@x", None), ("mb-reply@x", "mb-root@x")]
    )
    root_id, reply_id = seeded["mb-root@x"][0], seeded["mb-reply@x"][0]
    msg_path = _unroot_and_make_recent(root_id, reply_id)
    listed, canonical = _listed_and_canonical(client, root_id, msg_path)
    assert canonical == f"http://localhost{msg_path}"  # precondition: no /t claim
    assert listed == [canonical], listed


def test_inbox_item_list_follows_the_message_canonical_for_a_rooted_singleton(
    client, tmp_path
):
    """The healthy direction, on the real dashboard and predicate: a rooted
    single-message thread is named by its `/t`, which is its canonical."""
    from tests.test_routes._helpers import seed_thread_shape

    solo_id, _ = seed_thread_shape(tmp_path, "alpha", [("rooted@x", None)])["rooted@x"]
    msg_path = _make_recent(solo_id)
    listed, canonical = _listed_and_canonical(client, solo_id, msg_path)
    assert canonical == f"http://localhost{msg_path}/t"  # precondition: a /t claim
    assert listed == [canonical], listed


def test_inbox_reply_count_links_to_the_thread_view(client, tmp_path):
    """The "N replies in 7d" link on `/<inbox>/` is the thread's `/t`."""
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("lk-root@x", None), ("lk-reply@x", "lk-root@x")]
    )
    root_id, reply_id = seeded["lk-root@x"][0], seeded["lk-reply@x"][0]
    msg_path = _make_recent(root_id)
    with SessionLocal() as s:
        s.execute(
            update(Article)
            .where(Article.id == reply_id)
            .values(date=datetime.now(UTC) - timedelta(minutes=30))
        )
        s.commit()
    body = client.get("/alpha/").get_data(as_text=True)
    assert f'<a href="{msg_path}/t">1 reply in 7d</a>' in body
