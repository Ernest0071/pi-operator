"""Seezar Dashboard adapter.

Written before the real DOM was observed, so every locator is a layered guess:
try the semantic thing a human would use (a label, a role, visible text), then
progressively looser fallbacks. Methods that are allowed to fail return False
rather than raising, so a reconnaissance pass keeps going and records what it
found instead of dying on the first wrong guess.

Once `pi recon` has run, the guesses get replaced with what is actually there.
Anything still guessing after that is a bug.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pi_operator.browser.session import BrowserSession
from pi_operator.targets.base import TargetAdapter


class SeezarAdapter(TargetAdapter):
    name = "seezar"
    base_url = "https://seezar-dashboard.seez.dev"

    # ---------------------------------------------------------- auth

    async def login(self, session: BrowserSession) -> None:
        """Restore a human-bootstrapped session.

        Seezar authenticates with a one-time code emailed to the user. There is
        no password an operator could hold, and an agent that could read the
        mailbox would be a much larger security surface than this task warrants.

        So authentication is explicitly human-in-the-loop *once*: `pi login`
        opens a browser, the human completes the OTP, and the resulting cookies
        and localStorage are persisted. Every run after that reuses them.
        Expiry is detected and reported rather than worked around.
        """
        if not self.auth_state_path.exists():
            raise RuntimeError(
                f"No saved session for {self.name}. Run `pi login` and complete the "
                "emailed one-time code; the session is then reused by every run."
            )
        # BrowserSession loads the storage state at construction, so by here we
        # are either already authenticated or the saved session has expired.
        await session.goto("/")
        if not await self.is_authenticated(session):
            raise RuntimeError(
                f"The saved {self.name} session has expired. Run `pi login` again."
            )

    async def bootstrap_login(self, session: BrowserSession, wait_for_human) -> bool:
        """Interactive first-time login. Called only by `pi login`."""
        await session.goto("/login")
        page = session.page
        assert page

        if self.username:
            email = page.locator(
                "input[type='email'], input[name*='email' i], input[id*='email' i], "
                "input[placeholder*='mail' i]"
            )
            try:
                if await email.count():
                    await email.first.fill(self.username)
            except Exception:
                pass

        await wait_for_human()
        await session.settle()
        return await self.is_authenticated(session)

    async def is_authenticated(self, session: BrowserSession) -> bool:
        page = session.page
        if not page:
            return False
        if "/login" in page.url:
            return False
        try:
            # The dealership list is the reliable signal that we are inside.
            if await page.locator("a[href*='/dealership/']").count():
                return True
            # Otherwise: no OTP/email prompt left on screen.
            if await page.locator("input[type='password'], input[type='email']").count():
                return False
        except Exception:
            return False
        return True

    # ------------------------------------------------- navigation

    async def list_dealerships(self, session: BrowserSession) -> list[dict[str, str]]:
        """Every dealership in the left-hand tree, as {id, name, url}.

        The tree is built from ``<li data-dealership-id>`` nodes whose click
        handlers are bound in JavaScript — they are not anchors, and only the
        currently open dealership carries an href. The data attribute is the
        stable identifier, and it maps straight onto ``/dealership/{id}``,
        which is what makes Scenario IV's sweep navigable rather than a
        sequence of fragile clicks.

        Order follows the DOM, so "the first N dealerships" is well defined.
        """
        page = session.page
        assert page
        try:
            # The dealership tree arrives after the page shell; harvesting too
            # early silently returns an empty list, which downstream reads as
            # "this account has no dealerships".
            await page.wait_for_selector("li[data-dealership-id]", timeout=20_000)
        except Exception:
            return []
        try:
            found = await page.evaluate(r"""
              () => Array.from(document.querySelectorAll('li[data-dealership-id]'))
                .map((li) => {
                  // .nodeName holds the label AND a duplicate .popover copy;
                  // take the first <p> only or every name comes back doubled.
                  const label = li.querySelector('.nodeName p:not(.popover)')
                    || li.querySelector('.nodeName p') || li.querySelector('.nodeName');
                  const name = (label ? label.textContent : li.textContent) || '';
                  return {
                    id: li.getAttribute('data-dealership-id'),
                    name: name.replace(/\s+/g, ' ').trim(),
                    depth: li.querySelectorAll('li[data-dealership-id]').length,
                    index: li.getAttribute('index') || '',
                  };
                })
                .filter((d) => d.id && d.name)
            """)
        except Exception:
            return []

        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in found:
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            out.append({
                "id": entry["id"],
                "name": entry["name"],
                "index": str(entry.get("index", "")),
                "url": f"{self.base_url}/dealership/{entry['id']}",
            })
        return out

    async def find_dealership(self, session: BrowserSession, name: str) -> dict[str, str] | None:
        """Locate one dealership by name, searching the list if one is offered."""
        wanted = name.lower().strip()
        for entry in await self.list_dealerships(session):
            if wanted in entry["name"].lower():
                return entry

        page = session.page
        assert page
        search = page.locator(
            "input[type='search'], input[placeholder*='search' i], input[aria-label*='search' i]"
        )
        try:
            if await search.count():
                await search.first.fill(name)
                await session.settle(3_000)
                for entry in await self.list_dealerships(session):
                    if wanted in entry["name"].lower():
                        return entry
        except Exception:
            pass
        return None

    async def open_dealership(self, session: BrowserSession, target: str | dict) -> bool:
        """Open a dealership by id, by {id,...} record, or by name."""
        if isinstance(target, dict):
            await session.goto(f"/dealership/{target['id']}")
            return True
        if str(target).isdigit():
            await session.goto(f"/dealership/{target}")
            return True

        entry = await self.find_dealership(session, str(target))
        if entry:
            await session.goto(f"/dealership/{entry['id']}")
            return True
        return False

    # The app ships its own stable test ids. Note the tab buttons carry
    # aria-label="" which *suppresses* their accessible name, so role+name
    # matching silently fails on them — the id is the reliable handle.
    TAB_IDS: ClassVar[dict[str, str]] = {
        "overview": "button-header-overview",
        "data sources": "button-header-data",
        "customization": "button-header-customization",
        "usage": "button-header-usage",
        "conversations": "button-header-chat-history",
        "analytics": "button-header-analytics",
        "leads": "button-header-leads",
    }

    async def open_tab(self, session: BrowserSession, tab: str) -> bool:
        """Open a dealership tab and wait for its content to render."""
        page = session.page
        assert page

        test_id = self.TAB_IDS.get(tab.strip().lower())
        if test_id:
            locator = page.locator(f"[data-test-id='{test_id}'], #{test_id}").first
            try:
                # The tab bar mounts after the dealership page shell. Clicking
                # before it exists silently does nothing and the caller then
                # reads whatever page it is still on.
                await locator.wait_for(state="visible", timeout=20_000)
            except Exception:
                pass
            try:
                if await locator.count():
                    before = (await session.observe()).digest
                    await locator.click()
                    # Tabs render client-side without changing the URL, so wait
                    # for the page content itself to change.
                    for _ in range(20):
                        await session.settle(1_500)
                        if (await session.observe()).digest != before:
                            return True
                    return True
            except Exception:
                pass

        # Fall back to visible text, since our own perception resolves these
        # buttons by text even though role+name matching does not.
        try:
            snap = await session.observe()
            match = next((e for e in snap.elements
                          if e.name.strip().lower() == tab.strip().lower()
                          and e.role in ("button", "tab", "link")), None)
            if match:
                await session.locator(match.ref).first.click()
                await session.settle()
                return True
        except Exception:
            pass
        return False

    async def open_analytics(self, session: BrowserSession, dealership: str | dict) -> bool:
        """Open a dealership's Analytics view by URL.

        Preferred over clicking the Analytics tab. The tab is bound to a handler
        that needs the dealership's bot id, and while that is unresolved the
        click is silently inert — no navigation, no error, no failed request,
        which is the hardest kind of failure to detect. Observed intermittently
        on this environment.

        `/dealership/{id}/analytics` redirects to `/dealership/{id}/analytics/{botId}`
        on its own, so the bot id never has to be discovered or cached.
        """
        did = dealership["id"] if isinstance(dealership, dict) else str(dealership)
        await session.goto(f"/dealership/{did}/analytics")
        page = session.page
        assert page
        try:
            await page.wait_for_url(re.compile(r"/analytics(/\d+)?"), timeout=25_000)
        except Exception:
            pass
        await session.settle(3_000)
        return "/analytics" in (page.url or "")

    async def set_date_range(self, session: BrowserSession, label: str) -> bool:
        """Set the MAIN analytics range (the `.dateFilterSelect` control).

        Deliberately scoped. The analytics page carries a second, unrelated
        range control — the "7 Days / 14 Days" buttons on the busiest-day card —
        and a loose text match hits that one instead, silently changing a
        different chart while the caller believes it changed the page range.
        The main control offers only "30 Days" and "90 Days".
        """
        page = session.page
        assert page
        wanted = label.strip().lower().replace("days", "").strip()

        try:
            control = page.locator(".dateFilterSelect").first
            if not await control.count():
                return False
            current = (await control.locator("input").first.get_attribute("value") or "").strip()
            if wanted and wanted in current.lower():
                return True                      # already on the requested range

            before = (await session.observe()).digest
            await control.locator("input").first.click()
            await session.settle(1_500)

            option = control.locator("li", has_text=re.compile(rf"^\s*{wanted}\s*days?\s*$", re.I))
            if not await option.count():
                return False
            await option.first.click()

            for _ in range(20):
                await session.settle(1_500)
                if (await session.observe()).digest != before:
                    return True
            return True
        except Exception:
            return False

    async def available_date_ranges(self, session: BrowserSession) -> list[str]:
        """What the main range control actually offers."""
        page = session.page
        assert page
        try:
            return await page.evaluate("""
              () => Array.from(
                document.querySelectorAll('.dateFilterSelect li')
              ).map((li) => li.textContent.trim()).filter(Boolean)
            """)
        except Exception:
            return []

    async def set_timeline_range(self, session: BrowserSession, label: str) -> bool:
        """Set the busiest-day card's own range ("7 Days" / "14 Days").

        Separate from the page range on purpose — see set_date_range.
        """
        page = session.page
        assert page
        try:
            button = page.locator(".filterOptions button", has_text=re.compile(
                rf"^\s*{label.strip().split()[0]}\s*days?\s*$", re.I))
            if not await button.count():
                return False
            await button.first.click()
            await session.settle(2_000)
            return True
        except Exception:
            return False

    async def download_chat_history(self, session: BrowserSession) -> str | None:
        """Scenario II: the 'Chat History' button top-right yields a zip.

        Downloads are captured by BrowserSession; this returns the saved path.
        """
        page = session.page
        assert page
        before = len(session.downloads)
        button = page.get_by_role("button", name=re.compile(r"chat\s*history", re.I))
        if not await button.count():
            button = page.get_by_text(re.compile(r"chat\s*history", re.I))
        if not await button.count():
            return None
        await button.first.click()
        for _ in range(40):
            if len(session.downloads) > before:
                return str(session.downloads[-1])
            await session.settle(500)
        return None

    # ------------------------------------------------ prior knowledge

    def workflow_hints(self) -> dict[str, str]:
        return {
            "dealerships": (
                "Dealerships are listed down the left-hand side, each linking to "
                "/dealership/{id}. The list spans multiple pages."
            ),
            "tabs": (
                "Each dealership page has tabs: Overview, Data Sources, Customization, "
                "Usage, Conversations, Analytics, Leads. Dealership pages live at "
                "/dealership/{id}."
            ),
            "analytics": (
                "The Analytics tab has a date-range control offering ranges such as "
                "'7 days' and '30 days'. Engagement charts are further down the page, "
                "so scroll before reading them."
            ),
            "engagement": (
                "The 'User Engagement' card shows a total click count and a breakdown by "
                "event type (for example Forms submitted, CTAs clicked, Carousel clicked), "
                "each with a percentage and an absolute count."
            ),
            "conversations": (
                "The Conversations tab lists chat sessions by reference number and shows the "
                "transcript of the selected one."
            ),
            "chat_history": (
                "A 'Chat History' button at the top right of a dealership page downloads a "
                "zip archive of chat records."
            ),
        }

    def allowed_hosts(self) -> set[str]:
        return {"seezar-dashboard.seez.dev"}

    # ------------------------------------------------- verification

    async def verify(self, check: dict[str, Any]) -> tuple[bool, str]:
        """No out-of-band read channel is available on this target.

        Reported honestly rather than faked: verification for Seezar scenarios
        falls back to the model read-back layer, and the run report will say so.
        """
        return False, (
            "no out-of-band verification channel on the Seezar dashboard; "
            "relying on independent read-back instead"
        )
