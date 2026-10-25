"""Markdown versions of the message and thread pages (#884).

Separate `.md` URLs, never `Accept` negotiation. The `.md` body is the
same document as the HTML page under the same privacy filters: these
tests hold the two together on the URL, the canonical, the validator,
the crawler budget and, above all, on which addresses are visible.
"""

import html as html_lib
import re
import time

import pytest

from mimir.config import settings
from tests.test_routes._helpers import (
    _ingest_one_article,
    _ingest_with_attachment,
    _seed_three_message_thread,
    seed_thread_shape,
)

MD_TYPE = "text/markdown; charset=utf-8"


def _canonical(page: str) -> str | None:
    m = re.search(r'<link rel="canonical" href="([^"]+)"', page)
    return m.group(1) if m else None


def _alternate(page: str) -> list[str]:
    return re.findall(
        r'<link rel="alternate" type="text/markdown" href="([^"]+)"', page
    )


def _link_canonical(resp) -> str | None:
    m = re.fullmatch(r'<([^>]+)>; rel="canonical"', resp.headers.get("Link", ""))
    return m.group(1) if m else None


def _path(url: str) -> str:
    return url.replace("http://localhost", "")


# Bounded runs, same shape as `filters._EMAIL_TOKEN_RE`, so the oracle
# itself stays linear.
_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}")


def _visible_html_text(page: str) -> str:
    """What a reader sees: no head (canonical, JSON-LD), no scripts, no
    attributes. JSON-LD has its own known redaction gap, so letting it
    into the oracle would excuse the same leak in the `.md`."""
    page = re.sub(r"<head\b.*?</head\s*>", "", page, flags=re.DOTALL | re.IGNORECASE)
    page = re.sub(
        r"<script\b.*?</script\s*>", "", page, flags=re.DOTALL | re.IGNORECASE
    )
    return html_lib.unescape(re.sub(r"<[^>]+>", "", page))


def test_message_md_is_served_as_markdown(client, tmp_path):
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "md-basic@example.com",
        subject="Hello markdown",
        body=b"line one\nline two\n",
    )
    r = client.get(url + ".md")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == MD_TYPE
    assert r.headers["Cache-Control"] == "public, no-cache"
    assert r.headers.get("ETag")
    text = r.get_data(as_text=True)
    assert "Hello markdown" in text
    assert "line one\nline two" in text
    # Decision A: the message's own Message-ID is not exposed, exactly
    # as the HTML page does not expose it.
    assert "md-basic@example.com" not in text


def test_message_md_canonical_header_is_the_html_canonical(client, tmp_path):
    _, url = _ingest_one_article(tmp_path, "alpha", "md-canon@example.com")
    page = client.get(url).get_data(as_text=True)
    r = client.get(url + ".md")
    assert r.status_code == 200
    assert _link_canonical(r) == _canonical(page)
    assert _canonical(page).endswith(url)  # precondition: self-canonical


def test_reply_md_canonical_names_the_thread_page(client, tmp_path):
    """A reply cedes its canonical to the thread page holding it. The
    `.md` must name that page, byte-identical to the HTML, not the
    reply's own URL (a canonical chain)."""
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    _, root_url = seeded["root@x"]
    _, reply_url = seeded["reply@x"]
    page = client.get(reply_url).get_data(as_text=True)
    assert _canonical(page).endswith(root_url + "/t")  # precondition
    r = client.get(reply_url + ".md")
    assert r.status_code == 200
    assert _link_canonical(r) == _canonical(page)


@pytest.mark.parametrize("which", ["message", "thread", "thread_page_2"])
def test_html_alternate_names_the_md_url_and_it_resolves(
    client, tmp_path, monkeypatch, which
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 1)
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    _, root_url = seeded["root@x"]
    url = {
        "message": root_url,
        "thread": root_url + "/t",
        "thread_page_2": root_url + "/t/2",
    }[which]
    page = client.get(url)
    assert page.status_code == 200
    assert _alternate(page.get_data(as_text=True)) == [url + ".md"]
    r = client.get(url + ".md")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == MD_TYPE


