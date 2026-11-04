"""Tests for `mimir.robots`: CRUD on robots_rules + the
`render_robots_txt` body builder."""

import re

import pytest

from mimir import robots
from mimir.extensions import SessionLocal


def _star_present():
    with SessionLocal() as session:
        return robots.get_rule(session, "*") is not None


# ----- list / get / migration seed --------------------------------------


def test_list_rules_orders_star_first_then_alphabetical():
    robots.add_rule("Zeta", disallow=["/"])
    robots.add_rule("AlphaBot", disallow=["/private/"])
    robots.add_rule("BetaBot", disallow=["/"])

    with SessionLocal() as session:
        rules = robots.list_rules(session)

    assert [r.user_agent for r in rules] == [
        "*",
        "AlphaBot",
        "BetaBot",
        "Zeta",
    ]


# ----- validators --------------------------------------------------------


@pytest.mark.parametrize("ua", ["", " ", "with space", "x" * 65, "tab\there"])
def test_validate_user_agent_rejects_bad_input(ua):
    with pytest.raises(robots.RobotsValidationError):
        robots.validate_user_agent(ua)


@pytest.mark.parametrize("path", ["", "no-slash", "/bad space", "/" + "x" * 256])
def test_validate_disallow_path_rejects_bad_input(path):
    with pytest.raises(robots.RobotsValidationError):
        robots.validate_disallow_path(path)


@pytest.mark.parametrize("path", ["/", "/private/", "*.xml", "/path/file.html"])
def test_validate_disallow_path_accepts_typical(path):
    assert robots.validate_disallow_path(path) == path


@pytest.mark.parametrize("seconds", [-1, 86_401, "5", True])
def test_validate_crawl_delay_rejects_bad_input(seconds):
    with pytest.raises(robots.RobotsValidationError):
        robots.validate_crawl_delay(seconds)


# ----- add_rule ----------------------------------------------------------


def test_add_rule_creates_row_with_supplied_fields():
    rule = robots.add_rule(
        "GPTBot",
        disallow=["/"],
        crawl_delay=10,
    )
    assert rule.user_agent == "GPTBot"
    assert rule.crawl_delay == 10
    assert rule.disallow_paths == ["/"]


def test_add_rule_rejects_duplicate():
    robots.add_rule("GPTBot", disallow=["/"])
    with pytest.raises(robots.RobotsValidationError):
        robots.add_rule("GPTBot", disallow=["/"])


def test_add_rule_with_no_disallow_no_delay_creates_no_op_row():
    """The row exists but `render_robots_txt` skips it. Operator can
    later `update` it; this is a degenerate-but-allowed state."""
    rule = robots.add_rule("EmptyBot")
    assert rule.disallow_paths is None
    assert rule.crawl_delay is None


# ----- update_rule -------------------------------------------------------


def test_update_rule_add_disallow_is_set_semantic():
    robots.update_rule("*", add_disallow=["/private/"])
    robots.update_rule("*", add_disallow=["/private/"])  # idempotent
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.disallow_paths.count("/private/") == 1


def test_update_rule_remove_disallow_drops_path():
    robots.update_rule("*", remove_disallow=["/*/attachment/"])
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.disallow_paths is None or "/*/attachment/" not in rule.disallow_paths


def test_update_rule_sets_crawl_delay():
    robots.update_rule("*", crawl_delay=30)
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.crawl_delay == 30


def test_update_rule_clear_crawl_delay_writes_null():
    robots.update_rule("*", crawl_delay=5)
    with SessionLocal() as session:
        assert robots.get_rule(session, "*").crawl_delay == 5
    robots.update_rule("*", clear_crawl_delay=True)
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.crawl_delay is None


def test_update_rule_rejects_both_set_and_clear_crawl_delay():
    with pytest.raises(robots.RobotsValidationError):
        robots.update_rule("*", crawl_delay=10, clear_crawl_delay=True)


def test_update_rule_unknown_user_agent_raises():
    with pytest.raises(robots.RobotsRuleNotFound):
        robots.update_rule("NeverAdded", add_disallow=["/"])


# ----- remove_rule -------------------------------------------------------


def test_remove_rule_drops_row():
    robots.add_rule("GPTBot", disallow=["/"])
    robots.remove_rule("GPTBot")
    with SessionLocal() as session:
        assert robots.get_rule(session, "GPTBot") is None


def test_remove_rule_refuses_star():
    with pytest.raises(robots.RobotsValidationError) as ei:
        robots.remove_rule("*")
    assert "*" in str(ei.value)
    assert _star_present()


