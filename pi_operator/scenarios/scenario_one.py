"""Scenario I — compare engagement between two dealerships.

    Open dealership A, Analytics, 30 days, read the top events.
    Repeat for dealership B. Report which has more clicks and more forms.

The extraction is deterministic (`read_analytics`); the only judgement involved
is the comparison itself, which is arithmetic. There is nothing here a language
model would do better, so nothing here uses one — which is the point of having
drawn that line explicitly.

The report states what was found even when the finding is "no difference".
Both dealerships currently return identical figures on this environment, and
an operator that manufactured a winner to satisfy the question would be wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pi_operator.browser.session import BrowserSession
from pi_operator.config import REPO_ROOT
from pi_operator.scenarios.analytics import AnalyticsReading, read_analytics

DEFAULT_DEALERSHIPS = ["Ejner Hessel", "Approved Automotive"]
REPORTS_DIR = REPO_ROOT / "reports"


class Comparison(BaseModel):
    metric: str
    left: int | None = None
    right: int | None = None

    @property
    def winner(self) -> str:
        if self.left is None or self.right is None:
            return "unknown"
        if self.left == self.right:
            return "tie"
        return "left" if self.left > self.right else "right"


class ScenarioOneResult(BaseModel):
    date_range: str
    readings: list[AnalyticsReading] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    generated_at: str = ""
    warnings: list[str] = Field(default_factory=list)

    def markdown(self) -> str:
        if len(self.readings) < 2:
            return "# Scenario I\n\nCould not read both dealerships.\n"
        a, b = self.readings[0], self.readings[1]
        lines = [
            "# Scenario I — Dealership Engagement Comparison",
            "",
            f"Range **{self.date_range}** · generated {self.generated_at}",
            "",
            "## Top 3 events",
            "",
            f"| Event | {a.dealership_name} | {b.dealership_name} |",
            "|---|---:|---:|",
        ]
        names = [n for n, _, _ in a.top_events(3)] or [n for n, _, _ in b.top_events(3)]
        for name in names:
            av, bv = a.event_count(name), b.event_count(name)
            ap = next((p for n, _, p in a.top_events(9) if n == name), None)
            bp = next((p for n, _, p in b.top_events(9) if n == name), None)
            lines.append(
                f"| {name} | {_fmt(av)}{f' ({ap}%)' if ap is not None else ''} "
                f"| {_fmt(bv)}{f' ({bp}%)' if bp is not None else ''} |"
            )
        lines += [
            f"| **Total clicks** | **{_fmt(a.total_clicks)}** | **{_fmt(b.total_clicks)}** |",
            "",
            "## Answer",
            "",
        ]
        for comparison in self.comparisons:
            if comparison.winner == "tie":
                lines.append(
                    f"- **{comparison.metric}:** neither — both record "
                    f"{_fmt(comparison.left)}."
                )
            elif comparison.winner == "unknown":
                lines.append(f"- **{comparison.metric}:** could not be determined.")
            else:
                lead = a if comparison.winner == "left" else b
                hi = comparison.left if comparison.winner == "left" else comparison.right
                lo = comparison.right if comparison.winner == "left" else comparison.left
                lines.append(
                    f"- **{comparison.metric}:** {lead.dealership_name} "
                    f"({_fmt(hi)} vs {_fmt(lo)})."
                )
        if self.warnings:
            lines += ["", "## Notes", ""] + [f"- {w}" for w in self.warnings]
        lines += [
            "",
            "## How this was produced",
            "",
            "The operator signed in with a saved session, resolved each dealership from the",
            "sidebar tree to its id, opened the Analytics view directly by URL, applied the",
            "date range and read the User Engagement card from the DOM. Values come from the",
            "chart's legend markup rather than from the rendered canvas, so no image",
            "recognition is involved.",
            "",
            f"Sources: {a.url} · {b.url}",
        ]
        return "\n".join(lines)


def _fmt(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


async def run_scenario_one(
    session: BrowserSession,
    target,
    dealerships: list[str] | None = None,
    date_range: str = "30 Days",
) -> ScenarioOneResult:
    wanted = dealerships or DEFAULT_DEALERSHIPS
    result = ScenarioOneResult(
        date_range=date_range,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )

    await session.goto("/")
    for name in wanted:
        entry = await target.find_dealership(session, name)
        if entry is None:
            result.warnings.append(f"dealership {name!r} could not be found")
            continue
        reading = await read_analytics(session, target, entry, date_range)
        if not reading.complete:
            # One clean retry from a fresh navigation. The dashboard
            # intermittently serves an empty analytics view on first open.
            await session.goto("/")
            await session.settle(2_000)
            retry = await read_analytics(session, target, entry, date_range)
            if retry.complete:
                reading = retry
            else:
                result.warnings.append(f"{name}: {reading.note}")
        result.readings.append(reading)

    if len(result.readings) == 2:
        a, b = result.readings
        result.comparisons = [
            Comparison(metric="More clicks", left=a.total_clicks, right=b.total_clicks),
            Comparison(metric="More submitted forms",
                       left=a.forms_submitted, right=b.forms_submitted),
        ]
        # Only claim a tie on values actually read. Comparing two missing
        # readings makes None == None true and would report "both identical"
        # about a page that never loaded.
        both_read = None not in (a.total_clicks, b.total_clicks,
                                 a.forms_submitted, b.forms_submitted)
        if both_read and a.total_clicks == b.total_clicks \
                and a.forms_submitted == b.forms_submitted:
            result.warnings.append(
                "Both dealerships return identical engagement figures, which matches the "
                "example in the brief exactly. This card appears to serve fixed data on "
                "this environment rather than per-dealership values; the comparison is "
                "reported as a tie rather than inventing a winner."
            )
    return result


def save(result: ScenarioOneResult, out_dir: Path | None = None) -> Path:
    destination = out_dir or REPORTS_DIR
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scenario_one.json").write_text(
        json.dumps(result.model_dump(), indent=2, default=str)
    )
    path = destination / "scenario_one.md"
    path.write_text(result.markdown(), encoding="utf-8")
    return path
