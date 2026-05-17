"""Cross-cutting route tests: security headers (CSP, HSTS,
Permissions-Policy, XFO, XCTO, Referrer-Policy), access-log
shape, request-id round-trip, ProxyFix X-Forwarded-* handling,
keyboard-nav `data-nav-up` and the kbd-help dialog rendered
on every page, the external-stylesheet contract, and small
helper / filter unit tests imported from mimir.web."""


import pytest
from tests.test_routes._helpers import _build_app_with_hops, _ingest_one_article, _meta_value, _parse_csp


def test_pages_use_external_stylesheet_not_inline_style_blocks(client):
    """#228: CSS lives in `mimir/static/css/mimir.css`, loaded via a
    single `<link rel="stylesheet">` in `base.html`. Pages must NOT
    carry `<style>` blocks (that's the inline form the extraction
    targeted, and the form whose presence would force CSP to keep
    `'unsafe-inline'` on `style-src`).

    Per-element `style="..."` attributes are pinned separately by
    `test_pages_emit_no_inline_style_attributes`."""
    import re as _re
    # Sample a handful of routes covering every base template path
    # the inline blocks used to live in (index.html, message.html
    # via the seeded fixture pages, inbox.html for the dashboard).
    routes = ["/", "/alpha/", "/alpha/today", "/alpha/yesterday"]
    for path in routes:
        body = client.get(path).data.decode()
        assert "<style>" not in body, (
            f"`{path}` still emits an inline `<style>` block:\n"
            + body[body.index("<style>"):body.index("</style>") + 8]
        )
        # And the external sheet IS linked with the cache-bust.
        link_re = _re.compile(
            r'<link rel="stylesheet"\s+href="[^"]*'
            r'/static/css/mimir\.css\?v=[^"]+"'
        )
        assert link_re.search(body), (
            f"`{path}` missing the external mimir.css link tag"
        )


def test_pages_emit_no_inline_style_attributes(client, tmp_path):
    """Security pass: no rendered page carries a `style="..."` attr.
    Inline styles would force CSP `style-src` to keep `'unsafe-inline'`,
    which widens the XSS blast radius (an attacker who finds an HTML-
    escape gap can land a `<span style="...">` payload that runs CSS
    expressions / leaks data via CSS exfil tricks).

    Sweeps every template that historically carried inline styles:
    `/` (index hero + cards), `/<inbox>/` (dashboard sparkline),
    `/<inbox>/<year>/` (month tiles), `/<inbox>/<year>/<mm>/<id>`
    (thread tree depth, patch-state aside, body-text block,
    keyboard-help dialog from base.html on every page).
    """
    _, msg_url = _ingest_one_article(
        tmp_path, "alpha", "inline-style-sweep@example.com",
    )
    routes = [
        "/",
        "/alpha/",
        "/alpha/today",
        "/alpha/yesterday",
        "/alpha/2024/",
        "/alpha/2024/01/",
        msg_url,
    ]
    for path in routes:
        body = client.get(path).data.decode()
        assert 'style="' not in body, (
            f"`{path}` carries an inline style attribute, "
            "which forces CSP to keep `'unsafe-inline'` on "
            "`style-src`:\n"
            + body[max(0, body.index('style="') - 80):
                   body.index('style="') + 200]
        )


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


def test_unknown_inbox_since_404(client):
    assert (
        client.get("/nonexistent/since/2024-01-01").status_code == 404
    )


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
        "Permissions-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert r.headers.get(h), f"missing header {h}"


