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
from html.parser import HTMLParser
from typing import Awaitable, Callable, Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
import idna
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..config import Config
from ..errors import GaugeError, not_configured
from . import ClientFactory, Module, Setting

DAY = 86_400
CHECKS = ("dns", "http", "tls", "domain", "content", "headers")
AI_CHECKS = ("robots_ai", "structured_data", "server_text", "llms_txt")
SEO_CHECKS = ("seo_title", "seo_description", "seo_indexable", "seo_canonical", "seo_sitemap", "seo_basics", "seo_social")
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
    # Combined verdict of the scored AI-readiness checks (robots_ai, structured_data, server_text); null if none ran.
    ai_readiness: Status | None = None
    seo: Status | None = None


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
        self._title_parts: list[list[str]] = []
        self._in_title = False
        self.meta: list[tuple[str, str, str]] = []
        self.canonicals: list[str] = []
        self.lang = ""
        self.h1 = 0
        self.images_without_alt = 0

    @property
    def titles(self) -> list[str]:
        return [" ".join("".join(parts).split()) for parts in self._title_parts]

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        if tag == "script":
            mime = (dict(attrs).get("type") or "").partition(";")[0]
            self.in_json_ld = mime.strip().lower() == "application/ld+json"
            if self.in_json_ld:
                self.blocks.append("")
        elif tag == "title" and "svg" not in self.hidden:  # an inline SVG's <title> labels the icon, not the page
            self._title_parts.append([])
            self._in_title = True
        elif tag == "meta":
            attributes = dict(attrs)
            self.meta.append(((attributes.get("name") or "").strip().lower(),
                              (attributes.get("property") or "").strip().lower(),
                              attributes.get("content") or ""))
        elif tag == "link":
            attributes = dict(attrs)
            if "canonical" in (attributes.get("rel") or "").lower().split():
                self.canonicals.append((attributes.get("href") or "").strip())
        elif tag == "html" and not self.lang:
            self.lang = (dict(attrs).get("lang") or "").strip()
        elif tag == "h1":
            self.h1 += 1
        elif tag == "img" and "alt" not in dict(attrs):
            self.images_without_alt += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json_ld = False
        elif tag == "title":
            self._in_title = False
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if self._in_title:
            self._title_parts[-1].append(data)
        if self.in_json_ld:
            self.blocks[-1] += data
        elif not self.hidden:
            self.text.append(data)


def _seo_title(page: _PageHTML) -> CheckResult:
    titles = page.titles
    count = len(titles)
    length = len(titles[0]) if titles else 0
    if not count:
        failure = "no <title>"
    elif count > 1:
        failure = f"{count} <title> tags"
    elif not 10 <= length <= 65:
        failure = f"title is {length} characters (aim for 10–65)"
    else:
        failure = None
    return CheckResult(check="seo_title", status="FAILING" if failure else "OK",
                       detail=failure or "title present", data={"count": count, "length": length})


def _seo_description(page: _PageHTML) -> CheckResult:
    description = next((content for name, _, content in page.meta if name == "description"), "")
    length = len(description) if description.strip() else 0
    if not length:
        failure = "no meta description"
    elif not 50 <= length <= 160:
        failure = f"description is {length} characters (aim for 50–160)"
    else:
        failure = None
    return CheckResult(check="seo_description", status="FAILING" if failure else "OK",
                       detail=failure or "meta description present", data={"length": length})


def _seo_canonical(page: _PageHTML, url: httpx.URL) -> CheckResult:
    data = {"same_host": False, "self": False}
    check = "seo_canonical"
    if not page.canonicals or not page.canonicals[0]:
        return CheckResult(check=check, status="FAILING", detail="no canonical link", data=data)
    try:
        canonical = url.join(page.canonicals[0])
    except (ValueError, httpx.InvalidURL):
        return CheckResult(check=check, status="FAILING", detail="canonical is not an HTTP(S) URL", data=data)
    if canonical.scheme not in {"http", "https"} or not canonical.host:
        return CheckResult(check=check, status="FAILING", detail="canonical is not an HTTP(S) URL", data=data)
    if canonical.raw_host.lower() != url.raw_host.lower():
        return CheckResult(check=check, status="FAILING", detail="canonical points to another host", data=data)

    target, final = urlsplit(str(canonical)), urlsplit(str(url))
    data["same_host"] = True
    data["self"] = target._replace(path=target.path.removesuffix("/"), fragment="") == final._replace(
        path=final.path.removesuffix("/"), fragment="",
    )
    return CheckResult(check=check, status="OK", detail="canonical link present", data=data)