def test_remove_rule_unknown_raises():
    with pytest.raises(robots.RobotsRuleNotFound):
        robots.remove_rule("NeverAdded")


# ----- reset_rules -------------------------------------------------------


def test_reset_rules_drops_extras_and_reseeds_star():
    robots.add_rule("GPTBot", disallow=["/"])
    robots.update_rule("*", crawl_delay=99)

    robots.reset_rules()

    with SessionLocal() as session:
        rules = robots.list_rules(session)
    assert [r.user_agent for r in rules] == ["*"]
    star = rules[0]
    assert star.crawl_delay is None
    assert star.disallow_paths == [
        "/*/attachment/",
        "/api/",
        "/*/search",
        "/m/",
        "/*/m/",
        "/*/series/*/diff$",
    ]
    assert star.content_signals == {
        "search": "yes",
        "ai-train": "no",
        "ai-input": "yes",
    }


# ----- render_robots_txt -------------------------------------------------

LLMS_URL = "https://example.com/llms.txt"


def test_render_matches_seeded_defaults():
    """Default body after migration: preamble (because content_signals
    is non-empty on the seeded `*` row), then `*` stanza with
    Content-Signal/Disallow, then Sitemap."""
    robots.reset_rules()
    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session,
            ["https://example.com/sitemap.xml"],
            LLMS_URL,
        )
    assert body == (
        robots._ROBOTS_PREAMBLE
        + "User-agent: *\n"
        + "Content-Signal: search=yes, ai-input=yes, ai-train=no\n"
        + "Disallow: /*/attachment/\n"
        + "Disallow: /api/\n"
        + "Disallow: /*/search\n"
        + "Disallow: /m/\n"
        + "Disallow: /*/m/\n"
        + "Disallow: /*/series/*/diff$\n"
        + "\n"
        + "Sitemap: https://example.com/sitemap.xml\n"
        + f"LLM-Context: {LLMS_URL}\n"
    )


def test_render_preamble_emitted_when_any_signal_present():
    """Pin the wording: the preamble must include the operator-vs-
    rightsholder framing and the EU Directive 96/9/EC reference
    (compilation right) instead of 2019/790 (TDM opt-out) since
    mimir mirrors third-party authors' work."""
    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )
    assert "Copyright in individual messages belongs to their authors" in body
    assert "EU Directive 96/9/EC" in body
    # Don't accidentally regress to the Cloudflare-verbatim wording
    # that overreaches for a mirror operator.
    assert "DIRECTIVE 2019/790" not in body
    assert "EXPRESS RESERVATIONS OF RIGHTS" not in body


def test_render_preamble_suppressed_when_no_signals_anywhere():
    """If every rule has zero content_signals, the preamble's
    glossary describes directives that don't appear in the file;
    suppress it to keep the file coherent."""
    robots.update_rule("*", clear_all_content_signals=True)

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )
    assert "Content-Signal" not in body
    assert "EU Directive" not in body
    # The stanza itself still renders.
    assert "User-agent: *" in body
    assert "Disallow: /*/attachment/" in body


def test_render_emits_per_ua_stanzas():
    robots.add_rule("GPTBot", disallow=["/"])
    robots.add_rule("ClaudeBot", disallow=["/", "/api/"], crawl_delay=20)

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )

    # `*` first, then alphabetical (ClaudeBot < GPTBot). Preamble at
    # the top because the seeded `*` row carries Content-Signal.
    expected = (
        robots._ROBOTS_PREAMBLE
        + "User-agent: *\n"
        + "Content-Signal: search=yes, ai-input=yes, ai-train=no\n"
        + "Disallow: /*/attachment/\n"
        + "Disallow: /api/\n"
        + "Disallow: /*/search\n"
        + "Disallow: /m/\n"
        + "Disallow: /*/m/\n"
        + "Disallow: /*/series/*/diff$\n"
        + "\n"
        + "User-agent: ClaudeBot\n"
        + "Crawl-delay: 20\n"
        + "Disallow: /\n"
        + "Disallow: /api/\n"
        + "\n"
        + "User-agent: GPTBot\n"
        + "Disallow: /\n"
        + "\n"
        + "Sitemap: https://example.com/sm.xml\n"
        + f"LLM-Context: {LLMS_URL}\n"
    )
    assert body == expected


