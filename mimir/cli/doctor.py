"""`mimir doctor`: structured config-health report.

Operator-facing pre-deploy / post-deploy / "something feels off"
diagnostic that walks a small matrix of config checks and prints
each result with a status (ok / warning / error / info). Exits 0
when everything is clean, 1 when any error fires, 2 when only
warnings fire.

The check matrix is intentionally extensible: each check is a
function `(role, settings, session_factory) -> CheckResult` and
the matrix is just a list of those functions. New checks slot in
as one new function + one entry in `_CHECKS`; the doctor frame
(rendering, JSON serialisation, exit-code derivation) handles the
rest.

Driven by the 2026-06-01 production incident where `SITE_BASE_URL`
was set on `mimir-tasks` and `mimir-app` but missing on
`mimir-broker`, silently neutering the warm-cache sitemap targets
in the broker handler. Pre-deploy `mimir doctor` against the
broker env would have flagged the gap before the cliff opened
~70 min after deploy. Pairs with the broker startup WARNING
(Layer 1) and the warm-handler WARNING (Layer 2): the WARNINGs
surface drift after the fact in the running log; `doctor`
surfaces it on demand against a configured-but-not-yet-running
process tree.
"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from typing import Callable

import click

# Status tokens kept as module-level constants so JSON output and
# the render-time symbol map don't drift apart.
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
STATUS_INFO = "info"

# Visible glyphs for the human-readable mode. ASCII-only so a
# misconfigured terminal locale doesn't garble the report; the
# 2026-05-21 review's "no em-dashes" rule generalises to symbols
# we can't guarantee render.
_SYMBOLS = {
    STATUS_OK: "[ok]",
    STATUS_WARNING: "[warn]",
    STATUS_ERROR: "[err]",
    STATUS_INFO: "[info]",
}


@dataclass
class CheckResult:
    """One row in the doctor report.

    `name` is operator-facing (e.g. "DATABASE_URL"), `status` is
    one of the STATUS_* tokens above, `value` is the short human-
    readable observation (printed beside the name), and
    `remediation` is the optional one-line "fix it like this"
    hint surfaced under warning/error rows AND in JSON output.

    Each check function returns one of these. The doctor frame
    catches exceptions raised inside a check and wraps them as
    error results with the exception text in `value`, so a
    misconfigured check doesn't crash the whole command.
    """

    name: str
    status: str
    value: str = ""
    remediation: str = ""


# A check is a callable that, given the runtime role + settings +
# session factory, returns a single CheckResult. Side-effecting
# (DB query, socket connect) is allowed and expected. The matrix
# below is the v1 set; new checks slot in by appending to _CHECKS.
CheckFunc = Callable[[str, "object", "object"], CheckResult]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_database_url(role, settings, session_factory) -> CheckResult:
    """DATABASE_URL must be set; for sqlite, the parent directory
    must exist (else the broker can't even create the file)."""
    url = getattr(settings, "database_url", "") or ""
    if not url:
        return CheckResult(
            name="DATABASE_URL",
            status=STATUS_ERROR,
            value="UNSET",
            remediation="Set DATABASE_URL in the deploy env (e.g. sqlite:////data/db/mimir.db).",
        )
    if url.startswith("sqlite:///"):
        # sqlite:////abs/path → /abs/path; sqlite:///rel/path → rel/path
        from pathlib import Path

        path_part = url[len("sqlite:///") :]
        # Triple-slash forms: the fourth slash makes it absolute.
        if not path_part.startswith("/") and url.startswith("sqlite:////"):
            path_part = "/" + path_part
        db_path = Path(path_part)
        parent = db_path.parent
        if not parent.exists():
            return CheckResult(
                name="DATABASE_URL",
                status=STATUS_ERROR,
                value=f"{url} (parent {parent} does not exist)",
                remediation=f"Create {parent} or fix DATABASE_URL to point at an existing parent directory.",
            )
    return CheckResult(name="DATABASE_URL", status=STATUS_OK, value=url)


def _check_secret_key(role, settings, session_factory) -> CheckResult:
    """SECRET_KEY non-empty. The pydantic validator already enforces
    min_length=16 at Settings construction time, so by the time the
    doctor runs an empty key would have crashed startup; the check
    is still useful as a reassurance row in the report."""
    key = getattr(settings, "secret_key", "") or ""
    if not key:
        return CheckResult(
            name="SECRET_KEY",
            status=STATUS_ERROR,
            value="UNSET",
            remediation="Set SECRET_KEY in the deploy env (>= 16 chars).",
        )
    return CheckResult(
        name="SECRET_KEY",
        status=STATUS_OK,
        value=f"set ({len(key)} chars)",
    )


def _check_site_base_url(role, settings, session_factory) -> CheckResult:
    """SITE_BASE_URL is the gate the 2026-06-01 incident named. Unset
    silently degrades sitemap warming; severity warning, not error,
    because the rest of the surface (request-time canonical URLs
    via ProxyFix) continues to work."""
    raw = (getattr(settings, "site_base_url", "") or "").strip()
    if not raw:
        return CheckResult(
            name="SITE_BASE_URL",
            status=STATUS_WARNING,
            value="UNSET (sitemap warming disabled)",
            remediation="Set SITE_BASE_URL in the broker container's environment to enable sitemap warming.",
        )
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return CheckResult(
            name="SITE_BASE_URL",
            status=STATUS_WARNING,
            value=f"{raw} (missing http:// or https:// scheme)",
            remediation="SITE_BASE_URL should be an absolute URL starting with http:// or https://.",
        )
    return CheckResult(name="SITE_BASE_URL", status=STATUS_OK, value=raw)


def _check_indexnow_key(role, settings, session_factory) -> CheckResult:
    """INDEXNOW_KEY is intentionally optional. An unset key disables
    the IndexNow push-notification feature (Bing/Yandex/etc.); the
    sitemap is still the discovery path. Report as info, not warning."""
    key = getattr(settings, "indexnow_key", None)
    if key:
        return CheckResult(
            name="INDEXNOW_KEY",
            status=STATUS_OK,
            value=f"set ({len(key)} chars)",
        )
    return CheckResult(
        name="INDEXNOW_KEY",
        status=STATUS_INFO,
        value="not set (default; IndexNow push notifications disabled)",
    )


def _check_broker_socket(role, settings, session_factory) -> CheckResult:
    """Every non-broker process needs the broker reachable. If this
    process IS the broker, the check is N/A (the broker doesn't
    self-RPC). For everyone else, try a one-shot AF_UNIX connect
    against the configured path. Severity error on failure: an
    unreachable broker means every cache write and every admin op
    fails."""
    if getattr(settings, "mimir_is_broker", False):
        return CheckResult(
            name="Broker socket",
            status=STATUS_OK,
            value="N/A (this IS the broker)",
        )
    path = getattr(settings, "broker_socket_path", None)
    if path is None:
        return CheckResult(
            name="Broker socket",
            status=STATUS_ERROR,
            value="UNSET",
            remediation="Set BROKER_SOCKET_PATH in this container's env (default /data/.broker.sock).",
        )
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(str(path))
    except OSError as exc:
        return CheckResult(
            name="Broker socket",
            status=STATUS_ERROR,
            value=f"unreachable at {path} ({exc})",
            remediation="Confirm mimir-broker container is running and the socket path matches.",
        )
    finally:
        try:
            s.close()
        except OSError:
            pass
    return CheckResult(name="Broker socket", status=STATUS_OK, value=str(path))


def _check_role(role, settings, session_factory) -> CheckResult:
    """Informational: each container should know its role. Helps an
    operator running `mimir doctor` inside `podman exec` confirm
    they're looking at the container they think they are."""
    return CheckResult(name="Role", status=STATUS_OK, value=role)


def _check_warm_cadence(role, settings, session_factory) -> CheckResult:
    """fast cadence must be tighter than slow cadence. Swapping the
    two would silently invert the tier semantics; the broker still
    runs both, but the operator's expectation about "fast = per-
    minute, slow = per-hour" is wrong."""
    fast = getattr(settings, "warm_cache_fast_every", 0)
    slow = getattr(settings, "warm_cache_slow_every", 0)
    if fast >= slow:
        return CheckResult(
            name="Warm-cache cadence",
            status=STATUS_WARNING,
            value=f"fast={fast}s slow={slow}s (fast should be smaller)",
            remediation="Set WARM_CACHE_FAST_EVERY < WARM_CACHE_SLOW_EVERY.",
        )
    return CheckResult(
        name="Warm-cache cadence",
        status=STATUS_OK,
        value=f"fast={fast}s slow={slow}s",
    )


def _check_warm_refresh_window(role, settings, session_factory) -> CheckResult:
    """The fast-tier refresh window should not be larger than ~30
    ticks worth (window vs cadence sanity). A window much larger
    than the cadence flattens the probabilistic-refresh ramp and
    every tick fires; not catastrophic but a sign of a mistuned env."""
    fast = getattr(settings, "warm_cache_fast_every", 0)
    window = getattr(settings, "warm_cache_fast_refresh_window_sec", 0)
    if fast > 0 and window >= fast * 30:
        return CheckResult(
            name="Warm-cache refresh windows",
            status=STATUS_WARNING,
            value=f"fast_window={window}s vs fast_every={fast}s (window > 30x cadence)",
            remediation="Reduce WARM_CACHE_FAST_REFRESH_WINDOW_SEC or raise WARM_CACHE_FAST_EVERY.",
        )
    slow_window = getattr(settings, "warm_cache_slow_refresh_window_sec", 0)
    return CheckResult(
        name="Warm-cache refresh windows",
        status=STATUS_OK,
        value=f"fast={window}s slow={slow_window}s",
    )


def _check_inboxes_seeded(role, settings, session_factory) -> CheckResult:
    """At least one inbox must exist in the DB. Zero inboxes means
    a deploy is non-functional (no archive to browse) but not
    strictly broken: the broker still serves cache ops. Warning."""
    from mimir.models import Inbox
    from sqlalchemy import func, select

    with session_factory() as s:
        count = s.execute(select(func.count()).select_from(Inbox)).scalar_one()
    if count == 0:
        return CheckResult(
            name="Inboxes seeded",
            status=STATUS_WARNING,
            value="0 inboxes",
            remediation="Run `mimir bootstrap-inboxes` or seed INBOXES via env.",
        )
    return CheckResult(
        name="Inboxes seeded",
        status=STATUS_OK,
        value=f"{count} inbox(es)",
    )


def _check_mirror_paths(role, settings, session_factory) -> CheckResult:
    """For each configured inbox, the mirror_path directory must
    exist. A missing mirror is a silent ingest-skip surface: the
    `update` tick can't fetch into it, and the read path can't
    fetch blobs out of it. Warning, not error: the broker still
    runs."""
    from pathlib import Path

    from mimir.models import Inbox
    from sqlalchemy import select

    with session_factory() as s:
        rows = s.execute(select(Inbox.name, Inbox.mirror_path)).all()
    if not rows:
        return CheckResult(
            name="Mirror paths",
            status=STATUS_INFO,
            value="no inboxes configured",
        )
    missing: list[str] = []
    for name, mirror_path in rows:
        if not Path(mirror_path).exists():
            missing.append(name)
    if missing:
        # Cap the displayed list so a 200-inbox corpus with a bad
        # mount doesn't smear across the report.
        sample = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        return CheckResult(
            name="Mirror paths",
            status=STATUS_WARNING,
            value=f"{len(missing)} missing: {sample}{suffix}",
            remediation="Confirm the inbox bind mounts; ingest will skip missing mirrors.",
        )
    return CheckResult(
        name="Mirror paths",
        status=STATUS_OK,
        value=f"all {len(rows)} director(y/ies) present",
    )


# Append-only check matrix. Operators may extend later by writing a
# new `_check_*` function returning a CheckResult and appending it
# here. The frame iterates this list in order; output order matches.
_CHECKS: list[CheckFunc] = [
    _check_database_url,
    _check_secret_key,
    _check_site_base_url,
    _check_indexnow_key,
    _check_broker_socket,
    _check_role,
    _check_warm_cadence,
    _check_warm_refresh_window,
    _check_inboxes_seeded,
    _check_mirror_paths,
]


# ---------------------------------------------------------------------------
# Report assembly + rendering
# ---------------------------------------------------------------------------


@dataclass
class _Report:
    role: str
    version: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        s = {STATUS_OK: 0, STATUS_WARNING: 0, STATUS_ERROR: 0, STATUS_INFO: 0}
        for c in self.checks:
            s[c.status] = s.get(c.status, 0) + 1
        return s

    @property
    def exit_code(self) -> int:
        # Errors trump warnings: exit 1 on any error, exit 2 on any
        # warning (and no error), exit 0 otherwise. Info rows never
        # affect the exit code.
        summary = self.summary
        if summary[STATUS_ERROR] > 0:
            return 1
        if summary[STATUS_WARNING] > 0:
            return 2
        return 0


def _run_checks(role: str, version: str, settings, session_factory) -> _Report:
    """Execute every check in the matrix, wrap exceptions, return
    the aggregated report. Each check runs in its own try/except so
    a bug in one row can't suppress the rest."""
    report = _Report(role=role, version=version)
    for check in _CHECKS:
        try:
            result = check(role, settings, session_factory)
        except Exception as exc:  # noqa: BLE001
            result = CheckResult(
                name=getattr(check, "__name__", "<check>").removeprefix("_check_"),
                status=STATUS_ERROR,
                value=f"check raised: {exc!r}",
                remediation="Investigate the doctor check; the check itself crashed.",
            )
        report.checks.append(result)
    return report


def _render_human(report: _Report) -> str:
    """Human-readable rendering. Single column of `[status] Name
    value`, then a remediation line per warning/error, then a
    summary footer + exit-code hint."""
    lines: list[str] = []
    header = f"mimir doctor (running as {report.role}, version {report.version})"
    lines.append(header)
    lines.append("-" * len(header))
    # Pad names so the value column lines up. Cap the pad so a long
    # check name doesn't push the value off-screen.
    name_w = min(40, max((len(c.name) for c in report.checks), default=10))
    for c in report.checks:
        sym = _SYMBOLS.get(c.status, "[?]")
        lines.append(f"{sym} {c.name.ljust(name_w)}  {c.value}")
        if c.remediation and c.status in (STATUS_WARNING, STATUS_ERROR):
            lines.append(f"    -> {c.remediation}")
    s = report.summary
    lines.append("")
    lines.append(
        f"{s[STATUS_OK]} ok, {s[STATUS_WARNING]} warning, "
        f"{s[STATUS_ERROR]} error, {s[STATUS_INFO]} info."
    )
    code = report.exit_code
    if code == 0:
        lines.append("Exit code: 0 (all checks passed)")
    elif code == 2:
        lines.append("Exit code: 2 (warnings present)")
    else:
        lines.append("Exit code: 1 (errors present)")
    return "\n".join(lines)


def _render_json(report: _Report) -> str:
    payload = {
        "role": report.role,
        "version": report.version,
        "checks": [asdict(c) for c in report.checks],
        "summary": {
            "ok": report.summary[STATUS_OK],
            "warning": report.summary[STATUS_WARNING],
            "error": report.summary[STATUS_ERROR],
            "info": report.summary[STATUS_INFO],
        },
        "exit_code": report.exit_code,
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("doctor")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit a JSON document on stdout instead of the human-readable report.",
)
def doctor_command(json_output: bool) -> None:
    """Run a structured config-health check.

    Walks a small matrix of checks against the deploy env + DB,
    prints each result with a status (ok / warning / error / info)
    and exits 0 / 2 / 1 respectively (info rows do not affect the
    exit code). Use pre-deploy to catch obvious drift, or
    post-deploy when investigating a misbehaving tier.
    """
    from mimir import __version__
    from mimir.config import settings
    from mimir.extensions import SessionLocal

    role = "broker" if settings.mimir_is_broker else "web/tasks"
    report = _run_checks(role, __version__, settings, SessionLocal)
    if json_output:
        click.echo(_render_json(report))
    else:
        click.echo(_render_human(report))
    raise SystemExit(report.exit_code)


__all__ = [
    "CheckResult",
    "STATUS_ERROR",
    "STATUS_INFO",
    "STATUS_OK",
    "STATUS_WARNING",
    "doctor_command",
]
