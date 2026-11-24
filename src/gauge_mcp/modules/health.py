"""Read-only health checks for public websites."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Literal
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
import idna
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from ..config import Config
from ..errors import GaugeError, not_configured
from . import ClientFactory, Module, Setting

DAY = 86_400
CHECKS = ("dns", "http", "tls", "domain", "content", "headers")
AI_CHECKS = ("robots_ai", "structured_data", "server_text", "llms_txt")
SEARCH_BOTS = ["OAI-SearchBot", "PerplexityBot", "Claude-SearchBot"]
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Google-Extended"]
STATUSES = ("OK", "FAILING", "UNKNOWN")
WEB_SCHEMES = {"http", "https"}
REDIRECTS = {301, 302, 303, 307, 308}
SHARED_NETWORK = ipaddress.ip_network("100.64.0.0/10")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

Status = Literal["OK", "FAILING", "UNKNOWN"]


class NoSuchHost(Exception):
    pass


class ResolverError(Exception):
    pass


@dataclass
class TlsInfo:
    not_after: float | None = None
    error: str | None = None
    verify_code: int | None = None


@dataclass
class Settings:
    tls_min_days: int = 14
    domain_min_days: int = 30
    max_ms: int = 5000
    timeout_s: float = 10.0
    allow_private: bool = False
    max_body: int = 2_000_000
    max_redirects: int = 5
    deadline_s: float = 60.0


@dataclass
class Deps:
    resolve: Callable[[str], Awaitable[list[str]]]
    tls: Callable[[str, str, int, float], Awaitable[TlsInfo]]
    client_factory: ClientFactory
    now: Callable[[], float]
    clock: Callable[[], float]

    @classmethod
    def real(cls) -> Deps:
        async def resolve(host: str) -> list[str]:
            loop = asyncio.get_running_loop()
            try:
                records = await asyncio.wait_for(loop.getaddrinfo(host, None), timeout=5)
            except socket.gaierror as exc:
                missing = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
                if exc.errno in missing:
                    raise NoSuchHost(host) from exc
                raise ResolverError(host) from exc
            except TimeoutError as exc:
                raise ResolverError(host) from exc
            return list(dict.fromkeys(record[4][0] for record in records))

        async def tls(host: str, ip: str, port: int, timeout: float) -> TlsInfo:
            context = ssl.create_default_context()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port, ssl=context, server_hostname=host),
                    timeout=timeout,
                )
            except ssl.SSLCertVerificationError as exc:
                return TlsInfo(error=exc.verify_message or str(exc), verify_code=exc.verify_code)
            try:
                cert = writer.get_extra_info("ssl_object").getpeercert()
                return TlsInfo(not_after=ssl.cert_time_to_seconds(cert["notAfter"]))
            finally:
                writer.close()

        def client_factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False)

        return cls(resolve, tls, client_factory, time.time, time.monotonic)


class CheckResult(BaseModel):
    check: str
    status: Status
    detail: str
    data: dict = Field(default_factory=dict)


class Report(BaseModel):
    url: str
    overall: Status
    headers: Status | None
    checks: list[CheckResult]
    # Serialize the optional verdict while preserving the published health tool schema.
    ai_readiness: SkipJsonSchema[Status | None] = None


class Sweep(BaseModel):
    reports: list[Report]
    counts: dict[str, int]


def _ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def ascii_host(host: str) -> str:
    if _ip_address(host) is not None:
        return host
    return idna.encode(host, uts46=True).decode().lower()


def parse_rdap_expiry(obj: object) -> int | None:
    if not isinstance(obj, dict) or not isinstance(events := obj.get("events"), list):
        return None
    for event in events:
        if not isinstance(event, dict) or event.get("eventAction") != "expiration":
            continue
        value = event.get("eventDate")
        if not isinstance(value, str):
            continue
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            date = date.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None
        if 2000 <= date.year <= 2100:
            return (date - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)
        return None
    return None


def _validate_web_url(url: httpx.URL) -> None:
    if url.scheme not in WEB_SCHEMES or not url.host or url.userinfo:
        raise ValueError("expected an HTTP(S) URL without credentials")
    if url.port is not None and not 0 <= url.port <= 65535:
        raise ValueError("invalid port")
    ascii_host(url.host)


def _normalize_url(value: str) -> httpx.URL:
    if not isinstance(value, str):
        raise GaugeError("health", "bad_url", "expected an HTTP or HTTPS URL without userinfo.")
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        if len(value) > 500 or urlsplit(value).username is not None:
            raise ValueError("invalid URL length or userinfo")
        url = httpx.URL(value)
        _validate_web_url(url)
        return url.copy_with(raw_path=url.raw_path)
    except (ValueError, httpx.InvalidURL):
        raise GaugeError("health", "bad_url", "expected an HTTP or HTTPS URL without userinfo.") from None


def _blocked(address: str, settings: Settings) -> bool:
    if settings.allow_private:
        return False
    ip = _ip_address(address)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or ip in SHARED_NETWORK
    )


async def _resolve(host: str, deps: Deps, settings: Settings) -> CheckResult:
    try:
        addresses = [host] if _ip_address(host) is not None else await deps.resolve(host)
    except NoSuchHost:
        return CheckResult(check="dns", status="FAILING", detail="no such host")
    except ResolverError:
        return CheckResult(check="dns", status="UNKNOWN", detail="DNS lookup failed")
    if any(_blocked(address, settings) for address in addresses):
        return CheckResult(check="dns", status="FAILING", detail="resolves to a private address (blocked)")
    if not addresses:
        return CheckResult(check="dns", status="UNKNOWN", detail="DNS lookup returned no addresses")
    return CheckResult(check="dns", status="OK", detail="host resolved", data={"addresses": addresses[:10]})


def _connected_to_private(response: httpx.Response, settings: Settings) -> bool:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return False
    address = stream.get_extra_info("server_addr")
    if isinstance(address, tuple):
        address = address[0] if address else None
    return isinstance(address, str) and _blocked(address, settings)


def _http_result(url: httpx.URL, final: httpx.URL, status: int, elapsed: float,
                 chain: list[str], settings: Settings) -> CheckResult:
    gated = status in {401, 403, 429}
    verdict: Status = "OK" if 200 <= status < 400 or gated else "FAILING"
    detail = f"answered {status}"
    if gated:
        detail += " (access-controlled)"
    if elapsed > settings.max_ms:
        verdict = "FAILING"
        detail += " (slow)"
    return CheckResult(
        check="http", status=verdict, detail=detail,
        data={
            "status": status, "ms": int(elapsed), "chain": chain, "final_url": str(final),
            "cross_host": final.raw_host != url.raw_host, "gated": gated,
        },
    )


class _PageHTML(HTMLParser):
    hidden_tags = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self.text: list[str] = []
        self.hidden: list[str] = []
        self.in_json_ld = False

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        if tag == "script":
            mime = (dict(attrs).get("type") or "").partition(";")[0]
            self.in_json_ld = mime.strip().lower() == "application/ld+json"
            if self.in_json_ld:
                self.blocks.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json_ld = False
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if self.in_json_ld:
            self.blocks[-1] += data
        elif not self.hidden:
            self.text.append(data)


def _collect_types(node: object, types: list[str]) -> None:
    if not isinstance(node, dict) or len(types) == 20:
        return
    values = node.get("@type", [])
    if isinstance(values, str):
        values = [values]
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", value) and value not in types:
                types.append(value)
                if len(types) == 20:
                    return


def _structured_data(blocks: list[str]) -> CheckResult:
    types: list[str] = []
    data = {"blocks": len(blocks), "types": types}
    if not blocks:
        return CheckResult(check="structured_data", status="FAILING", detail="no JSON-LD structured data", data=data)
    for number, block in enumerate(blocks, 1):
        try:
            document = json.loads(block)
        except (ValueError, RecursionError):
            return CheckResult(check="structured_data", status="FAILING", detail=f"invalid JSON-LD in block {number}",
                               data=data)
        for node in document if isinstance(document, list) else [document]:
            _collect_types(node, types)
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                for member in node["@graph"]:
                    _collect_types(member, types)
    return CheckResult(check="structured_data", status="OK", detail=f"{len(blocks)} JSON-LD blocks", data=data)


def _server_text(text: list[str]) -> CheckResult:
    words = len(re.findall(r"\w+", " ".join(text)))
    detail = f"{words} words of text in the HTML"
    if words < 150:
        detail = f"only {detail}; the page may be built by JavaScript"
    return CheckResult(check="server_text", status="OK" if words >= 150 else "FAILING", detail=detail,
                       data={"words": words})


@asynccontextmanager
async def _guarded_get(url: httpx.URL, deps: Deps, settings: Settings, check: str):
    """Yield the final streaming response or a guard failure. The caller has checked the origin DNS."""
    current = url
    chain = []
    async with deps.client_factory() as client:
        start = deps.clock()
        for hop in range(settings.max_redirects + 1):
            chain.append(CONTROL_CHARS.sub("", str(current))[:200])
            async with client.stream(
                "GET", current, headers={"User-Agent": "gauge-mcp health check"}, follow_redirects=False,
            ) as response:
                if _connected_to_private(response, settings):
                    yield CheckResult(check=check, status="FAILING", detail="connected to a private address (blocked)")
                    return
                location = response.headers.get("location")
                if response.status_code not in REDIRECTS or not location:
                    yield response, (deps.clock() - start) * 1000, chain
                    return
                if hop == settings.max_redirects:
                    yield CheckResult(check=check, status="FAILING", detail="too many redirects")
                    return
                try:
                    target = response.url.join(location)
                    _validate_web_url(target)
                    if urlsplit(location).username is not None:
                        raise ValueError("redirect contains userinfo")
                except (ValueError, httpx.InvalidURL):
                    yield CheckResult(check=check, status="FAILING", detail="redirect to a non-web address")
                    return
                if target.raw_host != current.raw_host:
                    dns = await _resolve(ascii_host(target.host), deps, settings)
                    if dns.status != "OK":
                        yield CheckResult(check=check, status=dns.status, detail=dns.detail)
                        return
                current = target


async def _check_http(
    url: httpx.URL, deps: Deps, settings: Settings, results: dict[str, CheckResult],
    expect_text: str | None, check_headers: bool, page_checks: set[str],
) -> None:
    try:
        async with _guarded_get(url, deps, settings, "http") as fetched:
            if isinstance(fetched, CheckResult):
                results["http"] = fetched
                return
            response, elapsed, chain = fetched
            results["http"] = _http_result(url, response.url, response.status_code, elapsed, chain, settings)
            if check_headers:
                headers = {name.lower(): value for name, value in response.headers.items()}
                results["headers"] = _check_headers(response.url, headers)
            body, truncated = await _read_body(response, settings.max_body)
            if expect_text is not None:
                results["content"] = _check_content(body, truncated, expect_text)
            if page_checks:
                page = _PageHTML()
                page.feed(body.decode("utf-8", errors="replace"))
                page.close()
                if "structured_data" in page_checks:
                    results["structured_data"] = _structured_data(page.blocks)
                if "server_text" in page_checks:
                    results["server_text"] = _server_text(page.text)
    except httpx.HTTPError as exc:
        results["http"] = CheckResult(check="http", status="FAILING", detail=type(exc).__name__)


async def _read_body(response: httpx.Response, limit: int) -> tuple[bytearray, bool]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit - len(body)
        if len(chunk) > remaining:
            body.extend(chunk[:remaining])
            return body, True
        body.extend(chunk)
    return body, False


def _check_content(body: bytearray, truncated: bool, expect_text: str) -> CheckResult:
    found = expect_text in body.decode("utf-8", errors="replace")
    return CheckResult(
        check="content", status="OK" if found else "FAILING",
        detail="expected text found" if found else "expected text not found",
        data={"found": found, "truncated": truncated},
    )


def _check_headers(url: httpx.URL, headers: dict[str, str]) -> CheckResult:
    hsts = re.search(
        r'(?:^|;)\s*max-age\s*=\s*(?:[0-9]*[1-9][0-9]*|"[0-9]*[1-9][0-9]*")\s*(?:;|$)',
        headers.get("strict-transport-security", ""), re.IGNORECASE,
    )
    frame_ancestors = re.search(
        r"(?:^|[;,])\s*frame-ancestors(?=\s|[;,]|$)", headers.get("content-security-policy", ""), re.IGNORECASE,
    )
    required = {
        "strict-transport-security": url.scheme == "https" and hsts is not None,
        "x-content-type-options": headers.get("x-content-type-options", "").strip().lower() == "nosniff",
        "frame-protection": headers.get("x-frame-options", "").strip().upper() in {"DENY", "SAMEORIGIN"}
        or frame_ancestors is not None,
        "referrer-policy": "referrer-policy" in headers,
    }
    present = [name for name, enabled in required.items() if enabled]
    missing = [name for name, enabled in required.items() if not enabled]
    return CheckResult(
        check="headers", status="FAILING" if missing else "OK",
        detail="missing: " + ", ".join(missing) if missing else "security headers present",
        data={"present": present, "missing": missing},
    )


def _expiry_date(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


async def _check_tls(url: httpx.URL, host: str, address: str, deps: Deps, settings: Settings) -> CheckResult:
    port = url.port if url.scheme == "https" and url.port is not None else 443
    try:
        info = await deps.tls(host, address, port, settings.timeout_s)
    except (OSError, TimeoutError):
        return CheckResult(check="tls", status="UNKNOWN", detail=f"could not open a TLS connection on port {port}")
    if info.error:
        return CheckResult(check="tls", status="FAILING", detail=info.error[:200])
    if info.not_after is None:
        return CheckResult(check="tls", status="UNKNOWN", detail="certificate expiry unavailable")
    days_left = int((info.not_after - deps.now()) // DAY)
    return CheckResult(
        check="tls", status="FAILING" if days_left < settings.tls_min_days else "OK",
        detail=f"certificate expires in {days_left} days",
        data={"days_left": days_left, "expires": _expiry_date(info.not_after)},
    )


def _domain_result(obj: object, candidate: str, deps: Deps, settings: Settings) -> CheckResult:
    statuses = obj.get("status") if isinstance(obj, dict) else None
    if isinstance(statuses, list):
        for status in statuses:
            if isinstance(status, str) and "hold" in status.lower():
                return CheckResult(check="domain", status="FAILING", detail=f"domain is on hold ({status.lower()})")
    expiry = parse_rdap_expiry(obj)
    if expiry is None:
        return CheckResult(check="domain", status="UNKNOWN", detail="registry publishes no expiry date")
    seconds = expiry / 1000
    days_left = int((seconds - deps.now()) // DAY)
    return CheckResult(
        check="domain", status="FAILING" if days_left < settings.domain_min_days else "OK",
        detail=f"domain expires in {days_left} days",
        data={"domain": candidate, "days_left": days_left, "expires": _expiry_date(seconds)},
    )


async def _check_domain(host: str, domain: str | None, deps: Deps, settings: Settings) -> CheckResult:
    if domain is not None:
        candidates = [domain]
    elif _ip_address(host) is not None:
        return CheckResult(check="domain", status="UNKNOWN", detail="no domain for an IP address")
    else:
        labels = host.rstrip(".").split(".")
        candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]

    if candidates:
        try:
            async with deps.client_factory() as client:
                for candidate in candidates:
                    response = await client.get(
                        f"https://rdap.org/domain/{candidate}", headers={"Accept": "application/rdap+json"},
                        follow_redirects=True, timeout=settings.timeout_s,
                    )
                    # Not this name: registries answer 404, and some (.org) 400, for a name below the
                    # registered domain. Rate limits and server errors mean the lookup itself failed.
                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        continue
                    if response.status_code == 200:
                        return _domain_result(response.json(), candidate, deps, settings)
                    return CheckResult(check="domain", status="UNKNOWN", detail="registry lookup failed")
        except (httpx.HTTPError, ValueError):
            return CheckResult(check="domain", status="UNKNOWN", detail="registry lookup failed")
    return CheckResult(check="domain", status="UNKNOWN", detail="domain not found in RDAP")


def _robots_groups(body: bytearray):
    agents: set[str] = set()
    rules: list[tuple[str, bool]] = []
    for line in body.decode("utf-8-sig", errors="replace").splitlines():
        key, separator, value = line.partition("#")[0].partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                yield agents, rules
                agents, rules = set(), []
            agents.add(value.lower())
        elif agents and key in {"allow", "disallow"}:
            rules.append((value, key == "allow"))
    if agents:
        yield agents, rules


def _robots_path(value: str) -> str:
    # RFC 9309 compares unreserved ASCII octets decoded and non-ASCII octets percent-encoded.
    def normalize(match: re.Match) -> str:
        token = match.group()
        if token.startswith("%"):
            char = chr(int(token[1:], 16))
            if char.isascii() and (char.isalnum() or char in "-._~"):
                return char
            return token.upper()
        return "".join(f"%{octet:02X}" for octet in token.encode())

    return re.sub(r"%[0-9a-fA-F]{2}|[^\x00-\x7f]+", normalize, value)


def _robots_matches(pattern: str, path: str) -> bool:
    # Match literal spans in order so a file with many wildcards cannot cause regex backtracking.
    anchored = pattern.endswith("$")
    parts = pattern.removesuffix("$").split("*")
    if len(parts) == 1:
        return path == parts[0] if anchored else path.startswith(parts[0])
    if not path.startswith(parts[0]):
        return False
    start, end = len(parts[0]), len(path)
    if anchored:
        suffix = parts.pop()
        if not path.endswith(suffix, start):
            return False
        end -= len(suffix)
    for part in parts[1:]:
        position = path.find(part, start, end)
        if position == -1:
            return False
        start = position + len(part)
    return True


def _robots_ai(status: int, body: bytearray, path: str) -> CheckResult:
    if 400 <= status < 500 and status != 429:
        return CheckResult(check="robots_ai", status="OK", detail="no robots.txt; all crawlers allowed",
                           data={"robots_txt": False})
    if status != 200:
        return CheckResult(check="robots_ai", status="UNKNOWN", detail="could not read robots.txt")

    path = _robots_path(path)
    best: dict[str, tuple[int, bool]] = {}
    default = (0, True)
    for agents, rules in _robots_groups(body):
        winner = default
        for pattern, allow in rules:
            pattern = _robots_path(pattern)
            if pattern and _robots_matches(pattern, path):
                winner = max(winner, (len(pattern), allow))
        for agent in agents:
            best[agent] = max(best.get(agent, default), winner)

    fallback = best.get("*", default)
    allowed = {bot: best.get(bot.lower(), fallback)[1] for bot in SEARCH_BOTS + TRAINING_BOTS}
    blocked = [bot for bot in SEARCH_BOTS if not allowed[bot]]
    detail = f"robots.txt blocks AI search crawlers: {', '.join(blocked)}" if blocked else "AI search crawlers allowed"
    return CheckResult(check="robots_ai", status="FAILING" if blocked else "OK", detail=detail,
                       data={"robots_txt": True, "allowed": allowed, "blocked_search": blocked})


def _llms_txt(status: int, body: bytearray) -> CheckResult:
    if status != 200 and not 400 <= status < 500:
        return CheckResult(check="llms_txt", status="UNKNOWN", detail="could not read llms.txt (not scored)")
    stripped = body.strip()
    present = status == 200 and bool(stripped) and not stripped.startswith(b"<")
    detail = "llms.txt present" if present else "no llms.txt"
    return CheckResult(check="llms_txt", status="OK" if present else "FAILING",
                       detail=f"{detail} (informational, not scored)", data={"present": present})


async def _check_ai_file(check: str, url: httpx.URL, deps: Deps, settings: Settings) -> CheckResult:
    file_url = url.join("/robots.txt" if check == "robots_ai" else "/llms.txt")
    try:
        async with _guarded_get(file_url, deps, settings, check) as fetched:
            if isinstance(fetched, CheckResult):
                return fetched
            response, _, _ = fetched
            status = response.status_code
            body = bytearray()
            if status == 200:
                body, _ = await _read_body(response, 500_000)
    except httpx.HTTPError:
        status, body = 0, bytearray()
    if check == "robots_ai":
        return _robots_ai(status, body, url.raw_path.decode("ascii"))
    return _llms_txt(status, body)


async def run_checks(
    url: str, *, deps: Deps, settings: Settings, expect_text: str | None = None,
    domain: str | None = None, checks: list[str] | None = None,
) -> Report:
    normalized = _normalize_url(url)
    host = ascii_host(normalized.host)
    selected = set(CHECKS if checks is None else checks)
    if "ai" in selected:
        selected.remove("ai")
        selected.update(AI_CHECKS)
    unknown = selected.difference(CHECKS, AI_CHECKS)
    if unknown:
        raise GaugeError("health", "bad_input", f"unknown checks: {', '.join(sorted(unknown))}")
    if expect_text is None:
        selected.discard("content")
    if "domain" in selected and domain is not None:
        try:
            domain = ascii_host(domain)
        except ValueError:
            raise GaugeError("health", "bad_input", "expected a valid domain name.") from None

    results: dict[str, CheckResult] = {}
    page_checks = selected & {"structured_data", "server_text"}
    http_checks = selected.intersection({"http", "content", "headers"}) | page_checks
    file_checks = selected & {"robots_ai", "llms_txt"}

    async def run_tls(address: str) -> None:
        results["tls"] = await _check_tls(normalized, host, address, deps, settings)

    async def run_domain() -> None:
        results["domain"] = await _check_domain(host, domain, deps, settings)

    async def run_http() -> None:
        await _check_http(
            normalized, deps, settings, results, expect_text if "content" in selected else None,
            "headers" in selected, page_checks,
        )
        for check in http_checks - {"http"}:
            if check not in results:
                results[check] = CheckResult(check=check, status="UNKNOWN", detail="page not fetched")

    async def run_ai_file(check: str) -> None:
        results[check] = await _check_ai_file(check, normalized, deps, settings)

    async def run_site() -> None:
        dns = results["dns"] = await _resolve(host, deps, settings)
        if dns.status != "OK":
            for check in http_checks | file_checks | (selected & {"tls"}):
                results[check] = CheckResult(check=check, status="UNKNOWN", detail="skipped: dns check did not pass")
            return

        async with asyncio.TaskGroup() as probes:
            if "tls" in selected:
                probes.create_task(run_tls(dns.data["addresses"][0]))
            if http_checks:
                probes.create_task(run_http())
            for check in file_checks:
                probes.create_task(run_ai_file(check))

    async def run() -> None:
        async with asyncio.TaskGroup() as probes:
            probes.create_task(run_site())
            if "domain" in selected:
                probes.create_task(run_domain())

    try:
        await asyncio.wait_for(run(), timeout=settings.deadline_s)
    except TimeoutError:
        for check in selected - results.keys():
            results[check] = CheckResult(check=check, status="UNKNOWN", detail="deadline reached")

    reported = [results[check] for check in (*CHECKS, *AI_CHECKS) if check in selected]

    def verdict(names: set[str]) -> Status:
        statuses = {results[name].status for name in selected & names}
        return "FAILING" if "FAILING" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "OK"

    overall = verdict(set(CHECKS) - {"headers"})
    ai_readiness = verdict(set(AI_CHECKS) - {"llms_txt"}) if selected.intersection(AI_CHECKS) else None
    headers = results.get("headers")
    return Report(url=str(normalized), overall=overall, headers=headers.status if headers else None, checks=reported,
                  ai_readiness=ai_readiness)


def _targets(config: Config) -> list[dict]:
    targets = config.raw("health", "targets")
    if isinstance(targets, str):
        targets = [{"url": url.strip()} for url in targets.split(",") if url.strip()]
    if targets is None or targets == []:
        raise not_configured("health", ["GAUGE_HEALTH_TARGETS"])
    if not isinstance(targets, list):
        raise GaugeError("health", "bad_input", "expected a list of health targets.")
    if len(targets) > 25:
        raise GaugeError("health", "bad_input", "at most 25 targets")
    if any(not isinstance(target, dict) for target in targets):
        raise GaugeError("health", "bad_input", "expected each health target to be a table.")
    if any(
        target.get(key) is not None and not isinstance(target[key], str)
        for target in targets for key in ("expect", "domain")
    ):
        raise GaugeError("health", "bad_input", "target expect and domain values must be strings.")
    return targets


class HealthModule(Module):
    def __init__(self) -> None:
        super().__init__(
            name="health",
            default_enabled=False,
            summary="Site health checks: DNS, uptime and response time, TLS and domain expiry, "
                    "page content, security headers.",
            settings=[
                Setting("targets", "Sites for health_sweep, as URLs or config tables.", required=False),
                Setting("tls_min_days", "Minimum days before TLS certificate expiry.", required=False),
                Setting("domain_min_days", "Minimum days before domain expiry.", required=False),
                Setting("max_ms", "Maximum HTTP response time in milliseconds.", required=False),
                Setting("allow_private", "Allow checks of private IP addresses.", required=False),
            ],
            tools={
                "health_check": "Check one site: DNS, HTTP status and response time, TLS and domain expiry, "
                                "page text, security headers.",
                "health_sweep": "Check every site listed in settings.",
            },
        )

    def register(
        self, mcp: FastMCP, config: Config, client_factory: ClientFactory, *, deps: Deps | None = None,
    ) -> None:
        dependencies = deps if deps is not None else Deps.real()
        defaults = Settings()
        def number(key: str, default: int) -> int:
            # A bad value falls back to the default: settings must never stop the server starting.
            try:
                value = int(config.get(self.name, key) or default)
            except ValueError:
                return default
            return value if value >= 0 else default

        settings = Settings(
            tls_min_days=number("tls_min_days", defaults.tls_min_days),
            domain_min_days=number("domain_min_days", defaults.domain_min_days),
            max_ms=number("max_ms", defaults.max_ms),
            allow_private=config.flag(self.name, "allow_private", defaults.allow_private),
        )

        @mcp.tool(annotations=READ_ONLY)
        async def health_check(
            url: str, expect_text: str | None = None, domain: str | None = None, checks: list[str] | None = None,
        ) -> Report:
            """Check one site. Each check reports OK, FAILING or UNKNOWN (could not run). checks: any of dns, http, tls, domain, content, headers (default all; content needs expect_text). overall leaves out headers, which has its own verdict."""
            return await run_checks(
                url, deps=dependencies, settings=settings, expect_text=expect_text, domain=domain, checks=checks,
            )

        @mcp.tool(annotations=READ_ONLY)
        async def health_sweep() -> Sweep:
            """Check every target in settings (GAUGE_HEALTH_TARGETS, or [health] targets in the config file), at most 25, five at a time."""
            targets = _targets(config)
            semaphore = asyncio.Semaphore(5)

            async def check(target: dict) -> Report:
                url = target.get("url")
                async with semaphore:
                    try:
                        return await health_check(url, expect_text=target.get("expect"), domain=target.get("domain"))
                    except GaugeError as exc:
                        if exc.code != "bad_url":
                            raise
                        return Report(
                            url=url if isinstance(url, str) else "", overall="UNKNOWN", headers=None,
                            checks=[CheckResult(check="input", status="UNKNOWN", detail=str(exc))],
                        )

            reports = await asyncio.gather(*(check(target) for target in targets))
            counts = dict.fromkeys(STATUSES, 0)
            for report in reports:
                counts[report.overall] += 1
            return Sweep(reports=reports, counts=counts)
