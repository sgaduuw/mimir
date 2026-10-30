"""#989: the thread page's tabs, Patches index and per-patch revisions."""

from datetime import UTC, datetime


def test_thread_rows_share_the_page_order_and_the_page_rank(client, tmp_path):
    """`thread_rows` is read in the page's own order, so a message's index
    in it gives the page that renders it. That must equal `thread_page_of`,
    the rule every canonical uses, including for dateless, tied and
    out-of-order dates."""
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox
    from mimir.threading import thread_by_root_id, thread_page_of, thread_rows
    from tests.test_routes._helpers import seed_thread_shape

    edges = [("r@x", None)] + [(f"m{i}@x", "r@x") for i in range(7)]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    root_id = seeded["r@x"][0]
    tie = datetime(2024, 1, 5, tzinfo=UTC)
    with SessionLocal() as s:
        dates = {
            "m1@x": None,
            "m2@x": tie,
            "m3@x": tie,
            "m4@x": tie,
            "m5@x": datetime(2000, 1, 1, tzinfo=UTC),  # older than the root
        }
        for mid, when in dates.items():
            s.execute(
                update(Article).where(Article.id == seeded[mid][0]).values(date=when)
            )
        s.commit()
        alpha = s.scalar(select(Inbox).where(Inbox.name == "alpha"))
        rows = thread_rows(s, alpha, root_id)
        nodes = thread_by_root_id(s, alpha, root_id)
        cap = 3
        pages = {r.id: i // cap + 1 for i, r in enumerate(rows)}
        ranked = {
            r.id: thread_page_of(s, alpha.id, root_id, s.get(Article, r.id), cap)
            for r in rows
        }
    assert [r.id for r in rows] == [n.id for n in nodes]
    assert rows[0].id == root_id
    assert max(pages.values()) == 3  # precondition: more than one page
    assert pages == ranked


def test_a_reprocessed_patch_row_moves_the_thread_etag(client, tmp_path):
    """A `backfill-patch-series --reprocess` can clear a row's series key
    and position, which turns a patch into a reply for the tabs and the
    Patches index, without adding a message to the thread. The root here
    is prose, so `render_state_tag` covers none of it: only the patch-rows
    digest in the ETag can see the change."""
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("q@x", None), ("fix@x", "q@x"), ("ack@x", "fix@x")]
    )
    fix_id = seeded["fix@x"][0]
    url = seeded["q@x"][1] + "/t"
    with SessionLocal() as s:
        fix = s.get(Article, fix_id)
        fix.subject = "[PATCH 1/2] foo: fix the thing"
        fix.patch_series_key = "k" * 40
        fix.patch_series_position = 1
        s.commit()
    before = client.get(url)
    assert before.status_code == 200
    with SessionLocal() as s:
        s.execute(
            update(Article)
            .where(Article.id == fix_id)
            .values(patch_series_key=None, patch_series_position=None)
        )
        s.commit()
    after = client.get(url)
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
