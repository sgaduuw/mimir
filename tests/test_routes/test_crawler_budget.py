"""The crawler budget gate (#761): a `before_app_request` hook that
answers 429 + Retry-After to a configured crawler over its budget.

Time is injected through the gate's clock, never slept."""

import concurrent.futures
import json
import logging
import multiprocessing
import os
import re
import time
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


@pytest.fixture(autouse=True)
def _shared_files_in_tmp(tmp_path, monkeypatch):
    # from_settings names its file after the parent pid, which in a test
    # run is the shell's: keep each test's file to itself.
    monkeypatch.setattr(crawler_budget, "shared_dir", lambda: str(tmp_path))


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


def test_every_sitemap_endpoint_is_exempt_and_edge_cached():
    """The check above catches a misspelled name, not a missing one: a new
    sitemap route left out of both lists is budgeted like a page and sent
    with no Cache-Control, and nothing failed (#846)."""
    endpoints = {rule.endpoint for rule in create_app().url_map.iter_rules()}
    sitemaps = {e for e in endpoints if "sitemap" in e}
    assert len(sitemaps) >= 6, sitemaps  # precondition: the predicate finds them
    assert sitemaps <= EXEMPT_ENDPOINTS, sitemaps - EXEMPT_ENDPOINTS
    assert sitemaps <= hooks._CACHE_CONTROL_BY_ENDPOINT.keys()


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


def test_shared_buckets_hold_the_whole_share_unlisted_divide_by_workers():
    # Global 120, 8 workers, Applebot 10%: the shared buckets are the
    # server's, 12 a minute for Applebot and a 20-token global bucket.
    # An unlisted crawler's bucket is one worker's: 10% / 8 = 1.5.
    gate = _gate(Clock(), total=120, uas={"Applebot": 10}, workers=8)
    assert gate.ua_per_minute("Applebot") == pytest.approx(12)
    assert gate.global_tokens() == pytest.approx(
        120 * crawler_budget.BURST_SECONDS / 60
    )
    assert gate.check(GPTBOT) is None
    assert gate.ua_per_minute("gptbot") == pytest.approx(1.5)


def test_sub_one_per_minute_share_admits_one_then_every_60_over_rate_seconds():
    # An unlisted crawler on one of 8 workers: 60 * 10% / 8 = 0.75 a minute.
    clock = Clock()
    gate = _gate(clock, total=60, uas={"Applebot": 10}, workers=8)
    assert gate.check(GPTBOT) is None
    assert gate.ua_per_minute("gptbot") == pytest.approx(0.75)
    assert gate.check(GPTBOT) == "gptbot"
    clock.now += 80  # 60 / 0.75
    assert gate.check(GPTBOT) is None
    assert gate.check(GPTBOT) == "gptbot"


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
        ("Lightpanda/1.0", "lightpanda"),  # headless scraper, no bot marker (#828)
        ("bot/1.0", "bot"),  # a token that IS the marker counts, on purpose
        (APPLEBOT, "Applebot"),  # listed: the dict key, not the token
        (CUBOT_BROWSER, None),
        (BROWSER, None),
        ("Lynx/2.9.0 libwww-FM/2.14", None),
        # A user-initiated agent fetch (#884): no marker token, so it is
        # not a crawler and is ungated on HTML and Markdown alike.
        ("Claude-User (claude-code/2.1.0; +https://support.anthropic.com/)", None),
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
    # unlisted: default 10% of 120 = 12 a minute overall = 1.5 per worker;
    # listed and shared: 25% of 120 for the whole server
    assert gate.ua_per_minute("googlebot") == pytest.approx(1.5)
    assert gate.ua_per_minute("Applebot") == pytest.approx(30)


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
    clock = Clock()
    gate = _gate(clock, total=1000, uas={"Applebot": 100})
    assert gate.check(GOOGLEBOT) is None
    assert gate.check(BINGBOT) is None
    assert gate.unlisted_count() == 2
    clock.now += 1  # the global bucket is full again, the overflow untouched
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
        "lightpanda",
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
    # Global 120, pool 30%: 36 a minute, a 10 s bucket holds 6 tokens. A
    # 100% default share puts unlisted crawlers at the top of the global
    # bucket, so the pool is what binds.
    clock = Clock()
    gate = _gate(
        clock,
        total=120,
        uas={"Applebot": 10},
        unlisted_pool_pct=30,
        default_ua_pct=100,
    )
    admitted = sum(gate.check(f"Mozilla (x{i}-bot/1.0)") is None for i in range(50))
    assert admitted == 6
    # A listed crawler never touches the pool and still gets its share
    # (10% of 120 = 12 a minute, 2 tokens) during the flood, once the
    # global bucket is back above its 90% line.
    clock.now += 4
    pool = gate.pool_tokens()
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) is None
    assert gate.check(APPLEBOT) == "Applebot"
    assert gate.pool_tokens() == pool


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


