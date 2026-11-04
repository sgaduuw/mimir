"""Thread attribution follows MAINTAINERS list contacts, not traffic."""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Inbox, Subsystem, SubsystemList
from tests.test_routes._helpers import _seed_subsystem
from tests.test_routes.test_thread_header import _seed_header_thread


def _seed(tmp_path):
    seeded = _seed_header_thread(tmp_path)
    _seed_subsystem("UNRELATED", "Maintained", ["unrelated/"])
    with SessionLocal() as session:
        ids = dict(session.execute(select(Subsystem.name, Subsystem.id)).all())
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        inbox.list_address = "  LIST@Custom.Example  "
        session.commit()
    return seeded["c@x"][1] + "/t", ids


def _contacts(ids, *names, address="list@custom.example"):
    with SessionLocal() as session:
        session.add_all(
            SubsystemList(address=address, subsystem_id=ids[name]) for name in names
        )
        session.commit()


def _order(response, markdown):
    body = response.get_data(as_text=True)
    if markdown:
        subs = re.search(r"^- Subsystems: (.*)$", body, re.MULTILINE).group(1)
        maints = re.search(r"^- Maintainers: (.*)$", body, re.MULTILINE).group(1)
        return subs.split(", "), maints.split(", ")
    cards = {}
    for kind in ("subsystems", "maintainers"):
        cards[kind] = re.search(
            rf'aria-labelledby="card-{kind}">(.*?)</section>', body, re.DOTALL
        ).group(1)
    return (
        [
            s.upper()
            for s in re.findall(
                r'<li class="chip"><a[^>]*>([^<]*)', cards["subsystems"]
            )
        ],
        re.findall(r"Maint \w+", cards["maintainers"]),
    )


@pytest.mark.parametrize("markdown", [False, True])
def test_all_owned_hits_lead_in_stable_order_despite_busy_other_subsystem(
    client, tmp_path, markdown
):
    from mimir import cache
    from mimir.subsystems_dashboard.activity import SubsystemActivity

    url, ids = _seed(tmp_path)
    _contacts(ids, "DDD", "BBB", "UNRELATED")
    cache.set(
        "most_active_subsystems_in_inbox:alpha:7",
        [SubsystemActivity(ids["CCC"], "CCC", "alpha", 500, datetime.now(UTC))],
        3600,
    )
    assert cache.get("most_active_subsystems_in_inbox:alpha:7")[0].id == ids["CCC"]
    response = client.get(url + (".md" if markdown else ""))
    assert response.status_code == 200
    assert _order(response, markdown) == (
        ["BBB", "DDD", "AAA", "CCC"],
        ["Maint BBB", "Maint DDD", "Maint AAA", "Maint CCC"],
    )


@pytest.mark.parametrize("markdown", [False, True])
@pytest.mark.parametrize("change", ["add", "remove", "reassign"])
def test_warm_conditional_request_sees_contact_changes(
    client, tmp_path, markdown, change
):
    url, ids = _seed(tmp_path)
    url += ".md" if markdown else ""
    if change != "add":
        _contacts(ids, "DDD")
    if change == "reassign":
        _contacts(ids, "BBB", address="other@custom.example")
    before = client.get(url)
    assert before.status_code == 200
    expected_before = (
        ["AAA", "BBB", "CCC", "DDD"]
        if change == "add"
        else ["DDD", "AAA", "BBB", "CCC"]
    )
    assert _order(before, markdown)[0] == expected_before
    assert (
        client.get(url, headers={"If-None-Match": before.headers["ETag"]}).status_code
        == 304
    )
    with SessionLocal() as session:
        if change == "add":
            session.add(
                SubsystemList(address="list@custom.example", subsystem_id=ids["DDD"])
            )
        elif change == "remove":
            session.delete(
                session.get(SubsystemList, ("list@custom.example", ids["DDD"]))
            )
        else:
            session.scalar(
                select(Inbox).where(Inbox.name == "alpha")
            ).list_address = "other@custom.example"
        session.commit()
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    expected_after = {
        "add": ["DDD", "AAA", "BBB", "CCC"],
        "remove": ["AAA", "BBB", "CCC", "DDD"],
        "reassign": ["BBB", "AAA", "CCC", "DDD"],
    }[change]
    assert _order(after, markdown) == (
        expected_after,
        [f"Maint {name}" for name in expected_after],
    )
    assert (
        client.get(url, headers={"If-None-Match": after.headers["ETag"]}).status_code
        == 304
    )


