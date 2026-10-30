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


def _seed_two_versions(tmp_path, n: int):
    """v1 of an n-patch series in its own thread, then v2 in a thread of
    its own with one reply, so each v2 patch has a revisions timeline (v1,
    v2) with a diff link. One repository, so every body (and so every
    series diff) can be read. Returns the map for the v2 thread; its
    message ids are `c@x`, `p<i>@x` and `r@x`."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    key = "w" * 40
    patches = range(1, n + 1)
    edges = (
        [("c1@x", None)]
        + [(f"q{i}@x", "c1@x") for i in patches]
        + [("c@x", None)]
        + [(f"p{i}@x", "c@x") for i in patches]
        + [("r@x", "p1@x")]
    )
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        for mid, (aid, _url) in seeded.items():
            if mid == "r@x":
                continue
            version = "v1" if mid in {"c1@x"} | {f"q{i}@x" for i in patches} else "v2"
            pos = 0 if mid.startswith("c") else int(mid[1:].split("@")[0])
            a = s.get(Article, aid)
            a.subject = f"[PATCH {version} {pos}/{n}] foo: part {pos}"
            a.patch_series_key = key
            a.patch_series_version = version
            a.patch_series_position = pos
        s.commit()
    return {m: seeded[m] for m in ["c@x", "r@x", *(f"p{i}@x" for i in patches)]}


def test_the_patches_index_links_each_patch_to_the_page_that_renders_it(
    client, tmp_path, monkeypatch
):
    """Rows cover the whole thread; each links to the page and anchor that
    hold the patch, checked by fetching that page, not by recomputing it."""
    import re

    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_two_versions(tmp_path, 3)
    html = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    index = html.split('class="patch-index"')[1].split("</section>")[0]
    links = dict(
        re.findall(r'data-patch="(\d+)".*?<a href="([^"]+)"', index, re.DOTALL)
    )
    patches = {str(seeded[f"p{i}@x"][0]) for i in (1, 2, 3)}
    assert set(links) == patches
    pages = {link.split("#")[0] for link in links.values()}
    assert len(pages) > 1  # precondition: the patches span pages
    for aid, link in links.items():
        path, anchor = link.split("#")
        assert anchor == f"m{aid}"
        page = client.get(path).get_data(as_text=True)
        assert f'id="m{aid}"' in page, f"{link} does not render {aid}"


def _timeline(html: str, start: int) -> str:
    """The `<ol class="revisions-timeline">` of the first revisions fold
    at or after `start`."""
    rest = html[start:]
    i = rest.index('class="revisions-timeline"')
    return rest[i : rest.index("</ol>", i)]


def test_each_patch_disclosure_matches_its_message_page_s_timeline(client, tmp_path):
    """Each in-series patch's revisions on `/t` are byte-identical to the
    ones its own message page renders (one builder, `_series_timeline`),
    and every diff link resolves. Per-position diffs are not in the series
    diff sitemap (covers only), so the message page is the reference."""
    import html as htmllib
    import re

    seeded = _seed_two_versions(tmp_path, 3)
    page = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    checked = 0
    for i in (1, 2, 3):
        aid, url = seeded[f"p{i}@x"]
        start = page.find(f'<details class="patch-detail" id="patch-{aid}"')
        assert start != -1, f"no disclosure for p{i}"
        on_t = _timeline(page, start)
        msg = client.get(url).get_data(as_text=True)
        assert on_t == _timeline(msg, msg.index('class="revisions-fold"'))
        diffs = re.findall(r'href="([^"]*/series/[^"]*)"', on_t)
        assert len(diffs) == 1, diffs  # precondition: v1 to v2
        assert client.get(htmllib.unescape(diffs[0])).status_code == 200
        checked += 1
    assert checked == 3


def test_the_disclosures_cost_the_same_statements_for_any_series_size(client, tmp_path):
    """One batched read for every patch's revisions (count executions):
    a 3-patch and a 30-patch series run the same number of statements.
    Holds while the series stays under `selectinload`'s 500-row batch."""
    from sqlalchemy import delete, event

    from mimir.extensions import SessionLocal, engine
    from mimir.models import CacheEntry

    counts = {}
    for n in (3, 30):
        (tmp_path / str(n)).mkdir()
        seeded = _seed_two_versions(tmp_path / str(n), n)
        with SessionLocal() as s:
            s.execute(delete(CacheEntry))
            s.commit()
        seen = []

        def grab(*a, seen=seen):
            seen.append(a[2])

        event.listen(engine, "before_cursor_execute", grab)
        try:
            html = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
        finally:
            event.remove(engine, "before_cursor_execute", grab)
        assert html.count('class="patch-detail"') == n  # precondition
        counts[n] = len(seen)
    assert counts[3] == counts[30], counts


