"""Tests for mimir/web/routes/health.py: `/healthz` (liveness)
and `/readyz` (readiness) endpoints."""
from unittest.mock import patch


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_readyz(client):
    assert client.get("/readyz").status_code == 200


def test_readyz_503_body_carries_no_exception_repr(client):
    """The DB-down branch returns 503 with a fixed-string body, NOT
    `repr(exc)`. Leaking the SQLAlchemy / driver exception to an
    unauthenticated probe surfaces connection-string fragments,
    driver type, and any embedded credential leak in the URL
    (CodeQL alert #13, `py/stack-trace-exposure`). The fix logs
    the exception via `logger.exception` and emits a fixed body.

    Forces the failure by patching `SessionLocal` to raise on
    `__enter__` -- the exception path the readyz handler catches.
    """
    target = "mimir.web.routes.health.SessionLocal"
    with patch(target) as mock_sl:
        mock_sl.return_value.__enter__.side_effect = RuntimeError(
            "secret-connstring://user:hunter2@db.internal/mimir"
        )
        r = client.get("/readyz")
    assert r.status_code == 503
    body = r.data.decode()
    # Fixed string, no leak.
    assert body == "db unreachable\n"
    # Belt-and-braces: even if the response shape changes, the
    # specific leak markers from the forced exception must not
    # appear (credentials, connection-string scheme, exception
    # class name, internal hostname).
    assert "hunter2" not in body
    assert "secret-connstring" not in body
    assert "RuntimeError" not in body
    assert "db.internal" not in body