def test_default_crawler_list():
    assert DEFAULT_CRAWLER_UA_BUDGETS == {
        **dict.fromkeys(DEFAULT_LISTED, 10),
        "Googlebot": 80,
        "bingbot": 80,
    }
    assert "ChatGPT-User" not in DEFAULT_CRAWLER_UA_BUDGETS


def _production_shaped_gate(clock):
    """Default budgets, global 100 a minute over 16 workers (#848). The
    listed buckets are shared, so they hold each crawler's whole share."""
    return CrawlerBudget(100, DEFAULT_CRAWLER_UA_BUDGETS, 16, 120, clock)


@pytest.mark.parametrize("ua, key", [(GOOGLEBOT, "Googlebot"), (BINGBOT, "bingbot")])
def test_search_crawlers_are_listed_by_default(ua, key):
    gate = _production_shaped_gate(Clock())
    assert gate.crawler_key(ua) == key
    # 80% of 100, shared by every worker. An unlisted crawler has no
    # bucket, so this also fails when the key merely equals the token.
    assert gate.ua_per_minute(key) == 80


def _burst(gate, clock, ua, step=20):
    """Three requests `step` seconds apart."""
    answers = []
    for _ in range(3):
        answers.append(gate.check(ua))
        clock.now += step
    return answers


def test_search_crawler_burst_over_sixteen_workers_is_admitted():
    clock = Clock()
    gate = _production_shaped_gate(clock)
    # 80% of 100 = 80 a minute.
    assert _burst(gate, clock, GOOGLEBOT) == [None, None, None]
    assert _burst(gate, clock, BINGBOT) == [None, None, None]


def test_default_share_crawler_is_still_throttled_by_the_same_burst():
    clock = Clock()
    gate = _production_shaped_gate(clock)
    # 10% of 100 = 10 a minute, 1.67 tokens: three requests half a
    # second apart, the second and third are refused. Keeps the gate on.
    assert _burst(gate, clock, APPLEBOT, step=0.5) == [None, "Applebot", "Applebot"]


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
    assert gate.ua_per_minute("Applebot") == pytest.approx(60)


def test_nested_env_form_overrides_a_default_despite_lowercasing(monkeypatch):
    monkeypatch.setenv("CRAWLER_UA_BUDGETS__Applebot", "50")
    merged = Settings().effective_crawler_ua_budgets
    assert merged["Applebot"] == 50
    assert "applebot" not in merged
    assert list(merged) == list(DEFAULT_CRAWLER_UA_BUDGETS)


# Impossible Fetch Metadata / Referer combination (#903): a browser sends
# no Referer on a user-initiated navigation (`Sec-Fetch-Site: none`), so
# that pair is a client faking a click. One shared small bucket.
IMPOSSIBLE = {"sec_fetch_site": "none", "referer": "https://example.org/"}
IMPOSSIBLE_HDRS = {"Sec-Fetch-Site": "none", "Referer": "https://example.org/"}


@pytest.mark.parametrize(
    "site, referer, matches",
    [
        ("none", "https://example.org/", True),
        ("None", "https://example.org/", True),
        ("none", None, False),
        ("none", "", False),
        ("same-origin", "https://example.org/", False),
        ("cross-site", "https://example.org/", False),
        (None, "https://example.org/", False),
        (None, None, False),
    ],
)
def test_impossible_headers_predicate(site, referer, matches):
    assert crawler_budget.impossible_headers(site, referer) is matches


