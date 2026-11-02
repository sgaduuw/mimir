"""Inter-revision patch diff route.

`GET /<inbox_name>/series/<series_key>/diff?from=<vN>&to=<vM>&pos=<pos>[&s=<head>]`

`pos` is `cover` for the cover letter or a positive integer for an
in-series patch. Both `from` and `to` are version strings (`v1`,
`v2`, etc.) as they appear in `Article.patch_series_version`. `s` is
the series head id (#816), absent for a key's first series.

Resolves exact series slots with request-local navigation metadata, retaining
subject/file matching where indexing is incomplete. Fetches only the selected
two bodies and caches their rendered diff for 24 hours. Navigation and canonical
nomination are refreshed independently of that body cache.

Per-issue-210 design choices:
- **Full body** diff (commit message + patch hunks); the commit-
  message-only changes (added Fixes: trailer, rewrote explanation)
  matter to reviewers too.
- **Strict 404 on ambiguous match**: if two v2 candidates plausibly
  correspond to v1's `pos=N` and can't be disambiguated by file
  overlap, the route reports "couldn't pick" rather than guessing.
"""

from flask import abort, render_template, request

from mimir import cache
from mimir.extensions import SessionLocal
from mimir.models import Article
from mimir.patch_revisions import (
    RevisionDiff,
    compute_revision_diff,
    patch_bracket,
)
from mimir.store import read_first_readable
from mimir.web._blueprint import bp_web
from mimir.web.filters import _clean_subject_filter
from mimir.web.series_diff_navigation import load_series_diff_navigation
from mimir.web.urls import (
    _canonical_url_for,
    _get_inbox_or_404,
    _site_base,
)

# Long TTL: source emails are immutable in the public-inbox mirror
# (see CONTEXT.md "Append-only upstreams"), so a computed diff
# between two specific revisions is stable forever. 24h matches
# the archive-stats cache shape. The render2 key versions the markup
# without changing the registered RevisionDiff wire shape.
REVISION_DIFF_CACHE_TTL_SEC = 86400

# SQLite binds signed 64-bit integers; a larger one raises OverflowError
# at bind time, a 500 where every other bad value gets a 404.
_SQLITE_INT_LIMIT = 2**63


def _positive_int_or_404(raw: str) -> int:
    """`raw` as an integer SQLite can bind and at least 1, else 404."""
    try:
        value = int(raw)
    except ValueError:
        abort(404)
    if not 1 <= value < _SQLITE_INT_LIMIT:
        abort(404)
    return value


def _message_url(article: Article) -> str:
    """The article's canonical message URL, built as the revision
    panel builds it, so the link and the page it points at agree."""
    links = [(al.inbox_id, al.inbox.name) for al in article.lists]
    return _canonical_url_for(article, links) or ""


@bp_web.route("/<inbox_name>/series/<series_key>/diff")
def series_diff(inbox_name: str, series_key: str):
    """Render the diff between two revisions of a single patch
    series position, or 404 with an actionable message if the
    request can't be resolved."""
    from_version = request.args.get("from", "").strip()
    to_version = request.args.get("to", "").strip()
    pos_raw = request.args.get("pos", "").strip().lower()
    if not from_version or not to_version or not pos_raw:
        abort(404)
    if from_version == to_version:
        # Self-diff is meaningless; surface as a 400-ish 404 rather
        # than rendering an empty diff page.
        abort(404)

    # Parse `pos`: `cover` (or `0`) for the cover letter, positive
    # integers for in-series patches. Reject anything else.
    pos = 0 if pos_raw in ("cover", "0") else _positive_int_or_404(pos_raw)

    # `s`, the series head (#816): absent is the key's first series
    # (NULL head). Present, it must be the positive id `_series_diff_url`
    # spells; `s=0` or `s=` would serve that first series under a
    # second spelling.
    head_raw = request.args.get("s")
    head_id = None if head_raw is None else _positive_int_or_404(head_raw)

    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)

        navigation = load_series_diff_navigation(
            session, inbox, series_key, head_id, from_version, to_version, pos
        )
        from_cover, to_cover = navigation.from_cover, navigation.to_cover

        # Cache key: what the diff is computed from, not the series
        # identity in the URL (#816), so re-chaining a key can never
        # serve one series' diff for another. That is the two slots'
        # winner ids, which move when a row gains a diff. A fallback also
        # carries the two covers it walked from and the matched children:
        # a later child can change a match while the cover ids stay put.
        # The versions and position stay
        # too, because the cached diff is labelled with them and
        # `backfill-patch-series --reprocess` can rewrite a stored
        # version in place under the same ids. The inbox stays because
        # the body read depends on it: each list archives its own copy
        # (some append a footer), and the heuristic walks thread
        # children in the URL's inbox.
        if navigation.indexed:
            sides = f"{navigation.from_article.id}:{navigation.to_article.id}"
        else:
            sides = (
                f"h{from_cover.id}:{to_cover.id}:"
                f"{navigation.from_article.id}:{navigation.to_article.id}"
            )
        cache_key = f"revision_diff:render2:{inbox.name}:{from_version}:{to_version}:{pos}:{sides}"

        def _compute() -> RevisionDiff:
            v1_candidates = [navigation.from_article]
            v2_candidates = [navigation.to_article]
            v1_read = read_first_readable(session, v1_candidates, inbox.id)
            v2_read = read_first_readable(session, v2_candidates, inbox.id)
            if v1_read is None or v2_read is None:
                # No candidate's blob is reachable from any inbox it
                # is filed in. The canonical archive bytes are
                # inaccessible; we can't produce a faithful diff. 404
                # is the right shape.
                abort(
                    404,
                    description=(
                        "Patch body not available from the mirror; can't "
                        "render the diff."
                    ),
                )
            (v1_article, v1_parsed), (v2_article, v2_parsed) = v1_read, v2_read
            return compute_revision_diff(
                v1_parsed.body,
                v2_parsed.body,
                from_article_id=v1_article.id,
                to_article_id=v2_article.id,
                from_version=from_version,
                to_version=to_version,
                from_url=_message_url(v1_article),
                to_url=_message_url(v2_article),
                position=pos,
            )

        diff = cache.get_or_compute(
            session,
            cache_key,
            REVISION_DIFF_CACHE_TTL_SEC,
            _compute,
        )
        canonical_url = _site_base() + navigation.canonical_path
        # The title and description name the series (#846): ~194k of these
        # pages shared one generic title.
        bracket = patch_bracket(to_cover.subject or "")
        series_subject = bracket[1] if bracket else to_cover.subject

    position_label = "cover letter" if pos == 0 else f"patch {pos}"
    # The document title also identifies this particular comparison.
    heading = (
        f"{_clean_subject_filter(series_subject)}: "
        f"{diff.from_version}→{diff.to_version} diff ({position_label})"
    )
    return render_template(
        "series_diff.html",
        canonical_url=canonical_url,
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        series_key=series_key,
        series_subject=series_subject,
        heading=heading,
        diff=diff,
        navigation=navigation,
        position_label=position_label,
    )
