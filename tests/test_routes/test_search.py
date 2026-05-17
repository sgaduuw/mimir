"""Tests for mimir/web/routes/search.py: the `/<inbox>/search`
endpoint (form rendering, LIKE-substring matching, the
too-short guard, pagination behaviour, meta-description
shape)."""


from tests.test_routes._helpers import _json_ld_blocks, _meta_value, _title_of


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


def test_search_too_short(client, inbox_name):
    """Single-char query: server-side min-length check kicks in;
    response is 200 but renders the 'type at least N characters'
    notice rather than running a search."""
    r = client.get(f"/{inbox_name}/search?q=x")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "characters" in body.lower()


def test_search_title_includes_query(client, inbox_name):
    title = _title_of(
        client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    )
    assert title == f"Search 'Linux' | {inbox_name} | mimir"


def test_search_title_when_no_query(client, inbox_name):
    title = _title_of(client.get(f"/{inbox_name}/search").data.decode())
    assert title == f"Search | {inbox_name} | mimir"


def test_search_meta_description_with_query(client, inbox_name):
    html = client.get(f"/{inbox_name}/search?q=Linux").data.decode()
    desc = _meta_value(html, "description")
    assert desc == f"Search results for 'Linux' in {inbox_name}."


def test_search_meta_description_when_no_query(client, inbox_name):
    html = client.get(f"/{inbox_name}/search").data.decode()
    desc = _meta_value(html, "description")
    assert desc == f"Search the {inbox_name} archive by subject or author."


def test_search_page_emits_no_json_ld_without_results(client, inbox_name):
    """Empty / too-short / zero-results queries get a bare search
    form, no SearchResultsPage payload, emitting it would tell
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