def test_security_headers_present_on_unmatched_404(client):
    """The audit (2026-05-15) flagged that `@bp_web.after_request`
    only fires for routes that matched the blueprint -- URLs with no
    matching pattern at all (Flask's built-in 404) bypassed CSP,
    X-Frame-Options, X-Content-Type-Options, and the structured
    access-log line. Hooks moved to `before_app_request` /
    `after_app_request` so they fire on every request the app sees.

    A URL with 5+ path segments doesn't match any current route
    (the deepest patterns are 4-segment `<inbox>/<yyyy>/<mm>/<id>`),
    so Flask responds 404 before any blueprint endpoint is picked."""
    r = client.get("/no/such/route/exists/here")
    assert r.status_code == 404
    for h in (
        "Content-Security-Policy",
        "Permissions-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert r.headers.get(h), (
            f"missing header {h} on unmatched 404 -- "
            "before_app_request/after_app_request hooks regressed"
        )
    # X-Request-Id is populated by the before_app_request hook;
    # absence (or the "-" placeholder) means the hook didn't fire.
    rid = r.headers.get("X-Request-Id")
    assert rid and rid != "-", (
        f"X-Request-Id={rid!r}; before_app_request didn't run"
    )


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

    # style-src: the security pass dropped `'unsafe-inline'`. The
    # negative pin guards against a regression that re-introduces an
    # inline `<style>` block or `style="..."` attr and "fixes" it
    # the wrong way by re-allowing inline styles.
    style_src = directives.get("style-src", [])
    assert style_src, "style-src directive missing"
    assert "'unsafe-inline'" not in style_src, (
        "style-src must not allow 'unsafe-inline'; the security pass "
        "dropped it after sweeping the inline `style=`/`<style>` "
        "usage into `mimir/static/css/mimir.css`."
    )

    # Other crucial directives that block embed/iframe abuse.
    assert directives.get("frame-ancestors") == ["'none'"]
    assert directives.get("base-uri") == ["'self'"]

    # Other headers are short enough to pin exactly.
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_csp_script_src_pins_specific_htmx_version(client):
    """`script-src` carries a version-pinned unpkg path
    (`https://unpkg.com/htmx.org@<X.Y.Z>/`), not the bare unpkg.com
    origin. The version-pinned form means an htmx bump in
    `base.html` MUST update the CSP in lockstep; the bare origin
    would silently allow any unpkg package or version to load.

    Pinned via a positive allowlist over every source token: each
    must be either `'self'` or match the anchored htmx-version
    regex. Any other shape (a stray `unpkg.com` bare origin,
    `cdn.example.com`, …) fails the pin. Anchored-regex checks
    avoid CodeQL's `py/incomplete-url-substring-sanitization`
    rule, which flags substring / startswith / endswith URL
    comparisons indiscriminately.
    """
    import re
    r = client.get("/")
    directives = _parse_csp(r.headers.get("Content-Security-Policy", ""))
    script_src = directives.get("script-src", [])
    htmx_pin = re.compile(r"^https://unpkg\.com/htmx\.org@\d+\.\d+\.\d+/$")
    allowed = re.compile(r"^'self'$")
    for src in script_src:
        ok = bool(allowed.match(src) or htmx_pin.match(src))
        assert ok, (
            f"script-src source {src!r} does not match the pinned "
            f"allowlist (`'self'` or `{htmx_pin.pattern}`); full "
            f"script-src was {script_src!r}"
        )
    # And the pinned htmx source must actually be present, not just
    # be allowed.
    assert any(htmx_pin.match(s) for s in script_src), (
        "script-src missing the version-pinned htmx source matching "
        f"`{htmx_pin.pattern}`; full script-src was {script_src!r}"
    )


def test_permissions_policy_denies_powerful_features(client):
    """mimir is a read-only archive; none of the powerful features
    (camera, microphone, geolocation, payment, etc.) have a use
    case here. Permissions-Policy must explicitly deny them with
    empty allowlists `()`, both in the document and any embedded
    subframe.

    Pin the set of denies that would matter under a future XSS-
    gated bug: an injected `<iframe>` or `<embed>` that tries to
    activate camera/mic/geolocation must be blocked by policy, not
    just by `frame-ancestors`-style assumptions."""
    r = client.get("/")
    pp = r.headers.get("Permissions-Policy", "")
    assert pp, "Permissions-Policy header missing"
    # Parse `directive=(allowlist)` pairs; the directive name is what
    # we pin, the `()` empty allowlist is the deny shape.
    by_directive = {}
    for chunk in pp.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            name, val = chunk.split("=", 1)
            by_directive[name.strip()] = val.strip()
    # The high-impact features that an XSS would otherwise abuse.
    must_deny = [
        "camera",
        "microphone",
        "geolocation",
        "payment",
        "usb",
        "midi",
        "magnetometer",
        "accelerometer",
    ]
    for feature in must_deny:
        assert by_directive.get(feature) == "()", (
            f"Permissions-Policy must deny `{feature}` with an empty "
            f"allowlist `()`; got {by_directive.get(feature)!r}"
        )


