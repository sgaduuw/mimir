"""`admin robots …`: CRUD on the `robots_rules` table.

Thin click wrappers that dispatch writes via the broker (post-
2.0.0 the broker is the sole writer process). Read commands
(`list`, `show`) hit the DB directly via `query_only=1`
connections.

Broker error mapping: the service-layer `RobotsRuleNotFound` /
`RobotsValidationError` arrive over the wire as
`Reply(ok=False, error="RobotsRuleNotFound:<msg>")` /
`"InvalidRobotsRule:<msg>"`. `_robots_click_error` parses the
prefix and re-raises as `ClickException`.
"""

import click

from mimir.cli.admin import admin_group
from mimir.extensions import SessionLocal
from mimir.robots import RobotsValidationError, get_rule, list_rules


def _robots_click_error(exc) -> click.ClickException:
    """Unwrap the broker's structured Reply.error string back to the
    operator-facing message the direct path would have raised."""
    raw = str(exc)
    _, _, reply_error = raw.partition(": ")
    if reply_error.startswith("RobotsRuleNotFound:"):
        return click.ClickException(reply_error[len("RobotsRuleNotFound:") :])
    if reply_error.startswith("InvalidRobotsRule:"):
        return click.ClickException(reply_error[len("InvalidRobotsRule:") :])
    return click.ClickException(raw)


def _parse_content_signal_pair(raw: str) -> tuple[str, str]:
    """`KEY=VALUE` → `("key", "value")`. Click invokes this per
    `--content-signal` and `--set-content-signal` value."""
    if "=" not in raw:
        raise click.BadParameter(
            f"content-signal pair {raw!r} must be in KEY=VALUE form"
        )
    k, _, v = raw.partition("=")
    return k.strip(), v.strip()


def _format_content_signals(signals: dict[str, str]) -> str:
    """Canonical-order rendering matching the service-layer."""
    canonical_order = ("search", "ai-input", "ai-train")
    parts = [f"{k}={signals[k]}" for k in canonical_order if k in signals]
    return ", ".join(parts)


def _format_rule(rule_dict: dict) -> list[str]:
    """Multi-line pretty-print of one rule dict (used by `show` and
    the add/update success output). Matches the rendered file's
    stanza shape so an operator can eyeball the resulting block."""
    lines = [f"User-agent: {rule_dict['user_agent']}"]
    signals = rule_dict.get("content_signals") or {}
    if signals:
        lines.append(f"Content-Signal: {_format_content_signals(signals)}")
    if rule_dict.get("crawl_delay") is not None:
        lines.append(f"Crawl-delay: {rule_dict['crawl_delay']}")
    for p in rule_dict.get("disallow_paths") or []:
        lines.append(f"Disallow: {p}")
    return lines


@admin_group.group("robots")
def admin_robots_group() -> None:
    """CRUD on the `robots_rules` table that backs `/robots.txt`.

    Operator manages User-agent stanzas, Disallow paths, and
    Crawl-delay values at runtime; the `/robots.txt` route renders
    from the table on every request. The `*` stanza is structural
    (seeded by migration); use `reset` to restore its defaults."""


@admin_robots_group.command("list")
def admin_robots_list_command() -> None:
    """List every robots_rules row (`*` first, then alphabetical)."""
    with SessionLocal() as session:
        rules = list_rules(session)
    if not rules:
        click.echo("(no rules; /robots.txt would only emit a Sitemap line)")
        return
    ua_w = max(len(r.user_agent) for r in rules)
    for r in rules:
        delay = "-" if r.crawl_delay is None else str(r.crawl_delay)
        paths = ", ".join(r.disallow_paths or []) or "-"
        signals = _format_content_signals(r.content_signals or {}) or "-"
        click.echo(
            f"{r.user_agent:<{ua_w}}  crawl_delay={delay:<4}  "
            f"disallow={paths}  content_signals={signals}"
        )


@admin_robots_group.command("show")
@click.argument("user_agent")
def admin_robots_show_command(user_agent: str) -> None:
    """Detail view for one User-agent's stanza."""
    try:
        with SessionLocal() as session:
            rule = get_rule(session, user_agent)
    except RobotsValidationError as exc:
        raise click.ClickException(str(exc))
    if rule is None:
        raise click.ClickException(f"no rule for user_agent {user_agent!r}")
    click.echo(f"user_agent:     {rule.user_agent}")
    click.echo(
        f"crawl_delay:    {rule.crawl_delay if rule.crawl_delay is not None else '(unset)'}"
    )
    paths = list(rule.disallow_paths or [])
    if paths:
        click.echo("disallow_paths:")
        for p in paths:
            click.echo(f"  - {p}")
    else:
        click.echo("disallow_paths: (none)")
    signals = dict(rule.content_signals or {})
    if signals:
        click.echo("content_signals:")
        for k in ("search", "ai-input", "ai-train"):
            if k in signals:
                click.echo(f"  - {k}={signals[k]}")
    else:
        click.echo("content_signals: (none)")


