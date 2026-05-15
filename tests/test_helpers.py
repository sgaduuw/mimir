"""Helper-function contracts for `mimir.store` and `mimir.web`.

The route smoke tests cover the happy-path read of these end-to-
end. This file pins the error paths and the privacy / RFC-6266
edge cases that would never surface from real-data smoke runs.
"""
from sqlalchemy import select

import pytest

from mimir.models import Inbox
from mimir.store import MessageNotFound, read_message
from mimir.web import _content_disposition, _redact_trailer_address, _safe_from_filter


def _alpha(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


# store.read_message, error paths


def test_read_message_unknown_message_id_raises(seeded_db):
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound):
        read_message(s, alpha, "nonexistent@example.com")


def test_read_message_message_in_other_inbox_raises(seeded_db):
    """art2 is beta-only. Asking alpha for it must 404, even
    though the Message-ID exists in DB."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound):
        read_message(s, alpha, "art2@example.com")


def test_read_message_missing_mirror_raises(seeded_db):
    """The seed sets mirror_path=/tmp/alpha which doesn't exist on
    disk. A read against a real Message-ID should raise
    MessageNotFound with the path-not-found message rather than
    crashing trying to open the repo."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound, match="not found"):
        read_message(s, alpha, "art1@example.com")


def test_read_message_stale_commit_sha_raises(seeded_db, tmp_path):
    """A real-but-stale commit_sha (mirror got rewritten / blob GC'd)
    must surface as MessageNotFound, not bubble out as a KeyError
    that 500s the message route. dulwich raises KeyError when the
    commit isn't in the object store; _read_blob now catches and
    converts."""
    from dulwich.repo import Repo as DulwichRepo
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

    # Empty bare repo so the epoch_path.exists() guard passes but
    # the commit_sha lookup inside doesn't.
    epoch_dir = tmp_path / "0.git"
    DulwichRepo.init_bare(str(epoch_dir), mkdir=True)

    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one()
        ix.mirror_path = str(tmp_path)
        # art1 is alpha-only in the seeded DB; the seed left
        # commit_sha = "aa" * 20, which won't be in our empty bare
        # repo. That's the stale-row shape we want to exercise.
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == ix.id)
            .values(epoch="0.git")
        )
        s.commit()

    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound, match="blob"):
        read_message(s, alpha, "art1@example.com")


# web._safe_from_filter, privacy redaction


def test_safe_from_kernel_org_address_surfaces_in_full():
    out = _safe_from_filter("Linus Torvalds <torvalds@linux-foundation.org>")
    # Default email_allowlist contains "torvalds@", full surfaces.
    assert "torvalds@linux-foundation.org" in out


def test_safe_from_kernel_org_domain_surfaces_in_full():
    """`@kernel.org` is in the default allowlist."""
    out = _safe_from_filter("Greg KH <greg@kernel.org>")
    assert "greg@kernel.org" in out


def test_safe_from_other_domain_redacts_address_keeps_name():
    out = _safe_from_filter("Joe User <joe@example.com>")
    assert "joe@example.com" not in out
    assert "Joe User" in out
    assert "<hidden>" in out


def test_safe_from_no_display_name_only_redacts_to_hidden():
    out = _safe_from_filter("anon@example.com")
    assert "anon@example.com" not in out
    assert out == "<hidden>"


def test_safe_from_empty_returns_empty_string():
    assert _safe_from_filter(None) == ""
    assert _safe_from_filter("") == ""


# web._content_disposition, RFC 6266


def test_content_disposition_no_filename():
    assert _content_disposition(None) == "attachment"
    assert _content_disposition("") == "attachment"


def test_content_disposition_ascii_filename():
    cd = _content_disposition("patch.diff")
    # ASCII filename; both forms emitted.
    assert 'filename="patch.diff"' in cd
    assert "filename*=UTF-8''patch.diff" in cd


def test_content_disposition_strips_quote_and_backslash_in_ascii_form():
    """`"` and `\\` in the ASCII filename break the simple
    `filename="..."` quoting; the helper strips them. The full
    name is preserved in the `filename*=` form via percent-encoding."""
    cd = _content_disposition('weird "name" with \\backslash.txt')
    # ASCII form has the quotes and backslashes stripped.
    assert "weird name with backslash.txt" in cd
    # Percent-encoded form preserves them via URL escaping.
    assert "filename*=UTF-8''" in cd
    assert "%22" in cd  # encoded "
    assert "%5C" in cd  # encoded \\


def test_content_disposition_non_ascii_filename():
    """Non-ASCII filenames go through RFC 6266's filename* with
    percent-encoded UTF-8 octets."""
    cd = _content_disposition("café.txt")
    # The filename* form must percent-encode the non-ASCII bytes.
    assert "filename*=UTF-8''" in cd
    # 'é' is U+00E9 → UTF-8 0xC3 0xA9 → %C3%A9
    assert "%C3%A9" in cd


def test_content_disposition_strips_control_bytes_from_ascii_form():
    """Control bytes (CR, LF, NUL, tab, DEL) in the ASCII `filename="…"`
    form would let a maliciously-crafted attachment filename inject
    extra HTTP response headers (RFC 7230 header-line splitting).
    Defense in depth on top of whatever the WSGI layer rejects: strip
    them before they reach the header value. The percent-encoded
    `filename*` form is unaffected, quote() already escapes them."""
    cd = _content_disposition("evil\r\nX-Injected: yes.txt")
    assert "\r" not in cd
    assert "\n" not in cd
    # The ASCII form survives with the control bytes excised.
    assert 'filename="evilX-Injected: yes.txt"' in cd
    # The filename* form percent-encodes them.
    assert "%0D" in cd
    assert "%0A" in cd


# web._redact_trailer_address, DCO trailer redaction


def test_redact_trailer_address_allowlisted_returns_visible_angle_form():
    """Allowlisted addresses surface inside literal angle brackets so the
    rendered output reads `<addr@kernel.org>`, the renderer escapes the
    return value, so the redactor returns plain text with raw angle
    brackets and the browser sees them as visible characters."""
    out = _redact_trailer_address("torvalds@kernel.org")
    assert out == "<torvalds@kernel.org>"


def test_redact_trailer_address_non_allowlisted_returns_redacted():
    """Non-allowlisted addresses collapse to a single `<redacted>` token
   , plain text, again rendered as visible characters after the
    template's escaping pass."""
    out = _redact_trailer_address("random@example.com")
    assert out == "<redacted>"


def test_redact_trailer_address_substring_match_is_intentionally_loose(monkeypatch):
    """The allowlist uses substring matching, by design (see CONTEXT.md
   , an allowlist token matches any address containing that substring).
    This is intentional looseness for ergonomics; pin it here so a
    future tightening is a conscious decision, not a silent drift."""
    from mimir.config import settings
    # Set an explicit token to make the assertion deterministic, the
    # default allowlist contains the same token but pinning it here
    # keeps the test independent of the default's evolution.
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    out = _redact_trailer_address("attacker@kernel.org.evil.example")
    # Substring `@kernel.org` matches at position 8 in the address,
    # even though the actual host is `kernel.org.evil.example`. The
    # redactor returns the allowlisted form.
    assert out == "<attacker@kernel.org.evil.example>"