def _seo_basics(page: _PageHTML) -> CheckResult:
    missing = []
    if not page.lang:
        missing.append("lang")
    if not any(name == "viewport" and content.strip() for name, _, content in page.meta):
        missing.append("viewport")
    if page.h1 != 1:
        missing.append("one-h1")
    if page.images_without_alt:
        missing.append("img-alt")
    detail = f"missing: {', '.join(missing)}" if missing else "page basics present"
    return CheckResult(check="seo_basics", status="FAILING" if missing else "OK", detail=detail,
                       data={"missing": missing, "h1": page.h1, "images_without_alt": page.images_without_alt})


def _seo_social(page: _PageHTML) -> CheckResult:
    present = {key for name, prop, content in page.meta if content.strip() for key in (name, prop)}
    missing = [key for key in ("og:title", "og:description", "og:image") if key not in present]
    detail = f"missing: {', '.join(missing)}" if missing else "Open Graph tags present"
    return CheckResult(check="seo_social", status="FAILING" if missing else "OK", detail=detail,
                       data={"missing": missing})


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
                    target_host = ascii_host(target.host)
                except (ValueError, httpx.InvalidURL):
                    yield CheckResult(check=check, status="FAILING", detail="redirect to a non-web address")
                    return
                if target.raw_host != current.raw_host:
                    dns = await _resolve(target_host, deps, settings)
                    if dns.status != "OK":
                        yield CheckResult(check=check, status=dns.status, detail=dns.detail)
                        return
                current = target


async def _check_http(
    url: httpx.URL, deps: Deps, settings: Settings, results: dict[str, CheckResult],
    expect_text: str | None, check_headers: bool, page_checks: set[str], check_indexable: bool,
) -> tuple[bool, str | None]:
    noindex = False
    try:
        async with _guarded_get(url, deps, settings, "http") as fetched:
            if isinstance(fetched, CheckResult):
                results["http"] = fetched
                return noindex, "page not fetched"
            response, elapsed, chain = fetched
            results["http"] = _http_result(url, response.url, response.status_code, elapsed, chain, settings)
            if check_headers or check_indexable:
                headers = {name.lower(): value for name, value in response.headers.items()}
                if check_headers:
                    results["headers"] = _check_headers(response.url, headers)
                if check_indexable:
                    noindex = bool(re.search(r"\bnoindex\b", headers.get("x-robots-tag", ""), re.IGNORECASE))
            body, truncated = await _read_body(response, settings.max_body)
            if expect_text is not None:
                results["content"] = _check_content(body, truncated, expect_text)
            page_error = None
            if not 200 <= response.status_code < 300:
                page_error = f"page answered {response.status_code}; not checked"
            if check_indexable or (page_checks and page_error is None):
                page = _PageHTML()
                page.feed(body.decode("utf-8", errors="replace"))
                page.close()
                if check_indexable:
                    noindex = noindex or any(
                        re.search(r"\bnoindex\b", content, re.IGNORECASE)
                        for name, _, content in page.meta if name in {"robots", "googlebot"}
                    )
            if page_error is not None:
                # An error or bot-block page is not the page people see: don't score it.
                for name in page_checks:
                    results[name] = CheckResult(check=name, status="UNKNOWN", detail=page_error)
            elif page_checks:
                if "structured_data" in page_checks:
                    results["structured_data"] = _structured_data(page.blocks)
                if "server_text" in page_checks:
                    results["server_text"] = _server_text(page.text)
                if "seo_canonical" in page_checks:
                    results["seo_canonical"] = _seo_canonical(page, response.url)
                for name, evaluate in (("seo_title", _seo_title), ("seo_description", _seo_description),
                                       ("seo_basics", _seo_basics), ("seo_social", _seo_social)):
                    if name in page_checks:
                        results[name] = evaluate(page)
            return noindex, page_error
    except httpx.HTTPError as exc:
        results["http"] = CheckResult(check="http", status="FAILING", detail=type(exc).__name__)
        return noindex, "page not fetched"


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


def _robots_allowed(body: bytearray, path: str, bots: list[str]) -> dict[str, bool]:
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
    return {bot: best.get(bot.lower(), fallback)[1] for bot in bots}


def _robots_ai(status: int, body: bytearray, path: str) -> CheckResult:
    if 400 <= status < 500 and status != 429:
        return CheckResult(check="robots_ai", status="OK", detail="no robots.txt; all crawlers allowed",
                           data={"robots_txt": False})
    if status != 200:
        return CheckResult(check="robots_ai", status="UNKNOWN", detail="could not read robots.txt")

    allowed = _robots_allowed(body, path, SEARCH_BOTS + TRAINING_BOTS)
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