def test_a_change_to_another_revision_moves_the_etag_and_the_disclosure(
    client, tmp_path
):
    """The disclosures need no validator input of their own because, for a
    cover root, `render_state_tag` digests every position of its series
    (D5). A reprocess that renumbers v1 of a patch, in another thread, must
    move this page's ETag and show in that patch's timeline."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    seeded = _seed_two_versions(tmp_path, 2)
    url = seeded["c@x"][1] + "/t"
    before = client.get(url)  # primes the caches
    p1 = seeded["p1@x"][0]
    with SessionLocal() as s:
        older = (
            s.query(Article)
            .filter(
                Article.patch_series_position == 1,
                Article.patch_series_version == "v1",
            )
            .one()
        )
        older.patch_series_version = "v0"
        s.commit()
    after = client.get(url)
    assert after.headers["ETag"] != before.headers["ETag"]
    html = after.get_data(as_text=True)
    start = html.index(f'<details class="patch-detail" id="patch-{p1}"')
    assert ">v0<" in _timeline(html, start)


def test_each_patch_loads_its_related_patches_from_a_noindex_fragment(client, tmp_path):
    """D1 (b): "Other recent patches touching these files" is not on `/t`
    (no validator can carry it, and it costs up to about 0.5 s a patch).
    Each patch in the index opens it on demand: `hx-get` fires once, on
    the first toggle, at a fragment route that answers `noindex`."""
    import re

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleFile

    seeded = _seed_two_versions(tmp_path, 2)
    p1, p2 = seeded["p1@x"][0], seeded["p2@x"][0]
    with SessionLocal() as s:
        # Both v2 patches touch one file, so each is the other's related
        # patch, and both are recent: the list looks back 180 days.
        s.add_all(ArticleFile(article_id=a, path="drivers/foo.c") for a in (p1, p2))
        for aid in (p1, p2):
            s.get(Article, aid).date = datetime.now(UTC)
        s.commit()
    page = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    tag = re.search(rf'<details class="patch-detail" id="patch-{p1}"[^>]*>', page)
    assert tag, "no disclosure"
    assert 'hx-trigger="toggle once"' in tag.group(0)
    assert " href=" not in tag.group(0)
    url = re.search(r'hx-get="([^"]+)"', tag.group(0)).group(1)
    assert url == f"/api/alpha/related/{p1}"
    fragment = client.get(url)
    assert fragment.status_code == 200
    assert fragment.headers["X-Robots-Tag"] == "noindex"
    assert fragment.headers["Cache-Control"] == "public, max-age=300"
    assert f"/{p2}" in fragment.get_data(as_text=True)
    assert "<html" not in fragment.get_data(as_text=True)  # a fragment


def test_the_related_fragment_is_404_outside_its_inbox(client, tmp_path):
    from tests.test_routes._helpers import _ingest_one_article

    art_id, _ = _ingest_one_article(tmp_path, "alpha", "rel-404@x")
    assert client.get(f"/api/alpha/related/{art_id}").status_code == 200
    assert client.get(f"/api/beta/related/{art_id}").status_code == 404
    assert client.get("/api/alpha/related/99999999").status_code == 404


def test_the_htmx_slice_and_markdown_do_not_read_what_only_the_full_page_renders(
    client, tmp_path, monkeypatch
):
    """The Patches index and its revisions are rendered by `thread.html`
    only. The HTMX slice (`_thread_messages.html`) needs the kinds, not the
    series read; the Markdown page needs neither. Each "load more" click on
    a large series thread should not re-read the whole series."""
    import mimir.web.routes.thread as thread_route

    calls = []
    real_timelines = thread_route.patch_timelines
    real_rows = thread_route.thread_rows

    def spy_timelines(*a, **kw):
        calls.append("patch_timelines")
        return real_timelines(*a, **kw)

    def spy_rows(*a, **kw):
        calls.append("thread_rows")
        return real_rows(*a, **kw)

    monkeypatch.setattr(thread_route, "patch_timelines", spy_timelines)
    monkeypatch.setattr(thread_route, "thread_rows", spy_rows)
    seeded = _seed_two_versions(tmp_path, 3)
    url = seeded["c@x"][1] + "/t"

    assert client.get(url).status_code == 200
    assert calls == ["thread_rows", "patch_timelines"]  # precondition

    calls.clear()
    assert client.get(url, headers={"HX-Request": "true"}).status_code == 200
    assert "patch_timelines" not in calls, calls

    calls.clear()
    assert client.get(url + ".md").status_code == 200
    assert calls == [], calls


def test_the_tabs_control_a_labelled_tabpanel(client, tmp_path):
    """WAI-ARIA tabs pattern: the element the tabs' `aria-controls` names is
    a `tabpanel` with an accessible name, or a screen reader announces three
    tabs that control nothing it can name."""
    import re

    seeded, _ = _seed_series_thread(tmp_path)
    html = client.get(seeded["cov@x"][1] + "/t").get_data(as_text=True)
    controls = set(re.findall(r'role="tab"[^>]*aria-controls="([^"]+)"', html))
    assert controls == {"thread-messages"}  # precondition
    panel = re.search(r'<[^>]*\bid="thread-messages"[^>]*>', html).group(0)
    assert 'role="tabpanel"' in panel, panel
    assert "aria-labelledby=" in panel or "aria-label=" in panel, panel


def test_a_patch_disclosure_s_summary_holds_no_link(client, tmp_path):
    """A link inside `<summary>` nests one control in another: a tap on the
    subject navigates instead of opening the revisions and related list,
    leaving only the marker as the toggle target."""
    import re

    seeded = _seed_two_versions(tmp_path, 2)
    html = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    summaries = re.findall(
        r'<details class="patch-detail"[^>]*>\s*<summary>(.*?)</summary>',
        html,
        re.DOTALL,
    )
    assert len(summaries) == 2  # precondition
    for s in summaries:
        assert "<a " not in s, s


def test_a_patch_of_another_series_gets_no_inline_revisions(client, tmp_path):
    """D5: inline revisions only for the root's own series, the rows the
    validator digests. A patch of another series in the same thread keeps
    its index row and related list, but not its timeline, even when that
    series has two versions of it."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    seeded = _seed_two_versions(tmp_path, 2)
    p2 = seeded["p2@x"][0]
    with SessionLocal() as s:
        older = (
            s.query(Article)
            .filter(
                Article.patch_series_position == 2,
                Article.patch_series_version == "v1",
            )
            .one()
        )
        # p2 and its v1 move to a series of their own.
        for a in (older, s.get(Article, p2)):
            a.patch_series_key = "o" * 40
        s.commit()
    html = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    start = html.index(f'<details class="patch-detail" id="patch-{p2}"')
    disclosure = html[start : html.index("</details>", start)]
    assert "<summary>Related patches</summary>" in disclosure
    assert "revisions-fold" not in disclosure
    # Precondition: p1, in the root's series, does get its timeline.
    p1 = seeded["p1@x"][0]
    assert "Revisions and related patches" in html.split(f'id="patch-{p1}"')[1][:400]
