"""Tests for mimir/web/routes/message.py: the article view
(thread tree, patch-state card, related-patches, JSON-LD
DiscussionForumPosting + Breadcrumb, ETag-based conditional
revalidation, body redactions, off-list-parent hints, the
subject-normalized fallback grouping, and the 4-tuple URL
identity contract)."""

from tests.test_routes._helpers import (
    _data_attr_values,
    _ingest_one_article,
    _json_ld_blocks,
    _seed_mainline_commit,
    _seed_subsystem,
    _seed_three_message_thread,
    _title_of,
)


def test_message_url_four_tuple_identity_404s_on_mismatch(
    client,
    tmp_path,
):
    """`/<inbox>/<YYYY>/<MM>/<article_id>` is a 4-tuple identity:
    inbox + year + month + id must ALL match the article's storage
    or the route 404s. CONTEXT.md (URL scheme) is explicit, "URLs
    either resolve exactly or don't resolve at all", but the
    in-range component tests only catch impossible values
    (year 1990, month 0/13). The mismatched-but-plausible case
    (right id, wrong year/month/inbox) is unpinned, and that's the
    case a regression introducing a 301-to-canonical helper would
    quietly break.

    Ingest a real article and then probe every mismatched corner."""
    art_id, real_url = _ingest_one_article(
        tmp_path,
        "alpha",
        "four-tuple-identity@example.com",
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
    assert "beta" != "alpha"  # sanity, beta exists in seed

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


def test_message_subject_truncated_to_80(client, tmp_path):
    """Long subjects (patch series with v17 RFC 23/47 etc.) get
    truncated at 80 chars in <title> so SERPs don't overflow.
    Drives the real route end-to-end with a freshly-ingested article
    rather than testing Jinja's filter in isolation."""
    long_subject = "x" * 200
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "trunc@example.com",
        subject=long_subject,
    )
    r = client.get(url)
    assert r.status_code == 200
    title = _title_of(r.data.decode())
    assert title.endswith(" | alpha | mimir")
    # The subject portion shouldn't be the full 200-char input.
    subject_part = title.rsplit(" | alpha | mimir", 1)[0]
    assert len(subject_part) <= 84  # Jinja truncate(80) keeps ~81 incl ellipsis
    assert subject_part != long_subject


def test_thread_summary_helper_counts_and_relative_time(frozen_clock):
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


def test_message_page_emits_vary_hx_request(client, tmp_path):
    """The message endpoint returns either the full page or just the
    `_message_body.html` partial depending on `HX-Request`. Without
    `Vary: HX-Request`, an intermediary cache (Cloudflare, browser
    bfcache, Chrome prerender cache) can serve a partial response
    to a regular navigation (page renders as just the <article>,
    no chrome) or a full page to an HTMX swap (full page chrome
    duplicates into #msg). Both observed on the production deploy
    on 2026-05-14. The Vary header keys the two response shapes
    separately so caches treat them as distinct entities."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "vary-test@example.com",
        subject="vary",
    )
    # Full-page response.
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers.get("Vary") == "HX-Request"
    # HTMX partial response.
    r_hx = client.get(url, headers={"HX-Request": "true"})
    assert r_hx.status_code == 200
    assert r_hx.headers.get("Vary") == "HX-Request"


def test_message_page_single_message_thread_skips_fold_scaffolding(
    client,
    tmp_path,
):
    """When a thread has exactly one message and no off-list parent,
    the whole `.thread-context` block is omitted: no fold scaffolding,
    no toolbar, no `<html>`-level data attrs, no fold-controller
    script context."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "fold-solo@example.com",
        subject="solo",
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