def test_render_skips_degenerate_rows():
    """A row with no crawl_delay AND no disallow paths AND no
    content_signals is meaningless; rendering produces no stanza."""
    robots.add_rule("EmptyBot")  # nothing at all

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )

    assert "EmptyBot" not in body


def test_render_keeps_stanza_with_only_content_signals():
    """A row carrying ONLY content_signals (no disallow, no
    crawl_delay) is still meaningful and must render."""
    robots.add_rule("MysteryBot", content_signals={"ai-train": "no"})

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )

    assert "User-agent: MysteryBot\n" in body
    assert "Content-Signal: ai-train=no\n" in body


def test_render_with_only_degenerate_rows_still_emits_sitemap():
    """Pathological: every row is degenerate. The file should still
    carry the Sitemap directive so crawlers can discover the index."""
    robots.reset_rules()
    # Mutate the seeded `*` row into a no-op.
    # Strip whatever the seeded defaults are, not a hardcoded subset:
    # leaving one behind would keep the row non-degenerate and quietly
    # stop exercising the case this test exists for.
    robots.update_rule(
        "*",
        remove_disallow=list(robots._DEFAULT_STAR_DISALLOW),
        clear_crawl_delay=True,
        clear_all_content_signals=True,
    )

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, ["https://example.com/sm.xml"], LLMS_URL
        )

    assert body == f"Sitemap: https://example.com/sm.xml\nLLM-Context: {LLMS_URL}\n"

    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session,
            ["https://example.com/a.xml", "https://example.com/b.xml"],
            LLMS_URL,
        )
    assert body == (
        "Sitemap: https://example.com/a.xml\nSitemap: https://example.com/b.xml\n"
        f"LLM-Context: {LLMS_URL}\n"
    )


# ----- Content Signals --------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown": "yes"},
        {"search": "maybe"},
        {"": "yes"},
    ],
)
def test_validate_content_signals_rejects_bad_input(bad):
    with pytest.raises(robots.RobotsValidationError):
        robots.validate_content_signals(bad)


def test_validate_content_signals_normalises_case():
    assert robots.validate_content_signals({"Search": "YES"}) == {"search": "yes"}


def test_validate_content_signals_none_or_empty_passes_through():
    assert robots.validate_content_signals(None) is None
    assert robots.validate_content_signals({}) is None


def test_add_rule_with_content_signals():
    rule = robots.add_rule(
        "Googlebot", content_signals={"search": "yes", "ai-train": "no"}
    )
    assert rule.content_signals == {"search": "yes", "ai-train": "no"}


def test_update_rule_set_content_signal_upserts():
    robots.update_rule("*", set_content_signal={"ai-train": "yes"})
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.content_signals["ai-train"] == "yes"
    # Other seeded keys untouched.
    assert rule.content_signals["search"] == "yes"
    assert rule.content_signals["ai-input"] == "yes"


def test_update_rule_clear_content_signal_drops_specific_keys():
    robots.update_rule("*", clear_content_signal=["ai-train"])
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert "ai-train" not in rule.content_signals
    assert "search" in rule.content_signals


def test_update_rule_clear_all_content_signals_wipes_dict():
    robots.update_rule("*", clear_all_content_signals=True)
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.content_signals is None


def test_update_rule_rejects_clear_all_with_set():
    with pytest.raises(robots.RobotsValidationError):
        robots.update_rule(
            "*",
            set_content_signal={"search": "yes"},
            clear_all_content_signals=True,
        )


def test_update_rule_rejects_unknown_clear_signal_key():
    with pytest.raises(robots.RobotsValidationError):
        robots.update_rule("*", clear_content_signal=["unknown"])


def test_reset_rules_reseeds_content_signals():
    robots.update_rule("*", clear_all_content_signals=True)
    robots.reset_rules()
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule.content_signals == {
        "search": "yes",
        "ai-train": "no",
        "ai-input": "yes",
    }


# e3aa78c72a8d: backfilling the `*` stanza on EXISTING deploys.
#
# The route-level test in test_routes/test_static_meta.py proves fresh
# seeds get the new paths, but it passes via `_DEFAULT_STAR_DISALLOW`,
# not via the migration. Prod already has a seeded `*` row, and
# `render_robots_txt` reads rows rather than the constant, so the
# migration is the ONLY thing that reaches a running deploy. Untested,
# it is precisely the "ships silently inert" failure mode.


_API_SEARCH_MIGRATION = (
    "e3aa78c72a8d",
    "alembic/versions/e3aa78c72a8d_seed_robots_disallow_for_api_and_search_.py",
)
_MESSAGE_ID_MIGRATION = (
    "7484f4bf925c",
    "alembic/versions/7484f4bf925c_seed_robots_disallow_for_message_id_lookup.py",
)


