"""Helper-function contracts for `mimir.store` and `mimir.web`.

The route smoke tests cover the happy-path read of these end-to-
end. This file pins the error paths and the privacy / RFC-6266
edge cases that would never surface from real-data smoke runs.
"""
from sqlalchemy import select

import pytest

from mimir.models import Inbox
from mimir.store import MessageNotFound, read_message
from mimir.web import _content_disposition, _safe_from_filter


def _alpha(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


# store.read_message — error paths


def test_read_message_unknown_message_id_raises(seeded_db):
    alpha = _alpha(seeded_db)
    with seeded_db() as s, pytest.raises(MessageNotFound):
        read_message(s, alpha, "nonexistent@example.com")


def test_read_message_message_in_other_inbox_raises(seeded_db):
    """art2 is beta-only. Asking alpha for it must 404 — even
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


# web._safe_from_filter — privacy redaction


def test_safe_from_kernel_org_address_surfaces_in_full():
    out = _safe_from_filter("Linus Torvalds <torvalds@linux-foundation.org>")
    # Default email_allowlist contains "torvalds@" — full surfaces.
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


# web._content_disposition — RFC 6266


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
