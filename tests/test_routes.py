"""Status-code smoke tests for every public route.

Catches the routine "did I break the URL routing or break a template
unrelated to my actual change" regression with one cheap test per
endpoint. Runs against the seeded test DB (alpha + beta inboxes
with a few articles) set up by tests/conftest.py.
"""
import pytest


def _clear_sitemap_cache():
    """Sitemap routes cache their XML in the `cache` table; cross-test
    mutations (e.g. flipping `art3.canonical_inbox_id`) won't be
    visible until the cached rows expire. Tests that need a fresh
    render call this first."""
    from sqlalchemy import delete
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()


# Endpoints that don't depend on a configured inbox.
def test_meta_index_has_inboxes_anchor(client):
    """`/` carries the `id="inboxes"` structural anchor. The content
    of the inbox list -- and the pin-aware ordering -- is exercised
    in `test_meta_index_pins_inbox_first`, which subsumes the older
    presence-only checks for the alpha/beta links."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="inboxes"' in r.data.decode()


def test_footer_includes_mimir_version(client):
    """The footer surfaces the running package version so an operator
    can confirm the deployed image matches the expected tag. Pin via
    regex so a future re-word of the surrounding text still passes
    as long as the version number is rendered somewhere in the
    footer."""
    import re
    from mimir import __version__
    body = client.get("/").data.decode()
    # <footer>...<mimir-version>...</footer> -- whatever phrasing,
    # the version must appear in the footer.
    m = re.search(r"<footer.*?</footer>", body, re.DOTALL)
    assert m is not None, "page is missing <footer>"
    assert __version__ in m.group(0), (
        f"footer doesn't carry version {__version__!r}; got: {m.group(0)}"
    )


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


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_readyz(client):
    assert client.get("/readyz").status_code == 200


def test_unknown_inbox_404(client):
    assert client.get("/nonexistent/").status_code == 404
    assert client.get("/nonexistent/today").status_code == 404
    assert client.get("/nonexistent/2024/").status_code == 404
    assert client.get("/nonexistent/2024/05/").status_code == 404
    assert client.get("/nonexistent/feed.atom").status_code == 404
    assert client.get("/nonexistent/search?q=foo").status_code == 404


# Inbox-scoped routes: smoke (status 200 only) plus per-route content
# tests below. The smoke check catches wholesale breakage (routing,
# template parse error, ORM mismatch); the content tests catch
# regressions where the response shape is right but the actual data
# is wrong or missing.
@pytest.mark.parametrize("path", [
    "/",
    "/today",
    "/yesterday",
    "/2024/",
    "/2024/05/",
    "/search",
    "/search?q=Linux",
    "/feed.atom",
])
def test_inbox_scoped_routes_smoke_200(client, inbox_name, path):
    """Smoke only: route resolves, template renders, no 500."""
    assert client.get(f"/{inbox_name}{path}").status_code == 200


def test_inbox_dashboard_content(client, inbox_name):
    """The inbox dashboard surfaces the inbox name + at least one of
    the "recent" / "active" lists, plus a feed-discovery <link>."""
    body = client.get(f"/{inbox_name}/").data.decode()
    assert f">{inbox_name}<" in body, "inbox name missing from page chrome"
    # Atom autodiscovery for the inbox's own feed (set by base.html
    # via `current_inbox`).
    assert (
        f'href="/{inbox_name}/feed.atom"' in body
        and 'rel="alternate"' in body
    )


def test_inbox_today_route_renders_today_label(client, inbox_name):
    """`/today` heading must reference the current date (UTC). A
    template change that hardcoded a static label would pass the
    smoke status check but mislabel every daily view."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/today").data.decode()
    assert today in body, f"today's date {today!r} missing from /today body"


def test_inbox_yesterday_route_renders_yesterday_label(client, inbox_name):
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    body = client.get(f"/{inbox_name}/yesterday").data.decode()
    assert yesterday in body


def test_inbox_since_smoke_recent(client, inbox_name):
    """`/since/<recent-date>` resolves and renders without the cap
    notice; the seeded archive is older than 90d so the body just
    says 'no messages in this window' but the route still 200s."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{recent}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" not in body
    assert recent in body


def test_inbox_since_caps_window_with_notice(client, inbox_name):
    """A since-date older than 90 days clamps the window to the
    90-day floor and the template surfaces a notice so the operator
    sees why the window starts where it does."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    r = client.get(f"/{inbox_name}/since/{old}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Window capped" in body
    # The effective-since date (today - 90d) appears in the notice.
    floor = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    assert floor in body


def test_inbox_since_malformed_date_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/since/not-a-date").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-13-01").status_code == 404
    assert client.get(f"/{inbox_name}/since/2024-02-30").status_code == 404


def test_inbox_since_future_date_404(client, inbox_name):
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    assert client.get(f"/{inbox_name}/since/{future}").status_code == 404


def test_unknown_inbox_since_404(client):
    assert (
        client.get("/nonexistent/since/2024-01-01").status_code == 404
    )


def test_inbox_year_archive_renders_year_label(client, inbox_name):
    body = client.get(f"/{inbox_name}/2024/").data.decode()
    # Page title block lifts the year into <title>.
    assert "<title>2024 | " in body
    # The h2 heading carries the "archive · 2024" pattern (template-
    # stable across nested <small>/<em> wrappers around the year).
    assert "archive · 2024" in body


def test_inbox_month_archive_renders_month_label(client, inbox_name):
    body = client.get(f"/{inbox_name}/2024/05/").data.decode()
    # "May 2024" or similar — title block has at least the year + month
    # in some recognisable form. Pin "2024" + a month-name fragment.
    assert "2024" in body
    assert any(
        m in body for m in (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )
    ), "month label missing"


def test_inbox_search_form_renders_with_no_query(client, inbox_name):
    """`/search` (no q) renders the search input field; doesn't
    error or show stale results."""
    body = client.get(f"/{inbox_name}/search").data.decode()
    assert '<input' in body and 'name="q"' in body


def test_inbox_search_with_query_echoes_query(client, inbox_name):
    """The query string round-trips into the input's `value=` so the
    user can refine their search. A regression that lost the echo
    would pass the smoke status check."""
    body = client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    assert 'value="Linux"' in body


def test_html_open_tag_is_single_line_on_routes_without_data_attrs(
    client, inbox_name,
):
    """`<html lang="en">` renders as a clean single-line tag on every
    route that doesn't override the `html_data_attrs` block. The
    pre-fix shape left a stray indented `>` on its own line in
    view-source whenever the block was empty (most routes), flagged
    in the 2026-05-12 review."""
    for url in ("/", f"/{inbox_name}/", f"/{inbox_name}/search"):
        body = client.get(url).data.decode()
        # Locate the opening html tag and verify it closes on the
        # same line. A broken render leaves `<html lang="en"\n      >`.
        idx = body.index("<html lang=")
        same_line_close = body.index(">", idx)
        assert "\n" not in body[idx:same_line_close], (
            f"<html ...> on {url} spans multiple lines: "
            f"{body[idx:same_line_close + 1]!r}"
        )


def test_inbox_search_form_has_visible_submit_button(client, inbox_name):
    """Phone-thumb usability: enter-to-submit works on a hardware
    keyboard but isn't obvious on touch. A `<button type="submit">`
    inside the form fixes that. Flagged in the 2026-05-12 review."""
    import re
    body = client.get(f"/{inbox_name}/search").data.decode()
    # Slice down to the search form so a stray <button> elsewhere on
    # the page (none today, but defends future drift) doesn't satisfy
    # the assertion.
    form_match = re.search(r"<form[^>]*\baction=\"/[^\"]*search\"[^>]*>(.*?)</form>",
                           body, re.DOTALL)
    assert form_match is not None, "search form missing"
    form_html = form_match.group(1)
    assert re.search(r'<button[^>]+type="submit"', form_html), (
        "search form must carry a visible submit button "
        "(see 2026-05-12 review note on touch usability)"
    )


def test_search_and_author_pages_lead_with_h1(client, inbox_name):
    """Both content pages lead with `<h1>`, not `<h2>` -- a11y +
    SEO both want a single top-level heading per page (2026-05-13
    review nit). Pin both content pages so a future template tweak
    that drops the h1 back to h2 fails loudly."""
    import re
    for url in (f"/{inbox_name}/search", f"/{inbox_name}/author/torvalds"):
        body = client.get(url).data.decode()
        # Slice from <main> so the nav / footer don't satisfy the
        # assertion. Pico wraps content in `<main class="container">`.
        main_idx = body.index("<main")
        first_h = re.search(r"<(h[1-6])\b", body[main_idx:])
        assert first_h is not None, f"no heading on {url}"
        assert first_h.group(1) == "h1", (
            f"first heading on {url} is <{first_h.group(1)}>, "
            f"expected <h1> (2026-05-13 review)"
        )


def test_year_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/1990/").status_code == 404
    # _max_archive_year() = current_year + 1; pick something solidly above.
    assert client.get(f"/{inbox_name}/2999/").status_code == 404


def test_month_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/2024/0/").status_code == 404
    assert client.get(f"/{inbox_name}/2024/13/").status_code == 404


def test_message_url_four_tuple_identity_404s_on_mismatch(
    client, tmp_path,
):
    """`/<inbox>/<YYYY>/<MM>/<article_id>` is a 4-tuple identity:
    inbox + year + month + id must ALL match the article's storage
    or the route 404s. CONTEXT.md (URL scheme) is explicit — "URLs
    either resolve exactly or don't resolve at all" — but the
    in-range component tests only catch impossible values
    (year 1990, month 0/13). The mismatched-but-plausible case
    (right id, wrong year/month/inbox) is unpinned, and that's the
    case a regression introducing a 301-to-canonical helper would
    quietly break.

    Ingest a real article and then probe every mismatched corner."""
    art_id, real_url = _ingest_one_article(
        tmp_path, "alpha", "four-tuple-identity@example.com",
    )
    # Sanity: the correct URL resolves.
    assert client.get(real_url).status_code == 200

    # Parse the right components out so the wrong ones are nearby
    # (and definitely-different).
    parts = real_url.strip("/").split("/")
    # ['alpha', '2024', '01', '<id>']
    assert len(parts) == 4 and parts[0] == "alpha"
    real_year, real_month, real_id = parts[1], parts[2], parts[3]
    other_year = "2023" if real_year != "2023" else "2022"
    other_month = "07" if real_month != "07" else "08"
    assert "beta" != "alpha"  # sanity — beta exists in seed

    for wrong in (
        # Wrong year, right month + id.
        f"/alpha/{other_year}/{real_month}/{real_id}",
        # Wrong month, right year + id.
        f"/alpha/{real_year}/{other_month}/{real_id}",
        # Wrong inbox, right year + month + id (article isn't linked to beta).
        f"/beta/{real_year}/{real_month}/{real_id}",
    ):
        r = client.get(wrong, follow_redirects=False)
        assert r.status_code == 404, (
            f"expected 404 on mismatched URL {wrong!r}, got {r.status_code} "
            f"(location={r.headers.get('Location')!r})"
        )


