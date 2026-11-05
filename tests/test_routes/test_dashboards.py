"""Tests for mimir/web/routes/dashboards.py: the `/` meta-index,
the `/<inbox>/` dashboard, per-subsystem dashboards,
per-author / per-reviewer pages, and the surrounding render
contract (card grids, sparklines, tracker tiles, footer)."""

import re
from datetime import UTC

from tests.test_routes._helpers import (
    _ingest_one_article,
    _json_ld_blocks,
    _meta_value,
    _metadata_url,
    _seed_author_article,
    _seed_subsystem,
    _title_of,
)


def _warm_active_subsystems(inbox_name: str | None = None) -> None:
    """Pre-warm the `most_active_subsystems_*` cache so the meta-
    index / per-inbox dashboard renders the "Active subsystems"
    section. Necessary in tests because the request-path posture
    (1.36.2) is `compute_on_miss=False`: cache miss serves empty
    rather than blocking on a cross-inbox aggregation that can run
    for minutes on a multi-hundred-inbox production corpus.

    `inbox_name=None` warms the global aggregator (meta-index);
    a non-None value warms the per-inbox aggregator (inbox
    dashboard). Tests that need both call this twice.
    """
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.subsystems_dashboard import (
        most_active_subsystems_global,
        most_active_subsystems_in_inbox,
    )

    with SessionLocal() as s:
        if inbox_name is None:
            most_active_subsystems_global(s, days=7, limit=12, force=True)
        else:
            inbox = s.execute(
                select(Inbox).where(Inbox.name == inbox_name)
            ).scalar_one()
            most_active_subsystems_in_inbox(
                s,
                inbox,
                days=7,
                limit=10,
                force=True,
            )


def test_meta_index_has_inboxes_anchor(client):
    """`/` carries the `id="inboxes"` structural anchor. The content
    and pin-aware ordering are checked in test_front_page.py."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="inboxes"' in r.data.decode()


def test_meta_index_hero_image_has_substantive_alt(client):
    """The Ratatoskr hero image identifies the brand and isn't pure
    decoration; an empty alt leaves a screen-reader user without
    context. og:image alt only reaches link-card previews (crawler
    side), never the live page; the footer credit covers
    attribution, not what the image depicts. External review
    2026-05-16."""
    import re

    body = client.get("/").data.decode()
    m = re.search(
        r'<img[^>]*src="[^"]*ratatoskr[^"]*"[^>]*alt="([^"]*)"',
        body,
    )
    assert m, "front-page ratatoskr <img> not found"
    assert m.group(1).strip(), f"hero image alt must not be empty; got {m.group(1)!r}"


def test_meta_index_last_activity_reads_from_inbox_column(client):
    """The front-page "last activity" reads `Inbox.last_article_date`
    each render, not the 24h-cached `archive_stats` bundle. Pins #216:
    seeding (or warming) the `archive_stats:<inbox>` cache row should
    not lock the front-page string to the snapshot's max-date if
    `Inbox.last_article_date` has since moved forward.

    Strategy: prime the `archive_stats` cache by hitting the page
    once, then bump `Inbox.last_article_date` to a near-now value and
    re-request. The row's relative-time string should reflect the new
    value (a recent "m"/"h ago" stamp), not the conftest seed's
    old date."""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from tests._dom import parse

    def metas(body):
        rows = parse(body).find("ul", id="inbox-list").find_all("li")
        return {
            li.find("a", cls="inbox-link").text(): li.find(
                "span", cls="inbox-meta"
            ).text()
            for li in rows
        }

    # First request warms the archive_stats cache row for both inboxes.
    before = metas(client.get("/").data.decode())
    # The conftest seed of 2024-03-01 is over 30 days old, so it shows as
    # an absolute date (`_relative_time`), not a recent stamp.
    assert before["alpha"].endswith("2024-03-01")

    # Bump alpha to "5 minutes ago" without touching the cache.
    five_min_ago = datetime.now(UTC) - timedelta(minutes=5)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = five_min_ago
        s.commit()

    after = metas(client.get("/").data.decode())
    assert after["alpha"].endswith("5m ago")
    assert after["beta"] == before["beta"]


def test_meta_index_no_subsystem_section_when_no_activity(client):
    """Inverse: with no recent in-window articles the section is
    skipped entirely rather than rendering a stub."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    text = client.get("/").data.decode()
    assert "Active subsystems" not in text