# A body holding every shape the HTML hides an address in, plus one
# allowlisted address so the absence check cannot pass on an empty body.
_PRIVACY_BODY = (
    b"On Mon, Joe User <joe@example.com> wrote:\n"
    b"> Signed-off-by: Joe User <joe@example.com>\n"
    b"> some quoted prose\n"
    + (b"> " * 70)
    + b"Signed-off-by: Joe User <joe@example.com>\n"
    b"\n"
    b"my reply\n"
    b"\n"
    b"Signed-off-by: Joe User <joe@example.com>\n"
    b"Signed-off-by: Maintainer <m@kernel.org>\n"
)


def test_md_hides_a_non_allowlisted_sender_like_the_html(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "md-privacy@example.com",
        # #852: an email-shaped display name must not print the address.
        author='"joe@example.com" <joe@example.com>',
        body=_PRIVACY_BODY,
    )
    assert b"joe@example.com" in _PRIVACY_BODY  # precondition
    page = client.get(url).get_data(as_text=True)
    assert "joe@example.com" not in _visible_html_text(page)  # the HTML hides it
    r = client.get(url + ".md")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "joe@example.com" not in text
    assert "m@kernel.org" in text
    assert "<redacted>" in text


def test_thread_md_hides_non_allowlisted_senders(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist", [])
    seeded = _seed_three_message_thread(
        tmp_path,
        "alpha",
        authors=(
            "Joe <joe@example.com>",
            "Ann <ann@example.com>",
            "Joe <joe@example.com>",
        ),
    )
    _, root_url, _ = seeded["root"]
    r = client.get(root_url + "/t.md")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    # Precondition: every From rendered, escaped so Markdown keeps it.
    assert text.count(r"\<hidden\>") >= 3
    assert "joe@example.com" not in text
    assert "ann@example.com" not in text


# Each body is a place the plain-text pass could drift from the HTML.
_PARITY_BODIES = [
    b"On Mon, Joe <joe@example.com> wrote:\n> hi\n\nreply\n",
    b"> Signed-off-by: Q <q@example.com>\n>> Acked-by: R <r@example.com>\n",
    b">  Signed-off-by: S <s@example.com>\n",  # two spaces: not a trailer
    b"Cc: <cc@example.com>\nTo: Someone <to@example.com>\n",
    b"Reported-by: <rep@localhost>\n",  # trailer, no dotted TLD
    b"see <bare@localhost> here\n",  # no dotted TLD: the HTML keeps it
    b"plain bare@example.com address\n",  # bare: the HTML keeps it
    b"```\nauthor <fence@example.com>\n```\n",  # code: the HTML keeps it
    (
        b"---\n"
        b"diff --git a/MAINTAINERS b/MAINTAINERS\n"
        b"--- a/MAINTAINERS\n"
        b"+++ b/MAINTAINERS\n"
        b"@@ -1,1 +1,2 @@\n"
        b" X\n"
        b"+M:\tNew Person <new@example.org>\n"
    ),  # diff: the HTML keeps it verbatim
    b"> diff --git a/x b/x\n> +M:\tQuoted <qd@example.org>\n",
    # Trailers without a dotted TLD: only the trailer redactor hides
    # these, so they catch a trailer test that misses the quote prefix.
    b"> > Reported-by: <qrep@localhost>\n",  # quoted trailer
    (b"> " * 70) + b"Acked-by: D <deep@localhost>\n",  # past the depth cap
]


def test_md_shows_no_address_the_html_hides(client, tmp_path, monkeypatch):
    """The oracle: every address token in the `.md` is also visible on
    the HTML page, for each body shape above. A plain-text pass that
    drifts from the HTML renderer in either direction shows up here."""
    monkeypatch.setattr(settings, "email_allowlist", [])
    shown_by_html: set[str] = set()
    checked = 0
    for i, body in enumerate(_PARITY_BODIES):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        _, url = _ingest_one_article(sub, "alpha", f"parity{i}@example.net", body=body)
        page = client.get(url)
        md = client.get(url + ".md")
        assert md.status_code == 200, body
        html_tokens = set(
            _ADDR_RE.findall(_visible_html_text(page.get_data(as_text=True)))
        )
        md_tokens = set(_ADDR_RE.findall(md.get_data(as_text=True)))
        assert md_tokens <= html_tokens, (body, md_tokens - html_tokens)
        shown_by_html |= html_tokens
        checked += 1
    assert checked == len(_PARITY_BODIES)
    # Not degenerate: the HTML shows the verbatim shapes, so the subset
    # check compared real token sets rather than two empty ones.
    assert {"new@example.org", "bare@example.com", "fence@example.com"} <= (
        shown_by_html
    )


def test_md_keeps_a_patch_with_addresses_verbatim(client, tmp_path):
    body = _PARITY_BODIES[8]
    _, url = _ingest_one_article(tmp_path, "alpha", "md-diff@example.net", body=body)
    text = client.get(url + ".md").get_data(as_text=True)
    assert "+M:\tNew Person <new@example.org>\n" in text


def test_fence_outlasts_every_backtick_run_in_the_body(client, tmp_path):
    """Patches to `.md` and `.rst` docs carry triple backticks. A fixed
    fence would close early and turn the rest of the body into Markdown."""
    body = b"before\n```c\nint x;\n```\ninline `````five````` run\nafter\n"
    _, url = _ingest_one_article(tmp_path, "alpha", "md-fence@example.net", body=body)
    r = client.get(url + ".md")
    assert r.status_code == 200
    lines = r.get_data(as_text=True).split("\n")
    opening = [i for i, ln in enumerate(lines) if re.fullmatch(r"`{3,}text", ln)]
    assert len(opening) == 1
    start = opening[0]
    fence = lines[start][: -len("text")]
    assert len(fence) > 5
    end = lines.index(fence, start + 1)
    inner = lines[start + 1 : end]
    assert inner[0] == "before" and inner[-1] == "after"
    assert "inline `````five````` run" in inner
    closes_early = re.compile(r" {0,3}`{" + str(len(fence)) + r",}\s*")
    assert not any(closes_early.fullmatch(ln) for ln in inner)


@pytest.mark.parametrize(
    "hostile",
    [
        "`" * 300_000,
        "a`" * 300_000,
        "<a@" * 300_000,
        "Signed-off-by: " + "<a@b" * 100_000,
        "> " * 150_000 + "Signed-off-by: x <a@b.example>",
        "\n".join(["> Signed-off-by: x <a@b.example>"] * 50_000),
    ],
    ids=["ticks", "tick-pairs", "open-angles", "trailer-angles", "deep", "lines"],
)
def test_md_body_and_fence_scan_in_linear_time(client, hostile):
    """Check 7: both scans meet whatever a sender wrote. Bounded loosely
    so a slow runner passes and a quadratic scan does not. In a request
    context, as served, so the allowlist lookup is the memoised one."""
    from mimir.web.filters import _md_fence, _render_body_text_filter

    with client.application.test_request_context():
        t0 = time.perf_counter()
        text = _render_body_text_filter(hostile)
        _md_fence(text)
        assert time.perf_counter() - t0 < 5


def test_md_etag_is_its_own_and_revalidates(client, tmp_path, monkeypatch):
    _, url = _ingest_one_article(tmp_path, "alpha", "md-etag@example.com")
    html_etag = client.get(url).headers["ETag"]
    r = client.get(url + ".md")
    etag = r.headers["ETag"]
    assert etag != html_etag
    again = client.get(url + ".md", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["ETag"] == etag
    # An HTML validator must not revalidate the Markdown body.
    assert (
        client.get(url + ".md", headers={"If-None-Match": html_etag}).status_code == 200
    )
    # The content part is shared with the HTML, so its inputs move this too.
    monkeypatch.setattr(settings, "thread_view_render_cap", 7)
    assert client.get(url + ".md").headers["ETag"] != etag


def test_thread_md_pages_link_and_redirect_like_the_html(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "thread_view_render_cap", 1)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("root@x", None), ("r1@x", "root@x"), ("r2@x", "root@x")],
    )
    _, root_url = seeded["root@x"]
    _, r1_url = seeded["r1@x"]
    t = root_url + "/t"
    pages = {t: t + ".md", t + "/2": t + "/2.md", t + "/3": t + "/3.md"}
    for html_url, md_url in pages.items():
        page = client.get(html_url).get_data(as_text=True)
        r = client.get(md_url)
        assert r.status_code == 200, md_url
        assert r.headers["Content-Type"] == MD_TYPE
        assert r.headers["Cache-Control"] == "public, no-cache"
        assert _link_canonical(r) == _canonical(page)
    first = client.get(t + ".md").get_data(as_text=True)
    middle = client.get(t + "/2.md").get_data(as_text=True)
    assert "http://localhost" + t + "/2.md" in first
    assert "http://localhost" + t + ".md" in middle
    assert "http://localhost" + t + "/3.md" in middle
    one = client.get(t + "/1.md")
    assert one.status_code == 301
    assert _path(one.headers["Location"]) == t + ".md"
    reply = client.get(r1_url + "/t.md")
    assert reply.status_code == 301
    assert _path(reply.headers["Location"]) == t + "/2.md"
    assert client.get(t + "/4.md").status_code == 404


