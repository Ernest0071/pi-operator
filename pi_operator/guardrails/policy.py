"""Guardrails: what an autonomous operator is allowed to do without asking.

The design rule here is that **risk is derived, not enumerated**. Three inputs
decide every action:

1. what the tool declares about itself (``mutates_state``, ``reversible``, ``risk``)
2. what the *element being acted on* says it does — a button labelled "Void
   Invoice" is high-risk regardless of which tool clicks it
3. hard run budgets (steps, wall clock, spend)

That ordering matters. A policy that hardcodes "clicking Submit needs approval"
breaks the moment someone adds a tool; a policy driven by declarations plus
element semantics degrades gracefully instead.

The default posture is: reading is free, writing is allowed, and anything
irreversible or money-moving stops for a human.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from pi_operator.browser.perception import Snapshot
from pi_operator.browser.tools import Tool
from pi_operator.config import settings
from pi_operator.graph.state import RunState

Verdict = Literal["allow", "approve", "deny"]

# Words that mean "this cannot be undone from the UI".
DESTRUCTIVE = re.compile(
    r"\b(delete|remove|void|cancel|deactivate|archive|purge|discard|revoke|wipe)\b", re.I
)
# Words that mean "this commits the business to something".
COMMITTING = re.compile(
    r"\b(submit|confirm|approve|finali[sz]e|post|pay|charge|issue|place\s+order|"
    r"send\s+to|check\s*out|sign)\b",
    re.I,
)

MONEY = re.compile(r"(?:^|[^\d])(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d{2})?(?:$|[^\d])")


class Decision(BaseModel):
    verdict: Verdict
    reason: str = ""
    risk: str = "low"
    summary: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict != "allow"


class Policy(BaseModel):
    allowed_hosts: set[str] = Field(default_factory=set)
    max_steps: int = Field(default_factory=lambda: settings.max_steps)
    max_wall_seconds: int = Field(default_factory=lambda: settings.max_wall_seconds)
    max_usd: float = Field(default_factory=lambda: settings.max_usd)

    # Money above this in an action's arguments escalates to human approval.
    approval_amount_threshold: float = 5_000.0

    # Set false for unattended eval runs, where approvals are auto-answered.
    require_approval: bool = True

    @classmethod
    def for_target(cls, adapter, **overrides) -> Policy:
        return cls(allowed_hosts=adapter.allowed_hosts(), **overrides)

    # -- budgets ---------------------------------------------------------

    def check_budget(self, state: RunState) -> Decision | None:
        """Hard stops. These abort the run rather than asking for approval."""
        if state.step_count >= self.max_steps:
            return Decision(
                verdict="deny",
                reason=f"step budget exhausted ({self.max_steps} actions)",
                risk="high",
            )
        if state.elapsed_s >= self.max_wall_seconds:
            return Decision(
                verdict="deny",
                reason=f"wall-clock budget exhausted ({self.max_wall_seconds}s)",
                risk="high",
            )
        if state.usage.usd >= self.max_usd:
            return Decision(
                verdict="deny",
                reason=f"spend budget exhausted (${self.max_usd:.2f})",
                risk="high",
            )
        return None

    # -- per-action ------------------------------------------------------

    def assess(self, tool: Tool, snapshot: Snapshot | None, state: RunState) -> Decision:
        budget = self.check_budget(state)
        if budget:
            return budget

        # 1. Navigation may not leave the target application.
        target_url = getattr(tool, "url", None)
        if target_url and self.allowed_hosts:
            from urllib.parse import urlparse

            host = urlparse(target_url).netloc
            if host and host not in self.allowed_hosts:
                return Decision(
                    verdict="deny",
                    reason=f"navigation to {host!r} is outside the target application",
                    risk="high",
                )

        label = self._label_for(tool, snapshot)
        summary = f"{tool.tool_name}({label})" if label else tool.tool_name

        # 2. The tool declares itself dangerous.
        if not tool.reversible:
            return Decision(
                verdict=self._gate(),
                reason=f"{tool.tool_name} is declared irreversible",
                risk="high",
                summary=summary,
            )
        if tool.risk == "high":
            return Decision(
                verdict=self._gate(),
                reason=f"{tool.tool_name} is declared high-risk",
                risk="high",
                summary=summary,
            )

        # 3. The element being acted on declares itself dangerous.
        if label and tool.mutates_state:
            if DESTRUCTIVE.search(label):
                return Decision(
                    verdict=self._gate(),
                    reason=f"target element {label!r} appears destructive",
                    risk="high",
                    summary=summary,
                )
            if COMMITTING.search(label):
                return Decision(
                    verdict=self._gate(),
                    reason=f"target element {label!r} commits a business action",
                    risk="medium",
                    summary=summary,
                )

        # 4. The action carries a material amount of money.
        amount = self._largest_amount(tool)
        if amount is not None and amount >= self.approval_amount_threshold:
            return Decision(
                verdict=self._gate(),
                reason=f"action involves {amount:,.2f}, at or above the "
                f"{self.approval_amount_threshold:,.0f} approval threshold",
                risk="high",
                summary=summary,
            )

        return Decision(verdict="allow", risk=tool.risk, summary=summary)

    # -- helpers ---------------------------------------------------------

    def _gate(self) -> Verdict:
        return "approve" if self.require_approval else "allow"

    @staticmethod
    def _label_for(tool: Tool, snapshot: Snapshot | None) -> str:
        """Human-meaningful description of what the action touches."""
        ref = getattr(tool, "ref", None)
        if ref and snapshot:
            element = snapshot.by_ref(ref)
            if element:
                return element.name or element.value or element.role
        return getattr(tool, "option", "") or getattr(tool, "text", "")[:60]

    @staticmethod
    def _largest_amount(tool: Tool) -> float | None:
        """Largest currency-shaped number in the action's arguments.

        Deliberately crude and deliberately conservative: it exists to catch
        "type 45000 into the price field", not to parse accounting.
        """
        best: float | None = None
        for value in tool.model_dump().values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                candidate = float(value)
            elif isinstance(value, str):
                match = MONEY.search(value)
                if not match:
                    continue
                candidate = float(match.group(1).replace(",", ""))
            else:
                continue
            if best is None or candidate > best:
                best = candidate
        return best
