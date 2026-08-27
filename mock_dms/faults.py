"""Controlled failure injection.

This is the mock's reason to exist. You cannot ask a real ERP to expire a
session on request 7, rename a button, or reject the next save — and without
being able to do that on demand, "the agent recovers from failures" is an
anecdote rather than a measurement.

Each fault models a failure that actually happens in enterprise web apps.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field


class FaultConfig(BaseModel):
    # Network/server slowness — tests settle-detection and timeouts.
    latency_ms: int = 0

    # Reject the next N form submissions with a field-level validation error,
    # the way a real server-side rule would.
    fail_next_saves: int = 0
    validation_message: str = "Mileage must be a whole number of miles."

    # Drop the session after N requests — tests mid-run re-authentication.
    expire_session_after: int = 0

    # Rename primary action buttons — tests self-healing selector resolution.
    mutate_labels: bool = False
    # Mild drift by default — a label extended, which is how real renames
    # usually look and which self-healing resolution should absorb.
    label_map: dict[str, str] = Field(
        default_factory=lambda: {
            "Save Vehicle": "Save Vehicle Record",
            "Save Customer": "Save Customer Record",
        }
    )

    # Drastic rewrite — resolution SHOULD fail here, so the run falls back to
    # the model instead of confidently clicking the wrong control.
    drastic_labels: bool = False

    # Return HTTP 500 on the next N requests — tests retry behaviour.
    fail_next_requests: int = 0

    request_count: int = 0

    DRASTIC: ClassVar[dict[str, str]] = {
        "Save Vehicle": "Commit Unit To Stock",
        "Save Customer": "Enrol Account Holder",
    }

    def label(self, original: str) -> str:
        if self.drastic_labels:
            return self.DRASTIC.get(original, original)
        if not self.mutate_labels:
            return original
        return self.label_map.get(original, original)

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump()


FAULTS = FaultConfig()


def reset_faults() -> None:
    """Clear all faults *in place*.

    Rebinding the module global would not work: importers did
    ``from mock_dms.faults import FAULTS``, which binds the object, not the
    name, so a rebind here would leave them pointing at the old config and
    faults would silently leak between scenarios.
    """
    fresh = FaultConfig()
    for name in FaultConfig.model_fields:
        setattr(FAULTS, name, getattr(fresh, name))
