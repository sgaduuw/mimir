"""The crawler budget gate (#761): a `before_app_request` hook that
answers 429 + Retry-After to a configured crawler over its budget.

Time is injected through the gate's clock, never slept."""

import json
import logging
import os
from unittest.mock import patch

import pytest

from mimir import create_app
from mimir.config import DEFAULT_CRAWLER_UA_BUDGETS, Settings
from mimir.web import crawler_budget, hooks
from mimir.web.crawler_budget import EXEMPT_ENDPOINTS, OVERFLOW_KEY, CrawlerBudget

APPLEBOT = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15 (Applebot/0.1)"
BINGBOT = "Mozilla/5.0 (compatible; bingbot/2.0)"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GPTBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.0; +https://openai.com/gptbot"
AHREFS = "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)"
# A device called CUBOT, no `bot/` product token: must never be gated.
CUBOT_BROWSER = "Mozilla/5.0 (Linux; Android 13; CUBOT X30) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
BROWSER = "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/130.0"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _gate(clock, *, total=100, uas=None, workers=1, retry=120, **kw):
    return CrawlerBudget(
        total_per_minute=total,
        ua_percent={"Applebot": 1} if uas is None else uas,
        web_workers=workers,
        retry_after_sec=retry,
        clock=clock,
        **kw,
    )


def _install(monkeypatch, gate):
    monkeypatch.setattr(hooks, "_crawler_gate", gate)


def _get(client, path, ua):
    return client.get(path, headers={"User-Agent": ua})


def test_over_per_ua_budget_gets_429_then_passes_after_refill(client, monkeypatch):
    clock = Clock()
    # 1% of 1200 = 12 a minute, a 10 s bucket holds 2 tokens
    _install(monkeypatch, _gate(clock, total=1200))
    assert _get(client, "/", APPLEBOT).status_code == 200
    assert _get(client, "/", APPLEBOT).status_code == 200
    r = _get(client, "/", APPLEBOT)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "120"
    assert r.headers["Cache-Control"] == "no-store"
    # 12 a minute refills one token in 5 s.
    clock.now += 5
    assert _get(client, "/", APPLEBOT).status_code == 200


def test_429_keeps_security_headers_and_request_id(client, monkeypatch):
    _install(monkeypatch, _gate(Clock()))
    _get(client, "/", APPLEBOT)
    r = _get(client, "/", APPLEBOT)
    assert r.status_code == 429
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Request-Id"] != "-"


def test_global_empty_refuses_without_charging_per_ua_bucket(monkeypatch):
    clock = Clock()
    gate = _gate(clock, total=1, uas={"Applebot": 10, "bingbot": 100})
    assert gate.check(BINGBOT) is None  # drains the global bucket
    before = gate.ua_tokens("Applebot")
    assert gate.check(APPLEBOT) == "Applebot"
    assert gate.ua_tokens("Applebot") == before


def test_ua_empty_refuses_without_charging_global_bucket():
    gate = _gate(Clock(), total=10, uas={"Applebot": 10})
    assert gate.check(APPLEBOT) is None
    before = gate.global_tokens()
    assert gate.check(APPLEBOT) == "Applebot"
    assert gate.global_tokens() == before


def test_crawlers_share_the_global_budget():
    gate = _gate(Clock(), total=12, uas={"Applebot": 100, "bingbot": 100})
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) is None
    # bingbot is within its own budget but the shared one is gone.
    assert gate.check(BINGBOT) == "bingbot"


def test_browser_never_gated_with_buckets_empty(client, monkeypatch):
    _install(monkeypatch, _gate(Clock(), total=1, uas={"Applebot": 100}))
    _get(client, "/", APPLEBOT)
    assert _get(client, "/", APPLEBOT).status_code == 429
    assert _get(client, "/", BROWSER).status_code == 200
    assert client.get("/").status_code == 200  # no User-Agent override


@pytest.mark.parametrize(
    "path",
    [
        "/robots.txt",
        "/sitemap.xml",
        "/meta-sitemap.xml",
        "/sitemap-maintainers.xml",
        "/healthz",
        "/readyz",
        "/static/css/mimir.css",
    ],
)
def test_exempt_endpoints_pass_with_buckets_empty(client, monkeypatch, path):
    gate = _gate(Clock(), total=1, uas={"Applebot": 100})
    _install(monkeypatch, gate)
    _get(client, "/", APPLEBOT)
    assert _get(client, "/", APPLEBOT).status_code == 429
    assert _get(client, path, APPLEBOT).status_code != 429


