"""Skills: deterministic, replayable routines for the parts of a workflow that
do not need a model.

Why this layer exists
---------------------
Most of a dealership workflow is not ambiguous. "Open the new-vehicle form, put
the VIN in the VIN field, save" does not require reasoning — it required
reasoning *once*. Paying a model to rediscover it on every run is slow, costly
and, worst of all, non-deterministic: the same goal can take a different path
each time, which makes failures unreproducible.

So the operator runs skills first and falls back to the model only for novelty
and recovery. The model's job is the part that is actually hard.

The stability problem
---------------------
Perception refs (``e12``) are valid for exactly one snapshot — they cannot be
recorded. A skill therefore stores an *element descriptor* (role + accessible
name + context) and re-resolves it against a fresh snapshot at replay time,
through a ladder of increasingly loose matches. That ladder is the self-healing
selector mechanism: when a target renames "Save" to "Save Vehicle", the skill
still resolves, and reports that it healed so the drift is visible in metrics
rather than silent.
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from pi_operator.browser.perception import Element, Snapshot
from pi_operator.browser.session import BrowserSession

MatchTier = Literal["exact", "role_name", "name", "normalized", "contains", "fuzzy", "none"]


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class ElementDescriptor(BaseModel):
    """A run-stable way to refer to an element."""

    role: str
    name: str = ""
    context: str = ""

    def describe(self) -> str:
        bits = [self.role]
        if self.name:
            bits.append(repr(self.name))
        if self.context:
            bits.append(f"in {self.context}")
        return " ".join(bits)

    @classmethod
    def from_element(cls, element: Element) -> ElementDescriptor:
        return cls(role=element.role, name=element.name, context=element.context)


class Resolution(BaseModel):
    ref: str | None = None
    tier: MatchTier = "none"
    detail: str = ""

    @property
    def healed(self) -> bool:
        """True when the match required loosening — i.e. the target drifted."""
        return self.ref is not None and self.tier not in ("exact", "role_name")


def resolve(snapshot: Snapshot, want: ElementDescriptor) -> Resolution:
    """Find the element a skill meant, tolerating drift in how it is labelled."""
    candidates = snapshot.elements

    exact = [
        e for e in candidates
        if e.role == want.role and e.name == want.name and e.context == want.context
    ]
    if len(exact) == 1:
        return Resolution(ref=exact[0].ref, tier="exact")
    if exact:
        return Resolution(ref=exact[0].ref, tier="exact", detail=f"{len(exact)} matched; took first")

    role_name = [e for e in candidates if e.role == want.role and e.name == want.name]
    if role_name:
        return Resolution(
            ref=role_name[0].ref,
            tier="role_name",
            detail="context differed" if want.context else "",
        )

    by_name = [e for e in candidates if e.name == want.name and want.name]
    if by_name:
        return Resolution(
            ref=by_name[0].ref, tier="name",
            detail=f"role changed {want.role!r} -> {by_name[0].role!r}",
        )

    target = _normalize(want.name)
    if target:
        normalized = [e for e in candidates if _normalize(e.name) == target]
        if normalized:
            return Resolution(
                ref=normalized[0].ref, tier="normalized",
                detail=f"label reformatted to {normalized[0].name!r}",
            )

        # Renames usually extend or trim a label ("Save" -> "Save Vehicle")
        # rather than rewrite it, and containment catches that far more reliably
        # than edit distance, which scores such pairs surprisingly low.
        pool = [e for e in candidates if e.role == want.role] or candidates
        contained = [
            e for e in pool
            if _normalize(e.name) and (
                target in _normalize(e.name) or _normalize(e.name) in target
            )
        ]
        if len(contained) == 1:
            return Resolution(
                ref=contained[0].ref, tier="contains",
                detail=f"label drifted {want.name!r} -> {contained[0].name!r}",
            )

        # Last resort: closest label among elements of a compatible role.
        names = [_normalize(e.name) for e in pool]
        close = difflib.get_close_matches(target, [n for n in names if n], n=1, cutoff=0.82)
        if close:
            hit = pool[names.index(close[0])]
            return Resolution(
                ref=hit.ref, tier="fuzzy",
                detail=f"closest label to {want.name!r} was {hit.name!r}",
            )

    return Resolution(tier="none", detail=f"no element matching {want.describe()}")


class SkillResult(BaseModel):
    ok: bool
    skill: str
    message: str = ""
    steps_run: int = 0
    healed: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    failed_at: int | None = None

    @property
    def drifted(self) -> bool:
        return bool(self.healed)


class Skill(BaseModel):
    """Base class for a deterministic routine."""

    skill_name: ClassVar[str]
    skill_description: ClassVar[str]
    target: ClassVar[str] = "*"
    version: ClassVar[int] = 1

    async def run(self, session: BrowserSession, params: dict[str, Any]) -> SkillResult:
        raise NotImplementedError

    @classmethod
    def applies_to(cls, target_name: str) -> bool:
        return cls.target in ("*", target_name)


class RecordedStep(BaseModel):
    """One action in a recorded skill.

    ``args`` may contain ``{{param}}`` placeholders, substituted at replay.
    """

    tool: str
    element: ElementDescriptor | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    expect: str = Field(
        default="",
        description="Optional text expected to appear after this step; a cheap inline assertion.",
    )

    def bind(self, params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in self.args.items():
            if isinstance(value, str):
                for name, replacement in params.items():
                    value = value.replace(f"{{{{{name}}}}}", str(replacement))
                missing = re.findall(r"\{\{(\w+)\}\}", value)
                if missing:
                    raise KeyError(f"skill step is missing parameter(s): {missing}")
            out[key] = value
        return out


class RecordedSkill(Skill):
    """A skill defined as data rather than code.

    Recorded skills are what promotion produces: a successful model-driven
    trajectory, generalised into parameterised steps and replayed
    deterministically thereafter.
    """

    skill_name: ClassVar[str] = "recorded"
    skill_description: ClassVar[str] = "Replayable recorded trajectory."

    name: str
    description: str = ""
    target_name: str = "*"
    params: list[str] = Field(default_factory=list)
    steps: list[RecordedStep] = Field(default_factory=list)
    skill_version: int = 1
    promoted_at: float | None = None
    source_run: str = ""

    async def run(self, session: BrowserSession, params: dict[str, Any]) -> SkillResult:
        from pi_operator.browser.tools import ToolRegistry

        registry = ToolRegistry()
        started = time.time()
        healed: list[str] = []

        missing = [p for p in self.params if p not in params]
        if missing:
            return SkillResult(
                ok=False, skill=self.name,
                message=f"missing required parameter(s): {missing}",
            )

        for index, step in enumerate(self.steps):
            snapshot = await session.observe()
            args = step.bind(params)

            if step.element is not None:
                resolution = resolve(snapshot, step.element)
                if resolution.ref is None:
                    return SkillResult(
                        ok=False, skill=self.name, steps_run=index, failed_at=index,
                        healed=healed,
                        message=(
                            f"step {index} ({step.tool}) could not locate "
                            f"{step.element.describe()}: {resolution.detail}"
                        ),
                        duration_ms=int((time.time() - started) * 1000),
                    )
                if resolution.healed:
                    healed.append(f"step {index}: {resolution.tier} — {resolution.detail}")
                args["ref"] = resolution.ref

            tool = registry.build(step.tool, args)
            result = await tool.run(session, snapshot)
            if not result.ok:
                return SkillResult(
                    ok=False, skill=self.name, steps_run=index, failed_at=index, healed=healed,
                    message=f"step {index} ({step.tool}) failed: {result.message}",
                    duration_ms=int((time.time() - started) * 1000),
                )

            if step.expect:
                after = await session.observe()
                haystack = after.render().lower()
                if step.expect.lower() not in haystack:
                    return SkillResult(
                        ok=False, skill=self.name, steps_run=index + 1, failed_at=index,
                        healed=healed,
                        message=f"step {index} assertion failed: expected {step.expect!r} on page",
                        duration_ms=int((time.time() - started) * 1000),
                    )

        return SkillResult(
            ok=True, skill=self.name, steps_run=len(self.steps), healed=healed,
            message=f"completed {len(self.steps)} steps",
            duration_ms=int((time.time() - started) * 1000),
        )
