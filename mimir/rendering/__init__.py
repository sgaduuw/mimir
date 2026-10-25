"""Body rendering split across four submodules:

- `blocks`: line walker + `_Block` dataclass + diff/quote/code
  segmentation regexes. `parse_blocks` is the entry.
- `diff`: per-hunk anchor rendering + per-language Pygments overlay
  on context / + / - lines (#211). `_render_diff_block` is the
  entry.
- `linkify`: URL + Message-ID linkification, plus the DCO trailer
  redaction variant. `linkify` is the per-block entry.
- `body`: orchestrator, `_render_block` dispatches each block to
  the right submodule and `render_body` is the top-level API.
  `render_body_text` is its plain-text counterpart for the Markdown
  pages and JSON-LD.

The public surface (re-exported below) preserves the historical
names callers reach for: `render_body`, `linkify`,
`URL_OR_MSGID_RE`, `parse_blocks`.
External callers (`mimir.web.filters`, `mimir.web.routes.message`,
`mimir.seo.json_ld`, `tests.test_rendering`) don't shift on the
split.
"""

from mimir.rendering.blocks import parse_blocks
from mimir.rendering.body import render_body, render_body_text
from mimir.rendering.linkify import URL_OR_MSGID_RE, linkify

__all__ = [
    "URL_OR_MSGID_RE",
    "linkify",
    "parse_blocks",
    "render_body",
    "render_body_text",
]