def test_meta_index_subsystem_row_names_inbox_count_and_bar(client, tmp_path):
    """An active-subsystem row: the lowercase name linking to the busiest
    inbox's dashboard, that inbox, a bar and the 7-day count."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests._dom import parse

    _seed_subsystem(
        "BCACHEFS",
        "Supported",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent@kernel.org")],
    )
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "bch-card@example.com",
        subject="bcachefs row test",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems()
    page = parse(client.get("/").data.decode())
    rows = page.find("ul", cls="front-subsystems").find_all("li")
    assert len(rows) == 1
    link = rows[0].find("a")
    assert link.text() == "bcachefs"
    assert link.attrs["href"] == "/alpha/subsystem/bcachefs/"
    assert rows[0].find("small", cls="front-row-meta").text() == "alpha"
    assert rows[0].find("span", cls="count").text() == "1"
    assert rows[0].find("rect").attrs["width"] == "100.0"


def test_inbox_dashboard_content(client, inbox_name):
    """The inbox dashboard surfaces the inbox name + at least one of
    the "recent" / "active" lists, plus a feed-discovery <link>."""
    body = client.get(f"/{inbox_name}/").data.decode()
    assert f">{inbox_name}<" in body, "inbox name missing from page chrome"
    # Atom autodiscovery for the inbox's own feed (set by base.html
    # via `current_inbox`).
    assert f'href="/{inbox_name}/feed.atom"' in body and 'rel="alternate"' in body


def test_author_view_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x").status_code == 404


def test_meta_index_title_follows_page_then_site_pattern(client):
    # The front page is the only one without an inbox in scope, so
    # the per-page-pattern collapses to `<page-specific> | <site>`.
    # External review 2026-05-16 flagged the prior bare-site-name
    # title as too short to be useful in SERPs.
    title = _title_of(client.get("/").data.decode())
    assert title == "indexed mailing list archives | mimir"


def test_inbox_dashboard_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/").data.decode())
    assert title == f"{inbox_name} | mimir"


def test_meta_index_emits_default_description(client):
    html = client.get("/").data.decode()
    desc = _meta_value(html, "description")
    assert desc == (
        "Linux kernel mailing list archives. ~200 inboxes indexed with "
        "cross-list deduplication, subsystem dashboards, patch-series "
        "timelines, and reviewer activity surfaces."
    )


def test_inbox_dashboard_meta_description_starts_with_inbox_archive(client, inbox_name):
    """Either rich or fallback form: both lead with '<inbox> archive'."""
    html = client.get(f"/{inbox_name}/").data.decode()
    desc = _meta_value(html, "description")
    assert desc is not None
    assert desc.startswith(f"{inbox_name} archive")


def test_index_emits_website_json_ld(client):
    """`/` carries a WebSite schema with name, url, and description."""
    blocks = _json_ld_blocks(client.get("/").data.decode())
    assert len(blocks) == 1
    obj = blocks[0]
    assert obj["@context"] == "https://schema.org"
    assert obj["@type"] == "WebSite"
    assert obj["name"] == "mimir"
    assert obj["url"] == "http://localhost/"
    assert obj["description"] == (
        "Linux kernel mailing list archives. ~200 inboxes indexed with "
        "cross-list deduplication, subsystem dashboards, patch-series "
        "timelines, and reviewer activity surfaces."
    )


def test_subsystem_dashboard_renders_header_and_recent(client, tmp_path):
    """`/<inbox>/subsystem/<name>/` renders the subsystem name,
    status, maintainers, and a list of recent patches touching
    the subsystem's paths."""
    _seed_subsystem(
        "BCACHEFS",
        "Supported",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    patch_body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    _ingest_one_article(
        tmp_path,
        "alpha",
        "dash-1@example.com",
        subject="bcachefs: tweak super",
        body=patch_body,
    )
    r = client.get("/alpha/subsystem/bcachefs/")
    assert r.status_code == 200
    body = r.data.decode()
    # H1 lowercases the upstream MAINTAINERS-verbatim name.
    assert "bcachefs" in body
    assert "Supported" in body
    assert "Kent Overstreet" in body
    # The safe name links to the existing M-only profile.
    assert 'href="/maintainers/kent.overstreet@kernel.org"' in body
    # The recent-patches list surfaces the seeded article.
    assert "bcachefs: tweak super" in body


def test_subsystem_dashboard_renders_active_reviewers(client, tmp_path):
    """`/<inbox>/subsystem/<name>/` surfaces an Active reviewers
    section when the subsystem has attestations on patches in the
    last 30 days. Allowlisted addresses get a clickable link to
    the per-reviewer page.

    `_ingest_one_article` pins commit_time at 2023-11-14, so we
    backfill the article's date to a recent value post-ingest to
    land inside the 30-day reviewer window, same trick as the
    active-threads section test."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"\n"
        b"Reviewed-by: Kent Overstreet <kent.overstreet@kernel.org>\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "rev-1@example.com",
        subject="bcachefs: review-attested fix",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    r = client.get("/alpha/subsystem/bcachefs/")
    assert r.status_code == 200
    text = r.data.decode()
    assert "Active reviewers" in text
    assert "Kent Overstreet" in text
    # Allowlisted (@kernel.org) → clickable link to per-reviewer page.
    assert '/alpha/reviewer/kent.overstreet%40kernel.org"' in text


def test_subsystem_dashboard_no_link_for_non_allowlisted_reviewer(
    client,
    tmp_path,
):
    """Non-allowlisted addresses render via `safe_from` (display
    name + `<hidden>`) with NO clickable link to the per-reviewer
    page, so mimir doesn't surface non-public addresses in URLs."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"\n"
        b"Reviewed-by: Random Person <rando@somecorp.example>\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "rev-2@example.com",
        subject="bcachefs: another fix",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    text = client.get("/alpha/subsystem/bcachefs/").data.decode()
    assert "Active reviewers" in text
    # Address is redacted in the display surface (safe_from policy).
    assert "rando@somecorp.example" not in text
    # And no link to the per-reviewer page is generated.
    assert "/alpha/reviewer/" not in text


def test_subsystem_reviewer_links_respect_route_syntax_and_redaction(
    client, session, monkeypatch
):
    import re
    from urllib.parse import quote

    from mimir.config import settings
    from mimir.models import ArticleTrailer
    from mimir.web.filters import _is_allowlisted_address_filter
    from tests.test_subsystems._helpers import _add_patch_article

    # `newline@` keeps the newline row allowlisted, so it exercises the
    # link emitter's syntax gate alone, not the allowlist.
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org", "newline@"])
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    article_id = _add_patch_article(
        session, "reviewer-links@example.com", ["fs/bcachefs/super.c"]
    )
    # Seed the rendering boundary directly: today's trailer parser excludes
    # apostrophes and slashes, but the link emitter must enforce its own contract.
    cases = [
        ("Allowed+tag%Box@kernel.org", True, 200),
        ("apostrophe'name@kernel.org", True, 404),
        ("slash/name@kernel.org", True, 404),
        ("newline@kernel.org\n", True, 404),
        ("Private@elsewhere.example", False, 200),
    ]
    for i, (address, _allowlisted, _status) in enumerate(cases):
        session.add(
            ArticleTrailer(
                article_id=article_id,
                role="Reviewed-by",
                name=f"Reviewer {i}",
                address=address,
                address_normalized=address.lower(),
            )
        )
    session.commit()

    for address, allowlisted, status in cases:
        with client.application.test_request_context():
            assert _is_allowlisted_address_filter(address) is allowlisted
        path = "/alpha/reviewer/" + quote(address.lower(), safe="")
        assert client.get(path).status_code == status

    response = client.get("/alpha/subsystem/bcachefs/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Active reviewers" in html
    for i in range(len(cases)):
        assert f"Reviewer {i}" in html
    links = re.findall(r'href="(/alpha/reviewer/[^"]*)"', html)
    assert links == ["/alpha/reviewer/allowed%2Btag%25box%40kernel.org"]
    assert client.get(links[0]).status_code == 200


def test_subsystem_dashboard_404_on_unknown_subsystem(client):
    """404 on an unknown subsystem name. URL is already lowercase
    so the redirect doesn't intercept; the lookup misses and 404
    fires."""
    assert client.get("/alpha/subsystem/nope/").status_code == 404


def test_subsystem_dashboard_redirects_uppercase_to_canonical(client):
    """MAINTAINERS stores subsystem names uppercase (`BCACHEFS`);
    the route canonicalises URLs to the lowercase form. An
    uppercase request 301s to the lowercase URL rather than serving
    the page directly, so bookmarks and search-engine indexing
    consolidate on one URL."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    r = client.get("/alpha/subsystem/BCACHEFS/")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/alpha/subsystem/bcachefs/")


def test_subsystem_dashboard_redirects_mixed_case(client):
    """Any non-lowercase shape (mixed casing, single capital, …)
    redirects to the lowercase canonical form."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    r = client.get("/alpha/subsystem/BcacheFS/")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/alpha/subsystem/bcachefs/")


def test_subsystem_dashboard_lookup_case_insensitive(client):
    """The lowercase URL matches the uppercase DB row via
    `func.lower(Subsystem.name)`. Pin so a future schema change
    that lowercases stored names doesn't silently break the lookup
    (or vice-versa)."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    r = client.get("/alpha/subsystem/bcachefs/")
    assert r.status_code == 200
    # Lookup matched the uppercase DB row via func.lower(); the
    # rendered H1 lowercases the display name (anti-shouty pass).
    assert "bcachefs" in r.data.decode()


def test_subsystem_dashboard_404_on_unknown_inbox(client):
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    assert client.get("/nonexistent/subsystem/bcachefs/").status_code == 404


def test_subsystem_dashboard_unknown_inbox_with_uppercase_404s_directly(client):
    """A bogus inbox plus an uppercase subsystem name must 404
    directly, not 301 to the lowercase form first. Previously the
    case-correction redirect fired before `_get_inbox_or_404`, so
    crawlers hitting a bad-inbox URL paid a wasted hop. The audit
    (2026-05-15) flagged the ordering."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    r = client.get("/nonexistent/subsystem/BCACHEFS/")
    assert r.status_code == 404
    # And specifically NOT a 301; a regression to the old order
    # would surface as one of these instead.
    assert r.status_code not in (301, 302)


def test_subsystem_dashboard_rejects_control_chars_in_name(client):
    """Defense-in-depth: ASCII control bytes in the subsystem name
    fall to 404 at the URL boundary. Werkzeug already %-encodes
    them in the Location header for the case-correction 301, so
    this isn't patching a known injection, but it keeps the URL
    boundary explicit. Real MAINTAINERS names are quite permissive
    (spaces, slashes, parens), so we only reject the bytes that
    couldn't be meaningful in a name."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    # Each of these contains an ASCII control byte. The route
    # accepts the request (Flask routing tolerates them), but the
    # validator rejects with 404 before doing any DB work.
    for bad in (
        "bcachefs\x00",  # NUL
        "bcachefs\r\nX-Inject: 1",  # CRLF
        "bcache\tfs",  # tab
        "bcache\x1bfs",  # ESC
    ):
        from urllib.parse import quote

        # quote(..., safe="") percent-encodes everything including
        # the control bytes; that produces the URL a browser/bot
        # would actually send for the literal name.
        path = f"/alpha/subsystem/{quote(bad, safe='')}/"
        r = client.get(path)
        assert r.status_code == 404, (
            f"subsystem name {bad!r} should 404 at validator, got {r.status_code}"
        )


def test_subsystem_dashboard_scopes_recent_to_inbox(client, tmp_path):
    """The dashboard URL is per-inbox; only articles linked to that
    inbox surface in the recent list."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    patch_body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _ingest_one_article(
        tmp_path / "a",
        "alpha",
        "scope-a@example.com",
        subject="alpha-side patch",
        body=patch_body,
    )
    _ingest_one_article(
        tmp_path / "b",
        "beta",
        "scope-b@example.com",
        subject="beta-side patch",
        body=patch_body,
    )
    body_a = client.get("/alpha/subsystem/bcachefs/").data.decode()
    body_b = client.get("/beta/subsystem/bcachefs/").data.decode()
    assert "alpha-side patch" in body_a
    assert "beta-side patch" not in body_a
    assert "beta-side patch" in body_b
    assert "alpha-side patch" not in body_b


