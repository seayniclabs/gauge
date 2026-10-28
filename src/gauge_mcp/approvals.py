"""Local approval log: one JSON line per approval asked, granted or declined.

Written to $GAUGE_APPROVAL_LOG, or $XDG_STATE_HOME/gauge/approvals.jsonl
(~/.local/state/gauge/approvals.jsonl). It never leaves the machine. Message bodies are
not logged, only their length; recipients and subjects are, because those are what a
person approves.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Mapping


def log_path(env: Mapping[str, str]) -> Path:
    if env.get("GAUGE_APPROVAL_LOG"):
        return Path(env["GAUGE_APPROVAL_LOG"]).expanduser()
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "gauge" / "approvals.jsonl"


def _client_info(ctx: object | None) -> dict | None:
    """Extract client name and version from an MCP Context, if available."""
    if ctx is None:
        return None
    try:
        client_params = ctx.session.client_params  # type: ignore[union-attr]
    except (ValueError, AttributeError):
        return None
    if not client_params or not client_params.clientInfo:
        return None
    info = client_params.clientInfo
    return {"name": info.name, "version": info.version}


def record(
    env: Mapping[str, str],
    tool: str,
    decision: str,
    by: str | None,
    details: dict,
    ctx: object | None = None,
) -> None:
    """decision: requested | approved | declined | refused. Never raises: logging must not break a send."""
    try:
        path = log_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        client = _client_info(ctx)
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "decision": decision,
            "approved_by": by,
            **details,
        }
        if client:
            line["client"] = client
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass
