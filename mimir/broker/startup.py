"""Prepare the database and launch background verification at broker startup.

The server owns process and transport lifecycle. This module owns the ordered
pre-serve database work and launches read-only verification without joining it.
"""

import logging
import threading
import time
from pathlib import Path

from mimir.config import settings

# Preserve the logger name used by operator filters before this extraction.
logger = logging.getLogger("mimir.broker.server")


def prepare_database(socket_path: Path) -> None:
    """Complete startup writes, then launch verification outside readiness."""
    schema_changed = _migrate_if_needed(socket_path)
    _bootstrap_inboxes_if_needed(socket_path)
    _post_migrate_analyze_if_needed(socket_path, schema_changed)
    _backfill_thread_roots_if_needed(socket_path)
    # Run on every successful start, including when the backfill sentinel skips.
    # The verifier starts a daemon thread; never join it before accepting RPCs.
    _verify_thread_roots_async()


def _schema_revision() -> str | None:
    """The applied alembic revision, or None if there is no schema yet.

    Reads through `mimir.extensions.engine`, which is the SAME engine
    `alembic/env.py` runs migrations on. That is the point, not an
    incidental convenience: an earlier version parsed
    `settings.database_url` and opened its own sqlite3 connection, so
    "which database has the revision" and "which database gets migrated"
    were two independent answers that could disagree. When they do, the
    before/after comparison reads unchanged and the caller concludes no
    migration ran, silently disabling the post-migrate ANALYZE. Sharing
    the engine makes them agree by construction.

    Returns None when the schema does not exist yet (fresh volume, no
    `alembic_version` table), which is a real answer, not an error: the
    caller compares it against the revision after the upgrade.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from mimir.extensions import engine

    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except SQLAlchemyError:
        return None
    return row[0] if row else None


def _migrate_if_needed(socket_path: Path) -> bool:
    """Run `alembic upgrade head` on broker startup so the broker
    can serve against a schema known to be current.

    Returns whether the applied revision actually MOVED, which
    `_post_migrate_analyze_if_needed` needs: a migration that rebuilds a
    table destroys that table's `sqlite_stat1` rows, and the planner then
    runs blind until something re-samples them.

    `alembic upgrade head` is idempotent and cheap when no
    migrations are pending (one SELECT against `alembic_version`),
    so we always run it. Pre-2.4.1 used `.migrated` as a "ran once,
    skip forever" gate, which silently swallowed every new alembic
    revision on long-lived deploys; 2.4.0 introduced
    `mainline_state.last_walked_at` and the broker on an upgraded
    deploy skipped it because the sentinel was present from 2.3.0,
    so `update_mainline()` raised `OperationalError: no such
    column`. The sentinel survives as a first-vs-subsequent-run
    marker for operator log-reading, but its presence no longer
    short-circuits the upgrade call.

    2.0.0 moved schema-migration ownership onto the broker because
    it's a write operation and broker is the sole writer process
    post-cleanup. The pre-2.0.0 shape ran `alembic upgrade head` on
    the tasks container as a startup-only direct write; consolidating
    here matches the single-writer invariant without exception."""
    sentinel = socket_path.parent / ".migrated"
    first_run = not sentinel.exists()
    revision_before = _schema_revision()
    logger.info(
        "broker: running alembic upgrade head (%s)",
        "first run" if first_run else "idempotent re-check",
    )
    t0 = time.monotonic()
    try:
        from alembic.config import Config

        from alembic import command

        # Construct Config programmatically rather than passing
        # "alembic.ini": that ini carries [loggers] / [handlers] /
        # [formatters] sections, and Config reading them invokes
        # `logging.config.fileConfig` which clears every handler off
        # the root logger, including pytest's caplog and any
        # operator-set handlers. Loading the migration's required
        # options directly side-steps the reconfiguration. The
        # migrations live in `alembic/` per the project layout.
        cfg = Config()
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
    except Exception:
        # Refuse to start: a migration failure is load-bearing for
        # everything downstream. The healthcheck stays red so web /
        # tasks containers wait for an operator fix.
        logger.exception("broker: alembic upgrade failed, refusing to start")
        raise
    elapsed = time.monotonic() - t0
    sentinel.touch()
    revision_after = _schema_revision()
    schema_changed = revision_before != revision_after
    logger.info(
        "broker: alembic upgrade head complete in %.1fs; sentinel %s touched "
        "(revision %s -> %s)",
        elapsed,
        sentinel,
        revision_before,
        revision_after,
    )
    return schema_changed


def _bootstrap_inboxes_if_needed(socket_path: Path) -> None:
    """Reconcile `Settings.inboxes` (env config) into the `inboxes`
    table on broker startup. Idempotent via `ON CONFLICT (name) DO
    NOTHING`; admin edits to existing rows are never clobbered. That
    answers "are edits to a row preserved" (yes), not "does the table
    still match admin intent": an `admin inbox update --new-name`
    leaves `Settings.inboxes` naming the old name, so a later
    bootstrap hits no conflict and INSERTS a phantom row under it.
    Consistent with the insert-only reconcile, but not covered by the
    sentence above.

    Sentinel-gated. The post-2.0.0 broker container owns this work
    because it's a write; pre-2.0.0 the tasks container's
    `mimir bootstrap-inboxes` did it as a startup-only direct write."""
    sentinel = socket_path.parent / ".bootstrapped"
    if sentinel.exists():
        logger.debug(
            "broker: inbox-bootstrap sentinel %s present, skipping",
            sentinel,
        )
        return
    logger.info("broker: running bootstrap_inboxes")
    t0 = time.monotonic()
    try:
        from mimir.inboxes import bootstrap_inboxes

        result = bootstrap_inboxes()
    except Exception:
        # Same posture as `_migrate_if_needed`: bootstrap is
        # load-bearing for the web tier's first request, refuse to
        # advertise readiness without it.
        logger.exception("broker: bootstrap_inboxes failed, refusing to start")
        raise
    elapsed = time.monotonic() - t0
    sentinel.touch()
    logger.info(
        "broker: bootstrap_inboxes reconciled %d inbox(es) in %.1fs; sentinel %s touched",
        len(result),
        elapsed,
        sentinel,
    )


def _post_migrate_analyze_if_needed(
    socket_path: Path, schema_changed: bool = False
) -> None:
    """Run `ANALYZE` after a schema migration so the planner has stats
    for any newly-created index, and for any table a migration rebuilt.

    Runs when EITHER the first-run sentinel is absent OR the applied
    alembic revision just moved. The second condition is the important
    one and used to be missing: the sentinel is once-EVER, not
    once-per-migration, so on any long-lived deploy this step was
    permanently skipped while the docstring claimed it ran "once after a
    fresh schema migration".

    That is not cosmetic, because SQLite migrations rebuild tables. A
    `batch_alter_table` that adds a foreign key cannot use native `ALTER
    TABLE`, so alembic does move-and-copy, and the `DROP TABLE` in the
    middle deletes every `sqlite_stat1` row for that table. Production
    carries real stats for `article_lists` (`ix_article_lists_inbox_id`
    at `28765436 141702`, read 2026-07-30) and would have lost them on
    the next deploy with nothing to put them back, leaving the planner
    blind on a 28.8M-row table. CONTEXT.md records what bad stats on
    this exact table cost last time: a 15-message `get_thread` taking
    400 seconds, across four hotfix releases.

    The sentinel stays, as a first-vs-subsequent-run marker for operator
    log reading and so a manual `rm` still forces a pass.
    """
    sentinel = socket_path.parent / ".broker_initial_analyze"
    if sentinel.exists() and not schema_changed:
        logger.debug(
            "broker: post-migrate ANALYZE sentinel %s present and schema "
            "unchanged, skipping",
            sentinel,
        )
        return
    logger.info(
        "broker: running post-migrate ANALYZE (reason=%s)",
        "schema revision moved" if schema_changed else "first run",
    )
    t0 = time.monotonic()
    try:
        from mimir.maintenance import run_analyze

        result = run_analyze()
    except Exception:
        # Don't refuse to start the broker if ANALYZE fails; log
        # loudly and continue. The next scheduled ANALYZE
        # (weekly, from the scheduler) retries.
        logger.exception(
            "broker: post-migrate ANALYZE failed, continuing without sentinel"
        )
        return
    elapsed = time.monotonic() - t0
    sentinel.touch()
    logger.info(
        "broker: post-migrate ANALYZE complete in %.1fs "
        "(broker-reported elapsed_ms=%d); sentinel %s touched",
        elapsed,
        result.elapsed_ms,
        sentinel,
    )


def _backfill_thread_roots_if_needed(socket_path: Path) -> None:
    """Populate `article_lists.thread_root_id` before the broker serves.

    W8's column is read by `find_thread_root` and by the sitemap's root
    and singleton tests. Readers fall back to the recursive walk while
    a row is NULL, so a partially-filled corpus is correct, but the
    sitemap simply omits threads it has not reached yet: deploy without
    filling and the archive's own sitemap under-reports until somebody
    remembers to run the command.

    "Somebody remembers" is not a mechanism, so this runs it. Same
    shape as the post-migrate ANALYZE above: sentinel-gated, in
    `build_server`, so it completes before the broker ACCEPTS RPCs and
    therefore before the web tier's healthcheck dependency is
    satisfied. (Not "before the socket opens": `UnixStreamServer`
    binds and listens in its constructor, so `connect(2)` succeeds
    throughout this window and `broker-ping` hangs into its timeout
    rather than failing fast. That distinction is why this step counts
    against the healthcheck's `start_period` rather than being
    invisible to it.)

    Cost is one-time and proportional to corpus size; see
    `mimir.thread_roots` for both the measured per-row cost and the
    last real measurement of how many rows production actually has.
    Treat any extrapolation as a FLOOR: pass count grows with thread
    depth, and the corpus only grows. `compose.yaml`'s `start_period`
    has to cover this plus the migration, bootstrap and both ANALYZEs,
    and has already been wrong twice from an underestimated corpus.

    Committed per PASS, not per inbox and not once at the end, using
    the same `drive_passes` seam the broker's RPC handler uses. Per-
    inbox looks resumable and is not, for the only inbox where it
    matters: lkml alone is ~87%% of the total run, so a SIGKILL lands
    inside it with overwhelming probability and would discard all of
    it. Committing per pass makes "interrupted runs resume" true at
    the granularity the docstring on `drive_passes` claims, because
    every pass only touches NULL rows.

    Verification does NOT run here, and is no longer called from this
    function at all: being its last statement meant it only ever ran on
    the deploy that first filled the column, because this function
    returns early on the sentinel. `prepare_database` calls it separately on every startup.

    It stays off the startup path regardless. It is explicitly not
    allowed to gate startup, and it costs ~218 s on a prod-shape corpus
    (re-measured 2026-07-30, and a floor), which would be pure added
    latency in front of the healthcheck. See `_verify_thread_roots_async`
    for the breakdown.
    """
    sentinel = socket_path.parent / ".thread_roots_backfilled"
    if sentinel.exists() and not _has_unrooted_rows():
        logger.debug("broker: thread-roots sentinel %s present, skipping", sentinel)
        return
    if sentinel.exists():
        # Sentinel set but rows are NULL again. Reachable by rolling
        # back to a pre-column image (whose ORM omits the column on
        # INSERT) and then rolling forward: the sentinel would
        # short-circuit forever, and nothing else would notice, since
        # `verify_thread_roots` samples only non-NULL rows and the
        # sitemap's root test IS the column. Re-running is cheap when
        # there is nothing to do.
        #
        # This deliberately re-runs on ANY unrooted row rather than only
        # on more than some remembered count. A remembered count was
        # tried and reverted: it compares cardinality where the question
        # is identity, so two rows going NULL after two others were
        # repaired reads as "not above the floor" and those rows are
        # then never repaired, silently and permanently. The failure
        # modes are not symmetric. Re-running when it was unnecessary
        # costs a log line and a pass over ~200 inboxes that moves
        # nothing; not re-running when it was necessary leaves a thread
        # materialised in two pieces with no repair path, because every
        # repair path keys on NULL. Prefer the noisy, self-healing side.
        logger.warning(
            "broker: thread-roots sentinel present but unrooted rows exist; "
            "re-running the backfill"
        )

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.thread_roots import drive_passes

    logger.info("broker: backfilling thread roots (one-time)")
    t0 = time.monotonic()
    totals = {"seeded": 0, "propagated": 0, "cycles_broken": 0, "exhausted": 0}
    failed = 0
    try:
        with SessionLocal() as session:
            inboxes = list(session.execute(select(Inbox)).scalars())
            for inbox in inboxes:
                try:

                    def _run_pass(fn, _ix=inbox):
                        moved = fn(session, _ix.id)
                        # Commit each pass, so an interrupted run keeps
                        # everything up to the last completed pass even
                        # inside the one big inbox.
                        session.commit()
                        return moved

                    counts = drive_passes(_run_pass)
                    if counts["exhausted"]:
                        logger.warning(
                            "broker: thread-roots backfill hit MAX_PASSES on "
                            "inbox %s; rows remain unrooted",
                            inbox.name,
                        )
                except Exception:
                    session.rollback()
                    failed += 1
                    logger.exception(
                        "broker: thread-roots backfill failed for inbox %s, "
                        "continuing with the rest",
                        inbox.name,
                    )
                    continue
                for key in totals:
                    totals[key] += counts.get(key, 0)
    except Exception:
        # Don't refuse to start: readers fall back to the recursive
        # walk while rows are NULL, so an unfilled column is slow and
        # under-reported, not wrong. No sentinel, so the next restart
        # retries.
        logger.exception(
            "broker: thread-roots backfill failed, continuing without sentinel"
        )
        # Still re-sample. A session-level failure part-way through is a
        # PARTIAL fill, which is precisely the state where the column's
        # distribution has moved and the planner's stats are stale, so
        # returning here would reintroduce the skip this function was
        # just fixed to avoid, one level further out.
        _run_post_backfill_analyze()
        return

    elapsed = time.monotonic() - t0

    # Re-sample BEFORE deciding anything, and unconditionally. The fill
    # rewrote rows of a column ANALYZE may have sampled while it was 100%
    # NULL, so the planner otherwise keeps treating it as single-valued.
    # This used to sit under a success branch, which meant one inbox
    # hitting the pass budget or erroring skipped the re-sample for the
    # whole corpus while the broker went on to serve: a partial fill is
    # exactly the case where the column's distribution changed AND the
    # stats are stale, so it is the last case that should skip this.
    #
    # Running it before the count below is also what keeps that count
    # cheap: with fresh stats the planner takes a skip-scan over
    # `ix_article_lists_thread_root` instead of scanning the covering
    # index (measured at 28.8M rows: 0.00 s vs 0.47 s; still 28.8M on
    # 2026-08-04).
    _run_post_backfill_analyze()

    # The counters are PER RUN, not cumulative, because the backfill only
    # touches NULL rows and is therefore resumable. On a resumed run they
    # describe the remainder and nothing else, which reads exactly like a
    # partial fill: 3.7.0's deploy logged seeded=1468645 propagated=4305393
    # against a 28.8M-row table and cost a diagnostic round to establish
    # that an earlier interrupted start had already done the other ~23M.
    #
    # `remaining` is the number that actually answers "did this finish",
    # so it GATES the outcome rather than merely decorating the log line.
    # Deriving `incomplete` from the pass budget and error count alone
    # left a third way to finish dirty: `break_cycle` documents a state
    # where it deliberately declines to act ("a stall with no cycle in it
    # is left alone: those rows stay NULL"), and `drive_passes` then
    # exits its loop normally, so exhausted == failed == 0 while rows are
    # still unrooted. That run wrote the sentinel and logged "complete",
    # which is precisely the false positive this change exists to remove,
    # one level deeper. Found by the pre-PR whole-diff review.
    #
    # `None` (the count itself failed) deliberately does NOT mark the run
    # incomplete: the run's own signals say it succeeded, so a transient
    # count failure should not date-stamp it as dirty. Cheap either way,
    # and the earlier version of this comment overstated the stake: a
    # re-run against an already-filled corpus is an index seek per
    # inbox, seconds, not the multi-minute first fill. It renders as
    # "unknown" on the line, so it is visible rather than silently
    # treated as a reassuring zero.
    remaining = _count_unrooted_rows()
    incomplete = bool(totals["exhausted"] or failed or remaining)
    remaining_display = "unknown" if remaining is None else remaining

    if not incomplete:
        # NOT touched on an incomplete run. Note what that does and does
        # not buy, because an earlier version of this comment claimed
        # the sentinel is "the ONLY thing preventing a retry" and that
        # is false: the gate above is `sentinel.exists() and not
        # _has_unrooted_rows()`, so a run is retried whenever rows are
        # unrooted, sentinel or no sentinel. Nothing here ever unlinks
        # it either, so on any deploy after the first success the file
        # is already present and withholding it withholds nothing.
        #
        # It still earns the conditional: the sentinel's mtime is the
        # operator-visible record of the last CLEAN fill, and refreshing
        # it on a dirty run would date-stamp a success that did not
        # happen.
        sentinel.touch()

    # ONE line per outcome, and the failure line deliberately never says
    # "complete". This used to log the failure and then fall through to
    # an unconditional "backfill complete in %.1fs", so a failed run
    # emitted both and grepping for the ordinary success line returned a
    # false positive on precisely the run that needed attention. The
    # word "incomplete" is retained because README.md and
    # deploy/README.md both name it as the string to grep for.
    if incomplete:
        logger.error(
            "broker: thread-roots backfill incomplete after %.1fs "
            "(pass budget hit on %d inbox(es), errored on %d; "
            "seeded=%d propagated=%d cycles=%d remaining=%s); the next "
            "start retries automatically while any row is unrooted, or "
            "re-run `mimir backfill-thread-roots`",
            elapsed,
            totals["exhausted"],
            failed,
            totals["seeded"],
            totals["propagated"],
            totals["cycles_broken"],
            remaining_display,
        )
        return

    logger.info(
        "broker: thread-roots backfill complete in %.1fs "
        "(seeded=%d propagated=%d cycles=%d remaining=%s)",
        elapsed,
        totals["seeded"],
        totals["propagated"],
        totals["cycles_broken"],
        remaining_display,
    )


def _run_post_backfill_analyze() -> None:
    """Re-sample planner stats after the column has been written.

    One function because all three exits from the backfill need it: full
    success, partial success (an inbox hit the pass budget or errored),
    and a session-level failure. Only the first used to get it, which
    inverted the priority: the more incomplete the fill, the more likely
    the stats are stale and the less likely they were refreshed.

    Never raises. Stale stats make queries slow, not wrong, and refusing
    to start over them would turn a performance problem into an outage.
    """
    try:
        from mimir.maintenance import run_analyze

        run_analyze()
    except Exception:
        logger.exception(
            "broker: post-backfill ANALYZE failed; planner stats for "
            "thread_root_id may under-value the index until the next pass"
        )


def _has_unrooted_rows() -> bool:
    """Is any `article_lists` row still NULL? One indexed EXISTS."""
    from sqlalchemy import exists, select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    try:
        with SessionLocal() as session:
            return bool(
                session.scalar(
                    select(exists().where(ArticleList.thread_root_id.is_(None)))
                )
            )
    except Exception:
        # Treat "cannot tell" as "nothing to do": the caller's next
        # step is a backfill, and failing that query means the DB is
        # in no state to run one.
        logger.exception("broker: could not check for unrooted rows")
        return False


def _count_unrooted_rows() -> int | None:
    """How many `article_lists` rows are still NULL? One indexed COUNT.

    Reported on the backfill's own completion line so that line asserts
    the outcome instead of leaving it to be inferred from per-run
    counters (see the caller for why that inference misled).

    `None` means the count could not be taken, and the caller renders it
    as "unknown" rather than as a number. Reporting a failed count as 0
    would turn a diagnostic that could not run into a clean bill of
    health, which is the failure this whole change exists to remove:
    the sibling `_has_unrooted_rows` can safely answer "no" when it
    cannot tell, because its caller's fallback is to run the backfill
    anyway, but nothing downstream re-checks a log line.
    """
    from sqlalchemy import func, select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    try:
        with SessionLocal() as session:
            return int(
                session.scalar(
                    select(func.count()).where(ArticleList.thread_root_id.is_(None))
                )
                or 0
            )
    except Exception:
        logger.exception("broker: could not count unrooted rows")
        return None


def _verify_thread_roots_async() -> threading.Thread:
    """Recompute a sample per inbox and report disagreements, off the
    startup path.

    Verification is explicitly not allowed to gate startup: a mismatch
    is serious, but wedging the broker on it turns a reporting problem
    into an outage, and the failure it detects is not made worse by the
    site being up. It used to run inline anyway, which meant it could
    not fail the healthcheck but could still DELAY it past
    `start_period`.

    Cost, re-measured 2026-07-30 on a prod-shape corpus after the
    coherence check was added, because the previous figure (~23 s) is now
    wrong by an order of magnitude:

        find_incoherent_roots   134.8 s   (complete, one scan per inbox)
        verify_thread_roots      83.5 s   (sampled, 200 rows per inbox)
        -----------------------------
        total                   218   s

    Treat that as a FLOOR: the bench corpus's `thread_parent` resolves
    for none of its rows, so it never pays the parent-row seek that a
    real corpus does. The old docstring's claim that per-inbox cost is
    "near-constant" no longer holds either, since the coherence check
    scales with each inbox's row count rather than with a fixed sample.

    Two changes make this affordable rather than alarming. It runs on a
    daemon thread, so none of it is in front of the healthcheck. And it
    is read-only, so it cannot block the single writer; pysqlite's legacy
    isolation means a SELECT does not even open a transaction, so it
    holds nothing against WAL.

    It now runs on EVERY start rather than once ever, which is the point
    (see `prepare_database`), and restarts are deploy-frequency events, so
    ~3.5 minutes of background reads after a deploy is the whole bill.
    """

    def _run() -> None:
        from sqlalchemy import select

        from mimir.extensions import SessionLocal
        from mimir.models import Inbox
        from mimir.thread_roots import find_incoherent_roots, verify_thread_roots

        # Counted so the completion line can state the result rather
        # than leave it to be inferred from silence. Before this, the
        # only output was on failure, so "verified clean", "still
        # running" and "the thread died" were the same observation:
        # nothing. Answering "is the column still correct?" meant
        # running the invariant by hand against production, which it
        # did, twice, on the 3.7.0 deploy.
        t0 = time.monotonic()
        checked = 0
        split_inboxes = 0
        mismatch_inboxes = 0

        logger.info(
            "broker: verifying thread roots in the background "
            "(read-only, off the healthcheck path; expect minutes on a "
            "large corpus, and only the completion line proves it finished)"
        )
        try:
            with SessionLocal() as session:
                for inbox in session.execute(select(Inbox)).scalars():
                    # Complete structural check first: it is the one that
                    # actually catches a split thread, because it looks at
                    # every row instead of a 200-row sample.
                    split = find_incoherent_roots(session, inbox)
                    if split:
                        split_inboxes += 1
                        # "at least", because the query is LIMITed: the
                        # count is a sample size, not a total, and
                        # reporting it as a total would understate a
                        # corpus-wide problem as a handful of rows.
                        logger.error(
                            "broker: thread-roots verification found at least "
                            "%d row(s) in inbox %s whose parent carries a "
                            "different root, e.g. %s; the thread is "
                            "materialised in two pieces",
                            len(split),
                            inbox.name,
                            split[0],
                        )
                    bad = verify_thread_roots(session, inbox)
                    if bad:
                        mismatch_inboxes += 1
                        logger.error(
                            "broker: thread-roots verification found %d "
                            "mismatch(es) in inbox %s, e.g. %s; the "
                            "materialised column disagrees with find_thread_root",
                            len(bad),
                            inbox.name,
                            bad[0],
                        )
                    # Incremented only after both checks have actually
                    # run for this inbox, so the failure path's "after %d
                    # inbox(es)" counts inboxes VERIFIED, not inboxes
                    # reached. Counting at the top of the loop reported
                    # "after 1 inbox(es)" when the very first one raised
                    # and none had been verified at all.
                    checked += 1
        except Exception:
            # No completion line on this path, deliberately: its absence
            # is the signal that the run did not finish, and emitting one
            # here would report a partial sweep as a clean one.
            logger.exception(
                "broker: thread-roots verification failed to run after %d inbox(es)",
                checked,
            )
            return

        # The whole point of this change. A stated zero is evidence; an
        # absent line is not, and this line is absent precisely when it
        # should be: the thread is a daemon, so a restart inside the run
        # kills it mid-sweep and nothing is logged, which is exactly what
        # "did not finish" should look like.
        logger.info(
            "broker: thread-roots verification complete in %.1fs "
            "(inboxes=%d split_inboxes=%d mismatched_inboxes=%d)",
            time.monotonic() - t0,
            checked,
            split_inboxes,
            mismatch_inboxes,
        )

    # Returned so tests can join it. Callers on the startup path
    # deliberately do not: the whole point is that it is off the
    # critical path.
    t = threading.Thread(target=_run, name="thread-roots-verify", daemon=True)
    t.start()
    return t