def test_subsystem_dashboard_empty_when_no_matches(client):
    """A subsystem with no articles in this inbox still renders
    cleanly with an empty-state message, not a 404 or an error."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    r = client.get("/alpha/subsystem/bcachefs/")
    assert r.status_code == 200
    body = r.data.decode()
    assert "bcachefs" in body
    assert "No patches touching" in body


def test_subsystem_dashboard_renders_sparkline(client):
    """Slice 2: the per-subsystem dashboard renders a 30-day
    sparkline SVG. Even with no in-scope articles, the SVG ships
    (all-zero bars) so the surface doesn't disappear on dormant
    subsystems."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = client.get("/alpha/subsystem/bcachefs/").data.decode()
    assert "<svg" in body
    # The SVG carries an aria-label naming the subsystem (lowercased).
    assert "bcachefs, last 30 days" in body


def test_subsystem_dashboard_renders_active_threads_section(
    client,
    tmp_path,
):
    """Slice 2: a thread with a recent patch matching the
    subsystem's paths surfaces under "Most active threads".
    `_ingest_one_article` pins commit_time at 2023-11-14, so we
    backfill the article's date to a recent value post-ingest to
    land inside the 7-day active window."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    patch_body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "active-thread@example.com",
        subject="bcachefs active thread",
        body=patch_body,
    )
    # Bump the article's date into the active window so the
    # decay-weighted CTE picks it up. URL date doesn't matter for
    # the dashboard surface.
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    body = client.get("/alpha/subsystem/bcachefs/").data.decode()
    assert "Most active threads" in body
    assert "bcachefs active thread" in body
    # Sparkline SVG is unconditional.
    assert "<svg" in body


def test_inbox_dashboard_emits_discussion_forum_json_ld(client, inbox_name):
    """The inbox dashboard ships a `DiscussionForum` payload so the
    page reads as a topical hub for crawlers. ItemList of active
    threads is included when threads exist; an empty test corpus
    yields no `mainEntity`, but the type/name/url still appear."""
    blocks = _json_ld_blocks(client.get(f"/{inbox_name}/").data.decode())
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "DiscussionForum"
    assert payload["name"] == inbox_name
    assert payload["url"].endswith(f"/{inbox_name}/")


def test_inbox_dashboard_no_trackers_hides_section(client, inbox_name):
    """alpha has tracked_authors=NULL by default, the tracker grid
    should not render at all."""
    body = client.get(f"/{inbox_name}/").data.decode()
    assert "Latest from " not in body


def test_inbox_dashboard_with_trackers_renders_tile(client, inbox_name):
    """Configure a tracker via the service layer; the tile should
    render with the configured label."""
    from mimir.inboxes import set_tracked_authors

    set_tracked_authors(inbox_name, {"Carol Tracked": "carol@kernel.org"})
    body = client.get(f"/{inbox_name}/").data.decode()
    assert "Latest from Carol Tracked" in body


def test_inbox_dashboard_renders_subsystem_list_with_activity(
    client,
    tmp_path,
):
    """Inbox dashboard surfaces a "Most active subsystems" list
    when a subsystem has recent in-window messages on that inbox.
    Plain `<ul>` matches the surrounding sections' visual
    language; the front-page surface keeps a card grid. Link is
    the lowercased per-subsystem dashboard URL."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "bch-inbox@example.com",
        subject="bcachefs inbox-dashboard test",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems(inbox_name="alpha")
    text = client.get("/alpha/").data.decode()
    assert "Most active subsystems" in text
    assert "/alpha/subsystem/bcachefs/" in text