def _run_migration(migration=_API_SEARCH_MIGRATION, *, db_engine=None) -> None:
    """Drive a migration's upgrade against the test DB.

    `Operations.context` installs the global `op` proxy the migration
    module reaches for, so the real function body runs rather than a
    reimplementation of it. Loading by path (not import) because
    revision filenames aren't importable module names.
    """
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from mimir.extensions import engine

    revision, path = migration
    spec = importlib.util.spec_from_file_location(f"_mig_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with (db_engine if db_engine is not None else engine).begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()


def test_fresh_robots_seeds_render_current_policy():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    fresh = create_engine("sqlite://")
    try:
        for revision, filename in [
            ("cc79e4a44a23", "add_robots_rules_table"),
            ("82e825291162", "add_robots_rules_content_signals"),
        ]:
            _run_migration(
                (revision, f"alembic/versions/{revision}_{filename}.py"),
                db_engine=fresh,
            )
        with Session(fresh) as session:
            star = robots.get_rule(session, "*")
            assert star is not None
            assert star.disallow_paths == ["/*/attachment/"]
            body = robots.render_robots_txt(session, [], LLMS_URL)
        stanza = body.split("User-agent: *\n", 1)[1].split("\n\n", 1)[0].splitlines()
        assert stanza == [
            "Content-Signal: search=yes, ai-input=yes, ai-train=no",
            "Disallow: /*/attachment/",
        ]
    finally:
        fresh.dispose()


def _star_paths() -> list[str]:
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import RobotsRule

    with SessionLocal() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "*")
        ).scalar_one()
        return list(row.disallow_paths or [])


def _set_star_paths(paths: list[str]) -> None:
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import RobotsRule

    with SessionLocal() as s:
        s.execute(
            update(RobotsRule)
            .where(RobotsRule.user_agent == "*")
            .values(disallow_paths=paths)
        )
        s.commit()


def test_migration_appends_new_disallow_paths_to_existing_star_row():
    """The pre-migration prod shape: a `*` row carrying only the
    attachment rule. Upgrade must append, not replace."""
    _set_star_paths(["/*/attachment/"])
    _run_migration()
    assert _star_paths() == ["/*/attachment/", "/api/", "/*/search"]


def test_migration_appends_only_missing_paths_and_preserves_operator_edits():
    """An operator who already added `/api/` by hand must not end up
    with a duplicate, and their own entries must survive: the migration
    appends what's missing rather than rewriting the list (the same
    don't-clobber guard as the 82e825291162 content_signals backfill).
    """
    _set_star_paths(["/*/attachment/", "/api/", "/operator-private/"])
    _run_migration()
    assert _star_paths() == [
        "/*/attachment/",
        "/api/",
        "/operator-private/",
        "/*/search",
    ]


def test_migration_backfills_a_row_whose_paths_were_all_removed():
    """`update_rule` stores `paths or None` when the last disallow path
    goes, and SQLAlchemy's JSON type writes Python None as the *string*
    `'null'`, which decodes back to None. That must not be read as "no
    `*` row at all": conflating them silently no-ops the backfill on
    precisely the deploys that customised their robots.txt, which is
    the failure this migration exists to prevent. Note `[]` and `null`
    are two encodings of the same operator intent, so both must
    backfill.
    """
    robots.update_rule("*", remove_disallow=list(robots._DEFAULT_STAR_DISALLOW))
    _run_migration()
    assert _star_paths() == ["/api/", "/*/search"]


# The `/m/` message-id lookup paths (#984): same shape of backfill.


def test_message_id_migration_appends_to_a_pre_984_row():
    _set_star_paths(["/*/attachment/", "/api/", "/*/search"])
    _run_migration(_MESSAGE_ID_MIGRATION)
    assert _star_paths() == ["/*/attachment/", "/api/", "/*/search", "/m/", "/*/m/"]


def test_message_id_migration_is_a_no_op_when_the_paths_are_there():
    before = ["/*/attachment/", "/m/", "/api/", "/*/m/", "/*/search"]
    _set_star_paths(before)
    _run_migration(_MESSAGE_ID_MIGRATION)
    assert _star_paths() == before


