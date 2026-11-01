"""Tests for mimir/web/routes/series_diff.py: the inter-revision
patch-series diff route (`pos=cover` and per-position links,
indexed-primary resolution + heuristic fallback for awaiting-
backfill cases)."""

import html as html_lib
import re

import pytest
from sqlalchemy import select

from mimir import patch_revisions
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, ArticleList, Inbox
from tests.test_routes._helpers import _ingest_series_pair, _metadata_url, _seed_resends


def test_series_diff_cover_letter_renders(client, tmp_path):
    """Happy path: two cover letters with the same patch_series_key,
    `pos=cover` diffs their bodies. The diff appears in the rendered
    HTML wrapped in Pygments markup."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            (
                "v1-cv@x",
                "[PATCH 0/3] improve foo handling",
                None,
                b"original cover letter explanation\n",
                author,
            ),
        ],
        v2_messages=[
            (
                "v2-cv@x",
                "[PATCH v2 0/3] improve foo handling",
                None,
                b"revised cover letter explanation\nadded a Fixes line\n",
                author,
            ),
        ],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Pygments-wrapped diff content: both sides appear in the HTML.
    assert "original cover letter explanation" in body
    assert "revised cover letter explanation" in body
    assert "added a Fixes line" in body


def test_series_diff_identical_cover_letters(client, tmp_path):
    """When v1 and v2 bodies are byte-identical, render the
    "no changes" message rather than an empty diff."""
    author = "Alice <a@example>"
    body = b"identical cover\nbyte for byte\n"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("c1@x", "[PATCH 0/2] series", None, body, author)],
        v2_messages=[("c2@x", "[PATCH v2 0/2] series", None, body, author)],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    out = resp.data.decode()
    assert "No changes between v1 and v2" in out


def test_series_diff_over_cap_renders_notice_with_both_links(
    client, tmp_path, monkeypatch
):
    """An oversize pair shows a plain notice and links to both
    revisions instead of a diff (#801)."""
    monkeypatch.setattr(patch_revisions, "REVISION_DIFF_MAX_LINES", 2)
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("c1@x", "[PATCH 0/2] series", None, b"a\nb\nc\n", author)],
        v2_messages=[("c2@x", "[PATCH v2 0/2] series", None, b"a\nb\nd\n", author)],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    out = resp.data.decode()
    assert "too large to compare here." in out
    links = re.findall(r'<a href="([^"]+)">(v[12])</a>', out)
    assert [label for _, label in links] == ["v1", "v2"]
    assert all(url for url, _ in links)
    assert 'class="highlight"' not in out


def test_series_diff_per_patch_match_by_subject(client, tmp_path):
    """In-series patch (pos=1): matches v1's [PATCH 1/2] subject to
    v2's [PATCH v2 1/2] subject; diffs the patch bodies."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("v1-cv@x", "[PATCH 0/2] series title", None, b"cover\n", author),
            (
                "v1-p1@x",
                "[PATCH 1/2] foo: do bar",
                "v1-cv@x",
                b"v1 commit message\n---\n diff --git a/fs/foo.c b/fs/foo.c\n@@\n+old\n",
                author,
            ),
            ("v1-p2@x", "[PATCH 2/2] baz: fix", "v1-cv@x", b"v1 baz commit\n", author),
        ],
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/2] series title", None, b"cover v2\n", author),
            (
                "v2-p1@x",
                "[PATCH v2 1/2] foo: do bar",
                "v2-cv@x",
                b"v2 commit message updated\n---\n diff --git a/fs/foo.c b/fs/foo.c\n@@\n+new\n",
                author,
            ),
            (
                "v2-p2@x",
                "[PATCH v2 2/2] baz: fix",
                "v2-cv@x",
                b"v2 baz commit\n",
                author,
            ),
        ],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=1")
    assert resp.status_code == 200
    out = resp.data.decode()
    # Both patch bodies' distinctive content surfaces in the diff.
    assert "v1 commit message" in out
    assert "v2 commit message updated" in out
    h1 = html_lib.unescape(re.search(r"<h1>([^<]*)</h1>", out).group(1))
    assert h1 == "series title: v1→v2 diff (patch 1)", h1


def test_series_diff_no_match_404(client, tmp_path):
    """When v1 has pos=3 and v2 has no plausible counterpart (different
    subjects, no file overlap), 404 with an actionable message."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("v1-cv@x", "[PATCH 0/3] series", None, b"cover\n", author),
            ("v1-p1@x", "[PATCH 1/3] foo: do A", "v1-cv@x", b"v1 A\n", author),
            ("v1-p2@x", "[PATCH 2/3] bar: do B", "v1-cv@x", b"v1 B\n", author),
            ("v1-p3@x", "[PATCH 3/3] baz: do C", "v1-cv@x", b"v1 C\n", author),
        ],
        # v2 dropped patch 3 entirely; no subject match, no file overlap.
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/3] series", None, b"cover v2\n", author),
            ("v2-p1@x", "[PATCH v2 1/2] foo: do A", "v2-cv@x", b"v2 A\n", author),
            ("v2-p2@x", "[PATCH v2 2/2] bar: do B", "v2-cv@x", b"v2 B\n", author),
        ],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=3")
    assert resp.status_code == 404


