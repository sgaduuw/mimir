#!/bin/sh
# Scheduled-tasks loop. Run as a sidecar container alongside the web
# container, sharing the /data volume. Each subcommand is invoked via
# `mimir`, opens its own DB session, and exits cleanly
# so a crash in one task doesn't take the loop down.
#
# Cadences (env-overridable, all in seconds):
#   WARM_CACHE_EVERY      default 60     ; refresh dashboard helpers
#   UPDATE_EVERY          default 300    ; sync upstream + ingest new commits
#   UPDATE_MAINLINE_EVERY default 600    ; fetch linux.git + (re)parse MAINTAINERS
#   ANALYZE_EVERY         default 86400  ; refresh sqlite_stat1 (daily, bounded)
#   ANALYZE_FULL_EVERY    default 604800 ; full-sample sqlite_stat1 (weekly)
#   VACUUM_EVERY          default 604800 ; compact DB + collapse WAL (weekly)
#
# `update-mainline` no-ops cheaply when the mainline HEAD hasn't
# moved (state.last_commit_sha == fetched HEAD short-circuits the
# MAINTAINERS reparse; the Link-trailer walk is incremental). The
# 10-min default is "small enough that a kernel-tree subsystem
# rename or a new MAINTAINERS section propagates to the From-line
# allowlist within one rebase cycle, large enough that the per-tick
# git fetch on mainline.kernel.org stays polite."
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
# alembic upgrade head runs once at start, followed by an ANALYZE so
# any new index introduced by the just-applied migrations starts life
# with planner stats instead of being silently invisible to the
# optimiser until the next scheduled ANALYZE pass. This container is
# the single owner of DDL, the web container has no migration step
# and waits on this sidecar's healthcheck (which fires once the
# sentinel `/data/.migrated` is touched, below). Idempotent.
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

log "alembic: start"
if alembic upgrade head; then
    log "alembic: ok"
else
    log "alembic: failed (rc=$?), refusing to start sidecar loop"
    exit 1
fi

# Seed env-configured inboxes into the `inboxes` table. The web
# container's create_app() does not do this (writes belong on the
# sidecar, alongside migrations); without this step a fresh deploy
# would come up with an empty `inboxes` table and `/` would render
# a blank meta-index. Idempotent via ON CONFLICT DO NOTHING; admin
# edits to existing rows are never clobbered.
run "bootstrap-inboxes" "" mimir bootstrap-inboxes

# Refresh planner stats before unblocking the web tier. A migration
# in the just-applied stack can add a new index; without this pass
# sqlite_stat1 has no entry for it and the planner picks shapes
# blind (#202). Cheap on a multi-million-row corpus (tens of
# seconds), idempotent on subsequent restarts.
run "analyze (post-migrate)" /data/.last_analyze mimir analyze

# Pre-flight warm so the web tier doesn't start serving until the
# dashboard cache is hot. Without this, every container recreate
# served the first wave of `/`, `/<inbox>/` requests cold: each
# render miss writes back through the broker, and under broker mode
# (1.36.0+) any concurrent ingest commit makes that cache.set wait
# hundreds of ms. Multiplied by the ~8 cached surfaces a dashboard
# composes, the first-wave renders stacked seconds of wait time and
# tripped the gateway timeout. Pre-flight warm trades 30-60 s of
# post-migrate boot latency for zero cold-cache requests served
# after `.migrated` lands. Steady-state `warm-cache` ticks in the
# loop below keep the cache fresh; this initial pass closes the
# cold-start gap.
# shellcheck disable=SC2086  # SCHEDULER_VERBOSE is a flag string, intentionally unquoted to splat empty -> nothing.
run "warm-cache (initial)" /data/.last_warm mimir warm-cache $SCHEDULER_VERBOSE

# Healthcheck sentinel: the web container's depends_on uses
# condition: service_healthy and a `test -f /data/.migrated` test,
# so gunicorn waits for this file before it starts serving. Touched
# only after migrations + inbox bootstrap + post-migrate ANALYZE +
# pre-flight warm-cache, the four things the web tier needs to be
# in place before serving.
touch /data/.migrated

# Initial update so a fresh deployment has data to render before the
# first UPDATE_EVERY tick. Runs after `.migrated` so the web tier is
# already serving; an update that takes minutes on a backlogged
# upstream doesn't gate web startup behind it. The per-inbox post-
# ingest warm (in `mimir.ingest.orchestrate._warm_after_ingest`)
# refreshes the cache for inboxes that received new messages during
# this initial tick.
# shellcheck disable=SC2086
run "update (initial)" /data/.last_update mimir update $SCHEDULER_VERBOSE

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
