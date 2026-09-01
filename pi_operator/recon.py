"""Reconnaissance: learn a target's structure before writing an operator for it.

Writing selectors and workflow hints by guessing is how brittle agents get
built. This logs in once, walks the surfaces the scenarios need, and records
what is actually there: the distilled observation, the raw HTML, a screenshot,
and — the question that decides the whole approach — whether chart values are
readable text or pixels on a canvas.

It also saves the raw HTML of each page as a **snapshot fixture**, so the rest
of the build can be tested offline against what the real DOM looks like rather
than against a fake app.

Nothing here is scenario logic. It is the pass a person would do by hand before
writing a line of automation, done reproducibly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pi_operator.browser.perception import Snapshot, distill
from pi_operator.browser.session import BrowserSession
from pi_operator.config import REPO_ROOT, settings

RECON_DIR = REPO_ROOT / "recon"

# How a chart exposes its numbers decides whether a DOM distiller is enough.
CHART_PROBE = """
() => {
  const out = { canvas: [], svg: [], legend_text: [], suspects: [] };

  document.querySelectorAll('canvas').forEach((c) => {
    const r = c.getBoundingClientRect();
    if (r.width > 40 && r.height > 40) {
      out.canvas.push({ w: Math.round(r.width), h: Math.round(r.height),
                        cls: (c.className || '').toString().slice(0, 80) });
    }
  });

  document.querySelectorAll('svg').forEach((s) => {
    const r = s.getBoundingClientRect();
    if (r.width > 40 && r.height > 40) {
      const texts = Array.from(s.querySelectorAll('text'))
        .map((t) => (t.textContent || '').trim()).filter(Boolean).slice(0, 30);
      out.svg.push({ w: Math.round(r.width), h: Math.round(r.height),
                     text_nodes: texts.length, sample: texts.slice(0, 10) });
    }
  });

  // Text that looks like a chart legend entry: a label with a % and/or a count.
  const rx = /(\\d+(?:\\.\\d+)?\\s*%)|(\\(\\s*\\d[\\d,]*\\s*\\))/;
  document.querySelectorAll('*').forEach((el) => {
    if (el.children.length > 2) return;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 90) return;
    if (rx.test(t) && !out.legend_text.includes(t)) out.legend_text.push(t);
  });
  out.legend_text = out.legend_text.slice(0, 40);

  // Big standalone numbers, e.g. the "200 Clicks" headline.
  document.querySelectorAll('h1,h2,h3,h4,div,span,p').forEach((el) => {
    if (el.children.length) return;
    const t = (el.innerText || '').trim();
    if (/^\\d[\\d,]*\\s*[A-Za-z ]{0,20}$/.test(t) && t.length < 40) {
      if (!out.suspects.includes(t)) out.suspects.push(t);
    }
  });
  out.suspects = out.suspects.slice(0, 30);
  return out;
}
"""


class Recon:
    def __init__(self, out_dir: Path | None = None) -> None:
        self.dir = out_dir or RECON_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)
        self.findings: dict[str, Any] = {"steps": []}
        self.seq = 0

    async def capture(self, session: BrowserSession, label: str, note: str = "") -> Snapshot:
        """Record everything about the current page."""
        self.seq += 1
        stem = f"{self.seq:02d}_{label}"
        await session.settle()
        snap = await distill(session.page)

        (self.dir / "snapshots" / f"{stem}.html").write_text(
            await session.page.content(), encoding="utf-8"
        )
        (self.dir / "snapshots" / f"{stem}.txt").write_text(snap.render(max_elements=400),
                                                            encoding="utf-8")
        await session.page.screenshot(path=str(self.dir / "snapshots" / f"{stem}.png"),
                                      full_page=True)
        charts = await session.page.evaluate(CHART_PROBE)

        self.findings["steps"].append({
            "step": stem,
            "note": note,
            "url": session.page.url,
            "title": snap.title,
            "element_count": len(snap.elements),
            "tables": [{"headers": t.headers, "row_count": len(t.rows),
                        "first_row": t.rows[0] if t.rows else []} for t in snap.tables],
            "alerts": snap.alerts,
            "charts": charts,
            "interactables": [e.render() for e in snap.elements][:120],
        })
        print(f"  [{stem}] {session.page.url}  ({len(snap.elements)} elements, "
              f"{len(charts['canvas'])} canvas, {len(charts['svg'])} svg, "
              f"{len(charts['legend_text'])} legend-ish strings)")
        return snap

    def save(self) -> Path:
        (self.dir / "findings.json").write_text(json.dumps(self.findings, indent=2, default=str))
        report = self.dir / "RECON.md"
        report.write_text(self._markdown(), encoding="utf-8")
        return report

    def _markdown(self) -> str:
        lines = ["# Reconnaissance", "",
                 f"Target: `{settings.target_base_url}`", ""]
        verdict = self.chart_verdict()
        lines += ["## Can chart values be read from the DOM?", "", f"**{verdict['verdict']}**", "",
                  verdict["detail"], ""]
        for step in self.findings["steps"]:
            lines += [f"## {step['step']}", "",
                      f"- URL: `{step['url']}`",
                      f"- Title: {step['title']}",
                      f"- Interactable elements: {step['element_count']}",
                      f"- Canvas: {len(step['charts']['canvas'])}, "
                      f"SVG: {len(step['charts']['svg'])}"]
            if step["note"]:
                lines.append(f"- Note: {step['note']}")
            if step["charts"]["legend_text"]:
                lines += ["", "Legend-shaped text found:", ""]
                lines += [f"  - `{t}`" for t in step["charts"]["legend_text"][:15]]
            if step["charts"]["suspects"]:
                lines += ["", "Standalone numbers:", ""]
                lines += [f"  - `{t}`" for t in step["charts"]["suspects"][:12]]
            if step["tables"]:
                lines += ["", "Tables:", ""]
                for t in step["tables"]:
                    lines.append(f"  - {t['row_count']} rows, headers: {t['headers']}")
            lines.append("")
        return "\n".join(lines)

    def chart_verdict(self) -> dict[str, str]:
        """The decision that shapes the build."""
        steps = self.findings["steps"]
        legend = sum(len(step["charts"]["legend_text"]) for step in steps)
        svg_text = sum(svg["text_nodes"] for step in steps for svg in step["charts"]["svg"])
        canvas = sum(len(step["charts"]["canvas"]) for step in steps)

        if legend or svg_text:
            return {"verdict": "YES — values are readable text.",
                    "detail": f"Found {legend} legend-shaped strings and {svg_text} SVG text "
                              "nodes. The existing DOM distiller can extract chart values "
                              "directly; no vision fallback needed."}
        if canvas:
            return {"verdict": "NO — charts appear to be canvas-rendered.",
                    "detail": f"Found {canvas} canvas elements and no readable value text. "
                              "A screenshot + vision extraction tier will be required, or the "
                              "underlying data must be intercepted from network responses."}
        return {"verdict": "INCONCLUSIVE.",
                "detail": "No charts detected on the pages visited. The analytics view may not "
                          "have been reached — check the screenshots."}


async def run_recon(headless: bool = True) -> Path:
    """Log in and walk the surfaces the chosen scenarios need."""
    from pi_operator.targets import get_target

    target = get_target()
    recon = Recon()
    session = BrowserSession(headless=headless, base_url=target.base_url,
                             artifacts_dir=recon.dir,
                             storage_state=target.auth_state_path)
    await session.start()

    try:
        print("logging in …")
        await session.goto("/login")
        await recon.capture(session, "login_page", "form structure before authenticating")

        await target.login(session)
        authed = await target.is_authenticated(session)
        recon.findings["authenticated"] = authed
        print(f"  authenticated: {authed}")
        await recon.capture(session, "after_login", "landing page and left navigation")

        # Scenario I and IV both start from the dealership list.
        found = await target.list_dealerships(session)
        recon.findings["dealerships_found"] = found
        print(f"  dealerships on this page: {len(found)}")
        for entry in found[:12]:
            print(f"    {entry['id']:>8}  {entry['name']}")

        for wanted in ("Ejner Hessel", "Approved Automotive"):
            entry = await target.find_dealership(session, wanted)
            if not entry:
                print(f"  ! could not find dealership {wanted!r} — may be on another page")
                recon.findings.setdefault("missing_dealerships", []).append(wanted)
                continue
            match = entry["name"]
            print(f"opening {match!r} (id {entry['id']}) …")
            await target.open_dealership(session, entry)
            await recon.capture(session, f"dealership_{_slug(match)}", "dealership landing")

            await target.open_tab(session, "Analytics")
            await recon.capture(session, f"analytics_{_slug(match)}", "analytics, default range")

            for days in ("30", "7"):
                if await target.set_date_range(session, days):
                    await session.page.mouse.wheel(0, 2000)
                    await recon.capture(session, f"analytics_{_slug(match)}_{days}d",
                                        f"analytics scrolled, {days} day range")

            for tab in ("Conversations",):
                if await target.open_tab(session, tab):
                    await recon.capture(session, f"{tab.lower()}_{_slug(match)}", f"{tab} tab")
    finally:
        report = recon.save()
        await session.close()

    print(f"\n{recon.chart_verdict()['verdict']}")
    print(f"wrote {report}")
    return report


def _slug(text: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in text).strip("_")[:30]
