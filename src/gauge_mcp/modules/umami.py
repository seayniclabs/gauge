"""Umami web analytics (self-hosted or Umami Cloud)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..config import Config
from ..errors import GaugeError
from . import ClientFactory, Module, Setting

CLOUD_URL = "https://api.umami.is/v1"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


class Website(BaseModel):
    id: str
    name: str | None = None
    domain: str | None = None


class Websites(BaseModel):
    websites: list[Website]
    page: int
    page_size: int
    total: int | None = None


class Stats(BaseModel):
    website_id: str
    start: str
    end: str
    pageviews: int
    visitors: int
    visits: int
    bounces: int
    bounce_rate: float | None
    total_time_seconds: int | None = None


class Point(BaseModel):
    t: str
    pageviews: int
    sessions: int


class Pageviews(BaseModel):
    website_id: str
    start: str
    end: str
    unit: str
    points: list[Point]


class MetricRow(BaseModel):
    value: str | None
    count: int


class Metrics(BaseModel):
    website_id: str
    type: str
    start: str
    end: str
    limit: int
    offset: int
    rows: list[MetricRow]


def _num(v: Any) -> int:
    # Umami 3 returns plain numbers; Umami 2 returns {"value": n, "prev": m}.
    if isinstance(v, dict):
        v = v.get("value", 0)
    return int(v or 0)


def _to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _range(days: int, start: str | None, end: str | None) -> tuple[int, int]:
    try:
        end_ms = _to_ms(end) if end else int(time.time() * 1000)
        start_ms = _to_ms(start) if start else end_ms - days * 86_400_000
    except ValueError as e:
        raise GaugeError("umami", "bad_date", str(e), "use ISO 8601, e.g. 2026-10-01 or 2026-10-01T00:00:00Z.")
    if start_ms >= end_ms:
        raise GaugeError("umami", "bad_range", "start must be before end.")
    return start_ms, end_ms


class UmamiModule(Module):
    def __init__(self) -> None:
        super().__init__(
            name="umami",
            summary="Web analytics from your Umami instance: sites, totals, time series, top pages, referrers.",
            settings=[
                Setting("url", "Base URL of your Umami instance, e.g. https://analytics.example.com. "
                        "Optional with an API key (defaults to Umami Cloud).", required=False),
                Setting("api_key", "Umami Cloud API key. Use this OR username + password.", secret=True, required=False),
                Setting("username", "Self-hosted login. A view-only user is enough.", required=False),
                Setting("password", "Password for that user.", secret=True, required=False),
            ],
            tools={
                "umami_websites": "List tracked websites (paged).",
                "umami_stats": "Totals for a date range: pageviews, visitors, visits, bounces.",
                "umami_pageviews": "Pageviews and sessions over time.",
                "umami_metrics": "Ranked breakdown by path, referrer, country, browser, and more.",
            },
        )
        self._token: str | None = None

    def missing(self, config: Config) -> list[str]:
        if config.get(self.name, "api_key"):
            return []
        need = [s for s in ("url", "username", "password") if not config.get(self.name, s)]
        return [f"GAUGE_UMAMI_{k.upper()}" for k in need]

    def _base(self, config: Config) -> str:
        missing = self.missing(config)
        if missing:
            raise GaugeError(
                "umami", "not_configured", f"missing setting(s) {', '.join(missing)}.",
                "set GAUGE_UMAMI_API_KEY (Umami Cloud), or GAUGE_UMAMI_URL, GAUGE_UMAMI_USERNAME "
                "and GAUGE_UMAMI_PASSWORD (self-hosted).",
            )
        url = config.get(self.name, "url") or CLOUD_URL
        return url.rstrip("/")

    def register(self, mcp: FastMCP, config: Config, client_factory: ClientFactory) -> None:
        module = self

        async def login(client: httpx.AsyncClient, base: str) -> str:
            r = await client.post(
                f"{base}/api/auth/login",
                json={"username": config.get("umami", "username"), "password": config.get("umami", "password")},
            )
            if r.status_code in (401, 403):
                raise GaugeError("umami", "auth_failed", "Umami rejected the username or password.",
                                 "check GAUGE_UMAMI_USERNAME and GAUGE_UMAMI_PASSWORD.")
            r.raise_for_status()
            return r.json()["token"]

        async def get(path: str, params: dict | None = None) -> Any:
            base = module._base(config)
            api_key = config.get("umami", "api_key")
            # Self-hosted serves the API under /api; Umami Cloud's base URL already ends in /v1.
            url = f"{base}{path}" if api_key else f"{base}/api{path}"
            try:
                async with client_factory() as client:
                    for attempt in (1, 2):
                        if api_key:
                            headers = {"x-umami-api-key": api_key}
                        else:
                            if not module._token:
                                module._token = await login(client, base)
                            headers = {"Authorization": f"Bearer {module._token}"}
                        r = await client.get(url, params=params, headers=headers)
                        if r.status_code == 401 and not api_key and attempt == 1:
                            module._token = None  # expired token: log in again once
                            continue
                        break
            except httpx.HTTPError as e:
                raise GaugeError("umami", "unreachable", f"could not reach Umami ({type(e).__name__}).",
                                 "check GAUGE_UMAMI_URL and that the instance is running.")
            if r.status_code in (401, 403):
                raise GaugeError("umami", "auth_failed", f"Umami returned {r.status_code}.",
                                 "check the API key or login, and that the user can view this website.")
            if r.status_code == 404:
                raise GaugeError("umami", "not_found", f"{path} not found.", "check the website_id with umami_websites.")
            if r.status_code >= 400:
                raise GaugeError("umami", "api_error", f"Umami returned {r.status_code}: {r.text[:200]}")
            return r.json()

        @mcp.tool(annotations=READ_ONLY)
        async def umami_websites(page: int = 1, page_size: int = 20) -> Websites:
            """List websites tracked in Umami. Paged: page starts at 1, page_size up to 100."""
            page_size = max(1, min(page_size, 100))
            data = await get("/websites", {"page": page, "pageSize": page_size})
            rows = data.get("data", []) if isinstance(data, dict) else data
            total = data.get("count") if isinstance(data, dict) else len(rows)
            return Websites(
                websites=[Website(id=w["id"], name=w.get("name"), domain=w.get("domain")) for w in rows],
                page=page, page_size=page_size, total=total,
            )

        @mcp.tool(annotations=READ_ONLY)
        async def umami_stats(website_id: str, days: int = 7, start: str | None = None, end: str | None = None) -> Stats:
            """Totals for one website. Range: last `days` days, or ISO 8601 `start`/`end`."""
            s, e = _range(days, start, end)
            d = await get(f"/websites/{website_id}/stats", {"startAt": s, "endAt": e})
            visits = _num(d.get("visits"))
            bounces = _num(d.get("bounces"))
            total_time = d.get("totaltime")
            return Stats(
                website_id=website_id, start=_iso(s), end=_iso(e),
                pageviews=_num(d.get("pageviews")), visitors=_num(d.get("visitors")),
                visits=visits, bounces=bounces,
                bounce_rate=round(bounces / visits, 4) if visits else None,
                total_time_seconds=_num(total_time) if total_time is not None else None,
            )

        @mcp.tool(annotations=READ_ONLY)
        async def umami_pageviews(
            website_id: str, days: int = 7, start: str | None = None, end: str | None = None,
            unit: str = "day", timezone: str = "UTC",
        ) -> Pageviews:
            """Pageviews and sessions over time. unit: minute, hour, day, month or year."""
            if unit not in {"minute", "hour", "day", "month", "year"}:
                raise GaugeError("umami", "bad_unit", f"unknown unit {unit!r}.", "use minute, hour, day, month or year.")
            s, e = _range(days, start, end)
            d = await get(f"/websites/{website_id}/pageviews",
                          {"startAt": s, "endAt": e, "unit": unit, "timezone": timezone})
            sessions = {p.get("x"): _num(p.get("y")) for p in d.get("sessions", [])}
            points = [Point(t=str(p.get("x")), pageviews=_num(p.get("y")), sessions=sessions.get(p.get("x"), 0))
                      for p in d.get("pageviews", [])]
            return Pageviews(website_id=website_id, start=_iso(s), end=_iso(e), unit=unit, points=points)

        @mcp.tool(annotations=READ_ONLY)
        async def umami_metrics(
            website_id: str, type: str = "path", days: int = 7, start: str | None = None, end: str | None = None,
            limit: int = 10, offset: int = 0, search: str | None = None,
        ) -> Metrics:
            """Ranked breakdown, highest count first. type: path (top pages), referrer, entry, exit, title,
            country, region, city, browser, os, device, language, screen, event, utmSource, hostname."""
            limit = max(1, min(limit, 500))
            s, e = _range(days, start, end)
            params = {"startAt": s, "endAt": e, "type": type, "limit": limit, "offset": offset}
            if search:
                params["search"] = search
            try:
                d = await get(f"/websites/{website_id}/metrics", params)
            except GaugeError as err:
                # Umami 2 called the page column "url"; Umami 3 calls it "path".
                if type != "path" or err.code != "api_error":
                    raise
                d = await get(f"/websites/{website_id}/metrics", {**params, "type": "url"})
            rows = [MetricRow(value=None if r.get("x") is None else str(r.get("x")), count=_num(r.get("y")))
                    for r in (d if isinstance(d, list) else d.get("data", []))]
            return Metrics(website_id=website_id, type=type, start=_iso(s), end=_iso(e),
                           limit=limit, offset=offset, rows=rows[:limit])
