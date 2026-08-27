"""Full-graph execution against the real mock DMS, with a scripted LLM.

This is the test that proves the orchestration actually runs: setup → plan →
navigate → approval gate → verify → report, with real Playwright, a real server,
real guardrails and a real audit trace. Only the model is faked, and it is faked
deterministically so the test asserts on wiring rather than on model behaviour.

It covers the paths that are otherwise only exercised by spending money:
the approval gate parking and resuming a run, verification overruling a false
success claim, and the trace surviving the whole thing.
"""

from __future__ import annotations

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from pi_operator.llm.base import LLMProvider, LLMResponse, ToolCall, Usage

BASE = "http://127.0.0.1:8080"


def _running() -> bool:
    try:
        return httpx.get(f"{BASE}/login", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not _running(), reason="mock DMS not running on :8080")]

VIN = "1N4AL3AP8JC007777"


class ScriptedProvider(LLMProvider):
    """Returns canned tool calls, routed by which agent is calling.

    The navigator script is a list of (tool_name, args). Anything not scripted
    falls through to `done`, so a wiring bug shows up as an early finish rather
    than a hang.
    """

    name = "scripted"

    def __init__(self, plan_steps, navigator_script, verdict=True):
        self.plan_steps = plan_steps
        self.script = list(navigator_script)
        self.verdict = verdict
        self.calls = 0

    async def complete(self, *, system, messages, tools=None, max_tokens=8000,
                       effort="medium", force_tool=None, cache_system=True,
                       allow_parallel_tools=False) -> LLMResponse:
        self.calls += 1
        names = {t["name"] for t in (tools or [])}

        if "submit_plan" in names:
            return self._call("submit_plan", {"steps": self.plan_steps})
        if "submit_verdict" in names:
            return self._call("submit_verdict", {
                "passed": self.verdict,
                "detail": "read the vehicle detail page and confirmed the VIN",
            })
        if self.script:
            tool, args = self.script.pop(0)
            return self._call(tool, args)
        return self._call("done", {"summary": "script exhausted", "evidence": "n/a"})

    def _call(self, name, args) -> LLMResponse:
        call_id = f"call_{self.calls}"
        return LLMResponse(
            text="", thinking=f"scripted step {self.calls}",
            tool_calls=[ToolCall(id=call_id, name=name, args=args)],
            stop_reason="tool_use", model="scripted",
            usage=Usage(input_tokens=1000, output_tokens=100, usd=0.001),
            raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        )


async def _reset(**faults):
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{BASE}/api/_reset")
        if faults:
            await client.post(f"{BASE}/api/_fault", json=faults)


def _build(provider, *, policy=None, check=None, trace_dir=None):
    """Assemble a supervisor around a scripted provider."""
    from pi_operator.agents.navigator import Navigator
    from pi_operator.agents.planner import Planner
    from pi_operator.agents.verifier import Verifier
    from pi_operator.graph.supervisor import Supervisor
    from pi_operator.guardrails.policy import Policy
    from pi_operator.targets import get_target

    target = get_target("mockdms", base_url=BASE, username="operator", password="operator")
    active = policy or Policy.for_target(target)
    return target, active, lambda session, trace: Supervisor(
        session=session, target=target,
        planner=Planner(provider),
        navigator=Navigator(provider, policy=active, target=target),
        verifier=Verifier(provider, target=target),
        policy=active, trace=trace, verification_check=check,
    )