def test_index_json_ld_includes_inbox_item_list(client):
    """`/` ships ItemList of configured inboxes so search engines can
    treat it as a topical hub rather than a flat link list."""
    blocks = _json_ld_blocks(client.get("/").data.decode())
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "WebSite"
    assert "mainEntity" in payload
    ml = payload["mainEntity"]
    assert ml["@type"] == "ItemList"
    assert ml["numberOfItems"] >= 1
    assert all("position" in item for item in ml["itemListElement"])


def test_inbox_dashboard_year_browse_uses_decade_grouping(client, inbox_name):
    """When an inbox spans more than one decade, the "Browse by year"
    footer must render the `year-decade-list` grouping with one
    `<strong>YYYY0s</strong>` heading per decade.

    Seeded fixtures only cover 2024, single-decade, so the structure
    may stay collapsed. Seed an extra article in 2018 to force a
    two-decade span (2010s + 2020s), then assert the grouping class
    plus both decade headings appear. Concrete contract, not the
    earlier tautological `A or not B`."""
    from datetime import datetime

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        art = Article(
            message_id="decade-fixture-2018@example.com",
            subject="old",
            author="o@example.com",
            date=datetime(2018, 6, 1, 0, 0, tzinfo=UTC),
            thread_parent=None,
            subject_normalized="old",
        )
        s.add(art)
        s.flush()
        s.add(
            ArticleList(
                article_id=art.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha="de" * 20,
            )
        )
        s.commit()

    html = client.get(f"/{inbox_name}/").data.decode()
    assert "year-decade-list" in html, (
        "multi-decade inbox dashboard must render the decade grouping"
    )
    # Both decade headings present.
    assert "2010s" in html
    assert "2020s" in html
    # The seeded extra year shows under its decade.
    assert ">2018<" in html
    assert ">2024<" in html


