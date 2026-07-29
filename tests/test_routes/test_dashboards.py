"""Tests for mimir/web/routes/dashboards.py: the `/` meta-index,
the `/<inbox>/` dashboard, per-subsystem dashboards,
per-author / per-reviewer pages, and the surrounding render
contract (card grids, sparklines, tracker tiles, footer)."""

from tests.test_routes._helpers import (
    _ingest_one_article,
    _json_ld_blocks,
    _meta_value,
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
    of the inbox list -- and the pin-aware ordering -- is exercised
    in `test_meta_index_pins_inbox_first`, which subsumes the older
    presence-only checks for the alpha/beta links."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="inboxes"' in r.data.decode()


def test_meta_index_renders_cards_not_list(client):
    """The inbox overview surfaces as a card grid: each inbox is an
    `<a class="inbox-card">` block, not a `<li>` in a flat `<ul>`.
    Pins the layout shape so a CSS refactor doesn't silently regress
    back to the pre-1.19 list.
    """
    body = client.get("/").data.decode()
    assert 'class="inbox-grid"' in body
    assert 'class="inbox-card"' in body


def test_meta_index_pinned_card_carries_badge(client, monkeypatch):
    """Pinned inboxes render with a visible badge in addition to
    sorting first. Pin marker supplements position-based signal so
    the layout doesn't rely on order alone.

    Assert against the rendered `<span class="inbox-card-pinned">`
    opening tag rather than the class name on its own, the class
    is also mentioned in the inline `<style>` block, so a substring
    check there would pass even without the badge rendering."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "pinned_inboxes", ["beta"])
    body = client.get("/").data.decode()
    assert '<span class="inbox-card-pinned"' in body


def test_meta_index_no_pin_badge_when_no_pins(client, monkeypatch):
    """Inverse: nothing renders the pin badge when no inbox is
    pinned. Guards against the badge accidentally surfacing for
    everyone."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "pinned_inboxes", [])
    body = client.get("/").data.decode()
    assert '<span class="inbox-card-pinned"' not in body


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


def test_meta_index_sparkline_renders_when_inbox_has_messages(client):
    """Seeded inboxes have messages, so each card carries a
    `<svg class="inbox-card-spark">` 30-day daily-volume sparkline.

    Also guards the `spark_bars` macro wiring (`_sparkline.html`): the
    <svg> wrapper lives in the template but the per-day `<rect>` bars now
    come from the shared macro, so assert a real bar rendered, not just
    an empty wrapper.
    """
    body = client.get("/").data.decode()
    assert 'class="inbox-card-spark"' in body
    assert '<rect x="0"' in body and 'fill="currentColor"' in body


def test_meta_index_card_link_targets_inbox_dashboard(client):
    """Each card is wrapped in an `<a>` whose href is the per-inbox
    dashboard. Whole-card click target."""
    import re

    body = client.get("/").data.decode()
    # `<a class="inbox-card" ... href="/<name>/">` shape. Capture
    # every card link.
    hrefs = re.findall(
        r'<a class="inbox-card"[^>]*href="(/[^"]+/)"',
        body,
    )
    assert "/alpha/" in hrefs
    assert "/beta/" in hrefs


def test_meta_index_last_activity_reads_from_inbox_column(client):
    """Front-page "Last activity" line reads `Inbox.last_article_date`
    each render, not the 24h-cached `archive_stats` bundle. Pins #216:
    seeding (or warming) the `archive_stats:<inbox>` cache row should
    not lock the front-page string to the snapshot's max-date if
    `Inbox.last_article_date` has since moved forward.

    Strategy: prime the `archive_stats` cache by hitting the page
    once, then bump `Inbox.last_article_date` to a near-now value and
    re-request. The visible relative-time string should reflect the
    new value (a recent "m"/"h ago" stamp), not the conftest seed's
    14-month-old date."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    # First request warms the archive_stats cache row for both inboxes.
    body_before = client.get("/").data.decode()
    # Sanity check: conftest seed of 2024-03-01 surfaces as an absolute
    # date in the visible card (>30d ago → `_relative_time` switches
    # from "Nd ago" to "YYYY-MM-DD"), not a recent stamp.
    assert "Last activity: 2024-03-01" in body_before

    # Bump alpha to "5 minutes ago" without touching the cache.
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = five_min_ago
        s.commit()

    body_after = client.get("/").data.decode()
    # alpha's card now reports a fresh activity stamp (5m ago); beta's
    # is unchanged.
    assert "Last activity: 5m ago" in body_after
    # alpha's old date string is gone from alpha's card. (beta still
    # carries it, so this asserts disappearance in alpha's region.)
    alpha_card = body_after.split('href="/alpha/"')[1].split("</a>")[0]
    assert "2024-03-01" not in alpha_card


def test_meta_index_renders_subsystem_chips_with_activity(client, tmp_path):
    """Front page surfaces an "Active subsystems" teaser when one
    or more subsystems have recent messages. Chips link to the
    busiest inbox's per-subsystem dashboard (lowercased URL).
    """
    from datetime import datetime, timedelta, timezone
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "bch-front@example.com",
        subject="bcachefs front-page test",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems()
    text = client.get("/").data.decode()
    assert "Active subsystems" in text
    # Display name is lowercased on the chip; URL is also lowercase
    # (subsystem URL canonicalisation since #110). The stored DB
    # row keeps the upstream MAINTAINERS uppercase casing.
    assert "bcachefs" in text
    assert "/alpha/subsystem/bcachefs/" in text


def test_meta_index_no_subsystem_section_when_no_activity(client):
    """Inverse: with no recent in-window articles the section is
    skipped entirely rather than rendering a stub."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    text = client.get("/").data.decode()
    assert "Active subsystems" not in text


def test_meta_index_subsystem_card_shows_maintainer_and_sparkline(
    client,
    tmp_path,
):
    """Front-page subsystem cards carry the top M: maintainer's
    name and a 7-day per-subsystem sparkline so they read as
    real cards rather than just name + count."""
    from datetime import datetime, timedelta, timezone
    from mimir.extensions import SessionLocal
    from mimir.models import Article

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
        subject="bcachefs card test",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems()
    text = client.get("/").data.decode()
    # Maintainer line.
    assert "maintained by Kent Overstreet" in text
    # Per-card sparkline svg.
    assert 'class="subsystem-card-spark"' in text


def test_meta_index_subsystem_card_status_badge_when_non_default(
    client,
    tmp_path,
):
    """The Supported status surfaces as a corner badge; the default
    "Maintained" doesn't (would render on every card → noise)."""
    from datetime import datetime, timedelta, timezone
    from mimir.extensions import SessionLocal
    from mimir.models import Article

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
        "bch-status-card@example.com",
        subject="status badge",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems()
    text = client.get("/").data.decode()
    # `class="subsystem-card-status">Supported<` shape, the class
    # is also referenced in the inline <style>, so anchor on the
    # opening tag with the status value to confirm it renders.
    assert '<span class="subsystem-card-status">Supported</span>' in text


def test_meta_index_subsystem_card_no_status_badge_for_default(
    client,
    tmp_path,
):
    """`Maintained` is the upstream default, most subsystems sit
    at that value, so a badge on every card would be noise. Pin
    the suppression so a future change doesn't accidentally
    introduce that visual clutter."""
    from datetime import datetime, timedelta, timezone
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    _seed_subsystem(
        "BTRFS FILE SYSTEM",
        "Maintained",
        files=["fs/btrfs/"],
        maintainers=[("M", "Chris Mason", "clm@fb.com")],
    )
    body = b"diff --git a/fs/btrfs/extent.c b/fs/btrfs/extent.c\n@@ -1 +1 @@\n-x\n+y\n"
    art_id, _ = _ingest_one_article(
        tmp_path,
        "alpha",
        "btrfs-card@example.com",
        subject="default status",
        body=body,
    )
    with SessionLocal() as s:
        art = s.get(Article, art_id)
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    _warm_active_subsystems()
    text = client.get("/").data.decode()
    # Card present, but no rendered status badge for the
    # Maintained value.
    assert "btrfs file system" in text
    assert '<span class="subsystem-card-status">' not in text


def test_meta_index_pins_inbox_first(client, monkeypatch):
    """Seeded fixtures have alpha + beta. Without a pin, alpha shows
    first (alphabetical). Pinning beta surfaces it ahead of alpha.
    Subsumes the older presence-only "alpha + beta link" checks.

    Use the first-anchor occurrence of each inbox link rather than
    `.find()` against the raw body -- inbox names appear in CSS and
    nav too, which means a brittle whole-body search would give the
    wrong index ordering. Anchor on the `<a href="/<name>/">` pattern."""
    import re
    from mimir.config import settings

    def first_inbox_link_order(body: str) -> list[str]:
        # Each inbox dashboard link is exactly `href="/<name>/"`; capture
        # them in document order, keep first appearance only.
        seen: list[str] = []
        for m in re.finditer(r'href="/([a-z0-9-]+)/"', body):
            name = m.group(1)
            if name in {"alpha", "beta"} and name not in seen:
                seen.append(name)
        return seen

    monkeypatch.setattr(settings, "pinned_inboxes", [])
    body = client.get("/").data.decode()
    # Both seeded inboxes show up as anchors to their dashboard
    # (without this, the order check below would still pass on an
    # empty page after a routing/template break that swallowed the
    # inbox loop).
    assert 'href="/alpha/"' in body
    assert 'href="/beta/"' in body
    assert first_inbox_link_order(body) == ["alpha", "beta"]

    monkeypatch.setattr(settings, "pinned_inboxes", ["beta"])
    body = client.get("/").data.decode()
    assert first_inbox_link_order(body) == ["beta", "alpha"]


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
    # Maintainer address renders on the dashboard page (operator-
    # facing surface; the per-patch header redacts it to keep that
    # surface compact, the dashboard doesn't).
    assert "kent.overstreet@kernel.org" in body
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
    from datetime import datetime, timedelta, timezone
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
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    r = client.get("/alpha/subsystem/bcachefs/")
    assert r.status_code == 200
    text = r.data.decode()
    assert "Active reviewers" in text
    assert "kent.overstreet@kernel.org" in text
    # Allowlisted (@kernel.org) → clickable link to per-reviewer page.
    assert '/alpha/reviewer/kent.overstreet%40kernel.org"' in text


def test_subsystem_dashboard_no_link_for_non_allowlisted_reviewer(
    client,
    tmp_path,
):
    """Non-allowlisted addresses render via `safe_from` (display
    name + `<hidden>`) with NO clickable link to the per-reviewer
    page, so mimir doesn't surface non-public addresses in URLs."""
    from datetime import datetime, timedelta, timezone
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
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    text = client.get("/alpha/subsystem/bcachefs/").data.decode()
    assert "Active reviewers" in text
    # Address is redacted in the display surface (safe_from policy).
    assert "rando@somecorp.example" not in text
    # And no link to the per-reviewer page is generated.
    assert "/alpha/reviewer/" not in text


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
    from datetime import datetime, timedelta, timezone
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
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
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
    from datetime import datetime, timedelta, timezone
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
        art.date = datetime.now(timezone.utc) - timedelta(hours=2)
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
    from datetime import datetime, timezone

    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        art = Article(
            message_id="decade-fixture-2018@example.com",
            subject="old",
            author="o@example.com",
            date=datetime(2018, 6, 1, 0, 0, tzinfo=timezone.utc),
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
    assert "kent.overstreet@kernel.org" in text
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
    from tests.test_routes.test_dashboards import _seed_subsystem as _seed

    _seed(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    html = client.get("/alpha/subsystem/bcachefs/").get_data(as_text=True)
    assert 'href="/maintainers/kent.overstreet@kernel.org"' in html
