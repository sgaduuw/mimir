"""Re-parse persisted `parse_failures` rows after a parser fix.

`ingest_epoch` records every parse failure as a row in `parse_failures`;
this module re-walks them sequentially. On success: insert the Article
(or cross-post link) and delete the row. On failure: bump
attempts/last_attempt and refresh the error fields. Used by both the
`admin failures replay` CLI command and any operator-driven cleanup
after a parser change.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from mimir.extensions import SessionLocal
from mimir.ingest.epoch import _to_article
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    ParseFailure,
)
from mimir.parser import normalize_subject, parse_message
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import parse_cover_letter, series_key
from mimir.patches import extract_touched_paths
from mimir.trailers import extract_trailers

logger = logging.getLogger(__name__)


class ReplayResult(BaseModel):
    """Outcome of replaying persisted parse failures for one inbox."""

    attempted: int = 0
    recovered: int = 0  # parsed cleanly; row deleted, article inserted/linked.
    still_failed: int = 0  # parse still raises; row's last_attempt + attempts updated.
    skipped: int = 0  # blob couldn't be fetched (mirror missing, ref pruned).


def _replay_loop(
    conn_or_session,
    inbox_id: int,
    inbox_mirror_path: str,
    epoch_filter: str | None,
    limit: int | None,
    is_conn: bool,
) -> ReplayResult:
    """Core replay logic, shared by both the writer-thread and legacy paths.

    When `is_conn=True`, `conn_or_session` is a SQLAlchemy Core Connection and
    writes use Core DML statements with RETURNING for new Article ids.
    When `is_conn=False`, `conn_or_session` is an ORM Session and writes use
    the legacy ORM path.

    The dulwich blob fetches are identical in both paths: they read from the
    mirror on disk, no session involvement.
    """
    out = ReplayResult()

    q = select(ParseFailure).where(ParseFailure.inbox_id == inbox_id)
    if epoch_filter is not None:
        q = q.where(ParseFailure.epoch == epoch_filter)
    q = q.order_by(ParseFailure.epoch, ParseFailure.commit_sha)
    if limit is not None:
        q = q.limit(limit)

    if is_conn:
        rows = list(conn_or_session.execute(q).mappings())
    else:
        rows = list(conn_or_session.execute(q).scalars())

    # still-failed updates accumulate for a single executemany at the end
    # when on the conn path. On the session path we mutate ORM objects in
    # the loop and let the single commit flush them.
    still_failed_updates: list[dict] = []
    for row in rows:
        if is_conn:
            row_epoch = row["epoch"]
            row_commit_sha = row["commit_sha"]
        else:
            row_epoch = row.epoch
            row_commit_sha = row.commit_sha
        out.attempted += 1
        repo_path = Path(inbox_mirror_path) / row_epoch
        # Open the Repo with a context manager and close it as soon as the
        # row's blob + commit_time are extracted. Don't accumulate Repo
        # instances across the function lifetime: dulwich's pack-reading C
        # state assumes single-threaded access to a given .git, and two
        # concurrent Repos over the same path (replay vs warm handler, two
        # concurrent admin replays) can race on the shared pack index cache
        # on free-threaded 3.14t (audit #480).
        try:
            with Repo(str(repo_path)) as repo:
                try:
                    commit = repo[row_commit_sha.encode()]
                    tree = repo[commit.tree]
                    _mode, blob_sha = tree[b"m"]
                    raw = repo[blob_sha].data
                    commit_time = datetime.fromtimestamp(
                        commit.commit_time, timezone.utc
                    )
                except KeyError:
                    # Commit or `m` blob missing; mirror was pruned or rewound.
                    # Leave the row in place for the operator to inspect.
                    out.skipped += 1
                    continue
        except NotGitRepository, FileNotFoundError:
            out.skipped += 1
            continue

        try:
            parsed = parse_message(raw)
        except Exception as exc:
            if is_conn:
                # Defer to a batch update after the loop.
                still_failed_updates.append(
                    {
                        "_epoch": row_epoch,
                        "_commit_sha": row_commit_sha,
                        "_error_class": type(exc).__name__,
                        "_error_message": str(exc)[:1000],
                        "_now": datetime.now(timezone.utc),
                    }
                )
            else:
                row.last_attempt = datetime.now(timezone.utc)
                row.attempts += 1
                row.error_class = type(exc).__name__
                row.error_message = str(exc)[:1000]
            out.still_failed += 1
            continue

        if is_conn:
            existing_id = conn_or_session.execute(
                select(Article.id).where(Article.message_id == parsed.message_id)
            ).scalar_one_or_none()
            if existing_id is None:
                # Build the article values inline (mirrors _to_article
                # with session=None, i.e. no in-series parent lookup).
                thread_parent = parsed.in_reply_to or (
                    parsed.references[-1] if parsed.references else None
                )
                series_key_val: str | None = None
                series_version_val: str | None = None
                series_position_val: int | None = None
                cover = parse_cover_letter(parsed.subject)
                if cover is not None:
                    series_key_val = series_key(cover.title, parsed.author)
                    series_version_val = cover.version
                    series_position_val = 0
                else:
                    in_series = parse_in_series_patch_subject(parsed.subject)
                    if in_series is not None:
                        series_position_val = in_series.position
                        # No in-series parent lookup: replay is single-row,
                        # low-volume; the backfill command closes any gap in
                        # patch_series_key + patch_series_version later.

                result = conn_or_session.execute(
                    insert(Article)
                    .values(
                        message_id=parsed.message_id,
                        subject=parsed.subject,
                        author=parsed.author,
                        date=commit_time,
                        thread_parent=thread_parent,
                        subject_normalized=normalize_subject(parsed.subject),
                        canonical_inbox_id=None,
                        patch_series_key=series_key_val,
                        patch_series_version=series_version_val,
                        patch_series_position=series_position_val,
                    )
                    .returning(Article.id)
                )
                new_article_id = result.scalar_one()
                conn_or_session.execute(
                    insert(ArticleList).values(
                        article_id=new_article_id,
                        inbox_id=inbox_id,
                        epoch=row_epoch,
                        commit_sha=row_commit_sha,
                    )
                )
                # Insert any diff-touched path rows.
                touched_paths = sorted(extract_touched_paths(parsed.body))
                if touched_paths:
                    conn_or_session.execute(
                        insert(ArticleFile).values(
                            [
                                {"article_id": new_article_id, "path": p}
                                for p in touched_paths
                            ]
                        )
                    )
                # Insert any review-attestation trailer rows.
                trailer_tuples = list(extract_trailers(parsed.body))
                if trailer_tuples:
                    conn_or_session.execute(
                        insert(ArticleTrailer).values(
                            [
                                {
                                    "article_id": new_article_id,
                                    "role": role,
                                    "name": name,
                                    "address": address,
                                    "address_normalized": address.lower(),
                                }
                                for role, name, address in trailer_tuples
                            ]
                        )
                    )
            else:
                already_linked = conn_or_session.execute(
                    select(ArticleList.article_id).where(
                        ArticleList.article_id == existing_id,
                        ArticleList.inbox_id == inbox_id,
                    )
                ).scalar_one_or_none()
                if already_linked is None:
                    conn_or_session.execute(
                        insert(ArticleList).values(
                            article_id=existing_id,
                            inbox_id=inbox_id,
                            epoch=row_epoch,
                            commit_sha=row_commit_sha,
                        )
                    )
            conn_or_session.execute(
                delete(ParseFailure).where(
                    ParseFailure.inbox_id == inbox_id,
                    ParseFailure.epoch == row_epoch,
                    ParseFailure.commit_sha == row_commit_sha,
                )
            )
        else:
            existing_id = conn_or_session.execute(
                select(Article.id).where(Article.message_id == parsed.message_id)
            ).scalar_one_or_none()
            if existing_id is None:
                conn_or_session.add(
                    _to_article(
                        parsed,
                        inbox_id=inbox_id,
                        epoch=row_epoch,
                        commit_sha=row_commit_sha,
                        date=commit_time,
                        session=conn_or_session,
                    )
                )
            else:
                already_linked = conn_or_session.execute(
                    select(ArticleList.article_id).where(
                        ArticleList.article_id == existing_id,
                        ArticleList.inbox_id == inbox_id,
                    )
                ).scalar_one_or_none()
                if already_linked is None:
                    conn_or_session.add(
                        ArticleList(
                            article_id=existing_id,
                            inbox_id=inbox_id,
                            epoch=row_epoch,
                            commit_sha=row_commit_sha,
                        )
                    )
            conn_or_session.delete(row)
        out.recovered += 1

    # Replay inserts `article_lists` rows directly, so their
    # materialised thread root is unset. Left alone those rows stay
    # NULL permanently, even on a corpus where the backfill has already
    # run and the operator has no reason to run it again, and every
    # later reply to one of them stays NULL too. Resolving here keeps
    # the column self-maintaining rather than leaving a hole that only
    # a remembered follow-up command would close.
    #
    # Idempotent and cheap: the passes only touch rows that are still
    # NULL, so on a fully-backfilled corpus with nothing recovered this
    # is a couple of no-op statements.
    if out.recovered:
        # Flush first on the Session path. `SessionLocal` is
        # `autoflush=False`, and the passes below are raw `text()`
        # UPDATEs, so without this they would run against a database
        # that has not yet seen the ORM rows this loop just `add()`ed
        # and the whole call would be a silent no-op, re-creating
        # exactly the permanent-NULL hole it exists to close. Portfolio
        # MEMORY 2026-06-13: bulk raw SQL after ORM adds needs an
        # explicit flush under autoflush=False.
        flush = getattr(conn_or_session, "flush", None)
        if flush is not None:
            flush()
        _resolve_roots_after_replay(conn_or_session, inbox_id)

    # Flush still-failed updates: one UPDATE per row so each carries
    # its specific error_class / error_message.
    if is_conn:
        for upd in still_failed_updates:
            conn_or_session.execute(
                update(ParseFailure)
                .where(
                    ParseFailure.inbox_id == inbox_id,
                    ParseFailure.epoch == upd["_epoch"],
                    ParseFailure.commit_sha == upd["_commit_sha"],
                )
                .values(
                    last_attempt=upd["_now"],
                    attempts=ParseFailure.attempts + 1,
                    error_class=upd["_error_class"],
                    error_message=upd["_error_message"],
                )
            )
    else:
        conn_or_session.commit()

    return out


def _resolve_roots_after_replay(conn_or_session, inbox_id: int) -> None:
    """Run the thread-root passes for one inbox after a replay.

    `replay_failures` runs against either a writer Connection (broker
    path) or a Session (legacy path); `thread_roots`' passes take a
    Session, so wrap a Connection in one. The passes are the same ones
    the backfill uses and are idempotent, so this converges the rows
    replay just inserted without disturbing anything already correct.
    """
    from sqlalchemy.orm import Session as _Session

    from mimir.thread_roots import MAX_PASSES, break_cycle, propagate, seed_roots

    def _run(session) -> None:
        seed_roots(session, inbox_id)
        for _ in range(MAX_PASSES):
            if propagate(session, inbox_id):
                continue
            if break_cycle(session, inbox_id):
                continue
            break

    if isinstance(conn_or_session, _Session):
        _run(conn_or_session)
    else:
        with _Session(bind=conn_or_session) as session:
            _run(session)
            session.flush()


def replay_failures(
    inbox: Inbox,
    epoch_filter: str | None = None,
    limit: int | None = None,
) -> ReplayResult:
    """Re-parse persisted parse_failures rows for `inbox`.

    On success: insert the Article (or cross-post link) and delete the
    failure row. On failure: bump attempts/last_attempt and refresh the
    error fields. Sequential by design, replay is a low-volume admin
    op, not the hot ingest path.

    When an active broker WriterThread is present (broker context), the
    full replay loop runs inside a single WriteOp closure dispatched to
    the writer thread. When no broker context is active, the legacy
    SessionLocal() path is used.
    """
    try:
        from mimir.broker._context import get_active_writer

        writer = get_active_writer()
    except RuntimeError:
        writer = None

    if writer is not None:
        from mimir.broker.writes import WriteOp

        # Capture inbox attributes now; `inbox` may be a detached ORM
        # object and reading its attributes later (in a different thread)
        # is safe because we're only reading scalars that were already
        # loaded, not triggering lazy loads.
        inbox_id = inbox.id
        inbox_mirror_path = inbox.mirror_path

        def _fn(conn):
            return _replay_loop(
                conn,
                inbox_id=inbox_id,
                inbox_mirror_path=inbox_mirror_path,
                epoch_filter=epoch_filter,
                limit=limit,
                is_conn=True,
            )

        return writer.submit(WriteOp(label="failures_replay", fn=_fn)).result(
            timeout=300
        )

    # Legacy fallback (no broker context active).
    with SessionLocal() as session:
        attached = session.merge(inbox)
        return _replay_loop(
            session,
            inbox_id=attached.id,
            inbox_mirror_path=attached.mirror_path,
            epoch_filter=epoch_filter,
            limit=limit,
            is_conn=False,
        )