_FileResponse = tuple[int, bytearray] | CheckResult


async def _fetch_site_file(url: httpx.URL, deps: Deps, settings: Settings, check: str) -> _FileResponse:
    try:
        async with _guarded_get(url, deps, settings, check) as fetched:
            if isinstance(fetched, CheckResult):
                return fetched
            response, _, _ = fetched
            status = response.status_code
            body = bytearray()
            if status == 200:
                body, _ = await _read_body(response, 500_000)
    except httpx.HTTPError:
        status, body = 0, bytearray()
    return status, body


def _seo_indexable(url: httpx.URL, page_state: tuple[bool, str | None], robots: _FileResponse) -> CheckResult:
    noindex, page_error = page_state
    robots_allows = None
    if not isinstance(robots, CheckResult):
        robots_status, body = robots
        if robots_status == 200:
            robots_allows = _robots_allowed(body, url.raw_path.decode("ascii"), ["Googlebot"])["Googlebot"]
        elif 400 <= robots_status < 500 and robots_status != 429:
            robots_allows = True
    if noindex:
        status, detail = "FAILING", "page says noindex"
    elif robots_allows is False:
        status, detail = "FAILING", "robots.txt blocks search engines from this page"
    elif robots_allows is None:
        status, detail = "UNKNOWN", "could not read robots.txt"
    elif page_error is not None:
        status, detail = "UNKNOWN", page_error
    else:
        status, detail = "OK", "indexable"
    return CheckResult(check="seo_indexable", status=status, detail=detail,
                       data={"noindex": noindex, "robots_allows": robots_allows})


def _sitemap_candidates(robots: _FileResponse) -> list[httpx.URL]:
    candidates = []
    if isinstance(robots, CheckResult) or robots[0] != 200:
        return candidates
    for line in robots[1].decode("utf-8-sig", errors="replace").splitlines():
        key, _, value = line.partition("#")[0].partition(":")
        if key.strip().lower() != "sitemap":
            continue
        try:
            candidate = httpx.URL(value.strip())
            _validate_web_url(candidate)
            ascii_host(candidate.host)
        except (ValueError, httpx.InvalidURL):
            continue
        candidates.append(candidate)
        if len(candidates) == 3:
            break
    return candidates


def _parse_sitemap(body: bytearray, source: str) -> CheckResult:
    check = "seo_sitemap"
    data = {"source": source}
    # Also detect declarations in UTF-16/UTF-32 before passing the original bytes to the XML parser.
    markup = body.replace(b"\x00", b"").lower().removeprefix(b"\xef\xbb\xbf").lstrip()
    if markup.startswith((b"<html", b"<!doctype html")):
        failure = "sitemap is an HTML page"
    elif b"<!doctype" in markup or b"<!entity" in markup:
        failure = "sitemap uses a DOCTYPE; not parsed"
    else:
        try:
            root = ElementTree.fromstring(body)
        except (ElementTree.ParseError, LookupError, ValueError):
            failure = "sitemap is not valid XML"
        else:
            child_name = {"urlset": "url", "sitemapindex": "sitemap"}.get(root.tag.rsplit("}", 1)[-1])
            if child_name:
                count = sum(child.tag.rsplit("}", 1)[-1] == child_name for child in root)
                return CheckResult(check=check, status="OK", detail="sitemap found",
                                   data={"source": source, "urls": count})
            failure = "sitemap root is not urlset or sitemapindex"
    return CheckResult(check=check, status="FAILING", detail=failure, data=data)