def test_md_routing_edges(client, tmp_path):
    _, url = _ingest_one_article(tmp_path, "alpha", "md-edges@example.com")
    prefix = url.rsplit("/", 1)[0]
    assert client.get(prefix + "/999999999.md").status_code == 404
    assert client.get(url + ".md/").status_code == 404  # no slash redirect (#827)
    assert client.get(url + ".mdx").status_code == 404
    assert client.get(url + "/t.md/").status_code == 404
    # Right date, wrong inbox: 301 to the canonical page's Markdown form.
    wrong = "/beta/" + url.split("/", 2)[2]
    r = client.get(wrong + ".md")
    assert r.status_code == 301
    assert _path(r.headers["Location"]) == url + ".md"


def test_md_lists_attachments_as_links_that_resolve(client, tmp_path):
    url = _ingest_with_attachment(
        tmp_path,
        "alpha",
        "md-attach@example.com",
        attachment_filename="notes_[1].txt",
        attachment_content_type="text/plain",
        attachment_bytes=b"attached text",
    )
    text = client.get(url + ".md").get_data(as_text=True)
    m = re.search(r"\]\((http://localhost[^)\s]+)\)", text.split("Attachments", 1)[1])
    assert m, text
    assert m.group(1) == "http://localhost" + url + "/attachment/0"
    assert client.get(_path(m.group(1))).data == b"attached text"
    # The sender-controlled filename cannot close the link text early.
    assert r"notes\_\[1\].txt" in text