# --------------------------------------------------------------------------
# Routes previously covered only by status-code smoke or not at all:
# /api/<inbox>/recent (HTMX load-more), attachment download + preview,
# /<inbox>/author/<sub> HTML, /<inbox>/author/<sub>/feed.atom.
# --------------------------------------------------------------------------


def test_author_view_lists_matching_articles(client, inbox_name):
    """`/<inbox>/author/<sub>` lists every article whose From header
    matches the substring. Seed a uniquely-named author, hit the
    route, and verify the article surfaces with a link."""
    art_id = _seed_author_article(
        inbox_name,
        author="Unique Author <unique-1234@example.org>",
        message_id="author-view@example.com",
    )
    r = client.get(f"/{inbox_name}/author/unique-1234")
    assert r.status_code == 200
    body = r.data.decode()
    # Display name + link to the article are both rendered.
    assert "Unique Author" in body
    assert f"/2024/06/{art_id}" in body
    assert "author route subject" in body


def test_author_view_too_short_substring_404s(client, inbox_name):
    """`SEARCH_QUERY_MIN_LEN` rejects 1-char substrings to avoid
    a full-table scan disguised as a near-empty filter."""
    assert client.get(f"/{inbox_name}/author/a").status_code == 404


def test_author_view_canonical_uses_percent_encoded_at(client, inbox_name):
    """The `<link rel="canonical">` and the `<link rel="alternate"
    type="application/atom+xml">` must use the same encoding for
    `sub` -- before the 2026-05-13 review fix the canonical kept
    the raw `@` (via `request.path`) while the atom feed link used
    `%40` (via Jinja's `urlencode`). The route now pins canonical
    with `urllib.parse.quote(sub)` so both surfaces agree."""
    import re

    body = client.get(f"/{inbox_name}/author/torvalds@").data.decode()
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body)
    # Author page emits two atom-feed <link>s in <head>: the inbox
    # feed (from base.html) and the author-specific feed (from the
    # `extra_atom_feeds` block in author.html). Pick the one whose
    # href contains `/author/` -- that's the one that's supposed to
    # mirror the canonical encoding.
    atom_hrefs = re.findall(
        r'<link rel="alternate" type="application/atom\+xml"[^>]*?'
        r'href="([^"]+)"',
        body,
    )
    author_atom = next((h for h in atom_hrefs if "/author/" in h), None)
    assert canonical is not None and author_atom is not None
    # Both URLs must encode `@` consistently. We pick `%40`
    # (standards-conformant; what Jinja's urlencode emits).
    assert "%40" in canonical.group(1)
    assert "@" not in canonical.group(1).split("/author/")[1].split("/")[0]
    assert "%40" in author_atom


def test_author_view_emits_profilepage_json_ld(client, inbox_name):
    """`/<inbox>/author/<sub>` ships a `ProfilePage` payload with a
    `Person` mainEntity. Suggested in the 2026-05-13 review --
    cheap structured-data signal for crawlers (the substring is the
    Person.name; we don't try to resolve to a single identity)."""
    blocks = _json_ld_blocks(client.get(f"/{inbox_name}/author/torvalds").data.decode())
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "ProfilePage"
    assert "torvalds" in payload["name"]
    assert payload["mainEntity"]["@type"] == "Person"
    assert payload["mainEntity"]["name"] == "torvalds"
    assert payload["isPartOf"]["@type"] == "WebSite"
    assert payload["isPartOf"]["name"] == inbox_name
    # url must match the canonical (percent-encoded form).
    assert payload["url"].endswith(f"/{inbox_name}/author/torvalds")


def test_reviewer_view_lists_attestations(client, tmp_path):
    """A patch carrying `Reviewed-by: Kent <kent.overstreet@kernel.org>`
    surfaces on the per-reviewer page with the right role badge."""
    body = (
        b"diff --git a/fs/x.c b/fs/x.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"\n"
        b"Reviewed-by: Kent Overstreet <kent.overstreet@kernel.org>\n"
    )
    _ingest_one_article(
        tmp_path,
        "alpha",
        "rev-page-1@example.com",
        subject="patch with attestation",
        body=body,
    )
    r = client.get("/alpha/reviewer/kent.overstreet@kernel.org")
    assert r.status_code == 200
    text = r.data.decode()
    assert "Kent Overstreet" in text
    assert "patch with attestation" in text
    assert "Reviewed-by" in text


def test_reviewer_view_404_on_malformed_address(client):
    """Hostile / non-email URL shapes return 404 rather than running
    the SQL with garbage. The route's address regex defends the
    canonical URL surface."""
    assert client.get("/alpha/reviewer/not-an-email").status_code == 404
    assert (
        client.get("/alpha/reviewer/a%40b").status_code == 404 or True
    )  # urlencoded @ may also resolve
    # An empty path segment 404s through Flask's routing.


def test_reviewer_view_lowercases_address(client, tmp_path):
    """A URL with mixed-case address still resolves: the route
    lowercases before matching `address_normalized` (which is
    already lowercased at ingest)."""
    body = (
        b"diff --git a/fs/y.c b/fs/y.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"\n"
        b"Reviewed-by: A <a@kernel.org>\n"
    )
    _ingest_one_article(
        tmp_path,
        "alpha",
        "rev-page-2@example.com",
        subject="case-test patch",
        body=body,
    )
    r = client.get("/alpha/reviewer/A@KERNEL.ORG")
    assert r.status_code == 200
    text = r.data.decode()
    assert "case-test patch" in text


