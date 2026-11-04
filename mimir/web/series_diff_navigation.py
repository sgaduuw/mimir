"""Request-local metadata resolution and destinations for series comparisons."""

from collections import defaultdict
from dataclasses import dataclass

from flask import abort
from sqlalchemy import select

from mimir.models import Article
from mimir.patch_revisions import (
    AMBIGUOUS_MATCH,
    match_revision_position,
    resolve_series_patches,
)
from mimir.patch_series import adjacent_versions, same_series, slot_winner
from mimir.patches import article_has_files
from mimir.web.urls import (
    _advertised_urls_for,
    _canonical_inbox_names_for,
    _spell_series_diff_url,
)


@dataclass(frozen=True)
class Comparison:
    from_article: Article
    to_article: Article
    indexed: bool


@dataclass(frozen=True)
class PositionLink:
    position: int
    label: str
    url: str


@dataclass(frozen=True)
class PairLink:
    from_version: str
    to_version: str
    position: int
    url: str


@dataclass
class SeriesDiffNavigation:
    positions: list[PositionLink]
    pairs: list[PairLink]
    extent: int
    previous_url: str | None
    next_url: str | None
    from_article: Article
    to_article: Article
    from_cover: Article
    to_cover: Article
    indexed: bool
    canonical_path: str
    form_action: str
    head_id: int | None


class _SeriesComparisons:
    """One indexed batch, with each fallback posting read at most once."""

    def __init__(self, session, inbox, series_key, head_id):
        self.session = session
        self.inbox = inbox
        rows = session.execute(
            select(Article, article_has_files()).where(same_series(series_key, head_id))
        ).all()
        grouped = defaultdict(list)
        self.covers = {}
        self.revisions = []
        for article, has_diff in rows:
            version = article.patch_series_version
            position = article.patch_series_position or 0
            grouped[version, position].append((article, has_diff))
            if position == 0:
                self.covers.setdefault(version, article)
                self.revisions.append((article.date, article.id, version))
        self.slots = {key: slot_winner(slot) for key, slot in grouped.items()}
        self.fallback = {}

    def patches(self, version):
        if version not in self.fallback:
            self.fallback[version] = resolve_series_patches(
                self.session, self.inbox, self.covers[version]
            )
        return self.fallback[version]

    def resolve(self, older, newer, position):
        if older not in self.covers or newer not in self.covers:
            return None
        left = self.slots.get((older, position))
        right = self.slots.get((newer, position))
        if left is not None and right is not None:
            return Comparison(left, right, True)
        match = match_revision_position(
            self.patches(older), self.patches(newer), position
        )
        if match is None or match is AMBIGUOUS_MATCH:
            return match
        return Comparison(match[0].article, match[1].article, False)

    def observed_positions(self, older, newer):
        # Unindexed children must be discoverable even when the selected slot
        # is indexed. Reuse these same fallback lists for matching any gaps.
        positions = {pos for (version, pos) in self.slots if version in (older, newer)}
        for version in (older, newer):
            positions.update(p.position for p in self.patches(version))
        # Never range(max_position): mail can supply a very large sparse ordinal.
        return sorted(p for p in positions if 0 <= p < 2**63)


def incomplete_series_destination(
    session, inbox, series_key, head_id, from_version, to_version
):
    """Recover missing comparison inputs without guessing a comparison.

    There is no standalone series page. Send the reader to a cover's
    existing conversation, preserving canonical inbox/page and the shared
    message fallback when no materialised thread destination exists.
    """
    series = _SeriesComparisons(session, inbox, series_key, head_id)
    if not series.covers or any(
        version and version not in series.covers
        for version in (from_version, to_version)
    ):
        abort(404)
    version = to_version or from_version
    if not version:
        version = min(series.revisions, key=lambda r: (r[0] is None, r[0], r[1]))[2]
    cover = series.slots[version, 0]
    target = _advertised_urls_for(session, [cover]).get(cover.id)
    if target is None:
        abort(404)
    return target


def load_series_diff_navigation(
    session, inbox, series_key, head_id, from_version, to_version, position
):
    """Resolve the selected comparison and its links without reading mail bodies.

    Indexed slots win before the historical subject/file matcher. The route
    and every emitted destination therefore use the same acceptance rule.
    Canonical memberships are loaded in bulk, outside the rendered-diff cache.
    """
    series = _SeriesComparisons(session, inbox, series_key, head_id)
    if from_version not in series.covers or to_version not in series.covers:
        available = sorted(v for v in (from_version, to_version) if v in series.covers)
        abort(
            404,
            description=f"Series revision(s) not found. Available: {', '.join(available)}",
        )
    selected = series.resolve(from_version, to_version, position)
    if selected is AMBIGUOUS_MATCH:
        abort(
            404,
            description=(
                f"Couldn't pick which {to_version} patch corresponds to {from_version}'s "
                f"pos={position}: multiple candidates have matching file overlap."
            ),
        )
    if selected is None:
        abort(
            404,
            description=(
                f"No counterpart in {to_version} for {from_version}'s pos={position}; "
                "it may have been dropped between revisions."
            ),
        )
    observed = series.observed_positions(from_version, to_version)
    comparisons = {}
    for pos in observed:
        match = series.resolve(from_version, to_version, pos)
        if isinstance(match, Comparison):
            comparisons[pos] = match
    # The requested slot can be valid even when a heuristic counterpart was
    # numbered differently. Resolve each adjacent pair only at this position.
    pairs = []
    for older, newer in adjacent_versions(series.revisions):
        if (older, newer) == (from_version, to_version):
            continue
        pos = position
        match = series.resolve(older, newer, pos)
        if not isinstance(match, Comparison) and pos != 0:
            pos = 0
            match = series.resolve(older, newer, pos)
        if isinstance(match, Comparison):
            pairs.append((older, newer, pos, match))
    matches = [selected, *comparisons.values(), *(item[3] for item in pairs)]
    names = _canonical_inbox_names_for(
        session, list({match.to_article.id for match in matches if match.indexed})
    )

    def url(older, newer, pos, match):
        # A heuristic walks the requesting inbox only. Its matched article's
        # canonical inbox does not prove that the same fallback can run there.
        name = names.get(match.to_article.id) if match.indexed else None
        return _spell_series_diff_url(
            name or inbox.name, series_key, older, newer, pos, head_id=head_id
        )

    positions = [
        PositionLink(
            pos,
            "Cover letter" if pos == 0 else f"Patch {pos:02d}",
            url(from_version, to_version, pos, match),
        )
        for pos, match in comparisons.items()
    ]
    current_index = next(
        i for i, link in enumerate(positions) if link.position == position
    )
    return SeriesDiffNavigation(
        positions=positions,
        pairs=[
            PairLink(older, newer, pos, url(older, newer, pos, match))
            for older, newer, pos, match in pairs
        ],
        extent=max(observed, default=0),
        previous_url=positions[current_index - 1].url if current_index else None,
        next_url=positions[current_index + 1].url
        if current_index + 1 < len(positions)
        else None,
        from_article=selected.from_article,
        to_article=selected.to_article,
        from_cover=series.covers[from_version],
        to_cover=series.covers[to_version],
        indexed=selected.indexed,
        canonical_path=url(from_version, to_version, position, selected),
        form_action=_spell_series_diff_url(
            inbox.name, series_key, from_version, to_version, position, head_id=head_id
        ).partition("?")[0],
        head_id=head_id,
    )
