"""Audit trace: a replayable record of everything an operator did and why.

An autonomous system that acts on a business's data has to be able to answer
"what exactly did it do, and on what basis" after the fact — for debugging, for
the person who has to explain it to a dealer principal, and for the eval harness.

Every step writes one JSONL line at the moment it happens, so a crashed or
killed run still leaves a complete trail up to the failure. The HTML report is
generated from that file, not held in memory.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from pi_operator.config import settings
from pi_operator.graph.state import RunState


class Trace:
    def __init__(self, run_id: str, root: Path | None = None) -> None:
        self.run_id = run_id
        self.dir = (root or settings.runs_dir) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.started = time.time()
        self._last_step_written = 0

    # -- writing ---------------------------------------------------------

    def _append(self, payload: dict[str, Any]) -> None:
        payload["t"] = round(time.time() - self.started, 3)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

    def event(self, state: RunState, node: str, message: str) -> None:
        """Record a node-level event, plus any steps taken since the last call."""
        self._append({"kind": "event", "node": node, "message": message,
                      "status": state.status.value})
        self._flush_steps(state)

    def _flush_steps(self, state: RunState) -> None:
        for record in state.history[self._last_step_written:]:
            self._append({
                "kind": "step",
                "index": record.index,
                "node": record.node,
                "tool": record.tool,
                "args": record.args,
                "element": record.element,
                "ok": record.ok,
                "message": record.message,
                "thought": record.thought,
                "url": record.url,
                "digest": record.observation_digest,
                "screenshot": record.screenshot,
                "usd": round(record.usage.usd, 5),
                "ms": record.duration_ms,
            })
        self._last_step_written = len(state.history)

    def finish(self, state: RunState) -> Path:
        self._flush_steps(state)
        self._append({"kind": "finish", "status": state.status.value,
                      "summary": state.summarize()})

        summary = {
            "run_id": state.run_id,
            "goal": state.goal,
            "target": state.target,
            "status": state.status.value,
            "steps": state.step_count,
            "replans": state.replans,
            "elapsed_s": round(state.elapsed_s, 1),
            "usd": round(state.usage.usd, 4),
            "tokens": {
                "input": state.usage.input_tokens,
                "output": state.usage.output_tokens,
                "cache_read": state.usage.cache_read_tokens,
            },
            "verification": state.verification.model_dump() if state.verification else None,
            "failure_reason": state.failure_reason,
            "evidence": state.evidence,
            "result": {k: v for k, v in state.result.items() if not k.startswith("_")},
            "approval": state.approval.model_dump() if state.approval else None,
            "plan": [s.model_dump() for s in state.plan],
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        (self.dir / "state.json").write_text(state.model_dump_json(indent=2))
        return self.render_html(state)

    # -- reporting -------------------------------------------------------

    def render_html(self, state: RunState) -> Path:
        rows: list[str] = []
        for record in state.history:
            status = "ok" if record.ok else "bad"
            shot = ""
            if record.screenshot:
                rel = Path(record.screenshot)
                try:
                    rel = rel.relative_to(self.dir)
                except ValueError:
                    pass
                shot = f'<a class="shot" href="{html.escape(str(rel))}">screenshot</a>'
            args = html.escape(json.dumps(record.args, default=str))[:300]
            element = html.escape(json.dumps(record.element)) if record.element else "&mdash;"
            thought = html.escape(record.thought or "")[:600]
            rows.append(f"""
    <tr class="{status}">
      <td class="num">{record.index}</td>
      <td><code>{html.escape(record.tool or '—')}</code></td>
      <td class="args"><code>{args}</code></td>
      <td class="el"><code>{element}</code></td>
      <td>{html.escape(record.message)[:300]}</td>
      <td class="num">{record.duration_ms}ms</td>
      <td class="num">${record.usage.usd:.4f}</td>
      <td>{shot}</td>
    </tr>
    <tr class="thought-row"><td></td><td colspan="7" class="thought">{thought}</td></tr>""")

        verification = state.verification
        verdict = "not run" if not verification else (
            f"{'PASSED' if verification.passed else 'FAILED'} "
            f"({verification.method}) — {html.escape(verification.detail)}"
        )
        approval = "none"
        if state.approval:
            approval = (
                f"{'approved' if state.approval.approved else 'rejected'} by "
                f"{html.escape(state.approval.decided_by)}"
                + (f" — {html.escape(state.approval.note)}" if state.approval.note else "")
            )

        plan_items = "".join(
            f'<li class="{"done" if s.done else "todo"}">{html.escape(s.description)}'
            + (f' <span class="skill">via {html.escape(s.skill)}</span>' if s.skill else "")
            + (f'<div class="note">{html.escape(s.notes)}</div>' if s.notes else "")
            + "</li>"
            for s in state.plan
        ) or "<li>(no plan)</li>"

        doc = f"""<!doctype html>
