"""Front-page merge-window figures reuse independently warmed snapshots."""

from datetime import datetime

import pytest

import mimir.linus_pulls as pulls_module
import mimir.merge_window as window_module
from mimir.linus_pulls import LinusPull, LinusPulls
from mimir.merge_window import MergeWindow, MergeWindowSection
from tests._dom import parse

FIGURES = {"queued-patches", "queued-subsystems", "pulls-merged", "pulls-waiting"}


def _snapshots(tag="v7.4-rc4", *, walked=True):
    def section(name, patches=30):
        return MergeWindowSection(name=name, anchor=name, patches=patches, rows=[])

    window = MergeWindow(
        sections=[section("large-a"), section("large-b")],
        smaller=[section("small-a", 1), section("small-b", 1), section("small-c", 1)],
        queued=12345,
        not_archived=3,
        queued_walked_at=datetime(2026, 11, 26, 9, 7) if walked else None,
        **window_module._window(tag),
    )
    states = ["merged"] * 3 + ["pending"] * 2 + ["unknown"] * 4 + ["rejected"]
    pulls = LinusPulls(
        anchor="v7.3",
        rows=[
            LinusPull(
                subject=f"Pull {i}",
                url=f"/pull-{i}",
                author=None,
                date=None,
                state=state,
            )
            for i, state in enumerate(states)
        ],
    )
    return window, pulls


def _warm(session, monkeypatch, *, queue=True, pulls=True, tag="v7.4-rc4", walked=True):
    window, requests = _snapshots(tag, walked=walked)
    with monkeypatch.context() as warm:
        warm.setattr(window_module, "_compute", lambda s: window)
        warm.setattr(pulls_module, "_compute", lambda s: requests)
        if queue:
            assert window_module.merge_window(session) == window
        if pulls:
            assert pulls_module.linus_pulls(session) == requests

    def forbidden(*args, **kwargs):
        pytest.fail("Front page computed merge-window data")

    monkeypatch.setattr(window_module, "_compute", forbidden)
    monkeypatch.setattr(pulls_module, "_compute", forbidden)


@pytest.mark.parametrize(
    "tag,pill",
    [("v7.4", "Open since v7.4"), ("v7.4-rc4", "Closed until v7.4 is released")],
)
def test_front_merge_window_uses_warmed_figures(
    client, session, monkeypatch, tag, pill
):
    _warm(session, monkeypatch, tag=tag)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=300"
    assert "ETag" not in response.headers
    page = parse(response.get_data(as_text=True))
    assert page.find("span", cls="front-merge-window-status").text() == pill
    figures = {
        el.attrs["data-figure"]: el.text()
        for el in page.find_all()
        if el.attrs.get("data-figure") in FIGURES
    }
    assert figures == {
        "queued-patches": "12,345",
        "queued-subsystems": "5",
        "pulls-merged": "3",
        "pulls-waiting": "2",
    }
    updated = page.find("time", cls="front-merge-window-updated")
    assert updated.text() == "09:07 UTC"
    assert updated.attrs["datetime"] == "2026-11-26T09:07:00+00:00"
    assert tag in page.text()
    assert "v7.5" not in page.text()
    reference = parse(client.get("/merge-window").get_data(as_text=True))
    assert reference.find_all("span", cls="pill")[0].text() == pill
    assert (
        reference.find("section", id="pulls-merged").find("p", cls="count").text()
        == figures["pulls-merged"]
    )
    assert (
        reference.find("section", id="pulls-pending").find("p", cls="count").text()
        == figures["pulls-waiting"]
    )


@pytest.mark.parametrize("queue,pulls", [(False, False), (True, False), (False, True)])
def test_front_merge_window_cold_snapshots_are_independent(
    client, session, monkeypatch, queue, pulls
):
    _warm(session, monkeypatch, queue=queue, pulls=pulls)
    response = client.get("/")
    assert response.status_code == 200
    page = parse(response.get_data(as_text=True))
    for name in ("queued-patches", "queued-subsystems"):
        assert len(page.find_all(**{"data-figure": name})) == int(queue)
    for name in ("pulls-merged", "pulls-waiting"):
        assert len(page.find_all(**{"data-figure": name})) == int(pulls)
    assert len(page.find_all("time", cls="front-merge-window-updated")) == int(queue)
    assert page.find("h2", id="merge-window").find("a").attrs["href"] == "/merge-window"


