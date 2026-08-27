"""Navigator sub-agent: the observe-act-observe loop that drives the browser.

Deliberately exposes a single ``step()`` rather than running its own loop. The
graph owns iteration, which is what allows a run to be checkpointed and
interrupted mid-workflow for human approval — an agent that owns its own
``while`` loop cannot be paused for an hour and resumed.

Context growth is the other thing this file manages. Naively appending every
observation makes step 40 cost ten times what step 4 did. Older tool results are
compacted to one line, because what matters from six steps ago is that the click
worked, not the page it produced.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from pi_operator.agents.prompts import NAVIGATOR_SYSTEM, navigator_context
from pi_operator.browser.perception import Snapshot
from pi_operator.browser.session import BrowserSession
from pi_operator.browser.tools import ToolRegistry, ToolResult
from pi_operator.graph.state import ApprovalRequest, RunState, StepRecord
from pi_operator.guardrails.policy import Policy
from pi_operator.llm.base import LLMProvider

OutcomeKind = Literal[
    "acted", "step_done", "needs_approval", "needs_human", "done", "failed", "denied", "stalled",
]

# How many recent tool results keep their full observation text.
FULL_DETAIL_WINDOW = 3


class StepOutcome(BaseModel):
    kind: OutcomeKind
    record: StepRecord | None = None
    approval: ApprovalRequest | None = None
    question: str = ""
    message: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: str = ""


class Navigator:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        policy: Policy,
        target,
        registry: ToolRegistry | None = None,
        skills_catalogue: str = "",
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.target = target
        self.registry = registry or ToolRegistry()
        self.skills_catalogue = skills_catalogue

    # -- prompt assembly -------------------------------------------------

    def _system(self, state: RunState) -> str:
        step = state.current_step
        plan_line = f"{step.id + 1}. {step.description}" if step else "(no plan step; use judgement)"
        notes = "\n".join(f"  - {s.notes}" for s in state.plan if s.notes)
        return (
            NAVIGATOR_SYSTEM
            + "\n\n"
            + navigator_context(
                goal=state.goal,
                target_description=self.target.describe(),
                plan_line=plan_line,
                skills=self.skills_catalogue,
                notes=notes,
            )
        )

    @staticmethod
    def _compact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Shrink older observations so context does not grow without bound."""
        tool_result_positions = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        ]
        keep_from = set(tool_result_positions[-FULL_DETAIL_WINDOW:])

        out: list[dict[str, Any]] = []
        for i, message in enumerate(messages):
            if i in tool_result_positions and i not in keep_from:
                blocks = []
                for block in message["content"]:
                    if block.get("type") != "tool_result":
                        blocks.append(block)
                        continue
                    text = _block_text(block)
                    first = text.strip().splitlines()[0] if text.strip() else "(no output)"
                    blocks.append({
                        **{k: v for k, v in block.items() if k != "content"},
                        "content": f"{first[:200]} … (older observation elided)",
                    })
                out.append({**message, "content": blocks})
            else:
                out.append(message)
        return out

    # -- the step --------------------------------------------------------

    async def step(self, state: RunState, session: BrowserSession) -> StepOutcome:
        started = time.time()

        budget = self.policy.check_budget(state)
        if budget:
            return StepOutcome(kind="failed", message=budget.reason)

        snapshot = await session.observe()

        # Seed the conversation on the first step; afterwards the previous tool
        # result already carries the current view.
        if not state.messages:
            state.messages.append({
                "role": "user",
                "content": f"Begin. Current screen:\n\n{snapshot.render()}",
            })

        response = await self.provider.complete(
            system=self._system(state),
            messages=self._compact(state.messages),
            tools=self.registry.schemas(),
            max_tokens=8_000,
            effort="medium",
        )

        state.messages.append({"role": "assistant", "content": response.raw_content})

        if response.refused:
            return StepOutcome(
                kind="failed",
                message=f"model declined to act: {response.text[:300]}",
            )

        if not response.tool_calls:
            # The model talked instead of acting. One nudge, then it counts as stalled.
            state.messages.append({
                "role": "user",
                "content": (
                    "You did not take an action. Respond with a tool call. If you are "
                    "blocked, call ask_human; if the goal is complete and verified, call done."
                ),
            })
            record = StepRecord(
                index=state.step_count, node="navigator", thought=response.thinking,
                tool="", ok=False, message="no action taken",
                observation_digest=snapshot.digest, url=snapshot.url,
                usage=response.usage, duration_ms=int((time.time() - started) * 1000),
            )
            state.record(record)
            kind: OutcomeKind = "stalled" if state.consecutive_failures >= 2 else "acted"
            return StepOutcome(kind=kind, record=record, message="model did not act")

        call = response.tool_calls[0]
        try:
            tool = self.registry.build(call.name, call.args)
        except (KeyError, TypeError, ValueError) as exc:
            return self._tool_error(state, call, snapshot, response, started, str(exc))

        decision = self.policy.assess(tool, snapshot, state)

        if decision.verdict == "deny":
            self._append_tool_result(state, call.id, f"BLOCKED BY POLICY: {decision.reason}", error=True)
            record = self._record(state, call, snapshot, response, started,
                                  ok=False, message=f"denied: {decision.reason}")
            return StepOutcome(kind="denied", record=record, message=decision.reason)

        if decision.verdict == "approve":
            # Stop *before* acting. The dangling tool_use is intentional: the
            # tool_result is supplied on resume, so the model never sees a gap.
            approval = ApprovalRequest(
                step_index=state.step_count, tool=call.name, args=call.args,
                reason=decision.reason, summary=decision.summary, risk=decision.risk,
            )
            approval_extra = {"tool_use_id": call.id}
            state.pending_approval = approval
            state.result.setdefault("_pending", {}).update(approval_extra)
            return StepOutcome(kind="needs_approval", approval=approval, message=decision.reason)

        return await self._execute(state, session, snapshot, call, tool, response, started)

    # -- execution -------------------------------------------------------

    async def _execute(self, state, session, snapshot, call, tool, response, started) -> StepOutcome:
        try:
            result: ToolResult = await tool.run(session, snapshot)
        except LookupError as exc:
            # A stale ref. Recoverable, and the model is told exactly why.
            self._append_tool_result(state, call.id, str(exc), error=True)
            record = self._record(state, call, snapshot, response, started,
                                  ok=False, message=str(exc))
            return StepOutcome(kind="acted", record=record, message=str(exc))
        except Exception as exc:
            self._append_tool_result(state, call.id, f"action failed: {exc}", error=True)
            record = self._record(state, call, snapshot, response, started,
                                  ok=False, message=f"{type(exc).__name__}: {exc}")
            return StepOutcome(kind="acted", record=record, message=str(exc))

        payload = result.observation or result.message or "(no output)"
        self._append_tool_result(state, call.id, payload, error=not result.ok)

        screenshot = await session.screenshot(call.name)
        record = self._record(state, call, snapshot, response, started,
                              ok=result.ok, message=result.message,
                              screenshot=str(screenshot))

        if result.data.get("step_done"):
            return StepOutcome(kind="step_done", record=record,
                               message=result.data.get("notes", "") or result.message)
        if result.needs_human:
            return StepOutcome(kind="needs_human", record=record,
                               question=result.data.get("question", result.message))
        if result.terminal:
            if result.ok:
                return StepOutcome(
                    kind="done", record=record, message=result.message,
                    result=result.data.get("result", {}) or {},
                    evidence=result.data.get("evidence", ""),
                )
            return StepOutcome(kind="failed", record=record, message=result.message)

        return StepOutcome(kind="acted", record=record, message=result.message)

    async def resume_after_approval(
        self, state: RunState, session: BrowserSession, approved: bool, note: str = ""
    ) -> StepOutcome:
        """Continue a run that stopped at the approval gate."""
        pending = state.pending_approval
        if pending is None:
            return StepOutcome(kind="failed", message="no approval was pending")

        tool_use_id = state.result.get("_pending", {}).get("tool_use_id", "")
        state.pending_approval = None
        started = time.time()
        snapshot = await session.observe()

        if not approved:
            message = f"A human rejected this action. Reason: {note or 'not given'}. " \
                      "Do not retry it; choose another approach or stop."
            self._append_tool_result(state, tool_use_id, message, error=True)
            record = StepRecord(
                index=state.step_count, node="approval", tool=pending.tool, args=pending.args,
                ok=False, message="rejected by human", observation_digest=snapshot.digest,
                url=snapshot.url, duration_ms=int((time.time() - started) * 1000),
            )
            state.record(record)
            return StepOutcome(kind="acted", record=record, message="rejected by human")

        tool = self.registry.build(pending.tool, pending.args)

        class _Call:
            id = tool_use_id
            name = pending.tool
            args = pending.args

        from pi_operator.llm.base import LLMResponse

        return await self._execute(
            state, session, snapshot, _Call(), tool, LLMResponse(), started
        )

    # -- bookkeeping -----------------------------------------------------

    @staticmethod
    def _append_tool_result(state: RunState, tool_use_id: str, content: str, error: bool = False) -> None:
        state.messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                **({"is_error": True} if error else {}),
            }],
        })

    @staticmethod
    def _describe_element(snapshot: Snapshot, args: dict[str, Any]) -> dict[str, str] | None:
        ref = args.get("ref")
        if not ref:
            return None
        element = snapshot.by_ref(ref)
        if element is None:
            return None
        return {"role": element.role, "name": element.name, "context": element.context}

    def _record(self, state, call, snapshot, response, started, *,
                ok, message, screenshot=None) -> StepRecord:
        record = StepRecord(
            index=state.step_count,
            node="navigator",
            thought=getattr(response, "thinking", ""),
            tool=call.name,
            args=call.args,
            element=self._describe_element(snapshot, call.args),
            ok=ok,
            message=message,
            observation_digest=snapshot.digest,
            url=snapshot.url,
            screenshot=screenshot,
            usage=getattr(response, "usage", None) or StepRecord(index=0, node="x").usage,
            duration_ms=int((time.time() - started) * 1000),
        )
        state.record(record)
        return record

    def _tool_error(self, state, call, snapshot, response, started, detail) -> StepOutcome:
        self._append_tool_result(state, call.id, f"invalid tool call: {detail}", error=True)
        record = self._record(state, call, snapshot, response, started,
                              ok=False, message=f"invalid tool call: {detail}")
        return StepOutcome(kind="acted", record=record, message=detail)


def _block_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)
