---
name: weekly-site-report
description: Write a short weekly traffic report for a website from Umami data, using the Gauge tools. Use when the user asks how their site did this week or last week, wants a traffic summary, top pages, where visitors came from, or a report to email to themselves or a client.
license: MIT
source: https://github.com/seayniclabs/gauge
author: Seaynic Labs
---

# Weekly site report

Produce a report the user can read in under a minute: five numbers, three short lists, and at most three observations. Every number comes from a Gauge tool call; never estimate.

## Gather (one site at a time)

1. `umami_websites` if you don't have the site `id`.
2. `umami_stats` for this week: `start` = last Monday 00:00, `end` = now (ISO 8601). Then the same call for the previous full week, for comparison.
3. `umami_pageviews` with `unit=day` for this week: the daily shape.
4. `umami_metrics` three times, `limit=5`: `type=path` (top pages), `type=referrer` (sources), `type=country`.

## Write

```
<Site> · week of <Mon date>

Pageviews  <n>  (<+/-x%> vs last week)
Visitors   <n>  (<+/-x%>)
Visits     <n>
Bounce     <x%>  (was <y%>)
Busiest day <weekday>, <n> pageviews

Top pages     1. /path  n   2. ...
Top sources   1. referrer  n   ...   (blank referrer = direct or app traffic)
Top countries 1. ...
```

Then up to three observations, one line each, each tied to a number above. For example: "/pricing rose from 41 to 112 views after Tuesday's post." Skip observations that the numbers don't support.

## Rules

- Percent change = (this − last) / last. If last week was 0, write "new" instead of a percentage.
- Bounce rate comes from `umami_stats.bounce_rate` (a fraction): show it as a percentage.
- Small sites: below 50 visits a week, say the numbers are too small for trends and skip the observations.
- To email the report, draft it with `resend_send` (no `confirm`), show the preview, and let the user approve the send.