def test_hsts_emitted_only_on_https_requests(client):
    """HSTS is the one pin-the-browser-forever header in the bundle.
    It must NOT emit on plain-HTTP dev sessions (where it would brick
    the local workflow) and MUST emit on requests forwarded with
    X-Forwarded-Proto: https. max-age, includeSubDomains, and the
    `preload` directive are part of the contract; a refactor that
    drops any of them would silently weaken the production posture.
    """
    # Without the proxy header: no HSTS.
    r_plain = client.get("/")
    assert "Strict-Transport-Security" not in r_plain.headers

    # With X-Forwarded-Proto: https: HSTS emitted with the pinned value.
    r_https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    hsts = r_https.headers.get("Strict-Transport-Security", "")
    assert hsts, "HSTS must emit when X-Forwarded-Proto=https"
    assert "max-age=" in hsts
    # max-age must be >= 6 months (15768000s); under that, browsers
    # treat the policy as advisory rather than enforced. The
    # hstspreload.org submission requirement is >= 1 year (31536000s).
    import re
    m = re.search(r"max-age=(\d+)", hsts)
    assert m is not None
    assert int(m.group(1)) >= 31_536_000, (
        "max-age must be >= 1y to satisfy hstspreload.org submission"
    )
    assert "includeSubDomains" in hsts
    # `preload` opts the site into the browser-bundled HSTS preload
    # list. Removing this would walk back the security pass's
    # ratchet (un-preloading takes months) without an explicit code
    # signal; the assertion forces a deliberate edit if dropped.
    assert "preload" in hsts


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
    """With hops=0, XFF is NOT honoured, request.remote_addr stays
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


def test_keyboard_nav_script_loaded_on_every_page(client, inbox_name):
    """keyboard-nav.js (vim-style hjkl + ? for help) ships on every
    rendered page via base.html. Deferred (no FOUC concern; the
    controller binds on document keydown after parse)."""
    for path in ("/", f"/{inbox_name}/", f"/{inbox_name}/today"):
        body = client.get(path, follow_redirects=True).data.decode()
        assert 'src="/static/js/keyboard-nav.js"' in body, (
            f"keyboard-nav.js missing on {path!r}"
        )
        # `defer` keeps the load asynchronous, unlike thread-fold.js,
        # which is synchronous to dodge FOUC.
        assert 'src="/static/js/keyboard-nav.js" defer' in body


def test_keyboard_help_dialog_present_on_every_page(client, inbox_name):
    """The `?` key opens a <dialog id="keyboard-help">, must be
    rendered on every page so the binding works everywhere, not
    just inside an inbox."""
    for path in ("/", f"/{inbox_name}/"):
        body = client.get(path, follow_redirects=True).data.decode()
        assert 'id="keyboard-help"' in body, (
            f"keyboard-help dialog missing on {path!r}"
        )
        # Each binding row carries its <kbd> label, pin the full set
        # so a refactor that drops a row is caught.
        for key in ("h", "j", "k", "l", "Esc", "?"):
            assert f"<kbd>{key}</kbd>" in body


def test_keyboard_nav_data_nav_up_meta_index_has_no_parent(client):
    """`/` is the top, no parent, `h` is a no-op. The attribute is
    omitted entirely (not set to empty) so the JS short-circuits on
    `getAttribute` returning null."""
    body = client.get("/").data.decode()
    assert "data-nav-up=" not in body


def test_keyboard_nav_data_nav_up_inbox_dashboard_points_at_root(
    client, inbox_name,
):
    """The inbox dashboard's `h` key goes up to the meta-index."""
    body = client.get(f"/{inbox_name}/").data.decode()
    assert 'data-nav-up="/"' in body


def test_keyboard_nav_data_nav_up_message_page_points_at_inbox(
    client, tmp_path, inbox_name,
):
    """A message page's `h` key goes up to the inbox dashboard. Pins
    the "back out one level" mental model the issue specifies."""
    _, url = _ingest_one_article(
        tmp_path, inbox_name, "kbd-nav-up@example.com",
    )
    body = client.get(url).data.decode()
    assert f'data-nav-up="/{inbox_name}/"' in body


def test_keyboard_nav_data_nav_up_daily_view_points_at_inbox(
    client, inbox_name,
):
    """The daily view is a leaf surface inside the inbox, `h` goes
    back to the dashboard, not all the way to `/`. Pins the
    `is_inbox_root` discriminator on `request.path`."""
    body = client.get(f"/{inbox_name}/today").data.decode()
    assert f'data-nav-up="/{inbox_name}/"' in body


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
    """SITE_BASE_URL setting takes precedence over request.url_root  
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