def test_front_merge_window_omits_missing_walk_time_and_reads_each_state_once(
    client, session, monkeypatch
):
    _warm(session, monkeypatch, walked=False)
    calls = {"queue": 0, "pulls": 0}
    for module, name, kind in (
        (window_module, "merge_window_state", "queue"),
        (pulls_module, "linus_pulls_state", "pulls"),
    ):
        original = getattr(module, name)

        def counted(s, original=original, kind=kind):
            calls[kind] += 1
            return original(s)

        monkeypatch.setattr(module, name, counted)
    response = client.get("/")
    assert response.status_code == 200
    assert calls == {"queue": 1, "pulls": 1}
    assert not parse(response.get_data(as_text=True)).find_all(
        "time", cls="front-merge-window-updated"
    )


def test_front_merge_window_drops_old_snapshots_after_state_changes(
    client, session, monkeypatch
):
    from mimir.models import MainlineState

    _warm(session, monkeypatch)
    old_queue = window_module.merge_window_state(session)
    old_pulls = pulls_module.linus_pulls_state(session)
    session.merge(
        MainlineState(
            tree_name="linus",
            last_walked_at=datetime(2026, 11, 26, 11, 0),
            last_commit_sha="changed",
        )
    )
    session.commit()
    assert window_module.merge_window_state(session) != old_queue
    assert pulls_module.linus_pulls_state(session) != old_pulls
    assert (
        window_module.merge_window(session, old_queue, compute_on_miss=False)
        is not None
    )
    assert (
        pulls_module.linus_pulls(session, old_pulls, compute_on_miss=False) is not None
    )
    response = client.get("/")
    assert response.status_code == 200
    page = parse(response.get_data(as_text=True))
    assert not [el for el in page.find_all() if el.attrs.get("data-figure") in FIGURES]


def test_front_merge_window_warm_zeroes_are_visible(client, session, monkeypatch):
    mw, pulls = _snapshots()
    mw.sections = []
    mw.smaller = []
    mw.queued = 0
    pulls.rows = []
    with monkeypatch.context() as warm:
        warm.setattr(window_module, "_compute", lambda s: mw)
        warm.setattr(pulls_module, "_compute", lambda s: pulls)
        window_module.merge_window(session)
        pulls_module.linus_pulls(session)

    def forbidden(*args, **kwargs):
        pytest.fail("Front page computed warmed zero figures")

    monkeypatch.setattr(window_module, "_compute", forbidden)
    monkeypatch.setattr(pulls_module, "_compute", forbidden)
    page = parse(client.get("/").get_data(as_text=True))
    figures = {
        el.attrs["data-figure"]: el.text()
        for el in page.find_all()
        if el.attrs.get("data-figure") in FIGURES
    }
    assert figures == dict.fromkeys(FIGURES, "0")


def test_front_merge_window_converts_offset_walk_time_to_utc(
    client, session, monkeypatch
):
    from datetime import timedelta, timezone

    mw, pulls = _snapshots()
    mw.queued_walked_at = datetime(
        2026, 11, 26, 11, 7, tzinfo=timezone(timedelta(hours=2))
    )
    with monkeypatch.context() as warm:
        warm.setattr(window_module, "_compute", lambda s: mw)
        warm.setattr(pulls_module, "_compute", lambda s: pulls)
        window_module.merge_window(session)
        pulls_module.linus_pulls(session)

    def forbidden(*args, **kwargs):
        pytest.fail("Front page computed cached figures")

    monkeypatch.setattr(window_module, "_compute", forbidden)
    monkeypatch.setattr(pulls_module, "_compute", forbidden)
    page = parse(client.get("/").get_data(as_text=True))
    updated = page.find("time", cls="front-merge-window-updated")
    assert updated.text() == "09:07 UTC"
    assert updated.attrs["datetime"] == "2026-11-26T09:07:00+00:00"
