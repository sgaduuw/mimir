#!/bin/sh
# Scheduled-tasks loop. Run as a sidecar container alongside the web
# container, sharing the /data volume. Each subcommand is invoked via
# `flask --app mimir`, opens its own DB session, and exits cleanly
# so a crash in one task doesn't take the loop down.
#
# Cadences (env-overridable, all in seconds):
#   WARM_CACHE_EVERY  default 60     ; refresh dashboard helpers
#   UPDATE_EVERY      default 300    ; sync upstream + ingest new commits
#   ANALYZE_EVERY     default 86400  ; refresh sqlite_stat1 (daily)
#   VACUUM_EVERY      default 604800 ; compact DB + collapse WAL (weekly)
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

set -u

WARM_CACHE_EVERY=${WARM_CACHE_EVERY:-60}
UPDATE_EVERY=${UPDATE_EVERY:-300}
ANALYZE_EVERY=${ANALYZE_EVERY:-86400}
VACUUM_EVERY=${VACUUM_EVERY:-604800}

# Verbosity flag passed through to the underlying flask invocations.
# Default empty: warm-cache emits one summary line per tick, update
# stays silent on no-op ticks. Set to "-v" (or "-vv" for ingest
# detail) in compose env when troubleshooting; restart the sidecar
# to apply. For ad-hoc inspection without restarting:
#   podman exec mimir-tasks flask --app mimir warm-cache -v
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

# Refresh planner stats before unblocking the web tier. A migration
# in the just-applied stack can add a new index; without this pass
# sqlite_stat1 has no entry for it and the planner picks shapes
# blind (#202). Cheap on a multi-million-row corpus (tens of
# seconds), idempotent on subsequent restarts.
run "analyze (post-migrate)" /data/.last_analyze flask --app mimir analyze

# Healthcheck sentinel: the web container's depends_on uses
# condition: service_healthy and a `test -f /data/.migrated` test,
# so gunicorn waits for this file before it starts serving.
touch /data/.migrated

# Initial update so a fresh deployment has data to render before the
# first UPDATE_EVERY tick.
# shellcheck disable=SC2086  # SCHEDULER_VERBOSE is a flag string, intentionally unquoted to splat empty -> nothing.
run "update (initial)" /data/.last_update flask --app mimir update $SCHEDULER_VERBOSE

# shellcheck disable=SC2086
run "warm-cache (initial)" /data/.last_warm flask --app mimir warm-cache $SCHEDULER_VERBOSE

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
last_analyze=$(sentinel_mtime /data/.last_analyze)
last_vacuum=$(sentinel_mtime /data/.last_vacuum)

while true; do
    now=$(date +%s)

    if [ $((now - last_warm)) -ge "$WARM_CACHE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "warm-cache" /data/.last_warm flask --app mimir warm-cache $SCHEDULER_VERBOSE
        last_warm=$(date +%s)
    fi

    if [ $((now - last_update)) -ge "$UPDATE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "update" /data/.last_update flask --app mimir update $SCHEDULER_VERBOSE
        last_update=$(date +%s)
    fi

    if [ $((now - last_analyze)) -ge "$ANALYZE_EVERY" ]; then
        run "analyze" /data/.last_analyze flask --app mimir analyze
        last_analyze=$(date +%s)
    fi

    if [ $((now - last_vacuum)) -ge "$VACUUM_EVERY" ]; then
        run "vacuum" /data/.last_vacuum flask --app mimir vacuum
        last_vacuum=$(date +%s)
    fi

    sleep 10
done
