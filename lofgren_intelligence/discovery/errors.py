"""Typed errors for Discovery Intelligence. V2 fails closed.

Every error says what was wrong and where. Scientifically meaningful input
is never silently repaired: a NaN, a unit mismatch or a dangling reference
stops the operation with one of these.
"""

from __future__ import annotations


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


class InputTooLarge(DiscoveryError, ValueError):
    pass


class UnsafeName(DiscoveryError, ValueError):
    pass


class ReceiptTampered(DiscoveryError, ValueError):
    pass


class PromotionRefused(DiscoveryError, PermissionError):
    """Anything that tries to turn a hypothesis, candidate or simulation into verified evidence."""


__all__ = [
    "DiscoveryError", "DuplicateId", "ImpossibleTimestamp", "InputTooLarge", "InvalidScope", "MalformedInput",
    "NegativeCost", "NonFiniteValue", "PromotionRefused", "ReceiptTampered", "UnitMismatch", "UnknownReference",
    "UnknownStatus", "UnsafeName", "UnsupportedAlgorithm",
]
