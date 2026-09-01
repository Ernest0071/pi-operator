"""Scenario IV — anomaly detector across dealerships.

    Visit each dealership (first 10 if that is too many), scan Analytics over
    two ranges, and flag those whose conversion rate dropped or whose chat
    volume spiked. Produce a short alert report.

This is Scenario I generalised: the same `read_analytics` unit, run across many
dealerships at two ranges. Writing the extractor once is what makes this cheap —
the only new logic is deciding what counts as an anomaly, and that is stated as
explicit thresholds rather than left to a model, so the same inputs always
produce the same alerts.

Deviation from the brief, stated openly: the brief asks for 7 days vs 30 days,
but the Analytics range control offers only 30 and 90 days. The 7/14-day buttons
elsewhere on the page belong to the "Busiest day" card, not the page range, so
using them would compare two different things. The ranges are parameters, so
this becomes a one-line change if a 7-day range appears.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pi_operator.browser.session import BrowserSession
from pi_operator.config import REPO_ROOT
from pi_operator.scenarios.analytics import AnalyticsReading, read_analytics

REPORTS_DIR = REPO_ROOT / "reports"

# Explicit thresholds beat a model's judgement here: the same figures must
# always raise the same alert, or the report cannot be trusted or re-checked.
CONVERSION_DROP_POINTS = 5.0     # percentage points
VOLUME_SPIKE_RATIO = 1.5         # short-range rate exceeding long-range by this much


class Finding(BaseModel):
    dealership_id: str
    dealership_name: str
    severity: str = "info"
    kind: str = ""
    detail: str = ""


class DealershipScan(BaseModel):
    dealership_id: str
    dealership_name: str
    primary: AnalyticsReading | None = None
    comparison: AnalyticsReading | None = None
    findings: list[Finding] = Field(default_factory=list)
    error: str = ""

    @property
    def scanned(self) -> bool:
        return bool(self.primary and self.primary.complete)


class ScenarioFourResult(BaseModel):
    primary_range: str
    comparison_range: str
    scans: list[DealershipScan] = Field(default_factory=list)
    generated_at: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def alerts(self) -> list[Finding]:
        out = [f for s in self.scans for f in s.findings if f.severity in ("warning", "critical")]
        return sorted(out, key=lambda f: 0 if f.severity == "critical" else 1)

    def markdown(self) -> str:
        ok = [s for s in self.scans if s.scanned]
        lines = [
            "# Scenario IV — Dealership Alert Report",
            "",
            f"{len(ok)} of {len(self.scans)} dealerships scanned · "
            f"**{self.primary_range}** vs **{self.comparison_range}** · "
            f"generated {self.generated_at}",
            "",
        ]

        alerts = self.alerts
        lines += ["## Dealerships needing attention", ""]
        if alerts:
            lines += ["| Dealership | Severity | Issue |", "|---|---|---|"]
            for f in alerts:
                lines.append(f"| {f.dealership_name} | {f.severity} | {f.detail} |")
        else:
            lines.append(
                "No dealership crossed the alert thresholds "
                f"(conversion drop ≥ {CONVERSION_DROP_POINTS} points, "
                f"volume ratio ≥ {VOLUME_SPIKE_RATIO}x)."
            )

        lines += ["", "## All dealerships scanned", "",
                  f"| Dealership | Conv. ({self.primary_range}) | "
                  f"Conv. ({self.comparison_range}) | Clicks | Forms | Status |",
                  "|---|---:|---:|---:|---:|---|"]
        for scan in self.scans:
            p, c = scan.primary, scan.comparison
            status = "ok" if scan.scanned else (scan.error or "not read")
            lines.append(
                f"| {scan.dealership_name[:44]} "
                f"| {_pct(p.conversion_rate if p else None)} "
                f"| {_pct(c.conversion_rate if c else None)} "
                f"| {_num(p.total_clicks if p else None)} "
                f"| {_num(p.forms_submitted if p else None)} | {status} |"
            )

        if self.notes:
            lines += ["", "## Notes", ""] + [f"- {n}" for n in self.notes]

        lines += [
            "",
            "## Method",
            "",
            "For each dealership the operator opened the Analytics view directly, applied",
            f"the {self.primary_range} range and read every metric card, then repeated at",
            f"{self.comparison_range}. Alerts are raised by fixed thresholds so the same",
            "figures always produce the same report.",
        ]
        return "\n".join(lines)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _num(v: int | None) -> str:
    return "—" if v is None else f"{v:,}"


def assess(primary: AnalyticsReading, comparison: AnalyticsReading | None,
           name: str, did: str) -> list[Finding]:
    """Apply the alert rules to one dealership's two readings."""
    findings: list[Finding] = []

    if comparison and primary.conversion_rate is not None \
            and comparison.conversion_rate is not None:
        drop = comparison.conversion_rate - primary.conversion_rate
        if drop >= CONVERSION_DROP_POINTS:
            findings.append(Finding(
                dealership_id=did, dealership_name=name, severity="critical",
                kind="conversion_drop",
                detail=f"conversion fell {drop:.1f} points "
                       f"({comparison.conversion_rate:.1f}% → {primary.conversion_rate:.1f}%)",
            ))

    # Volume spike: compare like with like by normalising each range to a daily
    # rate, otherwise the longer window always looks busier.
    p_days = _range_days(primary.date_range)
    c_days = _range_days(comparison.date_range if comparison else "")
    if comparison and primary.total_messages and comparison.total_messages and p_days and c_days:
        p_rate = primary.total_messages / p_days
        c_rate = comparison.total_messages / c_days
        if c_rate and p_rate / c_rate >= VOLUME_SPIKE_RATIO:
            findings.append(Finding(
                dealership_id=did, dealership_name=name, severity="warning",
                kind="volume_spike",
                detail=f"chat volume running {p_rate / c_rate:.1f}x its longer-range daily rate",
            ))

    if primary.conversion_rate == 0 and primary.total_clicks:
        findings.append(Finding(
            dealership_id=did, dealership_name=name, severity="warning",
            kind="zero_conversion",
            detail=f"conversion rate reported as 0% despite {primary.total_clicks:,} clicks",
        ))
    return findings


