"""One field structure across lookup, search, live filters and diff navigation."""

import pytest

from tests._dom import parse


@pytest.mark.parametrize(
    "surface", ["front", "404", "inbox", "search", "merge", "diff"]
)
def test_every_search_field_uses_the_shared_label_and_group(client, tmp_path, surface):
    ids = {
        "front": ["message-id-input", "inbox-find"],
        "404": ["message-id-input"],
        "inbox": ["inbox-query"],
        "search": ["search-query"],
        "merge": ["mw-find-input"],
        "diff": ["series-diff-position"],
    }[surface]
    path = {
        "front": "/",
        "404": "/not-a-route",
        "inbox": "/alpha/",
        "search": "/alpha/search",
    }.get(surface)
    if surface == "merge":
        from mimir.extensions import SessionLocal
        from tests.test_merge_window import _seed_postings

        with SessionLocal() as session:
            _seed_postings(session, {"FIELD TEST": 2})
        path = "/merge-window"
    elif surface == "diff":
        from tests.test_routes._helpers import _ingest_series_pair

        key = _ingest_series_pair(
            tmp_path,
            "alpha",
            [("field-v1@x", "[PATCH 0/1] field test", None, b"old cover\n", "A <a@x>")],
            [
                (
                    "field-v2@x",
                    "[PATCH v2 0/1] field test",
                    None,
                    b"new cover\n",
                    "A <a@x>",
                )
            ],
        )
        path = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=cover"
    response = client.get(path)
    assert response.status_code == (404 if surface == "404" else 200)
    page = parse(response.get_data(as_text=True))
    fields = page.find_all("div", cls="search-field")
    assert len(fields) == len(ids), (surface, len(fields))
    for field, field_id in zip(fields, ids, strict=True):
        label = field.find("label", for_=field_id)
        control = field.find(id=field_id)
        assert label.parent is field
        assert label.text()
        buttons = field.find_all("button")
        if field_id in {"inbox-find", "mw-find-input"}:
            assert control.parent is field
            assert not buttons
            assert "data-filter-input" in control.attrs
        else:
            group = field.find("fieldset", role="group")
            assert control.parent is group
            assert len(buttons) == 1 and buttons[0].parent is group
            assert buttons[0].attrs["type"] == "submit"
            assert buttons[0].classes == ["secondary"]
            assert "hidden" not in buttons[0].attrs  # native fallback before JS
    assert not [
        f for f in page.find_all("form") if f.attrs.get("role") in {"search", "group"}
    ]
    assert not [
        g for g in page.find_all("fieldset", role="group") if g.find_all("label")
    ]
