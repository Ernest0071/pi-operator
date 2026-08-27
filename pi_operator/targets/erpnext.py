"""ERPNext adapter, configured as a dealership.

ERPNext is a real third-party ERP, not something written for this exercise, which
is the point: it was not designed to be automated by us. Its Desk UI is a
single-page app with generated element ids, virtualised list views and AJAX
saves — the failure modes are the ones a real DMS has.

Domain mapping used throughout:

    vehicle          -> Item (with VIN / make / model / mileage custom fields)
    customer         -> Customer
    deal / quote     -> Quotation
    sales order      -> Sales Order
    repair order     -> Maintenance Visit

Where ERPNext's own concepts leak (an "Item" is not a vehicle), the skill layer
and the prompt do the translation, not the agent's imagination.
"""

from __future__ import annotations

from typing import Any

import httpx

from pi_operator.browser.session import BrowserSession
from pi_operator.targets.base import TargetAdapter


class ERPNextAdapter(TargetAdapter):
    name = "erpnext"
    base_url = "http://localhost:8000"

    async def login(self, session: BrowserSession) -> None:
        await session.goto("/login")
        page = session.page
        assert page

        # Semantic locators with fallbacks: ERPNext's ids are stable today but
        # the label text is what a human would use, so prefer it.
        email = page.locator("#login_email, input[name='email'], input[type='email']").first
        pwd = page.locator("#login_password, input[name='password'], input[type='password']").first
        await email.fill(self.username)
        await pwd.fill(self.password)
        await pwd.press("Enter")

        await page.wait_for_url("**/app**", timeout=30_000)
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
        return any(c["name"] == "sid" and c["value"] not in ("", "Guest") for c in cookies)

    def workflow_hints(self) -> dict[str, str]:
        return {
            "vehicle_inventory": (
                "Vehicles are Items. List at /app/item. New vehicle at /app/item/new. "
                "VIN, make, model, mileage and condition are custom fields on the Item form."
            ),
            "customer": (
                "Customers at /app/customer; new at /app/customer/new. "
                "Customer Name and Customer Group are required."
            ),
            "deal": (
                "A deal is a Quotation at /app/quotation/new. Set the customer, add the "
                "vehicle Item as a row in the Items table, then Submit to advance it."
            ),
            "sales_order": "Confirmed deals become Sales Orders at /app/sales-order.",
            "service": "Repair orders are Maintenance Visits at /app/maintenance-visit.",
            "saving": (
                "Ctrl+S saves a document. A successful save changes the status indicator "
                "from 'Not Saved' and the URL stops ending in '/new'."
            ),
            "reports": (
                "Report view is the list URL with '/view/report'. Filters are set from the "
                "filter area above the list."
            ),
        }

    def entry_url(self) -> str:
        return f"{self.base_url}/app"

    async def verify(self, check: dict[str, Any]) -> tuple[bool, str]:
        """Verify via ERPNext's REST read API — out-of-band from the browser path.

        ``check`` shape::

            {"doctype": "Item", "filters": {"item_code": "..."},
             "expect": {"custom_vin": "JH4..."}}
        """
        doctype = check.get("doctype")
        filters = check.get("filters", {})
        expect: dict[str, Any] = check.get("expect", {})
        if not doctype:
            return False, "check is missing 'doctype'"

        url = f"{self.base_url}/api/resource/{doctype}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    params={"filters": _erp_filters(filters), "fields": _erp_fields(expect)},
                    auth=(self.username, self.password),
                    headers={"Accept": "application/json"},
                )
        except Exception as exc:  # network/target down is a failed verification
            return False, f"verification request failed: {exc}"

        if resp.status_code != 200:
            return False, f"verification API returned {resp.status_code}: {resp.text[:200]}"

        rows = resp.json().get("data", [])
        if not rows:
            return False, f"no {doctype} matching {filters}"

        row = rows[0]
        mismatches = [
            f"{k}: expected {v!r}, found {row.get(k)!r}"
            for k, v in expect.items()
            if str(row.get(k, "")).strip() != str(v).strip()
        ]
        if mismatches:
            return False, "; ".join(mismatches)
        return True, f"{doctype} matching {filters} exists with expected values"


def _erp_filters(filters: dict[str, Any]) -> str:
    import json

    return json.dumps([[k, "=", v] for k, v in filters.items()])


def _erp_fields(expect: dict[str, Any]) -> str:
    import json

    return json.dumps(["name", *expect.keys()])
