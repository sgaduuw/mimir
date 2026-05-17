"""Inter-revision patch diff route.

`GET /<inbox_name>/series/<series_key>/diff?from=<vN>&to=<vM>&pos=<pos>`

`pos` is `cover` for the cover letter or a positive integer for an
in-series patch. Both `from` and `to` are version strings (`v1`,
`v2`, etc.) as they appear in `Article.patch_series_version`.

Resolves both revisions' cover letters by `(patch_series_key,
patch_series_version)`, walks each cover's thread children to find
in-series patches (`mimir.patch_revisions.resolve_series_patches`),
matches the requested position across revisions, fetches both
bodies via `store.read_message`, computes the unified diff, and
caches the rendered HTML for 24h (source emails are immutable
once committed to the public-inbox mirror, so the diff is stable).

Per-issue-210 design choices:
- **Full body** diff (commit message + patch hunks); the commit-
  message-only changes (added Fixes: trailer, rewrote explanation)
  matter to reviewers too.
- **Strict 404 on ambiguous match**: if two v2 candidates plausibly
  correspond to v1's `pos=N` and can't be disambiguated by file
  overlap, the route reports "couldn't pick" rather than guessing.
"""
from flask import abort, render_template, request
from sqlalchemy import select

from mimir import cache
from mimir.extensions import SessionLocal
from mimir.models import Article
from mimir.patch_revisions import (
    AMBIGUOUS_MATCH,
    RevisionDiff,
    compute_revision_diff,
    match_revision_position,
    resolve_series_patches,
)
from mimir.store import MessageNotFound, read_message
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404, _msg_url


# Long TTL: source emails are immutable in the public-inbox mirror
# (see CONTEXT.md "Append-only upstreams"), so a computed diff
# between two specific revisions is stable forever. 24h matches
# the archive-stats cache shape; cache.NAMESPACE_VERSION bump
# invalidates everything if the RevisionDiff shape ever changes.
REVISION_DIFF_CACHE_TTL_SEC = 86400


def _resolve_via_index(
    session, series_key: str, from_version: str, to_version: str, pos: int,
) -> tuple[Article | None, Article | None]:
    """Indexed lookup for the two articles at `(series_key, version,
    pos)` for both sides. Returns `(from_article, to_article)`;
    either side may be None when its row hasn't been backfilled yet,
    in which case the caller falls back to the heuristic resolver.

    Two-row SELECT (one per version) over the
    `ix_articles_patch_series_key` index, then position filter, fast.
    """
    rows = session.execute(
        select(Article).where(
            Article.patch_series_key == series_key,
            Article.patch_series_version.in_([from_version, to_version]),
            Article.patch_series_position == pos,
        )
    ).scalars().all()
    from_a = next(
        (a for a in rows if a.patch_series_version == from_version), None,
    )
    to_a = next(
        (a for a in rows if a.patch_series_version == to_version), None,
    )
    return from_a, to_a


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
    if pos_raw in ("cover", "0"):
        pos = 0
    else:
        try:
            pos = int(pos_raw)
        except ValueError:
            abort(404)
        if pos < 1:
            abort(404)

    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)

        # Indexed-lookup primary path (#212): with
        # `patch_series_position` populated on both covers (pos=0)
        # and in-series patches (pos=N), a single indexed query
        # returns the two articles directly, no thread walk + match
        # heuristic. The heuristic fallback below catches articles
        # whose backfill hasn't run yet (rolling deploy, partial
        # backfill), so the route stays correct during transition.
        # Cover-letter listing for the "Available: …" 404 hint is
        # still needed for unknown-revision URLs.
        covers = session.execute(
            select(Article).where(
                Article.patch_series_key == series_key,
                Article.patch_series_version.in_([from_version, to_version]),
                Article.patch_series_position == 0,
            )
        ).scalars().all()
        from_cover = next(
            (c for c in covers if c.patch_series_version == from_version),
            None,
        )
        to_cover = next(
            (c for c in covers if c.patch_series_version == to_version),
            None,
        )
        if from_cover is None or to_cover is None:
            # Show the list of available revisions in the 404 message
            # so the user can fix the URL without guessing.
            available = sorted({
                c.patch_series_version
                for c in covers
                if c.patch_series_version
            })
            if not available:
                # Whole series_key is unknown. Plain 404; nothing to
                # show.
                abort(404)
            abort(404, description=(
                f"Series revision(s) not found. Available: "
                f"{', '.join(available)}"
            ))

        # Cache lookup: the diff between two specific (cover, cover,
        # position) triples is content-addressed by their identities,
        # so the cache key is inbox-scoped per-series.
        cache_key = (
            f"revision_diff:{inbox.name}:{series_key}:"
            f"{from_version}:{to_version}:{pos}"
        )

        def _compute() -> RevisionDiff:
            v1_article, v2_article = _resolve_via_index(
                session, series_key, from_version, to_version, pos,
            )
            if v1_article is None or v2_article is None:
                # Indexed lookup didn't find one or both sides
                # (in-series patch awaiting backfill, or partial
                # ingest). Fall back to the heuristic resolver from
                # #210. Drop this fallback once prod backfill is
                # complete.
                v1_patches = resolve_series_patches(session, inbox, from_cover)
                v2_patches = resolve_series_patches(session, inbox, to_cover)
                match = match_revision_position(v1_patches, v2_patches, pos)
                if match is AMBIGUOUS_MATCH:
                    abort(404, description=(
                        f"Couldn't pick which v{to_version.lstrip('v')} patch "
                        f"corresponds to v{from_version.lstrip('v')}'s "
                        f"pos={pos}: multiple candidates have matching file "
                        "overlap."
                    ))
                if match is None:
                    abort(404, description=(
                        f"No counterpart in v{to_version.lstrip('v')} for "
                        f"v{from_version.lstrip('v')}'s pos={pos}; it may have "
                        "been dropped between revisions."
                    ))
                v1_in_series, v2_in_series = match  # type: ignore[misc]
                v1_article = v1_in_series.article
                v2_article = v2_in_series.article
            try:
                v1_parsed = read_message(session, inbox, v1_article.message_id)
                v2_parsed = read_message(session, inbox, v2_article.message_id)
            except MessageNotFound:
                # One of the blobs is gone from the mirror. The
                # canonical archive bytes are inaccessible; we can't
                # produce a faithful diff. 404 is the right shape.
                abort(404, description=(
                    "Patch body not available from the mirror; can't "
                    "render the diff."
                ))
            return compute_revision_diff(
                v1_parsed.body, v2_parsed.body,
                from_article_id=v1_article.id,
                to_article_id=v2_article.id,
                from_version=from_version,
                to_version=to_version,
                from_url=_msg_url(v1_article, inbox.name),
                to_url=_msg_url(v2_article, inbox.name),
                position=pos,
            )

        diff = cache.get_or_compute(
            session, cache_key, REVISION_DIFF_CACHE_TTL_SEC, _compute,
        )

    return render_template(
        "series_diff.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        series_key=series_key,
        diff=diff,
        position_label="cover letter" if pos == 0 else f"patch {pos}",
    )
