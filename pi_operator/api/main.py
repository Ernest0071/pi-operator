"""Operator API and console.

The console is the human-in-the-loop surface: watch a run act in real time,
approve or reject the actions that stop at the gate, and answer the operator's
questions. It is also the demo surface — an approval landing in the browser
while the run waits is the clearest way to show the gate is real rather than
decorative.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from pi_operator.audit.trace import load_events
from pi_operator.config import settings
from pi_operator.graph.state import RunState
from pi_operator.runner import run_goal

app = FastAPI(title="PI Operator")


class StartRun(BaseModel):
    goal: str
    target: str | None = None
    headless: bool = True
    check: dict[str, Any] | None = None


class Decision(BaseModel):
    approved: bool = True
    note: str = ""


class Answer(BaseModel):
    answer: str


class RunHandle:
    """One in-flight run, plus the channel a human answers it through."""

    def __init__(self, run_id: str, goal: str) -> None:
        self.run_id = run_id
        self.goal = goal
        self.status = "starting"
        self.state: RunState | None = None
        self.pending: dict[str, Any] | None = None
        self._answer: asyncio.Future | None = None
        self.task: asyncio.Task | None = None

    async def wait_for_human(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Park the run until the console answers. This is the gate."""
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self.pending = payload
        self.status = "awaiting_" + payload.get("type", "input")
        try:
            return await self._answer
        finally:
            self.pending = None
            self.status = "running"

    def resolve(self, answer: dict[str, Any]) -> bool:
        if self._answer is None or self._answer.done():
            return False
        self._answer.set_result(answer)
        return True


RUNS: dict[str, RunHandle] = {}