def test_series_diff_unknown_version_404(client, tmp_path):
    """Asking for a version that doesn't exist returns 404. The
    available revisions are surfaced in the 404 description for
    debuggability."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("c1@x", "[PATCH 0/1] series", None, b"v1\n", author)],
        v2_messages=[("c2@x", "[PATCH v2 0/1] series", None, b"v2\n", author)],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v9&pos=cover")
    assert resp.status_code == 404
    available = re.search(r'Available: ([^<">]*)[<"]', resp.data.decode())
    assert available is not None
    assert available.group(1) == "v1"


def test_series_diff_self_diff_404(client, tmp_path):
    """`from=v1&to=v1` is meaningless; reject as 404 rather than
    rendering an empty diff page."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("c1@x", "[PATCH 0/1] series", None, b"v1\n", author)],
        v2_messages=[("c2@x", "[PATCH v2 0/1] series", None, b"v2\n", author)],
    )
    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v1&pos=cover")
    assert resp.status_code == 404


def test_series_diff_unknown_series_key_404(client, tmp_path):
    """A `series_key` that doesn't exist in the DB returns plain 404,
    not the "available revisions" message."""
    resp = client.get("/alpha/series/deadbeef/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 404


def test_series_diff_missing_inbox_404(client, tmp_path):
    """Unknown inbox slug 404s (the inbox guard runs before any
    series resolution)."""
    resp = client.get("/no-such-inbox/series/deadbeef/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 404


def test_series_diff_sidebar_links_on_cover_page(client, tmp_path):
    """Viewing a cover letter that has revisions, the patch-state
    card's Series-revisions row renders a `diff against previous`
    link per revision after the first, pointing at the new route."""
    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("v1-cv@x", "[PATCH 0/2] series title", None, b"v1 cover\n", author)
        ],
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/2] series title", None, b"v2 cover\n", author)
        ],
    )
    # Viewing v2 cover, the card should link a diff from v1 to v2.
    from sqlalchemy import select as _sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        v2_art = s.execute(
            _sa_select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        v2_url = f"/alpha/{v2_art.date.year}/{v2_art.date.month:02d}/{v2_art.id}"
    body = client.get(_metadata_url(v2_url)).data.decode()
    # Badge redesign: series revisions moved from the aside into
    # `_revisions_fold.html`; scope the assertions to that fold.
    fold = body.split('class="revisions-fold"')[1].split("</details>")[0]
    assert "diff against previous" in fold
    assert f"/alpha/series/{series_key}/diff" in fold
    assert "from=v1" in fold
    assert "to=v2" in fold
    assert "pos=cover" in fold


def test_series_diff_uses_indexed_lookup_without_thread_parent(
    client,
    tmp_path,
):
    """#212: the diff route's indexed-lookup path resolves
    `(key, version, position)` directly, without needing thread
    structure between cover and in-series patches. Seed two
    in-series patches with `patch_series_*` columns set but
    `thread_parent=None`, the heuristic resolver from #210 would
    fail (no children to walk), the indexed path succeeds."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    # Two cover letters (needed for the 404-availability lookup the
    # route does upfront) plus two in-series patches with no thread
    # linkage. Build a minimal mirror so `read_message` finds the
    # patch bodies.
    author = "Alice <a@example>"
    msgs_v1 = [
        ("v1-cv@x", "[PATCH 0/2] series", None, b"cover v1\n", author),
        (
            "v1-p1@x",
            "[PATCH 1/2] foo: do bar",
            None,  # NO thread parent
            b"v1 patch body\n",
            author,
        ),
    ]
    msgs_v2 = [
        ("v2-cv@x", "[PATCH v2 0/2] series", None, b"cover v2\n", author),
        (
            "v2-p1@x",
            "[PATCH v2 1/2] foo: do bar",
            None,  # NO thread parent
            b"v2 patch body updated\n",
            author,
        ),
    ]
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=msgs_v1,
        v2_messages=msgs_v2,
    )
    # Verify the seeded state matches the test premise: the in-series
    # patches have the indexed columns set even without thread linkage.
    with SessionLocal() as s:
        v1_p1 = s.execute(
            select(Article).where(Article.message_id == "v1-p1@x")
        ).scalar_one()
        v2_p1 = s.execute(
            select(Article).where(Article.message_id == "v2-p1@x")
        ).scalar_one()
    # Without a thread parent, ingest can't link the patch to its
    # cover, so key/version stay NULL here. Backfill them inline
    # to mirror the post-`backfill-patch-series` state and exercise
    # the indexed resolver.
    with SessionLocal() as s:
        v1_cover = s.execute(
            select(Article).where(Article.message_id == "v1-cv@x")
        ).scalar_one()
        v2_cover = s.execute(
            select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        for patch, cover in [(v1_p1, v1_cover), (v2_p1, v2_cover)]:
            p = s.get(Article, patch.id)
            p.patch_series_key = cover.patch_series_key
            p.patch_series_version = cover.patch_series_version
            # position already 1 from ingest's parser-only path.
        s.commit()

    resp = client.get(f"/alpha/series/{series_key}/diff?from=v1&to=v2&pos=1")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "v1 patch body" in body
    assert "v2 patch body updated" in body


def _file_in_beta(tmp_path, message_ids):
    """Link the named articles into `beta` too, pointing at the blobs
    `alpha` already holds (same epoch repos, so `beta.mirror_path` is
    `tmp_path` as well). Models a revision that was Cc'd to a second
    list without ingesting it twice."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        beta.mirror_path = str(tmp_path)
        for mid in message_ids:
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            (alpha_link,) = art.lists
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=beta.id,
                    epoch=alpha_link.epoch,
                    commit_sha=alpha_link.commit_sha,
                )
            )
        s.commit()