def _range_days(label: str) -> int | None:
    match = re.search(r"(\d+)", label or "")
    return int(match.group(1)) if match else None


async def run_scenario_four(
    session: BrowserSession,
    target,
    primary: str = "30 Days",
    comparison: str = "90 Days",
    limit: int = 10,
) -> ScenarioFourResult:
    result = ScenarioFourResult(
        primary_range=primary, comparison_range=comparison,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    result.notes.append(
        "The brief specifies 7 days vs 30 days. The Analytics range control offers only "
        "30 and 90 days, so those are compared instead; the ranges are parameters."
    )

    await session.goto("/")
    dealerships = await target.list_dealerships(session)
    if not dealerships:
        result.notes.append("no dealerships could be listed")
        return result

    chosen = dealerships[:limit]
    result.notes.append(
        f"Scanned the first {len(chosen)} of {len(dealerships)} dealerships in sidebar order."
    )

    for entry in chosen:
        scan = DealershipScan(dealership_id=entry["id"], dealership_name=entry["name"])
        try:
            scan.primary = await read_analytics(session, target, entry, primary)
            if scan.primary.complete:
                scan.comparison = await read_analytics(session, target, entry, comparison)
                scan.findings = assess(scan.primary, scan.comparison, entry["name"], entry["id"])
            else:
                scan.error = scan.primary.note or "analytics unavailable"
        except Exception as exc:
            scan.error = f"{type(exc).__name__}: {exc}"
        result.scans.append(scan)
        print(f"  [{len(result.scans)}/{len(chosen)}] {entry['name'][:40]:42} "
              f"{'ok' if scan.scanned else scan.error[:40]}")
    return result


def save(result: ScenarioFourResult, out_dir: Path | None = None) -> Path:
    destination = out_dir or REPORTS_DIR
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scenario_four.json").write_text(
        json.dumps(result.model_dump(), indent=2, default=str)
    )
    path = destination / "scenario_four.md"
    path.write_text(result.markdown(), encoding="utf-8")
    return path