def test_exempt_names_all_exist_in_url_map():
    endpoints = {rule.endpoint for rule in create_app().url_map.iter_rules()}
    assert EXEMPT_ENDPOINTS <= endpoints, EXEMPT_ENDPOINTS - endpoints


def test_defaults_gate_nothing(client):
    # The real, unpatched settings: every budget is 0.
    for _ in range(5):
        assert _get(client, "/", APPLEBOT).status_code == 200


def test_global_zero_is_the_master_switch():
    gate = _gate(Clock(), total=0, uas={"Applebot": 10})
    assert all(gate.check(APPLEBOT) is None for _ in range(1000))


def test_429_path_opens_no_db_session_and_reads_no_blob(client, monkeypatch):
    _install(monkeypatch, _gate(Clock()))
    _get(client, "/", APPLEBOT)
    boom = RuntimeError("the gate must not touch the database or the store")
    with (
        patch("mimir.extensions.SessionLocal", side_effect=boom),
        patch("mimir.web.routes.health.SessionLocal", side_effect=boom),
        patch("mimir.store.read_message", side_effect=boom),
        patch("mimir.store.read_messages", side_effect=boom),
    ):
        r = _get(client, "/", APPLEBOT)
    assert r.status_code == 429


def test_web_workers_divides_the_per_worker_rate():
    # Global 120, 8 workers, Applebot 10%: 12 a minute overall, 1.5 a
    # minute per worker (global bucket 15 a minute per worker).
    gate = _gate(Clock(), total=120, uas={"Applebot": 10}, workers=8)
    assert gate.ua_per_worker_per_minute("Applebot") == pytest.approx(1.5)
    assert gate.global_tokens() == pytest.approx(15 * crawler_budget.BURST_SECONDS / 60)


def test_sub_one_per_minute_share_admits_one_then_every_60_over_rate_seconds():
    # 60 * 10% / 8 workers = 0.75 a minute per worker.
    clock = Clock()
    gate = _gate(clock, total=60, uas={"Applebot": 10}, workers=8)
    assert gate.ua_per_worker_per_minute("Applebot") == pytest.approx(0.75)
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) == "Applebot"
    clock.now += 80  # 60 / 0.75
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) == "Applebot"


def test_web_workers_reads_the_workers_env_var(monkeypatch):
    monkeypatch.setenv("WORKERS", "8")
    assert Settings().web_workers == 8
    monkeypatch.delenv("WORKERS")
    assert Settings().web_workers == 1


def test_crawler_ua_budgets_env_json(monkeypatch):
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", '{"Applebot": 10, "bingbot": 30}')
    assert Settings().crawler_ua_budgets == {"Applebot": 10, "bingbot": 30}


