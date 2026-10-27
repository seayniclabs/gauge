"""Start-and-list, module switches, missing config, and one mocked call per tool.

No test touches the network: every HTTP call goes through httpx.MockTransport.
"""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from gauge_mcp.config import Config
from gauge_mcp.server import build_server

UMAMI = {"GAUGE_UMAMI_URL": "https://analytics.example.com", "GAUGE_UMAMI_USERNAME": "viewer",
         "GAUGE_UMAMI_PASSWORD": "pw"}
RESEND = {"GAUGE_RESEND_API_KEY": "re_test", "GAUGE_RESEND_FROM": "hello@example.com"}


def server(env, handler=None):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(500))))
    return build_server(Config(env=env, file_data={}), client_factory=factory)


def tools(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def call(mcp, name, **args):
    """Return the structured result, or raise the tool's error."""
    _, structured = asyncio.run(mcp.call_tool(name, args))
    return structured


ALL = {"gauge_modules", "umami_websites", "umami_stats", "umami_pageviews", "umami_metrics",
       "resend_send", "resend_emails", "resend_domains"}


# --- start, list, switches -------------------------------------------------

def test_empty_env_starts_and_lists_every_tool():
    assert tools(server({})) == ALL


def test_every_tool_has_an_output_schema():
    assert all(t.outputSchema for t in asyncio.run(server({}).list_tools()))


def test_module_switch_off_hides_its_tools():
    names = tools(server({"GAUGE_RESEND_ENABLED": "false"}))
    assert not any(n.startswith("resend_") for n in names)
    assert "umami_stats" in names


def test_config_file_is_read_and_env_wins():
    cfg = Config(env={"GAUGE_UMAMI_URL": "https://from-env.example.com"},
                 file_data={"umami": {"url": "https://from-file.example.com", "username": "u"}, "resend": {"enabled": False}})
    assert cfg.get("umami", "url") == "https://from-env.example.com"
    assert cfg.get("umami", "username") == "u"
    assert cfg.enabled("resend", True) is False


def test_gauge_modules_reports_missing_settings():
    out = call(server({}), "gauge_modules")
    mods = {m["name"]: m for m in out["modules"]}
    assert mods["umami"]["configured"] is False
    assert "GAUGE_UMAMI_URL" in mods["umami"]["missing"]
    assert mods["resend"]["missing"] == ["GAUGE_RESEND_API_KEY"]


def test_gauge_modules_detail_never_leaks_secret_values():
    out = call(server(RESEND), "gauge_modules", module="resend")
    text = json.dumps(out)
    assert "re_test" not in text
    assert {s["env_var"]: s["set"] for s in out["modules"][0]["settings"]}["GAUGE_RESEND_API_KEY"] is True


# --- missing config never crashes; errors name the variable --------------

@pytest.mark.parametrize("tool,args", [
    ("umami_websites", {}), ("umami_stats", {"website_id": "w"}), ("umami_pageviews", {"website_id": "w"}),
    ("umami_metrics", {"website_id": "w"}), ("resend_emails", {}), ("resend_domains", {}),
])
def test_unconfigured_tool_names_missing_variable(tool, args):
    with pytest.raises(Exception) as err:
        call(server({}), tool, **args)
    assert "not_configured" in str(err.value)
    assert "GAUGE_" in str(err.value)


def test_send_without_sender_names_from_variable():
    with pytest.raises(Exception) as err:
        call(server({"GAUGE_RESEND_API_KEY": "re_test"}), "resend_send", to=["a@example.com"], subject="s", text="t")
    assert "GAUGE_RESEND_FROM" in str(err.value)


# --- umami, mocked ----------------------------------------------------------

def umami_handler(seen):
    def handle(req: httpx.Request):
        seen.append(req)
        path = req.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"token": "tok"})
        assert req.headers["authorization"] == "Bearer tok"
        if path == "/api/websites":
            return httpx.Response(200, json={"data": [{"id": "w1", "name": "Site", "domain": "example.com"}], "count": 1})
        if path.endswith("/stats"):
            return httpx.Response(200, json={"pageviews": 120, "visitors": 40, "visits": 50, "bounces": 20, "totaltime": 900})
        if path.endswith("/pageviews"):
            return httpx.Response(200, json={"pageviews": [{"x": "2026-10-01", "y": 10}], "sessions": [{"x": "2026-10-01", "y": 4}]})
        if path.endswith("/metrics"):
            return httpx.Response(200, json=[{"x": "/", "y": 30}, {"x": "/pricing", "y": 12}, {"x": "/about", "y": 3}])
        return httpx.Response(404)
    return handle


