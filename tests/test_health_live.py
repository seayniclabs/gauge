"""Live health checks against real public hosts. Opt-in: GAUGE_LIVE=1 .venv/bin/pytest tests/test_health_live.py

Every check has a negative control that must FAIL on a known-bad public target. Your own sites are
never named in this repo: list them in GAUGE_LIVE_TARGETS (comma-separated URLs) to check them too.
"""

import asyncio
import os

import pytest

from gauge_mcp.modules import health as H

pytestmark = pytest.mark.skipif(os.environ.get("GAUGE_LIVE") != "1", reason="live network test; set GAUGE_LIVE=1")


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
    assert c["tls"].status == "FAILING"


def test_control_tls_wrong_host():
    _, c = check("https://wrong.host.badssl.com/")
    assert c["tls"].status == "FAILING"


def test_control_content_missing_string():
    _, c = check("https://github.com/", expect_text="gauge-control-string-7f3a9c")
    assert c["content"].status == "FAILING"


def test_control_headers_on_plain_http_site():
    _, c = check("http://neverssl.com/")
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
