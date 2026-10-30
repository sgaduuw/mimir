"""Phone re-flow of hard-wrapped prose (#989): which paragraphs qualify,
how they render, and that the scan is linear on hostile input."""

import time

import pytest

PROSE = [
    "This patch moves the allocation out of the hot path, so that the lock",
    "is held for a shorter time. On a 64-core machine the contention we saw",
    "in the benchmark drops by about a third.",
]


def test_wrapped_prose_qualifies():
    from mimir.rendering.reflow import reflowable

    assert reflowable(PROSE)
    # Two spaces after a full stop are prose, not a table.
    assert reflowable(
        [PROSE[0], "is held for a shorter time.  On a 64-core machine the", "end."]
    )


@pytest.mark.parametrize(
    ("why", "lines"),
    [
        ("one line", [PROSE[0]]),
        ("indented", [PROSE[0], "    " + PROSE[1]]),
        ("a short line in the middle", [PROSE[0], "Thanks,", PROSE[1]]),
        ("an over-long line", [PROSE[0] + " " + PROSE[1], PROSE[2]]),
        (
            "a list",
            ["- moves the allocation out of the hot path, so that the lock", PROSE[1]],
        ),
        (
            "a numbered list",
            ["1. moves the allocation out of the hot path, so that the lock", PROSE[1]],
        ),
        (
            "a trailer",
            [PROSE[0], "Signed-off-by: Ann Author <ann@example.com> and a long tail"],
        ),
        (
            "a table",
            [PROSE[0], "| column one | column two | column three | column four |"],
        ),
        (
            "ASCII art",
            [PROSE[0], "+------+------+------+------+------+------+------+------+"],
        ),
        ("box drawing", [PROSE[0], "┌── a box drawn around the text of this line ──┐"]),
        (
            "aligned columns",
            ["name          value         unit          description here", PROSE[1]],
        ),
        (
            "a URL on its own",
            [PROSE[0], "https://lore.kernel.org/all/20260101.example@x/"],
        ),
    ],
)
def test_conservative_exclusions(why, lines):
    from mimir.rendering.reflow import reflowable

    assert not reflowable(lines), why


def test_a_qualifying_paragraph_is_wrapped_and_others_are_not():
    """Only top-level prose gets the span; quote, diff, code and trailer
    text keep their lines exactly. The fold cuts first."""
    from mimir.rendering import render_body

    body = "\n".join(
        [
            *PROSE,
            "",
            "> " + PROSE[0],
            "> " + PROSE[1],
            "",
            "Signed-off-by: Ann Author <ann@example.com>",
            "---",
            "diff --git a/f.c b/f.c",
            "@@ -1 +1 @@",
            "-old",
            "+new",
        ]
    )
    html = str(render_body(body, address_redactor=lambda a: a))
    assert html.count('<span class="reflow">') == 1, html
    span = html.split('<span class="reflow">')[1].split("</span>")[0]
    assert span == "\n".join(PROSE)


def test_text_without_reflowable_prose_renders_as_before():
    """Splitting at blank lines must not change what linkify produces."""
    from mimir.rendering import render_body

    body = "Hi,\n\nSee https://example.com/x and <20260101.a@x>.\n\nThanks,\nAnn\n"
    html = str(render_body(body, address_redactor=lambda a: a))
    assert '<span class="reflow">' not in html
    assert html.startswith('<pre class="body-text-block">')
    assert "Thanks,\nAnn" in html


def test_reflowable_is_linear_on_hostile_input():
    """AGENTS.md test rule 7: a few hundred thousand lines of the shape it
    scans, loosely bounded, so quadratic code fails and slow CI passes."""
    from mimir.rendering.reflow import reflowable

    line = "word " * 14 + "end"  # 73 characters, qualifying shape
    hostile = [line] * 300_000 + ["x" * 200]  # fails on the very last line
    start = time.perf_counter()
    assert not reflowable(hostile)
    assert time.perf_counter() - start < 5


def test_both_pages_mark_the_prose_and_only_phones_join_it(client, tmp_path):
    """One render for both widths: the message page and `/t` carry the span,
    and the stylesheet joins its lines below 768 px only."""
    import re
    from pathlib import Path

    import mimir
    from tests.test_routes._helpers import _ingest_one_article

    _, url = _ingest_one_article(
        tmp_path, "alpha", "reflow@x", body=("\n".join(PROSE) + "\n").encode()
    )
    for page in (url, url + "/t"):
        assert '<span class="reflow">' in client.get(page).get_data(as_text=True), page
    css = (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    assert re.search(
        r"@media \(width < 768px\)\s*\{\s*pre\.body-text-block \.reflow\s*\{\s*"
        r"white-space:\s*normal;",
        css,
    )


@pytest.mark.parametrize(
    ("why", "lines"),
    [
        (
            "a list of commits",
            [
                "1a2b3c4d5e6f mm/page_alloc: fix the watermark check on boost",
                "7a8b9c0d1e2f mm/vmscan: avoid reclaim when the zone is balanced",
                "a1b2c3d4e5f6 mm: fix it",
            ],
        ),
        (
            "a list of paths",
            [
                "drivers/gpu/drm/i915/display/intel_display_power_well.c",
                "drivers/gpu/drm/i915/display/intel_display_power_map.c",
            ],
        ),
        (
            "base64",
            [
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5",
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5",
                "QUJD",
            ],
        ),
    ],
)
def test_one_item_per_line_text_is_not_prose(why, lines):
    """Conservative by design (reflow.py docstring): text laid out one item
    per line keeps its breaks. These are common in kernel mail (cover
    letters, pull requests, attachments) and are not hard-wrapped prose."""
    from mimir.rendering.reflow import reflowable

    assert not reflowable(lines), why