@pytest.mark.parametrize(
    "name, value",
    [
        ("CRAWLER_BUDGET_PER_MINUTE", "-1"),
        ("CRAWLER_UA_BUDGETS", '{"Applebot": -1}'),
        ("CRAWLER_UA_BUDGETS", '{"Applebot": 101}'),
        ("CRAWLER_RETRY_AFTER_SEC", "0"),
        ("WORKERS", "0"),
    ],
)
def test_invalid_values_are_refused_at_load(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings()


def test_deferred_log_line_reaches_the_root_logger_at_warning(client, monkeypatch):
    clock = Clock()
    _install(monkeypatch, _gate(clock))
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record)

    handler = Collect(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        _get(client, "/", APPLEBOT)
        _get(client, "/", APPLEBOT)  # deferred, first line logs at once
        _get(client, "/", APPLEBOT)  # deferred, within the minute: silent
        _get(client, "/", APPLEBOT)
        clock.now += 61
        _get(client, "/", APPLEBOT)  # admitted (refilled)
        _get(client, "/", APPLEBOT)  # deferred again: second line, count 1
    finally:
        root.removeHandler(handler)
    lines = [r for r in seen if r.name == "mimir.web.crawler_budget"]
    assert [r.levelno for r in lines] == [logging.WARNING, logging.WARNING]
    first, second = (r.getMessage() for r in lines)
    assert "Applebot" in first and "deferred=1" in first
    assert f"pid={os.getpid()}" in first
    assert "deferred=3" in second


def test_log_logger_is_not_cut_off_from_the_root():
    logger = logging.getLogger("mimir.web.crawler_budget")
    assert logger.propagate is True
    assert logger.getEffectiveLevel() <= logging.WARNING


def test_ua_match_is_a_case_insensitive_substring():
    gate = _gate(Clock())
    assert gate.check("something APPLEBOT/0.1") is None
    assert gate.check("something applebot/0.1") == "Applebot"


@pytest.mark.parametrize(
    "ua, key",
    [
        (GOOGLEBOT, "googlebot"),
        (BINGBOT, "bingbot"),
        (GPTBOT, "gptbot"),
        (AHREFS, "ahrefsbot"),
        ("curl/8.5.0", "curl"),
        ("PycURL/7.45.2", "pycurl"),
        ("Wget/1.21.4", "wget"),
        ("python-requests/2.31.0", "python-requests"),
        ("bot/1.0", "bot"),  # a token that IS the marker counts, on purpose
        (APPLEBOT, "Applebot"),  # listed: the dict key, not the token
        (CUBOT_BROWSER, None),
        (BROWSER, None),
        ("Lynx/2.9.0 libwww-FM/2.14", None),
        ("", None),
        (None, None),
    ],
)
def test_crawler_key_classification(ua, key):
    assert _gate(Clock()).crawler_key(ua) == key


def test_markers_are_configurable_and_escaped():
    gate = CrawlerBudget(100, {}, 1, 120, Clock(), markers=("scraper", "c++"))
    assert gate.crawler_key("FooScraper/1.0") == "fooscraper"
    assert gate.crawler_key("Mozilla (Googlebot/2.1)") is None


def test_listed_crawler_wins_over_marker_token():
    gate = _gate(Clock(), uas={"bingbot": 50})
    assert gate.crawler_key(BINGBOT) == "bingbot"
    assert gate.crawler_key("x Googlebot/2.1 bingbot/2.0") == "bingbot"


def test_unlisted_crawler_gets_the_default_percentage_listed_gets_its_own():
    gate = _gate(Clock(), total=120, uas={"Applebot": 25}, workers=8)
    assert gate.check(GOOGLEBOT) is None
    assert gate.check(APPLEBOT) is None
    # default 10% of 120 = 12 a minute overall = 1.5 per worker
    assert gate.ua_per_worker_per_minute("googlebot") == pytest.approx(1.5)
    assert gate.ua_per_worker_per_minute("Applebot") == pytest.approx(3.75)


def test_unlisted_crawler_over_default_share_gets_429(client, monkeypatch):
    _install(monkeypatch, _gate(Clock(), total=10, uas={"Applebot": 50}))
    assert _get(client, "/", GOOGLEBOT).status_code == 200
    r = _get(client, "/", GOOGLEBOT)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "120"


def test_unlisted_crawler_needs_a_global_token_and_refusal_charges_neither():
    gate = _gate(Clock(), total=6, uas={"Applebot": 50})  # global holds 1 token
    assert gate.check(BINGBOT) is None
    assert gate.global_tokens() < 1
    before = gate.global_tokens()
    assert gate.check(GPTBOT) == "gptbot"  # its own bucket is full
    assert gate.ua_tokens("gptbot") == gate._find("gptbot").capacity
    assert gate.global_tokens() == before


def test_cubot_browser_never_gated_with_buckets_empty(client, monkeypatch):
    _install(monkeypatch, _gate(Clock(), total=1, uas={"Applebot": 100}))
    _get(client, "/", GOOGLEBOT)
    assert _get(client, "/", GOOGLEBOT).status_code == 429
    assert _get(client, "/", CUBOT_BROWSER).status_code == 200


def test_unlisted_buckets_are_capped_and_overflow_shares_one(monkeypatch):
    monkeypatch.setattr(crawler_budget, "MAX_UNLISTED_BUCKETS", 2)
    gate = _gate(Clock(), total=1000, uas={"Applebot": 100})
    assert gate.check(GOOGLEBOT) is None
    assert gate.check(BINGBOT) is None
    assert gate.unlisted_count() == 2
    # Past the cap, new crawlers share ONE bucket at the default share:
    # 10% of 1000 = 100 a minute, a 10 s bucket holds 16.67 tokens.
    results = [gate.check(f"x{i}bot/1.0 ") for i in range(17)]
    assert gate.unlisted_count() == 2
    assert results[:16] == [None] * 16
    assert results[16] == OVERFLOW_KEY


def test_idle_full_unlisted_bucket_is_dropped_after_ten_minutes(monkeypatch):
    monkeypatch.setattr(crawler_budget, "MAX_UNLISTED_BUCKETS", 2)
    clock = Clock()
    gate = _gate(clock, total=1000, uas={"Applebot": 100})
    gate.check(GOOGLEBOT)
    gate.check(BINGBOT)
    clock.now += 599
    gate.check(GPTBOT)  # too early: both still held, so it overflows
    assert gate.unlisted_count() == 2
    clock.now += 62
    gate.check(GPTBOT)  # sweep drops the two idle, full buckets
    assert gate.unlisted_count() == 1
    assert gate.ua_tokens("gptbot") < 100


def test_idle_bucket_that_has_not_refilled_is_kept():
    clock = Clock()
    # 1 * 1% = 0.01 a minute: a drained bucket needs 6000 s to refill.
    gate = CrawlerBudget(1, {}, 1, 120, clock, default_ua_pct=1)
    gate.check(GOOGLEBOT)
    clock.now += 700
    gate.check(BROWSER)  # sweeping runs on any check
    assert gate.unlisted_count() == 1
    clock.now += 6000
    gate.check(BROWSER)
    assert gate.unlisted_count() == 0


def test_log_line_names_the_unlisted_bucket_key(client, monkeypatch):
    _install(monkeypatch, _gate(Clock(), total=10, uas={"Applebot": 50}))
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    logging.getLogger().addHandler(handler)
    try:
        _get(client, "/", GOOGLEBOT)
        _get(client, "/", GOOGLEBOT)
    finally:
        logging.getLogger().removeHandler(handler)
    assert any("crawler=googlebot" in m for m in seen)


@pytest.mark.parametrize("value", ["0", "101"])
def test_default_ua_pct_is_a_validated_percentage(monkeypatch, value):
    monkeypatch.setenv("CRAWLER_DEFAULT_UA_PCT", value)
    with pytest.raises(ValueError):
        Settings()


def test_marker_and_default_pct_settings(monkeypatch):
    assert Settings().crawler_default_ua_pct == 10
    assert Settings().crawler_ua_markers == [
        "bot",
        "crawler",
        "spider",
        "curl",
        "wget",
        "python-requests",
    ]
    monkeypatch.setenv("CRAWLER_UA_MARKERS", '["bot", "scraper"]')
    assert Settings().crawler_ua_markers == ["bot", "scraper"]


def test_first_listed_crawler_in_dict_order_wins():
    both = "Applebot/0.1 bingbot/2.0"
    assert (
        _gate(Clock(), uas={"Applebot": 10, "bingbot": 5}).crawler_key(both)
        == "Applebot"
    )
    assert (
        _gate(Clock(), uas={"bingbot": 5, "Applebot": 10}).crawler_key(both)
        == "bingbot"
    )


@pytest.mark.parametrize("key", ["", " "])
def test_blank_crawler_key_is_refused_at_load(monkeypatch, key):
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps({key: 10}))
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize("marker", ["", " "])
def test_blank_marker_is_refused_at_load(monkeypatch, marker):
    monkeypatch.setenv("CRAWLER_UA_MARKERS", json.dumps(["bot", marker]))
    with pytest.raises(ValueError):
        Settings()


