# Seezar Autonomous Operator

Two autonomous operators that complete the assessment scenarios by driving the
Seezar Dashboard **through a real browser** — signing in, navigating the
dealership tree, opening Analytics, applying date ranges, reading the charts out
of the DOM, and writing a report.

No API shortcuts on the execution path. Everything the operator knows, it read
off a rendered page.

- **Scenario I** — compare engagement between two dealerships → [`reports/scenario_one.md`](reports/scenario_one.md)
- **Scenario IV** — anomaly sweep across the first 10 dealerships → [`reports/scenario_four.md`](reports/scenario_four.md)

---

## Running it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env            # set PI_TARGET_USERNAME to your dashboard email

.venv/bin/pi login              # complete the emailed one-time code once
.venv/bin/pi scenario one       # -> reports/scenario_one.md
.venv/bin/pi scenario four      # -> reports/scenario_four.md
.venv/bin/pi recon              # re-survey the dashboard's structure
```

Add `--headed` to watch any of them work.

---

## Design decisions worth explaining

**Authentication is human-in-the-loop, once.** Seezar signs in with a one-time
code sent by email. There is no password an operator could hold, and an agent
with mailbox access would be a far larger security surface than this task
justifies. So `pi login` has a human complete the code once; the browser session
is persisted and every run reuses it. Expiry is detected and reported rather
than worked around.

**Perception, not scraping.** A distiller runs inside the page and emits a
compact, indexed view of what an operator could perceive — roles, accessible
names, values, states — tagging each element so the follow-up action resolves to
exactly the node that was perceived. Building it against this dashboard exposed
two gaps worth noting:

- The dealership tree is `<li>` elements with JavaScript-bound click handlers,
  not links. Nothing in the markup marks them interactive, so the first version
  perceived **2 elements on a 401 KB page**. Detecting `cursor: pointer` — the
  one trace such handlers leave — took it to 43.
- The tab buttons carry `aria-label=""`, which suppresses their accessible name.
  Playwright's `get_by_role("button", name="Analytics")` silently never matches.

**Charts are read from markup, not pixels.** The engagement donut is a
`<canvas>`, but its legend is real DOM text (`Forms submitted 65% (130)`), so
extraction reads the legend rows structurally. No OCR, no vision model.

**Deterministic where the page is known.** `read_analytics()` is the single unit
both scenarios are built on: one dealership, one date range, every metric. It is
plain scripted extraction, because the structure is known and stable and a model
would only make it slower and non-reproducible. Scenario IV is that same unit run
across 20 page loads. Alert thresholds are explicit constants, so the same
figures always produce the same report.

**Navigation by URL, not by clicking.** The Analytics tab is bound to a handler
that needs the dealership's bot id, and while that is unresolved the click is
**silently inert** — no navigation, no error, no failed network request. That
cost real debugging time and is the hardest class of failure to detect.
`/dealership/{id}/analytics` redirects to the right view on its own, so the
operator navigates directly and verifies the URL before reading anything.

---

## Findings about the environment

These affect what a *correct* answer looks like, so they are reported rather
than smoothed over.

**1. Engagement data is identical everywhere.** All 10 dealerships scanned
return exactly 200 clicks, 130 forms submitted, 30 CTAs clicked, 40 carousel
clicked — matching the example in the brief exactly. This card appears to serve
fixed data on this environment.

So the honest answer to Scenario I's *"for which dealership are clicks more and
submitted forms more?"* is **neither — they are identical**. The operator
reports the tie. An agent that manufactured a winner to satisfy the question
would be confidently wrong, and that is precisely the failure this is built to
avoid.

**2. Scenario IV asks for 7 days vs 30 days; that range does not exist.** The
Analytics range control offers only *30 Days* and *90 Days*. There are 7/14-day
buttons on the page, but they belong to the "Busiest day" card rather than the
page range, so using them would compare two different things. The operator
compares 30 vs 90 and says so in the report. The ranges are parameters.

**3. Conversion Rate reads 0%** on every dealership scanned despite non-zero
clicks, which the sweep flags. `read_analytics` also computes forms ÷ clicks
(65%) alongside it, since which figure the brief means is unconfirmed.

---

## Layout

```
pi_operator/
  browser/      perception distiller, session, tool registry
  targets/      SeezarAdapter — all dashboard-specific knowledge lives here
  scenarios/    read_analytics (shared unit), scenario_one, scenario_four
  recon.py      surveys the dashboard and records its real structure
  agents/       planner, navigator, verifier  (goal-driven operator layer)
  graph/        LangGraph supervisor with resumable human approval gates
  guardrails/   risk policy derived from tool and element semantics
reports/        generated scenario reports
```

`pi recon` surveys the dashboard and writes its structure, screenshots and HTML
snapshots to `recon/`. That output is kept out of version control because it
contains full captures of a live customer dashboard; re-run the command to
regenerate it.

## Limitations

- Both scenarios are read-only, so the write-safety machinery (approval gates,
  irreversibility rules) is present but not exercised by them.
- The dashboard is intermittently degraded; runs retry and report failure
  explicitly rather than presenting a partial read as a result.
- The eval harness and mock fixture in this repo target an earlier problem
  statement and are not part of the two scenarios above.
