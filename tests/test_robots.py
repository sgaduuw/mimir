"""Tests for `mimir.robots`: CRUD on robots_rules + the
`render_robots_txt` body builder."""

import pytest

from mimir import robots
from mimir.extensions import SessionLocal


def _star_present():
    with SessionLocal() as session:
        return robots.get_rule(session, "*") is not None


# ----- list / get / migration seed --------------------------------------


def test_migration_seeds_star_row():
    """Per the migration, `*` exists with the previous hardcoded
    template's values immediately after `alembic upgrade head`. The
    autouse `_reset_db` re-seeds via `reset_rules()` so this remains
    the per-test baseline."""
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
    assert rule is not None
    assert rule.crawl_delay == 5
    assert rule.disallow_paths == ["/*/attachment/"]


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
    assert star.crawl_delay == 5
    assert star.disallow_paths == ["/*/attachment/"]
    assert star.content_signals == {
        "search": "yes",
        "ai-train": "no",
        "ai-input": "no",
    }


# ----- render_robots_txt -------------------------------------------------


def test_render_matches_seeded_defaults():
    """Default body after migration: preamble (because content_signals
    is non-empty on the seeded `*` row), then `*` stanza with
    Content-Signal/Crawl-delay/Disallow, then Sitemap."""
    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session,
            "https://example.com/sitemap.xml",
        )
    assert body == (
        robots._ROBOTS_PREAMBLE
        + "User-agent: *\n"
        + "Content-Signal: search=yes, ai-input=no, ai-train=no\n"
        + "Crawl-delay: 5\n"
        + "Disallow: /*/attachment/\n"
        + "\n"
        + "Sitemap: https://example.com/sitemap.xml\n"
    )


def test_render_preamble_emitted_when_any_signal_present():
    """Pin the wording: the preamble must include the operator-vs-
    rightsholder framing and the EU Directive 96/9/EC reference
    (compilation right) instead of 2019/790 (TDM opt-out) since
    mimir mirrors third-party authors' work."""
    with SessionLocal() as session:
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")
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
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")
    assert "Content-Signal" not in body
    assert "EU Directive" not in body
    # The stanza itself still renders.
    assert "User-agent: *" in body
    assert "Disallow: /*/attachment/" in body


def test_render_emits_per_ua_stanzas():
    robots.add_rule("GPTBot", disallow=["/"])
    robots.add_rule("ClaudeBot", disallow=["/", "/api/"], crawl_delay=20)

    with SessionLocal() as session:
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")

    # `*` first, then alphabetical (ClaudeBot < GPTBot). Preamble at
    # the top because the seeded `*` row carries Content-Signal.
    expected = (
        robots._ROBOTS_PREAMBLE
        + "User-agent: *\n"
        + "Content-Signal: search=yes, ai-input=no, ai-train=no\n"
        + "Crawl-delay: 5\n"
        + "Disallow: /*/attachment/\n"
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
    )
    assert body == expected


def test_render_skips_degenerate_rows():
    """A row with no crawl_delay AND no disallow paths AND no
    content_signals is meaningless; rendering produces no stanza."""
    robots.add_rule("EmptyBot")  # nothing at all

    with SessionLocal() as session:
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")

    assert "EmptyBot" not in body


def test_render_keeps_stanza_with_only_content_signals():
    """A row carrying ONLY content_signals (no disallow, no
    crawl_delay) is still meaningful and must render."""
    robots.add_rule("MysteryBot", content_signals={"ai-train": "no"})

    with SessionLocal() as session:
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")

    assert "User-agent: MysteryBot\n" in body
    assert "Content-Signal: ai-train=no\n" in body


def test_render_with_only_degenerate_rows_still_emits_sitemap():
    """Pathological: every row is degenerate. The file should still
    carry the Sitemap directive so crawlers can discover the index."""
    robots.reset_rules()
    # Mutate the seeded `*` row into a no-op.
    robots.update_rule(
        "*",
        remove_disallow=["/*/attachment/"],
        clear_crawl_delay=True,
        clear_all_content_signals=True,
    )

    with SessionLocal() as session:
        body = robots.render_robots_txt(session, "https://example.com/sm.xml")

    assert body == "Sitemap: https://example.com/sm.xml\n"


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
    assert rule.content_signals["ai-input"] == "no"


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
        "ai-input": "no",
    }
