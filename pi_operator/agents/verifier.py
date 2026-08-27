"""Verifier sub-agent: independent confirmation that the world actually changed.

Two layers, in order of trust:

1. **Deterministic.** If the target can be queried out-of-band (a read API, a
   database), that answer is authoritative. It cannot be fooled by a page that
   merely looks like success.
2. **Model-driven read-back.** Where no such channel exists — which is the
   normal case for a legacy DMS — a separate agent navigates to where the record
   should be and reads it. It gets a read-only tool subset, so verification
   cannot itself mutate anything.

Running the second layer even when the first passes is deliberate: the API can
confirm a row exists while the operator still put the mileage in the price
field, and the read-back catches that class of error.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_operator.agents.prompts import VERIFIER_SYSTEM
from pi_operator.browser.session import BrowserSession
from pi_operator.browser.tools import (
    GoBack,
    Navigate,
    Observe,
    ReadTable,
    Scroll,
    SwitchTab,
    ToolRegistry,
)
from pi_operator.graph.state import RunState, VerificationResult
from pi_operator.llm.base import LLMProvider, Usage

# Read-only surface. Verification that can click "Save" is not verification.
READ_ONLY_TOOLS = [Navigate, GoBack, Observe, ReadTable, Scroll, SwitchTab]

VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Report whether the claimed outcome is actually present in the system.",
    "input_schema": {
        "type": "object",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "True only if you saw the expected record with the expected values.",
            },
            "detail": {
                "type": "string",
                "description": "What you observed. Name the screen and the values you read.",
            },
            "could_not_verify": {
                "type": "boolean",
                "description": "True if you could not reach a place where this could be confirmed.",
            },
        },
        "required": ["passed", "detail"],
    },
}


class VerifierOutput(BaseModel):
    result: VerificationResult
    usage: Usage = Field(default_factory=Usage)


class Verifier:
    def __init__(self, provider: LLMProvider, *, target, max_steps: int = 8) -> None:
        self.provider = provider
        self.target = target
        self.max_steps = max_steps
        self.registry = ToolRegistry(READ_ONLY_TOOLS)

    async def verify(
        self,
        state: RunState,
        session: BrowserSession,
        *,
        check: dict[str, Any] | None = None,
    ) -> VerifierOutput:
        usage = Usage()

        # Layer 1 — authoritative, when available.
        deterministic: VerificationResult | None = None
        if check:
            passed, detail = await self.target.verify(check)
            deterministic = VerificationResult(
                passed=passed, detail=detail, method="target-api"
            )
            if not passed:
                # An authoritative "no" ends it; no point reading the screen.
                return VerifierOutput(result=deterministic, usage=usage)

        # Layer 2 — independent read-back.
        readback = await self._read_back(state, session)
        usage = usage + readback.usage

        if deterministic is None:
            return VerifierOutput(result=readback.result, usage=usage)

        combined = VerificationResult(
            passed=deterministic.passed and readback.result.passed,
            method="target-api + read-back",
            detail=f"API: {deterministic.detail} | read-back: {readback.result.detail}",
        )
        return VerifierOutput(result=combined, usage=usage)

    async def _read_back(self, state: RunState, session: BrowserSession) -> VerifierOutput:
        snapshot = await session.observe()
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": (
                f"GOAL THAT WAS CLAIMED COMPLETE:\n{state.goal}\n\n"
                f"THE OPERATOR REPORTED:\n{state.evidence or '(no evidence given)'}\n\n"
                f"{self.target.describe()}\n\n"
                f"Confirm this independently. Current screen:\n\n{snapshot.render()}"
            ),
        }]
        usage = Usage()

        for _ in range(self.max_steps):
            response = await self.provider.complete(
                system=VERIFIER_SYSTEM,
                messages=messages,
                tools=[*self.registry.schemas(), VERDICT_TOOL],
                max_tokens=4_000,
                effort="medium",
            )
            usage = usage + response.usage
            messages.append({"role": "assistant", "content": response.raw_content})

            if not response.tool_calls:
                break

            call = response.tool_calls[0]
            if call.name == "submit_verdict":
                args = call.args
                if args.get("could_not_verify"):
                    return VerifierOutput(
                        result=VerificationResult(
                            passed=False, method="read-back",
                            detail=f"could not verify: {args.get('detail', '')}",
                        ),
                        usage=usage,
                    )
                return VerifierOutput(
                    result=VerificationResult(
                        passed=bool(args.get("passed")),
                        detail=str(args.get("detail", "")),
                        method="read-back",
                    ),
                    usage=usage,
                )

            try:
                tool = self.registry.build(call.name, call.args)
                current = await session.observe()
                result = await tool.run(session, current)
                payload = result.observation or result.message
            except Exception as exc:
                payload = f"error: {exc}"

            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": call.id, "content": payload,
                }],
            })

        return VerifierOutput(
            result=VerificationResult(
                passed=False, method="read-back",
                detail=f"verifier did not reach a verdict within {self.max_steps} steps",
            ),
            usage=usage,
        )
