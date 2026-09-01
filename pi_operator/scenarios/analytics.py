"""Reading one dealership's Analytics tab at one date range.

This is the unit both chosen scenarios are built on. Scenario I reads it twice
and compares; Scenario IV reads it across ten dealerships at two ranges and
looks for anomalies. Writing it once, deterministically, is what makes the
second scenario cheap.

It is deliberately *not* an LLM task. The page structure is known and stable, so
extraction is a scripted read — fast, free, and identical every run. The model's
job starts afterwards, on the parts that need judgement: deciding what counts as
an anomaly and writing the report.

Lazy rendering is the one real hazard here. Cards below the fold do not exist in
the DOM until scrolled to, so a naive read returns a page that is genuinely half
empty. `_reveal` walks the page before reading anything.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from pi_operator.browser.session import BrowserSession

# Runs in the page. Reads the analytics cards by their own structure
# (.card > .subText label + h2 value) plus the engagement legend rows.
EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const out = { cards: {}, events: {}, totals: {}, headings: [] };

  // Labelled metric cards: a subText caption above an h2 value.
  document.querySelectorAll('.card').forEach((card) => {
    const label = card.querySelector('.subText');
    const value = card.querySelector('h2');
    if (label && value) out.cards[clean(label.textContent)] = clean(value.textContent);
  });

  // Percentage cards such as Conversion Rate / Satisfaction Rate, which use a
  // heading + a percentage rather than the subText/h2 shape.
  document.querySelectorAll('div, section').forEach((el) => {
    if (el.children.length > 4) return;
    const t = clean(el.textContent);
    const m = t.match(/^(Conversion Rate|Satisfaction Rate)\s*([\d.]+\s*%)$/i);
    if (m) out.cards[m[1]] = m[2].replace(/\s+/g, '');
  });

  // Engagement legend. Read the DOM structure rather than the rendered text:
  // the label and its percentage are not separated by whitespace
  // ("Forms submitted65% (130)"), so any regex over the concatenated string
  // splits the number itself. Each row is:
  //   <div><div><span class=coloredDot/><span>NAME</span></div>
  //        <span><span>PCT%</span><span class=legendValue>(COUNT)</span></span></div>
  document.querySelectorAll('.legendValue').forEach((node) => {
    const countMatch = clean(node.textContent).match(/(\d[\d,]*)/);
    if (!countMatch) return;
    const count = parseInt(countMatch[1].replace(/,/g, ''), 10);

    const pctEl = node.previousElementSibling;
    const pctMatch = pctEl ? clean(pctEl.textContent).match(/([\d.]+)/) : null;
    const percent = pctMatch ? parseFloat(pctMatch[1]) : null;

    const row = node.parentElement && node.parentElement.parentElement;
    if (!row) return;
    const labelEl = row.querySelector('div span:last-of-type, div span');
    const name = clean(labelEl ? labelEl.textContent : '');
    if (name && name.length <= 40 && !isNaN(count)) {
      out.events[name] = { percent: percent, count: count };
    }
  });

  // Headline totals such as "200 Clicks" and "11732 messages".
  document.querySelectorAll('h1, h2, h3').forEach((h) => {
    const t = clean(h.textContent);
    out.headings.push(t);
    const m = t.match(/^(\d[\d,]*)\s+([A-Za-z]+)$/);
    if (m) out.totals[m[2].toLowerCase()] = parseInt(m[1].replace(/,/g, ''), 10);
  });

  const range = document.querySelector('.dateFilterSelect input');
  out.range = range ? clean(range.value) : '';
  return out;
}
"""


class AnalyticsReading(BaseModel):
    """One dealership, one date range."""

    dealership_id: str
    dealership_name: str = ""
    date_range: str = ""
    url: str = ""

    total_clicks: int | None = None
    events: dict[str, dict[str, float]] = Field(default_factory=dict)
    conversion_rate: float | None = None
    satisfaction_rate: float | None = None
    total_messages: int | None = None
    cards: dict[str, str] = Field(default_factory=dict)

    complete: bool = True
    note: str = ""

    # -- derived ---------------------------------------------------------

    def event_count(self, name: str) -> int | None:
        for key, value in self.events.items():
            if name.lower() in key.lower():
                return int(value["count"])
        return None

    def top_events(self, n: int = 3) -> list[tuple[str, int, float]]:
        ranked = sorted(self.events.items(), key=lambda kv: kv[1]["count"], reverse=True)
        return [(k, int(v["count"]), float(v["percent"])) for k, v in ranked[:n]]

    @property
    def forms_submitted(self) -> int | None:
        return self.event_count("forms submitted")

    @property
    def derived_conversion(self) -> float | None:
        """Forms submitted over total clicks.

        Reported alongside the dashboard's own Conversion Rate rather than
        instead of it, because which one Scenario IV means is unconfirmed.
        """
        if not self.total_clicks or self.forms_submitted is None:
            return None
        return round(100 * self.forms_submitted / self.total_clicks, 2)