def test_message_id_migration_appends_only_what_is_missing_and_keeps_edits():
    _set_star_paths(["/*/attachment/", "/m/", "/operator-private/"])
    _run_migration(_MESSAGE_ID_MIGRATION)
    assert _star_paths() == ["/*/attachment/", "/m/", "/operator-private/", "/*/m/"]


def test_message_id_migration_backfills_a_row_whose_paths_were_all_removed():
    robots.update_rule("*", remove_disallow=list(robots._DEFAULT_STAR_DISALLOW))
    _run_migration(_MESSAGE_ID_MIGRATION)
    assert _star_paths() == ["/m/", "/*/m/"]


def test_message_id_migration_leaves_a_missing_star_row_alone():
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import RobotsRule

    with SessionLocal() as s:
        s.execute(delete(RobotsRule))
        s.commit()
    _run_migration(_MESSAGE_ID_MIGRATION)
    with SessionLocal() as s:
        assert robots.get_rule(s, "*") is None


def test_default_disallow_carries_both_lookup_paths():
    assert {"/m/", "/*/m/"} <= set(robots._DEFAULT_STAR_DISALLOW)


def _blocked(pattern: str, path: str) -> bool:
    """robots.txt matching as crawlers read it: a prefix match, `*` is any
    run of characters, `$` anchors the end."""
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\$", "$")
    return re.match(regex, path) is not None


# One sample of every shape the sitemaps and nav emit for a reader.
_EMITTED = [
    "/",
    "/lkml/",
    "/lkml/today",
    "/lkml/2026/",
    "/lkml/2026/10/",
    "/lkml/2026/10/123",
    "/lkml/2026/10/123/t",
    "/lkml/2026/10/123/t/2",
    "/lkml/subsystem/",
    "/lkml/subsystem/kernel-foo/",
    "/lkml/reviewer/someone@kernel.org",
    "/lkml/author/someone",
    "/lkml/series/abc/diff?from=v1&to=v2&pos=cover",
    "/lkml/series/abc/diff?from=v1&to=v2&pos=cover&s=123",
    "/maintainers/someone@kernel.org",
    "/merge-window",
    "/privacy",
]


def test_default_disallow_blocks_the_lookup_and_nothing_a_reader_is_sent_to():
    """Emitter equals acceptor: a pattern that also matched a page we link
    to would hide it from crawlers without any error."""
    patterns = robots._DEFAULT_STAR_DISALLOW
    assert _blocked("/m/", "/m/a@b")
    assert _blocked("/*/m/", "/lkml/m/a@b")
    blocked = [(p, path) for p in patterns for path in _EMITTED if _blocked(p, path)]
    assert blocked == []


def test_default_blocks_bare_series_diff_but_not_complete_comparisons():
    pattern = "/*/series/*/diff$"
    assert pattern in robots._DEFAULT_STAR_DISALLOW
    assert _blocked(pattern, "/alpha/series/key/diff")
    assert not _blocked(pattern, "/alpha/series/key/diff?from=v1&to=v2&pos=cover")


_SERIES_DIFF_MIGRATION = (
    "b2cf46f509a8",
    "alembic/versions/b2cf46f509a8_seed_robots_disallow_for_bare_series_diffs.py",
)


@pytest.mark.parametrize(
    "stored",
    [None, "null", "[]", '["/operator/"]', '["/*/series/*/diff$","/operator/"]'],
)
def test_bare_series_diff_migration_preserves_custom_rules_and_is_idempotent(stored):
    import json

    from sqlalchemy import text

    from mimir.extensions import engine

    robots.update_rule("*", crawl_delay=19, set_content_signal={"ai-input": "no"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE robots_rules SET disallow_paths=:paths WHERE user_agent='*'"),
            {"paths": stored},
        )
    before = json.loads(stored) if stored is not None else []
    before = before or []
    expected = (
        before if "/*/series/*/diff$" in before else [*before, "/*/series/*/diff$"]
    )
    for _ in range(2):
        _run_migration(_SERIES_DIFF_MIGRATION)
        with SessionLocal() as session:
            star = robots.get_rule(session, "*")
            assert star.disallow_paths == expected
            assert star.crawl_delay == 19
            assert star.content_signals == {
                "search": "yes",
                "ai-input": "no",
                "ai-train": "no",
            }


def test_bare_series_diff_migration_does_not_create_missing_star():
    from sqlalchemy import delete

    from mimir.models import RobotsRule

    with SessionLocal() as session:
        session.execute(delete(RobotsRule))
        session.commit()
    _run_migration(_SERIES_DIFF_MIGRATION)
    assert not _star_present()
