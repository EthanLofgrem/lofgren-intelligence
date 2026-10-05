"""Typed V3 production errors. Every one is a ValueError, so callers and the MCP server treat it as bad input."""

from __future__ import annotations


class ProductionError(ValueError):
    """V3 refused to produce, or could not verify, an artifact."""


class SpecificationError(ProductionError):
    """The handoff cannot be compiled into a production specification."""
