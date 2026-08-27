"""Skill registry, trajectory compilation, and promotion.

Promotion is the part worth explaining. When the model-driven navigator
completes a workflow that had no skill, that trajectory is a proof that a
deterministic path exists. Compiling it into a recorded skill means the next run
of the same workflow costs no tokens, takes no reasoning, and follows the same
path every time.

Promotion is gated, not automatic. A trajectory becomes a skill only if it
replays cleanly against a fresh fixture — twice. A path that worked once may
have depended on incidental state; a path that replays twice from a clean start
is a routine. Anything that fails validation stays a candidate and the model
keeps handling that workflow.

The effect over time is that the system becomes *more* deterministic the more it
runs, which is the opposite of how prompt-only agents age.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pi_operator.browser.session import BrowserSession
from pi_operator.config import REPO_ROOT
from pi_operator.graph.state import StepRecord
from pi_operator.skills.base import ElementDescriptor, RecordedSkill, RecordedStep, Skill

LIBRARY_DIR = REPO_ROOT / "pi_operator" / "skills" / "library"

# Tools that are perception or control flow, not part of a replayable routine.
NON_REPLAYABLE = {"observe", "done", "fail", "ask_human", "screenshot", "handle_dialog"}


class SkillRegistry:
    """Holds both code-defined and recorded skills."""

    def __init__(self, library_dir: Path | None = None) -> None:
        self.library_dir = library_dir or LIBRARY_DIR
        self._coded: dict[str, type[Skill]] = {}
        self._recorded: dict[str, RecordedSkill] = {}
        self.load()

    # -- loading ---------------------------------------------------------

    def load(self) -> None:
        self._recorded.clear()
        if not self.library_dir.exists():
            return
        for path in sorted(self.library_dir.glob("*.json")):
            try:
                skill = RecordedSkill.model_validate_json(path.read_text())
            except Exception as exc:  # a malformed skill must not break a run
                print(f"[skills] skipping {path.name}: {exc}")
                continue
            self._recorded[skill.name] = skill

    def register(self, skill_cls: type[Skill]) -> None:
        self._coded[skill_cls.skill_name] = skill_cls

    # -- lookup ----------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        if name in self._recorded:
            return self._recorded[name]
        if name in self._coded:
            return self._coded[name]()
        return None

    def for_target(self, target_name: str) -> list[Skill]:
        out: list[Skill] = [
            s for s in self._recorded.values()
            if s.target_name in ("*", target_name)
        ]
        out.extend(cls() for cls in self._coded.values() if cls.applies_to(target_name))
        return out

    def catalogue(self, target_name: str) -> str:
        """Rendered for the planner, so it can prefer a skill over exploration."""
        skills = self.for_target(target_name)
        if not skills:
            return "(no skills available for this target yet)"
        lines = []
        for skill in skills:
            if isinstance(skill, RecordedSkill):
                params = ", ".join(skill.params) or "none"
                lines.append(f"  - {skill.name}({params}): {skill.description}")
            else:
                lines.append(f"  - {skill.skill_name}: {skill.skill_description}")
        return "\n".join(lines)

    # -- persistence -----------------------------------------------------

    def save(self, skill: RecordedSkill) -> Path:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        path = self.library_dir / f"{skill.name}.json"
        path.write_text(json.dumps(skill.model_dump(), indent=2))
        self._recorded[skill.name] = skill
        return path


class TrajectoryCompiler:
    """Turns a successful run's history into a parameterised recorded skill."""

    @staticmethod
    def compile(
        *,
        name: str,
        description: str,
        target_name: str,
        history: list[StepRecord],
        params: dict[str, Any],
        source_run: str = "",
    ) -> RecordedSkill:
        steps: list[RecordedStep] = []

        for record in history:
            if not record.ok or record.tool in NON_REPLAYABLE or not record.tool:
                continue

            args = {k: v for k, v in record.args.items() if k != "ref"}
            steps.append(
                RecordedStep(
                    tool=record.tool,
                    element=ElementDescriptor(**record.element) if record.element else None,
                    args=TrajectoryCompiler._generalize(args, params),
                )
            )

        return RecordedSkill(
            name=name,
            description=description,
            target_name=target_name,
            params=sorted(params),
            steps=steps,
            promoted_at=time.time(),
            source_run=source_run,
        )

    @staticmethod
    def _generalize(args: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        """Replace run-specific literals with placeholders.

        Longest values first, so a parameter whose value contains another
        parameter's value does not get partially substituted.
        """
        ordered = sorted(params.items(), key=lambda kv: len(str(kv[1])), reverse=True)
        out: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str):
                for param_name, param_value in ordered:
                    literal = str(param_value)
                    if literal and literal in value:
                        value = value.replace(literal, f"{{{{{param_name}}}}}")
            out[key] = value
        return out


class SkillPromoter:
    """Validates a candidate skill before it is allowed into the library."""

    def __init__(self, registry: SkillRegistry, replays: int = 2) -> None:
        self.registry = registry
        self.replays = replays

    async def validate(
        self,
        candidate: RecordedSkill,
        params: dict[str, Any],
        session_factory,
        reset_fixture=None,
    ) -> tuple[bool, str]:
        """Replay the candidate from a clean start ``replays`` times.

        ``session_factory`` returns a fresh authenticated BrowserSession;
        ``reset_fixture`` restores the target to a known state between replays.
        Both are injected so this works against any target.
        """
        for attempt in range(self.replays):
            if reset_fixture is not None:
                await reset_fixture()
            session: BrowserSession = await session_factory()
            try:
                result = await candidate.run(session, params)
            finally:
                await session.close()

            if not result.ok:
                return False, f"replay {attempt + 1}/{self.replays} failed: {result.message}"
            if result.drifted:
                return False, (
                    f"replay {attempt + 1}/{self.replays} needed selector healing "
                    f"({result.healed}); too unstable to promote"
                )
        return True, f"validated over {self.replays} clean replays"

    async def promote(
        self,
        candidate: RecordedSkill,
        params: dict[str, Any],
        session_factory,
        reset_fixture=None,
    ) -> tuple[bool, str]:
        ok, detail = await self.validate(candidate, params, session_factory, reset_fixture)
        if not ok:
            return False, detail
        path = self.registry.save(candidate)
        return True, f"promoted to {path.name}: {detail}"


class PromotionCandidate(BaseModel):
    """Recorded when a run succeeds without a skill — the promotion queue."""

    run_id: str
    goal: str
    target_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    step_count: int = 0
    created_at: float = Field(default_factory=time.time)