def test_nested_env_form_lowercases_the_key(monkeypatch):
    monkeypatch.setenv("CRAWLER_UA_BUDGETS__Applebot", "10")
    assert Settings().crawler_ua_budgets == {"applebot": 10}


def _admitted_in_window(gate, clock, seconds, step=0.25):
    n = 0
    end = clock.now + seconds
    while clock.now < end:
        while gate.check(APPLEBOT) is None:
            n += 1
        clock.now += step
    return n


def _burst_gate(clock):
    return _gate(clock, total=120, uas={"Applebot": 100})


# Budget 120 a minute is 2 a second, so ten seconds' worth is 20 tokens.
_WINDOW_BOUND = 120 + 10 * 2


def test_first_minute_admits_at_most_rate_plus_ten_seconds():
    clock = Clock()
    assert _admitted_in_window(_burst_gate(clock), clock, 60) <= _WINDOW_BOUND


def test_minute_after_an_idle_minute_admits_at_most_rate_plus_ten_seconds():
    clock = Clock()
    gate = _burst_gate(clock)
    _admitted_in_window(gate, clock, 120)  # steady state
    clock.now += 60  # crawler idle for a minute
    assert _admitted_in_window(gate, clock, 60) <= _WINDOW_BOUND


def test_markers_are_literal_not_regex():
    gate = CrawlerBudget(100, {}, 1, 120, Clock(), markers=("a.b",))
    assert gate.crawler_key("axb/1.0") is None
    assert gate.crawler_key("a.b/1.0") == "a.b"


