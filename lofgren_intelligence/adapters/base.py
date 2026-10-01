"""Evidence adapters and the source registry.

Every sense the system has (documents, the web, orbital tracking, satellite
imagery catalogs, IoT sensors, datasets) plugs in through one interface and
describes itself in the registry: what it can observe, what it costs, what
license governs it and which operations are authorized. The research planner
asks "which available source can answer this requirement?" instead of
hard-coding tools into workflows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..evidence.types import Evidence, Source

if TYPE_CHECKING:
    from ..intent.compiler import OutcomeContract, Question


@dataclass
class GatherResult:
    items: list[tuple[Source, Evidence]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # gaps, limits, reasons for nothing
    cost_usd: float = 0.0  # external data cost incurred (passed through to billing)


class Adapter(ABC):
    id: str = "adapter"
    capabilities: frozenset[str] = frozenset()
    license: str = "unknown"
    cost_per_call_usd: float = 0.0
    authorized_operations: frozenset[str] = frozenset({"read"})
    description: str = ""

    def available(self) -> bool:
        return True

    @abstractmethod
    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        ...

    def describe(self) -> dict:
        return {
            "id": self.id,
            "capabilities": sorted(self.capabilities),
            "license": self.license,
            "cost_per_call_usd": self.cost_per_call_usd,
            "authorized_operations": sorted(self.authorized_operations),
            "available": self.available(),
            "description": self.description,
        }


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> Adapter:
        if "write" in adapter.authorized_operations or "control" in adapter.authorized_operations:
            raise PermissionError(
                f"adapter {adapter.id} requests write/control access; V1 adapters are read-only. "
                "Actions go through the authority engine (V4)."
            )
        self._adapters[adapter.id] = adapter
        return adapter

    def get(self, adapter_id: str) -> Adapter:
        return self._adapters[adapter_id]

    def all(self) -> list[Adapter]:
        return list(self._adapters.values())

    def find(self, capability: str) -> list[Adapter]:
        """Available adapters that offer a capability, cheapest first."""
        found = [a for a in self._adapters.values() if capability in a.capabilities and a.available()]
        return sorted(found, key=lambda a: a.cost_per_call_usd)

    def describe(self) -> list[dict]:
        return [a.describe() for a in self._adapters.values()]
