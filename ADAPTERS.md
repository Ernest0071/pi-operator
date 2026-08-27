# Pointing this at a different DMS

The agent core knows nothing about any particular web application. Everything
target-specific lives behind one interface, `pi_operator/targets/base.py`.

Adding a target — including a real customer DMS — means implementing that class
and nothing else.

## The interface

```python
class TargetAdapter(ABC):
    name: str
    base_url: str

    async def login(self, session) -> None: ...
    async def is_authenticated(self, session) -> bool: ...
    def workflow_hints(self) -> dict[str, str]: ...
    async def verify(self, check: dict) -> tuple[bool, str]: ...

    def allowed_hosts(self) -> set[str]: ...   # defaults to base_url's host
    def entry_url(self) -> str: ...            # defaults to base_url
```

### `login`
Deterministic on purpose. Authentication is a known, stable path — exactly the
kind of thing that should never cost a model call. Prefer semantic locators
(`get_by_label`, `get_by_role`) over ids, which churn.

### `is_authenticated`
Called before each step so a session that dies mid-run is caught. Make it cheap
— a cookie check or a URL check, not a page load. Getting this wrong is how an
agent cheerfully reports success from a login screen.

### `workflow_hints`
The small amount of prior knowledge that stops the operator rediscovering the
menu structure on every run. Keep them terse and factual — landmarks, not
scripts. Anything that deserves to be a script belongs in the skill library.

```python
{
  "vehicle_inventory": "Vehicles are at /inventory. New vehicle at /inventory/new.",
  "saving": "Ctrl+S saves. A successful save clears the 'Not Saved' indicator.",
}
```

### `verify`
Out-of-band confirmation that a change landed. **This must not go through the
pages the agent just drove**, or a mis-click that lands on a success page
verifies itself.

Use whatever read channel exists — a REST endpoint, a reporting view, a
read-only database user. Reading is not the execution path, so this does not
violate the browser-only rule for taking actions.

If no such channel exists, return `(False, "no out-of-band verification
available")` and the model read-back layer becomes the only verification. That
is weaker, and the run report will show it as `method: read-back` so nobody is
misled about how strong the check was.

## Worked example

```python
from pi_operator.targets.base import TargetAdapter

class AcmeDMSAdapter(TargetAdapter):
    name = "acme"
    base_url = "https://dms.acme-dealer.example"

    async def login(self, session):
        await session.goto("/signin")
        page = session.page
        await page.get_by_label("User ID").fill(self.username)
        await page.get_by_label("Password").fill(self.password)
        await page.get_by_role("button", name="Sign In").click()
        await page.wait_for_url("**/home**")
        await session.settle()

    async def is_authenticated(self, session):
        cookies = await session.context.cookies()
        return any(c["name"] == "ASP.NET_SessionId" and c["value"] for c in cookies)

    def workflow_hints(self):
        return {
            "inventory": "Inventory: Vehicles > Stock List. Add via the New Unit button.",
            "deal": "Deals: Sales > Desking. A deal must have a customer before a unit.",
        }

    async def verify(self, check):
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/units/{check['stock_no']}",
                                 headers=self._auth_headers())
        if r.status_code != 200:
            return False, f"unit {check['stock_no']} not found ({r.status_code})"
        unit = r.json()
        bad = [f"{k}: expected {v!r}, found {unit.get(k)!r}"
               for k, v in check.get("expect", {}).items()
               if str(unit.get(k)) != str(v)]
        return (False, "; ".join(bad)) if bad else (True, "unit matches")
```

Register it:

```python
# pi_operator/targets/__init__.py
ADAPTERS = {..., AcmeDMSAdapter.name: AcmeDMSAdapter}
```

Then:

```bash
PI_TARGET=acme PI_TARGET_BASE_URL=https://dms.acme-dealer.example pi run "..."
```

## What you will also want

**Skills.** The generic operator works immediately, but it will reason its way
through your forms every time. Run each workflow once, then promote the
trajectory (`pi_operator/skills/registry.py`) so the common paths become
deterministic.

**Guardrail tuning.** `Policy.approval_amount_threshold` defaults to 5,000. The
destructive and committing word lists in `pi_operator/guardrails/policy.py` are
English and generic — add your system's vocabulary. If your UI says "Post to
Ledger" or "Finalise RO", add it, because that is the difference between a gate
that fires and one that does not.

**Allowlist.** `allowed_hosts` defaults to the base URL's host. If your DMS
redirects through an SSO domain, add it — otherwise login itself trips the
navigation guard.

## What does not transfer

Selectors, obviously. Less obviously: the **shape of the workflows**. The
planner prompt and the eval scenarios encode dealership concepts as this project
models them. Against a real DMS both want rewriting against the actual
processes — which, per the role context, is most of the real work.
