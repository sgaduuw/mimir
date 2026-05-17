"""Tests for mimir/web/routes/health.py: `/healthz` (liveness)
and `/readyz` (readiness) endpoints."""


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_readyz(client):
    assert client.get("/readyz").status_code == 200
