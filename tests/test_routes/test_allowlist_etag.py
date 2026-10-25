"""The static email allowlist is a validator input on every page with an
ETag (#900).

`Settings.email_allowlist` is an env knob: changing it restarts the same
image, so `mimir.__version__` does not move. Removing a token must still
reach caches, or they keep a page showing an address it now hides.
"""

import pytest

from mimir.config import settings
from mimir.web.urls import markdown_path
from tests.test_routes._helpers import (
    _ingest_one_article,
    _seed_three_message_thread,
)

ADDRESS = "dev@open.example"


def _message(client, tmp_path, inbox_name):
    _, url = _ingest_one_article(tmp_path, inbox_name, "al-etag@x", author=ADDRESS)
    return url


def _thread(client, tmp_path, inbox_name):
    t = _seed_three_message_thread(tmp_path, inbox_name, authors=(ADDRESS,) * 3)
    return t["root"][1] + "/t"


def _merge_window(client, tmp_path, inbox_name):
    from mimir.extensions import SessionLocal
    from tests.test_merge_window import MERGE_WINDOW_PATH, _article, _commit

    with SessionLocal() as s:
        _article(s, "al-mw@x").author = ADDRESS
        _commit(s, "al-mw@x", "linux-next")
        s.commit()
    return MERGE_WINDOW_PATH


@pytest.mark.parametrize(
    ("page", "md"),
    [
        (_message, False),
        (_message, True),
        (_thread, False),
        (_thread, True),
        (_merge_window, False),
    ],
    ids=["message", "message.md", "thread", "thread.md", "merge-window"],
)
def test_removing_an_allowlist_token_moves_the_etag(
    client, tmp_path, inbox_name, monkeypatch, page, md
):
    url = page(client, tmp_path, inbox_name)
    if md:
        url = markdown_path(url)

    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    first = client.get(url)
    assert first.status_code == 200
    # Precondition: the allowlisted address is on the page.
    assert ADDRESS in first.get_data(as_text=True)

    monkeypatch.setattr(settings, "email_allowlist", [])
    again = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 200
    assert again.headers["ETag"] != first.headers["ETag"]
    assert ADDRESS not in again.get_data(as_text=True)
