"""Typed errors for Discovery Intelligence. V2 fails closed.

Every error says what was wrong and where. Scientifically meaningful input
is never silently repaired: a NaN, a unit mismatch or a dangling reference
stops the operation with one of these.
"""

from __future__ import annotations

from ..research.planner import DependencyCycle as _V1DependencyCycle


class DiscoveryError(Exception):
    """Base class. `where` names the object, field or path that failed."""

    def __init__(self, message: str, where: str = "") -> None:
        self.where = where
        super().__init__(f"{message}" + (f" (at {where})" if where else ""))


class MalformedInput(DiscoveryError, ValueError):
    pass


class UnknownReference(DiscoveryError, KeyError):
    def __str__(self) -> str:  # KeyError would otherwise quote the message
        return Exception.__str__(self)


class UnitMismatch(DiscoveryError, ValueError):
    pass


class NonFiniteValue(DiscoveryError, ValueError):
    """NaN, Infinity, or an operation (such as division by zero) that produces one."""


class NegativeCost(DiscoveryError, ValueError):
    pass


class ImpossibleTimestamp(DiscoveryError, ValueError):
    pass


class InvalidScope(DiscoveryError, ValueError):
    pass


class UnsupportedAlgorithm(DiscoveryError, ValueError):
    pass


class UnknownStatus(DiscoveryError, ValueError):
    pass


class DuplicateId(DiscoveryError, ValueError):
    pass


class InvalidTransition(DiscoveryError, ValueError):
    """A status change the object's lifecycle does not allow."""


class DependencyCycle(DiscoveryError, _V1DependencyCycle):
    """Objects derive from each other in a cycle. Also catchable as the V1 research-planner error."""


class InputTooLarge(DiscoveryError, ValueError):
    pass


class UnsafeName(DiscoveryError, ValueError):
    pass


class ReceiptTampered(DiscoveryError, ValueError):
    pass


class FalseNovelty(MalformedInput):
    """Novelty language ("novel", "new", "first", ...) where only search coverage can be stated."""


class ContextMismatch(DiscoveryError, ValueError):
    """An object or reference belongs to a different V1 research run or knowledge map."""


class PromotionRefused(DiscoveryError, PermissionError):
    """Anything that tries to turn a hypothesis, candidate or simulation into verified evidence."""


__all__ = [
    "ContextMismatch", "DependencyCycle", "DiscoveryError", "DuplicateId", "FalseNovelty", "ImpossibleTimestamp", "InputTooLarge", "InvalidScope",
    "InvalidTransition", "MalformedInput", "NegativeCost", "NonFiniteValue", "PromotionRefused", "ReceiptTampered",
    "UnitMismatch", "UnknownReference", "UnknownStatus", "UnsafeName", "UnsupportedAlgorithm",
]