def _cross_list_series(tmp_path):
    """v1 filed only in alpha; v2 in alpha and beta. Returns the series
    key. Asserts the shape the tests exist for."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    author = "Alice <a@example>"
    series_key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("v1-cv@x", "[PATCH 0/1] series", None, b"v1 cover\n", author)],
        v2_messages=[("v2-cv@x", "[PATCH v2 0/1] series", None, b"v2 cover\n", author)],
    )
    _file_in_beta(tmp_path, ["v2-cv@x"])
    with SessionLocal() as s:
        homes = {
            mid: {
                al.inbox.name
                for al in s.execute(select(Article).where(Article.message_id == mid))
                .scalar_one()
                .lists
            }
            for mid in ("v1-cv@x", "v2-cv@x")
        }
    assert homes == {"v1-cv@x": {"alpha"}, "v2-cv@x": {"alpha", "beta"}}
    return series_key


def test_series_diff_reads_side_filed_in_another_inbox(client, tmp_path):
    """#661: the diff linked from beta's v2 page compares against v1,
    which beta never received. Both sides must still be read."""
    series_key = _cross_list_series(tmp_path)
    resp = client.get(f"/beta/series/{series_key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "v1 cover" in body
    assert "v2 cover" in body


def _canonical_of(art):
    from mimir.web.urls import _canonical_url_for

    return _canonical_url_for(art, [(al.inbox_id, al.inbox.name) for al in art.lists])


def test_series_diff_message_links_use_canonical_helper(client, tmp_path):
    """#661: `from_url`/`to_url` come from `_canonical_url_for`, not
    the URL's inbox. Both revisions are filed in alpha and beta, so
    the diff renders from either and the two builders can disagree
    without any 404 to hide it: beta's URL must still link to alpha,
    the canonical inbox."""
    from mimir.web.urls import _msg_url

    series_key = _cross_list_series(tmp_path)
    _file_in_beta(tmp_path, ["v1-cv@x"])
    resp = client.get(f"/beta/series/{series_key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    hrefs = re.findall(r'<a href="([^"]+)">message</a>', resp.data.decode())
    assert len(hrefs) == 2
    expected = []
    with SessionLocal() as s:
        for mid in ("v1-cv@x", "v2-cv@x"):
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            assert {al.inbox.name for al in art.lists} == {"alpha", "beta"}
            expected.append(_canonical_of(art))
            # Precondition: the URL-inbox builder would disagree.
            assert _msg_url(art, "beta") != expected[-1]
    assert hrefs == expected


def _two_mirrors(tmp_path, first):
    """One series whose messages are archived in BOTH inboxes from
    separate mirrors, the beta copies carrying a list footer, as
    mailman does. `first` is the inbox ingested first, which decides
    the order of each article's `lists` rows. Returns the series key."""
    footer = b"___\nbeta mailing list footer\n"
    author = "Alice <a@example>"
    key = None
    for name in (first, "beta" if first == "alpha" else "alpha"):
        tail = footer if name == "beta" else b""
        (tmp_path / name).mkdir()
        key = _ingest_series_pair(
            tmp_path / name,
            name,
            v1_messages=[
                ("v1-cv@x", "[PATCH 0/1] s", None, b"v1 cover\n" + tail, author)
            ],
            v2_messages=[
                ("v2-cv@x", "[PATCH v2 0/1] s", None, b"v2 cover\n" + tail, author)
            ],
        )
    with SessionLocal() as s:
        for mid in ("v1-cv@x", "v2-cv@x"):
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            # Precondition: one article, filed in both, in `first`'s order.
            order = [al.inbox.name for al in art.lists]
            assert sorted(order) == ["alpha", "beta"]
    return key