def test_message_page_thread_fold_context_is_root_when_viewing_root(
    client,
    tmp_path,
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
    client,
    tmp_path,
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
    client,
    tmp_path,
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
        r'<li[^>]*data-article-id="'
        + re.escape(str(nested_id))
        + r'"[^>]*class="is-active"',
        re.DOTALL,
    )
    assert li_active_pattern.search(body) is not None, (
        "expected active <li> for current message; markup was: "
        + body[body.find("thread-list") : body.find("</ul>")]
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


def test_message_page_thread_tree_uses_data_depth_not_inline_style(
    client,
    tmp_path,
):
    """Thread-tree depth used to be encoded as
    `<li style="padding-left: {N*1.5}rem">`; the security pass moved
    it to `<li data-depth="N">` so the static CSS ladder in
    `mimir.css` can apply `padding-left` without an inline style.
    The ladder is enumerated 0..20 in the stylesheet; values beyond
    20 are clamped in the template so they still match a rule.

    The 3-message thread `_seed_three_message_thread` yields depths
    0, 1, 2 -- inside the ladder. Pins (a) the new attribute is
    present on every tree `<li>`, (b) no `<li>` in the tree carries
    an inline `style="..."` attr, (c) the depths attached match the
    thread shape (0/1/2 in this fixture).
    """
    import re

    msgs = _seed_three_message_thread(tmp_path, "alpha")
    body = client.get(msgs["root"][1]).data.decode()

    ul_start = body.index('<ul class="thread-list"')
    ul_end = body.index("</ul>", ul_start)
    list_block = body[ul_start:ul_end]

    lis = re.findall(r"<li[^>]*>", list_block)
    assert len(lis) == 3, f"expected 3 tree <li>, got {len(lis)}"
    # Each <li> carries data-depth=, none carries an inline style.
    for li in lis:
        assert "data-depth=" in li, f"thread-tree <li> missing data-depth attr: {li!r}"
        assert "style=" not in li, (
            f"thread-tree <li> still carries inline style: {li!r}"
        )
    # Depths attached should be exactly {0, 1, 2} for this fixture.
    depths = sorted(int(d) for d in re.findall(r'data-depth="(\d+)"', list_block))
    assert depths == [0, 1, 2], (
        f"expected depths [0,1,2] for the 3-msg thread, got {depths}"
    )


def test_message_page_thread_fold_toolbar_summary_counts_match(
    client,
    tmp_path,
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
        body,
        re.DOTALL,
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
    client,
    tmp_path,
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
    anchors = re.findall(r"<a[^>]*>", list_block)
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
    client,
    tmp_path,
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
        tmp_path,
        "alpha",
        "htmx-swap@example.com",
        subject="swap test subject 12345",
    )
    r = client.get(url, headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.data.decode()

    # Outer shape: hx-swap=outerHTML on the link targets #msg, so the
    # response's root element must be <article id="msg" data-article-id=...>.
    assert body.lstrip().startswith('<article id="msg"'), (
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
    client,
    tmp_path,
):
    """Full page render and HX-Request render must contain identical
    content inside <article id="msg">. Regression guard: a refactor
    that diverges the two render paths (e.g. one uses _message_body.html
    and the other inlines) would silently break HTMX swap parity."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "parity@example.com",
        subject="parity check",
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
        '<article id="msg"> contents; divergence breaks intra-thread '
        "swap parity."
    )


def test_message_page_htmx_request_on_unknown_message_404s(
    client,
    tmp_path,
):
    """HTMX requests at non-existent URLs must still 404 (not return an
    empty 200 partial). HTMX surfaces this to htmx:responseError so the
    client can react; a silent 200 with empty body would render a blank
    panel where the message should be."""
    # Seed one valid article so the inbox exists with a real mirror.
    _ingest_one_article(
        tmp_path,
        "alpha",
        "valid@example.com",
        subject="valid",
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
        tmp_path,
        "alpha",
        "jsonld-fp@example.com",
        subject="hello world",
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


def test_message_page_shows_subsystem_header_for_patch(client, tmp_path):
    """A patch article whose touched-paths match a Subsystem
    surfaces the section name + maintainer on the rendered page.
    Pins the slice-3 happy path: subsystem_hits flows from view
    to template, the <details> block renders."""
    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    patch_body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "subsys-patch@example.com",
        body=patch_body,
    )
    body = client.get(url).data.decode()
    # Subsystem info renders inside the article <header> alongside
    # From / Date, it's identity metadata, not a floating aside.
    # Maintainer name shown, no address, no role tag. Detail moved
    # to MAINTAINERS-driven per-subsystem dashboards (issue #72).
    assert "<strong>Subsystem:</strong>" in body
    # Display lowercased (anti-shouty) since the post-1.18 UI pass.
    assert "bcachefs" in body
    assert "Kent Overstreet" in body
    assert "Maintainer" in body
    assert "kent.overstreet@kernel.org" not in body
    assert "<kbd>M</kbd>" not in body


def test_message_page_no_subsystem_block_when_no_match(client, tmp_path):
    """A patch touching paths no Subsystem claims renders without
    the Subsystem header line."""
    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
    )
    patch_body = (
        b"diff --git a/fs/unrelated/file.c b/fs/unrelated/file.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "no-match@example.com",
        body=patch_body,
    )
    body = client.get(url).data.decode()
    assert "<strong>Subsystem:</strong>" not in body


def test_message_page_no_subsystem_block_for_prose_only(client, tmp_path):
    """A discussion-only article (no diff in body) has no
    ArticleFile rows, so no Subsystem header line and no
    related-patches block."""
    _seed_subsystem("BCACHEFS", "Maintained", files=["fs/bcachefs/"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "prose@example.com",
        body=b"just a discussion, no diff\n",
    )
    body = client.get(url).data.decode()
    assert "<strong>Subsystem:</strong>" not in body
    assert "Other recent patches touching" not in body


def test_message_page_shows_related_patches_touching_same_file(
    client,
    tmp_path,
):
    """When two patches touch the same file, viewing one surfaces
    the other in the "Other recent patches touching these files"
    section. The current article is filtered out of its own
    sidebar.

    The article we view (second) is the one whose mirror_path
    `_ingest_one_article` left in place; that's the one the route
    can re-parse via `read_message`. The first article only needs
    its ArticleFile rows to land for the related-patches reverse
    lookup, which doesn't re-read the blob."""
    patch_body = (
        b"diff --git a/fs/shared/file.c b/fs/shared/file.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    # Ingest each into its own tmp subdir; `_ingest_one_article`
    # repoints the inbox's mirror_path each call, so only the
    # SECOND article's blob is reachable for the message view.
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    _, first_url = _ingest_one_article(
        tmp_path / "first",
        "alpha",
        "first@example.com",
        body=patch_body,
        subject="first touching shared",
    )
    _, second_url = _ingest_one_article(
        tmp_path / "second",
        "alpha",
        "second@example.com",
        body=patch_body,
        subject="second touching shared",
    )
    body = client.get(second_url).data.decode()
    assert "Other recent patches touching" in body
    assert "first touching shared" in body
    # Self-exclusion: the current article's subject isn't in the
    # related-patches block.
    related_section = body.split("Other recent patches touching")[1]
    assert "second touching shared" not in related_section


def test_message_page_lore_url_in_body_gets_local_mirror_link(
    client,
    tmp_path,
):
    """When a message body links to lore.kernel.org/<slug>/<msgid>
    and mimir has that msgid in *any* indexed inbox, the route
    builds `lore_mirror_urls` and the renderer appends a
    `(local)` link routed to the canonical mimir URL alongside
    the original lore anchor. Lore link stays present (we add,
    don't replace)."""
    # First ingest the referenced article so mimir has it indexed.
    (tmp_path / "ref").mkdir()
    (tmp_path / "view").mkdir()
    referenced_msgid = "referenced@x.invalid"
    _, ref_url = _ingest_one_article(
        tmp_path / "ref",
        "alpha",
        referenced_msgid,
        subject="the referenced one",
    )
    # Then ingest the message whose body links out to lore for the
    # referenced one. `_ingest_one_article` repoints the inbox's
    # mirror_path, so this second message is the one the view can
    # re-parse via `read_message`.
    body_bytes = (
        f"see https://lore.kernel.org/alpha/{referenced_msgid}/ for context"
    ).encode()
    _, view_url = _ingest_one_article(
        tmp_path / "view",
        "alpha",
        "viewer@x.invalid",
        body=body_bytes,
        subject="referrer",
    )
    page = client.get(view_url).data.decode()
    # The lore anchor survives verbatim.
    assert f'href="https://lore.kernel.org/alpha/{referenced_msgid}/"' in page
    # The local mirror anchor is appended, routed to the referenced
    # article's per-inbox URL.
    assert f'href="{ref_url}">local</a>' in page


def test_message_page_lore_url_unknown_msgid_no_mirror_link(
    client,
    tmp_path,
):
    """Inverse: a lore URL pointing at a msgid mimir doesn't have
    renders as the bare external anchor, no `(local)` suffix."""
    _, view_url = _ingest_one_article(
        tmp_path,
        "alpha",
        "lone@x.invalid",
        body=b"see https://lore.kernel.org/alpha/never-ingested@x.invalid/ here",
        subject="solo with lore link",
    )
    page = client.get(view_url).data.decode()
    assert 'href="https://lore.kernel.org/alpha/never-ingested@x.invalid/"' in page
    assert "local</a>" not in page


def test_message_page_short_thread_does_not_get_sidebar_class(client, tmp_path):
    """Short threads (below LONG_THREAD_SIDEBAR_THRESHOLD) keep the
    above-body layout, the sidebar modifier class is absent so the
    CSS grid rule doesn't fire."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "short-thread@example.com",
        subject="solo",
    )
    body = client.get(url).data.decode()
    assert 'class="message-page-grid"' in body
    assert "message-page-grid--with-sidebar" not in body


def test_message_page_long_thread_gets_sidebar_class(client, tmp_path):
    """Threads at or above LONG_THREAD_SIDEBAR_THRESHOLD (20 by
    default) get the `--with-sidebar` modifier so the CSS media
    query switches to the right-rail layout on wide viewports."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from mimir.web import LONG_THREAD_SIDEBAR_THRESHOLD
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    # Build a thread of `threshold` messages all chained off one root.
    # The root article goes through the real ingest path so the
    # message view can read its body via the git mirror; the replies
    # are SQL-only -- they only need to inflate the thread count, the
    # view doesn't read their bodies.
    _, root_url = _ingest_one_article(
        tmp_path,
        "alpha",
        "long-root@example.com",
        subject="long thread root",
    )
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i in range(LONG_THREAD_SIDEBAR_THRESHOLD - 1):
            r = Article(
                message_id=f"long-r{i}@example.com",
                subject=f"Re: long thread root [{i}]",
                author="r@example",
                date=datetime(2024, 1, 1, 12, i, tzinfo=timezone.utc),
                thread_parent="long-root@example.com",
                subject_normalized="long thread root",
                lists=[
                    ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="d" * 40)
                ],
            )
            s.add(r)
        s.commit()
    body = client.get(root_url).data.decode()
    assert "message-page-grid--with-sidebar" in body


def test_message_page_renders_hunk_quote_with_jump_to_parent(client, tmp_path):
    """A reply that quotes a patch hunk from its parent renders
    the quote folded inside `<details class="hunk-quote">` with a
    `↗ jump to hunk` link pointing at the parent message's URL.
    Pins the issue-68 slice-1 end-to-end: parser → renderer →
    template, with the view computing `parent_url` from
    `article.thread_parent` via the thread's URL map."""
    parent_dir = tmp_path / "parent"
    reply_dir = tmp_path / "reply"
    parent_dir.mkdir()
    reply_dir.mkdir()
    _, parent_url = _ingest_one_article(
        parent_dir,
        "alpha",
        "patch-parent@example.com",
        subject="[PATCH] foo: fix bar",
        body=(
            b"This adds a thing.\n\n"
            b"diff --git a/foo b/foo\n"
            b"@@ -1,3 +1,3 @@\n"
            b" int main(void) {\n"
            b"-    return 0;\n"
            b"+    return 1;\n"
            b" }\n"
        ),
    )
    _, reply_url = _ingest_one_article(
        reply_dir,
        "alpha",
        "patch-reply@example.com",
        subject="Re: [PATCH] foo: fix bar",
        in_reply_to="patch-parent@example.com",
        body=(
            b"On Mon, Alice wrote:\n"
            b"> @@ -1,3 +1,3 @@\n"
            b">  int main(void) {\n"
            b"> -    return 0;\n"
            b"> +    return 1;\n"
            b">  }\n\n"
            b"Looks good, but consider returning -1 instead.\n"
        ),
    )
    body = client.get(reply_url).data.decode()
    assert '<details class="hunk-quote">' in body
    assert "quoted hunk" in body
    assert "↗ jump to hunk" in body
    assert f'href="{parent_url}"' in body


def test_message_page_hunk_quote_omits_jump_link_when_parent_off_list(
    client,
    tmp_path,
):
    """A reply whose parent isn't in this archive (off-list ancestor)
    has no resolvable `parent_url`. The fold still happens, but the
    jump-to-hunk link is omitted rather than pointing at a dead URL."""
    _, reply_url = _ingest_one_article(
        tmp_path,
        "alpha",
        "orphan-reply@example.com",
        subject="Re: missing patch",
        in_reply_to="off-list-patch@example.com",
        body=(b"> @@ -1 +1 @@\n> -a\n> +b\n\nMy comment.\n"),
    )
    body = client.get(reply_url).data.decode()
    assert '<details class="hunk-quote">' in body
    assert "↗ jump to hunk" not in body


def test_maintainer_listed_address_surfaces_in_from_line(
    client,
    tmp_path,
):
    """A From-line address that isn't in the static
    `email_allowlist` BUT is listed as M:/R: in `subsystem_maintainers`
    surfaces verbatim. Pins the dynamic-allowlist union behaviour
    on the From-line render path."""
    # Seed a maintainer entry for an address outside the static
    # allowlist (no kernel.org / torvalds@ / gregkh@ tokens).
    from mimir import maintainer_allowlist
    from mimir.extensions import SessionLocal
    from mimir.models import Subsystem, SubsystemMaintainer

    with SessionLocal() as s:
        sub = Subsystem(name="OUTSIDE-CONTRIB", status="Maintained")
        s.add(sub)
        s.flush()
        s.add(
            SubsystemMaintainer(
                subsystem_id=sub.id,
                role="M",
                name="Outside Contributor",
                address="contrib@somecorp.example",
            )
        )
        s.commit()
    maintainer_allowlist.invalidate()

    # Ingest one article with that address as the sender.
    _ingest_one_article(
        tmp_path,
        "alpha",
        "from-test@example.com",
        subject="patch from outside contributor",
        author="Outside Contributor <contrib@somecorp.example>",
    )
    # The author surfaces on the inbox dashboard recent list.
    body = client.get("/alpha/").data.decode()
    assert "contrib@somecorp.example" in body, (
        "MAINTAINERS-derived address should bypass <hidden> redaction"
    )


def test_non_maintainer_address_still_redacted(client, tmp_path):
    """Inverse: an address neither in the static allowlist nor
    MAINTAINERS still goes through the `<hidden>` placeholder.
    Without this, the union check would be silently leaking and we
    couldn't tell."""
    from mimir import maintainer_allowlist

    maintainer_allowlist.invalidate()  # ensure empty set
    _ingest_one_article(
        tmp_path,
        "alpha",
        "from-test-2@example.com",
        subject="patch from random",
        author="Random Sender <rando@somecorp.example>",
    )
    body = client.get("/alpha/").data.decode()
    assert "rando@somecorp.example" not in body
    # Display name + <hidden> placeholder is the documented shape.
    assert "Random Sender" in body


def test_message_page_shows_applied_as_when_mainline_commit_matches(
    client,
    tmp_path,
):
    """A patch whose Message-ID matches a `mainline_commits` row
    surfaces a "Landed:" line on the state card. Pins the
    issue-66 happy path: walker -> DB -> render.

    Card-gated on `is_patch`, so a non-patch article with a
    mainline_commits row would NOT render here. The mainline
    walker's Link: trailers point at actual patches in practice,
    so this is the realistic scenario."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "applied-msg@example.com",
        subject="[PATCH 1/2] foo: do bar",
    )
    _seed_mainline_commit(
        message_id="applied-msg@example.com",
        commit_sha="abc1234567890def1234567890abcdef12345678",
    )
    body = client.get(url).data.decode()
    assert 'class="patch-state"' in body
    assert "Landed:" in body
    # SHA truncated to first 12 chars on display.
    assert "<code>abc123456789</code>" in body
    # Tree is now labelled via "Applied to <strong>…</strong>", not
    # a bare <code> tag. The slug "linus" still appears in the body
    # (as the tree_label fallback) but not wrapped in <code>.
    assert "linus" in body
    assert "<code>linus</code>" not in body


def test_message_page_no_applied_as_when_no_commit_matches(
    client,
    tmp_path,
):
    """Articles without a mainline-commit reference render no
    "Landed:" line in the state card. Absence is non-informative
    (may simply not be indexed yet); the row is opt-in."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "unapplied@example.com",
        subject="[PATCH 1/2] foo: in flight",
    )
    body = client.get(url).data.decode()
    assert "Landed:" not in body
    # Old wording from the pre-#208 standalone aside; must not regress.
    assert "Applied as" not in body


def test_message_page_shows_multiple_applied_as_when_commit_carries_multiple_links(
    client,
    tmp_path,
):
    """When a commit references the article via two `Link:` trailers
    (rare), or when two distinct commits apply the same patch (less
    rare on backports), every mainline_commits row gets surfaced.
    Ordered by committed_at asc, the first application is the
    primary one."""
    from datetime import datetime, timezone

    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "multi-app@example.com",
        subject="[PATCH 1/2] foo: backported",
    )
    _seed_mainline_commit(
        message_id="multi-app@example.com",
        commit_sha="11" * 20,
        date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    _seed_mainline_commit(
        message_id="multi-app@example.com",
        commit_sha="22" * 20,
        date=datetime(2024, 7, 1, tzinfo=timezone.utc),
    )
    body = client.get(url).data.decode()
    # Both shas appear, ordered by date asc (June before July).
    first_idx = body.index("<code>111111111111</code>")
    second_idx = body.index("<code>222222222222</code>")
    assert first_idx < second_idx


def test_message_page_renders_patch_series_timeline(client, tmp_path):
    """When two cover letters share a `patch_series_key`, viewing
    one renders the state card (`<aside class="patch-state">`)
    with a Series-revisions row carrying both versions: the current
    one as plain text (`<strong>v2</strong>`), the other as a link.
    Pins the issue-65 happy path through the #208 card."""
    # Two cover-letter subjects, same author, same title → same
    # series. mkdir each subdir first; `_ingest_one_article`
    # creates `0.git` inside but doesn't make the parent.
    (tmp_path / "v1").mkdir()
    (tmp_path / "v2").mkdir()
    common_author = "Alice <a@example>"
    _, v1_url = _ingest_one_article(
        tmp_path / "v1",
        "alpha",
        "v1-cover@example.com",
        subject="[PATCH 0/3] improve foo handling",
        author=common_author,
    )
    _, v2_url = _ingest_one_article(
        tmp_path / "v2",
        "alpha",
        "v2-cover@example.com",
        subject="[PATCH v2 0/3] improve foo handling",
        author=common_author,
    )
    body = client.get(v2_url).data.decode()
    assert 'class="patch-state"' in body
    assert "Series revisions:" in body
    # The current revision (v2) is rendered as bold, not as a link.
    assert "<strong>v2</strong>" in body
    # The prior revision (v1) is rendered as a link.
    card = body.split('class="patch-state"')[1].split("</aside>")[0]
    assert "<a href=" in card
    assert ">v1</a>" in card
    # Arrow between revisions.
    assert "→" in card


def test_message_page_no_series_timeline_for_individual_patch(
    client,
    tmp_path,
):
    """A `[PATCH v2 1/3]` subject is an individual patch, not a
    cover letter. The state card still renders (it's a patch) but
    the Series-revisions row is omitted in slice 1."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "patch-1of3@example.com",
        subject="[PATCH v2 1/3] foo: add bar",
    )
    body = client.get(url).data.decode()
    assert "Series revisions:" not in body


def test_message_page_no_series_timeline_for_solo_cover_letter(
    client,
    tmp_path,
):
    """A cover letter with no other revisions in the DB (only
    v1, no v2 yet) still gets the `patch_series_key` set but the
    Series-revisions row stays hidden, the timeline needs ≥2
    revisions to be useful, and one row on its own would just say
    "v1 (this)" which is visual clutter."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "lonely-v1@example.com",
        subject="[PATCH 0/3] something nobody resent",
    )
    body = client.get(url).data.decode()
    assert "Series revisions:" not in body