def test_umami_websites():
    seen = []
    out = call(server(UMAMI, umami_handler(seen)), "umami_websites")
    assert out["websites"][0] == {"id": "w1", "name": "Site", "domain": "example.com"}
    assert seen[0].url.path == "/api/auth/login"


def test_umami_stats_computes_bounce_rate():
    out = call(server(UMAMI, umami_handler([])), "umami_stats", website_id="w1", start="2026-10-01", end="2026-10-08")
    assert (out["pageviews"], out["visits"], out["bounce_rate"]) == (120, 50, 0.4)
    assert out["start"].startswith("2026-10-01")


def test_umami_stats_accepts_umami2_shape():
    def handle(req):
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"token": "tok"})
        return httpx.Response(200, json={"pageviews": {"value": 7, "prev": 1}, "visitors": {"value": 3},
                                         "visits": {"value": 4}, "bounces": {"value": 1}})
    assert call(server(UMAMI, handle), "umami_stats", website_id="w1")["pageviews"] == 7


def test_umami_pageviews():
    out = call(server(UMAMI, umami_handler([])), "umami_pageviews", website_id="w1")
    assert out["points"] == [{"t": "2026-10-01", "pageviews": 10, "sessions": 4}]


def test_umami_pageviews_rejects_bad_unit():
    with pytest.raises(Exception) as err:
        call(server(UMAMI, umami_handler([])), "umami_pageviews", website_id="w1", unit="week")
    assert "bad_unit" in str(err.value)


def test_umami_metrics_top_pages_honours_limit():
    seen = []
    out = call(server(UMAMI, umami_handler(seen)), "umami_metrics", website_id="w1", type="path", limit=2)
    assert [r["value"] for r in out["rows"]] == ["/", "/pricing"]
    assert seen[-1].url.params["type"] == "path" and seen[-1].url.params["limit"] == "2"


def test_umami_metrics_falls_back_to_url_type_on_umami2():
    def handle(req):
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"token": "tok"})
        if req.url.params["type"] == "path":
            return httpx.Response(400, text="bad type")
        return httpx.Response(200, json=[{"x": "/", "y": 5}])
    assert call(server(UMAMI, handle), "umami_metrics", website_id="w1")["rows"] == [{"value": "/", "count": 5}]


def test_umami_relogs_once_on_expired_token():
    tokens = iter(["old", "new"])
    calls = {"stats": 0}

    def handle(req):
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"token": next(tokens)})
        calls["stats"] += 1
        if req.headers["authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"pageviews": 1, "visitors": 1, "visits": 1, "bounces": 0})
    assert call(server(UMAMI, handle), "umami_stats", website_id="w1")["pageviews"] == 1
    assert calls["stats"] == 2


def test_umami_cloud_uses_api_key_header():
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [], "count": 0})
    call(server({"GAUGE_UMAMI_API_KEY": "k"}, handle), "umami_websites")
    assert seen[0].headers["x-umami-api-key"] == "k"
    assert str(seen[0].url).startswith("https://api.umami.is/v1/websites")


# --- resend, mocked ---------------------------------------------------------

def test_resend_send_previews_without_confirm():
    seen = []
    out = call(server(RESEND, lambda r: seen.append(r) or httpx.Response(500)), "resend_send",
               to=["a@example.com"], subject="Hi", text="Hello")
    assert out["sent"] is False and seen == []
    assert out["draft"]["sender"] == "hello@example.com"


def send_handler(seen):
    def handle(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "em_1"})
    return handle


def test_resend_confirm_alone_cannot_send_without_a_person():
    # The model passing confirm=true is not approval. With no way to ask the person, refuse.
    seen = []
    with pytest.raises(Exception) as err:
        call(server(RESEND, send_handler(seen)), "resend_send", to=["a@example.com"], subject="Hi", text="Hello", confirm=True)
    assert "approval_unavailable" in str(err.value) and seen == []


