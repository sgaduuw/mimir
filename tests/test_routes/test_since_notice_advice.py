"""#799 review: the truncation notice must not promise something the view
cannot do. The list keeps the threads with the latest activity; a later
`since` date only drops older threads, so it never reveals a hidden one."""

from datetime import UTC, datetime, timedelta

from tests.test_routes.test_timeviews import _seed_since_threads, _shrink_since_cap


def test_pick_a_later_date_reveals_a_hidden_thread(
    client, inbox_name, frozen_clock, monkeypatch
):
    _shrink_since_cap(monkeypatch, 3)
    _seed_since_threads(120)  # one per hour over five days, 119 newest
    now = datetime.now(UTC)
    first = client.get(
        f"/{inbox_name}/since/{(now - timedelta(days=7)):%Y-%m-%d}"
    ).data.decode()
    hidden = [f"since cap {i}<" for i in range(120) if f"since cap {i}<" not in first]
    assert hidden, "setup: the cap must hide something"
    if "pick a later" not in first:
        return  # the notice makes no such promise
    # Every later date the view accepts, up to today.
    revealed = set()
    for days in range(6, -1, -1):
        body = client.get(
            f"/{inbox_name}/since/{(now - timedelta(days=days)):%Y-%m-%d}"
        ).data.decode()
        revealed |= {h for h in hidden if h in body}
    assert revealed, (
        "the notice says to pick a later date to see the rest, but no later "
        "date shows any of the hidden threads"
    )