# --- #208 patch-state card integration ---------------------------------------


def test_patch_state_card_renders_on_patch_subject(client, tmp_path):
    """A `[PATCH …]` subject opts the message page into the
    consolidated state card. With no extra inputs, only the
    Activity row renders ("No replies") and the rest are
    silently skipped, the card scales from minimal to full."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "bare-patch@example.com",
        subject="[PATCH 1/2] foo: add bar",
    )
    body = client.get(url).data.decode()
    assert 'class="patch-state"' in body
    # Bare patch → only Activity. The other rows are absent.
    assert "Activity:" in body
    assert "No replies" in body
    assert "Trailers:" not in body
    assert "Landed:" not in body
    assert "Series revisions:" not in body


def test_patch_state_card_absent_on_non_patch_subject(client, tmp_path):
    """A plain message (no `[PATCH …]` bracketing) gets no card at
    all. Pins the `is_patch` gate, the card is patch-only by design
    so non-patch articles don't ship an empty shell."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "prose@example.com",
        subject="thoughts on memory model wording",
    )
    body = client.get(url).data.decode()
    assert 'class="patch-state"' not in body
    assert "Activity:" not in body


def test_patch_state_card_absent_on_git_pull(client, tmp_path):
    """`[GIT PULL]` looks bracketed but isn't a patch in the sense
    we're indexing, the bracket-token guard in `_is_patch_subject`
    keys on the literal `PATCH` word."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "pull@example.com",
        subject="[GIT PULL] urgent fixes for 6.13",
    )
    body = client.get(url).data.decode()
    assert 'class="patch-state"' not in body


def test_patch_state_trailers_row_aggregates_by_role(client, tmp_path):
    """Trailers in the patch body group by canonical role, each
    role rendered with its total count. Pins the per-role
    aggregation: two Reviewed-by + one Acked-by yields the
    bucketed shape "2 Reviewed-by, 1 Acked-by" rather than three
    separate entries."""
    body_bytes = (
        b"diff --git a/file b/file\n@@ -1 +1 @@\n-x\n+y\n\n"
        b"Reviewed-by: Reviewer One <r1@example.com>\n"
        b"Reviewed-by: Reviewer Two <r2@example.com>\n"
        b"Acked-by: Acker <a@example.com>\n"
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "trailers@example.com",
        subject="[PATCH] foo: do bar",
        body=body_bytes,
    )
    body = client.get(url).data.decode()
    assert "Trailers:" in body
    assert "2 Reviewed-by" in body
    assert "1 Acked-by" in body


def test_patch_state_trailers_row_marks_maintainer_attestation(
    client,
    tmp_path,
):
    """A trailer whose address matches an M:/R: row on a subsystem
    this patch touches is counted into the maintainer subset; the
    rendered chip reads "(N maintainer)". Pins the per-role
    maintainer aggregation against the subsystem-maintainer
    lookup."""
    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[("M", "Kent Overstreet", "kent.overstreet@kernel.org")],
    )
    body_bytes = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n\n"
        b"Reviewed-by: Kent Overstreet <kent.overstreet@kernel.org>\n"
        b"Reviewed-by: Random Reviewer <random@elsewhere.example>\n"
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "maintainer-trailer@example.com",
        subject="[PATCH] bcachefs: tweak super",
        body=body_bytes,
    )
    body = client.get(url).data.decode()
    assert "Trailers:" in body
    # Two reviewers total, one of whom is a recognised maintainer.
    assert "2 Reviewed-by (1 maintainer)" in body


def test_patch_state_activity_row_shows_days_since_last_reply(
    client,
    tmp_path,
):
    """When the article has a reply that's older than today, the
    Activity row reports a non-zero day count. The reply is
    inserted directly into the DB (date in the past) so we can
    control the activity surface without depending on time."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    art_id, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "with-reply@example.com",
        subject="[PATCH] foo: trigger reply",
    )
    # Re-anchor the article date 10 days ago so the reply we insert
    # 3 days ago is plausibly *after* it. `_ingest_one_article`
    # defaults to "yesterday" (1.36.3), which was after the reply
    # date and made the activity row skip the reply.
    reply_at = datetime.now(timezone.utc) - timedelta(days=3)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = s.get(Article, art_id)
        art.date = datetime.now(timezone.utc) - timedelta(days=10)
        reply = Article(
            message_id="reply-3d-old@example.com",
            subject="Re: [PATCH] foo: trigger reply",
            author="r@example.com",
            date=reply_at,
            thread_parent="with-reply@example.com",
        )
        s.add(reply)
        s.flush()
        s.add(
            ArticleList(
                article_id=reply.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha="deadbeef",
            )
        )
        s.commit()
    body = client.get(url).data.decode()
    assert "Activity:" in body
    # Exact day count depends on rounding; either "3 days ago" or
    # "2 days ago" depending on the now() boundary.
    assert "Last reply" in body
    assert "days ago" in body


