"""`normalise_message_id`: the one reader of a Message-ID a person typed.

The form route and both `/m/` routes call it, so what the form accepts and
what the routes accept cannot drift apart (#984).
"""

import pytest

from mimir.web.routes.message_id import normalise_message_id

ID = "20260101.12345-1-someone@example.org"

ACCEPTED = [
    # A bare ID, with the wrappers people paste around it.
    (ID, ID),
    (f"<{ID}>", ID),
    (f"mid:{ID}", ID),
    (f"mid:<{ID}>", ID),
    (f"  {ID}\n", ID),
    # A real ID may contain a slash.
    ("a/b@host.example", "a/b@host.example"),
    # lore's own shapes, and the same paths pasted onto this host.
    (f"https://lore.kernel.org/linux-fsdevel/{ID}/", ID),
    (f"https://lore.kernel.org/linux-fsdevel/{ID}", ID),
    (f"http://lore.kernel.org/all/{ID}/", ID),
    (f"/all/{ID}/", ID),
    (f"/r/{ID}", ID),
    (f"/linux-fsdevel/{ID}/", ID),
    # Percent-encoded, as a browser address bar leaves it.
    (f"/linux-fsdevel/{ID.replace('@', '%40')}/", ID),
    (f"/r/%3C{ID}%3E", ID),
    # lore's thread, raw and mbox forms keep the ID before the suffix.
    (f"https://lore.kernel.org/x/{ID}/T/#u", ID),
    (f"https://lore.kernel.org/x/{ID}/T/", ID),
    (f"https://lore.kernel.org/x/{ID}/raw", ID),
    (f"https://lore.kernel.org/x/{ID}/t.mbox.gz", ID),
    (f"/r/{ID}/raw", ID),
    # A query string or fragment is not part of the ID.
    (f"https://lore.kernel.org/x/{ID}/?q=1#frag", ID),
]

REJECTED = [
    None,
    "",
    "   ",
    "<>",
    "mid:",
    "has space@example.org",
    "tab\t@example.org",
    "nul\x00@example.org",
    "ctl\x1f@example.org",
    "del\x7f@example.org",
    "x" * 999,
    # A path or URL with no ID segment.
    "/",
    "/all/",
    "/linux-fsdevel/",
    "https://lore.kernel.org/",
    "https://lore.kernel.org/linux-fsdevel/",
    # Not a web URL: only the ID is ever taken from it, and only from http(s).
    f"ftp://lore.kernel.org/x/{ID}/",
    f"javascript://x/{ID}",
    # Percent-encoded control byte.
    "/r/a%00@example.org",
    # urlsplit refuses this one.
    "http://[::1/x@y",
]


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED)
def test_accepts(raw, expected):
    assert normalise_message_id(raw) == expected


@pytest.mark.parametrize("raw", REJECTED)
def test_rejects(raw):
    assert normalise_message_id(raw) is None
