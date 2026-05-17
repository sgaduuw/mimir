"""`/healthz` (process liveness) and `/readyz` (DB-reachable readiness).

Liveness is cheap and has no DB dependency so load balancers can hit
it on the seconds cadence; readiness probes the DB so it's right for
the "serving traffic" decision, not for liveness restarts.
"""
import logging

from flask import Response
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.web._blueprint import bp_web

logger = logging.getLogger(__name__)


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
    'serving traffic' decision, not for liveness restarts.

    Exception details on a failed probe go to the structured log
    (operators see them via `journalctl` / `podman logs`), not the
    response body: leaking `repr(exc)` over the wire would surface
    SQLAlchemy driver type, connection-string fragments, and any
    embedded credential leak in the URL to whatever scraper is
    hitting the endpoint. The fixed-string body keeps the probe
    semantics (load balancer sees 503, marks unhealthy) without the
    information disclosure.
    """
    try:
        with SessionLocal() as session:
            session.execute(select(1))
    except Exception:  # pragma: no cover - defensive
        logger.exception("readyz: DB probe failed")
        return Response(
            "db unreachable\n",
            status=503,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})
