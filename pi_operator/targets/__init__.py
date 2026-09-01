"""Target registry."""

from __future__ import annotations

from pi_operator.config import settings
from pi_operator.targets.base import TargetAdapter
from pi_operator.targets.mockdms import MockDMSAdapter
from pi_operator.targets.seezar import SeezarAdapter

ADAPTERS: dict[str, type[TargetAdapter]] = {
    SeezarAdapter.name: SeezarAdapter,
    MockDMSAdapter.name: MockDMSAdapter,
}


def get_target(name: str | None = None, **overrides) -> TargetAdapter:
    key = (name or settings.target).lower()
    if key not in ADAPTERS:
        raise KeyError(f"unknown target {key!r}; available: {sorted(ADAPTERS)}")
    cls = ADAPTERS[key]
    return cls(
        base_url=overrides.get("base_url") or settings.target_base_url,
        username=overrides.get("username") or settings.target_username,
        password=overrides.get("password") or settings.target_password,
    )


__all__ = ["ADAPTERS", "ERPNextAdapter", "MockDMSAdapter", "TargetAdapter", "get_target"]