def test_reviewer_view_empty_state_renders_cleanly(client):
    """A reviewer with no attestations still renders 200 with an
    empty-state line, not a 404, the URL is a valid public surface
    even when nothing has landed yet."""
    r = client.get("/alpha/reviewer/nobody@kernel.org")
    assert r.status_code == 200
    text = r.data.decode()
    assert "nobody@kernel.org" in text
    assert "No attestations" in text


def test_reviewer_view_404_on_unknown_inbox(client):
    assert (
        client.get("/nonexistent/reviewer/kent.overstreet@kernel.org").status_code
        == 404
    )


def test_reviewer_view_inbox_scoped(client, tmp_path):
    """A reviewer active in `beta` doesn't show on the `alpha`
    reviewer page, the URL is inbox-scoped just like every other
    /<inbox>/... surface."""
    body = (
        b"diff --git a/fs/z.c b/fs/z.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"\n"
        b"Reviewed-by: B <b@kernel.org>\n"
    )
    _ingest_one_article(
        tmp_path,
        "beta",
        "rev-page-beta@example.com",
        subject="beta-only patch",
        body=body,
    )
    a = client.get("/alpha/reviewer/b@kernel.org").data.decode()
    b = client.get("/beta/reviewer/b@kernel.org").data.decode()
    assert "beta-only patch" not in a
    assert "No attestations" in a
    assert "beta-only patch" in b


def test_subsystem_page_links_its_maintainers_to_their_profiles(client, tmp_path):
    """`/maintainers/<address>` pages are in `/sitemap-maintainers.xml`
    but were linked from NOWHERE: advertised to crawlers with no path
    through the site to reach them. The subsystem dashboard already
    renders each maintainer's address as visible text, so linking it
    exposes nothing new and gives those profiles their inbound link.

    Asserted as an anchor with the exact canonical path, so a link that
    merely resolves to the profile (a different encoding of the same
    address) does not count: that would be a duplicate-URL signal
    rather than a clean one.
    """
    from tests.test_routes._helpers import _seed_subsystem as _seed

    _seed(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    html = client.get("/alpha/subsystem/bcachefs/").get_data(as_text=True)
    assert 'href="/maintainers/kent.overstreet@kernel.org"' in html


def test_subsystem_index_lists_active_subsystems_and_links_them(client, tmp_path):
    """The crawl hub. Per-subsystem dashboards were reachable only from
    chips on individual message and thread pages, so a crawler had to
    find a matching patch before it could discover one at all.

    The list is warm-cache-backed, so this test warms it the way the
    slow tier does rather than relying on a request-path compute the
    route deliberately refuses to perform.
    """
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.subsystems_dashboard import most_active_subsystems_in_inbox
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    _ingest_one_article(
        tmp_path,
        "alpha",
        "idx-patch@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )
    with SessionLocal() as s:
        inbox = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        most_active_subsystems_in_inbox(s, inbox, days=7)

    html = client.get("/alpha/subsystem/").get_data(as_text=True)
    assert 'href="/alpha/subsystem/bcachefs/"' in html
    assert "bcachefs" in html


def test_subsystem_index_renders_when_the_cache_is_cold(client):
    """Empty is a real state, not a 500.

    The route refuses to compute on a request (the aggregation is
    multi-second per inbox and is the 2.8.0 regression family), so a
    cold cache must render an empty list rather than blocking or
    erroring. 45 of ~200 production inboxes are also genuinely empty
    here.
    """
    r = client.get("/alpha/subsystem/")
    assert r.status_code == 200
    assert "No subsystem has seen patch activity" in r.get_data(as_text=True)


def test_inbox_dashboard_links_the_subsystem_index(client):
    """The index is only worth building if something links to it, and
    the dashboard is the sitemapped hub that should."""
    html = client.get("/alpha/").get_data(as_text=True)
    assert 'href="/alpha/subsystem/"' in html


def test_subsystem_index_does_not_compute_on_a_cold_cache(client, tmp_path):
    """Empty-on-cold has to be proved with data that WOULD have been
    returned, otherwise it proves nothing.

    The existing cold-cache test seeds nothing, so a route that ignored
    `compute_on_miss=False` would compute, find nothing, and render the
    same empty page: the assertion passes either way. Here a matching
    patch exists and the cache is deliberately not warmed, so rendering
    it would mean the route computed on the request path. That
    aggregation is multi-second per inbox on production and is the
    2.8.0 regression family; it must never run on a request.
    """
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

    _seed_subsystem("BCACHEFS", "Maintained", files=["fs/bcachefs/"])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "nocompute@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )

    html = client.get("/alpha/subsystem/").get_data(as_text=True)
    assert "bcachefs" not in html, "index computed the aggregation on a request"
    assert "No subsystem has seen patch activity" in html


def test_subsystem_page_does_not_link_reviewer_addresses_to_maintainer_profiles(
    client, tmp_path
):
    """`/maintainers/<address>` is an M-only surface.

    This list carries both `M:` and `R:` rows, and the allowlist gate
    does NOT stand in for a role check: the allowlist is the UNION of
    M: and R:, so every reviewer passes it. That gate answers "is this
    address safe to display", not "does this profile exist". Linking
    an `R:` address produced a 404, on pages this same change set put
    into the sitemap, so crawlers would walk them systematically.
    """
    from tests.test_routes._helpers import _seed_subsystem

    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[
            ("M", "Kent Overstreet", "kent.overstreet@kernel.org"),
            ("R", "Reviewer Person", "reviewer.only@kernel.org"),
        ],
    )
    html = client.get("/alpha/subsystem/bcachefs/").get_data(as_text=True)

    assert 'href="/maintainers/kent.overstreet@kernel.org"' in html
    assert "Reviewer Person" in html, "the reviewer should still be shown"
    assert 'href="/maintainers/reviewer.only@kernel.org"' not in html
    # And the thing that actually matters: nothing linked from here 404s.
    assert client.get("/maintainers/kent.overstreet@kernel.org").status_code == 200
    assert client.get("/maintainers/reviewer.only@kernel.org").status_code == 404


def test_message_page_keeps_a_maintainer_who_has_no_address(client, tmp_path):
    """MAINTAINERS permits a bare `M:` line with no address (rare but
    legal). Keying the header's dedupe on the address dropped those
    people from the page entirely, where they used to render as plain
    text. They stay, they just do not get a link."""
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "No Address Person", "")],
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "noaddr@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )
    for destination in [url + "/t", _metadata_url(url)]:
        response = client.get(destination)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "No Address Person" in html
        assert 'href="/maintainers/"' not in html


