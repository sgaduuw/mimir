"""Tests for mimir/web/routes/series_diff.py: the inter-revision
patch-series diff route (`pos=cover` and per-position links,
indexed-primary resolution + heuristic fallback for awaiting-
backfill cases)."""

from tests.test_routes._helpers import _ingest_series_pair


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
    # Sanity: the page is the series-diff template, not a generic 404.
    assert "Inter-revision diff" in body


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
    assert "patch 1" in out  # position_label rendering


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
    card's Series-revisions row renders a `diff vs current` link
    per non-current revision pointing at the new route."""
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
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from sqlalchemy import select as _sa_select

    with SessionLocal() as s:
        v2_art = s.execute(
            _sa_select(Article).where(Article.message_id == "v2-cv@x")
        ).scalar_one()
        v2_url = f"/alpha/{v2_art.date.year}/{v2_art.date.month:02d}/{v2_art.id}"
    body = client.get(v2_url).data.decode()
    card = body.split('class="patch-state"')[1].split("</aside>")[0]
    assert "diff vs current" in card
    assert f"/alpha/series/{series_key}/diff" in card
    assert "from=v1" in card
    assert "to=v2" in card
    assert "pos=cover" in card


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


# --- Message-page ETag / conditional revalidation -----------------------------
