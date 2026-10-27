---
name: getting-started
description: Set up and check the Gauge MCP bundle (Umami analytics and Resend email). Use when the user installs Gauge, asks what Gauge can do, gets a "not_configured" or "auth_failed" error from a gauge tool, or wants to turn the analytics or email part on or off.
license: MIT
source: https://github.com/seayniclabs/gauge
author: Seaynic Labs
---

# Getting started with Gauge

Gauge connects you to two services the user already has: Umami (website analytics) and Resend (email). Each is a module the user can switch off.

## 1. See what is ready

Call `gauge_modules` first. It returns each module with `enabled`, `configured` and `missing` (the exact settings to add). Report the missing settings by name. Never guess values.

## 2. Settings the user adds

Settings go in the MCP app's `env` block, or in `~/.config/gauge/config.toml` (sections `[umami]` and `[resend]`). Environment variables win over the file.

| Setting | Needed for |
|---|---|
| `GAUGE_UMAMI_URL`, `GAUGE_UMAMI_USERNAME`, `GAUGE_UMAMI_PASSWORD` | Self-hosted Umami. A view-only Umami user is enough. |
| `GAUGE_UMAMI_API_KEY` | Umami Cloud, instead of a login |
| `GAUGE_RESEND_API_KEY` | Any Resend tool |
| `GAUGE_RESEND_FROM` | Sending. Must be an address on a domain verified in Resend (check with `resend_domains`). |
| `GAUGE_RESEND_ENABLED=false` | Turns email off entirely |

Tell the user to restart their MCP app after changing settings.

## 3. First checks

1. `umami_websites`: lists their sites and each site's `id`. Every other Umami tool needs that `id`.
2. `umami_stats` with that `id`: last 7 days of pageviews, visitors, visits and bounce rate.
3. `resend_domains`: shows which sending domains are verified.

## 4. Sending email

`resend_send` without `confirm=true` returns a preview. Show the user the full preview. With `confirm=true`, Gauge asks the user directly through the app to approve the send; you cannot approve it for them. If the app cannot ask, the tool returns `approval_unavailable` and names the two settings that pre-approve sending. Explain those settings; do not set them yourself.

## Errors

Every error reads `gauge.<module>.<code>: <message> Fix: <hint>`. Pass the fix on to the user as written.

