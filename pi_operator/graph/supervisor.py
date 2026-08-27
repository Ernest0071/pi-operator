"""Supervisor: the LangGraph state machine that orchestrates the operator.

    setup → plan → route ┬→ run_skill ─┐
                         └→ navigate ──┼→ (approval_gate) → navigate
                                       ├→ (human_gate)    → navigate
                                       ├→ replan          → route
                                       └→ verify → report

Why a state machine rather than one agent loop
----------------------------------------------
Three things need to happen *between* actions, and none of them belong to the
model: enforcing budgets, stopping for human approval, and deciding when a plan
has stopped working and needs replacing. Putting that control flow in explicit,
inspectable edges — rather than hoping a prompt enforces it — is the difference
between a demo and something you would let near a customer's DMS.

Checkpointing note
------------------
State is checkpointed to SQLite, so an approval can be answered minutes or hours
later and the run resumes with its full plan, history and conversation intact.
The browser itself is not serialisable: resuming in a fresh process
re-authenticates and re-navigates rather than restoring a live DOM. That is a
real limitation and is documented rather than hidden.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from pi_operator.agents.navigator import Navigator
from pi_operator.agents.planner import Planner
from pi_operator.agents.verifier import Verifier
from pi_operator.audit.trace import Trace
from pi_operator.browser.session import BrowserSession
from pi_operator.graph.state import (
    ApprovalDecision,
    RunState,
    RunStatus,
    StepRecord,
)
from pi_operator.guardrails.policy import Policy
from pi_operator.skills.registry import SkillRegistry
from pi_operator.targets.base import TargetAdapter

MAX_REPLANS = 2


class Supervisor:
    """Builds and runs the operator graph for one target/session pair."""

    def __init__(
        self,
        *,
        session: BrowserSession,
        target: TargetAdapter,
        planner: Planner,
        navigator: Navigator,
        verifier: Verifier,
        policy: Policy,
        skills: SkillRegistry | None = None,
        trace: Trace | None = None,
        verification_check: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.target = target
        self.planner = planner
        self.navigator = navigator
        self.verifier = verifier
        self.policy = policy
        self.skills = skills or SkillRegistry()
        self.trace = trace
        self.verification_check = verification_check

    # ---------------------------------------------------------------- nodes

    async def setup(self, state: RunState) -> dict[str, Any]:
        state.status = RunStatus.PLANNING
        state.target = self.target.name
        try:
            await self.target.ensure_authenticated(self.session)
        except Exception as exc:
            state.status = RunStatus.FAILED
            state.failure_reason = f"authentication failed: {exc}"
            return _commit(state)
        await self.session.goto(self.target.entry_url())
        self._emit(state, "setup", f"authenticated against {self.target.name}")
        return _commit(state)

    async def plan(self, state: RunState) -> dict[str, Any]:
        result = await self.planner.plan(
            goal=state.goal,
            target_description=self.target.describe(),
            skills_catalogue=self.skills.catalogue(self.target.name),
            prior_failure=state.last_message if state.replans else "",
        )
        state.usage = state.usage + result.usage

        if result.clarification_needed and not state.human_answer:
            state.status = RunStatus.AWAITING_INPUT
            state.pending_question = result.clarification_needed
            state.last_outcome = "needs_human"
            return _commit(state)

        if not result.steps:
            state.status = RunStatus.FAILED
            state.failure_reason = "planner produced no usable plan"
            state.last_outcome = "failed"
            return _commit(state)

        state.plan = result.steps
        state.cursor = 0
        state.status = RunStatus.RUNNING
        state.last_outcome = "planned"
        self._emit(state, "plan", f"planned {len(result.steps)} steps")
        return _commit(state)

    async def route(self, state: RunState) -> dict[str, Any]:
        """Choose the cheapest mechanism that can perform the current step."""
        step = state.current_step
        state.last_outcome = "navigate"
        if step and step.skill and self.skills.get(step.skill):
            state.last_outcome = "skill"
        return _commit(state)

    async def run_skill(self, state: RunState) -> dict[str, Any]:
        step = state.current_step
        assert step and step.skill
        skill = self.skills.get(step.skill)
        assert skill

        params = dict(state.result.get("params", {}))
        result = await skill.run(self.session, params)

        record = StepRecord(
            index=state.step_count, node="skill", tool=f"skill:{step.skill}",
            args=params, ok=result.ok, message=result.message,
            duration_ms=result.duration_ms,
        )
        state.record(record)
        self._emit(state, "skill", f"{step.skill}: {result.message}")

        if result.ok:
            step.done = True
            step.notes = result.message
            state.cursor += 1
            if result.drifted:
                # Not a failure, but the target has moved under us. Surfacing it
                # here is what stops silent selector rot.
                step.notes += f" (selector healing: {'; '.join(result.healed)})"
            state.last_outcome = "step_done" if not state.plan_complete else "plan_complete"
        else:
            # Skills are an optimisation, never a dependency. Falling back to the
            # model is the whole point of having both.
            step.skill = None
            step.notes = f"skill failed, falling back to navigation: {result.message}"
            state.last_outcome = "navigate"

        return _commit(state)

    async def navigate(self, state: RunState) -> dict[str, Any]:
        outcome = await self.navigator.step(state, self.session)
        state.last_outcome = outcome.kind
        state.last_message = outcome.message
        if outcome.record:
            self._emit(state, "navigate",
                       f"{outcome.record.tool or '(no action)'}: {outcome.message}")

        if outcome.kind == "step_done":
            step = state.current_step
            if step:
                step.done = True
                step.notes = outcome.message
            state.cursor += 1
            if state.plan_complete:
                state.last_outcome = "plan_complete"

        elif outcome.kind == "needs_approval":
            state.status = RunStatus.AWAITING_APPROVAL

        elif outcome.kind == "needs_human":
            state.status = RunStatus.AWAITING_INPUT
            state.pending_question = outcome.question

        elif outcome.kind == "done":
            state.result.update(outcome.result)
            state.evidence = outcome.evidence
            state.status = RunStatus.VERIFYING

        elif outcome.kind in ("failed", "denied"):
            state.status = RunStatus.FAILED
            state.failure_reason = outcome.message

        return _commit(state)

    async def approval_gate(self, state: RunState) -> dict[str, Any]:
        """Hand control to a human, then resume exactly where we stopped."""
        pending = state.pending_approval
        assert pending

        answer = interrupt({
            "type": "approval",
            "run_id": state.run_id,
            "goal": state.goal,
            "action": pending.summary or pending.tool,
            "tool": pending.tool,
            "args": pending.args,
            "reason": pending.reason,
            "risk": pending.risk,
            "url": state.history[-1].url if state.history else "",
        })

        approved = bool(answer.get("approved")) if isinstance(answer, dict) else bool(answer)
        note = answer.get("note", "") if isinstance(answer, dict) else ""

        state.approval = ApprovalDecision(approved=approved, note=note)
        state.status = RunStatus.RUNNING
        self._emit(state, "approval",
                   f"{'approved' if approved else 'rejected'}: {pending.summary} — {note}")

        outcome = await self.navigator.resume_after_approval(
            state, self.session, approved=approved, note=note
        )
        state.last_outcome = outcome.kind
        state.last_message = outcome.message

        if outcome.kind == "done":
            state.result.update(outcome.result)
            state.evidence = outcome.evidence
            state.status = RunStatus.VERIFYING
        elif outcome.kind in ("failed", "denied"):
            state.status = RunStatus.FAILED
            state.failure_reason = outcome.message

        return _commit(state)

    async def human_gate(self, state: RunState) -> dict[str, Any]:
        answer = interrupt({
            "type": "question",
            "run_id": state.run_id,
            "goal": state.goal,
            "question": state.pending_question or "",
        })
        text = answer.get("answer", "") if isinstance(answer, dict) else str(answer)

        state.human_answer = text
        state.pending_question = None
        state.status = RunStatus.RUNNING
        state.messages.append({
            "role": "user",
            "content": f"A human operator answered your question: {text}",
        })
        self._emit(state, "human", f"answered: {text[:120]}")
        state.last_outcome = "answered"
        return _commit(state)

    async def replan(self, state: RunState) -> dict[str, Any]:
        state.replans += 1
        state.digest_counts.clear()
        state.consecutive_failures = 0
        state.last_message = (
            state.last_message
            or "the previous plan stopped making progress (repeated identical screens)"
        )
        self._emit(state, "replan", f"replan #{state.replans}: {state.last_message}")
        return await self.plan(state)

    async def verify(self, state: RunState) -> dict[str, Any]:
        state.status = RunStatus.VERIFYING
        output = await self.verifier.verify(
            state, self.session, check=self.verification_check
        )
        state.usage = state.usage + output.usage
        state.verification = output.result
        self._emit(state, "verify",
                   f"{'passed' if output.result.passed else 'FAILED'}: {output.result.detail[:200]}")
        return _commit(state)

    async def report(self, state: RunState) -> dict[str, Any]:
        if state.status is RunStatus.FAILED:
            pass
        elif state.verification and not state.verification.passed:
            state.status = RunStatus.FAILED
            state.failure_reason = f"verification failed: {state.verification.detail}"
        elif state.verification and state.verification.passed:
            state.status = RunStatus.SUCCEEDED
        else:
            state.status = RunStatus.FAILED
            state.failure_reason = state.failure_reason or "run ended without verification"

        self._emit(state, "report", state.summarize())
        if self.trace:
            self.trace.finish(state)
        return _commit(state)

    # ------------------------------------------------------------ routing

    @staticmethod
    def after_plan(state: RunState) -> str:
        if state.status is RunStatus.AWAITING_INPUT:
            return "human_gate"
        if state.status is RunStatus.FAILED:
            return "report"
        return "route"

    @staticmethod
    def after_route(state: RunState) -> str:
        return "run_skill" if state.last_outcome == "skill" else "navigate"

    @staticmethod
    def after_skill(state: RunState) -> str:
        if state.last_outcome == "plan_complete":
            return "verify"
        return "route"

    @staticmethod
    def after_navigate(state: RunState) -> str:
        outcome = state.last_outcome

        if outcome == "needs_approval":
            return "approval_gate"
        if outcome == "needs_human":
            return "human_gate"
        if outcome == "done":
            return "verify"
        if outcome in ("failed", "denied"):
            return "report"
        if outcome == "plan_complete":
            return "verify"

        # Progress checks. Oscillation is not the same as failure: the actions
        # succeed, they just stop changing anything.
        if state.oscillating() or outcome == "stalled" or state.consecutive_failures >= 3:
            return "replan" if state.replans < MAX_REPLANS else "report"

        return "navigate"

    @staticmethod
    def after_gate(state: RunState) -> str:
        if state.status is RunStatus.FAILED:
            return "report"
        if state.last_outcome == "done" or state.status is RunStatus.VERIFYING:
            return "verify"
        return "navigate"

    # -------------------------------------------------------------- build

    def build(self, checkpointer=None):
        graph = StateGraph(RunState)

        graph.add_node("setup", self.setup)
        graph.add_node("plan", self.plan)
        graph.add_node("route", self.route)
        graph.add_node("run_skill", self.run_skill)
        graph.add_node("navigate", self.navigate)
        graph.add_node("approval_gate", self.approval_gate)
        graph.add_node("human_gate", self.human_gate)
        graph.add_node("replan", self.replan)
        graph.add_node("verify", self.verify)
        graph.add_node("report", self.report)

        graph.add_edge(START, "setup")
        graph.add_conditional_edges(
            "setup",
            lambda s: "report" if s.status is RunStatus.FAILED else "plan",
            {"plan": "plan", "report": "report"},
        )
        graph.add_conditional_edges(
            "plan", self.after_plan,
            {"route": "route", "human_gate": "human_gate", "report": "report"},
        )
        graph.add_conditional_edges(
            "route", self.after_route,
            {"run_skill": "run_skill", "navigate": "navigate"},
        )
        graph.add_conditional_edges(
            "run_skill", self.after_skill, {"route": "route", "verify": "verify"},
        )
        graph.add_conditional_edges(
            "navigate", self.after_navigate,
            {
                "navigate": "navigate", "approval_gate": "approval_gate",
                "human_gate": "human_gate", "verify": "verify",
                "replan": "replan", "report": "report",
            },
        )
        graph.add_conditional_edges(
            "approval_gate", self.after_gate,
            {"navigate": "navigate", "verify": "verify", "report": "report"},
        )
        graph.add_conditional_edges(
            "human_gate", self.after_gate,
            {"navigate": "navigate", "verify": "verify", "report": "report"},
        )
        graph.add_conditional_edges(
            "replan", self.after_plan,
            {"route": "route", "human_gate": "human_gate", "report": "report"},
        )
        graph.add_edge("verify", "report")
        graph.add_edge("report", END)

        return graph.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------ helpers

    def _emit(self, state: RunState, node: str, message: str) -> None:
        if self.trace:
            self.trace.event(state, node, message)


def _commit(state: RunState) -> dict[str, Any]:
    """Nodes mutate state in place; this publishes the whole object to the graph.

    Returning the full dump rather than a field subset keeps node code honest —
    a node that forgets to declare a mutated field cannot silently lose it.
    """
    return state.model_dump()