def test_impossible_headers_get_a_one_percent_bucket_that_charges_global():
    gate = _gate(Clock(), total=1200)  # 1% = 12 a minute, capacity 2
    assert gate.check(BROWSER, **IMPOSSIBLE) is None
    assert gate.check(BROWSER, **IMPOSSIBLE) is None
    assert gate.check(BROWSER, **IMPOSSIBLE) == crawler_budget.IMPOSSIBLE_KEY
    assert gate.global_tokens() == pytest.approx(200 - 2)
    assert gate.unlisted_count() == 0
    assert gate.pool_tokens() == pytest.approx(60)  # 30% of 200, untouched


def test_impossible_bucket_capacity_follows_the_setting():
    gate = _gate(Clock(), total=1200, impossible_headers_pct=50)  # 600/min, cap 100
    admitted = sum(gate.check(BROWSER, **IMPOSSIBLE) is None for _ in range(300))
    assert admitted == 100


def test_impossible_headers_pct_zero_disables_it():
    gate = _gate(Clock(), total=100, impossible_headers_pct=0)
    assert all(gate.check(BROWSER, **IMPOSSIBLE) is None for _ in range(500))


def test_global_zero_disables_impossible_headers():
    gate = _gate(Clock(), total=0)
    assert all(gate.check(BROWSER, **IMPOSSIBLE) is None for _ in range(500))


def test_declared_crawler_keeps_its_own_bucket_when_headers_are_impossible():
    # 50% for the header bucket keeps the global bucket's priority out of
    # the way: Applebot at 1% and the header bucket at 1% would compete
    # for the same top 1% of it.
    gate = _gate(Clock(), total=1200, impossible_headers_pct=50)
    assert gate.check(APPLEBOT, **IMPOSSIBLE) is None
    assert gate.check(APPLEBOT, **IMPOSSIBLE) is None
    assert gate.check(APPLEBOT, **IMPOSSIBLE) == "Applebot"
    # The impossible-header bucket was never touched.
    assert gate.check(BROWSER, **IMPOSSIBLE) is None
    assert gate.check(BROWSER, **IMPOSSIBLE) is None


def test_impossible_refusal_charges_no_bucket():
    gate = _gate(Clock(), total=1200)
    for _ in range(2):
        gate.check(BROWSER, **IMPOSSIBLE)
    before = gate.global_tokens()
    assert gate.check(BROWSER, **IMPOSSIBLE) == crawler_budget.IMPOSSIBLE_KEY
    assert gate.global_tokens() == before


def test_impossible_headers_route_gets_429_but_real_navigation_never_does(
    client, monkeypatch
):
    _install(monkeypatch, _gate(Clock(), total=1200))
    h = {"User-Agent": BROWSER, **IMPOSSIBLE_HDRS}
    assert client.get("/", headers=h).status_code == 200
    assert client.get("/", headers=h).status_code == 200
    r = client.get("/", headers=h)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "120"
    # Bucket empty: an in-site click and a direct visit still pass.
    click = {
        "User-Agent": BROWSER,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Referer": "https://example.org/",
    }
    direct = {"User-Agent": BROWSER, "Sec-Fetch-Site": "none"}
    for _ in range(5):
        assert client.get("/", headers=click).status_code == 200
        assert client.get("/", headers=direct).status_code == 200


@pytest.mark.parametrize("path", ["/robots.txt", "/sitemap.xml", "/healthz", "/readyz"])
def test_exempt_endpoints_are_never_gated_on_impossible_headers(
    client, monkeypatch, path
):
    _install(monkeypatch, _gate(Clock(), total=1200, impossible_headers_pct=100))
    h = {"User-Agent": BROWSER, **IMPOSSIBLE_HDRS}
    for _ in range(300):  # past the 200 token capacity
        client.get("/", headers=h)
    assert client.get("/", headers=h).status_code == 429
    assert client.get(path, headers=h).status_code != 429


def _warnings(fn):
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    logging.getLogger().addHandler(handler)
    try:
        fn()
    finally:
        logging.getLogger().removeHandler(handler)
    return seen


