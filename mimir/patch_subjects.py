"""Shared patch identity predicates independent of web state aggregation."""

from collections.abc import Iterator

from sqlalchemy import text

from mimir.patch_revisions import parse_in_series_patch_subject, patch_bracket
from mimir.patch_series import parse_cover_letter


def _is_patch_subject(subject: str | None) -> bool:
    """True when the subject is a patch: a cover letter, an in-series
    patch or a solo `[PATCH] foo`, list-tagged or not. `patch_bracket`
    decides, the same predicate both series parsers use (#711), so the
    state card cannot disagree with them. False for replies, prose,
    `[GIT PULL]`, `[ANNOUNCE]` and `[RFC]` without a numbered bracket."""
    return subject is not None and patch_bracket(subject) is not None


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


def is_canonical_patch(row) -> bool:
    """True when an article counts as a patch in recent postings: a patch
    subject that is not a reply whose series fields were cleared. Stored
    as `articles.canonical_patch` at every write of these three inputs,
    because SQL must not call back into Python (#1221). A change to
    `patch_bracket`, `parse_cover_letter` or
    `parse_in_series_patch_subject` therefore needs a
    `backfill-patch-series` run to refresh stored rows."""
    return _is_patch_subject(row.subject) and not _is_reply_to_patch(row)


def canonical_patch_batches(conn, batch_size: int = 50_000) -> Iterator[list[int]]:
    """Yield, per id range, the ids of articles whose `canonical_patch` is
    true. Reads only; the migration that adds the column writes them.

    Walks the primary key in order, so each batch is one bounded read and
    no cursor is open on `articles` while the caller updates rows already
    passed.
    Subjects without a `[` never parse as a patch, so SQLite drops them
    before Python sees them."""
    last = 0
    while True:
        rows = conn.execute(
            text(
                "SELECT id, subject, patch_series_position, patch_series_key"
                " FROM articles WHERE id > :last AND instr(subject, '[') > 0"
                " ORDER BY id LIMIT :n"
            ),
            {"last": last, "n": batch_size},
        ).all()
        if not rows:
            return
        last = rows[-1].id
        yield [row.id for row in rows if is_canonical_patch(row)]