def test_patch_state_card_skips_empty_rows(client, tmp_path):
    """A bare cover-letter render with no trailers, no landing, no
    sibling revisions, no replies skips every optional row, only
    Activity ("No replies") remains. Pins the row-skipping logic so
    the card doesn't degrade into a wall of empty labels."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "lone-cover@example.com",
        subject="[PATCH 0/2] new feature",
    )
    body = client.get(url).data.decode()
    card = body.split('class="patch-state"')[1].split("</aside>")[0]
    assert "Trailers:" not in card
    assert "Landed:" not in card
    assert "Series revisions:" not in card
    assert "Activity:" in card
    assert "No replies" in card


def test_message_page_emits_breadcrumb_list(client, tmp_path):
    """The same @graph also carries a BreadcrumbList with the
    Site → Inbox → Subject chain."""
    art_id, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-bc@example.com",
        subject="brief subject",
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
        tmp_path,
        "alpha",
        "jsonld-bclong@example.com",
        subject=long_subject,
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    bc = next(g for g in graph if g["@type"] == "BreadcrumbList")
    last = bc["itemListElement"][-1]
    assert len(last["name"]) <= 80
    assert last["name"] != long_subject


def test_message_json_ld_author_strips_hidden_placeholder(
    client,
    tmp_path,
    monkeypatch,
):
    """JSON-LD author.name on a redacted sender is the display name
    only, no `<hidden>` placeholder. The placeholder is a rendering
    decision for the visible HTML; in structured data it reads as
    broken metadata. Flagged in the 2026-05-12 review."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])  # nothing allowlisted
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-named@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "David Woodhouse"


