"""The `bar_meter` macro (#988): a proportional bar as inline SVG, since
the CSP forbids the styled-span form."""

import re

import pytest
from flask import render_template_string

_CALL = '{% from "_bar.html" import bar_meter %}{{ bar_meter(value, maximum, label) }}'


def _render(value, maximum, label="a label"):
    from mimir import create_app

    with create_app().test_request_context():
        return render_template_string(_CALL, value=value, maximum=maximum, label=label)


def _width(svg: str) -> float:
    (width,) = re.findall(r'<rect[^>]*\bwidth="([\d.]+)"', svg)
    return float(width)


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [
        (0, 100, 0.0),  # zero: an empty bar, not a missing one
        (100, 100, 100.0),  # maximum: fills the track
        (25, 100, 25.0),
        (1, 3, 33.3),  # rounded, not a long float
        (0, 0, 0.0),  # all-zero data must not divide by zero
    ],
)
def test_bar_width_is_proportional(value, maximum, expected):
    assert _width(_render(value, maximum)) == expected


def test_bar_is_an_svg_with_a_text_equivalent():
    svg = _render(5, 10, label="5 of 10")
    assert 'viewBox="0 0 100 6"' in svg
    assert 'role="img"' in svg
    assert 'aria-label="5 of 10"' in svg
    assert 'fill="currentColor"' in svg
    assert "style=" not in svg
