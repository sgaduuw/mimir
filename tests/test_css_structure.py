"""mimir.css must stay balanced.

A merge of two branches that each append a block ending in a media query
can keep the one shared closing brace outside the conflict markers, and
leave a block open: every rule after it then sits inside that media query,
silently, with no error anywhere. Counting braces is enough to see it.
"""

import re
from pathlib import Path

import mimir

CSS = Path(mimir.__file__).parent / "static" / "css" / "mimir.css"


def test_braces_balance():
    text = CSS.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", text)
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"closing brace with nothing open, line {number}"
    assert depth == 0, f"{depth} block(s) left open at the end of the file"