def test_series_diff_body_independent_of_first_requesting_inbox(client, tmp_path):
    """Each list archives its own copy of a message, and the body the
    diff shows depends on which inbox it was read from. The cached diff
    is keyed per inbox, so beta's footer must not reach /alpha/."""
    key = _two_mirrors(tmp_path, "alpha")
    q = "diff?from=v1&to=v2&pos=cover"
    alone = client.get(f"/alpha/series/{key}/{q}")
    assert alone.status_code == 200
    assert "beta mailing list footer" not in alone.data.decode()  # precondition
    from tests.test_routes._helpers import _clear_sitemap_cache

    _clear_sitemap_cache()  # drops every cache row, not only sitemaps
    beta = client.get(f"/beta/series/{key}/{q}")
    assert "beta mailing list footer" in beta.data.decode()  # precondition
    after = client.get(f"/alpha/series/{key}/{q}")
    assert after.status_code == 200
    assert "beta mailing list footer" not in after.data.decode()


def test_series_diff_heuristic_result_not_shared_across_inboxes(client, tmp_path):
    """The heuristic fallback walks thread children in the URL's inbox.
    alpha holds two v2 copies of patch 1 (the matcher cannot pick,
    strict 404); beta holds one. A beta request must not change what
    alpha answers."""
    author = "Alice <a@example>"
    key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("v1-cv@x", "[PATCH 0/1] s", None, b"v1 cover\n", author),
            ("v1-p1@x", "[PATCH 1/1] foo: bar", "v1-cv@x", b"v1 p1\n", author),
        ],
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/1] s", None, b"v2 cover\n", author),
            ("v2-p1@x", "[PATCH v2 1/1] foo: bar", "v2-cv@x", b"v2 p1\n", author),
            ("v2-p1b@x", "[PATCH v2 1/1] foo: bar", "v2-cv@x", b"v2 p1b\n", author),
        ],
    )
    _file_in_beta(tmp_path, ["v1-cv@x", "v1-p1@x", "v2-cv@x", "v2-p1@x"])
    # Force the heuristic: v2's in-series rows are "awaiting backfill".
    with SessionLocal() as s:
        for mid in ("v2-p1@x", "v2-p1b@x"):
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            art.patch_series_position = None
        s.commit()
    q = "diff?from=v1&to=v2&pos=1"
    assert client.get(f"/alpha/series/{key}/{q}").status_code == 404  # precondition
    assert client.get(f"/beta/series/{key}/{q}").status_code == 200
    assert client.get(f"/alpha/series/{key}/{q}").status_code == 404