def test_message_json_ld_author_no_email_leak_for_bare_address(
    client,
    tmp_path,
    monkeypatch,
):
    """A From: line with only a bare address falls back to a neutral
    string in JSON-LD, not the email itself, which would defeat the
    visible HTML redaction the page already applied."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    # _ingest_one_article's default `author=a@b.example` is a bare
    # address with no display name.
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-bare@example.com",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "<hidden>" not in posting["author"]["name"]
    assert "@" not in posting["author"]["name"]


def test_message_json_ld_author_includes_email_when_allowlisted(
    client,
    tmp_path,
    monkeypatch,
):
    """Allowlisted senders surface their full From-line in the
    visible HTML (institutional kernel.org accounts, MAINTAINERS-
    listed maintainers). JSON-LD's `Person.email` mirrors that:
    present iff the address is already on the rendered page, omitted
    otherwise. `name` stays display-name only across both surfaces."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", ["@b.example"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-allow@example.com",
        author="Allowed Person <allowed@b.example>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "Allowed Person"
    assert "@b.example" not in posting["author"]["name"]
    assert posting["author"]["email"] == "allowed@b.example"


def test_message_json_ld_author_omits_email_when_not_allowlisted(
    client,
    tmp_path,
    monkeypatch,
):
    """Non-allowlisted senders: visible HTML hides the address and
    `Person.email` is absent. Crawlers see the same redaction state
    on both surfaces."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-hide@example.com",
        author="Casual Sender <casual@example.org>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "email" not in posting["author"]
    assert posting["author"]["name"] == "Casual Sender"


def test_message_json_ld_includes_text_snippet(client, tmp_path):
    """Google's DiscussionForumPosting validator requires one of
    `text` / `image` / `video`; we ship `text` derived from the
    parsed body. Search Console flagged this as critical on
    2026-05-14."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-text@example.com",
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
    from mimir.seo import JSON_LD_TEXT_MAX

    # 4× the cap, all real words so collapsing whitespace doesn't
    # shrink it under the limit.
    body = b"alpha bravo " * (JSON_LD_TEXT_MAX // 6)
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-text-long@example.com",
        body=body,
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
    client,
    tmp_path,
    monkeypatch,
):
    """The JSON-LD `text` snippet must apply the same DCO trailer
    redaction as the visible HTML, otherwise non-allowlisted
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
        tmp_path,
        "alpha",
        "jsonld-text-dco@example.com",
        body=body,
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    text = posting["text"]
    assert "m@kernel.org" in text  # allowlisted survives
    assert "o@example.com" not in text  # non-allowlisted redacted
    assert "<redacted>" in text


def test_message_json_ld_text_omitted_when_body_empty(client, tmp_path):
    """Whitespace-only bodies emit no `text` field rather than an
    empty string, which would re-fail the validator that flagged
    this in the first place."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-text-empty@example.com",
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
        tmp_path,
        "alpha",
        "jsonld-author-url@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    author = posting["author"]
    assert author["name"] == "David Woodhouse"
    assert author["url"].endswith("/alpha/author/David%20Woodhouse")


def test_message_json_ld_author_url_omitted_for_unknown_sender(
    client,
    tmp_path,
    monkeypatch,
):
    """A bare address with no display name renders as
    `unknown sender` (see `_display_name_filter`), which would
    match no one as a substring, omit the URL so we don't ship
    a stable link to a useless query."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "jsonld-author-url-bare@example.com",
        # Bare address → parseaddr returns no display name.
    )
    graph = _json_ld_blocks(client.get(url).data.decode())[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert "url" not in posting["author"]


def test_message_page_visible_html_redacts_non_allowlisted_from_address(
    client,
    tmp_path,
    monkeypatch,
):
    """Visible-HTML side of the redaction posture: a non-allowlisted
    From: must surface as `<display-name> <hidden>` on the rendered
    message page, never as the raw address. Unit-level coverage in
    `test_helpers.py` pins `_safe_from_filter` in isolation; this
    pins the template-side wiring (`| safe_from`), a regression that
    dropped the filter would pass every existing redaction test
    because the structured surfaces (JSON-LD, atom, data-*) have
    their own paths."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "visible-html-redact@example.com",
        author="Joe User <joe@example.com>",
    )
    body = client.get(url).data.decode()
    assert "joe@example.com" not in body
    assert "Joe User" in body
    assert "&lt;hidden&gt;" in body or "<hidden>" in body