def test_search_too_short(client, inbox_name):
    """Single-char query: server-side min-length check kicks in;
    response is 200 but renders the 'type at least N characters'
    notice rather than running a search."""
    r = client.get(f"/{inbox_name}/search?q=x")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "characters" in body.lower()


def test_author_view_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x").status_code == 404


def test_author_feed_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x/feed.atom").status_code == 404


def test_request_id_round_trip(client):
    """X-Request-Id from the client should come back on the response;
    if absent, the server mints one."""
    r = client.get("/healthz", headers={"X-Request-Id": "abc-test-id"})
    assert r.headers.get("X-Request-Id") == "abc-test-id"
    r = client.get("/healthz")
    rid = r.headers.get("X-Request-Id")
    assert rid and rid != "abc-test-id"


def test_security_headers_present(client):
    r = client.get("/")
    for h in (
        "Content-Security-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert r.headers.get(h), f"missing header {h}"


def _parse_csp(csp: str) -> dict[str, list[str]]:
    """Parse a CSP header into a {directive: [sources, ...]} map.
    Robust to whitespace, directive order, and the `script-src` vs
    `script-src-elem` substring trap."""
    out: dict[str, list[str]] = {}
    for part in csp.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        out[tokens[0]] = tokens[1:]
    return out


def test_security_headers_values_pin_csp_contract(client):
    """Header presence isn't enough -- the *values* are the contract.
    A future refactor that left `script-src 'unsafe-inline'` in the
    CSP would silently re-enable the inline-script vector that
    blocked the thread-fold controller (#fixed in 1.12.2). Pin the
    directives that matter via a parsed-directive lookup so a future
    reorder of the CSP header doesn't silently invalidate the test."""
    r = client.get("/")
    csp = r.headers.get("Content-Security-Policy", "")
    directives = _parse_csp(csp)

    # default-src 'self' (no `*`, no permissive fallback).
    assert directives.get("default-src") == ["'self'"]

    # script-src is the recurring footgun: no 'unsafe-inline', no
    # 'unsafe-eval'. Pin the negative contract directly on the
    # directive's source list, not a substring of the raw header.
    script_src = directives.get("script-src", [])
    assert script_src, "script-src directive missing"
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    # 'unsafe-eval' must also be absent globally (no other directive
    # may smuggle it in via, e.g., script-src-elem).
    assert all("'unsafe-eval'" not in srcs for srcs in directives.values())

    # Other crucial directives that block embed/iframe abuse.
    assert directives.get("frame-ancestors") == ["'none'"]
    assert directives.get("base-uri") == ["'self'"]

    # Other headers are short enough to pin exactly.
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_hsts_emitted_only_on_https_requests(client):
    """HSTS is the one pin-the-browser-forever header in the bundle.
    It must NOT emit on plain-HTTP dev sessions (where it would brick
    the local workflow) and MUST emit on requests forwarded with
    X-Forwarded-Proto: https. max-age and the includeSubDomains
    directive are part of the contract; a refactor that drops either
    would silently weaken the production posture."""
    # Without the proxy header: no HSTS.
    r_plain = client.get("/")
    assert "Strict-Transport-Security" not in r_plain.headers

    # With X-Forwarded-Proto: https: HSTS emitted with the pinned value.
    r_https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    hsts = r_https.headers.get("Strict-Transport-Security", "")
    assert hsts, "HSTS must emit when X-Forwarded-Proto=https"
    assert "max-age=" in hsts
    # max-age must be >= 6 months (15768000s); under that, browsers
    # treat the policy as advisory rather than enforced.
    import re
    m = re.search(r"max-age=(\d+)", hsts)
    assert m is not None
    assert int(m.group(1)) >= 15_768_000
    assert "includeSubDomains" in hsts


def test_author_page_autodiscovery_link(client, inbox_name):
    """The per-author HTML page advertises its atom feed via
    `<link rel="alternate" type="application/atom+xml">` in the head,
    so feed readers can subscribe from the page URL alone."""
    r = client.get(f"/{inbox_name}/author/example.com")
    assert r.status_code == 200
    body = r.data.decode()
    # The autodiscovery contract is *one specific <link>* element
    # carrying all three attrs together. Separate string searches
    # would pass even if rel="alternate" and the Atom feed lived
    # on different elements. Match the whole element instead.
    import re
    link_re = re.compile(
        r'<link\b[^>]*rel="alternate"[^>]*type="application/atom\+xml"[^>]*'
        r'href="/' + re.escape(inbox_name)
        + r'/author/example\.com/feed\.atom"',
        re.DOTALL,
    )
    assert link_re.search(body) is not None, (
        f"missing <link rel='alternate' type='application/atom+xml' "
        f"href='/{inbox_name}/author/example.com/feed.atom'> in <head>"
    )


def test_inbox_tracker_tile_links_to_atom(client, inbox_name):
    """The tracker tiles on the inbox dashboard surface a small `atom`
    link next to `all →` so the per-author feed is discoverable
    without visiting the HTML view first."""
    from mimir.inboxes import set_tracked_authors

    # Seed a tracker on the inbox so the tile section renders.
    set_tracked_authors(inbox_name, {"Examples": "example.com"})
    r = client.get(f"/{inbox_name}/")
    assert r.status_code == 200
    body = r.data.decode()
    assert f'/{inbox_name}/author/example.com/feed.atom' in body


def test_atom_feed_well_formed(client, inbox_name):
    """The Atom feed must:
    - parse as XML with the Atom namespace,
    - carry required <id>, <title>, <updated> at the feed level,
    - carry at least one <entry> (the seeded inbox has articles),
    - and each entry must have <id>, <title>, <updated>, plus a
      <link rel="alternate"> pointing back into the archive.

    A feed missing entries -- which the older presence-only test
    would've passed -- is useless to feed readers."""
    import re
    import xml.etree.ElementTree as ET

    r = client.get(f"/{inbox_name}/feed.atom")
    assert r.status_code == 200
    assert r.mimetype.startswith("application/atom+xml")
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.tag.endswith("feed")  # root is <feed>
    for required in ("a:id", "a:title", "a:updated"):
        assert root.find(required, ns) is not None, f"feed-level {required} missing"

    entries = root.findall("a:entry", ns)
    assert len(entries) > 0, "feed has no <entry> elements"
    for e in entries:
        assert e.find("a:id", ns) is not None
        assert e.find("a:title", ns) is not None
        assert e.find("a:updated", ns) is not None
        # Each entry has an alternate link pointing at the article URL.
        alt = e.find("a:link[@rel='alternate']", ns)
        assert alt is not None and alt.get("href"), (
            "entry missing rel=alternate link with href"
        )
        # The href must be under the inbox we're querying. Cross-post
        # canonical resolution flips it to a sibling inbox; both
        # `/<inbox>/...` and `/<sibling>/...` are valid, but the host
        # mustn't leak (only path) and the path must be one of mimir's
        # message URLs.
        href = alt.get("href")
        # Path-shape match rather than a hardcoded year disjunction —
        # the latter goes stale every January and gives no diagnostic
        # value beyond "the URL contains some year we listed."
        assert re.search(r"/\d{4}/\d{2}/\d+(?:[/?#]|$)", href), (
            f"entry link doesn't look like a message URL: {href}"
        )


def test_atom_feed_author_strips_placeholder_and_email(
    client, tmp_path, monkeypatch,
):
    """Atom <author><name> is the display name only -- same posture
    as JSON-LD's author.name. Feed readers render <name> as the
    byline, where the `<hidden>` placeholder reads as broken
    metadata exactly as it did in JSON-LD before the 2026-05-12
    fix. Allowlisted senders also surface display-name only -- the
    visible HTML still shows their full address, but the structured
    surfaces stay symmetric across feed readers and search engines."""
    import xml.etree.ElementTree as ET
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _ingest_one_article(
        tmp_path, "alpha", "atom-named@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    r = client.get("/alpha/feed.atom")
    assert r.status_code == 200
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    names = [
        e.findtext("a:author/a:name", default="", namespaces=ns)
        for e in root.findall("a:entry", ns)
    ]
    target = next(n for n in names if "David Woodhouse" in n)
    assert target == "David Woodhouse"
    # Belt-and-braces: across all entries, no placeholder, no `@`
    # in any byline. The seeded `a@b.example` corpus has no display
    # name and falls through to the neutral fallback rather than
    # leaking the bare address.
    for n in names:
        assert "<hidden>" not in n
        assert "@" not in n


# robots.txt / security.txt / sitemap.xml


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Sitemap:" in body
    assert "/sitemap.xml" in body


def test_security_txt_404_when_unconfigured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", None)
    assert client.get("/security.txt").status_code == 404
    assert client.get("/.well-known/security.txt").status_code == 404


def test_security_txt_when_configured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", None)
    monkeypatch.setattr(settings, "security_encryption_url", None)
    for url in ("/security.txt", "/.well-known/security.txt"):
        r = client.get(url)
        assert r.status_code == 200
        assert r.mimetype == "text/plain"
        body = r.get_data(as_text=True)
        assert "Contact: mailto:test@example.com" in body
        # Expires is dynamic; just confirm the field is present.
        assert "Expires:" in body
        assert "Preferred-Languages: en" in body


def test_security_txt_optional_fields_emitted_when_set(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", "https://example.com/policy")
    monkeypatch.setattr(settings, "security_encryption_url", "https://example.com/pgp.asc")
    body = client.get("/security.txt").get_data(as_text=True)
    assert "Policy: https://example.com/policy" in body
    assert "Encryption: https://example.com/pgp.asc" in body


def _any_article_in(inbox_name):
    """Helper: return one (Article, inbox_name) pair from the running
    DB so the redirect tests have a real Message-ID to point at."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        return s.execute(
            select(Article)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(Inbox.name == inbox_name)
            .limit(1)
        ).scalar_one_or_none()


@pytest.mark.parametrize("prefix", ["/m", "/{inbox}/m"])
def test_message_id_lookup_301_redirects_to_canonical(client, inbox_name, prefix):
    """Both the global Message-ID lookup (`/m/<id>`) and the
    inbox-scoped form (`/<inbox>/m/<id>`) 301 to the article's
    canonical URL. Parametrized over the URL prefix so the redirect
    contract is pinned exactly once."""
    art = _any_article_in(inbox_name)
    if art is None:
        pytest.skip("no articles in DB")
    url = prefix.format(inbox=inbox_name) + f"/{art.message_id}"
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert loc.endswith(f"/{art.id}")
    # Inbox-scoped form must resolve to the same inbox's URL space;
    # the global form may go to any canonical inbox so we only pin
    # the trailing id.
    if prefix.startswith("/{inbox}"):
        assert loc.startswith(f"/{inbox_name}/")
    # Cache header on the redirect itself -- crawlers should be able
    # to cache the hop.
    assert r.headers.get("Cache-Control", "").startswith("public")


def test_message_id_404_unknown(client):
    assert client.get("/m/no-such-message-id-12345").status_code == 404


def test_sitemap_xml(client):
    """`/sitemap.xml` is the sitemap index: a `<sitemapindex>` of
    `<sitemap>` children, one per inbox sub-sitemap plus `/meta-sitemap.xml`.
    Every linked sub-sitemap URL must itself resolve (200) -- a
    crawler that follows a 404 sitemap link reports the site as
    broken even if `/sitemap.xml` itself rendered fine."""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # Root element is <sitemapindex>, not <urlset>.
    assert root.tag.endswith("sitemapindex")
    sitemaps = root.findall("s:sitemap", ns)
    locs = {sm.find("s:loc", ns).text for sm in sitemaps}
    # Every configured inbox gets a sub-sitemap. A future regression
    # that drops an inbox from the iteration would silently leave it
    # un-indexed.
    expected_sub_sitemaps = {"/meta-sitemap.xml", "/alpha/sitemap.xml", "/beta/sitemap.xml"}
    found_paths = {urlparse(loc).path for loc in locs}
    assert expected_sub_sitemaps.issubset(found_paths), (
        f"missing sub-sitemap(s): {expected_sub_sitemaps - found_paths}"
    )
    # Every advertised sub-sitemap URL actually resolves (within mimir's
    # own routes). Hit each path and check 200 + parseable XML.
    for loc in locs:
        path = urlparse(loc).path
        sub_r = client.get(path)
        assert sub_r.status_code == 200, (
            f"sitemap index lists {path!r} which returned {sub_r.status_code}"
        )
        ET.fromstring(sub_r.get_data())  # well-formed XML


# ProxyFix: trusted_proxy_hops controls X-Forwarded-* honouring


def _build_app_with_hops(monkeypatch, hops: int):
    """Re-create the Flask app with `trusted_proxy_hops` patched.
    `create_app()` decides ProxyFix wiring at construction time."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    from mimir import create_app
    return create_app()


def test_proxy_fix_off_when_hops_zero(monkeypatch):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app = _build_app_with_hops(monkeypatch, 0)
    assert not isinstance(app.wsgi_app, ProxyFix)


def test_proxy_fix_wrapped_when_hops_positive(monkeypatch):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app = _build_app_with_hops(monkeypatch, 1)
    assert isinstance(app.wsgi_app, ProxyFix)


def test_proxy_fix_unwraps_remote_addr_from_xff(monkeypatch):
    """End-to-end: with trusted_proxy_hops=1, request.remote_addr
    reflects X-Forwarded-For instead of the connection IP."""
    from flask import request
    app = _build_app_with_hops(monkeypatch, 1)
    captured: dict[str, str | None] = {}

    @app.route("/__probe_remote")
    def _probe():
        captured["remote"] = request.remote_addr
        captured["scheme"] = request.scheme
        return ""

    client = app.test_client()
    client.get(
        "/__probe_remote",
        environ_base={"REMOTE_ADDR": "10.30.30.2"},
        headers={
            "X-Forwarded-For": "203.0.113.7",
            "X-Forwarded-Proto": "https",
        },
    )
    assert captured["remote"] == "203.0.113.7"
    assert captured["scheme"] == "https"


def test_proxy_fix_off_keeps_connection_remote_addr(monkeypatch):
    """With hops=0, XFF is NOT honoured — request.remote_addr stays
    the connection IP. Guards against accidentally enabling ProxyFix
    on a directly-exposed app, where attackers could spoof XFF."""
    from flask import request
    app = _build_app_with_hops(monkeypatch, 0)
    captured: dict[str, str | None] = {}

    @app.route("/__probe_remote")
    def _probe():
        captured["remote"] = request.remote_addr
        return ""

    client = app.test_client()
    client.get(
        "/__probe_remote",
        environ_base={"REMOTE_ADDR": "10.30.30.2"},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert captured["remote"] == "10.30.30.2"


# Access-log shape


def test_access_log_records_user_agent(client):
    """Regression: the structured access log captured `ua: null` for
    every request because the falsy check on `request.user_agent`
    depends on Werkzeug's UA parser recognising a browser, which
    misfires on non-browser UAs (curl, wget) and even some browsers
    in newer Werkzeug. Read the raw header instead."""
    import json
    import logging

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    request_logger = logging.getLogger("mimir.request")
    prior = (request_logger.level, request_logger.disabled)
    # pytest's logging plugin marks unmanaged loggers as disabled
    # between tests; flip it back so emit() reaches our handler.
    request_logger.disabled = False
    request_logger.setLevel(logging.INFO)
    handler = _Capture()
    request_logger.addHandler(handler)
    try:
        client.get("/healthz", headers={"User-Agent": "probe-agent/1.0"})
    finally:
        request_logger.removeHandler(handler)
        request_logger.level, request_logger.disabled = prior

    assert captured, "no log line was emitted"
    payload = json.loads(captured[-1])
    assert payload["ua"] == "probe-agent/1.0"


# Phase 3: render-side canonical surface


def test_canonical_inbox_name_uses_canonical_id():
    from unittest.mock import MagicMock
    from mimir.web import _canonical_inbox_name

    art = MagicMock()
    art.canonical_inbox_id = 7
    # links: linux-fsdevel is canonical; alphabetical-first would be lkml.
    links = [(1, "lkml"), (7, "linux-fsdevel")]
    assert _canonical_inbox_name(art, links) == "linux-fsdevel"


def test_canonical_inbox_name_falls_back_alphabetical_when_null():
    from unittest.mock import MagicMock
    from mimir.web import _canonical_inbox_name

    art = MagicMock()
    art.canonical_inbox_id = None
    links = [(7, "linux-fsdevel"), (1, "lkml")]
    assert _canonical_inbox_name(art, links) == "linux-fsdevel"


def test_canonical_inbox_name_returns_none_for_orphan_article():
    from unittest.mock import MagicMock
    from mimir.web import _canonical_inbox_name

    art = MagicMock()
    art.canonical_inbox_id = None
    assert _canonical_inbox_name(art, []) is None


def test_canonical_url_for_combines_base_and_msg_url():
    from unittest.mock import MagicMock
    from datetime import datetime
    from mimir.web import _canonical_url_for

    art = MagicMock()
    art.id = 99
    art.canonical_inbox_id = 7
    art.date = datetime(2024, 5, 1)
    links = [(7, "linux-fsdevel")]
    assert (
        _canonical_url_for(art, links, base="https://ex.com")
        == "https://ex.com/linux-fsdevel/2024/05/99"
    )


def test_sitemap_cross_post_appears_in_each_linked_inbox(client):
    """art3 is cross-posted alpha+beta. In the per-inbox sitemap world,
    it is listed under *both* `/alpha/sitemap.xml` and
    `/beta/sitemap.xml` — each one is a real, crawlable URL, and the
    canonical `<link>` on the page itself tells search engines which
    to keep. The sitemap doesn't try to enforce one-canonical-URL-per-
    article anymore (that was the old global-sitemap design)."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art3 = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art_id = art3.id

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def article_locs(inbox: str) -> list[str]:
        root = ET.fromstring(client.get(f"/{inbox}/sitemap.xml").get_data())
        return [
            u.find("s:loc", ns).text
            for u in root.findall("s:url", ns)
            if u.find("s:loc", ns).text.endswith(f"/{art_id}")
        ]

    alpha_locs = article_locs("alpha")
    beta_locs = article_locs("beta")
    assert len(alpha_locs) == 1 and "/alpha/" in alpha_locs[0]
    assert len(beta_locs) == 1 and "/beta/" in beta_locs[0]


def test_atom_feed_cross_post_id_is_canonical_in_either_feed(client):
    """The same cross-posted article surfaces in both /alpha/feed.atom
    and /beta/feed.atom. With canonical resolution, both feeds' entries
    for art3 must carry the SAME <id> — the canonical URL — so feed
    readers that key on <id> deduplicate across feeds."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art3 = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art_id = art3.id

    ns = {"a": "http://www.w3.org/2005/Atom"}

    def find_id_for(feed_xml: bytes, art_id: int) -> str | None:
        root = ET.fromstring(feed_xml)
        for entry in root.findall("a:entry", ns):
            link = entry.find("a:link[@rel='alternate']", ns)
            if link is not None and link.get("href", "").endswith(f"/{art_id}"):
                id_el = entry.find("a:id", ns)
                return id_el.text if id_el is not None else None
        return None

    alpha_feed = client.get("/alpha/feed.atom")
    beta_feed = client.get("/beta/feed.atom")
    assert alpha_feed.status_code == 200
    assert beta_feed.status_code == 200

    alpha_id = find_id_for(alpha_feed.get_data(), art_id)
    beta_id = find_id_for(beta_feed.get_data(), art_id)
    assert alpha_id is not None and beta_id is not None
    assert alpha_id == beta_id
    # Fallback is alphabetical-first → alpha.
    assert "alpha" in alpha_id


# Per-page title tags + sitemap <lastmod>


def _title_of(html: str) -> str:
    import re
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    return m.group(1).strip() if m else ""


def _ingest_one_article(
    tmp_path,
    inbox_name: str,
    message_id: str,
    subject: str = "test",
    in_reply_to: str | None = None,
    to: str | None = None,
    author: str = "a@b.example",
    body: bytes = b"body",
) -> tuple[int, str]:
    """Build a tiny pubinbox-shaped bare repo with one message and
    ingest it into `inbox_name`. Repoints the inbox's mirror_path at
    `tmp_path` so `read_message` can fetch the blob; returns
    `(article_id, /<inbox>/YYYY/MM/<id>)` for routing tests."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    extra = b""
    if in_reply_to is not None:
        extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
    if to is not None:
        extra += b"To: " + to.encode() + b"\r\n"
    raw = (
        b"Message-ID: <" + message_id.encode() + b">\r\n"
        b"From: " + author.encode() + b"\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        + extra +
        b"\r\n"
        + body
    )
    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    blob = Blob.from_string(raw)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"m", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = []
    commit.author = commit.committer = b"test <t@x>"
    commit.commit_time = commit.author_time = 1700000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"add"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        art = s.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one()
        url = (
            f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"
        )
        return art.id, url


def test_meta_index_title_is_just_site_name(client):
    title = _title_of(client.get("/").data.decode())
    assert title == "mimir"


def test_inbox_dashboard_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/").data.decode())
    assert title == f"{inbox_name} | mimir"


def test_search_title_includes_query(client, inbox_name):
    title = _title_of(
        client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    )
    assert title == f"Search 'Linux' | {inbox_name} | mimir"


def test_search_title_when_no_query(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/search").data.decode())
    assert title == f"Search | {inbox_name} | mimir"


def test_year_archive_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/2024/").data.decode())
    assert title == f"2024 | {inbox_name} | mimir"


def test_month_archive_title(client, inbox_name):
    # Month label format is "Month YYYY" — just sanity-check the
    # separator + scope tokens are present.
    title = _title_of(client.get(f"/{inbox_name}/2024/05/").data.decode())
    assert title.endswith(f" | {inbox_name} | mimir")
    assert "2024" in title


def test_daily_today_title(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/today").data.decode())
    assert " | " in title
    assert title.endswith(f" | {inbox_name} | mimir")


def test_since_title_shape(client, inbox_name):
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    title = _title_of(client.get(f"/{inbox_name}/since/{recent}").data.decode())
    assert title.endswith(f" | {inbox_name} | mimir")
    assert recent in title


def test_message_subject_truncated_to_80(client, tmp_path):
    """Long subjects (patch series with v17 RFC 23/47 etc.) get
    truncated at 80 chars in <title> so SERPs don't overflow.
    Drives the real route end-to-end with a freshly-ingested article
    rather than testing Jinja's filter in isolation."""
    long_subject = "x" * 200
    _, url = _ingest_one_article(
        tmp_path, "alpha", "trunc@example.com", subject=long_subject,
    )
    r = client.get(url)
    assert r.status_code == 200
    title = _title_of(r.data.decode())
    assert title.endswith(" | alpha | mimir")
    # The subject portion shouldn't be the full 200-char input.
    subject_part = title.rsplit(" | alpha | mimir", 1)[0]
    assert len(subject_part) <= 84  # Jinja truncate(80) keeps ~81 incl ellipsis
    assert subject_part != long_subject


def test_sitemap_index_sub_sitemap_lastmods(client):
    """The sitemap index emits a `<lastmod>` per sub-sitemap so
    crawlers can skip unchanged inboxes. With seeded data, alpha,
    beta, and the meta-sitemap all carry the global max 2024-03-01
    (art3's cross-post date)."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    lastmods = {
        sm.find("s:loc", ns).text: (
            sm.find("s:lastmod", ns).text
            if sm.find("s:lastmod", ns) is not None
            else None
        )
        for sm in root.findall("s:sitemap", ns)
    }
    meta_loc = next(loc for loc in lastmods if loc.endswith("/meta-sitemap.xml"))
    alpha_loc = next(loc for loc in lastmods if loc.endswith("/alpha/sitemap.xml"))
    beta_loc = next(loc for loc in lastmods if loc.endswith("/beta/sitemap.xml"))
    assert lastmods[meta_loc] == "2024-03-01"
    assert lastmods[alpha_loc] == "2024-03-01"
    assert lastmods[beta_loc] == "2024-03-01"


def test_meta_sitemap_lastmod_is_global_max(client):
    """`/meta-sitemap.xml` is a one-URL urlset listing `/` with the
    global max article date as its lastmod."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/meta-sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = root.findall("s:url", ns)
    assert len(urls) == 1
    assert urls[0].find("s:loc", ns).text.endswith("/")
    assert urls[0].find("s:lastmod", ns).text == "2024-03-01"


def test_inbox_sitemap_dashboard_lastmod_is_inbox_max(client):
    """First entry of `/<inbox>/sitemap.xml` is the inbox dashboard,
    with the per-inbox max as lastmod. alpha's range is 2024-01-01
    to 2024-03-01 (art3 cross-post); beta's is 2024-02-01 to
    2024-03-01."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for inbox in ("alpha", "beta"):
        root = ET.fromstring(client.get(f"/{inbox}/sitemap.xml").get_data())
        urls = root.findall("s:url", ns)
        assert urls[0].find("s:loc", ns).text.endswith(f"/{inbox}/")
        assert urls[0].find("s:lastmod", ns).text == "2024-03-01"


def test_inbox_sitemap_article_lastmod_matches_article_date(client):
    """In a per-inbox sitemap, the per-article `<lastmod>` is the
    article's own date in YYYY-MM-DD."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art1 = s.execute(
            select(Article).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        art1_id = art1.id
        art1_date = art1.date

    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    art1_lastmod = None
    for u in root.findall("s:url", ns):
        loc = u.find("s:loc", ns).text
        if loc.endswith(f"/{art1_id}"):
            lm = u.find("s:lastmod", ns)
            art1_lastmod = lm.text if lm is not None else None
            break
    assert art1_lastmod == art1_date.strftime("%Y-%m-%d")


def test_inbox_sitemap_lists_year_and_month_archives(client):
    """Year + month archives that actually have data appear as
    lastmod-less `<url>` entries (discovery anchors, not refresh
    signals). Empty months are skipped — `/alpha/2024/` and
    `/alpha/2024/01/` are present, but only months with messages."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {u.find("s:loc", ns).text for u in root.findall("s:url", ns)}
    assert any(loc.endswith("/alpha/2024/") for loc in locs)
    # alpha has messages in 2024-01 (art1, art4) and 2024-03 (art3),
    # but not 2024-02.
    assert any(loc.endswith("/alpha/2024/01/") for loc in locs)
    assert any(loc.endswith("/alpha/2024/03/") for loc in locs)
    assert not any(loc.endswith("/alpha/2024/02/") for loc in locs)


def test_inbox_sitemap_404_for_unknown_inbox(client):
    """Unknown inbox slug returns 404 rather than rendering an empty
    sitemap."""
    r = client.get("/notreal/sitemap.xml")
    assert r.status_code == 404


def test_inbox_sitemap_articles_scoped_to_that_inbox(client):
    """Articles only linked to beta (art2) don't appear in alpha's
    sitemap, and vice versa."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art2 = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        art2_id = art2.id

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    alpha_root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    alpha_locs = {u.find("s:loc", ns).text for u in alpha_root.findall("s:url", ns)}
    assert not any(loc.endswith(f"/{art2_id}") for loc in alpha_locs)
    beta_root = ET.fromstring(client.get("/beta/sitemap.xml").get_data())
    beta_locs = {u.find("s:loc", ns).text for u in beta_root.findall("s:url", ns)}
    assert any(loc.endswith(f"/{art2_id}") for loc in beta_locs)


def test_global_message_id_lookup_uses_canonical_inbox(client):
    """art3 is cross-posted alpha+beta. Setting canonical_inbox_id to
    beta makes /m/<id> redirect to /beta/.../<id>, not /alpha/.../<id>
    (which would be the alphabetical-first fallback)."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        art = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art.canonical_inbox_id = beta.id
        s.commit()

    r = client.get("/m/art3@example.com", follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert "/beta/" in loc
    assert "/alpha/" not in loc


def test_global_message_id_lookup_falls_back_to_alphabetical(client):
    """With canonical_inbox_id NULL (default for seeded art3), the
    redirect goes to alphabetical-first — alpha."""
    r = client.get("/m/art3@example.com", follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert "/alpha/" in loc


# Per-page meta description + Open Graph + Twitter Card tags


def _meta_value(html: str, name_or_property: str) -> str | None:
    """Extract a <meta> tag's content. Matches both `name=` and
    `property=` since OG uses property and the rest use name."""
    import re
    pattern = (
        r'<meta\s+(?:property|name)="' + re.escape(name_or_property)
        + r'"\s+content="([^"]*)"'
    )
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _data_attr_values(html: str) -> str:
    """Concatenate every `data-…="…"` attribute value in the document
    into a single string for `not-in` checks. Used to defend the
    invariant that machine-readable attributes never carry tokens
    (Message-IDs, raw email addresses) that the visible HTML
    redacted."""
    import re
    return "\n".join(re.findall(r'data-[a-z0-9-]+="([^"]*)"', html))


def test_meta_index_emits_default_description(client):
    html = client.get("/").data.decode()
    desc = _meta_value(html, "description")
    assert desc == (
        "Indexed mailing-list archives, served from local "
        "public-inbox v2 mirrors."
    )


def test_inbox_dashboard_meta_description_starts_with_inbox_archive(client, inbox_name):
    """Either rich or fallback form: both lead with '<inbox> archive'."""
    html = client.get(f"/{inbox_name}/").data.decode()
    desc = _meta_value(html, "description")
    assert desc is not None
    assert desc.startswith(f"{inbox_name} archive")


def test_search_meta_description_with_query(client, inbox_name):
    html = client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    desc = _meta_value(html, "description")
    assert desc == f"Search results for 'Linux' in {inbox_name}."


def test_search_meta_description_when_no_query(client, inbox_name):
    html = client.get(f"/{inbox_name}/search").data.decode()
    desc = _meta_value(html, "description")
    assert desc == f"Search the {inbox_name} archive by subject or author."


def test_og_tags_on_index_match_expected_values(client):
    html = client.get("/").data.decode()
    expected_desc = (
        "Indexed mailing-list archives, served from local "
        "public-inbox v2 mirrors."
    )
    assert _meta_value(html, "og:title") == "mimir"
    assert _meta_value(html, "og:type") == "website"
    assert _meta_value(html, "og:site_name") == "mimir"
    assert _meta_value(html, "og:url") == "http://localhost/"
    assert _meta_value(html, "og:description") == expected_desc


def test_og_type_article_on_message_page(client, tmp_path):
    """Message pages override og:type to 'article' (the rest of the
    site stays on the 'website' default). End-to-end via a real
    ingested article so we exercise the rendered tag, not the
    template source."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "ogtype@example.com",
    )
    r = client.get(url)
    assert r.status_code == 200
    assert _meta_value(r.data.decode(), "og:type") == "article"


def test_twitter_card_tags_match_og_pair(client, inbox_name):
    """twitter:title/description should mirror og:title/description so
    Twitter's preview matches whatever Slack/etc. show via OG. The
    card type is `summary_large_image` paired with the 1200x630 PNG
    og:image (Twitter/X doesn't render SVG, LinkedIn is inconsistent
    on it -- flagged in the 2026-05-12 review)."""
    html = client.get(f"/{inbox_name}/").data.decode()
    assert _meta_value(html, "twitter:card") == "summary_large_image"
    assert _meta_value(html, "twitter:title") == _meta_value(html, "og:title")
    assert (
        _meta_value(html, "twitter:description")
        == _meta_value(html, "og:description")
    )
    assert _meta_value(html, "twitter:title") == f"{inbox_name} | mimir"
    # Image mirrors og:image; the asset itself is a 1200x630 PNG.
    assert _meta_value(html, "twitter:image") == _meta_value(html, "og:image")
    assert _meta_value(html, "og:image").endswith("/og-image.png")
    # Width / height / alt help picky link-card renderers pick the
    # intended size and improve a11y. Mirrored on twitter:image:alt.
    assert _meta_value(html, "og:image:width") == "1200"
    assert _meta_value(html, "og:image:height") == "630"
    og_alt = _meta_value(html, "og:image:alt") or ""
    assert "Ratatoskr" in og_alt
    assert _meta_value(html, "twitter:image:alt") == og_alt


def test_meta_description_inbox_uses_rich_form_when_stats_present(client):
    """Seeded alpha has 3 linked articles → stats.total>0 → description
    is the rich form: '<inbox> archive: N message(s) from <first> to
    <last>, indexed and searchable.' (NOT the bare fallback '<inbox>
    archive on <site>.')"""
    html = client.get("/alpha/").data.decode()
    desc = _meta_value(html, "description")
    assert desc is not None
    # Markers exclusive to the rich form.
    assert desc.startswith("alpha archive: ")
    assert "message" in desc
    assert "indexed and searchable" in desc


# JSON-LD structured data


def _json_ld_blocks(html: str) -> list[dict]:
    """Extract every <script type=application/ld+json> JSON payload."""
    import json
    import re
    out: list[dict] = []
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>',
        html, re.DOTALL,
    ):
        out.append(json.loads(m.group(1)))
    return out


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
        "Indexed mailing-list archives, served from local "
        "public-inbox v2 mirrors."
    )


def test_thread_summary_helper_counts_and_relative_time():
    """`_thread_summary` returns author_count (deduped by email) and a
    coarse relative-time string for the most-recent message. Drives
    the closed-state fold one-liner ('23 messages, 5 authors, 2h ago')."""
    from dataclasses import dataclass
    from datetime import datetime, timedelta, timezone
    from mimir.web import _thread_summary

    @dataclass
    class N:
        author: str
        date: datetime | None
        message_id: str = ""
        depth: int = 0

    now = datetime.now(timezone.utc)
    nodes = [
        N(author="Alice <a@x.example>", date=now - timedelta(days=2)),
        # Same address, different display name. Dedup must key on
        # email so display-name drift (signatures change, From-line
        # variations) doesn't fragment the count. A regression that
        # deduped on the full author string or just the display name
        # would emit 3 here.
        N(author="Alicia Q. <A@X.example>", date=now - timedelta(days=1)),
        N(author="Bob <b@y.example>", date=now - timedelta(hours=3)),
        N(author=None, date=None),  # missing data; doesn't crash
    ]
    s = _thread_summary(nodes)
    # Alice/Alicia (case-insensitive dedup on the email) + Bob = 2;
    # None ignored.
    assert s["author_count"] == 2
    assert s["last_activity_rel"] == "3h ago"


def test_message_page_single_message_thread_skips_fold_scaffolding(
    client, tmp_path,
):
    """When a thread has exactly one message and no off-list parent,
    the whole `.thread-context` block is omitted: no fold scaffolding,
    no toolbar, no `<html>`-level data attrs, no fold-controller
    script context."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "fold-solo@example.com", subject="solo",
    )
    body = client.get(url).data.decode()
    assert "thread-context" not in body
    assert "data-thread-fold" not in body
    assert "data-thread-root-id=" not in body
    assert "data-fold-set=" not in body
    # thread-fold.js is loaded on every page (from base.html) but with no
    # data-thread-* attrs on <html> the controller short-circuits the
    # FOUC-free block and the event handlers find no .thread-context.
    assert 'src="/static/js/thread-fold.js"' in body


def _seed_three_message_thread(tmp_path, inbox_name):
    """Build a 3-message thread root → reply → reply-to-reply in ONE
    shared bare repo, so the inbox's mirror_path resolves blobs for
    every article (not just the last-ingested one).

    `_ingest_one_article` creates its own fresh `0.git` per call and
    repoints mirror_path; that's fine for single-message tests but
    breaks here -- viewing the root would 500 trying to look up the
    root's commit_sha in the *reply's* mirror. This helper appends
    each message as a separate commit in the same bare repo so all
    three resolve under one mirror_path.

    Returns a dict keyed by role ("root" / "reply" / "nested") with
    `(article_id, url, message_id)` tuples for each.
    """
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    prev_commit = None

    def _add(message_id: str, subject: str, in_reply_to: str | None) -> None:
        nonlocal prev_commit
        extra = b""
        if in_reply_to is not None:
            extra += b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n"
        raw = (
            b"Message-ID: <" + message_id.encode() + b">\r\n"
            b"From: a@b.example\r\n"
            b"Subject: " + subject.encode() + b"\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
            + extra +
            b"\r\n"
            b"body"
        )
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [prev_commit] if prev_commit else []
        commit.author = commit.committer = b"test <t@x>"
        # 1704067200 == 2024-01-01 00:00 UTC; bumped per message so each
        # commit is unique and threading order is stable.
        commit.commit_time = commit.author_time = 1704067200 + len(_added)
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = b"add"
        repo.object_store.add_object(commit)
        prev_commit = commit.id
        _added.append(message_id)

    _added: list[str] = []
    _add("fold-root@example.com", "root msg", None)
    _add("fold-reply@example.com", "Re: root msg", "fold-root@example.com")
    _add("fold-nested@example.com", "Re: root msg", "fold-reply@example.com")
    repo.refs[b"HEAD"] = prev_commit

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        out = {}
        for role, mid in [
            ("root", "fold-root@example.com"),
            ("reply", "fold-reply@example.com"),
            ("nested", "fold-nested@example.com"),
        ]:
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            url = f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"
            out[role] = (art.id, url, mid)
        return out


def test_message_page_thread_fold_context_is_root_when_viewing_root(
    client, tmp_path,
):
    """Viewing the thread root sets data-thread-context="root" on
    <html>; the controller script reads that and defaults the fold
    state to `partial` (vs `closed` for a deep reply). The thread-
    root id is the integer Article.id, not the RFC 822 Message-ID
    -- the latter would leak email-shaped tokens that the visible
    redaction was supposed to hide."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    root_id = msgs["root"][0]
    body = client.get(msgs["root"][1]).data.decode()
    assert f'data-thread-root-id="{root_id}"' in body
    assert 'data-thread-context="root"' in body
    # Belt-and-braces: the RFC 822 Message-ID must not appear in any
    # data-* attribute.
    assert "fold-root@example.com" not in _data_attr_values(body)


def test_message_page_thread_fold_context_is_deep_for_replies(
    client, tmp_path,
):
    """Viewing any non-root message in a thread sets
    data-thread-context="deep" -- the controller defaults to `closed`
    so the body gets full real estate."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    root_id = msgs["root"][0]
    body = client.get(msgs["nested"][1]).data.decode()
    assert f'data-thread-root-id="{root_id}"' in body
    assert 'data-thread-context="deep"' in body


def test_message_page_thread_fold_active_marker_on_current_li(
    client, tmp_path,
):
    """The tree <li> whose data-article-id matches the current view
    carries class="is-active"; the others carry no such class. The
    JS controller toggles this class on htmx:afterSwap, but the
    server's initial render has to set it correctly for the first
    paint (when JS hasn't run yet or is disabled)."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    nested_id, nested_url, nested_mid = msgs["nested"]
    body = client.get(nested_url).data.decode()

    # The active <li> markup: data-article-id matches current view AND
    # class="is-active". Use a regex-ish substring check; the exact
    # element ordering is set by the template.
    import re
    li_active_pattern = re.compile(
        r'<li[^>]*data-article-id="' + re.escape(str(nested_id)) +
        r'"[^>]*class="is-active"',
        re.DOTALL,
    )
    assert li_active_pattern.search(body) is not None, (
        "expected active <li> for current message; markup was: "
        + body[body.find("thread-list"):body.find("</ul>")]
    )

    # No other <li> carries .is-active. There are three <li> total
    # (root + reply + nested); one is active, two must not be.
    li_with_class = re.findall(r'<li[^>]*class="is-active"', body)
    assert len(li_with_class) == 1
    # Message-ID must not have leaked into any data-* attribute on the
    # <li>: the original visible-redaction rationale is "Message-IDs
    # leak email-shaped tokens"; carrying them in data-message-id
    # silently undid that. Belt-and-braces check.
    assert nested_mid not in _data_attr_values(body)


def test_message_page_thread_fold_toolbar_summary_counts_match(
    client, tmp_path,
):
    """The closed-state summary line ("N messages, M authors, Th ago")
    inside .thread-summary must reflect the real thread shape -- the
    seeded 3-message thread has 3 distinct authors (each
    `_ingest_one_article` defaults to `a@b.example`, so the dedup ends
    up at 1 author). Pin both counts explicitly so a future change to
    the summary helper that drops uniqueness is caught."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    body = client.get(msgs["root"][1]).data.decode()

    import re
    # Extract the .thread-summary span contents.
    m = re.search(
        r'<span class="thread-summary">(.*?)</span>',
        body, re.DOTALL,
    )
    assert m is not None, "thread-summary span missing"
    summary_text = " ".join(m.group(1).split())  # collapse whitespace
    # All three articles share author "a@b.example" -> dedupes to 1.
    assert "3 messages" in summary_text
    assert "1 author" in summary_text
    assert "1 authors" not in summary_text  # singular form
    # Seeded thread is dated in Jan 2024 (commit_time fixed in
    # `_seed_three_message_thread`); the render falls back to an
    # absolute date beyond the 30-day relative window. Match any
    # 2024-anchored date format (YYYY-MM-DD, `Jan 1, 2024`, ...) so a
    # benign re-format of the date filter doesn't break this test --
    # the contract is "the year of the thread is shown", not the
    # exact string format.
    assert re.search(r"\b2024\b", summary_text), (
        f"thread summary doesn't carry the thread year: {summary_text!r}"
    )


def test_message_page_thread_fold_links_carry_htmx_attrs(
    client, tmp_path,
):
    """Every non-active tree <li> must carry an <a> with the full HTMX
    attribute set: hx-get, hx-target=#msg, hx-swap=outerHTML, and
    hx-push-url=true. Without them, intra-thread navigation falls
    back to full page reloads."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    body = client.get(msgs["root"][1]).data.decode()

    import re
    # All <a> inside the actual thread-list <ul>. Anchor on the element
    # open tag so the slice doesn't include the earlier `.thread-list`
    # CSS rule in the <style> block.
    ul_start = body.index('<ul class="thread-list"')
    ul_end = body.index("</ul>", ul_start)
    list_block = body[ul_start:ul_end]
    anchors = re.findall(r'<a[^>]*>', list_block)
    # Every <li> -- active or not -- carries an <a> with HTMX attrs.
    # The active treatment is class-on-<li> + CSS, not anchor suppression:
    # that keeps the JS controller's class-toggle-on-swap path simple
    # (no DOM rewrites between active <span> and inactive <a>).
    assert len(anchors) == 3, f"expected 3 anchors, got {len(anchors)}: {anchors}"
    for a in anchors:
        assert "hx-get=" in a
        assert 'hx-target="#msg"' in a
        assert 'hx-swap="outerHTML"' in a
        assert 'hx-push-url="true"' in a


def test_message_page_thread_fold_script_loads_before_section(
    client, tmp_path,
):
    """thread-fold.js is loaded from /static/ with a synchronous
    <script src=...> in <head>. It must execute *before* the
    <section class="thread-context"> opens so that the FOUC-free
    setAttribute on <html> resolves before CSS evaluates the section's
    descendant rules. Otherwise a localStorage pin overriding the
    server's context default would cause a visible flash."""
    msgs = _seed_three_message_thread(tmp_path, "alpha")
    body = client.get(msgs["nested"][1]).data.decode()
    script_idx = body.index('src="/static/js/thread-fold.js"')
    section_idx = body.index('<section class="thread-context"')
    body_idx = body.index("<body")
    assert script_idx < body_idx, (
        "thread-fold.js must be loaded from <head>, not <body>, to run "
        "before the section paints"
    )
    assert script_idx < section_idx


def test_message_page_htmx_request_returns_body_partial(client, tmp_path):
    """When HX-Request: true is set, the message route returns only the
    <article id="msg"> partial -- no <html>, no <head>, no thread tree.
    That's what lets the client-side script swap intra-thread nav into
    `#msg` without re-rendering surrounding chrome. The partial must
    carry the *content* the user came for, not just an empty article
    wrapper: the headline/from/date header, the message body, and
    attachments if any."""
    art_id, url = _ingest_one_article(
        tmp_path, "alpha", "htmx-swap@example.com",
        subject="swap test subject 12345",
    )
    r = client.get(url, headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.data.decode()

    # Outer shape: hx-swap=outerHTML on the link targets #msg, so the
    # response's root element must be <article id="msg" data-article-id=...>.
    assert body.lstrip().startswith("<article id=\"msg\""), (
        "HTMX partial must start with the swap-target element so "
        "hx-swap=outerHTML replaces #msg cleanly"
    )
    assert f'data-article-id="{art_id}"' in body
    # The Message-ID is the original RFC 822 token; it must not appear
    # in any data-* attribute. The swap key is the integer Article id.
    assert "htmx-swap@example.com" not in _data_attr_values(body)

    # Actual content present (not an empty wrapper).
    assert "swap test subject 12345" in body
    assert "<strong>From:</strong>" in body
    assert "<strong>Date:</strong>" in body
    assert 'class="message-body"' in body

    # No surrounding chrome -- the chrome must stay on the client.
    # Use stricter `<tag>` / `<tag ` patterns; bare "<head" would also
    # match "<header>" inside the article, which is legitimate.
    lower = body.lower()
    assert "<!doctype" not in lower
    assert "<html>" not in lower and "<html " not in lower
    assert "<head>" not in lower and "<head " not in lower
    assert "<body>" not in lower and "<body " not in lower
    assert "thread-context" not in body
    assert "thread-toolbar" not in body
    assert "thread-fold.js" not in body


def test_message_page_full_and_htmx_responses_share_article_content(
    client, tmp_path,
):
    """Full page render and HX-Request render must contain identical
    content inside <article id="msg">. Regression guard: a refactor
    that diverges the two render paths (e.g. one uses _message_body.html
    and the other inlines) would silently break HTMX swap parity."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "parity@example.com", subject="parity check",
    )
    full = client.get(url).data.decode()
    partial = client.get(url, headers={"HX-Request": "true"}).data.decode()

    # Extract the <article id="msg"...> ... </article> block from each.
    def article_block(html: str) -> str:
        start = html.index('<article id="msg"')
        end = html.index("</article>", start) + len("</article>")
        return html[start:end]

    assert article_block(full) == article_block(partial), (
        "Full-page and HTMX-partial responses must share the exact "
        "<article id=\"msg\"> contents; divergence breaks intra-thread "
        "swap parity."
    )


def test_message_page_htmx_request_on_unknown_message_404s(
    client, tmp_path,
):
    """HTMX requests at non-existent URLs must still 404 (not return an
    empty 200 partial). HTMX surfaces this to htmx:responseError so the
    client can react; a silent 200 with empty body would render a blank
    panel where the message should be."""
    # Seed one valid article so the inbox exists with a real mirror.
    _ingest_one_article(
        tmp_path, "alpha", "valid@example.com", subject="valid",
    )
    # Hit an article id that's 999999 above any real id; route 404s.
    r = client.get(
        "/alpha/2024/01/999999",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 404


def test_message_page_emits_discussion_forum_posting(client, tmp_path):
    """Message page graph contains DiscussionForumPosting with all
    required-for-rich-results fields populated, AND the emitted URLs
    match the canonical-inbox shape `/<canonical>/YYYY/MM/<id>`. A
    regression in canonical resolution would otherwise pass the
    `endswith(f"/{art_id}")` check while pointing at the wrong inbox."""
    import re
    art_id, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-fp@example.com", subject="hello world",
    )
    blocks = _json_ld_blocks(client.get(url).data.decode())
    assert len(blocks) == 1
    graph = blocks[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["headline"] == "hello world"
    # Canonical message URL: `/<inbox>/<YYYY>/<MM>/<id>` -- pin all
    # four segments, not just the trailing id.
    url_re = re.compile(r"^https?://[^/]+/alpha/\d{4}/\d{2}/" + str(art_id) + r"$")
    assert url_re.match(posting["@id"]), (
        f"@id doesn't match canonical pattern: {posting['@id']!r}"
    )
    assert posting["url"] == posting["@id"]
    assert posting["mainEntityOfPage"] == posting["@id"]
    assert posting["isPartOf"]["@type"] == "WebSite"
    assert posting["isPartOf"]["name"] == "alpha"
    isPartOf_re = re.compile(r"^https?://[^/]+/alpha/$")
    assert isPartOf_re.match(posting["isPartOf"]["url"]), (
        f"isPartOf.url isn't the canonical inbox dashboard: "
        f"{posting['isPartOf']['url']!r}"
    )
    # Default Date in _ingest_one_article is "Mon, 1 Jan 2024 00:00:00 +0000".
    assert posting["datePublished"].startswith("2024-01-01T00:00:00")
    assert posting["dateModified"] == posting["datePublished"]


def test_message_page_emits_breadcrumb_list(client, tmp_path):
    """The same @graph also carries a BreadcrumbList with the
    Site → Inbox → Subject chain."""
    art_id, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-bc@example.com", subject="brief subject",
    )
    blocks = _json_ld_blocks(client.get(url).data.decode())
    graph = blocks[0]["@graph"]
    bc = next(g for g in graph if g["@type"] == "BreadcrumbList")
    items = bc["itemListElement"]
    assert len(items) == 3
    assert items[0]["position"] == 1
    assert items[0]["name"] == "mimir"
    assert items[0]["item"].endswith("/")
    assert items[1]["position"] == 2
    assert items[1]["name"] == "alpha"
    assert items[1]["item"].endswith("/alpha/")
    assert items[2]["position"] == 3
    assert items[2]["name"] == "brief subject"
    assert items[2]["item"].endswith(f"/{art_id}")


def test_message_breadcrumb_subject_truncated_to_80(client, tmp_path):
    """Breadcrumb item names mirror the <title> truncation budget so
    SERP breadcrumb display doesn't blow out either."""
    long_subject = "y" * 200
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-bclong@example.com", subject=long_subject,
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    bc = next(g for g in graph if g["@type"] == "BreadcrumbList")
    last = bc["itemListElement"][-1]
    assert len(last["name"]) <= 80
    assert last["name"] != long_subject


def test_message_json_ld_author_strips_hidden_placeholder(
    client, tmp_path, monkeypatch,
):
    """JSON-LD author.name on a redacted sender is the display name
    only — no `<hidden>` placeholder. The placeholder is a rendering
    decision for the visible HTML; in structured data it reads as
    broken metadata. Flagged in the 2026-05-12 review."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", [])  # nothing allowlisted
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-named@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "David Woodhouse"


def test_message_json_ld_author_no_email_leak_for_bare_address(
    client, tmp_path, monkeypatch,
):
    """A From: line with only a bare address falls back to a neutral
    string in JSON-LD — not the email itself, which would defeat the
    visible HTML redaction the page already applied."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", [])
    # _ingest_one_article's default `author=a@b.example` is a bare
    # address with no display name.
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-bare@example.com",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "<hidden>" not in posting["author"]["name"]
    assert "@" not in posting["author"]["name"]


def test_message_json_ld_author_strips_email_even_when_allowlisted(
    client, tmp_path, monkeypatch,
):
    """Allowlisted senders surface their full From-line in the visible
    HTML (institutional kernel.org accounts), but JSON-LD's
    author.name is still display-name only — schema.org consumers
    don't need the email and crawlers should treat both author
    surfaces consistently."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", ["@b.example"])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-allow@example.com",
        author="Allowed Person <allowed@b.example>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "Allowed Person"
    assert "@b.example" not in posting["author"]["name"]


def test_message_json_ld_includes_text_snippet(client, tmp_path):
    """Google's DiscussionForumPosting validator requires one of
    `text` / `image` / `video`; we ship `text` derived from the
    parsed body. Search Console flagged this as critical on
    2026-05-14."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-text@example.com",
        body=b"Hello world, this is the body of the message.",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "text" in posting
    assert posting["text"].startswith("Hello world")


def test_message_json_ld_text_truncated_at_word_boundary(client, tmp_path):
    """Long bodies are truncated under JSON_LD_TEXT_MAX so the
    structured-data blob stays lean across the crawl. Truncation
    falls on the last whitespace inside the cap and adds an
    ellipsis."""
    from mimir.web import JSON_LD_TEXT_MAX
    # 4× the cap, all real words so collapsing whitespace doesn't
    # shrink it under the limit.
    body = (b"alpha bravo " * (JSON_LD_TEXT_MAX // 6))
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-text-long@example.com", body=body,
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    text = posting["text"]
    assert text.endswith("...")
    # Trailing ellipsis is 3 chars; the rest must fit under the cap.
    assert len(text) <= JSON_LD_TEXT_MAX + 3
    # No mid-word cut: the char before the ellipsis is a letter
    # (last full word) not a partial token.
    assert text[-4].isalpha()


def test_message_json_ld_text_redacts_dco_trailer_addresses(
    client, tmp_path, monkeypatch,
):
    """The JSON-LD `text` snippet must apply the same DCO trailer
    redaction as the visible HTML — otherwise non-allowlisted
    Signed-off-by addresses leak through structured data even
    though the rendered page redacts them. CONTEXT.md flags
    cross-surface consistency as the rule."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    body = (
        b"text body\n\n"
        b"Signed-off-by: Maintainer <m@kernel.org>\n"
        b"Signed-off-by: Outsider <o@example.com>\n"
    )
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-text-dco@example.com", body=body,
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    text = posting["text"]
    assert "m@kernel.org" in text     # allowlisted survives
    assert "o@example.com" not in text  # non-allowlisted redacted
    assert "<redacted>" in text


def test_message_json_ld_text_omitted_when_body_empty(client, tmp_path):
    """Whitespace-only bodies emit no `text` field rather than an
    empty string, which would re-fail the validator that flagged
    this in the first place."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-text-empty@example.com",
        body=b"   \n\t  \n",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "text" not in posting


def test_message_json_ld_author_has_url(client, tmp_path, monkeypatch):
    """`author.url` points at the per-inbox author view. Search
    Console flagged the missing url as a non-critical issue on
    2026-05-14. The URL uses the canonical inbox (passed through
    from the message view) and percent-encodes the display name so
    spaces and other path-unsafe chars survive intact."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-author-url@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    author = posting["author"]
    assert author["name"] == "David Woodhouse"
    assert author["url"].endswith("/alpha/author/David%20Woodhouse")


def test_message_json_ld_author_url_omitted_for_unknown_sender(
    client, tmp_path, monkeypatch,
):
    """A bare address with no display name renders as
    `unknown sender` (see `_display_name_filter`), which would
    match no one as a substring — omit the URL so we don't ship
    a stable link to a useless query."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "jsonld-author-url-bare@example.com",
        # Bare address → parseaddr returns no display name.
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "url" not in posting["author"]


def test_message_page_visible_html_redacts_non_allowlisted_from_address(
    client, tmp_path, monkeypatch,
):
    """Visible-HTML side of the redaction posture: a non-allowlisted
    From: must surface as `<display-name> <hidden>` on the rendered
    message page, never as the raw address. Unit-level coverage in
    `test_helpers.py` pins `_safe_from_filter` in isolation; this
    pins the template-side wiring (`| safe_from`) — a regression that
    dropped the filter would pass every existing redaction test
    because the structured surfaces (JSON-LD, atom, data-*) have
    their own paths."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "visible-html-redact@example.com",
        author="Joe User <joe@example.com>",
    )
    body = client.get(url).data.decode()
    assert "joe@example.com" not in body
    assert "Joe User" in body
    assert "&lt;hidden&gt;" in body or "<hidden>" in body


def test_message_page_visible_html_surfaces_allowlisted_from_address(
    client, tmp_path, monkeypatch,
):
    """Companion to the redaction test: allowlisted senders DO surface
    their address verbatim on the visible HTML page (kernel.org-shaped
    institutional accounts). Pinning both halves keeps the redaction
    posture explicit."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", ["@b.example"])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "visible-html-allow@example.com",
        author="Allowed Person <allowed@b.example>",
    )
    body = client.get(url).data.decode()
    assert "allowed@b.example" in body
    assert "<hidden>" not in body


def test_message_page_dco_trailer_redacts_non_allowlisted_address(
    client, tmp_path, monkeypatch,
):
    """End-to-end of `_redact_trailer_address`: a `Signed-off-by:` in
    the body must surface allowlisted addresses verbatim and non-
    allowlisted ones as `<redacted>`. CONTEXT.md flags this as one of
    the three display-time redaction invariants; previously had zero
    end-to-end coverage."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    body = (
        b"text body\n\n"
        b"Signed-off-by: Maintainer <m@kernel.org>\n"
        b"Signed-off-by: Outsider <o@example.com>\n"
    )
    _, url = _ingest_one_article(
        tmp_path, "alpha", "dco-redact@example.com", body=body,
    )
    page = client.get(url).data.decode()
    # Allowlisted address survives verbatim.
    assert "m@kernel.org" in page
    # Non-allowlisted address is gone.
    assert "o@example.com" not in page
    # Redacted placeholder is visible.
    assert "redacted" in page


def test_message_page_dco_trailer_no_xss_via_address_metacharacters(
    client, tmp_path, monkeypatch,
):
    """Hostile-trailer XSS: an attacker-controlled DCO trailer with
    HTML metacharacters in the local-part must not land a live tag
    or attribute on the rendered page, even when the substring
    allowlist matches.

    Two defenses are in play and this test pins both:
    1. The email regex no longer matches local-parts containing
       HTML metacharacters (`"`, `<`, `'`, `=`).
    2. Even if it did, the renderer escapes the redactor's return
       value before splicing into output.

    Either defense alone closes the gap; both together is the
    intended posture. Production redactor `_redact_trailer_address`
    is used (no monkeypatch) since it's what the route actually
    wires."""
    import re as _re
    from mimir.config import settings
    monkeypatch.setattr(settings, "email_allowlist", ["kernel.org"])
    # Payload smuggles a real event-handler attribute through the
    # local-part. The `"` would break out of a quoted attribute if
    # the renderer ever rendered this in attribute context; here we
    # want to confirm it never reaches HTML at all.
    body = (
        b"text body\n\n"
        b'Signed-off-by: Attacker <a"onmouseover=alert(1)@kernel.org>\n'
    )
    _, url = _ingest_one_article(
        tmp_path, "alpha", "dco-xss@example.com", body=body,
    )
    page = client.get(url).data.decode()
    # No live tag may carry the smuggled event-handler attribute.
    for tag in _re.findall(r"<[a-zA-Z][^>]*>", page):
        assert "onmouseover" not in tag, (
            f"live tag {tag!r} carries onmouseover from a DCO trailer payload"
        )
    # The raw payload must not appear verbatim in the page; if it
    # rendered as text it would be escaped.
    assert 'a"onmouseover=alert(1)@kernel.org' not in page


def test_search_page_emits_no_json_ld_without_results(client, inbox_name):
    """Empty / too-short / zero-results queries get a bare search
    form, no SearchResultsPage payload — emitting it would tell
    crawlers "this is a results page" when it isn't. The seed
    corpus has no `Linux`-shaped subjects."""
    blocks = _json_ld_blocks(
        client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    )
    assert blocks == []
    # Same for the empty-q and too-short forms.
    assert _json_ld_blocks(client.get(f"/{inbox_name}/search").data.decode()) == []
    assert _json_ld_blocks(client.get(f"/{inbox_name}/search?q=x").data.decode()) == []


def test_search_page_emits_searchresultspage_with_results(client, inbox_name):
    """When the search route renders actual results, a
    `SearchResultsPage` payload appears so crawlers get a structured
    signal. `url` mirrors the `<link rel="canonical">` (bare
    `/<inbox>/search`, no query) so individual `?q=` URLs stay out
    of the index. Suggested in the 2026-05-13 review."""
    blocks = _json_ld_blocks(
        client.get(f"/{inbox_name}/search?q=hello").data.decode()
    )
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "SearchResultsPage"
    assert "hello" in payload["name"]
    assert payload["url"].endswith(f"/{inbox_name}/search")
    assert "?" not in payload["url"]
    assert payload["isPartOf"]["@type"] == "WebSite"
    assert payload["isPartOf"]["name"] == inbox_name


def test_inbox_dashboard_emits_discussion_forum_json_ld(client, inbox_name):
    """The inbox dashboard ships a `DiscussionForum` payload so the
    page reads as a topical hub for crawlers. ItemList of active
    threads is included when threads exist; an empty test corpus
    yields no `mainEntity`, but the type/name/url still appear."""
    blocks = _json_ld_blocks(
        client.get(f"/{inbox_name}/").data.decode()
    )
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "DiscussionForum"
    assert payload["name"] == inbox_name
    assert payload["url"].endswith(f"/{inbox_name}/")


def test_inbox_dashboard_no_trackers_hides_section(client, inbox_name):
    """alpha has tracked_authors=NULL by default — the tracker grid
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


def test_off_list_parent_hint_surfaces_unindexed_list(client, tmp_path):
    """When the thread root has an off-list parent and the article's
    To: header points at a list-shaped address that doesn't match any
    configured inbox, the rendered thread tree should surface that
    address as a hover-only hint so the operator knows which list to
    add. The address rides in `data-tooltip=` rather than visible
    text so the line stays compact."""
    _, url = _ingest_one_article(
        tmp_path, "alpha", "hint-shown@example.com",
        in_reply_to="missing-parent@example.com",
        to="linux-arm-kernel@lists.infradead.org",
    )
    body = client.get(url).data.decode()
    assert "off-list ancestor" in body
    assert (
        'data-tooltip="hint: linux-arm-kernel@lists.infradead.org"' in body
    )
    # Placed below the trigger to escape the .thread-box overflow clip
    # that hides the default top-positioned tooltip on the first row.
    assert 'data-placement="bottom"' in body


def test_off_list_parent_hint_skips_already_configured_lists(client, tmp_path):
    """If the To: address matches a configured inbox's list_address,
    the hint must be suppressed — there's nothing to add, that list
    is already indexed."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.list_address = "linux-arm-kernel@lists.infradead.org"
        s.commit()

    _, url = _ingest_one_article(
        tmp_path, "alpha", "hint-suppressed@example.com",
        in_reply_to="missing-parent-2@example.com",
        to="linux-arm-kernel@lists.infradead.org",
    )
    body = client.get(url).data.decode()
    assert "off-list ancestor" in body
    assert "linux-arm-kernel@lists.infradead.org" not in body


# Meta-sweep (issue #3): canonical, favicon/og:image, ItemList JSON-LD,
# subject normalisation, display_name filter, SITE_BASE_URL override.


def test_canonical_link_on_homepage(client):
    """Every emitted page carries a canonical. Pre-meta-sweep the
    homepage was missing one; now it pins itself."""
    html = client.get("/").data.decode()
    import re as _re
    m = _re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None
    href = m.group(1)
    assert href.endswith("/")
    assert href.startswith("http://") or href.startswith("https://")


def test_canonical_link_on_inbox_dashboard(client, inbox_name):
    html = client.get(f"/{inbox_name}/").data.decode()
    import re as _re
    m = _re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None
    assert m.group(1).endswith(f"/{inbox_name}/")


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


def test_favicon_svg_served(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.mimetype == "image/svg+xml"
    assert b"<svg" in r.data


def test_og_image_png_served(client):
    """The OG image is a pre-baked 1200x630 PNG (no longer an SVG
    rendered from a template -- Twitter/X doesn't render SVG and
    LinkedIn is inconsistent on it; flagged in the 2026-05-12
    review). The asset lives at `mimir/static/img/og-image.png` and
    is served from the site root for URL-shape continuity with the
    prior `/og-image.svg`. The route is bare bytes; we assert the
    PNG signature and the dimensions reflect the baked composite."""
    r = client.get("/og-image.png")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    # PNG magic bytes — defends against the file being silently
    # replaced by something else with a `.png` name.
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR chunk holds width/height in the first 8 bytes after the
    # length+type prefix (16..24). Pin the dimensions; a re-bake at
    # a different size would invalidate the og:image:width/height
    # meta tags emitted in base.html.
    import struct
    width, height = struct.unpack(">II", r.data[16:24])
    assert (width, height) == (1200, 630)


def test_static_assets_carry_cache_control(client):
    """Files served by Flask's built-in /static/* blueprint must carry
    a public Cache-Control with a non-trivial max-age. Flask defaults
    to `no-cache`, which makes every page load re-fetch the JS
    controller (and any future bytes-on-disk asset). The
    `_add_cache_headers` after_request hook is registered on bp_web
    and doesn't run for /static/*, so the only lever is
    SEND_FILE_MAX_AGE_DEFAULT in the app factory."""
    import re
    r = client.get("/static/js/thread-fold.js")
    assert r.status_code == 200
    cc = r.headers.get("Cache-Control", "")
    assert cc.startswith("public"), (
        f"/static/* must be cacheable; got Cache-Control={cc!r}. "
        "Check SEND_FILE_MAX_AGE_DEFAULT in mimir.create_app."
    )
    m = re.search(r"max-age=(\d+)", cc)
    assert m is not None, f"no max-age in Cache-Control={cc!r}"
    # Pin a sane lower bound; bare "max-age=0" or trivially small
    # values would defeat the purpose. Anything >= 1h is fine; the
    # current default is 1 day.
    assert int(m.group(1)) >= 3600


def test_clean_subject_filter_collapses_whitespace():
    from mimir.web import _clean_subject_filter
    assert _clean_subject_filter("a\n  b") == "a b"
    assert _clean_subject_filter("a\tb\r\nc") == "a b c"
    assert _clean_subject_filter("  spaces  ") == "spaces"
    assert _clean_subject_filter(None) == ""
    assert _clean_subject_filter("") == ""


def test_display_name_filter_strips_address(client):
    from mimir.web import _display_name_filter
    assert _display_name_filter("Bob <bob@example.com>") == "Bob"
    assert _display_name_filter("bob@example.com") == "unknown sender"
    assert _display_name_filter(None) == "unknown sender"
    assert _display_name_filter("") == "unknown sender"


def test_site_base_url_override_forces_scheme(monkeypatch):
    """SITE_BASE_URL setting takes precedence over request.url_root —
    the production escape hatch for `http://` leaking through a
    misconfigured proxy chain."""
    from mimir import config
    monkeypatch.setattr(
        config.settings, "site_base_url", "https://forced.example.com",
    )
    from mimir import create_app
    c = create_app().test_client()
    html = c.get("/").data.decode()
    import re as _re
    m = _re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None
    assert m.group(1).startswith("https://forced.example.com/")
    # og:url and og:image also pick up the forced base.
    assert _meta_value(html, "og:url").startswith("https://forced.example.com/")
    assert _meta_value(html, "og:image").startswith("https://forced.example.com/")


# Year browse decade grouping (issue #4).


def test_year_decade_groups_groups_by_decade():
    from mimir.web import _year_decade_groups
    out = _year_decade_groups(1996, 2026)
    decades = [decade for decade, _ in out]
    assert decades == [2020, 2010, 2000, 1990]
    # Each list is descending within the decade.
    assert out[0][1] == [2026, 2025, 2024, 2023, 2022, 2021, 2020]
    assert out[3][1] == [1999, 1998, 1997, 1996]


def test_year_decade_groups_single_decade():
    from mimir.web import _year_decade_groups
    out = _year_decade_groups(2024, 2026)
    assert out == [(2020, [2026, 2025, 2024])]


def test_year_decade_groups_single_year():
    from mimir.web import _year_decade_groups
    assert _year_decade_groups(2025, 2025) == [(2020, [2025])]


def test_year_decade_groups_handles_decade_boundary():
    """1999 → 2010 spans 3 decades; each gets exactly the years that
    belong to it (no off-by-one wrap)."""
    from mimir.web import _year_decade_groups
    out = _year_decade_groups(1999, 2010)
    assert out == [
        (2010, [2010]),
        (2000, [2009, 2008, 2007, 2006, 2005, 2004, 2003, 2002, 2001, 2000]),
        (1990, [1999]),
    ]


def test_year_decade_groups_returns_empty_when_inverted():
    from mimir.web import _year_decade_groups
    assert _year_decade_groups(2026, 1996) == []


def test_inbox_dashboard_year_browse_uses_decade_grouping(client, inbox_name):
    """When an inbox spans more than one decade, the "Browse by year"
    footer must render the `year-decade-list` grouping with one
    `<strong>YYYY0s</strong>` heading per decade.

    Seeded fixtures only cover 2024 — single-decade, so the structure
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
        s.add(ArticleList(
            article_id=art.id, inbox_id=ix.id, epoch="0.git",
            commit_sha="de" * 20,
        ))
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


def test_api_recent_returns_partial_with_article_links(client, inbox_name):
    """The HTMX load-more endpoint returns the `_recent_items.html`
    fragment -- bare <li>s, no <html>/<head> wrapping -- with <a>
    links pointing at messages in this inbox. A response that
    somehow included the full layout would still be `200 OK` but
    would break htmx-target swaps."""
    r = client.get(f"/api/{inbox_name}/recent?offset=0")
    assert r.status_code == 200
    body = r.data.decode()
    # Fragment, not a full page.
    lower = body.lower()
    assert "<html" not in lower
    assert "<head>" not in lower and "<head " not in lower
    assert "<body" not in lower
    # At least one article-link <li>. The exact count depends on
    # seeded data (alpha has 3, fewer than the page size), so we
    # bound the lower side and check the structural shape.
    li_count = body.count("<li")
    assert li_count >= 1, f"no <li> in partial: {body!r}"
    assert f'href="/{inbox_name}/' in body, (
        f"partial must link to /{inbox_name}/<...>: {body!r}"
    )


def test_api_recent_unknown_inbox_returns_404(client):
    """Same 404 contract as the rest of the inbox-scoped routes."""
    assert client.get("/api/no-such-inbox/recent").status_code == 404


def _ingest_with_attachment(
    tmp_path,
    inbox_name: str,
    message_id: str,
    *,
    attachment_filename: str,
    attachment_content_type: str,
    attachment_bytes: bytes,
) -> str:
    """Build + ingest a multipart/mixed message with a single
    base64-encoded attachment. Returns the article URL prefix
    (without `/attachment/...`)."""
    import base64
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.ingest import ingest_epoch
    from mimir.models import Article, Inbox

    b64 = base64.b64encode(attachment_bytes).decode()
    # Chunk to RFC-compatible 76-char lines.
    b64_chunked = "\r\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))

    raw = (
        b"Message-ID: <" + message_id.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: with attachment\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b'Content-Type: multipart/mixed; boundary="bnd"\r\n'
        b"\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"the body\r\n"
        b"--bnd\r\n"
        b"Content-Type: " + attachment_content_type.encode() + b"\r\n"
        b'Content-Disposition: attachment; filename="' + attachment_filename.encode() + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        + b64_chunked.encode() + b"\r\n"
        b"--bnd--\r\n"
    )

    repo_dir = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_dir), mkdir=True)
    blob = Blob.from_string(raw)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"m", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    c = Commit()
    c.tree = tree.id
    c.parents = []
    c.author = c.committer = b"t <t@x>"
    c.commit_time = c.author_time = 1704067200
    c.commit_timezone = c.author_timezone = 0
    c.encoding = b"UTF-8"
    c.message = b"add"
    repo.object_store.add_object(c)
    repo.refs[b"HEAD"] = c.id

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", repo_dir, workers=1)
        art = s.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one()
        return f"/{inbox_name}/{art.date.year}/{art.date.month:02d}/{art.id}"


