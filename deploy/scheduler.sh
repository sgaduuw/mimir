#!/bin/sh
# Scheduled-tasks loop. Run as a sidecar container alongside the web
# container, sharing the /data volume. Each subcommand is invoked via
# `mimir`, opens its own DB session, and exits cleanly
# so a crash in one task doesn't take the loop down.
#
# Cadences (env-overridable, all in seconds):
#   WARM_CACHE_EVERY      default 60     ; refresh dashboard helpers
#   UPDATE_EVERY          default 300    ; sync upstream + ingest new commits
#   UPDATE_MAINLINE_EVERY default 600    ; check every configured tree for due-ness (per-tree cadence inside the CLI)
#   ANALYZE_EVERY         default 86400  ; refresh sqlite_stat1 (daily, bounded)
#   ANALYZE_FULL_EVERY    default 604800 ; full-sample sqlite_stat1 (weekly)
#   VACUUM_EVERY          default 604800 ; compact DB + collapse WAL (weekly)
#
# `update-mainline` is now multi-tree aware: each invocation
# iterates `Settings.trees` and walks every tree whose per-tree
# `walk_every_seconds` budget has elapsed since its last successful
# walk. Linus's cadence is ~25 min; subsystem trees hourly;
# linux-next daily (it rebases). The scheduler's UPDATE_MAINLINE_EVERY
# just bounds how often we check for due trees; the CLI handles the
# per-tree gating internally via `mainline_state.last_walked_at`.
# Skipped trees are cheap (one row read); HEAD-unchanged trees still
# short-circuit MAINTAINERS reparse on Linus.
#
# Timing is wall-clock, persisted across container restarts via
# `/data/.last_<task>` sentinel files. Without persistence, a release
# rollover at a cadence shorter than the slowest task (weekly VACUUM,
# daily ANALYZE) would reset the timer on every restart and the task
# would never fire (#202). Reading the sentinel mtime on boot means
# the cadence intent survives.
#
# Sleeps 10s between ticks; tighter than the warm-cache cadence so the
# loop is responsive after a task that took longer than its slot.
#
# 2.0.0: schema migration, inbox bootstrap, and post-migrate ANALYZE
# moved to the broker container's self-bootstrap sequence. This
# script no longer does any direct SQLite writes; it dispatches all
# scheduled work to the broker via RPC and gates on the broker's
# healthcheck via `depends_on` in compose.
#
# Ad-hoc pause: `touch /data/.scheduler-paused` quiesces the loop
# (no warm-cache / update / update-mainline / analyze /
# analyze --full / vacuum firings) within ~10s;
# `rm /data/.scheduler-paused` resumes. Used
# during operator maintenance (e.g. `admin canonicals backfill`,
# manual SQL) where scheduler write contention would extend the
# ad-hoc work. Initial-boot passes ignore the sentinel; if you
# restart the sidecar mid-maintenance, drop the sentinel first.

set -u

WARM_CACHE_EVERY=${WARM_CACHE_EVERY:-60}
UPDATE_EVERY=${UPDATE_EVERY:-300}
UPDATE_MAINLINE_EVERY=${UPDATE_MAINLINE_EVERY:-600}
ANALYZE_EVERY=${ANALYZE_EVERY:-86400}
# Full-scan ANALYZE (no analysis_limit cap) on a weekly cadence.
# The daily ANALYZE above uses settings.analyze_limit (default 4000
# from 1.36.4) which is fast (~1-3 s) and accurate enough for most
# planning, but a few tail-heavy indexes can still drift under that
# sample. The weekly full pass (analysis_limit=0) re-samples every
# row of every index, holding the writer lock ~25-30 s once a week
# in exchange for guaranteed-accurate stats. Override via
# ANALYZE_FULL_EVERY.
ANALYZE_FULL_EVERY=${ANALYZE_FULL_EVERY:-604800}
VACUUM_EVERY=${VACUUM_EVERY:-604800}

# Verbosity flag passed through to the underlying flask invocations.
# Default empty: warm-cache emits one summary line per tick, update
# stays silent on no-op ticks. Set to "-v" (or "-vv" for ingest
# detail) in compose env when troubleshooting; restart the sidecar
# to apply. For ad-hoc inspection without restarting:
#   podman exec mimir-tasks mimir warm-cache -v
SCHEDULER_VERBOSE=${SCHEDULER_VERBOSE:-}

log() { echo "[scheduler $(date -Iseconds)] $*"; }

# Wall-clock seconds since unix epoch for a file's mtime. Returns 0
# when the file is missing so the comparing tick fires immediately.
sentinel_mtime() {
    if [ -f "$1" ]; then
        # `date -r FILE +%s` is portable across BusyBox sh (alpine
        # base image) and coreutils; stat(1) syntax diverges.
        date -r "$1" +%s
    else
        echo 0
    fi
}

run() {
    label=$1
    sentinel=$2
    shift 2
    log "$label: start"
    if "$@"; then
        log "$label: ok"
        # Touch on success so a transient failure doesn't push the
        # next attempt out by a full cadence; the next tick will
        # retry. Sentinel skipped when empty (initial-run case).
        [ -n "$sentinel" ] && touch "$sentinel"
    else
        rc=$?
        log "$label: failed rc=$rc"
    fi
}