def test_invented_crawler_names_share_the_unlisted_pool():
    # Global 120, pool 30%: 36 a minute, a 10 s bucket holds 6 tokens.
    gate = _gate(Clock(), total=120, uas={"Applebot": 10}, unlisted_pool_pct=30)
    admitted = sum(gate.check(f"Mozilla (x{i}-bot/1.0)") is None for i in range(50))
    assert admitted == 6
    # A listed crawler never touches the pool and still gets its share
    # (10% of 120 = 12 a minute, 2 tokens) during the flood.
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) == "Applebot"


def test_pool_refusal_charges_no_bucket():
    gate = _gate(Clock(), total=120, uas={"Applebot": 10}, unlisted_pool_pct=1)
    assert gate.check(GOOGLEBOT) is None  # pool of 1 token is now empty
    before = (gate.global_tokens(), gate.ua_tokens("googlebot"))
    assert gate.check(BINGBOT) == "bingbot"
    assert (gate.global_tokens(), gate.ua_tokens("googlebot")) == before
    assert gate.ua_tokens("bingbot") == gate._find("bingbot").capacity


def test_listed_crawlers_do_not_draw_on_the_pool():
    gate = _gate(Clock(), total=120, uas={"Applebot": 100}, unlisted_pool_pct=1)
    pool_before = gate.pool_tokens()
    gate.check(APPLEBOT)
    assert gate.pool_tokens() == pool_before


@pytest.mark.parametrize("value", ["0", "101"])
def test_unlisted_pool_pct_is_a_validated_percentage(monkeypatch, value):
    monkeypatch.setenv("CRAWLER_UNLISTED_POOL_PCT", value)
    with pytest.raises(ValueError):
        Settings()


def test_unlisted_pool_default_is_30():
    assert Settings().crawler_unlisted_pool_pct == 30


@pytest.mark.parametrize("key", ["bot", "BOT", "Spider", "curl"])
def test_listed_key_equal_to_a_marker_is_refused(monkeypatch, key):
    # A listed key is a plain substring with no slash rule, so "bot"
    # would gate the CUBOT browser.
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps({key: 10}))
    with pytest.raises(ValueError):
        Settings()


def test_listed_key_checked_against_configured_markers(monkeypatch):
    monkeypatch.setenv("CRAWLER_UA_MARKERS", '["scraper"]')
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps({"Scraper": 10}))
    with pytest.raises(ValueError):
        Settings()


DEFAULT_LISTED = {
    "Bytespider": "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)",
    "PetalBot": "Mozilla/5.0 (compatible;PetalBot;+https://webmaster.petalsearch.com/site/petalbot)",
    "meta-externalagent": "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)",
    "facebookexternalhit": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Slurp": "Mozilla/5.0 (compatible; Yahoo! Slurp; http://help.yahoo.com/help/us/ysearch/slurp)",
    "msnbot": "msnbot-media/1.1 (+http://search.msn.com/msnbot.htm)",
    "Baiduspider": "Baiduspider+(+http://www.baidu.com/search/spider.htm)",
    "Applebot": APPLEBOT,
}
HUMAN_UAS = [
    BROWSER,
    CUBOT_BROWSER,
    "Mozilla/5.0 (Linux; Android 10; CUBOT_X19 Build/QP1A.190711.020) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBDV/iPhone14,2;FBMD/iPhone;FBSN/iOS;FBSV/17.0]",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Instagram 300.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Thunderbird/115.10.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
    "Lynx/2.9.0dev.12 libwww-FM/2.14 SSL-MM/1.4.1 GNUTLS/3.7.9",
]


def _default_gate():
    return CrawlerBudget(120, DEFAULT_CRAWLER_UA_BUDGETS, 1, 120)


@pytest.mark.parametrize("key, ua", DEFAULT_LISTED.items())
def test_default_listed_crawlers_are_classified_by_key(key, ua):
    assert _default_gate().crawler_key(ua) == key


def test_default_list_has_the_ten_percent_crawlers():
    assert DEFAULT_CRAWLER_UA_BUDGETS == dict.fromkeys(DEFAULT_LISTED, 10)
    assert "ChatGPT-User" not in DEFAULT_CRAWLER_UA_BUDGETS


@pytest.mark.parametrize("ua", HUMAN_UAS)
def test_default_list_never_matches_a_browser(ua):
    assert _default_gate().crawler_key(ua) is None


def _effective(monkeypatch, budgets=None):
    if budgets is not None:
        monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps(budgets))
    return Settings().effective_crawler_ua_budgets


