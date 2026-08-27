"""MCP server exposing the operator as tools.

The point is that an autonomous operator is a *capability*, not a script. Once
the workflows are behind a tool interface, anything that speaks MCP — Claude
Desktop, Claude Code, another agent — can ask the dealership system to do
something without knowing anything about Playwright, the DOM, or the target's
menu structure.

That is also the honest version of "agents interacting with software": the
browser work stays here, behind a stable contract, instead of leaking into
every caller.

Run with:  python -m pi_operator.mcp_server
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from pi_operator.config import settings
from pi_operator.runner import auto_approve, run_goal
from pi_operator.skills.registry import SkillRegistry

server = MCPServer(
    name="pi-operator",
    title="PI Operator",
    instructions=(
        "Autonomous operators that complete dealership workflows by driving a "
        "dealership management system through a real browser. Actions that are "
        "irreversible or move money stop for human approval unless explicitly "
        "run unattended."
    ),
)


@server.tool(
    name="run_dealership_workflow",
    description=(
        "Complete a dealership workflow end to end by driving the DMS in a browser. "
        "Give the goal in plain language, including any specific values (VIN, price, "
        "customer name) the operator will need — it will stop and ask rather than "
        "invent missing data. Returns the outcome plus independent verification."
    ),
)
async def run_dealership_workflow(
    goal: str,
    target: str | None = None,
    unattended: bool = False,
) -> dict[str, Any]:
    """Run one goal. ``unattended`` auto-approves gated actions — use with care."""
    state = await run_goal(
        goal,
        target_name=target,
        on_interrupt=auto_approve if unattended else None,
        headless=True,
    )
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "summary": state.summarize(),
        "verified": state.verification.passed if state.verification else None,
        "verification_detail": state.verification.detail if state.verification else "",
        "evidence": state.evidence,
        "failure_reason": state.failure_reason,
        "pending_question": state.pending_question,
        "actions": state.step_count,
        "cost_usd": round(state.usage.usd, 4),
        "audit_report": f"runs/{state.run_id}/report.html",
    }


@server.tool(
    name="list_dealership_skills",
    description=(
        "List the deterministic skills the operator can run without model reasoning. "
        "Skills are promoted from successful runs, so this grows over time."
    ),
)
async def list_dealership_skills(target: str | None = None) -> str:
    registry = SkillRegistry()
    return registry.catalogue(target or settings.target)


@server.tool(
    name="get_dealership_run",
    description="Fetch the outcome and audit summary of a previous operator run by id.",
)
async def get_dealership_run(run_id: str) -> dict[str, Any]:
    path = settings.runs_dir / run_id / "summary.json"
    if not path.exists():
        return {"error": f"no run {run_id}"}
    return json.loads(path.read_text())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
