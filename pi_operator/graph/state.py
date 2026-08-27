"""Run state: the single typed object that flows through the graph.

Everything needed to resume a run lives here and nowhere else. That is what
makes the approval gate real — a run can be checkpointed mid-workflow, sit
overnight waiting for a human, and pick up exactly where it stopped.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from pi_operator.llm.base import Usage


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABORTED}


class PlanStep(BaseModel):
    id: int
    description: str
    skill: str | None = Field(default=None, description="Skill to try first, if one applies.")
    done: bool = False
    notes: str = ""


class StepRecord(BaseModel):
    """One observe-act-observe cycle. The audit trail is a list of these."""

    index: int
    node: str
    thought: str = ""
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    # Descriptor of the element acted on, captured at action time. Refs are only
    # valid for one snapshot, so this is what makes a trajectory replayable.
    element: dict[str, str] | None = None
    ok: bool = True
    message: str = ""
    observation_digest: str = ""
    url: str = ""
    screenshot: str | None = None
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = 0
    ts: float = Field(default_factory=time.time)


class ApprovalRequest(BaseModel):
    step_index: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str
    summary: str
    risk: str = "medium"
    requested_at: float = Field(default_factory=time.time)


class ApprovalDecision(BaseModel):
    approved: bool
    decided_by: str = "human"
    note: str = ""
    decided_at: float = Field(default_factory=time.time)


class VerificationResult(BaseModel):
    passed: bool
    detail: str = ""
    method: str = ""


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str
    target: str = ""
    status: RunStatus = RunStatus.PENDING

    plan: list[PlanStep] = Field(default_factory=list)
    cursor: int = 0

    history: list[StepRecord] = Field(default_factory=list)
    # Conversation blocks replayed to the model. Thinking blocks must survive
    # round-tripping unchanged, so this holds provider content, not strings.
    messages: list[dict[str, Any]] = Field(default_factory=list)

    digest_counts: dict[str, int] = Field(default_factory=dict)
    consecutive_failures: int = 0
    replans: int = 0

    pending_approval: ApprovalRequest | None = None
    approval: ApprovalDecision | None = None
    pending_question: str | None = None
    human_answer: str | None = None

    verification: VerificationResult | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: str = ""
    failure_reason: str = ""

    usage: Usage = Field(default_factory=Usage)
    started_at: float = Field(default_factory=time.time)
    artifacts: list[str] = Field(default_factory=list)

    # Transient routing signal from the last node. Persisted only so a resumed
    # run can report where it stopped.
    last_outcome: str = ""
    last_message: str = ""

    # -- derived ---------------------------------------------------------

    @property
    def step_count(self) -> int:
        return len(self.history)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    @property
    def current_step(self) -> PlanStep | None:
        if 0 <= self.cursor < len(self.plan):
            return self.plan[self.cursor]
        return None

    @property
    def plan_complete(self) -> bool:
        return bool(self.plan) and all(s.done for s in self.plan)

    def record(self, step: StepRecord) -> None:
        self.history.append(step)
        self.usage = self.usage + step.usage
        if step.observation_digest:
            self.digest_counts[step.observation_digest] = (
                self.digest_counts.get(step.observation_digest, 0) + 1
            )
        self.consecutive_failures = 0 if step.ok else self.consecutive_failures + 1

    def oscillating(self, threshold: int = 3) -> bool:
        """True when the page has returned to the same state too many times.

        Distinct from a retry: retrying a flaky click is fine, but arriving at
        an identical observation three times means the current plan cannot make
        progress and needs replanning, not another attempt.
        """
        return any(count >= threshold for count in self.digest_counts.values())

    def summarize(self) -> str:
        done = sum(1 for s in self.plan if s.done)
        return (
            f"run {self.run_id} [{self.status.value}] {done}/{len(self.plan)} steps planned, "
            f"{self.step_count} actions, {self.elapsed_s:.0f}s, ${self.usage.usd:.3f}"
        )