def test_attachment_download_serves_bytes_with_content_disposition(
    client, tmp_path,
):
    """The download route returns the attachment bytes verbatim with
    a Content-Disposition header carrying the filename. RFC 6266
    `filename*=UTF-8''...` is appended for round-trippability with
    non-ASCII filenames; this test pins the ASCII case (where both
    `filename=` and `filename*=` appear)."""
    payload = b"hello attachment world"
    url = _ingest_with_attachment(
        tmp_path, "alpha",
        "attach-dl@example.com",
        attachment_filename="hello.bin",
        attachment_content_type="application/octet-stream",
        attachment_bytes=payload,
    )
    r = client.get(f"{url}/attachment/0")
    assert r.status_code == 200
    assert r.data == payload
    assert r.mimetype == "application/octet-stream"
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert 'filename="hello.bin"' in cd
    assert "filename*=UTF-8''hello.bin" in cd


def test_attachment_index_out_of_range_returns_404(client, tmp_path):
    """An attachment index past the parsed-list length must 404,
    not raise IndexError or hand back an empty body."""
    url = _ingest_with_attachment(
        tmp_path, "alpha",
        "attach-oob@example.com",
        attachment_filename="x.bin",
        attachment_content_type="application/octet-stream",
        attachment_bytes=b"abc",
    )
    assert client.get(f"{url}/attachment/99").status_code == 404
    assert client.get(f"{url}/attachment/99/preview").status_code == 404