@app.post("/api/runs")
async def start_run(payload: StartRun) -> dict[str, str]:
    import uuid

    run_id = uuid.uuid4().hex[:12]
    handle = RunHandle(run_id, payload.goal)
    RUNS[run_id] = handle

    async def execute() -> None:
        handle.status = "running"
        try:
            handle.state = await run_goal(
                payload.goal,
                target_name=payload.target,
                headless=payload.headless,
                verification_check=payload.check,
                on_interrupt=handle.wait_for_human,
                run_id=run_id,
            )
            handle.status = handle.state.status.value
        except Exception as exc:
            handle.status = "failed"
            handle.pending = {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    handle.task = asyncio.create_task(execute())
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    live = [
        {"run_id": h.run_id, "goal": h.goal, "status": h.status, "live": True}
        for h in RUNS.values()
    ]
    seen = {h["run_id"] for h in live}
    for path in sorted(settings.runs_dir.glob("*/summary.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:25]:
        data = json.loads(path.read_text())
        if data["run_id"] not in seen:
            live.append({"run_id": data["run_id"], "goal": data["goal"],
                         "status": data["status"], "live": False})
    return live


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    handle = RUNS.get(run_id)
    if handle:
        state = handle.state
        return {
            "run_id": run_id, "goal": handle.goal, "status": handle.status,
            "pending": handle.pending,
            "summary": state.summarize() if state else "",
            "steps": state.step_count if state else 0,
            "usd": round(state.usage.usd, 4) if state else 0.0,
            "plan": [s.model_dump() for s in state.plan] if state else [],
            "verification": state.verification.model_dump()
                            if state and state.verification else None,
        }
    path = settings.runs_dir / run_id / "summary.json"
    if not path.exists():
        raise HTTPException(404, f"no run {run_id}")
    return json.loads(path.read_text())


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str):
    """Tail the audit trace. The trace is the source of truth, not a side channel."""
    run_dir = settings.runs_dir / run_id

    async def generator():
        sent = 0
        idle = 0
        while idle < 600:
            events = load_events(run_dir)
            for event in events[sent:]:
                yield {"event": "step", "data": json.dumps(event)}
            if len(events) > sent:
                sent = len(events)
                idle = 0
            handle = RUNS.get(run_id)
            if handle:
                yield {"event": "status", "data": json.dumps(
                    {"status": handle.status, "pending": handle.pending})}
                if handle.status in {"succeeded", "failed", "aborted"}:
                    break
            idle += 1
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


@app.post("/api/runs/{run_id}/approve")
async def approve(run_id: str, decision: Decision) -> dict[str, Any]:
    handle = RUNS.get(run_id)
    if not handle:
        raise HTTPException(404, f"no live run {run_id}")
    if not handle.resolve({"approved": decision.approved, "note": decision.note}):
        raise HTTPException(409, "this run is not waiting for an approval")
    return {"ok": True, "approved": decision.approved}


@app.post("/api/runs/{run_id}/answer")
async def answer(run_id: str, payload: Answer) -> dict[str, Any]:
    handle = RUNS.get(run_id)
    if not handle:
        raise HTTPException(404, f"no live run {run_id}")
    if not handle.resolve({"answer": payload.answer}):
        raise HTTPException(409, "this run is not waiting for an answer")
    return {"ok": True}


@app.get("/api/runs/{run_id}/report")
async def report(run_id: str):
    path = settings.runs_dir / run_id / "report.html"
    if not path.exists():
        raise HTTPException(404, "no report yet")
    return FileResponse(path)


@app.get("/api/runs/{run_id}/screens/{name}")
async def screenshot(run_id: str, name: str):
    path = settings.runs_dir / run_id / "screens" / Path(name).name
    if not path.exists():
        raise HTTPException(404, "no such screenshot")
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
async def console() -> str:
    return CONSOLE_HTML


CONSOLE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PI Operator Console</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.55 ui-sans-serif, system-ui, sans-serif; margin: 0;
         background: #0f1115; color: #e6e8eb; }
  header { padding: .8rem 1.4rem; border-bottom: 1px solid #2a2f3a;
           display: flex; align-items: center; gap: 1rem; }
  h1 { font-size: 1rem; margin: 0; letter-spacing: .06em; text-transform: uppercase; }
  main { display: grid; grid-template-columns: 1fr 400px; gap: 1.2rem;
         padding: 1.2rem 1.4rem; align-items: start; }
  .panel { background: #161a22; border: 1px solid #2a2f3a; border-radius: 10px; padding: 1rem; }
  .panel h2 { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
              color: #8b93a3; margin: 0 0 .7rem; }
  input, button { font: inherit; }
  input[type=text] { width: 100%; padding: .55rem .7rem; border-radius: 6px;
                     border: 1px solid #333a47; background: #0f1115; color: inherit; }
  button { background: #2f6feb; color: #fff; border: 0; border-radius: 6px;
           padding: .5rem .9rem; cursor: pointer; }
  button.reject { background: #b4232a; } button.ghost { background: #333a47; }
  #steps { max-height: 62vh; overflow: auto; font-size: .84rem; }
  .step { padding: .4rem .1rem; border-bottom: 1px solid #222732; display: flex; gap: .6rem; }
  .step .tool { color: #7aa2f7; min-width: 116px; font-family: ui-monospace, monospace; }
  .step .msg { color: #c3c9d4; flex: 1; }
  .step.bad .msg { color: #ff8a8a; }
  .ev { color: #8b93a3; font-style: italic; padding: .3rem .1rem; font-size: .82rem; }
  #gate { border-color: #d29922; background: #241d08; }
  .kv { display: flex; gap: .5rem; font-size: .82rem; margin: .2rem 0; }
  .kv b { color: #8b93a3; font-weight: 500; min-width: 62px; }
  .pill { padding: .1rem .5rem; border-radius: 99px; font-size: .72rem; background: #333a47; }
  code { font-family: ui-monospace, monospace; font-size: .8rem; }
</style></head>
<body>
<header><h1>PI Operator</h1><span id="status" class="pill">idle</span>
  <span id="cost" class="pill"></span>
  <span style="flex:1"></span>
  <a id="reportLink" href="#" style="color:#7aa2f7;display:none">audit report</a>
</header>
<main>
  <div>
    <div class="panel" style="margin-bottom:1rem">
      <h2>Goal</h2>
      <input type="text" id="goal" placeholder="Add a 2021 Nissan Altima, VIN …, 38,400 miles, list at 19,750">
      <div style="margin-top:.6rem"><button onclick="start()">Run</button></div>
    </div>
    <div class="panel"><h2>Activity</h2><div id="steps"></div></div>
  </div>
  <div>
    <div class="panel" id="gate" style="display:none">
      <h2>Human decision required</h2>
      <div id="gateBody"></div>
    </div>
    <div class="panel" style="margin-top:1rem"><h2>Plan</h2><ol id="plan"></ol></div>
  </div>
</main>
<script>
let runId = null;
async function start() {
  const goal = document.getElementById('goal').value.trim();
  if (!goal) return;
  document.getElementById('steps').innerHTML = '';
  const r = await fetch('/api/runs', {method:'POST', headers:{'content-type':'application/json'},
                                      body: JSON.stringify({goal})});
  runId = (await r.json()).run_id;
  document.getElementById('reportLink').href = '/api/runs/' + runId + '/report';
  document.getElementById('reportLink').style.display = 'inline';
  listen();
  poll();
}
function listen() {
  const es = new EventSource('/api/runs/' + runId + '/events');
  es.addEventListener('step', e => {
    const d = JSON.parse(e.data);
    const box = document.getElementById('steps');
    const el = document.createElement('div');
    if (d.kind === 'step') {
      el.className = 'step' + (d.ok ? '' : ' bad');
      el.innerHTML = `<span class="tool">${d.tool || '—'}</span>
                      <span class="msg">${escapeHtml(d.message || '')}</span>`;
    } else {
      el.className = 'ev';
      el.textContent = `${d.node}: ${d.message || ''}`;
    }
    box.appendChild(el); box.scrollTop = box.scrollHeight;
  });
  es.addEventListener('status', e => {
    const d = JSON.parse(e.data);
    document.getElementById('status').textContent = d.status;
    renderGate(d.pending);
  });
}
async function poll() {
  if (!runId) return;
  const d = await (await fetch('/api/runs/' + runId)).json();
  document.getElementById('cost').textContent = '$' + (d.usd || 0).toFixed(3)
      + ' · ' + (d.steps || 0) + ' actions';
  document.getElementById('plan').innerHTML = (d.plan || [])
      .map(s => `<li style="color:${s.done ? '#6cc46c' : '#8b93a3'}">${escapeHtml(s.description)}</li>`)
      .join('');
  if (!['succeeded','failed','aborted'].includes(d.status)) setTimeout(poll, 1200);
}
function renderGate(p) {
  const gate = document.getElementById('gate');
  if (!p) { gate.style.display = 'none'; return; }
  gate.style.display = 'block';
  if (p.type === 'approval') {
    gate.querySelector('#gateBody').innerHTML = `
      <div class="kv"><b>Action</b><span>${escapeHtml(p.action || p.tool)}</span></div>
      <div class="kv"><b>Reason</b><span>${escapeHtml(p.reason || '')}</span></div>
      <div class="kv"><b>Risk</b><span>${escapeHtml(p.risk || '')}</span></div>
      <div class="kv"><b>Args</b><code>${escapeHtml(JSON.stringify(p.args || {}))}</code></div>
      <div style="margin-top:.8rem;display:flex;gap:.5rem">
        <button onclick="decide(true)">Approve</button>
        <button class="reject" onclick="decide(false)">Reject</button></div>`;
  } else if (p.type === 'question') {
    gate.querySelector('#gateBody').innerHTML = `
      <div class="kv"><b>Asks</b><span>${escapeHtml(p.question || '')}</span></div>
      <input type="text" id="ans" placeholder="your answer">
      <div style="margin-top:.6rem"><button onclick="sendAnswer()">Send</button></div>`;
  } else {
    gate.querySelector('#gateBody').textContent = p.message || '';
  }
}
async function decide(ok) {
  await fetch('/api/runs/' + runId + '/approve', {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({approved: ok, note: ok ? 'approved in console' : 'rejected in console'})});
  document.getElementById('gate').style.display = 'none';
}
async function sendAnswer() {
  await fetch('/api/runs/' + runId + '/answer', {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({answer: document.getElementById('ans').value})});
  document.getElementById('gate').style.display = 'none';
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }
</script>
</body></html>
"""
