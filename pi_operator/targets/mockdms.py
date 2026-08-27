"""Adapter for the bundled mock DMS.

The mock exists for one reason the real target cannot serve: **controlled
failure**. You cannot ask a real ERP to expire a session on step 7, mutate a
selector, or fail a save with a validation error on demand — and without that,
robustness numbers are anecdotes.

It is an eval fixture, not the demo target, and the README says so.
"""

from __future__ import annotations

from typing import Any

import httpx

from pi_operator.browser.session import BrowserSession
from pi_operator.targets.base import TargetAdapter


class MockDMSAdapter(TargetAdapter):
    name = "mockdms"
    base_url = "http://localhost:8080"

    async def login(self, session: BrowserSession) -> None:
        await session.goto("/login")
        page = session.page
        assert page
        await page.fill("#username", self.username or "operator")
        await page.fill("#password", self.password or "operator")
        await page.click("button[type=submit]")
        await session.settle()

    async def is_authenticated(self, session: BrowserSession) -> bool:
        page = session.page
        if not page:
            return False
        if "/login" in page.url:
            return False
        try:
            cookies = await session.context.cookies()  # type: ignore[union-attr]
        except Exception:
            return False
        return any(c["name"] == "dms_session" and c["value"] for c in cookies)

    def workflow_hints(self) -> dict[str, str]:
        return {
            "inventory": "Vehicle inventory at /inventory. Add a vehicle at /inventory/new.",
            "customers": "Customers at /customers; create at /customers/new.",
            "deals": (
                "Deals at /deals. Building a deal is a 3-step wizard: choose customer, "
                "choose vehicle, then add F&I products and submit for finance approval."
            ),
            "service": "Repair orders at /service.",
            "reports": "Inventory aging report at /reports/aging, with a CSV export button.",
        }

    async def verify(self, check: dict[str, Any]) -> tuple[bool, str]:
        """Verify against the mock's read-only introspection endpoint.

        Deliberately separate from the pages the agent drives, so a mis-click
        cannot verify itself.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{self.base_url}/api/_verify", json=check)
        except Exception as exc:
            return False, f"verification request failed: {exc}"
        if resp.status_code != 200:
            return False, f"verification returned {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        return bool(body.get("passed")), str(body.get("detail", ""))