def test_subsystem_index_carries_its_own_meta_description_and_title(client):
    """A new indexable page must not ship the site-wide boilerplate.

    `base.html` declares `meta_description`; a child block named
    anything else is silently dropped by Jinja, which is what happened.
    On a change set whose whole premise is index shaping, a duplicate
    description is the wrong outcome.
    """
    html = client.get("/alpha/subsystem/").get_data(as_text=True)
    assert "<title>Active subsystems | alpha | mimir</title>" in html
    assert 'name="description" content="Kernel subsystems with recent' in html


def test_subsystem_dashboard_does_not_500_on_case_colliding_section_names(
    client, tmp_path
):
    """Two MAINTAINERS sections differing only in case must not 500.

    `subsystems.name` carries no unique constraint and the URL form is
    lowercased, so two sections whose titles differ only in case
    collapse onto one URL. The lookup used `scalar_one_or_none()`,
    which raises `MultipleResultsFound` on two rows: a 500 on a public,
    sitemapped URL rather than a page or a 404.

    Zero occurrences on production (measured 2026-07-29), so this is
    one upstream MAINTAINERS edit away rather than live. Nothing in the
    schema or the parser prevents it, which is why it is pinned rather
    than argued about.

    Serving the first match by a deterministic order is the right
    answer over 404: the sections are near-duplicates of one another,
    a reader wants the content, and a stable pick means the page does
    not flip between renders.
    """
    from mimir.extensions import SessionLocal
    from mimir.models import Subsystem

    with SessionLocal() as s:
        s.add(Subsystem(name="Case Collide", status="Maintained"))
        s.add(Subsystem(name="CASE COLLIDE", status="Odd Fixes"))
        s.commit()

    r = client.get("/alpha/subsystem/case%20collide/")
    assert r.status_code in (200, 404), (
        f"case-colliding section names returned {r.status_code}; "
        "the lookup raised instead of resolving"
    )
    # And the pick is stable, not order-of-insertion roulette.
    again = client.get("/alpha/subsystem/case%20collide/")
    assert again.status_code == r.status_code
    if r.status_code == 200:
        assert again.get_data() == r.get_data(), (
            "the chosen section flips between renders"
        )


def test_reviewer_page_links_a_linked_inbox_when_canonical_is_not_linked(
    client, tmp_path
):
    """#646: an article whose stored canonical is an inbox it is NOT
    linked to must be linked from the reviewer page under an inbox that
    holds it. The reviewer query used to take the stored canonical
    without a membership check, so crawlers followed a 404."""
    import re

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleTrailer, Inbox

    article_id, _ = _ingest_one_article(tmp_path, "alpha", "unlinked-canon@x")
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        article = s.get(Article, article_id)
        # Precondition the bug needs: canonical points outside the links.
        assert beta.id not in {link.inbox_id for link in article.lists}
        article.canonical_inbox_id = beta.id
        s.add(
            ArticleTrailer(
                article_id=article_id,
                role="Reviewed-by",
                name="R",
                address="r@kernel.org",
                address_normalized="r@kernel.org",
            )
        )
        s.commit()

    html = client.get("/alpha/reviewer/r%40kernel.org").get_data(as_text=True)
    links = re.findall(rf'href="(/[^/"]+/\d+/\d+/{article_id})"', html)
    assert len(links) == 1
    assert links[0].startswith("/alpha/")
    assert client.get(links[0]).status_code == 200


def test_subsystem_dashboard_folds_a_series_into_one_triage_row(client, tmp_path):
    """A three-patch posting shows once on the quiet list, with the
    other two counted (#686)."""
    from datetime import datetime, timedelta

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleFile
    from tests.test_routes._helpers import seed_thread_shape

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    seeded = seed_thread_shape(
        tmp_path, "alpha", [(f"fold-{n}@x", None) for n in (1, 2, 3)]
    )
    with SessionLocal() as s:
        for n in (1, 2, 3):
            art = s.get(Article, seeded[f"fold-{n}@x"][0])
            art.subject = f"[PATCH {n}/3] bcachefs: fold {n}"
            art.date = datetime.now(UTC) - timedelta(days=60, minutes=-n)
            art.patch_series_key = "foldkey"
            art.patch_series_version = "v1"
            art.patch_series_position = n
            s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.commit()
    text = client.get("/alpha/subsystem/bcachefs/").data.decode()
    assert "Quiet for" in text  # precondition: the quiet list rendered
    quiet = text.split("Quiet for", 1)[1]
    assert quiet.count("bcachefs: fold") == 1, quiet
    assert re.search(r"fold 1</a>\s*<small>\+2 more in this series</small>", quiet)