def test_md_counts_against_the_same_crawler_budget(client, tmp_path, monkeypatch):
    """No separate rule: a crawler's HTML and `.md` requests draw on one
    bucket. Applebot at 1% of 100 a minute holds a single token."""
    from tests.test_routes.test_crawler_budget import (
        APPLEBOT,
        Clock,
        _gate,
        _install,
    )

    _, url = _ingest_one_article(tmp_path, "alpha", "md-budget@example.com")
    # Precondition: a 429 on an unmatched URL would pass this test too.
    assert client.get(url + ".md").status_code == 200
    _install(monkeypatch, _gate(Clock(), total=100))
    assert client.get(url, headers={"User-Agent": APPLEBOT}).status_code == 200
    r = client.get(url + ".md", headers={"User-Agent": APPLEBOT})
    assert r.status_code == 429


def test_thread_md_carries_the_status_lines_of_the_html(client, tmp_path):
    from datetime import UTC, datetime

    from mimir.extensions import SessionLocal
    from mimir.models import Article, MainlineCommit

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()
    page = client.get(root_url + "/t").get_data(as_text=True)
    m = re.search(r"(Landed in mainline as[^<]*)", page)
    assert m  # precondition: the HTML states it
    text = client.get(root_url + "/t.md").get_data(as_text=True)
    assert m.group(1).strip() in text
    assert re.search(r"\bLANDED\b", text)


def test_thread_md_etag_is_its_own(client, tmp_path):
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    t = seeded["root@x"][1] + "/t"
    html_etag = client.get(t).headers["ETag"]
    md_etag = client.get(t + ".md").headers["ETag"]
    assert md_etag != html_etag
    assert (
        client.get(t + ".md", headers={"If-None-Match": html_etag}).status_code == 200
    )
    assert client.get(t + ".md", headers={"If-None-Match": md_etag}).status_code == 304


def test_md_subject_and_filename_have_no_bare_entity(client, tmp_path):
    """CommonMark decodes `&name;` entities, so an unescaped `&` in a
    sender field would render `joe&commat;example&period;com` as an
    address that the HTML page shows as literal text."""
    url = _ingest_with_attachment(
        tmp_path,
        "alpha",
        "ent@example.net",
        attachment_filename="joe&commat;example&period;com &lt;b&gt;.txt",
        attachment_content_type="text/plain",
        attachment_bytes=b"x",
    )
    md = client.get(url + ".md").get_data(as_text=True)
    att = next(ln for ln in md.split("\n") if ln.startswith("- ["))
    assert not re.search(r"(?<!\\)&", att), att
