---
name: site-health-check
description: Check whether a website or web app is healthy with the Gauge health tools — DNS, uptime and response time, TLS certificate expiry, domain renewal date, a must-appear page string, security headers, and opt-in SEO and AI-readiness (AEO/GEO) checks. Use when the user asks "is my site up", "is it down", "check my site", "when does my certificate or domain expire", "run a health check or sweep", "check all my sites", "is my site ready for AI search", "check my SEO", or gets a FAILING or UNKNOWN result from health_check or health_sweep.
license: MIT
source: https://github.com/seayniclabs/gauge
author: Seaynic Labs
---

# Site health checks

Gauge's `health` module runs fixed checks with no AI in them. Each check returns one of three states:

| State | Means | Say to the user |
|---|---|---|
| `OK` | The check ran and passed. | It passed, with the number that matters (days left, response time). |
| `FAILING` | The check ran and found a problem. | What failed, in the `detail` words, and the fix. |
| `UNKNOWN` | The check could not run (lookup failed, page not fetched, registry gives no date, earlier check failed). | That you could not tell. **Never report UNKNOWN as fine.** |

## 1. Turn it on

The module ships switched off. Call `gauge_modules`; if `health` shows `enabled: false`, tell the user to set `GAUGE_HEALTH_ENABLED=true` (or `[health] enabled = true` in the config file) and restart their MCP app. No account or key is needed.

Settings (all optional):

| Setting | Default | What it does |
|---|---|---|
| `GAUGE_HEALTH_TARGETS` | — | Comma-separated URLs for `health_sweep`. The config file can list targets with a page string and domain: `[[health.targets]]` `url = "https://example.com"` `expect = "Order a cake"` `domain = "example.com"` |
| `GAUGE_HEALTH_TLS_MIN_DAYS` | 14 | A certificate with fewer days left is FAILING. |
| `GAUGE_HEALTH_DOMAIN_MIN_DAYS` | 30 | A domain with fewer days to renewal is FAILING. |
| `GAUGE_HEALTH_MAX_MS` | 5000 | A slower answer is FAILING ("slow"). |
| `GAUGE_HEALTH_ALLOW_PRIVATE` | false | Lets checks reach private and local addresses (a home lab, an intranet). Only the user sets this; there is no tool argument for it. |

## 2. Check one site

`health_check(url, expect_text?, domain?, checks?)`

- `url`: a bare host gets `https://`.
- `expect_text`: a phrase that must be on the page, exactly as written (case matters). Choose one that only shows when the page really works, such as a heading or a product name, not the site name in the title. Without it the content check is left out.
- `domain`: the registered domain, if Gauge would guess wrong (for example a site on a hosting subdomain).
- `checks`: a subset of `dns`, `http`, `tls`, `domain`, `content`, `headers`.

## 3. Sweep every site

`health_sweep()` checks every target in settings, at most 25, five at a time. It returns one report per site and `counts` of OK / FAILING / UNKNOWN by `overall`. Lead with the failing sites, then the unknowns, then one line saying the rest passed.

## 4. Read a report

`overall` combines every check **except headers**. `headers` is its own verdict, because a site can be up and working while missing a security header.

| Check | FAILING usually means | UNKNOWN usually means |
|---|---|---|
| `dns` | The name doesn't exist, or it points at a private address (blocked). | The resolver didn't answer; try again. Every check that needs the site is then UNKNOWN too. |
| `http` | Error status (404, 500s), no connection, too many redirects, or slower than `max_ms`. 401/403/429 count as up (`gated: true`): the site answered but asks for a sign-in or is rate-limiting. | — |
| `tls` | Expired, wrong name, untrusted, or fewer than `tls_min_days` left. | Nothing answers on the HTTPS port. |
| `domain` | Renewal within `domain_min_days`, or the domain is on hold. | The registry lookup failed, or the registry publishes no expiry date (some country domains, such as .de, never do). Pass `domain` if Gauge looked up the wrong name. |
| `content` | The phrase isn't on the page Gauge received. | The page couldn't be fetched. |
| `headers` | Named headers are missing (`data.missing`). | The page couldn't be fetched. |

Read these before concluding anything:

- **Check `data.final_url` and `cross_host`.** If the site redirects to a sign-in page or another host, the content and headers results describe *that* page.
- **The content check reads the HTML the server sends.** Pages built in the browser by JavaScript may not contain the phrase yet, so a FAILING content result on such a page can be wrong. Pick a phrase that is in the page source.
- **One run is one moment.** A single FAILING `http` can be a blip; run it again before calling the site down. For alerts around the clock, use a monitor (such as Uptime Kuma). Gauge checks when asked.
- **Gauge never returns page text or header values**, only found or missing. Never quote page content as if Gauge returned it.

## 5. AI readiness (AEO/GEO): opt-in

How a site can show up in AI answers (ChatGPT search, Perplexity, Claude, Google AI features). Run it with `checks=["ai"]` (all four) or by name. These checks never change `overall`; they roll up into `ai_readiness`.

| Check | OK | FAILING | UNKNOWN |
|---|---|---|---|
| `robots_ai` | robots.txt lets the AI search crawlers (OAI-SearchBot, PerplexityBot, Claude-SearchBot) reach the page, or there is no robots.txt | it blocks one or more (named in `data.blocked_search`) | robots.txt couldn't be read |
| `structured_data` | JSON-LD blocks parse; `data.types` lists their schema.org types | no JSON-LD, or a block is invalid JSON | page not fetched, or it answered an error or block page |
| `server_text` | at least 150 words of text are in the HTML the server sends | fewer: the page is probably built by JavaScript, which many AI crawlers don't run | same as above |
| `llms_txt` | `/llms.txt` exists | it doesn't | couldn't be read |

- `llms_txt` is **reported, not scored**: no AI engine has confirmed it uses the file. Mention it; don't call a site broken without it.
- Training crawlers (GPTBot, ClaudeBot, Google-Extended) are listed in `data.allowed` but never fail the check. Blocking training is the owner's choice, and it doesn't stop citations.
- **These checks can't tell whether an AI actually cites the site.** That needs asking AI tools real questions, which is a separate, manual step in the `ai-seo` skill. Never present `ai_readiness: OK` as "AI tools recommend you".
- For what to change after a FAILING result, use the `ai-seo` skill (structure, schema, crawler access) and `seo-audit` for classic search.

## 6. SEO: opt-in

Classic search basics, checked from one fetch of the page plus robots.txt and the sitemap. Run them with `checks=["seo"]` (all seven) or by name. They never change `overall`; they roll up into `seo`.

| Check | FAILING when |
|---|---|
| `seo_title` | no `<title>`, more than one, or not 10–65 characters (`data.length`) |
| `seo_description` | no meta description, or not 50–160 characters |
| `seo_indexable` | the page says `noindex` (meta tag or `X-Robots-Tag`), or robots.txt blocks Googlebot from it. UNKNOWN if robots.txt can't be read |
| `seo_canonical` | no canonical link, or it points to another host. `data.self` says whether it points at this page |
| `seo_sitemap` | no sitemap (robots.txt `Sitemap:` lines, else `/sitemap.xml`), or it isn't valid sitemap XML |
| `seo_basics` | `data.missing` names: `lang`, `viewport`, `one-h1` (exactly one H1), `img-alt` (images with no alt attribute; `alt=""` is fine for decorative images) |
| `seo_social` | `og:title`, `og:description` or `og:image` missing |

- Title and description lengths are guidance from how search results display, not rules. Say "Google will likely cut this title off", not "this page is broken".
- These read the HTML the server sends. A plugin that adds tags with JavaScript won't show here; if the user says a tag is there, check the page source before arguing.
- Gauge never returns the title, description or canonical URL text, only lengths and yes/no. To suggest a new title, read the page yourself or ask the user.
- For what to change, use the `seo-audit` skill.

## 7. Report

```
<site>: <overall>
  dns OK · http OK 200 in 180 ms · tls OK 74 days · domain OK 301 days · content OK
  headers FAILING: missing strict-transport-security, referrer-policy
```

Then one line per problem, giving the fix: renew the certificate or check auto-renew, renew the domain at the registrar, add the missing headers at the host or CDN. Don't fix anything yourself unless the user asks.

## Errors

`gauge.health.bad_url` (not an http or https address), `gauge.health.bad_input` (an unknown check name, more than 25 targets), `gauge.health.not_configured` (no targets for a sweep). Pass the `Fix:` text on to the user.