def test_header_bucket_log_line_says_headers_not_user_agent():
    clock = Clock()
    gate = _gate(clock, total=1200)

    def run():
        for _ in range(4):  # 2 admitted, 2 deferred: one line, the second silent
            gate.check(BROWSER, **IMPOSSIBLE)
        clock.now += 61  # bucket full again
        for _ in range(3):  # 2 admitted, 1 deferred, plus the silent one
            gate.check(BROWSER, **IMPOSSIBLE)

    pid = os.getpid()
    assert _warnings(run) == [
        "crawler budget: matched on request headers, not User-Agent: "
        f'rule="Sec-Fetch-Site: none with Referer" deferred=1 own=1 global=0 pool=0 '
        f"pid={pid}",
        "crawler budget: matched on request headers, not User-Agent: "
        f'rule="Sec-Fetch-Site: none with Referer" deferred=2 own=2 global=0 pool=0 '
        f"pid={pid}",
    ]


def test_ua_log_line_is_unchanged_next_to_the_header_line():
    gate = _gate(Clock(), total=1200)

    def run():
        for _ in range(3):
            gate.check(APPLEBOT, **IMPOSSIBLE)

    assert _warnings(run) == [
        "crawler budget: crawler=Applebot deferred=1 own=1 global=0 pool=0 "
        f"pid={os.getpid()}"
    ]


def test_impossible_headers_pct_setting(monkeypatch):
    assert Settings().crawler_impossible_headers_pct == 1
    monkeypatch.setenv("CRAWLER_IMPOSSIBLE_HEADERS_PCT", "0")
    assert Settings().crawler_impossible_headers_pct == 0
    monkeypatch.setenv("CRAWLER_IMPOSSIBLE_HEADERS_PCT", "7")
    monkeypatch.setenv("CRAWLER_BUDGET_PER_MINUTE", "1200")
    s = Settings()
    assert s.crawler_impossible_headers_pct == 7
    gate = CrawlerBudget.from_settings(s)
    assert gate.impossible_per_minute() == pytest.approx(84)


@pytest.mark.parametrize("value", ["-1", "101"])
def test_impossible_headers_pct_is_a_validated_percentage(monkeypatch, value):
    monkeypatch.setenv("CRAWLER_IMPOSSIBLE_HEADERS_PCT", value)
    with pytest.raises(ValueError):
        Settings()


# Priority on the global bucket and buckets shared across workers (#1026).
CLAUDEBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"


def _refusals(line):
    """The per-bucket counts on a deferral log line, parsed."""
    return {k: int(v) for k, v in re.findall(r"\b(own|global|pool)=(\d+)", line)}


def test_googlebot_is_admitted_while_applebot_and_an_unlisted_crawler_saturate():
    # The production shape that refused Googlebot: default budgets, 100 a
    # minute, Applebot and ClaudeBot (unlisted) asking ten times a second.
    clock = Clock()
    gate = _production_shaped_gate(clock)
    google = []
    low_refused = 0
    for tick in range(600):  # 60 s in 0.1 s steps
        low_refused += gate.check(APPLEBOT) is not None
        low_refused += gate.check(CLAUDEBOT) is not None
        if tick % 10 == 0:
            google.append(gate.check(GOOGLEBOT))
        clock.now += 0.1
    assert low_refused > 1000  # precondition: they really are saturating
    assert len(google) == 60
    assert google == [None] * 60


def test_a_low_share_crawler_is_served_only_from_the_top_of_the_global_bucket():
    # Global 600 a minute holds 100 tokens. bingbot at 50% draws it to 50.
    gate = _gate(
        Clock(), total=600, uas={"bingbot": 50, "Googlebot": 80, "Applebot": 10}
    )
    assert sum(gate.check(BINGBOT) is None for _ in range(200)) == 50
    assert gate.global_tokens() == pytest.approx(50)
    # Applebot's own bucket is full, but 50 is below its 90% line...
    assert gate.ua_tokens("Applebot") == pytest.approx(10)
    assert gate.check(APPLEBOT) == "Applebot"
    # ...while Googlebot may draw the bucket down to 20%.
    assert gate.check(GOOGLEBOT) is None


