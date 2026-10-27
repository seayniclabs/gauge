"""`gauge-mcp --doctor`: check settings and reachability, print a fix for each problem.

Makes only the calls a person asked for by running it: Umami's heartbeat, and one
read-only Resend call (list domains). Exit status 0 = ready, 1 = something to fix.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from . import __version__
from .approvals import log_path
from .config import Config, config_path
from .modules import all_modules
from .modules.umami import CLOUD_URL


async def _checks(config: Config) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    path = config_path(config.env)
    out.append(("config file", True, f"{path} ({'found' if path.is_file() else 'not used'})"))
    async with httpx.AsyncClient(timeout=8.0) as client:
        for m in all_modules():
            if not config.enabled(m.name, m.default_enabled):
                out.append((m.name, True, "switched off"))
                continue
            missing = m.missing(config)
            if missing:
                out.append((m.name, False, f"missing {', '.join(missing)}. Add them to your MCP app's env or to {path}."))
                continue
            try:
                if m.name == "umami":
                    url = (config.get("umami", "url") or CLOUD_URL).rstrip("/")
                    if config.get("umami", "api_key"):
                        r = await client.get(f"{url}/websites", headers={"x-umami-api-key": config.get('umami', 'api_key')}, params={"pageSize": 1})
                    else:
                        r = await client.get(f"{url}/api/heartbeat")
                    ok = r.status_code < 400
                    out.append((m.name, ok, f"{url} answered {r.status_code}" if ok else
                                f"{url} answered {r.status_code}. Check GAUGE_UMAMI_URL and the login or API key."))
                elif m.name == "resend":
                    r = await client.get("https://api.resend.com/domains",
                                         headers={"Authorization": f"Bearer {config.get('resend', 'api_key')}"})
                    if r.status_code >= 400:
                        out.append((m.name, False, f"Resend answered {r.status_code}. Check GAUGE_RESEND_API_KEY."))
                        continue
                    domains = {d.get("name"): d.get("status") for d in r.json().get("data", [])}
                    sender = config.get("resend", "from") or ""
                    dom = sender.rsplit("@", 1)[-1] if "@" in sender else ""
                    if not sender:
                        out.append((m.name, True, "key works; set GAUGE_RESEND_FROM to send"))
                    elif domains.get(dom) == "verified":
                        out.append((m.name, True, f"key works; sender domain {dom} is verified"))
                    else:
                        out.append((m.name, False, f"sender domain {dom or '?'} is not verified in Resend (status: {domains.get(dom, 'not added')})."))
            except httpx.HTTPError as e:
                out.append((m.name, False, f"could not connect ({type(e).__name__}). Check the address and your network."))
    out.append(("approval log", True, str(log_path(config.env))))
    return out


def run(config: Config | None = None) -> int:
    config = config or Config()
    results = asyncio.run(_checks(config))
    print(f"gauge-mcp {__version__} doctor")
    for name, ok, msg in results:
        print(f"  {'ok ' if ok else 'FIX'}  {name:<13} {msg}")
    bad = sum(1 for _, ok, _ in results if not ok)
    print("ready" if not bad else f"{bad} thing(s) to fix")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