def test_message_page_visible_html_surfaces_allowlisted_from_address(
    client,
    tmp_path,
    monkeypatch,
):
    """Companion to the redaction test: allowlisted senders DO surface
    their address verbatim on the visible HTML page (kernel.org-shaped
    institutional accounts). Pinning both halves keeps the redaction
    posture explicit."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", ["@b.example"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "visible-html-allow@example.com",
        author="Allowed Person <allowed@b.example>",
    )
    body = client.get(url).data.decode()
    assert "allowed@b.example" in body
    assert "<hidden>" not in body


def test_message_page_dco_trailer_redacts_non_allowlisted_address(
    client,
    tmp_path,
    monkeypatch,
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
        tmp_path,
        "alpha",
        "dco-redact@example.com",
        body=body,
    )
    page = client.get(url).data.decode()
    # Allowlisted address survives verbatim.
    assert "m@kernel.org" in page
    # Non-allowlisted address is gone.
    assert "o@example.com" not in page
    # Redacted placeholder is visible.
    assert "redacted" in page


def test_message_page_dco_trailer_no_xss_via_address_metacharacters(
    client,
    tmp_path,
    monkeypatch,
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
    body = b'text body\n\nSigned-off-by: Attacker <a"onmouseover=alert(1)@kernel.org>\n'
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "dco-xss@example.com",
        body=body,
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


def test_message_page_subsystem_header_is_clickable(client, tmp_path):
    """The subsystem name on a patch page links to the per-subsystem
    dashboard so a reader can navigate into the broader context.
    URL takes the lowercased form; display keeps upstream casing."""
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    body = (
        b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n"
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "bch-link@example.com",
        subject="bcachefs link test",
        body=body,
    )
    text = client.get(url).data.decode()
    # Link present with lowercased URL AND lowercased display
    # (anti-shouty pass, display takes the lowercase form too).
    assert '<a href="/alpha/subsystem/bcachefs/">bcachefs</a>' in text


def test_off_list_parent_hint_surfaces_unindexed_list(client, tmp_path):
    """When the thread root has an off-list parent and the article's
    To: header points at a list-shaped address that doesn't match any
    configured inbox, the rendered thread tree should surface that
    address as a hover-only hint so the operator knows which list to
    add. The address rides in `data-tooltip=` rather than visible
    text so the line stays compact."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "hint-shown@example.com",
        in_reply_to="missing-parent@example.com",
        to="linux-arm-kernel@lists.infradead.org",
    )
    body = client.get(url).data.decode()
    assert "off-list ancestor" in body
    assert 'data-tooltip="hint: linux-arm-kernel@lists.infradead.org"' in body
    # Placed below the trigger to escape the .thread-box overflow clip
    # that hides the default top-positioned tooltip on the first row.
    assert 'data-placement="bottom"' in body