def test_setting_defaults_to_empty_and_defaults_apply(monkeypatch):
    assert Settings().crawler_ua_budgets == {}
    assert _effective(monkeypatch) == DEFAULT_CRAWLER_UA_BUDGETS


def test_override_changes_one_entry_and_keeps_the_rest(monkeypatch):
    merged = _effective(monkeypatch, {"Applebot": 50})
    assert merged["Applebot"] == 50
    assert {k: v for k, v in merged.items() if k != "Applebot"} == {
        k: v for k, v in DEFAULT_CRAWLER_UA_BUDGETS.items() if k != "Applebot"
    }
    # An overridden default keeps its position.
    assert list(merged) == list(DEFAULT_CRAWLER_UA_BUDGETS)


def test_new_key_is_added_after_the_defaults(monkeypatch):
    merged = _effective(monkeypatch, {"Applebot": 50, "ChatGPT-User": 5})
    assert list(merged) == [*DEFAULT_CRAWLER_UA_BUDGETS, "ChatGPT-User"]
    assert merged["ChatGPT-User"] == 5


def test_zero_drops_a_default_entry(monkeypatch):
    merged = _effective(monkeypatch, {"Bytespider": 0})
    assert "Bytespider" not in merged
    assert len(merged) == len(DEFAULT_CRAWLER_UA_BUDGETS) - 1
    monkeypatch.setenv("CRAWLER_BUDGET_PER_MINUTE", "120")
    gate = CrawlerBudget.from_settings(Settings())
    # No slash token in the Bytespider UA, so it is no longer classified.
    assert gate.crawler_key(DEFAULT_LISTED["Bytespider"]) is None
    assert gate.crawler_key(DEFAULT_LISTED["PetalBot"]) == "PetalBot"


def test_marker_check_runs_on_the_merged_map(monkeypatch):
    # Clashes with a configured marker via an override...
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps({"spider": 10}))
    with pytest.raises(ValueError):
        Settings()
    # ...and via a default when a marker is configured to equal one.
    monkeypatch.delenv("CRAWLER_UA_BUDGETS")
    monkeypatch.setenv("CRAWLER_UA_MARKERS", '["msnbot"]')
    with pytest.raises(ValueError):
        Settings()


def test_from_settings_uses_the_merged_map(monkeypatch):
    monkeypatch.setenv("CRAWLER_BUDGET_PER_MINUTE", "120")
    monkeypatch.setenv("CRAWLER_UA_BUDGETS", json.dumps({"Applebot": 50}))
    gate = CrawlerBudget.from_settings(Settings())
    assert gate.crawler_key(DEFAULT_LISTED["Slurp"]) == "Slurp"
    assert gate.crawler_key(APPLEBOT) == "Applebot"
    gate.check(APPLEBOT)
    assert gate.ua_per_worker_per_minute("Applebot") == pytest.approx(60)


def test_nested_env_form_overrides_a_default_despite_lowercasing(monkeypatch):
    monkeypatch.setenv("CRAWLER_UA_BUDGETS__Applebot", "50")
    merged = Settings().effective_crawler_ua_budgets
    assert merged["Applebot"] == 50
    assert "applebot" not in merged
    assert list(merged) == list(DEFAULT_CRAWLER_UA_BUDGETS)
