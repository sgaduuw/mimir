"""The legal text of /privacy as a list of blocks, for the golden check.

Headings, paragraphs and list items of the notice, in order, whitespace
normalised. Skipped: the title block (its date changes at each release),
the navigation, and the "In short" box, which are presentation and a
summary, not the notice itself.
"""

from tests._dom import Node

_KEEP = {"h2", "p", "li"}
_SKIP_TAGS = {"hgroup", "nav"}
_SKIP_CLASSES = {"privacy-short"}


def legal_blocks(root: Node) -> list[str]:
    privacy = root.find_all(cls="privacy")
    assert len(privacy) == 1, "one .privacy root"
    out: list[str] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag in _SKIP_TAGS or _SKIP_CLASSES & set(child.classes):
                continue
            if child.tag in _KEEP:
                out.append(child.text())
            else:
                walk(child)

    walk(privacy[0])
    return out