def test_off_list_parent_hint_skips_already_configured_lists(client, tmp_path):
    """If the To: address matches a configured inbox's list_address,
    the hint must be suppressed, there's nothing to add, that list
    is already indexed."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.list_address = "linux-arm-kernel@lists.infradead.org"
        s.commit()

    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "hint-suppressed@example.com",
        in_reply_to="missing-parent-2@example.com",
        to="linux-arm-kernel@lists.infradead.org",
    )
    body = client.get(url).data.decode()
    assert "off-list ancestor" in body
    assert "linux-arm-kernel@lists.infradead.org" not in body


# Meta-sweep (issue #3): canonical, favicon/og:image, ItemList JSON-LD,
# subject normalisation, display_name filter, SITE_BASE_URL override.


def test_message_page_patch_series_revisions_does_not_n1_inbox(client, tmp_path):
    """The cover-letter patch-series sidebar (issue 65) iterates each
    revision's `lists` and reads `al.inbox.name` per `ArticleList`. The
    handler eager-loads the `Article.lists` collection, but until #198
    the eager-load did not chain through to `ArticleList.inbox`, so
    each per-revision `al.inbox.name` traversal triggered a lazy fetch
    per distinct inbox the series had touched.

    SQLAlchemy's identity map dedupes within the session so the worst
    case scaled with distinct-inbox-count rather than `len(revisions)
    * len(lists)`, but for a cross-posted series the per-render cost
    was still bounded by (extra-inboxes-in-series + 1) round-trips
    above the eager-load. Cover letters render on every load.

    The fix chained `.selectinload(Article.lists).selectinload(
    ArticleList.inbox)`. Pin that by counting `FROM inboxes` queries
    fired during the render: with the fix the only inbox SELECTs are
    the URL-resolution lookup (1) and the bulk selectinload (1).
    Without the fix a third per-id SELECT would fire for the
    cross-post inbox the identity map hasn't cached yet."""
    from sqlalchemy import event
    from mimir.extensions import SessionLocal, engine
    from mimir.models import Article, ArticleList, Inbox
    from sqlalchemy import select as _sa_select

    # Two cover-letter revisions, same author + title so they share a
    # `patch_series_key`. Ingest both into alpha via the public helper.
    (tmp_path / "v1").mkdir()
    (tmp_path / "v2").mkdir()
    common_author = "Alice <a@example>"
    _, v1_url = _ingest_one_article(
        tmp_path / "v1",
        "alpha",
        "n1-v1-cover@example.com",
        subject="[PATCH 0/3] eager-load chain",
        author=common_author,
    )
    _, v2_url = _ingest_one_article(
        tmp_path / "v2",
        "alpha",
        "n1-v2-cover@example.com",
        subject="[PATCH v2 0/3] eager-load chain",
        author=common_author,
    )
    # Cross-post both revisions to beta so each `Article.lists` has
    # two `ArticleList` rows pointing at distinct inboxes; that's the
    # shape the lazy load fell over on.
    with SessionLocal() as s:
        beta = s.execute(_sa_select(Inbox).where(Inbox.name == "beta")).scalar_one()
        for mid in ("n1-v1-cover@example.com", "n1-v2-cover@example.com"):
            art = s.execute(
                _sa_select(Article).where(Article.message_id == mid)
            ).scalar_one()
            s.add(
                ArticleList(
                    article_id=art.id,
                    inbox_id=beta.id,
                    epoch="0.git",
                    commit_sha="ee" * 20,
                )
            )
        s.commit()

    inbox_selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, parameters, context, executemany):
        if "FROM inboxes" in statement:
            inbox_selects.append(statement)

    try:
        resp = client.get(v2_url)
    finally:
        event.remove(engine, "before_cursor_execute", _grab)
    assert resp.status_code == 200
    # Three `FROM inboxes` SELECTs are expected, all batched: the URL
    # inbox-name resolution (`WHERE inboxes.name = ?`), the `all_links`
    # bulk fetch (`JOIN article_lists WHERE al.article_id = ?`), and
    # the chained selectinload for the patch-series revisions (`WHERE
    # inboxes.id IN (...)`). Any per-id `WHERE inboxes.id = ?` is the
    # lazy-load signature and means the chain broke.
    lazy = [s for s in inbox_selects if "inboxes.id = ?" in s]
    assert not lazy, (
        f"patch-series render lazy-loaded {len(lazy)} inbox row(s); "
        "the selectinload chain through ArticleList.inbox broke:\n"
        + "\n---\n".join(lazy)
    )


# --- Series-diff route (#210) -------------------------------------------------


