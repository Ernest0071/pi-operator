"""Planner sub-agent: goal -> ordered, observable plan."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_operator.agents.prompts import PLANNER_SYSTEM
from pi_operator.config import settings
from pi_operator.graph.state import PlanStep
from pi_operator.llm.base import LLMProvider, Usage

PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the ordered plan for this goal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "Ordered steps, each independently observable.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "skill": {
                            "type": ["string", "null"],
                            "description": (
                                "Name of a listed skill that performs this step, if any."
                            ),
                        },
                    },
                    "required": ["description"],
                },
            },
            "clarification_needed": {
                "type": ["string", "null"],
                "description": "Set only if the goal is ambiguous in a way that changes the work.",
            },
        },
        "required": ["steps"],
    },
}


class PlanResult(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)
    clarification_needed: str | None = None
    usage: Usage = Field(default_factory=Usage)


class Planner:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def plan(
        self, *, goal: str, target_description: str, skills_catalogue: str,
        prior_failure: str = "",
    ) -> PlanResult:
        context: list[str] = [f"GOAL: {goal}", "", target_description]
        if skills_catalogue:
            context += ["", "SKILLS AVAILABLE:", skills_catalogue]
        if prior_failure:
            context += [
                "",
                "A previous attempt failed. Plan a different approach; do not repeat it.",
                f"FAILURE: {prior_failure}",
            ]

        response = await self.provider.complete(
            system=PLANNER_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(context)}],
            tools=[PLAN_TOOL],
            force_tool="submit_plan",
            max_tokens=4_000,
            effort="medium",
        )

        if not response.tool_calls:
            return PlanResult(
                clarification_needed="planner returned no plan",
                usage=response.usage,
            )

        args: dict[str, Any] = response.tool_calls[0].args
        steps = [
            PlanStep(id=i, description=s.get("description", ""), skill=s.get("skill") or None)
            for i, s in enumerate(args.get("steps", []))
            if s.get("description")
        ]
        return PlanResult(
            steps=steps,
            clarification_needed=args.get("clarification_needed") or None,
            usage=response.usage,
        )


def default_planner() -> Planner:
    from pi_operator.llm.anthropic_provider import get_provider

    return Planner(get_provider(settings.planner_model))