def _canonical(html):
    m = re.search(r'<link rel="canonical" href="([^"]*)">', html)
    assert m is not None, "page nominates no canonical"
    return html_lib.unescape(m.group(1))


def test_series_diff_canonical_is_the_panel_link_and_resolves(client, tmp_path):
    """#661 item 5: the diff page names itself, query string included,
    byte-identically to the link the revision panel publishes, and that
    URL serves the page. Without a `canonical_url` the page fell back to
    the bare request path, which 404s."""
    key = _cross_list_series(tmp_path)
    q = "from=v1&to=v2&pos=cover"
    # The panel on the current (v2) page, viewed from beta.
    with SessionLocal() as s:
        v2 = s.execute(
            select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        beta_url = f"/beta/{v2.date.year}/{v2.date.month:02d}/{v2.id}"
    page = client.get(_metadata_url(beta_url))
    assert page.status_code == 200
    fold = page.data.decode().split('class="revisions-fold"')[1].split("</details>")[0]
    (panel_href,) = re.findall(r'href="([^"]*/diff\?[^"]*)"', fold)
    panel_href = html_lib.unescape(panel_href)
    resp = client.get(f"/beta/series/{key}/diff?{q}")
    assert resp.status_code == 200  # precondition
    html = resp.data.decode()
    canonical = _canonical(html)
    assert "?" in canonical  # precondition: not the bare path
    # Absolute canonical vs the panel's relative link: same bytes after the host.
    assert canonical.endswith(panel_href)
    og = re.search(r'og:url" content="([^"]*)"', html).group(1)
    assert html_lib.unescape(og) == canonical
    path = re.sub(r"^https?://[^/]+", "", canonical)
    assert client.get(path).status_code == 200


def test_series_diff_one_canonical_across_inboxes(client, tmp_path):
    """The same diff renders under every inbox either side is filed in.
    Those pages must nominate one canonical."""
    key = _cross_list_series(tmp_path)
    q = "diff?from=v1&to=v2&pos=cover"
    a = client.get(f"/alpha/series/{key}/{q}")
    b = client.get(f"/beta/series/{key}/{q}")
    assert (a.status_code, b.status_code) == (200, 200)  # precondition
    assert _canonical(a.data.decode()) == _canonical(b.data.decode())


def test_series_diff_url_nominates_inbox_of_the_slot_patch():
    """The panel and the route hand the helper the slot's patch; the
    nominated inbox is that article's. The lower-id article lives in
    beta only, the other in alpha only."""
    from mimir.web.urls import _series_diff_url

    _seed_resends([["beta"], ["alpha"]])
    with SessionLocal() as s:
        low, high = sorted(
            s.execute(
                select(Article).where(Article.message_id.like("resend-%"))
            ).scalars(),
            key=lambda a: a.id,
        )
        assert {al.inbox.name for al in low.lists} == {"beta"}  # precondition
        assert {al.inbox.name for al in high.lists} == {"alpha"}
        urls = [
            _series_diff_url(
                winner,
                "k",
                "v1",
                "v2",
                0,
                head_id=None,
                fallback_inbox="zeta",
                base="https://x",
            )
            for winner in (low, high, None)
        ]
    assert urls == [
        "https://x/beta/series/k/diff?from=v1&to=v2&pos=cover",
        "https://x/alpha/series/k/diff?from=v1&to=v2&pos=cover",
        "https://x/zeta/series/k/diff?from=v1&to=v2&pos=cover",
    ]


def test_series_diff_canonical_serves_when_heuristic_path_used(client, tmp_path):
    """v1's patch awaits backfill, so the body comes from the heuristic,
    which walks children in the URL's inbox only. The page must nominate
    that inbox, not the to-side's canonical one (beta), where v1 was
    never sent."""
    author = "Alice <a@example>"
    key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("v1-cv@x", "[PATCH 0/1] s", None, b"v1 cover\n", author),
            ("v1-p1@x", "[PATCH 1/1] foo: bar", "v1-cv@x", b"v1 p1\n", author),
        ],
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/1] s", None, b"v2 cover\n", author),
            ("v2-p1@x", "[PATCH v2 1/1] foo: bar", "v2-cv@x", b"v2 p1\n", author),
        ],
    )
    _file_in_beta(tmp_path, ["v2-cv@x", "v2-p1@x"])
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        for mid, canon, pos in (("v2-p1@x", beta.id, 1), ("v1-p1@x", None, None)):
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            art.canonical_inbox_id = canon or art.canonical_inbox_id
            art.patch_series_position = pos  # v1: awaiting backfill
        s.commit()
    resp = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=1")
    assert resp.status_code == 200  # precondition: renders where v1 lives
    canonical = _canonical(resp.data.decode())
    assert canonical.startswith("http://localhost/alpha/")  # beta would 404
    assert client.get(re.sub(r"^https?://[^/]+", "", canonical)).status_code == 200


