"""The subsystem hub renders only addressable, warmed activity rows."""

import re
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest

from mimir.subsystems import subsystem_path
from mimir.subsystems_dashboard.activity import SubsystemActivity
from tests._dom import parse


def _activity(name, count, **kwargs):
    return SubsystemActivity(
        id=1,
        name=name,
        inbox_name="alpha",
        message_count=count,
        last_activity=datetime(2026, 11, 2, 12, 34, tzinfo=UTC),
        **kwargs,
    )


def _render(client, monkeypatch, rows):
    from mimir.web.routes import dashboards

    calls = []

    def cached(session, inbox, **kwargs):
        calls.append((inbox.name, kwargs))
        return rows

    monkeypatch.setattr(dashboards, "most_active_subsystems_in_inbox", cached)
    response = client.get("/alpha/subsystem/")
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == "alpha"
    assert calls[0][1]["compute_on_miss"] is False
    return response.get_data(as_text=True)


def _table(body):
    table = re.search(
        r'<table\b[^>]*class="subsystem-index-table".*?</table>', body, re.DOTALL
    )
    assert table is not None, "expected the subsystem activity table"
    return ET.fromstring(table.group())


def test_subsystem_index_aligns_visible_rows_counts_meters_and_links(
    client, monkeypatch
):
    body = _render(
        client,
        monkeypatch,
        [
            _activity("HIDDEN\tNAME", 1000),
            _activity(
                "ARM/DEMO & DRIVERS",
                10,
                status="Supported",
                maintainer_name="Pat Example",
                multiple_maintainers=True,
            ),
            _activity(
                "SECOND", 5, status="Maintained", maintainer_name="hidden@example.com"
            ),
        ],
    )
    table = _table(body)
    assert [
        "".join(th.itertext()).strip() for th in table.findall("./thead/tr/th")
    ] == [
        "Subsystem",
        "Messages, 7 days",
        "Maintainer",
        "Last",
    ]
    rows = table.findall("./tbody/tr")
    assert len(rows) == 2
    links = [row.find("./th/a") for row in rows]
    assert [link.attrib["href"] for link in links] == [
        subsystem_path("alpha", "ARM/DEMO & DRIVERS"),
        subsystem_path("alpha", "SECOND"),
    ]
    assert [link.text for link in links] == ["arm/demo & drivers", "second"]
    assert "2 subsystems with patch activity" in body
    assert "HIDDEN" not in "".join(table.itertext())
    assert [float(row.find("./td/span/svg/rect").attrib["width"]) for row in rows] == [
        100,
        50,
    ]
    assert [row.find("./td/span/strong").text for row in rows] == ["10", "5"]
    assert rows[0].find("./th/span").text == "Supported"
    assert "pill-supported" in rows[0].find("./th/span").attrib["class"].split()
    assert rows[1].find("./th/span") is None
    assert "".join(rows[0].findall("td")[1].itertext()).strip() == "Pat Example et al."
    assert "".join(rows[1].findall("td")[1].itertext()).strip() == "none listed"
    assert rows[0].find("./td/time").text == "11-02 12:34"
    assert rows[0].find("./td/time").attrib["datetime"] == "2026-11-02T12:34:00+00:00"
    assert table.findall(".//*[@style]") == []


def test_subsystem_index_handles_zero_counts_missing_metadata_and_escaped_names(
    client, monkeypatch
):
    row = _activity("ZERO", 0, status="Orphan", maintainer_name="<b>Display Name</b>")
    row.last_activity = None
    body = _render(client, monkeypatch, [row])
    table = _table(body)
    assert "1 subsystem with patch activity" in body
    assert float(table.find("./tbody/tr/td/span/svg/rect").attrib["width"]) == 0
    assert table.find("./tbody/tr/td/span/strong").text == "0"
    assert table.find("./tbody/tr/th/span").text == "Orphan"
    assert table.findall(".//b") == []
    assert "<b>Display Name</b>" in "".join(table.itertext())
    assert "not available" in "".join(table.itertext())
    assert table.findall(".//time") == []


def test_subsystem_index_with_only_unaddressable_rows_uses_empty_state(
    client, monkeypatch
):
    body = _render(client, monkeypatch, [_activity("BAD\tNAME", 99)])
    assert "No subsystem has seen patch activity" in body
    assert 'class="subsystem-index-table"' not in body
    assert "BAD" not in body


@pytest.mark.parametrize(
    ("status", "modifier"),
    [
        ("Maintained", "pill-maintained"),
        ("mAiNtAiNeD", "pill-maintained"),
        ("Supported", "pill-supported"),
        ("Odd Fixes", None),
        ('<script>alert("x")</script>', None),
        (None, None),
    ],
)
def test_status_pill_preserves_and_escapes_text(client, status, modifier):
    template = client.application.jinja_env.from_string(
        '{% from "_pill.html" import status_pill %}{{ status_pill(status) }}'
    )
    page = parse(template.render(status=status))
    pills = page.find_all("span", cls="pill")
    if status is None:
        assert pills == []
    else:
        assert len(pills) == 1
        assert pills[0].text() == status
        assert pills[0].classes == ["pill"] + ([modifier] if modifier else [])
        assert page.find_all("script") == []


def test_subsystem_index_keeps_mixed_case_maintained_neutral(client, monkeypatch):
    body = _render(client, monkeypatch, [_activity("EXAMPLE", 1, status="mAiNtAiNeD")])
    pill = _table(body).find("./tbody/tr/th/span")
    assert pill is not None
    assert pill.text == "mAiNtAiNeD"
    assert pill.attrib["class"].split() == ["pill"]