def test_attachment_preview_pygmentizes_text(client, tmp_path):
    """A `.py` attachment is previewable; the response contains the
    Pygments-highlighted output (token spans, not the raw source)
    plus the file's contents recognisably."""
    src = b"def hello():\n    return 'world'\n"
    url = _ingest_with_attachment(
        tmp_path, "alpha",
        "attach-preview@example.com",
        attachment_filename="snippet.py",
        attachment_content_type="text/x-python",
        attachment_bytes=src,
    )
    r = client.get(f"{url}/attachment/0/preview")
    assert r.status_code == 200
    body = r.data.decode()
    # Pygments noclasses=True emits inline style="..." spans.
    assert "<span" in body
    # Source content survives the highlight.
    assert "hello" in body
    assert "world" in body


def test_attachment_preview_falls_back_for_non_previewable(client, tmp_path):
    """A binary attachment (application/octet-stream + no text-like
    extension) renders the "can't preview" path: still 200, but the
    response carries the fallback marker rather than highlighted
    output."""
    url = _ingest_with_attachment(
        tmp_path, "alpha",
        "attach-binary@example.com",
        attachment_filename="payload.bin",
        attachment_content_type="application/octet-stream",
        attachment_bytes=b"\x00\x01\x02\xff",
    )
    r = client.get(f"{url}/attachment/0/preview")
    assert r.status_code == 200
    body = r.data.decode()
    # Filename surfaces somewhere in the not-previewable page.
    assert "payload.bin" in body
    # No Pygments highlighting on a binary blob.
    assert "<span" not in body or "linenos" not in body


