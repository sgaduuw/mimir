"""Pins for the Phase C safety caps: parser multipart-count cap,
patches.extract_touched_paths cap, Pygments block-size clamp,
canonical tooltip-address sanitizer + env override."""

import email.mime.multipart
import email.mime.text

import pytest

from mimir.canonical import (
    LIST_HOST_SUFFIXES,
    _effective_suffixes,
    extract_list_addresses,
    is_list_address,
)
from mimir.config import settings
from mimir.parser import (
    MAX_ATTACHMENT_PARTS,
    MessageTooComplex,
    parse_message,
)
from mimir.patches import MAX_TOUCHED_PATHS, extract_touched_paths
from mimir.rendering.body import PYGMENTS_MAX_BLOCK_CHARS, _render_block
from mimir.rendering.blocks import _Block


# ----- parser: multipart-count cap ----------------------------------------


def _build_multipart_message(part_count: int) -> bytes:
    """Build a raw message with `part_count` text/plain leaf parts
    inside a single multipart/mixed envelope. Avoids the bytes cap
    by keeping each part tiny."""
    outer = email.mime.multipart.MIMEMultipart("mixed")
    outer["From"] = "sender@example.org"
    outer["To"] = "recipient@example.org"
    outer["Subject"] = "test"
    outer["Message-ID"] = f"<test-{part_count}@example.org>"
    outer["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    for i in range(part_count):
        outer.attach(email.mime.text.MIMEText(f"part {i}\n"))
    return outer.as_bytes()


def test_parse_message_accepts_realistic_part_count():
    """Sanity check: a message with ~10 parts (well within real
    lkml shapes) parses cleanly."""
    raw = _build_multipart_message(10)
    parsed = parse_message(raw)
    assert parsed.message_id == "test-10@example.org"


def test_parse_message_rejects_pathological_part_count():
    """A message that crosses `MAX_ATTACHMENT_PARTS` parts trips
    `MessageTooComplex`. Defends against a crafted blob that fits
    under `MAX_RAW_MESSAGE_BYTES` but encodes hundreds of
    thousands of nested MIME boundaries."""
    raw = _build_multipart_message(MAX_ATTACHMENT_PARTS + 5)
    with pytest.raises(MessageTooComplex):
        parse_message(raw)


# ----- patches: extract_touched_paths cap ---------------------------------


def test_extract_touched_paths_caps_at_max():
    """A body with more `diff --git` headers than the cap should
    return at most `MAX_TOUCHED_PATHS` entries. Defends against a
    crafted body that floods `article_files` with synthetic rows."""
    lines = [f"diff --git a/x{i}.c b/x{i}.c" for i in range(MAX_TOUCHED_PATHS + 50)]
    body = "\n".join(lines)
    paths = extract_touched_paths(body)
    assert len(paths) == MAX_TOUCHED_PATHS


def test_extract_touched_paths_normal_patch_passes():
    """Real-shape patch with three files still produces the expected
    set; the cap is invisible below the threshold."""
    body = (
        "diff --git a/a.c b/a.c\n"
        "diff --git a/b.c b/b.c\n"
        "diff --git a/sub/c.c b/sub/c.c\n"
    )
    assert extract_touched_paths(body) == {"a.c", "b.c", "sub/c.c"}


# ----- rendering: Pygments block-size clamp -------------------------------


def test_oversized_code_block_falls_back_to_text_lexer():
    """A code block past `PYGMENTS_MAX_BLOCK_CHARS` renders through
    `TextLexer` instead of the chosen language. Defends against a
    crafted body pinning a gunicorn worker on a pathological
    Pygments regex run.

    Detection: the language-coloured spans (`class="k"` etc.) only
    show up under a real lexer. A TextLexer pass produces no token
    classes, just the raw text inside the highlight container."""
    # Build something safely above the cap; after splitlines + rejoin
    # in the renderer the per-line newlines are preserved so the
    # rejoined length is roughly equal to the source.
    line = "int main(void) { return 0; }"
    n_lines = (PYGMENTS_MAX_BLOCK_CHARS // len(line)) + 200
    block = _Block(kind="code", lines=[line] * n_lines, info="c")
    html = _render_block(block, msgid_urls={})
    # TextLexer fallback: no per-token class spans (no keyword `k`,
    # name-function `nf`, etc.).
    assert 'class="k"' not in html
    assert 'class="nf"' not in html
    # Content still renders (wrapped in the highlight div).
    assert "int main" in html


def test_normal_code_block_keeps_syntax_highlighting():
    """Sanity check that the clamp doesn't fire on a small block."""
    block = _Block(
        kind="code",
        lines=["int main(void) { return 0; }"],
        info="c",
    )
    html = _render_block(block, msgid_urls={})
    # Below the threshold the CLexer's tokens land as class-named
    # spans.
    assert 'class="k"' in html or 'class="kt"' in html  # at least one keyword token


# ----- canonical: tooltip-address sanitizer -------------------------------


def test_extract_list_addresses_strips_control_bytes():
    """A To/Cc value with embedded control bytes / surrogates must
    not flow into the rendered `data-tooltip`. The sanitizer
    strips them before emitting the address."""
    headers = {
        # Embedded \x01 control byte plus a surrogate-range codepoint.
        "To": "linux-arm-kernel\x01@lists.infradead.org",
    }
    addrs = extract_list_addresses(headers)
    assert addrs == ["linux-arm-kernel@lists.infradead.org"]
    for a in addrs:
        assert "\x01" not in a
        assert "\ud800" not in a


def test_extract_list_addresses_caps_per_address_length():
    """An address with a pathological multi-KB localpart gets
    truncated to the RFC 5321 254-char path cap before being
    list-matched. (A real such address wouldn't be list-shaped
    after truncation anyway, so this is bounding the cost of
    even attempting the match.)"""
    big_local = "x" * 3000
    headers = {"To": f"{big_local}@lists.infradead.org"}
    addrs = extract_list_addresses(headers)
    # Either dropped (truncated tail breaks domain match) or
    # truncated; the contract is "doesn't produce a multi-KB string."
    for a in addrs:
        assert len(a) <= 254


# ----- canonical: LIST_HOST_SUFFIXES env override -------------------------


def test_effective_suffixes_returns_baseline_by_default():
    """No override → effective set equals the baseline constant."""
    assert _effective_suffixes() == LIST_HOST_SUFFIXES


def test_effective_suffixes_unions_overrides(monkeypatch):
    """Operator override unions with the baseline so the operator
    can add lists from a domain we don't ship as a default without
    losing baseline coverage."""
    monkeypatch.setattr(
        settings,
        "list_host_suffix_overrides",
        ["lists.example.org", "lists.acme.io"],
    )
    eff = _effective_suffixes()
    # Override entries present.
    assert "lists.example.org" in eff
    assert "lists.acme.io" in eff
    # Baseline still present.
    for s in LIST_HOST_SUFFIXES:
        assert s in eff


def test_is_list_address_honors_override(monkeypatch):
    """End-to-end: an address on an override-only suffix matches
    `is_list_address` after the override is set, and doesn't match
    without it."""
    addr = "x@lists.example.org"
    assert is_list_address(addr) is False
    monkeypatch.setattr(
        settings,
        "list_host_suffix_overrides",
        ["lists.example.org"],
    )
    assert is_list_address(addr) is True