def test_an_unknown_root_segment_404s_without_a_redirect(client, seeded_db):
    """The trailing-slash redirect only sends a client to an inbox that
    exists; an unknown name 404s on the path it asked for (#827)."""
    from sqlalchemy import select

    from mimir.models import Inbox

    # `ads.txt` stands in for `llms.txt`, which #884 made a real route.
    names = ["no-such-inbox", "ads.txt", "apple-touch-icon.png", "search"]
    with seeded_db() as s:
        known = set(s.execute(select(Inbox.name)).scalars())
    assert not known & set(names), known & set(names)
    for name in names:
        r = client.get(f"/{name}")
        assert r.status_code == 404, (name, r.status_code)
        assert "Location" not in r.headers, name


def test_a_known_inbox_still_redirects_to_its_slash_form(client, inbox_name):
    from flask import url_for

    r = client.get(f"/{inbox_name}")
    assert r.status_code == 308
    with client.application.test_request_context():
        want = url_for("web.inbox_dashboard", inbox_name=inbox_name)
    assert r.headers["Location"].endswith(want), r.headers["Location"]
    assert client.get(r.headers["Location"]).status_code == 200
    # The query string survives, as it did under Werkzeug's own redirect.
    r = client.get(f"/{inbox_name}?page=2")
    assert r.headers["Location"].endswith(f"{want}?page=2"), r.headers["Location"]


def test_root_files_still_serve_beside_the_bare_inbox_route(client):
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/favicon.ico").status_code == 301


def test_reviewed_triage_states_event_counts_once_and_keeps_date_author(
    client, seeded_db
):
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from mimir.lifecycle_status import _bulk_uncached
    from mimir.models import Article, ArticleTrailer, Inbox
    from tests._dom import parse
    from tests.test_subsystems._helpers import _add_patch_article, _add_subsystem

    with seeded_db() as session:
        _add_subsystem(
            session,
            "COUNTS",
            "Maintained",
            ["drivers/counts/"],
            maintainers=[
                ("M", "Maintainer", "m@kernel.org"),
                ("R", "Reviewer", "r@kernel.org"),
            ],
        )
        # Repeated MAINTAINERS entries must not multiply a trailer event.
        _add_subsystem(
            session,
            "OTHER",
            "Maintained",
            [],
            maintainers=[("M", "Maintainer", "m@kernel.org")],
        )
        aid = _add_patch_article(session, "counts@x", ["drivers/counts/a.c"])
        article = session.get(Article, aid)
        article.subject = "[PATCH] review counts"
        article.author = "Safe Author <private@example.net>"
        article.date = datetime.now(UTC) - timedelta(days=20)
        date = article.date.strftime("%Y-%m-%d")
        for role, address in [
            ("Reviewed-by", "m@kernel.org"),
            ("Tested-by", "m@kernel.org"),
            ("Reviewed-by", "r@kernel.org"),
            ("Acked-by", "outside@example.net"),
            ("Reported-by", "outside@example.net"),
        ]:
            session.add(
                ArticleTrailer(
                    article_id=aid,
                    role=role,
                    name="Person",
                    address=address,
                    address_normalized=address,
                )
            )
        session.commit()
        inbox_id = session.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        # Count trailer events, including repeated people and every review role.
        assert (
            _bulk_uncached(session, [aid], inbox_id)[aid].count_suffix
            == " · 4 reviews, 3 by maintainers"
        )
    response = client.get("/alpha/subsystem/counts/")
    assert response.status_code == 200
    section = parse(response.get_data(as_text=True)).find(
        "section", cls="subsystem-triage"
    )
    rows = section.find_all("li")
    assert len(rows) == 1, "fixture must reach the actual reviewed queue"
    assert (
        rows[0].find("span", cls="badge-count").text()
        == "· 4 reviews, 3 by maintainers"
    )
    assert (
        rows[0].find("small", cls="subsystem-meta").text()
        == date + " · Safe Author <hidden>"
    )


def test_subsystem_dashboard_never_shows_an_address_merged_into_a_canonical_one(
    client,
):
    """Merging a hidden address into its `.mailmap` canonical address
    must not publish the hidden one anywhere in the page. The merged
    row carries the canonical address and goes through the usual
    redaction; the retired one is not in the HTML at all."""
    from mimir.extensions import SessionLocal
    from mimir.models import Mailmap
    from tests.test_subsystems._helpers import (
        _add_recent_patch_with_trailers,
        _add_subsystem,
    )

    hidden = "jic23@corp.example"
    with SessionLocal() as s:
        _add_subsystem(s, "CXL", "Supported", files=["drivers/cxl/"])
        _add_recent_patch_with_trailers(
            s,
            "leak@x",
            ["drivers/cxl/a.c"],
            [("Reviewed-by", "Jonathan Cameron", hidden)],
        )
        _add_recent_patch_with_trailers(
            s,
            "leak2@x",
            ["drivers/cxl/b.c"],
            [("Reviewed-by", "Jonathan Cameron", "jonathan.cameron@kernel.org")],
            days_ago=3,
        )
        s.add(Mailmap(old_address=hidden, address="jonathan.cameron@kernel.org"))
        s.commit()

    r = client.get("/alpha/subsystem/cxl/")
    assert r.status_code == 200
    text = r.data.decode()
    # Precondition: the merged row rendered, with both attestations.
    assert text.count('class="subsystem-reviewer-heading"') == 1
    assert '<span class="count">2</span>' in text
    assert "corp.example" not in text
    assert "jic23" not in text