def _seed_author_article(inbox_name: str, *, author: str, message_id: str) -> int:
    """Insert one Article row tied to the given inbox with a chosen
    author string. Returns the Article id."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        art = Article(
            message_id=message_id,
            subject="author route subject",
            author=author,
            date=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="author route subject",
        )
        s.add(art)
        s.flush()
        s.add(ArticleList(
            article_id=art.id, inbox_id=ix.id, epoch="0.git",
            commit_sha="aa" * 20,
        ))
        s.commit()
        return art.id


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
    blocks = _json_ld_blocks(
        client.get(f"/{inbox_name}/author/torvalds").data.decode()
    )
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


def test_author_feed_is_well_formed_atom_with_matching_entry(
    client, inbox_name,
):
    """Atom feed for one author: must parse as Atom, carry the
    correct feed-level title for the substring, and include the
    seeded article as an entry."""
    import xml.etree.ElementTree as ET

    _seed_author_article(
        inbox_name,
        author="Atomic Person <atomic-77@example.org>",
        message_id="author-feed@example.com",
    )
    r = client.get(f"/{inbox_name}/author/atomic-77/feed.atom")
    assert r.status_code == 200
    assert "atom" in r.mimetype or r.mimetype == "application/atom+xml"

    root = ET.fromstring(r.data)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.tag.endswith("feed")

    feed_title = root.find("a:title", ns)
    assert feed_title is not None
    assert "atomic-77" in (feed_title.text or "")

    entries = root.findall("a:entry", ns)
    assert len(entries) >= 1
    entry_titles = [e.find("a:title", ns).text or "" for e in entries]
    assert any("author route subject" in t for t in entry_titles)


def test_author_feed_too_short_substring_404s(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/a/feed.atom").status_code == 404
