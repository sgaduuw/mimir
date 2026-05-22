"""Tests for mimir/web/routes/attachments.py: per-message
attachment download + Pygments-syntax-highlighted preview,
and the race-condition fallback when the git blob is gone."""

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
