"""Target adapters: everything that is specific to one web application.

The agent core knows nothing about any particular DMS. All target-specific
knowledge lives behind this interface:

* how to authenticate
* how to tell whether we are still authenticated (sessions expire mid-run)
* navigation hints — the small amount of prior knowledge that saves an agent
  from rediscovering the menu structure on every run
* how to verify, out-of-band, that a change actually landed

That last one matters. Verification must not go through the same browser path
the agent just used, or a mis-click that lands on a "success" page verifies
itself. Where a target exposes a read API, verification uses it — reading is not
the execution path, so this does not violate the browser-only rule for *doing*.

Adding a new target — including a real customer DMS — means implementing this
class and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from pi_operator.browser.session import BrowserSession


class TargetAdapter(ABC):
    name: str
    base_url: str

    def __init__(self, base_url: str | None = None, username: str = "", password: str = "") -> None:
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.username = username
        self.password = password

    # -- authentication --------------------------------------------------

    @abstractmethod
    async def login(self, session: BrowserSession) -> None:
        """Authenticate. Deterministic on purpose: login is never worth an LLM."""

    @abstractmethod
    async def is_authenticated(self, session: BrowserSession) -> bool:
        """Cheap check used before each step to catch mid-run session expiry."""

    @property
    def auth_state_path(self):
        """Where this target's persisted browser session is stored."""
        from pi_operator.config import settings

        return settings.auth_dir / f"{self.name}.json"

    async def ensure_authenticated(self, session: BrowserSession) -> bool:
        """Returns True if a re-login was performed."""
        if await self.is_authenticated(session):
            return False
        await self.login(session)
        return True

    # -- prior knowledge -------------------------------------------------

    @abstractmethod
    def workflow_hints(self) -> dict[str, str]:
        """Named navigation hints injected into the navigator's prompt.

        Deliberately terse. These are landmarks, not scripts — anything that
        deserves to be a script belongs in the skill library instead.
        """

    def entry_url(self) -> str:
        return self.base_url

    # -- guardrails ------------------------------------------------------

    def allowed_hosts(self) -> set[str]:
        """Hosts the agent may navigate to. Anything else aborts the run."""
        host = urlparse(self.base_url).netloc
        return {host}

    # -- verification ----------------------------------------------------

    @abstractmethod
    async def verify(self, check: dict[str, Any]) -> tuple[bool, str]:
        """Out-of-band confirmation that a record exists / has expected values.

        Returns ``(passed, detail)``. Used by the verifier node and by the
        eval harness, which must never take the agent's word for success.
        """

    def describe(self) -> str:
        lines = [f"TARGET: {self.name} at {self.base_url}", "", "NAVIGATION HINTS:"]
        for key, hint in self.workflow_hints().items():
            lines.append(f"  - {key}: {hint}")
        return "\n".join(lines)
