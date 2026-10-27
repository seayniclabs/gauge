# Changelog

Gauge follows [semantic versioning](https://semver.org). Modules added after 0.1 ship switched off.

## 0.1.0 — unreleased

- First release as one bundle with two modules: `umami` (web analytics) and `resend` (email).
- `gauge_modules` discovery tool: which modules are on, which settings are missing, what each tool does.
- Every tool returns structured data with a published output schema.
- `umami_metrics` gives real ranked breakdowns (top pages, referrers, countries, browsers and more), with `limit`, `offset` and `search`.
- Umami Cloud supported through an API key, as well as a self-hosted login.
- `resend_send` previews by default and sends only with `confirm=true`. The sender comes from settings, never from code.
- Runs over stdio or streamable HTTP. Starts with no settings and reports what is missing when a tool is called.
