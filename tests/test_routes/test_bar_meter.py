"""The shared bar-meter macro (#988): width is an SVG attribute, because
the CSP forbids `style=`."""

import pytest

from tests._dom import parse


def _bar(client, *args, **kwargs):
    env = client.application.jinja_env
    call = ", ".join([*map(repr, args), *(f"{k}={v!r}" for k, v in kwargs.items())])
    source = (
        f'{{% from "_bar_meter.html" import bar_meter %}}{{{{ bar_meter({call}) }}}}'
    )
    html = env.from_string(source).render()
    return html, parse(html).find("svg")


@pytest.mark.parametrize(
    ("value", "maximum", "width"),
    [(3, 3, "100.0"), (2, 3, "66.7"), (0, 3, "0.0"), (5, 0, "0.0"), (0, 0, "0.0")],
)
def test_width_is_proportional_and_safe_at_the_edges(client, value, maximum, width):
    html, svg = _bar(client, value, maximum)
    assert svg.find("rect").attrs["width"] == width
    assert "style" not in html


def test_unlabelled_bar_is_decoration_and_labelled_bar_is_an_image(client):
    _, plain = _bar(client, 1, 2)
    assert plain.attrs["aria-hidden"] == "true"
    assert "role" not in plain.attrs
    _, labelled = _bar(client, 1, 2, label="half of the busiest")
    assert labelled.attrs["role"] == "img"
    assert labelled.attrs["aria-label"] == "half of the busiest"
    assert "aria-hidden" not in labelled.attrs