def test_a_global_refusal_is_counted_as_one_on_the_log_line():
    gate = _gate(Clock(), total=600, uas={"bingbot": 50, "Applebot": 10})
    for _ in range(60):
        gate.check(BINGBOT)
    lines = _warnings(lambda: gate.check(APPLEBOT))
    applebot = [m for m in lines if "crawler=Applebot " in m]
    assert len(applebot) == 1
    assert _refusals(applebot[0]) == {"own": 0, "global": 1, "pool": 0}


def test_a_high_share_crawler_is_still_capped_by_its_own_share():
    clock = Clock()
    gate = _gate(clock, total=600, uas={"Googlebot": 80})  # 480 a minute
    admitted = 0
    for _ in range(1200):  # 60 s at 20 a second
        admitted += gate.check(GOOGLEBOT) is None
        clock.now += 0.05
    # A minute's share plus its ten-second burst (80 tokens).
    assert 480 <= admitted <= 480 + 80


def test_two_gates_on_one_file_share_their_buckets(tmp_path):
    path = str(tmp_path / "budget")
    clock = Clock()
    a = _gate(clock, total=1200, uas={"Applebot": 1}, shared_path=path)
    b = _gate(clock, total=1200, uas={"Applebot": 1}, shared_path=path)
    assert a.check(APPLEBOT) is None
    assert a.check(APPLEBOT) is None  # 1% of 1200 holds 2 tokens
    assert b.check(APPLEBOT) == "Applebot"
    # The second gate found the slots running and did not refill them.
    assert b.ua_tokens("Applebot") == pytest.approx(0)


def _hammer(path, deadline):
    """One process's share of the multi-process test: ask until the
    deadline, report (asked, admitted)."""
    gate = CrawlerBudget(600, {"Applebot": 100}, 4, 120, shared_path=path)
    asked = admitted = 0
    while time.monotonic() < deadline:
        asked += 1
        admitted += gate.check(APPLEBOT) is None
    return asked, admitted


def test_worker_processes_share_one_budget(tmp_path):
    path = str(tmp_path / "budget")
    window = 4.0
    deadline = time.monotonic() + window
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(4, mp_context=ctx) as pool:
        results = list(pool.map(_hammer, [path] * 4, [deadline] * 4))
    # Precondition: at least two processes really asked at the same time.
    assert sum(asked > 0 for asked, _ in results) >= 2, results
    admitted = sum(n for _, n in results)
    # 600 a minute is 10 a second; the bucket holds 100. Four private
    # buckets would admit over 400.
    assert admitted <= 10 * window + 100 + 1, results


def test_from_settings_creates_one_file_only_when_the_budget_is_on(
    tmp_path, monkeypatch
):
    CrawlerBudget.from_settings(Settings())  # the default budget is 0
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setenv("CRAWLER_BUDGET_PER_MINUTE", "120")
    CrawlerBudget.from_settings(Settings())
    CrawlerBudget.from_settings(Settings())  # a second worker, same file
    files = [p.name for p in tmp_path.iterdir()]
    assert len(files) == 1
    assert files[0].startswith(f"mimir-crawler-budget-{os.getppid()}-")


def test_a_worker_whose_clock_read_lags_never_moves_a_stamp_back(tmp_path):
    # Worker B read the clock before waiting for the lock, so its `now`
    # is behind the stamp worker A just wrote. Moving the stamp back
    # would credit 1005..1010 twice.
    path = str(tmp_path / "budget")
    clock_a, clock_b = Clock(), Clock()
    a = _gate(clock_a, total=600, uas={"Applebot": 100}, shared_path=path)
    b = _gate(clock_b, total=600, uas={"Applebot": 100}, shared_path=path)
    clock_a.now = clock_b.now = 1010.0
    for _ in range(50):
        assert a.check(APPLEBOT) is None  # 100 tokens down to 50
    clock_b.now = 1005.0
    assert b.check(APPLEBOT) is None  # 49
    assert a.check(APPLEBOT) is None  # still 1010: nothing to refill
    assert a.ua_tokens("Applebot") == pytest.approx(48)
