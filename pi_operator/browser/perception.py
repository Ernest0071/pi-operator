"""Perception: turn a live page into a compact, indexed, model-legible observation.

Design notes
------------
Feeding raw HTML to a model is both expensive and ineffective: a mid-sized ERP
page is 200k+ characters of markup, most of it layout noise. Instead we run a
distiller in the page (``_distill.js``) that emits only what an operator could
perceive and act on, and we tag each returned element with ``data-pi-ref`` so
the follow-up action resolves to exactly the node that was perceived.

Two properties matter for reliability:

* **Ref stability within a step.** ``data-pi-ref`` is written during the
  snapshot and read by the very next action, so a re-render between perceive and
  act is detected (the ref vanishes) rather than silently mis-clicked.
* **Diffs, not re-reads.** After an action we show the model what *changed*.
  That keeps step cost roughly flat instead of growing with page size.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_DISTILL_JS = (Path(__file__).parent / "_distill.js").read_text(encoding="utf-8")


class Element(BaseModel):
    ref: str
    role: str
    name: str = ""
    value: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    options: list[str] | None = None
    context: str = ""
    bbox: list[int] = Field(default_factory=list)
    in_viewport: bool = Field(default=True, alias="inViewport")

    model_config = {"populate_by_name": True}

    @property
    def identity(self) -> str:
        """Content-addressed identity, independent of snapshot ordering.

        Used for diffing: an element keeps its identity across re-renders as long
        as its role, name and context are unchanged, even if its ref shifts.
        """
        return f"{self.role}|{self.name}|{self.context}"

    def render(self) -> str:
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f"value={self.value!r}")
        flags = [k if v is True else f"{k}={v}"
                 for k, v in self.state.items() if v not in (False, None)]
        if flags:
            bits.append(" ".join(flags))
        if self.options:
            shown = self.options[:8]
            more = "" if len(self.options) <= 8 else f" +{len(self.options) - 8} more"
            bits.append(f"options={shown}{more}")
        if self.context:
            bits.append(f"in:{self.context}")
        if not self.in_viewport:
            bits.append("(off-screen)")
        return " ".join(bits)


class Table(BaseModel):
    ref: str
    caption: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    truncated: bool = False

    def render(self, max_rows: int = 10) -> str:
        head = f"table [{self.ref}]" + (f" {self.caption!r}" if self.caption else "")
        lines = [head]
        if self.headers:
            lines.append("  | " + " | ".join(self.headers) + " |")
        for row in self.rows[:max_rows]:
            lines.append("  | " + " | ".join(row) + " |")
        omitted = len(self.rows) - max_rows
        if omitted > 0 or self.truncated:
            lines.append(f"  ... ({max(omitted, 0)}+ more rows not shown)")
        return "\n".join(lines)


class Snapshot(BaseModel):
    url: str
    title: str = ""
    elements: list[Element] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    text: list[str] = Field(default_factory=list)
    truncated: bool = False
    scroll: dict[str, int] = Field(default_factory=dict)

    def by_ref(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    @property
    def digest(self) -> str:
        """Stable hash of perceivable state. Drives the oscillation detector."""
        payload = json.dumps(
            {
                "url": self.url,
                "els": sorted(e.identity + "=" + e.value for e in self.elements),
                "alerts": sorted(self.alerts),
                "text": sorted(self.text),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def render(self, max_elements: int = 120, max_text: int = 80) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.alerts:
            lines.append("ALERTS: " + " | ".join(self.alerts))
        scrolled = self.scroll.get("height", 0) > self.scroll.get("viewport", 0)
        if scrolled:
            lines.append(
                f"SCROLL: y={self.scroll.get('y', 0)} of {self.scroll.get('height', 0)}px "
                f"(viewport {self.scroll.get('viewport', 0)}px)"
            )
        lines.append("")
        lines.append("ELEMENTS:")
        for el in self.elements[:max_elements]:
            lines.append("  " + el.render())
        if len(self.elements) > max_elements or self.truncated:
            lines.append(f"  ... ({len(self.elements) - max_elements}+ more elements not shown)")
        for table in self.tables:
            lines.append("")
            lines.append(table.render())
        if self.text:
            lines.append("")
            lines.append("PAGE TEXT:")
            for chunk in self.text[:max_text]:
                lines.append(f"  {chunk}")
            if len(self.text) > max_text:
                lines.append(f"  ... ({len(self.text) - max_text} more text blocks)")
        return "\n".join(lines)


def diff(before: Snapshot, after: Snapshot) -> str:
    """Human/model-readable delta between two snapshots.

    The agent loop sends this instead of a full re-render whenever the page did
    not navigate, which is the common case for AJAX-heavy enterprise UIs.
    """
    if before.url != after.url:
        return f"NAVIGATED: {before.url} -> {after.url}\n\n{after.render()}"

    old = {e.identity: e for e in before.elements}
    new = {e.identity: e for e in after.elements}

    added = [new[k] for k in new.keys() - old.keys()]
    removed = [old[k] for k in old.keys() - new.keys()]
    changed = [
        (old[k], new[k])
        for k in old.keys() & new.keys()
        if old[k].value != new[k].value or old[k].state != new[k].state
    ]
    new_alerts = [a for a in after.alerts if a not in before.alerts]

    if not (added or removed or changed or new_alerts):
        return "NO VISIBLE CHANGE. The action did not alter the page."

    lines: list[str] = []
    if new_alerts:
        lines.append("NEW ALERTS: " + " | ".join(new_alerts))
    if changed:
        lines.append("CHANGED:")
        for o, n in changed[:25]:
            lines.append(f"  {n.render()}   (was value={o.value!r} state={o.state})")
    if added:
        lines.append("APPEARED:")
        for e in added[:40]:
            lines.append("  " + e.render())
    if removed:
        lines.append("DISAPPEARED:")
        for e in removed[:20]:
            lines.append(f"  {e.role} {e.name!r}")
    # Tables are re-rendered wholesale; row-level diffing is not worth the tokens.
    after_tables = [t.model_dump() for t in after.tables]
    before_tables = [t.model_dump() for t in before.tables]
    if after.tables and after_tables != before_tables:
        lines.append("TABLES UPDATED:")
        for t in after.tables:
            lines.append(t.render())
    return "\n".join(lines)


async def distill(page) -> Snapshot:
    """Snapshot the page's current perceivable state and tag it for action."""
    raw = await page.evaluate(_DISTILL_JS)
    return Snapshot.model_validate(raw)