def test_series_diff_canonical_ignores_unfiled_canonical_inbox(client, tmp_path):
    """`canonical_inbox_id` can name an inbox the article is not filed
    in. The nominated inbox must be a filed one."""
    key = _cross_list_series(tmp_path)
    with SessionLocal() as s:
        gamma = Inbox(
            name="gamma", mirror_path="/nonexistent", upstream_url="https://x/g"
        )
        s.add(gamma)
        s.flush()
        v2 = s.execute(
            select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        v2.canonical_inbox_id = gamma.id
        s.commit()
        assert "gamma" not in {al.inbox.name for al in v2.lists}  # precondition
    resp = client.get(f"/beta/series/{key}/diff?from=v1&to=v2&pos=cover")
    assert resp.status_code == 200
    canonical = _canonical(resp.data.decode())
    assert "/gamma/" not in canonical
    assert client.get(re.sub(r"^https?://[^/]+", "", canonical)).status_code == 200


def _put_series_under_head(head_of):
    """Give both covers the head id `head_of(v1_cover_id)`, as the chain
    backfill does for a series split off its key (#816). Returns it."""
    with SessionLocal() as s:
        covers = [
            s.execute(select(Article).where(Article.message_id == mid)).scalar_one()
            for mid in ("v1-cv@x", "v2-cv@x")
        ]
        head = head_of(covers[0].id)
        for c in covers:
            c.patch_series_head_id = head
        s.commit()
    return head


@pytest.mark.parametrize("split", [False, True], ids=["key_only", "split_off"])
def test_series_diff_panel_link_and_route_agree_on_the_series_head(
    client, tmp_path, split
):
    """#816: a series split off its key is (key, head). The panel spells
    the head as `s=`, omitted for NULL so existing links stay
    byte-identical, the page names the same URL as its canonical, and
    the route serves it. The bare key must not serve a split-off series,
    or the old merged one would answer for it."""
    key = _cross_list_series(tmp_path)
    head = _put_series_under_head(lambda v1_id: v1_id if split else None)
    with SessionLocal() as s:
        v2 = s.execute(
            select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        page_url = f"/alpha/{v2.date.year}/{v2.date.month:02d}/{v2.id}"
    fold = client.get(_metadata_url(page_url)).data.decode()
    fold = fold.split('class="revisions-fold"')[1].split("</details>")[0]
    (panel_href,) = re.findall(r'href="([^"]*/diff\?[^"]*)"', fold)
    panel_href = html_lib.unescape(panel_href)
    query = panel_href.split("?", 1)[1]
    expected = "from=v1&to=v2&pos=cover" + (f"&s={head}" if split else "")
    assert query == expected

    resp = client.get(panel_href)
    assert resp.status_code == 200
    assert _canonical(resp.data.decode()).endswith(panel_href)
    bare = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover"
    assert client.get(bare).status_code == (404 if split else 200)


@pytest.mark.parametrize("s_value", ["", "0", "-3", "abc", "1.5", "99999"])
def test_series_diff_rejects_a_bad_or_unknown_series_head(client, tmp_path, s_value):
    """`s` is validated like `pos`: a positive integer naming a head the
    key holds. Anything else 404s rather than serving the NULL-head
    series under a URL that names another."""
    key = _cross_list_series(tmp_path)
    ok = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover")
    assert ok.status_code == 200  # precondition: the NULL-head series serves
    resp = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover&s={s_value}")
    assert resp.status_code == 404


def _two_row_slot(tmp_path):
    """v1 with one cover, v2 with two covers (a resend with its own body),
    all in alpha. Returns `(series_key, low_id, high_id)`, the v2 slot's
    ids. Asserts the slot holds two rows and neither has a diff."""
    author = "Alice <a@example>"
    key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("v1-cv@x", "[PATCH 0/1] series", None, b"v1 cover\n", author)],
        v2_messages=[
            ("v2-cv@x", "[PATCH v2 0/1] series", None, b"v2 low body\n", author),
            ("v2-cv-b@x", "[PATCH v2 0/1] series", None, b"v2 high body\n", author),
        ],
    )
    with SessionLocal() as s:
        slot = (
            s.execute(
                select(Article).where(
                    Article.patch_series_key == key,
                    Article.patch_series_version == "v2",
                    Article.patch_series_position == 0,
                )
            )
            .scalars()
            .all()
        )
        assert len(slot) == 2, slot  # precondition: two rows, one slot
        assert not any(a.files for a in slot)
        low, high = sorted(a.id for a in slot)
    return key, low, high


