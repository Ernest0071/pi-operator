"""Provider-neutral LLM interface.

The agent loop talks to this, never to a vendor SDK directly, so swapping or
benchmarking models is a config change rather than a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            usd=self.usd + other.usd,
        )


class LLMResponse(BaseModel):
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = ""
    model: str = ""
    usage: Usage = Field(default_factory=Usage)

    # Raw provider content blocks. Thinking blocks must be replayed to the model
    # unchanged on the next turn, so the loop appends this rather than `text`.
    raw_content: Any = None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class LLMProvider(ABC):
    """Minimal surface the agent loop depends on."""

    name: str

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8_000,
        effort: Effort = "medium",
        force_tool: str | None = None,
        cache_system: bool = True,
        allow_parallel_tools: bool = False,
    ) -> LLMResponse:
        """One turn. Implementations must not retry silently past their budget."""
        raise NotImplementedError
