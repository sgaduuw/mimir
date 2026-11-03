"""One lowercasing for subsystem names (#987, #988).

MAINTAINERS prints section titles in capitals. The site shows them
lowercased, and used to do it with `|lower` at each template that printed
one. A new page had to remember it, so one filter owns it now. It changes
the display only: the URL keeps `subsystem_path`'s own spelling.
"""

import re
from pathlib import Path

from mimir.subsystems import subsystem_path

TEMPLATES = Path(__file__).resolve().parents[2] / "mimir" / "templates"
NAME = "KERNEL FOO (BAR)"

# Operands that are not subsystem names: status words, a series-diff
# position label and the first letter of a thread tag.
_OTHER_OPERANDS = {
    "pull_labels[state]",
    "status",
    "s.status",
    "subsystem.status",
    "next_position.label",
    "tag[:1]",
}
_LOWER_RE = re.compile(r"([\w.\[\]:]+)\s*\|\s*lower\b")


def _render(client, source: str, **context) -> str:
    env = client.application.jinja_env
    return env.from_string(source).render(**context)


def test_filter_lowercases_and_tolerates_none(client):
    assert _render(client, "{{ n|subsystem_label }}", n=NAME) == "kernel foo (bar)"
    assert _render(client, "{{ n|subsystem_label }}", n=None) == ""


def test_link_macro_shows_the_label_and_keeps_the_url(client):
    html = _render(
        client,
        '{% from "_subsystem_link.html" import subsystem_link %}'
        "{{ subsystem_link(n, 'alpha') }}",
        n=NAME,
    )
    assert ">kernel foo (bar)</a>" in html
    assert f'href="{subsystem_path("alpha", NAME)}"' in html


def test_no_template_lowercases_a_subsystem_name_itself():
    """The sweep, as a check: only the filter may lowercase a name."""
    offenders = []
    scanned = counted = 0
    for path in sorted(TEMPLATES.glob("*.html")):
        scanned += 1
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for operand in _LOWER_RE.findall(line):
                counted += 1
                if operand not in _OTHER_OPERANDS:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert scanned > 20
    # The allowed operands are really there, so the scan is looking at them.
    assert counted > 0
    assert offenders == []