@admin_robots_group.command("add")
@click.argument("user_agent")
@click.option(
    "--disallow",
    multiple=True,
    help="One Disallow path to seed (repeatable).",
)
@click.option(
    "--crawl-delay",
    type=int,
    default=None,
    help="Crawl-delay seconds (omit to leave unset).",
)
@click.option(
    "--content-signal",
    multiple=True,
    help="KEY=VALUE pair for Content-Signal (repeatable). "
    "Keys: search, ai-input, ai-train. Values: yes, no.",
)
def admin_robots_add_command(
    user_agent: str,
    disallow: tuple[str, ...],
    crawl_delay: int | None,
    content_signal: tuple[str, ...],
) -> None:
    """Insert a new User-agent stanza.

    Common operational shapes: block a misbehaving bot with
    `admin robots add GPTBot --disallow /`. Express per-bot AI
    consent with
    `admin robots add Googlebot --content-signal search=yes`."""
    signals = dict(_parse_content_signal_pair(s) for s in content_signal)
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        rule_dict = get_broker_client().robots_add(
            user_agent,
            disallow=list(disallow),
            crawl_delay=crawl_delay,
            content_signals=signals or None,
        )
    except BrokerUnavailable as exc:
        raise _robots_click_error(exc)
    click.echo(f"added rule for user_agent {rule_dict['user_agent']!r}:")
    for line in _format_rule(rule_dict):
        click.echo(f"  {line}")


@admin_robots_group.command("update")
@click.argument("user_agent")
@click.option(
    "--add-disallow",
    multiple=True,
    help="Disallow path to add (repeatable; no-op if already present).",
)
@click.option(
    "--remove-disallow",
    multiple=True,
    help="Disallow path to remove (repeatable; no-op if absent).",
)
@click.option(
    "--crawl-delay",
    type=int,
    default=None,
    help="Set Crawl-delay seconds. Mutually exclusive with --clear-crawl-delay.",
)
@click.option(
    "--clear-crawl-delay",
    is_flag=True,
    help="Drop the Crawl-delay line from this stanza.",
)
@click.option(
    "--set-content-signal",
    multiple=True,
    help="KEY=VALUE pair to upsert in Content-Signal (repeatable). "
    "Keys: search, ai-input, ai-train. Values: yes, no.",
)
@click.option(
    "--clear-content-signal",
    multiple=True,
    help="One Content-Signal KEY to drop (repeatable).",
)
@click.option(
    "--clear-all-content-signals",
    is_flag=True,
    help="Drop the entire Content-Signal line from this stanza. "
    "Mutually exclusive with --set-content-signal / --clear-content-signal.",
)
def admin_robots_update_command(
    user_agent: str,
    add_disallow: tuple[str, ...],
    remove_disallow: tuple[str, ...],
    crawl_delay: int | None,
    clear_crawl_delay: bool,
    set_content_signal: tuple[str, ...],
    clear_content_signal: tuple[str, ...],
    clear_all_content_signals: bool,
) -> None:
    """Mutate an existing stanza in place."""
    set_signals = dict(_parse_content_signal_pair(s) for s in set_content_signal)
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        rule_dict = get_broker_client().robots_update(
            user_agent,
            add_disallow=list(add_disallow),
            remove_disallow=list(remove_disallow),
            crawl_delay=crawl_delay,
            clear_crawl_delay=clear_crawl_delay,
            set_content_signal=set_signals or None,
            clear_content_signal=list(clear_content_signal),
            clear_all_content_signals=clear_all_content_signals,
        )
    except BrokerUnavailable as exc:
        raise _robots_click_error(exc)
    click.echo(f"updated rule for user_agent {rule_dict['user_agent']!r}:")
    for line in _format_rule(rule_dict):
        click.echo(f"  {line}")


@admin_robots_group.command("remove")
@click.argument("user_agent")
def admin_robots_remove_command(user_agent: str) -> None:
    """Drop one User-agent stanza. Refuses `*` (use `reset` instead)."""
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        get_broker_client().robots_remove(user_agent)
    except BrokerUnavailable as exc:
        raise _robots_click_error(exc)
    click.echo(f"removed rule for user_agent {user_agent!r}")


@admin_robots_group.command("reset")
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
def admin_robots_reset_command(yes: bool) -> None:
    """Drop every row and re-seed the `*` stanza with the migration's
    defaults. Use this to undo accidental edits."""
    if not yes:
        click.confirm(
            "reset will drop every robots_rules row and re-seed the "
            "`*` stanza with the migration defaults. Continue?",
            abort=True,
        )
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        get_broker_client().robots_reset()
    except BrokerUnavailable as exc:
        raise _robots_click_error(exc)
    click.echo("reset robots_rules to seeded defaults")