def test_resend_allowlist_approves_listed_recipients():
    seen = []
    env = {**RESEND, "GAUGE_RESEND_FROM_NAME": "Example", "GAUGE_RESEND_ALLOWED_RECIPIENTS": "@example.com"}
    out = call(server(env, send_handler(seen)), "resend_send", to=["a@example.com"], subject="Hi", text="Hello", confirm=True)
    assert (out["sent"], out["id"], out["approved_by"]) == (True, "em_1", "allowlist")
    body = json.loads(seen[0].content)
    assert body["from"] == "Example <hello@example.com>" and body["to"] == ["a@example.com"]
    assert seen[0].headers["authorization"] == "Bearer re_test"


def test_resend_allowlist_blocks_other_recipients():
    seen = []
    env = {**RESEND, "GAUGE_RESEND_ALLOWED_RECIPIENTS": "@example.com"}
    with pytest.raises(Exception):
        call(server(env, send_handler(seen)), "resend_send", to=["x@other.test"], subject="Hi", text="Hello", confirm=True)
    assert seen == []


@pytest.mark.parametrize("bad", ["x@evil.test, a@example.com", "Name <a@example.com>", "a@example.com\nbcc:x@evil.test",
                                 "a@example.com;x@evil.test", "not-an-address"])
def test_resend_rejects_anything_but_one_plain_address(bad):
    seen = []
    env = {**RESEND, "GAUGE_RESEND_ALLOWED_RECIPIENTS": "@example.com"}
    with pytest.raises(Exception) as err:
        call(server(env, send_handler(seen)), "resend_send", to=[bad], subject="Hi", text="Hello", confirm=True)
    assert "bad_address" in str(err.value) and seen == []


def test_resend_require_confirm_false_lets_assistant_send():
    seen = []
    out = call(server({**RESEND, "GAUGE_RESEND_REQUIRE_CONFIRM": "false"}, send_handler(seen)), "resend_send",
               to=["a@example.com"], subject="Hi", text="Hello", confirm=True)
    assert out["approved_by"] == "settings" and len(seen) == 1


def send_via_client(answer):
    """Call resend_send through a real MCP client session that answers the approval prompt."""
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import ElicitResult

    seen, asked = [], []

    async def on_elicit(context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"send": answer}) if answer is not None else ElicitResult(action="decline")

    async def run():
        async with create_connected_server_and_client_session(
                server(RESEND, send_handler(seen)), elicitation_callback=on_elicit) as client:
            r = await client.call_tool("resend_send", {"to": ["a@example.com"], "subject": "Hi", "text": "Hello", "confirm": True})
            return r.structuredContent
    return asyncio.run(run()), seen, asked


def test_resend_person_approves_through_the_client():
    out, seen, asked = send_via_client(True)
    assert out["sent"] is True and out["approved_by"] == "person" and len(seen) == 1
    # The person sees everything that will be sent, not just the subject.
    for part in ("a@example.com", "Subject: 'Hi'", "Text: 'Hello'", "HTML: (none)", "Reply-To:"):
        assert part in asked[0]


def test_resend_person_declines_through_the_client():
    out, seen, _ = send_via_client(None)
    assert out["sent"] is False and seen == []


def test_resend_emails_pages_with_cursor():
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200, json={"has_more": True, "data": [
            {"id": "e1", "to": ["a@example.com"], "from": "hello@example.com", "subject": "s", "created_at": "t"}]})
    out = call(server(RESEND, handle), "resend_emails", limit=1, after="e0")
    assert out["next_after"] == "e1" and out["has_more"] is True
    assert seen[0].url.params["after"] == "e0"


def test_resend_domains():
    out = call(server(RESEND, lambda r: httpx.Response(200, json={"data": [{"id": "d1", "name": "example.com", "status": "verified"}]})),
               "resend_domains")
    assert out["domains"][0]["status"] == "verified"


def test_resend_auth_error_is_readable():
    with pytest.raises(Exception) as err:
        call(server(RESEND, lambda r: httpx.Response(401)), "resend_domains")
    assert "gauge.resend.auth_failed" in str(err.value)


# --- real process over stdio, empty environment ----------------------------

