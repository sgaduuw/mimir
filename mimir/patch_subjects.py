"""Shared patch identity predicates independent of web state aggregation."""

from mimir.patch_revisions import parse_in_series_patch_subject, patch_bracket
from mimir.patch_series import parse_cover_letter


def _is_patch_subject(subject: str | None) -> bool:
    """True when the subject is a patch: a cover letter, an in-series
    patch or a solo `[PATCH] foo`, list-tagged or not. `patch_bracket`
    decides, the same predicate both series parsers use (#711), so the
    state card cannot disagree with them. False for replies, prose,
    `[GIT PULL]`, `[ANNOUNCE]` and `[RFC]` without a numbered bracket."""
    return bool(subject) and patch_bracket(subject) is not None


def _is_reply_to_patch(article) -> bool:
    """True when a cover or in-series subject has neither a stored
    position nor a stored key. Ingest and the reprocess clear both on a
    reply that repeats such a subject (#820, #837); a real patch keeps
    its position and a cover stored before the #212 backfill keeps its
    key. A solo `[PATCH]` never has either, so it is not judged here
    (#838)."""
    return (
        article.patch_series_position is None
        and article.patch_series_key is None
        and (
            parse_cover_letter(article.subject) is not None
            or parse_in_series_patch_subject(article.subject) is not None
        )
    )
