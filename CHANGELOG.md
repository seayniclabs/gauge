# Changelog

Gauge follows [semantic versioning](https://semver.org). Modules added after 0.1 ship switched off.

## 0.5.0 — 2026-11-24

Change the pin to `gauge-mcp==0.5.0`. The SEO checks run only when asked for (`checks=["seo"]`).

- SEO checks in the `health` module, opt-in with `checks=["seo"]`: `seo_title`, `seo_description`, `seo_indexable` (noindex in a meta tag or `X-Robots-Tag`, robots.txt for Googlebot), `seo_canonical`, `seo_sitemap` (from robots.txt or `/sitemap.xml`; DOCTYPE and entities are never expanded), `seo_basics` (lang, viewport, exactly one H1, images with no alt) and `seo_social` (Open Graph). They roll up into a new `seo` verdict and never change `overall`. Page text is never returned, only lengths and yes/no. robots.txt is now fetched once per check and shared.

## 0.4.0 — 2026-11-24

Change the pin to `gauge-mcp==0.4.0`. The AI-readiness checks run only when asked for (`checks=["ai"]`).

- AI-readiness checks (AEO/GEO) in the `health` module, opt-in with `checks=["ai"]`: `robots_ai` (robots.txt lets OAI-SearchBot, PerplexityBot and Claude-SearchBot in; training crawlers are reported, not scored), `structured_data` (JSON-LD present and parses, with its types), `server_text` (at least 150 words in the HTML, not only built by JavaScript), `llms_txt` (reported, not scored). They roll up into a new `ai_readiness` verdict and never change `overall`. Pages that answer an error or block page are not scored. No AI is involved; whether AI tools cite a site stays a manual step in the `ai-seo` skill.
- New skill `ai-seo` (Corey Haines, MIT).

## 0.3.0 — 2026-11-24

Change the pin to `gauge-mcp==0.3.0`. The new module is off until you set `GAUGE_HEALTH_ENABLED=true`.

- New module `health`, **switched off by default** (`GAUGE_HEALTH_ENABLED=true`). `health_check` and `health_sweep` check DNS, HTTP status and response time (redirects followed and recorded), TLS certificate expiry, domain renewal date (RDAP), a must-appear page string and security headers. Each check reads OK, FAILING or UNKNOWN; no AI is involved. Private and local addresses are refused unless allowed in settings; page text and header values are never returned.
- New skill `site-health-check`; `getting-started` covers the health module.
- The tool lock and security card now include modules that ship switched off.
- Tests no longer write to your real approval log.

## 0.2.1 — 2026-10-27

**No code changes.** Change the pin to `gauge-mcp==0.2.1`.

### Release process

- Built and published by GitHub Actions (`release.yml`) with PyPI trusted publishing. The wheel and sdist carry PyPI provenance and a GitHub build attestation; check a download with `gh attestation verify <file> --owner seayniclabs`.

## 0.2.0 — 2026-10-27

- Skills: 9 skills ship with Gauge. `getting-started` and `weekly-site-report` are new; `analytics`, `seo-audit`, `copywriting` and `emails` (Corey Haines) and `resend`, `email-best-practices` and `react-email` (Resend) are included under MIT with credit.
- Every skill is also served as an MCP prompt, and every skill file as an MCP resource (`skill://gauge/<skill>/<file>`), so apps without Agent Skills support get them too.
- `gauge_modules` lists the skills, and names the publisher.
- Claude Code plugin: `/plugin install gauge@seayniclabs` installs the server and the skills together.
- `CREDITS.md` and a CI check that every skill has a license, a source, and (if third-party) its license file and a credit.
- `gauge-mcp --doctor`: checks settings and connections and prints a fix for each problem.
- Local approval log (`~/.local/state/gauge/approvals.jsonl`): every send approved, declined or refused. Message bodies are never logged.
- `tool-manifest.lock.json`: a hash of every tool description, prompt and the server instructions; CI fails if they change silently.
- README security card (generated from the live server), `SECURITY.md`, five starter prompts, pinned install.

## 0.1.0 — 2026-10-27

- First release as one bundle with two modules: `umami` (web analytics) and `resend` (email).
- `gauge_modules` discovery tool: which modules are on, which settings are missing, what each tool does.
- Every tool returns structured data with a published output schema.
- `umami_metrics` gives real ranked breakdowns (top pages, referrers, countries, browsers and more), with `limit`, `offset` and `search`.
- Umami Cloud supported through an API key, as well as a self-hosted login.
- `resend_send` previews by default. Sending needs the person's approval: asked through MCP elicitation, or given ahead of time with `GAUGE_RESEND_ALLOWED_RECIPIENTS` or `GAUGE_RESEND_REQUIRE_CONFIRM=false`. The assistant's own `confirm=true` is never enough. The sender comes from settings, never from code.
- Runs over stdio or streamable HTTP. Starts with no settings and reports what is missing when a tool is called.