def test_stdio_process_starts_with_empty_env(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "gauge_mcp.server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out, _ = proc.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=60)
    replies = {r.get("id"): r for r in map(json.loads, out.splitlines()) if r.get("id")}
    from gauge_mcp import __version__
    assert replies[1]["result"]["serverInfo"] == {"name": "gauge", "version": __version__}
    assert {t["name"] for t in replies[2]["result"]["tools"]} == ALL


# --- skills: one source, served as MCP prompts and resources ---------------

from pathlib import Path

SKILLS = sorted(p.name for p in (Path(__file__).resolve().parent.parent / "skills").iterdir() if (p / "SKILL.md").exists())


def test_every_skill_is_an_mcp_prompt():
    prompts = {p.name for p in asyncio.run(server({}).list_prompts())}
    assert prompts == set(SKILLS) and "getting-started" in prompts


def test_skill_prompt_returns_the_skill_text():
    msgs = asyncio.run(server({}).get_prompt("weekly-site-report")).messages
    assert "umami_metrics" in msgs[0].content.text


def test_skill_files_are_resources():
    uris = {str(r.uri) for r in asyncio.run(server({}).list_resources())}
    assert "skill://gauge/getting-started/SKILL.md" in uris
    assert any(u.startswith("skill://gauge/copywriting/references/") for u in uris)


def test_discovery_lists_skills_and_credits_the_publisher():
    out = call(server({}), "gauge_modules")
    assert {s["name"] for s in out["skills"]} == set(SKILLS)
    assert out["publisher"] == "Seaynic Labs" and out["homepage"] == "https://github.com/seayniclabs/gauge"


def test_no_promotion_reaches_the_model():
    # Credit lives in the README, CREDITS and metadata. Text the model reads (instructions, prompts,
    # tool descriptions) carries no links to our services: that looks like prompt injection and costs tokens.
    srv = server({})
    texts = [srv.instructions or ""] + [t.description or "" for t in asyncio.run(srv.list_tools())]
    texts += [asyncio.run(srv.get_prompt(p.name)).messages[0].content.text for p in asyncio.run(srv.list_prompts())]
    assert not any("seayniclabs.com" in x or "Need tools like this" in x for x in texts)


def test_skill_checker_passes_and_catches_a_missing_license(tmp_path):
    root = Path(__file__).resolve().parent.parent
    ok = subprocess.run([sys.executable, str(root / "scripts" / "check_skills.py")], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout
    # a copy of the repo layout with one third-party skill missing its LICENSE must fail
    import shutil
    shutil.copytree(root / "skills", tmp_path / "skills")
    shutil.copy(root / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(root / "CREDITS.md", tmp_path / "CREDITS.md")
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts" / "check_skills.py", tmp_path / "scripts" / "check_skills.py")
    (tmp_path / "skills" / "copywriting" / "LICENSE").unlink()
    bad = subprocess.run([sys.executable, str(tmp_path / "scripts" / "check_skills.py")], capture_output=True, text=True)
    assert bad.returncode == 1 and "copywriting" in bad.stdout


# --- approval log and doctor -----------------------------------------------

def read_log(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_approval_log_records_grant_without_the_body(tmp_path):
    log = tmp_path / "approvals.jsonl"
    env = {**RESEND, "GAUGE_RESEND_ALLOWED_RECIPIENTS": "@example.com", "GAUGE_APPROVAL_LOG": str(log)}
    call(server(env, send_handler([])), "resend_send", to=["a@example.com"], subject="Hi", text="secret body", confirm=True)
    rows = read_log(log)
    assert [r["decision"] for r in rows] == ["approved"] and rows[0]["approved_by"] == "allowlist"
    assert "secret body" not in log.read_text() and rows[0]["body_chars"] == len("secret body")


def test_approval_log_records_refusal(tmp_path):
    log = tmp_path / "approvals.jsonl"
    with pytest.raises(Exception):
        call(server({**RESEND, "GAUGE_APPROVAL_LOG": str(log)}, send_handler([])), "resend_send",
             to=["a@example.com"], subject="Hi", text="x", confirm=True)
    assert [r["decision"] for r in read_log(log)] == ["refused"]


def test_doctor_names_missing_settings_and_exits_1(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "gauge_mcp.server", "--doctor"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "GAUGE_RESEND_API_KEY" in r.stdout and "FIX" in r.stdout


def test_tool_lock_is_current():
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, str(root / "scripts" / "tool_lock.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
