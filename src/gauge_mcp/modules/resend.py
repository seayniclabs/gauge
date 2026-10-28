"""Resend email: send, list sent mail, list sending domains."""

from __future__ import annotations

import re
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..config import Config
from .. import approvals
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
    approved_by: str | None = None  # "person" (asked directly), "allowlist", or "settings"
    note: str | None = None


class SendApproval(BaseModel):
    send: bool = Field(description="Send this email now?")


_ADDR = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")


def _address(value: str, field: str) -> str:
    """One bare addr-spec, lower-cased, or an error. No display names, lists, comments or control chars,
    so the address we check is exactly the address we send to."""
    v = value.strip()
    if not _ADDR.fullmatch(v):
        raise GaugeError("resend", "bad_address", f"{field} {value!r} is not a single plain email address.",
                         "pass each address on its own, like name@example.com, with no display name.")
    return v.lower()


def _allowed(address: str, allowlist: list[str]) -> bool:
    domain = address.rsplit("@", 1)[-1]
    return any(a == address or a == f"@{domain}" for a in allowlist)


def _excerpt(value: str | None, limit: int = 600) -> str:
    if not value:
        return "(none)"
    shown = value if len(value) <= limit else value[:limit] + "…"
    return f"{shown!r} ({len(value)} characters)"


def _can_ask_person(ctx: Context) -> bool:
    try:
        caps = ctx.session.client_params.capabilities  # type: ignore[union-attr]
    except (ValueError, AttributeError):
        return False
    return bool(caps and caps.elicitation)


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
                Setting("allowed_recipients", "Comma-separated addresses or @domains the assistant may send to "
                        "without asking each time, e.g. me@example.com,@example.com.", required=False),
                Setting("require_confirm", "true (default): a person approves each send. false: the assistant "
                        "sends on its own.", required=False),
            ],
            tools={
                "resend_send": "Send an email. Preview first; a person approves before it goes out.",
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
            ctx: Context, to: list[str], subject: str, text: str | None = None, html: str | None = None,
            reply_to: str | None = None, confirm: bool = False,
        ) -> SendResult:
            """Send an email from the configured sender. Without confirm=true it only returns a preview.
            With confirm=true the person is asked to approve it (unless the recipients are pre-approved
            in settings); the assistant cannot approve on their behalf."""
            if not to:
                raise GaugeError("resend", "bad_input", "at least one recipient is required.")
            if not (text or html):
                raise GaugeError("resend", "bad_input", "provide text, html, or both.")
            if any(ord(c) < 32 for c in subject):
                raise GaugeError("resend", "bad_input", "subject contains control characters.")
            to = [_address(r, "recipient") for r in to]
            reply_to = _address(reply_to, "reply_to") if reply_to else None
            address = module.require(config, "from")["from"]
            name = config.get("resend", "from_name")
            draft = EmailDraft(sender=f"{name} <{address}>" if name else address, to=to, subject=subject,
                               text=text, html=html, reply_to=reply_to)
            allowlist = [a.strip().lower() for a in (config.get("resend", "allowed_recipients") or "").split(",") if a.strip()]
            outside = [r for r in to if not _allowed(r, allowlist)] if allowlist else []
            if not confirm:
                note = "Preview only. Call again with confirm=true; the person will be asked to approve it."
                if outside:
                    note = f"Preview only. Not in GAUGE_RESEND_ALLOWED_RECIPIENTS: {', '.join(outside)}."
                return SendResult(sent=False, draft=draft, note=note)

            # The approval must come from the person or their settings, never from the model's own arguments.
            approved_by = None
            logged = {"to": to, "subject": subject, "body_chars": len(text or "") + len(html or "")}
            if not config.flag("resend", "require_confirm", True):
                approved_by = "settings"
            elif allowlist and not outside:
                approved_by = "allowlist"
            elif _can_ask_person(ctx):
                summary = "\n".join([
                    "Send this email?",
                    f"From: {draft.sender!r}",
                    f"To: {', '.join(repr(r) for r in to)}",
                    f"Reply-To: {reply_to!r}" if reply_to else "Reply-To: (none)",
                    f"Subject: {subject!r}",
                    f"Text: {_excerpt(text)}",
                    f"HTML: {_excerpt(html)}",
                ])
                approvals.record(config.env, "resend_send", "requested", None, logged, ctx)
                answer = await ctx.elicit(summary, SendApproval)
                if answer.action != "accept" or not answer.data.send:
                    approvals.record(config.env, "resend_send", "declined", "person", logged, ctx)
                    return SendResult(sent=False, draft=draft, note="The person declined. Nothing was sent.")
                approved_by = "person"
            else:
                approvals.record(config.env, "resend_send", "refused", None, logged, ctx)
                raise GaugeError(
                    "resend", "approval_unavailable",
                    "this MCP client cannot ask you to approve a send, and the recipients are not pre-approved.",
                    "set GAUGE_RESEND_ALLOWED_RECIPIENTS to the addresses or @domains it may send to, or "
                    "GAUGE_RESEND_REQUIRE_CONFIRM=false to let the assistant send on its own.",
                )

            body = {"from": draft.sender, "to": to, "subject": subject}
            for k in ("text", "html", "reply_to"):
                if getattr(draft, k):
                    body[k] = getattr(draft, k)
            approvals.record(config.env, "resend_send", "approved", approved_by, logged, ctx)
            data = await call("POST", "/emails", json=body)
            return SendResult(sent=True, id=data.get("id"), draft=draft, approved_by=approved_by)

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
