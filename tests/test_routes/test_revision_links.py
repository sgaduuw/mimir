"""Revision links on the thread page (follow-up to #989): every one equals
the revision's message-page canonical path, plus `#m<id>` where that is a
thread page, byte for byte (CONTEXT.md "Emitter and acceptor must share one
validity rule"), across cross-post, date and same-thread shapes. Written by
the whole-diff review; the cross-post and re-post cases kill the canonical
pick and current-thread mutants that the single-inbox tests let survive."""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update

from tests.test_routes.test_thread_tabs import _seed_two_versions


def _canon_path(client, aid):
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        a = s.get(Article, aid)
        name = s.scalar(
            select(Inbox.name)
            .join(ArticleList, ArticleList.inbox_id == Inbox.id)
            .where(ArticleList.article_id == aid)
            .order_by(Inbox.name)
        )
        url = f"/{name}/{a.date.year}/{a.date.month:02d}/{aid}" if a.date else None
    if url is None:
        return None
    r = client.get(url)
    if r.status_code == 301:
        r = client.get(r.headers["Location"])
    assert r.status_code == 200, (url, r.status_code)
    c = re.search(r'<link rel="canonical" href="([^"]+)"', r.get_data(as_text=True))
    return re.sub(r"^https?://[^/]+", "", c.group(1))


def _check(client, url):
    page = client.get(url).get_data(as_text=True)
    hrefs = re.findall(r'<a class="rev-label" href="([^"]+)"', page) + re.findall(
        r'<a href="([^"]+)">go to this post</a>', page
    )
    assert hrefs  # precondition: the page has revision links
    bad = []
    checked = 0
    for h in hrefs:
        path, _, anchor = h.partition("#")
        aid = int(anchor[1:]) if anchor else int(path.rstrip("/").split("/")[-1])
        canon = _canon_path(client, aid)
        if canon is None:
            # A dateless revision has no message page: its link must be the
            # thread page that renders it, with no redirect.
            r = client.get(path)
            if r.status_code != 200 or f'id="m{aid}"' not in r.get_data(as_text=True):
                bad.append((h, r.status_code))
        # Anchorless = message-page link: the message page must then be
        # self-canonical, else /t could have named a page and did not.
        elif canon != path:
            bad.append((h, canon))
        checked += 1
    assert checked == len(hrefs)
    return hrefs, bad


def _crosspost(mids, *, root_in_beta=None, canonical_beta=False, self_root=False):
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        alpha = s.scalar(select(Inbox).where(Inbox.name == "alpha"))
        beta = s.scalar(select(Inbox).where(Inbox.name == "beta"))
        ids = {
            m: s.scalar(select(Article.id).where(Article.message_id == m)) for m in mids
        }
        for m, aid in ids.items():
            al = s.scalar(
                select(ArticleList).where(
                    ArticleList.article_id == aid, ArticleList.inbox_id == alpha.id
                )
            )
            root = aid if self_root else ids.get(root_in_beta)
            s.add(
                ArticleList(
                    article_id=aid,
                    inbox_id=beta.id,
                    epoch=al.epoch,
                    commit_sha=al.commit_sha,
                    thread_root_id=root,
                )
            )
            if canonical_beta:
                s.get(Article, aid).canonical_inbox_id = beta.id
        s.commit()


@pytest.fixture
def cap2(monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)