def test_message_page_sends_etag_and_no_cache(client, tmp_path):
    """The message route emits a strong ETag and `Cache-Control:
    public, no-cache`. Pairs with the route-level conditional check
    that returns 304 when If-None-Match matches; together they
    eliminate the within-cache-window stale-after-deploy problem
    while keeping repeated loads cheap via 304s."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "etag-headers@example.com",
        subject="basic article",
    )
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers.get("ETag"), "message page must emit ETag"
    assert resp.headers.get("Cache-Control") == "public, no-cache"
    # Vary on HX-Request was already set; pin it stays set so a future
    # refactor doesn't drop it (browsers would otherwise confuse the
    # full-page response with the HTMX partial under the same URL).
    assert "HX-Request" in resp.headers.get("Vary", "")


def test_message_page_returns_304_on_matching_if_none_match(client, tmp_path):
    """Repeating the request with `If-None-Match` set to the ETag from
    the first response returns 304 with no body. The 304 must still
    carry Cache-Control (RFC 7232) so the client knows when to
    revalidate next."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "etag-304@example.com",
        subject="basic article",
    )
    first = client.get(url)
    etag = first.headers.get("ETag")
    assert etag, "must have ETag on the first response"
    second = client.get(url, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.data == b""
    # 304 echoes ETag and Cache-Control per RFC.
    assert second.headers.get("ETag") == etag
    assert second.headers.get("Cache-Control") == "public, no-cache"


def test_message_page_returns_200_when_if_none_match_does_not_match(client, tmp_path):
    """A stale or unrelated If-None-Match value gets a full 200
    response, not a misleading 304. Pins that the matcher does an
    actual comparison rather than blindly 304-ing."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "etag-mismatch@example.com",
        subject="basic article",
    )
    resp = client.get(url, headers={"If-None-Match": '"deadbeef"'})
    assert resp.status_code == 200
    assert resp.data  # body present


def test_message_page_etag_changes_when_thread_gains_a_reply(client, tmp_path):
    """The ETag includes `max(thread node date)`; adding a reply to
    the thread must change the value so the browser's previously
    cached version is no longer "still good" and a fresh render
    surfaces the new reply in the thread tree.

    Implementation note: the reply is inserted directly into the DB
    rather than via `_ingest_one_article`, because that helper
    re-points the inbox's `mirror_path` per call and the second
    invocation would render the root article's blob unreachable
    (the route's body fetch would then 404, never getting to the
    ETag check). Direct DB insert is enough; the ETag only reads
    the date column."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select
    from mimir.extensions import SessionLocal as _SL
    from mimir.models import (
        Article as _Article,
        ArticleList as _ArticleList,
        CacheEntry as _CacheEntry,
        Inbox as _Inbox,
    )

    root_id, root_url = _ingest_one_article(
        tmp_path,
        "alpha",
        "etag-thread-root@example.com",
        subject="root subject",
    )
    first = client.get(root_url)
    assert first.status_code == 200, first.data[:200]
    first_etag = first.headers["ETag"]

    # Insert a reply pointing at the root via thread_parent. Reply
    # date is later so the thread's max date moves forward. Doesn't
    # need a blob in the mirror (the message page being rendered is
    # the root, not the reply; the reply just shows up in the
    # thread-tree sidebar).
    with _SL() as s:
        alpha = s.execute(_select(_Inbox).where(_Inbox.name == "alpha")).scalar_one()
        root_date = s.get(_Article, root_id).date
        reply = _Article(
            message_id="etag-thread-reply@example.com",
            subject="Re: root subject",
            author="r@b.example",
            date=(root_date or datetime.now(timezone.utc)) + timedelta(hours=1),
            thread_parent="etag-thread-root@example.com",
            subject_normalized="root subject",
            lists=[
                _ArticleList(
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="ee" * 20,
                )
            ],
        )
        s.add(reply)
        # Bust the threading-helper cache so the next render reflects
        # the new reply (cache TTL is 5 min; the test would otherwise
        # see the pre-reply thread and the ETag wouldn't change).
        s.execute(_delete(_CacheEntry))
        s.commit()

    second = client.get(root_url)
    assert second.status_code == 200, second.data[:200]
    second_etag = second.headers["ETag"]
    assert second_etag != first_etag, (
        f"ETag must change when the thread gains a reply; "
        f"first={first_etag} second={second_etag}"
    )


def test_message_page_hx_request_has_distinct_etag(client, tmp_path):
    """The full-page response and the HTMX intra-thread-swap partial
    are different bodies for the same URL; they must have distinct
    ETags so a browser that cached one can't reuse the cache entry
    for the other on a subsequent request of the opposite type."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "etag-hx@example.com",
        subject="basic article",
    )
    full = client.get(url)
    partial = client.get(url, headers={"HX-Request": "true"})
    assert full.headers["ETag"] != partial.headers["ETag"]


# --- Lifecycle timeline + tree-labelled landings (Task 12) ---


def test_message_page_renders_lifecycle_timeline(client, tmp_path):
    """A patch article with at least one timeline event (the 'Posted'
    event is always present) renders the lifecycle-timeline section."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "lifecycle-patch@example.com",
        subject="[PATCH 1/2] net: do thing",
    )
    r = client.get(url)
    assert r.status_code == 200
    body = r.data.decode()
    assert 'class="lifecycle-timeline"' in body
    # The 'Posted' event is emitted for every patch with a date.
    assert ">Posted<" in body


def test_message_page_omits_timeline_on_non_patch(client, tmp_path):
    """A non-patch article (no [PATCH] prefix) does NOT render the
    lifecycle-timeline section."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "prose-msg@example.com",
        subject="Thoughts on the scheduler",
    )
    body = client.get(url).data.decode()
    assert 'class="lifecycle-timeline"' not in body


def test_message_page_card_landings_label_tree(client, tmp_path):
    """Each landing entry on the patch-state card shows 'Applied to'
    followed by the tree label, not just the raw tree_name slug."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "landed-patch@example.com",
        subject="[PATCH 1/2] net: add feature",
    )
    _seed_mainline_commit(
        message_id="landed-patch@example.com",
        commit_sha="aabbccddeeff" + "00" * 14,
        tree_name="linus",
    )
    body = client.get(url).data.decode()
    assert "Applied to" in body
    # tree_label for "linus" falls back to slug when no display_name is set.
    assert "linus" in body
    # The old bare `<code>linus</code>` shape (tree_name in code tag) must
    # NOT appear; tree labelling now goes via the "Applied to <strong>"
    # pattern.
    assert "<code>linus</code>" not in body


def test_message_page_lifecycle_timeline_shows_tree_landing_event(client, tmp_path):
    """When a patch has a mainline_commits row the timeline includes a
    tree-pickup event with a label derived from the tree slug."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "tree-event@example.com",
        subject="[PATCH 1/2] mm: fix leak",
    )
    _seed_mainline_commit(
        message_id="tree-event@example.com",
        commit_sha="deadbeef1234" + "00" * 14,
        tree_name="linus",
    )
    body = client.get(url).data.decode()
    assert 'class="lifecycle-timeline"' in body
    # The tree-pickup event label for "linus" is "Landed".
    assert "Landed" in body
