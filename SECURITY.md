# Security policy

## Reporting a vulnerability

Report it privately on GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Please don't open a public issue.

We acknowledge reports within 2 business days and aim to ship a fix for confirmed high-severity issues within 14 days. We credit reporters in the release notes unless you ask us not to.

## What Gauge does and doesn't do

- It connects only to the services you configure (your Umami instance or Umami Cloud, and the Resend API). It has no telemetry.
- Credentials come from environment variables or `~/.config/gauge/config.toml`. They are never tool arguments and never returned to the model.
- Sending email needs a person's approval, given in the app or ahead of time in settings. The model's own `confirm=true` is never enough.
- Every approval decision is logged locally in `~/.local/state/gauge/approvals.jsonl`.
- `tool-manifest.lock.json` records a hash of every tool description, prompt and the server instructions. CI fails if they change without the lock changing too.

## Supported versions

The latest minor release receives security fixes.
