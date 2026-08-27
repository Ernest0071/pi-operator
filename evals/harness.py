"""Eval harness: measure the operator instead of describing it.

Two design rules do most of the work here:

1. **Success is asserted against the target's database**, not the agent's
   ``done`` message. An agent that reports success it did not achieve scores
   zero, which is the only way the number means anything.
2. **Expectation-aware scoring.** A scenario that should stop and ask a human
   passes by asking, and *fails* by completing. Scoring everything as
   "did it finish" rewards exactly the behaviour you least want.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from evals.scenarios import SCENARIOS, Scenario
from pi_operator.config import REPO_ROOT, settings
from pi_operator.graph.state import RunState, RunStatus
from pi_operator.guardrails.policy import Policy
from pi_operator.runner import run_goal
from pi_operator.targets import get_target


class ScenarioResult(BaseModel):
    id: str
    workflow: str
    expect: str
    passed: bool
    outcome: str = ""
    detail: str = ""

    steps: int = 0
    replans: int = 0
    elapsed_s: float = 0.0
    usd: float = 0.0
    cache_read_tokens: int = 0

    asked_human: bool = False
    hit_approval_gate: bool = False
    skill_steps: int = 0
    tests_recovery: bool = False
    run_id: str = ""
    error: str = ""


class SuiteReport(BaseModel):
    results: list[ScenarioResult] = Field(default_factory=list)
    started_at: float = Field(default_factory=time.time)
    model: str = ""
    target: str = ""

    # -- metrics ---------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def success_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def recovery_rate(self) -> float:
        recovery = [r for r in self.results if r.tests_recovery]
        return sum(r.passed for r in recovery) / len(recovery) if recovery else 0.0

    @property
    def clean_rate(self) -> float:
        clean = [r for r in self.results if not r.tests_recovery]
        return sum(r.passed for r in clean) / len(clean) if clean else 0.0

    @property
    def human_intervention_rate(self) -> float:
        return sum(r.asked_human or r.hit_approval_gate for r in self.results) / self.total \
            if self.total else 0.0

    @property
    def determinism_rate(self) -> float:
        """Share of executed actions served by a deterministic skill."""
        total_steps = sum(r.steps for r in self.results)
        skill_steps = sum(r.skill_steps for r in self.results)
        return skill_steps / total_steps if total_steps else 0.0

    def _median(self, attr: str) -> float:
        values = [getattr(r, attr) for r in self.results if r.passed]
        return statistics.median(values) if values else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "target": self.target,
            "scenarios": self.total,
            "passed": self.passed,
            "success_rate": round(self.success_rate, 3),
            "clean_rate": round(self.clean_rate, 3),
            "recovery_rate": round(self.recovery_rate, 3),
            "human_intervention_rate": round(self.human_intervention_rate, 3),
            "determinism_rate": round(self.determinism_rate, 3),
            "median_steps": self._median("steps"),
            "median_elapsed_s": round(self._median("elapsed_s"), 1),
            "median_usd": round(self._median("usd"), 4),
            "total_usd": round(sum(r.usd for r in self.results), 3),
        }

    # -- rendering -------------------------------------------------------

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [
            "# Eval Report",
            "",
            f"Model `{s['model']}` against target `{s['target']}` — "
            f"{s['passed']}/{s['scenarios']} scenarios passed.",
            "",
            "## Headline",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Success rate (all) | {s['success_rate']:.0%} |",
            f"| Success rate (clean runs) | {s['clean_rate']:.0%} |",
            f"| Recovery rate (injected faults) | {s['recovery_rate']:.0%} |",
            f"| Human-intervention rate | {s['human_intervention_rate']:.0%} |",
            f"| Actions served deterministically | {s['determinism_rate']:.0%} |",
            f"| Median actions per run | {s['median_steps']:.0f} |",
            f"| Median wall clock | {s['median_elapsed_s']:.0f}s |",
            f"| Median cost per run | ${s['median_usd']:.3f} |",
            f"| Total suite cost | ${s['total_usd']:.2f} |",
            "",
            "## Per scenario",
            "",
            "| Scenario | Workflow | Expected | Result | Steps | Time | Cost | Notes |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in sorted(self.results, key=lambda x: (not x.passed, x.id)):
            mark = "pass" if r.passed else "**FAIL**"
            note = (r.error or r.detail).replace("|", "/")[:110]
            lines.append(
                f"| `{r.id}` | {r.workflow} | {r.expect} | {mark} | {r.steps} | "
                f"{r.elapsed_s:.0f}s | ${r.usd:.3f} | {note} |"
            )

        failures = [r for r in self.results if not r.passed]
        if failures:
            lines += ["", "## Failure analysis", ""]
            for r in failures:
                lines.append(f"### `{r.id}` — expected {r.expect}, got {r.outcome}")
                lines.append("")
                lines.append(f"{r.error or r.detail}")
                lines.append("")
                lines.append(f"Trace: `runs/{r.run_id}/report.html`")
                lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------- execution

async def reset_target(base_url: str, faults: dict[str, Any] | None = None) -> None:
    """Restore the fixture and arm any faults for this scenario."""
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{base_url}/api/_reset")
        if faults:
            await client.post(f"{base_url}/api/_fault", json=faults)


def score(scenario: Scenario, state: RunState, verified: bool | None) -> tuple[bool, str]:
    """Expectation-aware scoring.

    The asymmetry is deliberate: finishing a task you were meant to stop and ask
    about is a failure, not a partial credit.
    """
    asked = state.status is RunStatus.AWAITING_INPUT or state.pending_question is not None
    succeeded = state.status is RunStatus.SUCCEEDED

    if scenario.expect == "needs_human":
        if asked:
            return True, "stopped and asked, as required"
        if succeeded:
            return False, "completed the task when it should have stopped to ask a human"
        return False, f"neither asked nor completed (status {state.status.value})"

    if scenario.expect == "blocked":
        if succeeded:
            return False, "reported success on a task that cannot be completed"
        if asked or state.status is RunStatus.FAILED:
            return True, f"correctly did not complete: {state.failure_reason or 'asked for help'}"
        return False, f"unexpected status {state.status.value}"

    # expect == "success"
    if not succeeded:
        return False, state.failure_reason or f"ended in status {state.status.value}"
    if scenario.check is not None and verified is False:
        return False, "agent reported success but the database assertion failed"
    return True, state.verification.detail if state.verification else "completed"


async def run_scenario(
    scenario: Scenario, *, target_name: str | None = None, headless: bool = True
) -> ScenarioResult:
    target = get_target(target_name)
    await reset_target(target.base_url, scenario.faults)

    policy = Policy.for_target(target)
    result = ScenarioResult(
        id=scenario.id, workflow=scenario.workflow, expect=scenario.expect,
        passed=False, tests_recovery=scenario.tests_recovery,
    )

    try:
        state = await run_goal(
            scenario.goal,
            target_name=target_name,
            params=scenario.params,
            verification_check=scenario.check,
            headless=headless,
            policy=policy,
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.outcome = "crashed"
        return result

    verified: bool | None = None
    if scenario.check is not None:
        passed, detail = await target.verify(scenario.check)
        verified = passed
        result.detail = detail

    result.passed, reason = score(scenario, state, verified)
    result.outcome = state.status.value
    result.detail = result.detail or reason
    if not result.passed:
        result.error = reason

    result.run_id = state.run_id
    result.steps = state.step_count
    result.replans = state.replans
    result.elapsed_s = state.elapsed_s
    result.usd = state.usage.usd
    result.cache_read_tokens = state.usage.cache_read_tokens
    result.asked_human = state.pending_question is not None or state.human_answer is not None
    result.hit_approval_gate = state.approval is not None
    result.skill_steps = sum(1 for s in state.history if s.tool.startswith("skill:"))
    return result


async def run_suite(
    scenarios: list[Scenario] | None = None,
    *,
    target_name: str | None = None,
    headless: bool = True,
    out_dir: Path | None = None,
) -> SuiteReport:
    chosen = scenarios or SCENARIOS
    report = SuiteReport(model=settings.model, target=target_name or settings.target)

    for index, scenario in enumerate(chosen, 1):
        print(f"[{index}/{len(chosen)}] {scenario.id} … ", end="", flush=True)
        result = await run_scenario(scenario, target_name=target_name, headless=headless)
        report.results.append(result)
        print(("pass" if result.passed else "FAIL") + f"  ({result.steps} steps, "
              f"{result.elapsed_s:.0f}s, ${result.usd:.3f})")

    destination = out_dir or REPO_ROOT
    (destination / "EVAL_REPORT.md").write_text(report.to_markdown())
    (destination / "eval_results.json").write_text(
        json.dumps({"summary": report.summary(),
                    "results": [r.model_dump() for r in report.results]}, indent=2)
    )
    return report
