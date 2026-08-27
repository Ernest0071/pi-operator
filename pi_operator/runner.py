"""Top-level entry point: assemble the operator and run one goal to completion.

This is the only place that knows how all the pieces fit together, which keeps
the CLI, the HTTP API and the eval harness from each growing their own slightly
different wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command

from pi_operator.agents.navigator import Navigator
from pi_operator.agents.planner import Planner
from pi_operator.agents.verifier import Verifier
from pi_operator.audit.trace import Trace
from pi_operator.browser.session import BrowserSession
from pi_operator.browser.tools import ToolRegistry
from pi_operator.config import settings
from pi_operator.graph.state import RunState, RunStatus
from pi_operator.graph.supervisor import Supervisor
from pi_operator.guardrails.policy import Policy
from pi_operator.llm.anthropic_provider import get_provider
from pi_operator.skills.registry import SkillRegistry
from pi_operator.targets import get_target

# Called when the run stops for a human. Receives the interrupt payload, returns
# {"approved": bool, "note": str} for approvals or {"answer": str} for questions.
InterruptHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


async def auto_approve(payload: dict[str, Any]) -> dict[str, Any]:
    """Unattended default, used by the eval harness.

    Approves and answers nothing useful, on purpose: a scenario that needs a
    real human answer should fail an unattended run rather than quietly proceed
    on a fabricated one.
    """
    if payload.get("type") == "approval":
        return {"approved": True, "note": "auto-approved (unattended run)"}
    return {"answer": "No human is available. Proceed only if you can without this."}


async def deny_all(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("type") == "approval":
        return {"approved": False, "note": "auto-rejected (policy test)"}
    return {"answer": "No human is available."}


async def run_goal(
    goal: str,
    *,
    target_name: str | None = None,
    params: dict[str, Any] | None = None,
    verification_check: dict[str, Any] | None = None,
    on_interrupt: InterruptHandler | None = None,
    headless: bool | None = None,
    policy: Policy | None = None,
    checkpoint_path: Path | None = None,
    run_id: str | None = None,
) -> RunState:
    """Run one goal end to end and return the final state."""
    provider_check = get_provider()
    if hasattr(provider_check, "preflight"):
        provider_check.preflight()   # before a browser is launched

    target = get_target(target_name)
    state = RunState(goal=goal, target=target.name)
    if run_id:
        state.run_id = run_id
    if params:
        state.result["params"] = dict(params)

    trace = Trace(state.run_id)
    session = BrowserSession(
        headless=settings.headless if headless is None else headless,
        artifacts_dir=trace.dir,
        base_url=target.base_url,
    )
    await session.start()

    skills = SkillRegistry()
    active_policy = policy or Policy.for_target(target)
    provider = get_provider()

    supervisor = Supervisor(
        session=session,
        target=target,
        planner=Planner(get_provider(settings.planner_model)),
        navigator=Navigator(
            provider,
            policy=active_policy,
            target=target,
            registry=ToolRegistry(),
            skills_catalogue=skills.catalogue(target.name),
        ),
        verifier=Verifier(provider, target=target),
        policy=active_policy,
        skills=skills,
        trace=trace,
        verification_check=verification_check,
    )

    handler = on_interrupt or auto_approve
    config: dict[str, Any] = {
        "configurable": {"thread_id": state.run_id},
        # Each navigate iteration is a node visit, so the ceiling must clear the
        # step budget with room for gates and replans.
        "recursion_limit": max(50, active_policy.max_steps * 4),
    }

    try:
        if checkpoint_path is not None:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                app = supervisor.build(checkpointer=saver)
                final = await _drive(app, state, config, handler)
        else:
            # A checkpointer is not optional: LangGraph's interrupt() — which is
            # what the approval gate is built on — cannot resume without one.
            # In-memory is enough when the run is not meant to outlive the process.
            from langgraph.checkpoint.memory import InMemorySaver

            app = supervisor.build(checkpointer=InMemorySaver())
            final = await _drive(app, state, config, handler)
    finally:
        await session.close()

    return final


async def _drive(app, state: RunState, config: dict[str, Any], handler: InterruptHandler) -> RunState:
    """Run the graph, servicing human-in-the-loop interrupts until it finishes."""
    result = await app.ainvoke(state, config)

    while True:
        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        if not interrupts:
            break
        payload = getattr(interrupts[0], "value", interrupts[0])
        answer = handler(payload)
        if inspect.isawaitable(answer):
            answer = await answer
        result = await app.ainvoke(Command(resume=answer), config)

    if isinstance(result, RunState):
        return result
    clean = {k: v for k, v in result.items() if not k.startswith("__")}
    return RunState.model_validate(clean)


def print_summary(state: RunState) -> None:
    from rich.console import Console

    console = Console()
    colour = {
        RunStatus.SUCCEEDED: "green",
        RunStatus.FAILED: "red",
        RunStatus.ABORTED: "yellow",
    }.get(state.status, "cyan")

    console.print(f"\n[{colour}]{state.status.value.upper()}[/] — {state.summarize()}")
    if state.verification:
        mark = "[green]passed[/]" if state.verification.passed else "[red]failed[/]"
        console.print(f"  verification {mark} ({state.verification.method}): "
                      f"{state.verification.detail}")
    if state.failure_reason:
        console.print(f"  [red]reason:[/] {state.failure_reason}")
    if state.evidence:
        console.print(f"  evidence: {state.evidence}")
    console.print(f"  trace: runs/{state.run_id}/report.html")
