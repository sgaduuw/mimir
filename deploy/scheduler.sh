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
# Timing is relative to container start, not wall-clock, fine for a
# personal-archive workload. Sleeps 10s between ticks; tighter than
# the warm-cache cadence so the loop is responsive after a task that
# took longer than its slot.
#
# alembic upgrade head runs once at start. This container is the
# single owner of DDL, the web container has no migration step
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

run() {
    label=$1; shift
    log "$label: start"
    if "$@"; then
        log "$label: ok"
    else
        rc=$?
        log "$label: failed rc=$rc"
    fi
}

log "alembic: start"
if alembic upgrade head; then
    log "alembic: ok"
    # Healthcheck sentinel: the web container's depends_on uses
    # condition: service_healthy and a `test -f /data/.migrated` test,
    # so gunicorn waits for this file before it starts serving.
    touch /data/.migrated
else
    log "alembic: failed (rc=$?), refusing to start sidecar loop"
    exit 1
fi

# Initial update so a fresh deployment has data to render before the
# first UPDATE_EVERY tick.
# shellcheck disable=SC2086  # SCHEDULER_VERBOSE is a flag string, intentionally unquoted to splat empty -> nothing.
run "update (initial)" flask --app mimir update $SCHEDULER_VERBOSE

now=$(date +%s)
last_warm=$now
last_update=$now
last_analyze=$now
last_vacuum=$now

# Ran initial update above; reset its counter so the next "update"
# tick fires UPDATE_EVERY from now, not from cold start.
# shellcheck disable=SC2086
run "warm-cache (initial)" flask --app mimir warm-cache $SCHEDULER_VERBOSE

while true; do
    now=$(date +%s)

    if [ $((now - last_warm)) -ge "$WARM_CACHE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "warm-cache" flask --app mimir warm-cache $SCHEDULER_VERBOSE
        last_warm=$(date +%s)
    fi

    if [ $((now - last_update)) -ge "$UPDATE_EVERY" ]; then
        # shellcheck disable=SC2086
        run "update" flask --app mimir update $SCHEDULER_VERBOSE
        last_update=$(date +%s)
    fi

    if [ $((now - last_analyze)) -ge "$ANALYZE_EVERY" ]; then
        run "analyze" flask --app mimir analyze
        last_analyze=$(date +%s)
    fi

    if [ $((now - last_vacuum)) -ge "$VACUUM_EVERY" ]; then
        run "vacuum" flask --app mimir vacuum
        last_vacuum=$(date +%s)
    fi

    sleep 10
done