<meta charset="utf-8">
<title>Run {html.escape(state.run_id)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 1200px;
         padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .2rem; }}
  .goal {{ color: #666; margin-bottom: 1.5rem; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .card {{ border: 1px solid #8884; border-radius: 8px; padding: .6rem .9rem; min-width: 120px; }}
  .card b {{ display: block; font-size: 1.2rem; }}
  .card span {{ color: #888; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }}
  .status-succeeded {{ color: #1a7f37; }} .status-failed {{ color: #c33; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #8882;
            vertical-align: top; }}
  th {{ font-size: .75rem; text-transform: uppercase; color: #888; letter-spacing: .04em; }}
  tr.bad td {{ background: #ff000010; }}
  td.num {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.args, td.el {{ max-width: 260px; overflow-wrap: anywhere; }}
  code {{ font-size: .8rem; }}
  .thought-row td {{ border-bottom: none; padding-top: 0; }}
  .thought {{ color: #888; font-style: italic; font-size: .82rem; }}
  ol.plan li.done {{ color: #1a7f37; }} ol.plan li.todo {{ color: #999; }}
  .skill {{ background: #8882; border-radius: 4px; padding: 0 .3rem; font-size: .75rem; }}
  .note {{ color: #888; font-size: .8rem; }}
  .box {{ border: 1px solid #8884; border-left-width: 3px; border-radius: 6px;
          padding: .6rem .9rem; margin: .5rem 0; }}
</style>
<h1>Run {html.escape(state.run_id)}
  <span class="status-{state.status.value}">{state.status.value}</span></h1>
<div class="goal">{html.escape(state.goal)} &middot; target <b>{html.escape(state.target)}</b></div>

<div class="cards">
  <div class="card"><span>Actions</span><b>{state.step_count}</b></div>
  <div class="card"><span>Elapsed</span><b>{state.elapsed_s:.0f}s</b></div>
  <div class="card"><span>Cost</span><b>${state.usage.usd:.3f}</b></div>
  <div class="card"><span>Replans</span><b>{state.replans}</b></div>
  <div class="card"><span>Cache reads</span><b>{state.usage.cache_read_tokens:,}</b></div>
</div>

<div class="box"><b>Verification:</b> {verdict}</div>
<div class="box"><b>Human approval:</b> {approval}</div>
{f'<div class="box"><b>Failure:</b> {html.escape(state.failure_reason)}</div>' if state.failure_reason else ''}
{f'<div class="box"><b>Evidence:</b> {html.escape(state.evidence)}</div>' if state.evidence else ''}

<h2>Plan</h2>
<ol class="plan">{plan_items}</ol>

<h2>Actions</h2>
<table>
  <tr><th>#</th><th>Tool</th><th>Args</th><th>Element</th><th>Result</th>
      <th>Time</th><th>Cost</th><th></th></tr>
  {''.join(rows)}
</table>
"""
        path = self.dir / "report.html"
        path.write_text(doc, encoding="utf-8")
        return path


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