async def _run(provider, script_state, *, policy=None, check=None, on_interrupt=None):
    from pi_operator.audit.trace import Trace
    from pi_operator.browser.session import BrowserSession
    from pi_operator.graph.state import RunState
    from pi_operator.runner import _drive, auto_approve

    target, _policy, make_supervisor = _build(provider, policy=policy, check=check)
    state = RunState(goal=script_state, target=target.name)
    trace = Trace(state.run_id)
    session = await BrowserSession(headless=True, base_url=BASE,
                                   artifacts_dir=trace.dir).start()
    try:
        supervisor = make_supervisor(session, trace)
        app = supervisor.build(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": state.run_id}, "recursion_limit": 120}
        return await _drive(app, state, config, on_interrupt or auto_approve), trace
    finally:
        await session.close()


PLAN = [{"description": "Open the new vehicle form and enter the vehicle details"},
        {"description": "Save the vehicle and confirm it was created"}]


def _intake_script():
    """Fill the intake form by accessible name, the way the navigator would."""
    return [
        ("navigate", {"url": "/inventory/new"}),
        ("type", {"ref": "@VIN", "text": VIN}),
        ("type", {"ref": "@Year", "text": "2021"}),
        ("type", {"ref": "@Make", "text": "Nissan"}),
        ("type", {"ref": "@Model", "text": "Altima"}),
        ("type", {"ref": "@Mileage", "text": "38400"}),
        ("type", {"ref": "@Asking Price", "text": "19750"}),
        ("select", {"ref": "@Status", "option": "listed"}),
        ("complete_step", {"notes": "details entered"}),
        ("click", {"ref": "@Save Vehicle"}),
        ("complete_step", {"notes": "saved"}),
        ("done", {"summary": f"added {VIN}", "evidence": "vehicle detail page shown"}),
    ]


class NameResolvingProvider(ScriptedProvider):
    """Resolves '@Accessible Name' placeholders against the live observation.

    Scripting raw refs would be fragile and would not exercise the real
    perception path; resolving by name each turn does.
    """

    def __init__(self, *args, session_getter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_getter = session_getter
        self.last_snapshot = None

    async def complete(self, **kwargs):
        response = await super().complete(**kwargs)
        for call in response.tool_calls:
            ref = call.args.get("ref", "")
            if isinstance(ref, str) and ref.startswith("@") and self.last_snapshot:
                want = ref[1:]
                match = next(
                    (e for e in self.last_snapshot.elements if e.name == want), None
                )
                call.args["ref"] = match.ref if match else "MISSING"
                option = call.args.get("option", "")
                if isinstance(option, str) and option.startswith("#") and match:
                    index = int(option[1:])
                    call.args["option"] = (match.options or [""])[index]
                response.raw_content[0]["input"] = call.args
        return response


@pytest.fixture
async def wired():
    """A provider that sees each fresh observation before scripting the next call."""
    await _reset()
    yield


async def test_full_run_creates_a_vehicle_and_verifies_it(wired):
    """The happy path, end to end through the real graph."""
    from pi_operator.audit.trace import Trace
    from pi_operator.browser.session import BrowserSession
    from pi_operator.graph.state import RunState, RunStatus
    from pi_operator.runner import _drive, auto_approve

    provider = NameResolvingProvider(PLAN, _intake_script())
    target, _policy, make_supervisor = _build(
        provider, check={"table": "vehicles", "filters": {"vin": VIN},
                         "expect": {"make": "Nissan", "status": "listed"}})

    state = RunState(goal=f"Add vehicle {VIN}", target=target.name)
    trace = Trace(state.run_id)
    session = await BrowserSession(headless=True, base_url=BASE,
                                   artifacts_dir=trace.dir).start()

    # Keep the scripted provider looking at the current page each turn.
    original_observe = session.observe

    async def observing():
        snap = await original_observe()
        provider.last_snapshot = snap
        return snap

    session.observe = observing

    try:
        supervisor = make_supervisor(session, trace)
        app = supervisor.build(checkpointer=InMemorySaver())
        final = await _drive(app, state,
                             {"configurable": {"thread_id": state.run_id},
                              "recursion_limit": 120}, auto_approve)
    finally:
        await session.close()

    assert final.status is RunStatus.SUCCEEDED, final.failure_reason
    assert final.verification and final.verification.passed
    assert "target-api" in final.verification.method

    async with httpx.AsyncClient() as client:
        check = (await client.post(f"{BASE}/api/_verify", json={
            "table": "vehicles", "filters": {"vin": VIN},
            "expect": {"make": "Nissan", "mileage": "38400"}})).json()
    assert check["passed"], check["detail"]

    report = trace.dir / "report.html"
    assert report.exists() and report.stat().st_size > 1000
    assert (trace.dir / "events.jsonl").exists()
    assert final.step_count > 5


async def test_verification_overrules_a_false_success_claim(wired):
    """The operator says done; the database disagrees. The run must fail."""
    from pi_operator.audit.trace import Trace
    from pi_operator.browser.session import BrowserSession
    from pi_operator.graph.state import RunState, RunStatus
    from pi_operator.runner import _drive, auto_approve

    # Claims success without ever filling the form.
    provider = NameResolvingProvider(
        PLAN,
        [("navigate", {"url": "/inventory/new"}),
         ("done", {"summary": "added the vehicle", "evidence": "it looked saved"})],
    )
    target, _policy, make_supervisor = _build(
        provider, check={"table": "vehicles", "filters": {"vin": "DOESNOTEXIST00000"}})

    state = RunState(goal="Add a vehicle", target=target.name)
    trace = Trace(state.run_id)
    session = await BrowserSession(headless=True, base_url=BASE,
                                   artifacts_dir=trace.dir).start()
    try:
        app = make_supervisor(session, trace).build(checkpointer=InMemorySaver())
        final = await _drive(app, state,
                             {"configurable": {"thread_id": state.run_id},
                              "recursion_limit": 120}, auto_approve)
    finally:
        await session.close()

    assert final.status is RunStatus.FAILED
    assert "verification failed" in final.failure_reason


async def test_approval_gate_parks_the_run_and_a_rejection_is_respected(wired):
    """A gated action must not execute until a human answers — and a 'no' must hold."""
    from pi_operator.audit.trace import Trace
    from pi_operator.browser.session import BrowserSession
    from pi_operator.graph.state import RunState
    from pi_operator.runner import _drive

    seen: list[dict] = []

    async def reject(payload):
        seen.append(payload)
        return {"approved": False, "note": "not authorised to submit deals"}

    provider = NameResolvingProvider(
        [{"description": "Build the deal and submit it"}],
        [("navigate", {"url": "/deals/new?step=1"}),
         ("select", {"ref": "@Customer", "option": "Marcus Webb"}),
         ("click", {"ref": "@Continue to Vehicle"}),
         ("select", {"ref": "@Vehicle", "option": "#1"}),
         ("click", {"ref": "@Continue to F&I"}),
         ("click", {"ref": "@Submit for Finance Approval"}),   # <- must be gated
         ("fail", {"reason": "human rejected the submission"})],
    )
    target, _policy, make_supervisor = _build(provider)

    state = RunState(goal="Submit a deal for Marcus Webb", target=target.name)
    trace = Trace(state.run_id)
    session = await BrowserSession(headless=True, base_url=BASE,
                                   artifacts_dir=trace.dir).start()
    original = session.observe

    async def observing():
        snap = await original()
        provider.last_snapshot = snap
        return snap

    session.observe = observing

    try:
        app = make_supervisor(session, trace).build(checkpointer=InMemorySaver())
        await _drive(app, state, {"configurable": {"thread_id": state.run_id},
                                  "recursion_limit": 120}, reject)
    finally:
        await session.close()

    assert seen, "the run should have stopped for human approval"
    approval = next((p for p in seen if p.get("type") == "approval"), None)
    assert approval is not None
    assert "Submit" in (approval.get("action") or approval.get("tool", ""))
    assert approval["risk"] in {"medium", "high"}

    async with httpx.AsyncClient() as client:
        check = (await client.post(f"{BASE}/api/_verify",
                                   json={"table": "deals", "filters": {"id": 1}})).json()
    assert not check["passed"], "a rejected submission must not have created a deal"
