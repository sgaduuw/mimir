"""`/healthz` (process liveness) and `/readyz` (DB-reachable readiness).

Liveness is cheap and has no DB dependency so load balancers can hit
it on the seconds cadence; readiness probes the DB so it's right for
the "serving traffic" decision, not for liveness restarts.
"""
from flask import Response
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.web._blueprint import bp_web


@bp_web.route("/healthz")
def healthz():
    """Cheap liveness probe, confirms the app factory ran. No DB
    work; load balancers / orchestrators can hit this on the seconds-
    cadence they want."""
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})


@bp_web.route("/readyz")
def readyz():
    """Readiness probe, also confirms the DB is reachable via a
    `SELECT 1`. Slightly more expensive than /healthz; use for the
    'serving traffic' decision, not for liveness restarts."""
    try:
        with SessionLocal() as session:
            session.execute(select(1))
    except Exception as exc:  # pragma: no cover - defensive
        return Response(
            f"db unreachable: {exc!r}\n",
            status=503,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})
