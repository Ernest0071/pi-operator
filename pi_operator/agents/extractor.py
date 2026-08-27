"""Extractor sub-agent: structured data out of a screen, against a caller schema.

Separated from the navigator because the failure modes differ. A navigator that
guesses wrong takes a wrong action and usually finds out. An extractor that
guesses wrong returns clean, well-typed, plausible, incorrect data — and nothing
downstream notices. Hence the hard rule about never completing a value, and the
explicit ``complete`` flag for multi-page data.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_operator.agents.prompts import EXTRACTOR_SYSTEM
from pi_operator.browser.session import BrowserSession
from pi_operator.browser.tools import Navigate, Observe, ReadTable, Scroll, ToolRegistry
from pi_operator.llm.base import LLMProvider, Usage

PAGING_TOOLS = [Observe, ReadTable, Scroll, Navigate]


class ExtractionResult(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    complete: bool = True
    note: str = ""
    usage: Usage = Field(default_factory=Usage)


def build_extract_tool(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """Wrap the caller's row schema in the submission tool."""
    return {
        "name": "submit_extraction",
        "description": f"Return the extracted data. {description}",
        "input_schema": {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": schema},
                "complete": {
                    "type": "boolean",
                    "description": "False if more data exists beyond what you read.",
                },
                "note": {"type": "string", "description": "Anything the caller should know."},
            },
            "required": ["rows", "complete"],
        },
    }


class Extractor:
    def __init__(self, provider: LLMProvider, *, max_steps: int = 10) -> None:
        self.provider = provider
        self.max_steps = max_steps
        self.registry = ToolRegistry(PAGING_TOOLS)

    async def extract(
        self,
        session: BrowserSession,
        *,
        instruction: str,
        row_schema: dict[str, Any],
        description: str = "",
    ) -> ExtractionResult:
        snapshot = await session.observe()
        submit_tool = build_extract_tool(row_schema, description)

        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": (
                f"EXTRACT: {instruction}\n\n"
                "Page through the data if it spans more than one screen. "
                "When you have everything you can see, call submit_extraction.\n\n"
                f"Current screen:\n\n{snapshot.render()}"
            ),
        }]
        usage = Usage()

        for _ in range(self.max_steps):
            response = await self.provider.complete(
                system=EXTRACTOR_SYSTEM,
                messages=messages,
                tools=[*self.registry.schemas(), submit_tool],
                max_tokens=8_000,
                effort="medium",
            )
            usage = usage + response.usage
            messages.append({"role": "assistant", "content": response.raw_content})

            if not response.tool_calls:
                break

            call = response.tool_calls[0]
            if call.name == "submit_extraction":
                return ExtractionResult(
                    rows=call.args.get("rows", []),
                    complete=bool(call.args.get("complete", True)),
                    note=str(call.args.get("note", "")),
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

        return ExtractionResult(
            rows=[], complete=False,
            note=f"extractor did not submit within {self.max_steps} steps",
            usage=usage,
        )