async def _seo_sitemap(url: httpx.URL, robots: _FileResponse, deps: Deps, settings: Settings) -> CheckResult:
    check = "seo_sitemap"
    candidates = _sitemap_candidates(robots)
    source = "robots.txt" if candidates else "/sitemap.xml"
    data = {"source": source}
    for candidate in candidates or [url.join("/sitemap.xml")]:
        if candidate.raw_host != url.raw_host:
            dns = await _resolve(ascii_host(candidate.host), deps, settings)
            if dns.status != "OK":
                if "private" in dns.detail:
                    return CheckResult(check=check, status=dns.status, detail=dns.detail, data=data)
                continue
        fetched = await _fetch_site_file(candidate, deps, settings, check)
        if isinstance(fetched, CheckResult):
            return fetched.model_copy(update={"data": data})
        status, body = fetched
        if status == 200:
            return _parse_sitemap(body, source)
        if 400 <= status < 500 and status != 429:
            return CheckResult(check=check, status="FAILING", detail="no sitemap found", data=data)
        if status:
            break
    return CheckResult(check=check, status="UNKNOWN", detail="could not read the sitemap", data=data)


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
    if "seo" in selected:
        selected.remove("seo")
        selected.update(SEO_CHECKS)
    unknown = selected.difference(CHECKS, AI_CHECKS, SEO_CHECKS)
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
    page_checks = selected & {"structured_data", "server_text", "seo_title", "seo_description",
                              "seo_canonical", "seo_basics", "seo_social"}
    http_checks = selected.intersection({"http", "content", "headers"}) | page_checks
    robots_checks = selected & {"robots_ai", "seo_indexable", "seo_sitemap"}
    check_indexable = "seo_indexable" in selected

    async def run_tls(address: str) -> None:
        results["tls"] = await _check_tls(normalized, host, address, deps, settings)

    async def run_domain() -> None:
        results["domain"] = await _check_domain(host, domain, deps, settings)

    async def run_http() -> tuple[bool, str | None]:
        page_state = await _check_http(
            normalized, deps, settings, results, expect_text if "content" in selected else None,
            "headers" in selected, page_checks, check_indexable,
        )
        for check in http_checks - {"http"}:
            if check not in results:
                results[check] = CheckResult(check=check, status="UNKNOWN", detail="page not fetched")
        return page_state

    async def run_llms_txt() -> None:
        fetched = await _fetch_site_file(normalized.join("/llms.txt"), deps, settings, "llms_txt")
        results["llms_txt"] = fetched if isinstance(fetched, CheckResult) else _llms_txt(*fetched)

    async def run_robots() -> _FileResponse:
        fetched = await _fetch_site_file(normalized.join("/robots.txt"), deps, settings, "robots_ai")
        if "robots_ai" in selected:
            results["robots_ai"] = fetched if isinstance(fetched, CheckResult) else _robots_ai(
                *fetched, normalized.raw_path.decode("ascii"),
            )
        return fetched

    async def run_indexable(page: Awaitable[tuple[bool, str | None]], robots: Awaitable[_FileResponse]) -> None:
        results["seo_indexable"] = _seo_indexable(normalized, await page, await robots)

    async def run_sitemap(robots: Awaitable[_FileResponse]) -> None:
        results["seo_sitemap"] = await _seo_sitemap(normalized, await robots, deps, settings)

    async def run_site() -> None:
        dns = results["dns"] = await _resolve(host, deps, settings)
        if dns.status != "OK":
            for check in http_checks | robots_checks | (selected & {"tls", "llms_txt"}):
                results[check] = CheckResult(check=check, status="UNKNOWN", detail="skipped: dns check did not pass")
            return

        async with asyncio.TaskGroup() as probes:
            if "tls" in selected:
                probes.create_task(run_tls(dns.data["addresses"][0]))
            page = probes.create_task(run_http()) if http_checks or check_indexable else None
            if "llms_txt" in selected:
                probes.create_task(run_llms_txt())
            if robots_checks:
                robots = probes.create_task(run_robots())
                if check_indexable and page is not None:
                    probes.create_task(run_indexable(page, robots))
                if "seo_sitemap" in selected:
                    probes.create_task(run_sitemap(robots))

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

    reported = [results[check] for check in (*CHECKS, *AI_CHECKS, *SEO_CHECKS) if check in selected]

    def verdict(names: set[str]) -> Status:
        statuses = {results[name].status for name in selected & names}
        return "FAILING" if "FAILING" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "OK"

    overall = verdict(set(CHECKS) - {"headers"})
    scored_ai = set(AI_CHECKS) - {"llms_txt"}
    ai_readiness = verdict(scored_ai) if selected & scored_ai else None
    seo = verdict(set(SEO_CHECKS)) if selected.intersection(SEO_CHECKS) else None
    headers = results.get("headers")
    return Report(url=str(normalized), overall=overall, headers=headers.status if headers else None, checks=reported,
                  ai_readiness=ai_readiness, seo=seo)


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
            """Check one site. Each check reports OK, FAILING or UNKNOWN (could not run). checks: any of dns, http, tls, domain, content, headers (default all; content needs expect_text). overall leaves out headers, which has its own verdict. Opt-in AI-readiness checks (AEO/GEO): robots_ai, structured_data, server_text, llms_txt, or "ai" for all four; they roll up into ai_readiness (llms_txt is reported, not scored) and never change overall. Opt-in SEO checks: seo_title, seo_description, seo_indexable, seo_canonical, seo_sitemap, seo_basics, seo_social, or "seo" for all seven; they roll up into seo and never change overall."""
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
