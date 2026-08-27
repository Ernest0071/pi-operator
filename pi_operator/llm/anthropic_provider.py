"""Anthropic implementation of the provider interface.

Notes that matter for cost and correctness on current models:

* ``budget_tokens`` is gone. Thinking depth is controlled by
  ``thinking={"type": "adaptive"}`` plus ``output_config.effort``.
* Thinking blocks must be replayed unchanged on subsequent turns of the same
  conversation, so ``raw_content`` carries the provider blocks back to the loop.
* Tools and the system prompt are stable across every step of a run, and they
  render before messages, so a cache breakpoint on the system block makes the
  whole tool schema + instruction prefix a cache read after step one. On a
  60-step run that is the difference between paying for the prefix once and
  paying for it sixty times.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import anthropic

from pi_operator.config import settings
from pi_operator.llm.base import Effort, LLMProvider, LLMResponse, ToolCall, Usage

# USD per 1M tokens. Cache reads bill at ~0.1x input, cache writes at ~1.25x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
}


def price(model: str, usage: Any) -> float:
    inp, out = PRICES.get(model, PRICES["claude-sonnet-5"])
    fresh = getattr(usage, "input_tokens", 0) or 0
    cached_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cached_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    return (
        fresh * inp
        + cached_read * inp * 0.1
        + cached_write * inp * 1.25
        + output * out
    ) / 1_000_000


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.model
        key = api_key or settings.anthropic_api_key
        # A bare client also resolves an `ant auth login` profile, so an unset
        # key is not necessarily an error.
        self.client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()

    def preflight(self) -> None:
        """Fail fast, and legibly, when no credential can be resolved.

        The SDK raises a TypeError deep in header construction at request time,
        which surfaces as an unreadable traceback several seconds into a run —
        after a browser has already started. Check up front instead.
        """
        import os

        if settings.anthropic_api_key or os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return
        # An `ant auth login` profile also counts, and the SDK finds it itself.
        profile_dir = pathlib.Path.home() / ".config" / "anthropic"
        if profile_dir.exists() and any(profile_dir.iterdir()):
            return
        raise RuntimeError(
            "No Anthropic credential found. Set ANTHROPIC_API_KEY in .env "
            "(copy .env.example), export it, or run `ant auth login`."
        )

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
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort},
        }
        if tools:
            kwargs["tools"] = tools
            if force_tool:
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
            elif not allow_parallel_tools:
                # Browser actions are order-dependent and each one invalidates the
                # refs the next would use, so the operator acts one step at a time.
                kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

        try:
            response = await self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            raise RuntimeError(f"Anthropic rejected the request: {exc.message}") from exc
        except TypeError as exc:
            if "authentication method" in str(exc):
                raise RuntimeError(
                    "No Anthropic credential found. Set ANTHROPIC_API_KEY in .env "
                    "(copy .env.example), export it, or run `ant auth login`."
                ) from exc
            raise
        except anthropic.AuthenticationError as exc:
            raise RuntimeError(
                "Anthropic authentication failed. Set ANTHROPIC_API_KEY in .env, "
                "or run `ant auth login`."
            ) from exc

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(getattr(block, "thinking", "") or "")
            elif block.type == "tool_use":
                # Inputs arrive as parsed objects; never string-match the raw form.
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                calls.append(ToolCall(id=block.id, name=block.name, args=args))

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            usd=price(self.model, response.usage),
        )

        return LLMResponse(
            text="\n".join(text_parts).strip(),
            thinking="\n".join(p for p in thinking_parts if p).strip(),
            tool_calls=calls,
            stop_reason=response.stop_reason or "",
            model=response.model,
            usage=usage,
            # Serialised so run state stays checkpointable. Thinking blocks carry
            # a signature and must be replayed byte-identical, so dump, not rebuild.
            raw_content=[block.model_dump() for block in response.content],
        )


def get_provider(model: str | None = None) -> LLMProvider:
    """Single construction point, so adding a provider touches one function."""
    return AnthropicProvider(model=model)
