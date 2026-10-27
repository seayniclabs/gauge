"""Resend email: send, list sent mail, list sending domains."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..config import Config
from ..errors import GaugeError
from . import ClientFactory, Module, Setting

API = "https://api.resend.com"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
SENDS_EMAIL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


class EmailDraft(BaseModel):
    sender: str
    to: list[str]
    subject: str
    text: str | None = None
    html: str | None = None
    reply_to: str | None = None


class SendResult(BaseModel):
    sent: bool
    id: str | None = None
    draft: EmailDraft
    note: str | None = None


class SentEmail(BaseModel):
    id: str
    to: list[str]
    sender: str | None = None
    subject: str | None = None
    created_at: str | None = None
    last_event: str | None = None


class EmailPage(BaseModel):
    emails: list[SentEmail]
    has_more: bool
    next_after: str | None = None


class Domain(BaseModel):
    id: str | None = None
    name: str
    status: str | None = None
    region: str | None = None


class Domains(BaseModel):
    domains: list[Domain]


class ResendModule(Module):
    def __init__(self) -> None:
        super().__init__(
            name="resend",
            summary="Email through your Resend account: send (preview first), list sent mail, list domains.",
            settings=[
                Setting("api_key", "Resend API key. A sending-only key is enough for resend_send.", secret=True),
                Setting("from", "Sender address on a verified domain, e.g. hello@example.com. Needed to send.",
                        required=False),
                Setting("from_name", "Display name for the sender, e.g. Example Team.", required=False),
                Setting("require_confirm", "true (default): resend_send previews until called with confirm=true.",
                        required=False),
            ],
            tools={
                "resend_send": "Send an email. Returns a preview unless confirm=true.",
                "resend_emails": "List sent emails (cursor-paged).",
                "resend_domains": "List sending domains and their verification status.",
            },
        )

    def register(self, mcp: FastMCP, config: Config, client_factory: ClientFactory) -> None:
        module = self

        async def call(method: str, path: str, **kwargs: Any) -> Any:
            key = module.require(config, "api_key")["api_key"]
            try:
                async with client_factory() as client:
                    r = await client.request(method, f"{API}{path}",
                                             headers={"Authorization": f"Bearer {key}"}, **kwargs)
            except httpx.HTTPError as e:
                raise GaugeError("resend", "unreachable", f"could not reach Resend ({type(e).__name__}).")
            if r.status_code in (401, 403):
                raise GaugeError("resend", "auth_failed", f"Resend returned {r.status_code}.",
                                 "check GAUGE_RESEND_API_KEY and that the key's permission allows this call.")
            if r.status_code == 429:
                raise GaugeError("resend", "rate_limited", "Resend rate limit reached.", "wait and retry.")
            if r.status_code >= 400:
                try:
                    message = r.json().get("message", r.text)
                except ValueError:
                    message = r.text
                raise GaugeError("resend", "api_error", f"Resend returned {r.status_code}: {str(message)[:200]}")
            return r.json()

        @mcp.tool(annotations=SENDS_EMAIL)
        async def resend_send(
            to: list[str], subject: str, text: str | None = None, html: str | None = None,
            reply_to: str | None = None, confirm: bool = False,
        ) -> SendResult:
            """Send an email from the configured sender. Without confirm=true this only returns a preview,
            so a person can check recipients and content first."""
            if not to:
                raise GaugeError("resend", "bad_input", "at least one recipient is required.")
            if not (text or html):
                raise GaugeError("resend", "bad_input", "provide text, html, or both.")
            address = module.require(config, "from")["from"]
            name = config.get("resend", "from_name")
            draft = EmailDraft(sender=f"{name} <{address}>" if name else address, to=to, subject=subject,
                               text=text, html=html, reply_to=reply_to)
            if config.flag("resend", "require_confirm", True) and not confirm:
                return SendResult(sent=False, draft=draft,
                                  note="Preview only. Call again with confirm=true to send.")
            body = {"from": draft.sender, "to": to, "subject": subject}
            for k in ("text", "html", "reply_to"):
                if getattr(draft, k):
                    body[k] = getattr(draft, k)
            data = await call("POST", "/emails", json=body)
            return SendResult(sent=True, id=data.get("id"), draft=draft)

        @mcp.tool(annotations=READ_ONLY)
        async def resend_emails(limit: int = 20, after: str | None = None) -> EmailPage:
            """List sent emails, newest first. Page with after=<next_after from the previous page>."""
            params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
            if after:
                params["after"] = after
            d = await call("GET", "/emails", params=params)
            emails = [SentEmail(id=e["id"], to=list(e.get("to") or []), sender=e.get("from"),
                                subject=e.get("subject"), created_at=e.get("created_at"),
                                last_event=e.get("last_event"))
                      for e in d.get("data", [])]
            has_more = bool(d.get("has_more"))
            return EmailPage(emails=emails, has_more=has_more,
                             next_after=emails[-1].id if has_more and emails else None)

        @mcp.tool(annotations=READ_ONLY)
        async def resend_domains() -> Domains:
            """List sending domains and whether each is verified."""
            d = await call("GET", "/domains")
            return Domains(domains=[Domain(id=x.get("id"), name=x["name"], status=x.get("status"),
                                           region=x.get("region")) for x in d.get("data", [])])
