# Gauge

Gauge is an MCP server for people running a small website. It lets an AI assistant read your site's analytics and send email, from one install.

It is a **bundle**: one server made of modules. You switch on the modules you use. New modules arrive switched off, so an update never changes what your assistant can do until you choose to.

| Module | What it does | Status |
|---|---|---|
| `umami` | Web analytics from [Umami](https://umami.is), self-hosted or Umami Cloud: sites, totals, time series, top pages, referrers, countries, browsers | 0.1 |
| `resend` | Email through [Resend](https://resend.com): send (preview first), list sent mail, list sending domains | 0.1 |

Gauge works with any MCP client: Claude Desktop, Claude Code, Cursor, VS Code, Sonique, Hermes, OpenClaw and others. It runs on your computer and stores nothing; your data stays in Umami and Resend.

## Install

Gauge needs Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/) you don't install anything ahead of time:

```bash
uvx gauge-mcp --version
```

Or with pip:

```bash
pip install gauge-mcp
```

## Settings

Set these as environment variables in your MCP client's config, or put them in a config file (see below). You only need the settings for the modules you use. Gauge starts without any of them and tells you what's missing when you call a tool.

| Variable | Module | What it is |
|---|---|---|
| `GAUGE_UMAMI_URL` | umami | Your Umami address, e.g. `https://analytics.example.com` |
| `GAUGE_UMAMI_USERNAME` / `GAUGE_UMAMI_PASSWORD` | umami | A self-hosted Umami login. A view-only user is enough. |
| `GAUGE_UMAMI_API_KEY` | umami | Umami Cloud API key (instead of a login). `GAUGE_UMAMI_URL` defaults to Umami Cloud when this is set. |
| `GAUGE_RESEND_API_KEY` | resend | Resend API key |
| `GAUGE_RESEND_FROM` | resend | Sender address on a domain you verified in Resend |
| `GAUGE_RESEND_FROM_NAME` | resend | Optional sender display name |
| `GAUGE_RESEND_ALLOWED_RECIPIENTS` | resend | Optional. Comma-separated addresses or `@domains` the assistant may send to without asking you each time, e.g. `me@example.com,@example.com` |
| `GAUGE_RESEND_REQUIRE_CONFIRM` | resend | `true` by default: you approve each send. `false` lets the assistant send on its own. |
| `GAUGE_<MODULE>_ENABLED` | any | `false` turns a module off, e.g. `GAUGE_RESEND_ENABLED=false` |

**Config file (optional).** Gauge also reads `~/.config/gauge/config.toml`, or the file named by `GAUGE_CONFIG`. Environment variables win over the file.

```toml
[umami]
url = "https://analytics.example.com"
username = "viewer"
password = "change-me"

[resend]
enabled = false
```

## Add it to your MCP client

Most clients use the same JSON shape. Put it where your client keeps MCP servers:

```json
{
  "mcpServers": {
    "gauge": {
      "command": "uvx",
      "args": ["gauge-mcp"],
      "env": {
        "GAUGE_UMAMI_URL": "https://analytics.example.com",
        "GAUGE_UMAMI_USERNAME": "viewer",
        "GAUGE_UMAMI_PASSWORD": "change-me",
        "GAUGE_RESEND_API_KEY": "re_your_key",
        "GAUGE_RESEND_FROM": "hello@example.com"
      }
    }
  }
}
```

| Client | Where the config goes |
|---|---|
| Claude Desktop | `claude_desktop_config.json` (Settings → Developer → Edit Config) |
| Claude Code | `claude mcp add gauge -- uvx gauge-mcp`, then add the `env` values, or a project `.mcp.json` |
| Cursor | `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project |
| VS Code | `.vscode/mcp.json`, using `"servers"` instead of `"mcpServers"` |
| Sonique, Hermes, OpenClaw, others | Any client that launches a stdio MCP server: command `uvx`, args `["gauge-mcp"]`, plus the env values |

**Over HTTP.** For clients that connect to a URL instead of launching a process:

```bash
gauge-mcp --transport http --port 8765
```

It serves streamable HTTP at `http://127.0.0.1:8765/mcp`. It listens only on your own machine by default. Put authentication in front of it before exposing it anywhere else.

## Tools

| Tool | What it returns |
|---|---|
| `gauge_modules` | Which modules are on, which settings are missing, and what each tool does. Call this first. |
| `umami_websites` | Tracked websites, paged |
| `umami_stats` | Pageviews, visitors, visits, bounces and bounce rate for a date range |
| `umami_pageviews` | Pageviews and sessions over time |
| `umami_metrics` | A ranked breakdown: `type` = `path` (top pages), `referrer`, `country`, `browser`, `os`, `device` and more |
| `resend_send` | Sends an email after you approve it. Without `confirm=true` it only returns a preview. |
| `resend_emails` | Sent emails, paged with a cursor |
| `resend_domains` | Sending domains and whether each is verified |

Every tool returns structured data with a published schema, so an assistant can write code against the results instead of reading text. Date ranges take `days` (default 7) or ISO 8601 `start` and `end`. Errors always look like `gauge.<module>.<code>: <message> Fix: <hint>`.

## Safety

- Gauge only calls the services you configure. It sends nothing anywhere else and keeps no data.
- `resend_send` sends real email, so **you** approve each one, not the assistant. When the assistant asks to send, Gauge asks you directly through your MCP client (MCP elicitation) and shows the sender, recipients and subject. The assistant can't answer that question for you.
- Some MCP clients can't ask you questions yet. In those, Gauge refuses to send unless you've pre-approved the recipients with `GAUGE_RESEND_ALLOWED_RECIPIENTS`, or turned approval off with `GAUGE_RESEND_REQUIRE_CONFIRM=false`.
- Give Gauge the least access it needs: a view-only Umami user, and a Resend key limited to sending if you don't need the other email tools.

## Develop

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## License

MIT. Published by Seaynic Labs.