async def _await_shell(session: BrowserSession, timeout_ms: int = 25_000) -> bool:
    """Wait for the analytics page frame to exist."""
    page = session.page
    assert page
    try:
        await page.wait_for_selector(".dateFilterSelect, .card", timeout=timeout_ms)
        await session.settle(1_500)
        return True
    except Exception:
        return False


async def _await_values(session: BrowserSession, attempts: int = 16) -> bool:
    """Wait until the cards actually contain figures.

    The analytics view fits in roughly one viewport, so nothing here needs
    scrolling — an earlier version scrolled aggressively to "reveal" cards and
    made things worse, because mouse-wheel events land on whatever is under the
    pointer, which is the dealership sidebar. The cards simply arrive from a
    second request a moment after the frame renders; the only thing needed is
    to wait for them.
    """
    page = session.page
    assert page
    for _ in range(attempts):
        ready = await page.evaluate(
            "() => document.querySelectorAll('.legendValue').length > 0"
            " || Array.from(document.querySelectorAll('.card h2'))"
            "      .some((h) => h.textContent.trim().length > 0)"
        )
        if ready:
            return True
        await session.settle(1_500)
    return False


def _percent(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"([\d.]+)\s*%", raw)
    return float(match.group(1)) if match else None


async def read_analytics(
    session: BrowserSession,
    target,
    dealership: dict[str, str],
    date_range: str = "30 Days",
) -> AnalyticsReading:
    """Open a dealership's Analytics tab at a range and read every metric."""
    # Navigate straight to the analytics view rather than clicking the tab:
    # the tab handler is intermittently inert on this environment and fails
    # silently, leaving the extractor reading the Overview page.
    opened = False
    if hasattr(target, "open_analytics"):
        for _ in range(3):
            if await target.open_analytics(session, dealership):
                opened = True
                break
            await session.settle(2_500)
    else:
        await target.open_dealership(session, dealership)
        await session.settle(2_500)
        for _ in range(3):
            await target.open_tab(session, "Analytics")
            await _await_shell(session)
            if "/analytics" in (session.page.url or ""):
                opened = True
                break
            await session.settle(2_500)

    if not opened:
        return AnalyticsReading(
            dealership_id=dealership["id"], dealership_name=dealership.get("name", ""),
            complete=False, note="could not open the Analytics view",
        )

    ready = await _await_shell(session)
    ranged = await target.set_date_range(session, date_range)

    # The cards are virtualised: they mount as they are scrolled into view and
    # unmount again afterwards, and their values arrive from a later request.
    # Polling the DOM between scrolls fights that — the poll's own scrolling
    # unmounts what it is waiting for. Instead do a full reveal pass and read;
    # if the read comes back empty, reveal and read again.
    # Changing the range triggers a refetch, so wait for values, not for the frame.
    populated = await _await_values(session)
    raw: dict[str, Any] = await session.page.evaluate(EXTRACT_JS)

    if not raw.get("events") and not raw.get("totals", {}).get("clicks"):
        # One clean retry: re-enter the tab and wait again.
        if hasattr(target, "open_analytics"):
            await target.open_analytics(session, dealership)
        else:
            await target.open_tab(session, "Analytics")
        await _await_shell(session)
        populated = await _await_values(session)
        raw = await session.page.evaluate(EXTRACT_JS)

    reading = AnalyticsReading(
        dealership_id=dealership["id"],
        dealership_name=dealership.get("name", ""),
        date_range=raw.get("range") or date_range,
        url=session.page.url,
        events=raw.get("events", {}),
        cards=raw.get("cards", {}),
        total_clicks=raw.get("totals", {}).get("clicks"),
        total_messages=raw.get("totals", {}).get("messages"),
        conversion_rate=_percent(raw.get("cards", {}).get("Conversion Rate")),
        satisfaction_rate=_percent(raw.get("cards", {}).get("Satisfaction Rate")),
    )

    problems = []
    if not ready:
        problems.append("analytics page did not render")
    elif not populated:
        problems.append("analytics cards never populated")
    if not ranged:
        problems.append(f"could not set the range to {date_range!r}")
    if not reading.events:
        problems.append("no engagement events found")
    if reading.total_clicks is None:
        problems.append("no total click count found")
    if problems:
        reading.complete = False
        reading.note = "; ".join(problems)
    return reading