def _give_a_diff(article_id):
    with SessionLocal() as s:
        s.add(ArticleFile(article_id=article_id, path="fs/foo.c"))
        s.commit()


def test_series_diff_serves_the_lowest_id_row_with_a_diff(client, tmp_path):
    """#861: with no diff in the slot the lowest id is the patch; a row
    gaining one takes over, whatever the ids. Primed with a normal
    request, so a cache key that missed the diff would serve the old
    body."""
    key, low, high = _two_row_slot(tmp_path)
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover"
    first = client.get(url).get_data(as_text=True)
    assert "v2 low body" in first and "v2 high body" not in first
    _give_a_diff(high)
    second = client.get(url).get_data(as_text=True)
    assert "v2 high body" in second and "v2 low body" not in second
    _give_a_diff(low)  # both have one: the lowest id again
    third = client.get(url).get_data(as_text=True)
    assert "v2 low body" in third and "v2 high body" not in third


def test_series_diff_ignores_a_re_post_that_cannot_win(client, tmp_path, monkeypatch):
    """#861: the cache key is the winner ids, so a row joining the slot
    that loses it computes nothing again."""
    from mimir.web.routes import series_diff as route

    calls = []
    real = route.compute_revision_diff

    def counting(*args, **kwargs):
        calls.append(kwargs["to_article_id"])
        return real(*args, **kwargs)

    monkeypatch.setattr(route, "compute_revision_diff", counting)
    key, low, _high = _two_row_slot(tmp_path)
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover"
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert calls == [low]  # precondition: the second request was a hit
    with SessionLocal() as s:
        s.add(
            Article(
                message_id="v2-cv-late@x",
                subject="[PATCH v2 0/1] series",
                author="Alice <a@example>",
                patch_series_key=key,
                patch_series_version="v2",
                patch_series_position=0,
            )
        )
        s.commit()
    assert client.get(url).status_code == 200
    assert calls == [low]


def test_series_diff_canonical_and_sitemap_name_the_same_slot_patch(client, tmp_path):
    """#861: the page's canonical and the sitemap entry are byte for
    byte one URL on a two-row slot, and both follow the row with a
    diff. The lower row's canonical inbox is alpha, the higher row,
    filed in beta only, nominates beta."""
    from tests.test_routes._helpers import _clear_sitemap_cache

    key, low, high = _two_row_slot(tmp_path)
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        beta.mirror_path = str(tmp_path)
        art = s.get(Article, high)
        (alpha_link,) = art.lists
        s.add(
            ArticleList(
                article_id=high,
                inbox_id=beta.id,
                epoch=alpha_link.epoch,
                commit_sha=alpha_link.commit_sha,
            )
        )
        s.flush()
        s.delete(alpha_link)
        art.canonical_inbox_id = beta.id
        s.commit()
    _give_a_diff(high)
    path = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover"
    # The page is served from alpha, where the winner is not filed:
    # read it through beta, which both rows' bodies are readable from.
    page = client.get(path.replace("/alpha/", "/beta/"))
    assert page.status_code == 200
    canonical = _canonical(page.get_data(as_text=True))
    _clear_sitemap_cache()
    xml = client.get(f"/sitemap-series-diffs-{key[0]}.xml").get_data(as_text=True)
    locs = [html_lib.unescape(m) for m in re.findall(r"<loc>([^<]+)</loc>", xml)]
    mine = [loc for loc in locs if f"/series/{key}/" in loc]
    assert mine == [canonical]
    assert "/beta/series/" in canonical  # the winner's inbox, not the low row's


