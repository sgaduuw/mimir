"""Tests for `mimir/cli/doctor.py`.

Covers:
- happy-path: all checks ok, exit 0.
- SITE_BASE_URL unset surfaces as a warning (exit 2). The 2026-06-01
  incident's load-bearing case.
- Unreachable broker socket surfaces as an error (exit 1).
- INDEXNOW_KEY unset is informational, not a warning.
- `--json` emits a structured payload with the documented keys.

The doctor command calls `sys.exit(report.exit_code)` via
`SystemExit`; Click's `CliRunner` captures that into `result.exit_code`.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from mimir.cli import doctor_command


@pytest.fixture
def runner():
    return CliRunner()


def test_doctor_all_ok(runner, seeded_db, monkeypatch, tmp_path):
    """Happy path: every check passes, exit 0. Configures the env
    so SITE_BASE_URL is set, INDEXNOW_KEY is unset (info, not
    warning), broker is reachable, and inboxes are seeded with
    mirror paths that exist."""
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    # Make the broker socket check pass by claiming this process is
    # the broker; the doctor short-circuits to "N/A (this IS the
    # broker)" without trying to connect.
    monkeypatch.setattr(live_settings, "mimir_is_broker", True)
    monkeypatch.setattr(live_settings, "site_base_url", "https://example.com")
    monkeypatch.setattr(live_settings, "indexnow_key", None)

    # Seeded inboxes carry `/tmp/alpha` and `/tmp/beta` from
    # conftest's _reset_db; create those directories so the
    # mirror-paths check passes.
    Path("/tmp/alpha").mkdir(exist_ok=True)
    Path("/tmp/beta").mkdir(exist_ok=True)

    # Re-point seeded inboxes at tmp directories that exist for sure
    # (in case the global /tmp paths get cleaned up between runs).
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    with SessionLocal() as s:
        for name, mp in (("alpha", alpha_dir), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, [])
    assert result.exit_code == 0, result.output
    assert "[ok]" in result.output
    # The summary footer names the totals.
    assert "ok," in result.output
    assert "Exit code: 0" in result.output


def test_doctor_warns_on_unset_site_base_url(runner, seeded_db, monkeypatch, tmp_path):
    """SITE_BASE_URL empty surfaces as a warning row (exit 2). This
    is the load-bearing 2026-06-01 incident shape: an operator
    running `mimir doctor` against the broker container would have
    seen the gap before deploy."""
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    monkeypatch.setattr(live_settings, "mimir_is_broker", True)
    monkeypatch.setattr(live_settings, "site_base_url", "")
    monkeypatch.setattr(live_settings, "indexnow_key", None)

    # Mirror paths exist so they don't perturb the warning count.
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    with SessionLocal() as s:
        for name, mp in (("alpha", alpha_dir), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, [])
    assert result.exit_code == 2, result.output
    assert "SITE_BASE_URL" in result.output
    assert "[warn]" in result.output
    assert "Exit code: 2" in result.output


def test_doctor_errors_on_unreachable_broker(runner, seeded_db, monkeypatch, tmp_path):
    """When this process is NOT the broker and broker_socket_path
    points at a nonexistent socket, exit 1 (any error trumps any
    warnings). The check name + status flagged on the row."""
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    monkeypatch.setattr(live_settings, "mimir_is_broker", False)
    monkeypatch.setattr(live_settings, "site_base_url", "https://example.com")
    monkeypatch.setattr(live_settings, "indexnow_key", None)
    # Nonexistent socket path: AF_UNIX connect will raise ENOENT.
    bogus_sock = tmp_path / "absent.sock"
    monkeypatch.setattr(live_settings, "broker_socket_path", bogus_sock)

    # Repoint mirrors so they don't add their own warnings.
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    with SessionLocal() as s:
        for name, mp in (("alpha", alpha_dir), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, [])
    assert result.exit_code == 1, result.output
    assert "Broker socket" in result.output
    assert "[err]" in result.output
    assert "Exit code: 1" in result.output


def test_doctor_json_output_shape(runner, seeded_db, monkeypatch, tmp_path):
    """`--json` emits a JSON payload with role/version/checks/summary
    /exit_code keys, and each check carries name/status/value/
    remediation fields. Pinning the wire shape so a future scripting
    consumer doesn't break silently."""
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    monkeypatch.setattr(live_settings, "mimir_is_broker", True)
    monkeypatch.setattr(live_settings, "site_base_url", "")
    monkeypatch.setattr(live_settings, "indexnow_key", None)

    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    with SessionLocal() as s:
        for name, mp in (("alpha", alpha_dir), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, ["--json"])
    assert result.exit_code == 2, result.output  # site_base_url unset → warning
    payload = json.loads(result.output)
    assert payload["role"] == "broker"
    assert "version" in payload
    assert isinstance(payload["checks"], list)
    assert payload["exit_code"] == 2
    assert "summary" in payload
    for c in payload["checks"]:
        assert "name" in c and "status" in c and "value" in c
    # The SITE_BASE_URL row is in there and flagged warning.
    site_row = next(c for c in payload["checks"] if c["name"] == "SITE_BASE_URL")
    assert site_row["status"] == "warning"
    assert site_row["remediation"]


