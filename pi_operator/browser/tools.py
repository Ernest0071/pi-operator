"""Tool registry: the complete action surface available to an operator.

Each tool is a Pydantic model, so its JSON schema is generated rather than
hand-written and stays in sync with the code that executes it.

Every tool carries risk metadata (``mutates_state``, ``reversible``, ``risk``).
The guardrail layer reads that metadata to decide what needs human approval, so
adding a new dangerous tool cannot silently bypass the policy — the policy is
driven by declarations, not by a list maintained somewhere else.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from pi_operator.browser.perception import Snapshot, diff
from pi_operator.browser.session import BrowserSession

Risk = Literal["low", "medium", "high"]


class ToolResult(BaseModel):
    ok: bool = True
    message: str = ""
    observation: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    needs_human: bool = False
    terminal: bool = False


class Tool(BaseModel):
    """Base class. Subclasses declare args as fields and implement ``run``."""

    tool_name: ClassVar[str]
    tool_description: ClassVar[str]
    mutates_state: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    risk: ClassVar[Risk] = "low"

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------

    async def _observe_delta(self, session: BrowserSession, before: Snapshot) -> str:
        await session.settle()
        after = await session.observe()
        return diff(before, after)

    async def _resolve(self, session: BrowserSession, ref: str):
        loc = session.locator(ref)
        if await loc.count() == 0:
            raise LookupError(
                f"ref {ref!r} no longer exists — the page re-rendered since it was perceived. "
                "Take a fresh observation before acting."
            )
        return loc.first


# --------------------------------------------------------------- navigation

class Navigate(Tool):
    tool_name: ClassVar[str] = "navigate"
    tool_description: ClassVar[str] = (
        "Load a URL. Accepts an absolute URL or a path relative to the target's base URL."
    )
    url: str = Field(description="Absolute URL, or a path beginning with '/'.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        await session.goto(self.url)
        after = await session.observe()
        return ToolResult(message=f"navigated to {after.url}", observation=after.render())


class GoBack(Tool):
    tool_name: ClassVar[str] = "go_back"
    tool_description: ClassVar[str] = "Return to the previous page in history."

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        assert session.page
        await session.page.go_back()
        await session.settle()
        after = await session.observe()
        return ToolResult(message="went back", observation=after.render())


# ------------------------------------------------------------------ actions

class Click(Tool):
    tool_name: ClassVar[str] = "click"
    tool_description: ClassVar[str] = "Click the element with the given ref."
    mutates_state: ClassVar[bool] = True
    risk: ClassVar[Risk] = "medium"

    ref: str = Field(description="Element ref from the latest observation, e.g. 'e12'.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        el = await self._resolve(session, self.ref)
        await el.scroll_into_view_if_needed()
        await el.click()
        target = before.by_ref(self.ref)
        label = f"{target.role} {target.name!r}" if target else self.ref
        return ToolResult(
            message=f"clicked {label}",
            observation=await self._observe_delta(session, before),
        )


class Type(Tool):
    tool_name: ClassVar[str] = "type"
    tool_description: ClassVar[str] = (
        "Set the value of a text field. Replaces existing content unless append is true."
    )
    mutates_state: ClassVar[bool] = True

    ref: str = Field(description="Element ref of the field to fill.")
    text: str = Field(description="Text to enter.")
    append: bool = Field(default=False, description="Append instead of replacing.")
    press_enter: bool = Field(default=False, description="Press Enter after typing.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        el = await self._resolve(session, self.ref)
        await el.scroll_into_view_if_needed()
        if self.append:
            await el.click()
            await el.type(self.text, delay=15)
        else:
            await el.fill(self.text)
        if self.press_enter:
            await el.press("Enter")
        return ToolResult(
            message=f"typed {self.text!r} into {self.ref}",
            observation=await self._observe_delta(session, before),
        )


class Select(Tool):
    tool_name: ClassVar[str] = "select"
    tool_description: ClassVar[str] = (
        "Choose an option in a <select>. Use one of the option labels from the observation."
    )
    mutates_state: ClassVar[bool] = True

    ref: str = Field(description="Element ref of the combobox/listbox.")
    option: str = Field(description="Visible label of the option to select.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        el = await self._resolve(session, self.ref)
        target = before.by_ref(self.ref)
        if target and target.options and self.option not in target.options:
            return ToolResult(
                ok=False,
                message=f"{self.option!r} is not an available option. Available: {target.options}",
            )
        await el.select_option(label=self.option)
        return ToolResult(
            message=f"selected {self.option!r}",
            observation=await self._observe_delta(session, before),
        )


class SetCheckbox(Tool):
    tool_name: ClassVar[str] = "set_checkbox"
    tool_description: ClassVar[str] = "Check or uncheck a checkbox / toggle a switch."
    mutates_state: ClassVar[bool] = True

    ref: str = Field(description="Element ref of the checkbox or switch.")
    checked: bool = Field(description="Desired state.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        el = await self._resolve(session, self.ref)
        await el.set_checked(self.checked)
        return ToolResult(
            message=f"set {self.ref} checked={self.checked}",
            observation=await self._observe_delta(session, before),
        )


class Press(Tool):
    tool_name: ClassVar[str] = "press"
    tool_description: ClassVar[str] = "Press a key, e.g. 'Enter', 'Escape', 'Tab', 'Control+s'."

    key: str = Field(description="Key or chord to press.")
    ref: str | None = Field(default=None, description="Optional element to focus first.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        assert session.page
        if self.ref:
            el = await self._resolve(session, self.ref)
            await el.press(self.key)
        else:
            await session.page.keyboard.press(self.key)
        return ToolResult(
            message=f"pressed {self.key}",
            observation=await self._observe_delta(session, before),
        )


class Scroll(Tool):
    tool_name: ClassVar[str] = "scroll"
    tool_description: ClassVar[str] = "Scroll the page, or scroll a specific element into view."

    direction: Literal["down", "up", "top", "bottom"] = "down"
    ref: str | None = Field(default=None, description="Scroll this element into view instead.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        assert session.page
        if self.ref:
            el = await self._resolve(session, self.ref)
            await el.scroll_into_view_if_needed()
        else:
            deltas = {"down": "window.scrollBy(0, window.innerHeight*0.8)",
                      "up": "window.scrollBy(0, -window.innerHeight*0.8)",
                      "top": "window.scrollTo(0, 0)",
                      "bottom": "window.scrollTo(0, document.body.scrollHeight)"}
            await session.page.evaluate(deltas[self.direction])
        return ToolResult(
            message=f"scrolled {self.ref or self.direction}",
            observation=await self._observe_delta(session, before),
        )


class UploadFile(Tool):
    tool_name: ClassVar[str] = "upload_file"
    tool_description: ClassVar[str] = "Attach a local file to a file-upload input."
    mutates_state: ClassVar[bool] = True

    ref: str = Field(description="Element ref of the file input.")
    path: str = Field(description="Absolute path of the file to upload.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        el = await self._resolve(session, self.ref)
        await el.set_input_files(self.path)
        return ToolResult(
            message=f"attached {self.path}",
            observation=await self._observe_delta(session, before),
        )


# ----------------------------------------------------------------- dialogs

class HandleDialog(Tool):
    tool_name: ClassVar[str] = "handle_dialog"
    tool_description: ClassVar[str] = (
        "Respond to a native confirm/prompt dialog that the page raised. "
        "Dialogs are dismissed by default, so re-trigger the action after arming this."
    )
    mutates_state: ClassVar[bool] = True
    reversible: ClassVar[bool] = False
    risk: ClassVar[Risk] = "high"

    accept: bool = Field(description="True to accept, false to dismiss.")
    prompt_text: str | None = Field(default=None, description="Text for a prompt() dialog.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        assert session.page

        async def handler(dialog):
            session.pending_dialogs.append({"type": dialog.type, "message": dialog.message})
            if self.accept:
                await dialog.accept(self.prompt_text or "")
            else:
                await dialog.dismiss()

        session.page.remove_listener("dialog", session._on_dialog)
        session.page.once("dialog", handler)
        return ToolResult(
            message=f"armed dialog handler (accept={self.accept}); repeat the triggering action",
            observation="",
        )


class SwitchTab(Tool):
    tool_name: ClassVar[str] = "switch_tab"
    tool_description: ClassVar[str] = "Switch the active tab by index (0-based)."

    index: int = Field(description="Tab index.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        await session.switch_tab(self.index)
        after = await session.observe()
        return ToolResult(message=f"switched to tab {self.index}", observation=after.render())


# ------------------------------------------------------------ read / finish

class Observe(Tool):
    tool_name: ClassVar[str] = "observe"
    tool_description: ClassVar[str] = (
        "Re-read the current page in full. Use after a re-render invalidates known refs."
    )

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        await session.settle()
        after = await session.observe()
        return ToolResult(message="observed", observation=after.render())


class ReadTable(Tool):
    tool_name: ClassVar[str] = "read_table"
    tool_description: ClassVar[str] = "Read a table's rows as structured data."

    ref: str = Field(description="Table ref, e.g. 't0'.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        table = next((t for t in before.tables if t.ref == self.ref), None)
        if table is None:
            return ToolResult(ok=False, message=f"no table with ref {self.ref!r}")
        return ToolResult(
            message=f"read {len(table.rows)} rows",
            observation=table.render(max_rows=50),
            data={"headers": table.headers, "rows": table.rows},
        )


class AskHuman(Tool):
    tool_name: ClassVar[str] = "ask_human"
    tool_description: ClassVar[str] = (
        "Pause and ask a human operator. Use when the goal is ambiguous or the data needed "
        "is not present anywhere in the system."
    )
    risk: ClassVar[Risk] = "medium"

    question: str = Field(description="What you need from the human.")
    context: str = Field(default="", description="Why you are blocked.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        return ToolResult(
            message=self.question, needs_human=True,
            data={"question": self.question, "context": self.context},
        )


class CompleteStep(Tool):
    tool_name: ClassVar[str] = "complete_step"
    tool_description: ClassVar[str] = (
        "Mark the current plan step finished and advance to the next one. "
        "Only call this once the step's outcome is visible on screen."
    )

    notes: str = Field(default="", description="Anything the next step should know.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        return ToolResult(
            message=f"plan step complete. {self.notes}".strip(),
            data={"step_done": True, "notes": self.notes},
        )


class Done(Tool):
    tool_name: ClassVar[str] = "done"
    tool_description: ClassVar[str] = (
        "Declare the goal complete. Only call this after verifying the change actually "
        "landed in the system — a submitted form is not proof."
    )
    terminal: ClassVar[bool] = True

    summary: str = Field(description="What was accomplished.")
    result: dict[str, Any] = Field(default_factory=dict, description="Structured outcome data.")
    evidence: str = Field(default="", description="What you observed that proves it worked.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        return ToolResult(
            message=self.summary, terminal=True,
            data={"result": self.result, "evidence": self.evidence},
        )


class Fail(Tool):
    tool_name: ClassVar[str] = "fail"
    tool_description: ClassVar[str] = (
        "Declare the goal unachievable and stop. Preferred over looping or inventing data."
    )
    terminal: ClassVar[bool] = True

    reason: str = Field(description="Why the goal cannot be completed.")

    async def run(self, session: BrowserSession, before: Snapshot) -> ToolResult:
        return ToolResult(ok=False, message=self.reason, terminal=True)


ALL_TOOLS: list[type[Tool]] = [
    Navigate, GoBack, Click, Type, Select, SetCheckbox, Press, Scroll, UploadFile,
    HandleDialog, SwitchTab, Observe, ReadTable, CompleteStep, AskHuman, Done, Fail,
]


class ToolRegistry:
    """Holds the tool subset a given sub-agent is allowed to use."""

    def __init__(self, tools: list[type[Tool]] | None = None) -> None:
        self.tools = {t.tool_name: t for t in (tools or ALL_TOOLS)}

    def schemas(self) -> list[dict[str, Any]]:
        """Anthropic tool-use schemas, generated from the Pydantic models."""
        out = []
        for name, cls in self.tools.items():
            schema = cls.model_json_schema()
            schema.pop("title", None)
            out.append({
                "name": name,
                "description": cls.tool_description,
                "input_schema": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            })
        return out

    def build(self, name: str, args: dict[str, Any]) -> Tool:
        if name not in self.tools:
            raise KeyError(f"unknown tool {name!r}; available: {sorted(self.tools)}")
        return self.tools[name](**args)

    def subset(self, names: list[str]) -> ToolRegistry:
        return ToolRegistry([self.tools[n] for n in names if n in self.tools])
