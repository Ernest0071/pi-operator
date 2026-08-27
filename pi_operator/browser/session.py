"""Browser session: the single place that owns Playwright lifecycle and page state.

Everything the agent does to the outside world goes through here, which makes it
the natural choke point for the things that make autonomy survivable in a real
enterprise app: dialog interception, popup/tab tracking, download capture,
settle-detection for AJAX rerenders, and per-step screenshot evidence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    Playwright,
    async_playwright,
)

from pi_operator.browser.perception import Snapshot, distill
from pi_operator.config import settings


class BrowserSession:
    """Owns one browser context for the duration of a run."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        storage_state: Path | None = None,
        artifacts_dir: Path | None = None,
        base_url: str | None = None,
    ) -> None:
        self.headless = settings.headless if headless is None else headless
        self.storage_state = storage_state
        self.artifacts_dir = artifacts_dir or (settings.runs_dir / "scratch")
        self.base_url = base_url or settings.target_base_url

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

        # Dialogs are auto-dismissed but recorded: an agent that silently accepts
        # every confirm() is an agent that will eventually delete something.
        self.pending_dialogs: list[dict[str, str]] = []
        self.downloads: list[Path] = []
        self._screenshot_seq = 0

    # ------------------------------------------------------------------ setup

    async def start(self) -> BrowserSession:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)

        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": settings.viewport_width, "height": settings.viewport_height},
            "accept_downloads": True,
        }
        if self.storage_state and self.storage_state.exists():
            ctx_kwargs["storage_state"] = str(self.storage_state)

        self.context = await self._browser.new_context(**ctx_kwargs)
        self.context.set_default_timeout(settings.nav_timeout_ms)
        self.context.on("page", self._on_new_page)

        self.page = await self.context.new_page()
        self._wire_page(self.page)
        return self

    def _wire_page(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)

    def _on_new_page(self, page: Page) -> None:
        """A popup or target=_blank became the active surface."""
        self._wire_page(page)
        self.page = page

    async def _on_dialog(self, dialog: Dialog) -> None:
        self.pending_dialogs.append({"type": dialog.type, "message": dialog.message})
        # Default-deny: confirm/prompt dialogs are decisions, and decisions belong
        # to the agent loop (or a human), not to an event handler.
        try:
            if dialog.type == "alert":
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:
            pass

    async def _on_download(self, download) -> None:
        dest = self.artifacts_dir / "downloads" / download.suggested_filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await download.save_as(str(dest))
            self.downloads.append(dest)
        except Exception:
            pass

    # ------------------------------------------------------------- operations

    async def goto(self, url: str) -> None:
        assert self.page
        if url.startswith("/"):
            url = self.base_url.rstrip("/") + url
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.settle()

    async def settle(self, timeout_ms: int = 5_000) -> None:
        """Wait for the page to stop changing.

        Enterprise SPAs rarely reach networkidle, so we fall back to DOM
        quiescence: two consecutive identical perception digests.
        """
        assert self.page
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return
        except Exception:
            pass

        previous = None
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            try:
                current = (await distill(self.page)).digest
            except Exception:
                await asyncio.sleep(0.25)
                continue
            if current == previous:
                return
            previous = current
            await asyncio.sleep(0.35)

    async def observe(self) -> Snapshot:
        assert self.page
        return await distill(self.page)

    def locator(self, ref: str):
        """Resolve a perception ref to a Playwright locator.

        Fails loudly when the ref is gone: a vanished ref means the page
        re-rendered between perceive and act, and acting anyway is how agents
        click the wrong row.
        """
        assert self.page
        return self.page.locator(f'[data-pi-ref="{ref}"]')

    async def screenshot(self, label: str = "step") -> Path:
        assert self.page
        self._screenshot_seq += 1
        path = self.artifacts_dir / "screens" / f"{self._screenshot_seq:03d}_{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.page.screenshot(path=str(path))
        except Exception:
            return path
        return path

    async def save_auth(self, path: Path) -> None:
        assert self.context
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(path))

    async def tabs(self) -> list[str]:
        assert self.context
        return [p.url for p in self.context.pages]

    async def switch_tab(self, index: int) -> None:
        assert self.context
        pages = self.context.pages
        if not 0 <= index < len(pages):
            raise IndexError(f"tab {index} out of range (0..{len(pages) - 1})")
        self.page = pages[index]
        await self.page.bring_to_front()

    # ---------------------------------------------------------------- teardown

    async def close(self) -> None:
        for closer in (self.context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    async def __aenter__(self) -> BrowserSession:
        return await self.start()

    async def __aexit__(self, *_exc) -> None:
        await self.close()
