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


def _seed_series_thread(tmp_path):
    """A cover letter, two patches of its posting and one reply. Returns
    `seed_thread_shape`'s map plus each message's expected kind."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("cov@x", None),
            ("p1@x", "cov@x"),
            ("p2@x", "cov@x"),
            ("rev@x", "p1@x"),
        ],
    )
    series = {
        "cov@x": ("[PATCH v2 0/2] foo: do the thing", 0),
        "p1@x": ("[PATCH v2 1/2] foo: first", 1),
        "p2@x": ("[PATCH v2 2/2] foo: second", 2),
    }
    with SessionLocal() as s:
        for mid, (subject, position) in series.items():
            a = s.get(Article, seeded[mid][0])
            a.subject = subject
            a.patch_series_key = "t" * 40
            a.patch_series_version = "v2"
            a.patch_series_position = position
        s.commit()
    kinds = {"cov@x": "cover", "p1@x": "patch", "p2@x": "patch", "rev@x": "reply"}
    return seeded, kinds


def _articles(html: str) -> dict[int, str]:
    """`article id -> its opening <article> tag` for each message rendered."""
    import re

    return {
        int(m.group(1)): m.group(0)
        for m in re.finditer(r'<article class="thread-message" id="m(\d+)"[^>]*>', html)
    }


def test_every_message_is_rendered_and_carries_its_kind(client, tmp_path):
    """Tabs are display only: the page still renders every message, so the
    canonical invariant holds (each message is on the page its canonical
    names). Each carries its kind for the script, and the root says so."""
    seeded, kinds = _seed_series_thread(tmp_path)
    html = client.get(seeded["cov@x"][1] + "/t").get_data(as_text=True)
    tags = _articles(html)
    assert set(tags) == {aid for aid, _ in seeded.values()}
    for mid, (aid, _url) in seeded.items():
        assert f'data-kind="{kinds[mid]}"' in tags[aid], (mid, tags[aid])
    assert [aid for aid, tag in tags.items() if "data-root" in tag] == [
        seeded["cov@x"][0]
    ]


def test_the_tablist_counts_the_whole_thread_and_starts_hidden(client, tmp_path):
    """Whole-thread counts, as the issue counts them: replies, patches and
    everything (the cover is in neither of the first two). `hidden` until
    the script runs, so without JS the reader sees every message and no
    inert buttons. The reply count lets the script open on Everything when
    there is nothing to discuss."""
    import re

    seeded, _ = _seed_series_thread(tmp_path)
    html = client.get(seeded["cov@x"][1] + "/t").get_data(as_text=True)
    tablist = re.search(r'<div class="thread-tabs" role="tablist"[^>]*>', html)
    assert tablist, "no tablist"
    assert " hidden" in tablist.group(0)
    assert 'data-replies="1"' in tablist.group(0)
    counts = dict(
        re.findall(
            r'<button[^>]*data-tab="(\w+)"[^>]*>[^<]*<span class="tab-count">(\d+)</span>',
            html,
        )
    )
    assert counts == {"discussion": "1", "patches": "2", "all": "4"}


def test_no_tablist_without_patches_or_with_one_message(client, tmp_path):
    from tests.test_routes._helpers import _ingest_one_article, seed_thread_shape

    _, solo = _ingest_one_article(tmp_path, "alpha", "solo-tabs@x")
    (tmp_path / "talk").mkdir()
    talk = seed_thread_shape(
        tmp_path / "talk", "alpha", [("t0@x", None), ("t1@x", "t0@x")]
    )
    for url in (solo, talk["t0@x"][1]):
        html = client.get(url + "/t").get_data(as_text=True)
        assert 'role="tablist"' not in html, url
        assert 'class="thread-message"' in html  # precondition: it rendered


def test_the_htmx_slice_carries_each_message_s_kind(client, tmp_path, monkeypatch):
    """#990's rule on `/t`: what an HTMX append brings carries its own
    per-message data, so the script can sort appended messages too."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded, kinds = _seed_series_thread(tmp_path)
    fragment = client.get(
        seeded["cov@x"][1] + "/t/2", headers={"HX-Request": "true"}
    ).get_data(as_text=True)
    tags = _articles(fragment)
    assert len(tags) == 2  # precondition: page 2 of 2
    by_id = {aid: mid for mid, (aid, _) in seeded.items()}
    for aid, tag in tags.items():
        assert f'data-kind="{kinds[by_id[aid]]}"' in tag, tag


def test_the_tabs_script_loads_only_where_there_are_tabs(client, tmp_path):
    """A static, deferred file (CSP: no inline script), only on a thread
    page that renders the tablist. CSS does the hiding, keyed on the
    container's `data-tab`, so appended messages follow without JS work."""
    import re
    from pathlib import Path

    import mimir
    from tests.test_routes._helpers import _ingest_one_article

    seeded, _ = _seed_series_thread(tmp_path)
    tag = re.compile(r'<script src="/static/js/thread-tabs\.js" defer></script>')
    assert tag.search(client.get(seeded["cov@x"][1] + "/t").get_data(as_text=True))
    (tmp_path / "solo").mkdir()
    _, solo = _ingest_one_article(tmp_path / "solo", "alpha", "solo-js@x")
    for url in (solo, solo + "/t"):
        assert "thread-tabs.js" not in client.get(url).get_data(as_text=True), url
    css = (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    assert re.search(
        r'\.thread-messages\[data-tab="discussion"\] '
        r'\.thread-message\[data-kind="patch"\]:not\(\[data-root\]\)\s*\{[^}]*display:\s*none',
        css,
    )
    assert re.search(
        r'\.thread-messages\[data-tab="patches"\] '
        r'\.thread-message:not\(\[data-kind="patch"\]\):not\(\[data-root\]\)\s*\{[^}]*display:\s*none',
        css,
    )