def test_doctor_indexnow_unset_is_ok_not_warning(
    runner, seeded_db, monkeypatch, tmp_path
):
    """INDEXNOW_KEY is intentionally optional. An unset key reports
    info ("not set; IndexNow disabled"), not a warning; the exit
    code is unaffected by its absence."""
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    monkeypatch.setattr(live_settings, "mimir_is_broker", True)
    monkeypatch.setattr(live_settings, "site_base_url", "https://example.com")
    monkeypatch.setattr(live_settings, "indexnow_key", None)

    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    with SessionLocal() as s:
        for name, mp in (("alpha", alpha_dir), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, ["--json"])
    payload = json.loads(result.output)
    indexnow_row = next(c for c in payload["checks"] if c["name"] == "INDEXNOW_KEY")
    assert indexnow_row["status"] == "info"
    assert "IndexNow" in indexnow_row["value"]
    # Info rows don't add to the warning bucket; with no other
    # issues the exit is 0.
    assert payload["summary"]["warning"] == 0
    assert payload["exit_code"] == 0


def test_doctor_warns_on_missing_mirror_path(runner, seeded_db, monkeypatch, tmp_path):
    """`_check_mirror_paths` in mimir/cli/doctor.py:295 walks every
    configured inbox and reports a warning when the inbox's
    mirror_path directory doesn't exist on disk. CONTEXT.md
    "Production deployment posture": a missing bind mount means
    the read path can't fetch blobs and `update` can't pull,
    silently breaking ingest. Pin that doctor surfaces this
    pre-deploy.

    Audit asked for this specific failure mode. Existing tests
    all `mkdir()` the mirror dirs; this one deliberately doesn't,
    asserting the WARN exit code and the "missing" diagnostic.
    """
    from mimir.config import settings as live_settings
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from sqlalchemy import select

    monkeypatch.setattr(live_settings, "mimir_is_broker", True)
    monkeypatch.setattr(live_settings, "site_base_url", "https://example.com")
    monkeypatch.setattr(live_settings, "indexnow_key", None)

    # Re-point alpha at a directory that DOESN'T exist (no mkdir);
    # beta gets a real one so the test isolates the mirror check.
    missing = tmp_path / "alpha-not-on-disk"
    beta_dir = tmp_path / "beta-real"
    beta_dir.mkdir()
    assert not missing.exists()
    with SessionLocal() as s:
        for name, mp in (("alpha", missing), ("beta", beta_dir)):
            ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
            ix.mirror_path = str(mp)
        s.commit()

    result = runner.invoke(doctor_command, ["--json"])
    assert result.exit_code != 0, (
        f"doctor must exit non-zero when an inbox's mirror_path is "
        f"missing on disk; got exit={result.exit_code} output={result.output!r}"
    )
    payload = json.loads(result.output)
    mirror_check = next(c for c in payload["checks"] if c["name"] == "Mirror paths")
    assert mirror_check["status"] == "warning", (
        f"Mirror paths check must report warning; got "
        f"{mirror_check['status']!r}, value={mirror_check.get('value')!r}"
    )
    assert "alpha" in mirror_check["value"], (
        f"the missing inbox name (alpha) must surface in the warning "
        f"value so the operator knows WHICH bind mount to check; got "
        f"{mirror_check['value']!r}"
    )
    assert payload["summary"]["warning"] >= 1
