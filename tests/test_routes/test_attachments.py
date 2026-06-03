"""Tests for mimir/web/routes/attachments.py: per-message
attachment download + Pygments-syntax-highlighted preview,
and the race-condition fallback when the git blob is gone."""

import pytest

from tests.test_routes._helpers import _ingest_with_attachment


def test_attachment_download_serves_bytes_with_content_disposition(
    client,
    tmp_path,
):
    """The download route returns the attachment bytes verbatim with
    a Content-Disposition header carrying the filename. RFC 6266
    `filename*=UTF-8''...` is appended for round-trippability with
    non-ASCII filenames; this test pins the ASCII case (where both
    `filename=` and `filename*=` appear)."""
    payload = b"hello attachment world"
    url = _ingest_with_attachment(
        tmp_path,
        "alpha",
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
        tmp_path,
        "alpha",
        "attach-oob@example.com",
        attachment_filename="x.bin",
        attachment_content_type="application/octet-stream",
        attachment_bytes=b"abc",
    )
    assert client.get(f"{url}/attachment/99").status_code == 404
    assert client.get(f"{url}/attachment/99/preview").status_code == 404


def test_attachment_404_when_blob_unreachable_via_read_message(client, seeded_db):
    """The attachment route fetches via `read_message`, which raises
    `MessageNotFound` if the git blob is missing (mirror pruned, ref
    rewound, mirror disk vanished). The route must 404, not 500
    (covers web.py:1481-1482).

    The seeded `alpha` inbox points at `/tmp/alpha` which doesn't
    exist on disk in the test environment, so any read of a seeded
    article fails inside `_read_blob` and surfaces as MessageNotFound
    -- the exact race-condition shape this branch defends against."""
    from sqlalchemy import select
    from mimir.models import Article

    with seeded_db() as s:
        art_id = s.execute(
            select(Article.id).where(Article.message_id == "art1@example.com")
        ).scalar_one()

    # Both download and preview routes go through `_fetch_article_for_attachment`.
    assert client.get(f"/alpha/2024/01/{art_id}/attachment/0").status_code == 404
    assert (
        client.get(f"/alpha/2024/01/{art_id}/attachment/0/preview").status_code == 404
    )


def test_attachment_preview_pygmentizes_text(client, tmp_path):
    """A `.py` attachment is previewable; the response contains the
    Pygments-highlighted output (token spans, not the raw source)
    plus the file's contents recognisably."""
    src = b"def hello():\n    return 'world'\n"
    url = _ingest_with_attachment(
        tmp_path,
        "alpha",
        "attach-preview@example.com",
        attachment_filename="snippet.py",
        attachment_content_type="text/x-python",
        attachment_bytes=src,
    )
    r = client.get(f"{url}/attachment/0/preview")
    assert r.status_code == 200
    body = r.data.decode()
    # Pygments noclasses=False emits class-named spans (e.g.
    # `<span class="k">def</span>`); the inline `style="color:..."`
    # form was dropped in the security pass so CSP can deny
    # `'unsafe-inline'` on `style-src`.
    assert "<span" in body
    assert 'style="color' not in body
    # Source content survives the highlight.
    assert "hello" in body
    assert "world" in body


def test_attachment_preview_falls_back_for_non_previewable(client, tmp_path):
    """A binary attachment (application/octet-stream + no text-like
    extension) renders the "can't preview" path: still 200, but the
    response carries the fallback marker rather than highlighted
    output."""
    url = _ingest_with_attachment(
        tmp_path,
        "alpha",
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


# ---------------------------------------------------------------------------
# RFC 6266 Content-Disposition: multibyte UTF-8 filename coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_quoted_substring",
    [
        # CJK (Japanese: "invitation.txt")
        ("招待状.txt", "%E6%8B%9B%E5%BE%85%E7%8A%B6.txt"),
        # Latin-1 with diacritics (French: "cafe.txt" with é)
        ("café.txt", "caf%C3%A9.txt"),
        # Emoji (party popper, U+1F389, requires surrogate pair in UTF-16
        # but a 4-byte UTF-8 sequence; common real-world filename input)
        ("🎉.txt", "%F0%9F%8E%89.txt"),
        # Cyrillic (Russian: "doc.txt" in Cyrillic letters)
        ("документ.txt", "%D0%B4%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82.txt"),
    ],
    ids=["CJK", "Latin-1-diacritic", "emoji-4byte", "Cyrillic"],
)
def test_content_disposition_rfc6266_multibyte_utf8(
    filename, expected_quoted_substring
):
    """`_content_disposition` must produce a valid RFC 6266
    `filename*=UTF-8''<percent-encoded>` parameter for ANY multibyte
    filename, including:

    - 3-byte CJK codepoints (Japanese, Chinese, Korean)
    - 2-byte Latin-1 supplements with diacritics
    - 4-byte UTF-8 sequences (emoji, mathematical alphanumerics, etc.)
    - Cyrillic (mixed multi-byte)

    Existing `test_attachment_download_serves_bytes_with_content_disposition`
    pins only the ASCII case. A regression that bypassed
    `urllib.parse.quote` for the multibyte path (e.g. emitted raw
    bytes in the header) would inject invalid UTF-8 bytes into the
    HTTP response line, breaking compliant clients.

    Direct unit test on `_content_disposition` rather than a full
    route integration so the contract is pinned even if the route's
    SQL fixture machinery changes shape.
    """
    from mimir.web.routes.attachments import _content_disposition

    cd = _content_disposition(filename)

    # The RFC 6266 `filename*` parameter is the load-bearing surface
    # for non-ASCII filenames; pin its presence + the exact percent-
    # encoded form.
    assert f"filename*=UTF-8''{expected_quoted_substring}" in cd, (
        f"`filename*` parameter for {filename!r} doesn't match the "
        f"expected percent-encoded form. Got Content-Disposition: {cd!r}"
    )
    # The header must still start with `attachment;` so browsers
    # treat it as a download rather than rendering inline.
    assert cd.startswith("attachment;"), (
        f"Content-Disposition must start with `attachment;`; got {cd!r}"
    )
    # No raw multibyte codepoints should leak into the ASCII
    # `filename="..."` form (whatever the bare filename= contains,
    # the filename* form is what compliant clients use; verify the
    # quoted form was produced).
    assert "%" in cd, (
        f"expected percent-encoded characters in Content-Disposition for "
        f"multibyte filename {filename!r}; got {cd!r}"
    )
