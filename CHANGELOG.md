# Changelog

Gauge follows [semantic versioning](https://semver.org). Modules added after 0.1 ship switched off.

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