def test_baseline(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert not bad


def test_v1_crossposted_canonical_beta(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    _crosspost(
        ["c1@x", "q1@x", "q2@x", "q3@x"], root_in_beta="c1@x", canonical_beta=True
    )
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert any("/beta/" in h for h in hrefs)
    assert not bad


def test_v1_crossposted_alpha_wins(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    _crosspost(["c1@x", "q1@x", "q2@x", "q3@x"], root_in_beta="c1@x")
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert not bad


def test_q2_alone_in_beta(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    _crosspost(["q2@x"], canonical_beta=True, self_root=True)
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert not bad


def test_q2_in_beta_stale_root(client, tmp_path, cap2):
    # beta row names c1 as root but c1 is not in beta (reindex leftover)
    seeded = _seed_two_versions(tmp_path, 3)
    _crosspost(["q2@x"], root_in_beta=None, canonical_beta=True)
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        beta = s.scalar(select(Inbox).where(Inbox.name == "beta"))
        c1 = s.scalar(select(Article.id).where(Article.message_id == "c1@x"))
        q2 = s.scalar(select(Article.id).where(Article.message_id == "q2@x"))
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id == q2, ArticleList.inbox_id == beta.id)
            .values(thread_root_id=c1)
        )
        s.commit()
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert not bad


def test_v2_thread_crossposted_viewed_from_alpha_canonical_beta(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    _crosspost(
        ["c1@x", "q1@x", "q2@x", "q3@x"], root_in_beta="c1@x", canonical_beta=True
    )
    _crosspost(
        ["c@x", "p1@x", "p2@x", "p3@x", "r@x"], root_in_beta="c@x", canonical_beta=True
    )
    for u in (
        seeded["c@x"][1] + "/t",
        seeded["c@x"][1].replace("/alpha/", "/beta/") + "/t",
    ):
        hrefs, bad = _check(client, u)
        assert not bad


def test_null_dates(client, tmp_path, cap2):
    seeded = _seed_two_versions(tmp_path, 3)
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        for m in ["q1@x", "q3@x"]:
            s.execute(update(Article).where(Article.message_id == m).values(date=None))
        s.execute(
            update(Article)
            .where(Article.message_id == "q2@x")
            .values(date=datetime(2000, 1, 1, tzinfo=UTC))
        )
        s.commit()
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert not bad
    # The two dateless revisions are linked on their thread page, which
    # `_check` verified by fetching it, not skipped.
    with SessionLocal() as s:
        dateless = s.scalars(
            select(Article.id).where(Article.message_id.in_(["q1@x", "q3@x"]))
        ).all()
    anchored = {int(h.rsplit("#m", 1)[1]) for h in hrefs if "#m" in h}
    assert len(dateless) == 2 and set(dateless) <= anchored, hrefs


def test_own_root_revision_in_an_unranked_thread_links_its_message_page(
    client, tmp_path, cap2
):
    """The unranked check runs before the own-root short-cut. With the v1
    patches unrooted, the v1 cover is alone in its group (the short-cut is
    eligible) and its thread unranked, so it links its self-canonical
    message page. Written by the fix review, red with the two arms
    swapped."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList

    seeded = _seed_two_versions(tmp_path, 3)
    with SessionLocal() as s:
        qs = s.scalars(
            select(Article.id).where(Article.message_id.in_(["q1@x", "q2@x", "q3@x"]))
        ).all()
        c1 = s.scalar(select(Article.id).where(Article.message_id == "c1@x"))
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id.in_(qs))
            .values(thread_root_id=None)
        )
        s.commit()
    page = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    links = re.findall(r'<a class="rev-label" href="([^"]+)"', page)
    c1_links = [
        h for h in links if h.endswith(f"#m{c1}") or h.rstrip("/").endswith(f"/{c1}")
    ]
    assert len(c1_links) == 1, links  # precondition: the v1 cover's fold renders
    canon = _canon_path(client, c1)
    assert canon is not None and not re.search(r"/t(/\d+)?$", canon)
    assert c1_links[0] == canon, (c1_links, canon)


def _seed_with_repost(tmp_path, n=3):
    """v1 thread, v2 thread with a re-post of v2 patch 2 (`pp2@x`) and of the
    cover (`cc@x`) inside the v2 thread, posted late."""
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
        + [("r@x", "p1@x"), ("r2@x", "p1@x"), ("pp2@x", "c@x"), ("cc@x", "c@x")]
    )
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        for mid, (aid, _url) in seeded.items():
            if mid in ("r@x", "r2@x"):
                continue
            version = "v1" if mid in {"c1@x"} | {f"q{i}@x" for i in patches} else "v2"
            pos = 0 if mid.startswith("c") else int(mid.lstrip("pq").split("@")[0])
            a = s.get(Article, aid)
            a.subject = f"[PATCH {version} {pos}/{n}] foo: part {pos}"
            a.patch_series_key = key
            a.patch_series_version = version
            a.patch_series_position = pos
        s.commit()
    return seeded


def test_repost_in_current_thread(client, tmp_path, cap2):
    seeded = _seed_with_repost(tmp_path)
    hrefs, bad = _check(client, seeded["c@x"][1] + "/t")
    assert any("go to" not in h for h in hrefs)
    assert not bad


def test_repost_in_current_thread_crossposted_canonical_beta(client, tmp_path, cap2):
    seeded = _seed_with_repost(tmp_path)
    allv2 = ["c@x", "p1@x", "p2@x", "p3@x", "r@x", "r2@x", "pp2@x", "cc@x"]
    _crosspost(allv2, root_in_beta="c@x", canonical_beta=True)
    for u in (
        seeded["c@x"][1] + "/t",
        seeded["c@x"][1].replace("/alpha/", "/beta/") + "/t",
    ):
        hrefs, bad = _check(client, u)
        assert not bad


def _revision_statements(client, tmp_path, versions):
    import mimir.web.routes.thread as t
    from mimir.extensions import SessionLocal, engine
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    n = 10
    edges = []
    for v in range(1, versions):  # older versions: every patch its own thread
        edges += [(f"c{v}@x", None)] + [(f"q{v}_{i}@x", None) for i in range(1, n + 1)]
    edges += [("c@x", None)] + [(f"p{i}@x", "c@x") for i in range(1, n + 1)]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        for mid, (aid, _) in seeded.items():
            a = s.get(Article, aid)
            if mid.startswith("c"):
                ver = mid[1:-2] or str(versions)
                pos = 0
            elif mid.startswith("q"):
                ver, pos = mid[1:-2].split("_")
            else:
                ver, pos = str(versions), mid[1:-2]
            a.subject = f"[PATCH v{ver} {pos}/{n}] foo"
            a.patch_series_key = "k" * 40
            a.patch_series_version = f"v{ver}"
            a.patch_series_position = int(pos)
        s.commit()

    counts = {"n": 0, "on": False}
    orig = t._revision_hrefs

    def wrapped(*a, **kw):
        counts["on"] = True
        try:
            return orig(*a, **kw)
        finally:
            counts["on"] = False

    def before(*_a, **_k):
        if counts["on"]:
            counts["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    t._revision_hrefs = wrapped
    try:
        r = client.get(seeded["c@x"][1] + "/t")
    finally:
        t._revision_hrefs = orig
        event.remove(engine, "before_cursor_execute", before)
    assert r.status_code == 200
    # Precondition: every older revision (cover and each patch, per older
    # version) got a thread-page link, parsed rather than counted as text.
    links = re.findall(r'<a class="rev-label" href="([^"]+)"', r.get_data(as_text=True))
    linked = [h for h in links if re.fullmatch(r"/\S+/t(/\d+)?#m\d+", h)]
    assert len(linked) == len(links) == (versions - 1) * (n + 1), links
    return counts["n"]


def test_unthreaded_revisions_cost_the_same_statements_for_any_count(client, tmp_path):
    """Older versions posted unthreaded make every revision its own root.
    A root is on page 1 (`thread_page_of`), so those cost no thread read:
    the statements stay constant as versions grow."""
    counts = {}
    for versions in (2, 5):
        (tmp_path / str(versions)).mkdir()
        counts[versions] = _revision_statements(
            client, tmp_path / str(versions), versions
        )
    assert counts[2] == counts[5], counts
