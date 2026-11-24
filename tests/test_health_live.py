"""Live health checks against real public hosts. Opt-in: GAUGE_LIVE=1 .venv/bin/pytest tests/test_health_live.py

Every check has a negative control that must FAIL on a known-bad public target. Your own sites are
never named in this repo: list them in GAUGE_LIVE_TARGETS (comma-separated URLs) to check them too.
"""

import asyncio
import os

import pytest

from gauge_mcp.modules import health as H

pytestmark = pytest.mark.skipif(os.environ.get("GAUGE_LIVE") != "1", reason="live network test; set GAUGE_LIVE=1")


def reachable(c, name):
    """A control proves something only if its host answered; otherwise skip and say why."""
    if c["http"].status == "FAILING" and c["http"].detail.startswith("Connect"):
        pytest.skip(f"control host for {name} unreachable right now ({c['http'].detail})")


def check(url, **kw):
    report = asyncio.run(H.run_checks(url, deps=H.Deps.real(), settings=H.Settings(), **kw))
    return report, {c.check: c for c in report.checks}


def test_known_good_site():
    r, c = check("https://github.com/", expect_text="GitHub")
    assert c["dns"].status == "OK"
    assert c["http"].status == "OK"
    assert c["tls"].status == "OK" and c["tls"].data["days_left"] > 0
    assert c["domain"].status == "OK" and c["domain"].data["domain"] == "github.com"
    assert c["content"].status == "OK"


def test_control_dns_dead_host():
    _, c = check("https://gauge-health-control.invalid/")
    assert c["dns"].status == "FAILING"


def test_control_tls_expired():
    _, c = check("https://expired.badssl.com/")
    if c["tls"].status == "UNKNOWN":
        pytest.skip("expired.badssl.com did not accept a connection right now")
    assert c["tls"].status == "FAILING"


def test_control_tls_wrong_host():
    _, c = check("https://wrong.host.badssl.com/")
    if c["tls"].status == "UNKNOWN":
        pytest.skip("wrong.host.badssl.com did not accept a connection right now")
    assert c["tls"].status == "FAILING"


def test_control_content_missing_string():
    _, c = check("https://github.com/", expect_text="gauge-control-string-7f3a9c")
    assert c["content"].status == "FAILING"


def test_control_headers_on_plain_http_site():
    _, c = check("http://neverssl.com/")
    reachable(c, "headers")
    assert c["headers"].status == "FAILING"


def test_control_private_address_blocked():
    _, c = check("http://127.0.0.1:9/")
    assert c["dns"].status == "FAILING"


def test_control_http_dead_port():
    # Public host, closed port: the request cannot connect.
    _, c = check("https://github.com:81/")
    assert c["http"].status == "FAILING"


@pytest.mark.parametrize("url", [u.strip() for u in os.environ.get("GAUGE_LIVE_TARGETS", "").split(",") if u.strip()])
def test_your_targets_report_a_state_for_every_check(url):
    r, c = check(url)
    print(url, r.overall, {k: (v.status, v.detail) for k, v in c.items()})
    assert all(v.status in ("OK", "FAILING", "UNKNOWN") for v in c.values())


# --- AI readiness (AEO/GEO): good public reference plus a known-bad control for each check ----------------

def test_ai_known_good_site():
    r, c = check("https://www.python.org/", checks=["ai"])
    assert c["robots_ai"].status == "OK"
    assert c["structured_data"].status == "OK" and c["structured_data"].data["types"]
    assert c["server_text"].status == "OK"
    assert r.ai_readiness == "OK"


def test_control_robots_blocks_ai_search_crawlers():
    _, c = check("https://www.nytimes.com/", checks=["robots_ai"])
    assert c["robots_ai"].status == "FAILING"
    assert "OAI-SearchBot" in c["robots_ai"].data["blocked_search"]


def test_control_no_structured_data_and_thin_html():
    _, c = check("https://example.com/", checks=["structured_data", "server_text"])
    assert c["structured_data"].status == "FAILING"
    assert c["server_text"].status == "FAILING"


def test_llms_txt_present_and_absent():
    _, good = check("https://llmstxt.org/", checks=["llms_txt"])
    _, bad = check("https://example.com/", checks=["llms_txt"])
    assert good["llms_txt"].data["present"] is True
    assert bad["llms_txt"].data["present"] is False


def test_bot_blocked_page_is_not_scored():
    # This site answers 403 to non-browser clients: the page checks must say UNKNOWN, not FAILING.
    _, c = check("https://www.nytimes.com/", checks=["http", "server_text"])
    if c["http"].data.get("status") == 403:
        assert c["server_text"].status == "UNKNOWN"


# --- SEO: a known-good public page and a known-bad control for each check ----------------------------------

def test_seo_known_good_checks():
    _, c = check("https://github.com/", checks=["seo_indexable", "seo_canonical", "seo_sitemap", "seo_basics", "seo_social"])
    assert {k: v.status for k, v in c.items() if k.startswith("seo_")} == dict.fromkeys(
        ["seo_indexable", "seo_canonical", "seo_sitemap", "seo_basics", "seo_social"], "OK")
    _, p = check("https://www.python.org/", checks=["seo_title", "seo_description"])
    assert p["seo_title"].status == "OK" and p["seo_description"].status == "OK"


def test_control_seo_bare_page():
    _, c = check("https://example.com/", checks=["seo"])
    for name in ("seo_description", "seo_canonical", "seo_sitemap", "seo_basics", "seo_social"):
        assert c[name].status == "FAILING", name


def test_control_seo_blocked_from_search_and_short_title():
    _, c = check("https://www.google.com/search", checks=["seo_indexable", "seo_title"])
    assert c["seo_indexable"].status == "FAILING"
    assert c["seo_indexable"].data["robots_allows"] is False
    assert c["seo_title"].status == "FAILING"