@pytest.mark.parametrize("param", ["s", "pos"])
def test_series_diff_out_of_range_integer_is_a_404(client, tmp_path, param):
    """An integer SQLite cannot bind must 404 like any other bad value,
    not 500. `s` is new in #816; `pos` had the same gap before it."""
    key = _cross_list_series(tmp_path)
    big = str(2**63)
    q = {"s": f"pos=cover&s={big}", "pos": f"pos={big}"}[param]
    resp = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&{q}")
    assert resp.status_code == 404


def test_series_diff_labels_follow_a_version_rewritten_in_place(client, tmp_path):
    """`backfill-patch-series --reprocess` can rewrite a stored version
    in place (#816 PR 1 reads `[PATCHv2 0/9]` as v2 where it was v1).
    The slots keep their article ids, so a cache keyed by ids alone
    serves the diff labelled with the old version for 24 hours."""
    key = _cross_list_series(tmp_path)
    first = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover")
    assert first.status_code == 200
    assert "v1→v2 diff" in first.get_data(as_text=True)  # precondition: labelled v1
    with SessionLocal() as s:
        v1 = s.execute(
            select(Article).where(Article.message_id == "v1-cv@x")
        ).scalar_one()
        v1.patch_series_version = "v3"
        s.commit()
    resp = client.get(f"/alpha/series/{key}/diff?from=v3&to=v2&pos=cover")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "v3→v2 diff" in html, "served the cached diff labelled with the old version"


def test_series_diff_title_and_description_name_the_series(client, tmp_path):
    """#846: every diff page had the same generic title, so a sitemap of
    them would publish ~194k duplicate titles. The series subject, without
    its `[PATCH v2 0/3]` bracket, plus the versions, tells them apart."""
    author = "Alice <a@example>"
    key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[
            ("c1@x", "[PATCH 0/2] improve foo handling", None, b"a\n", author)
        ],
        v2_messages=[
            ("c2@x", "[PATCH v2 0/2] improve foo handling", None, b"b\n", author)
        ],
    )
    out = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover").data.decode()
    title = html_lib.unescape(re.search(r"<title>([^<]*)</title>", out).group(1))
    desc = html_lib.unescape(
        re.search(r'<meta name="description" content="([^"]*)"', out).group(1)
    )
    assert title.startswith("improve foo handling: v1→v2 diff (cover letter)"), title
    assert "[PATCH" not in title
    assert "improve foo handling" in desc
    assert "v1" in desc and "v2" in desc


def test_series_diff_heading_escapes_the_subject(client, tmp_path):
    """#880: the heading is built in Python, so the template must still
    escape it. A raw `<b>` in the H1 or title is markup from a mail header."""
    author = "Alice <a@example>"
    key = _ingest_series_pair(
        tmp_path,
        "alpha",
        v1_messages=[("c1@x", "[PATCH 0/2] fix a & <b>", None, b"a\n", author)],
        v2_messages=[("c2@x", "[PATCH v2 0/2] fix a & <b>", None, b"b\n", author)],
    )
    out = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover").data.decode()
    h1 = re.search(r"<h1>(.*?)</h1>", out, re.DOTALL).group(1)
    title = re.search(r"<title>(.*?)</title>", out, re.DOTALL).group(1)
    assert h1 == "fix a &amp; &lt;b&gt;: v1→v2 diff (cover letter)", h1
    assert title.startswith(h1 + " | "), (h1, title)


# --- Message-page ETag / conditional revalidation -----------------------------