# Schema migration + inbox bootstrap + post-migrate ANALYZE all
# moved to the broker container as of 2.0.0. The broker is now the
# sole SQLite writer process; everything that touches the DB at
# startup runs inside its self-bootstrap sequence (gated by
# `.migrated`, `.bootstrapped`, and `.broker_initial_analyze`
# sentinels respectively). The tasks container is read-only at
# the SQLite level (every connection opens `query_only=1`) and
# its job here is purely orchestration: pre-flight warm, initial
# update, then the periodic loop firing RPCs at the broker.
#
# `depends_on: mimir-broker (service_healthy)` in compose.yaml
# orders the chain: broker self-bootstraps to a healthy state,
# THEN this container starts. So we can assume schema + inboxes
# are already in place when this script runs.

# Pre-flight warm-cache runs in the BACKGROUND so the main loop
# below stays responsive from t=0. The original justification was
# "the web tier shouldn't start serving until the dashboard cache
# is hot", but under 2.0.0 the web container `depends_on:
# mimir-broker` only (see compose.yaml), so it starts serving the
# moment the broker is healthy regardless of what this script is
# doing. Synchronously warming on a populated /data took up to
# ~2 h on a fully cold cache and blocked the loop's `update`,
# `update-mainline`, `analyze`, and `vacuum` ticks for that entire
# window: inbox sync stalled, mainline drifted, ANALYZE didn't run.
# Backgrounding the initial warm preserves the cache-warmth intent
# (the warm still runs from t=0) without serialising the rest of
# the loop behind it. The loop's WARM_CACHE_EVERY tick catches up
# on whatever the background pass hasn't reached yet.
#
# Initial `update` is no longer special-cased here either: the loop
# below fires it on the first iteration because /data/.last_update
# doesn't exist yet (sentinel_mtime returns 0, so now - 0 >=
# UPDATE_EVERY is trivially true). One code path, no preamble drift.
# shellcheck disable=SC2086  # SCHEDULER_VERBOSE is a flag string, intentionally unquoted to splat empty -> nothing.
run "warm-cache (initial, backgrounded)" /data/.last_warm \
    mimir warm-cache $SCHEDULER_VERBOSE &

# Persisted-mtime initialisation: read each sentinel's last-touch
# time off /data. Missing file → 0 → the next tick fires immediately
# (recovers from a deleted /data volume, or from this being the
# first deploy after #202). The post-migrate analyze and the two
# (initial) runs above wrote fresh sentinels for analyze/update/
# warm, so the in-loop ticks for those start with a fresh clock;
# vacuum is the only one whose first run is gated by the persisted
# mtime alone.
last_warm=$(sentinel_mtime /data/.last_warm)
last_update=$(sentinel_mtime /data/.last_update)
last_update_mainline=$(sentinel_mtime /data/.last_update_mainline)
last_analyze=$(sentinel_mtime /data/.last_analyze)
last_analyze_full=$(sentinel_mtime /data/.last_analyze_full)
last_vacuum=$(sentinel_mtime /data/.last_vacuum)

# Tracks whether we logged the most recent pause/resume transition so
# the journal carries one line per state change (rather than one per
# 10s tick while the sentinel sits).
was_paused=0

while true; do
    now=$(date +%s)

    if [ -f /data/.scheduler-paused ]; then
        if [ "$was_paused" -eq 0 ]; then
            log "paused (sentinel /data/.scheduler-paused present); skipping task ticks until removed"
            was_paused=1
        fi
        sleep 10
        continue
    fi
    if [ "$was_paused" -eq 1 ]; then
        log "resumed (sentinel cleared)"
        was_paused=0
    fi

    if [ $((now - last_warm)) -ge "$WARM_CACHE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "warm-cache" /data/.last_warm mimir warm-cache $SCHEDULER_VERBOSE
        last_warm=$(date +%s)
    fi

    if [ $((now - last_update)) -ge "$UPDATE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "update" /data/.last_update mimir update $SCHEDULER_VERBOSE
        last_update=$(date +%s)
    fi

    if [ $((now - last_update_mainline)) -ge "$UPDATE_MAINLINE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "update-mainline" /data/.last_update_mainline mimir update-mainline $SCHEDULER_VERBOSE
        last_update_mainline=$(date +%s)
    fi

    if [ $((now - last_analyze)) -ge "$ANALYZE_EVERY" ]; then
        run "analyze" /data/.last_analyze mimir analyze
        last_analyze=$(date +%s)
    fi

    # Weekly full ANALYZE (analysis_limit=0). The daily ANALYZE
    # above uses the bounded analysis_limit (default 4000 from
    # 1.36.4) which is fast and accurate enough for most planning,
    # but a few tail-heavy indexes can drift under that sample. The
    # full pass re-samples every row and holds the writer lock for
    # ~25-30 s; running once a week in exchange for guaranteed-
    # accurate plans is the safety net for distribution drift.
    if [ $((now - last_analyze_full)) -ge "$ANALYZE_FULL_EVERY" ]; then
        run "analyze --full" /data/.last_analyze_full mimir analyze --full
        last_analyze_full=$(date +%s)
        # Touch the daily-analyze sentinel too so the daily one
        # doesn't fire right after a full pass that just refreshed
        # the same stats.
        touch /data/.last_analyze
        last_analyze=$(date +%s)
    fi

    if [ $((now - last_vacuum)) -ge "$VACUUM_EVERY" ]; then
        run "vacuum" /data/.last_vacuum mimir vacuum
        last_vacuum=$(date +%s)
    fi

    sleep 10
done