@pytest.mark.parametrize(
    "address", [None, "", "unknown@example.org", "list@custom.example"]
)
def test_no_owned_attributed_hit_keeps_plain_order(client, tmp_path, address):
    url, ids = _seed(tmp_path)
    _contacts(ids, "UNRELATED")
    with SessionLocal() as session:
        session.scalar(
            select(Inbox).where(Inbox.name == "alpha")
        ).list_address = address
        session.commit()
    response = client.get(url)
    assert response.status_code == 200
    assert _order(response, False)[0] == ["AAA", "BBB", "CCC", "DDD"]


def test_equivalent_normalized_address_keeps_validator(client, tmp_path):
    url, ids = _seed(tmp_path)
    _contacts(ids, "BBB")
    before = client.get(url)
    with SessionLocal() as session:
        session.scalar(
            select(Inbox).where(Inbox.name == "alpha")
        ).list_address = "list@custom.example"
        session.commit()
    assert (
        client.get(url, headers={"If-None-Match": before.headers["ETag"]}).status_code
        == 304
    )


def test_address_change_is_a_validator_input_even_for_the_same_owned_ids(
    client, tmp_path
):
    url, ids = _seed(tmp_path)
    _contacts(ids, "BBB")
    _contacts(ids, "BBB", address="other@custom.example")
    before = client.get(url)
    with SessionLocal() as session:
        session.scalar(
            select(Inbox).where(Inbox.name == "alpha")
        ).list_address = "other@custom.example"
        session.commit()
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    assert _order(after, False) == _order(before, False)


def test_shared_maintainer_stays_once_in_first_owned_hit_order(client, tmp_path):
    from mimir.models import SubsystemMaintainer

    url, ids = _seed(tmp_path)
    _contacts(ids, "DDD", "BBB")
    with SessionLocal() as session:
        # The same maintainer occurs in both groups, under distinct names.
        session.add(
            SubsystemMaintainer(
                subsystem_id=ids["AAA"],
                role="M",
                name="Other spelling",
                address="bbb@kernel.org",
            )
        )
        session.commit()
    response = client.get(url)
    assert _order(response, False)[1] == [
        "Maint BBB",
        "Maint DDD",
        "Maint AAA",
        "Maint CCC",
    ]
    assert "Other spelling" not in response.get_data(as_text=True)


@pytest.mark.parametrize("markdown", [False, True])
def test_force_populates_unchanged_blob_and_revalidates_warm_thread(
    client, tmp_path, markdown
):
    from mimir.mainline import load_maintainers
    from tests.test_cli_maintainers import _build_mainline_repo

    url, _ = _seed(tmp_path)
    url += ".md" if markdown else ""
    source = (
        "\n\n".join(
            f"{name}\nM: Maint {name} <{name.lower()}@kernel.org>\nF: fs/{name.lower()}/"
            + ("\nL: list@custom.example" if name == "DDD" else "")
            for name in ("AAA", "BBB", "CCC", "DDD")
        ).encode()
        + b"\n"
    )
    repo = tmp_path / "linux.git"
    _build_mainline_repo(repo, source)
    assert load_maintainers(repo)[0] is True
    # Model an upgraded database with the blob cursor but no L rows.
    with SessionLocal() as session:
        session.query(SubsystemList).delete()
        session.commit()
    before = client.get(url)
    assert _order(before, markdown)[0] == ["AAA", "BBB", "CCC", "DDD"]
    assert load_maintainers(repo)[0] is False
    assert (
        client.get(url, headers={"If-None-Match": before.headers["ETag"]}).status_code
        == 304
    )
    assert load_maintainers(repo, force=True)[0] is True
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert _order(after, markdown)[0] == ["DDD", "AAA", "BBB", "CCC"]
    assert (
        client.get(url, headers={"If-None-Match": after.headers["ETag"]}).status_code
        == 304
    )


def test_ownership_reads_once_on_200_and_304_using_address_index(client, tmp_path):
    from sqlalchemy import event

    from mimir.extensions import engine

    url, ids = _seed(tmp_path)
    _contacts(ids, "DDD", "BBB")
    reads = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM subsystem_lists" in statement:
            reads.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = client.get(url)
        assert first.status_code == 200
        assert len(reads) == 1
        second = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        assert second.status_code == 304
        assert len(reads) == 2
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert reads[0] == reads[1]
    statement, parameters = reads[0]
    assert parameters == ("list@custom.example",)
    with engine.connect() as conn:
        plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
    assert any(
        "SEARCH subsystem_lists USING COVERING INDEX" in row[3]
        and "address=?" in row[3]
        for row in plan
    ), plan
