"""Hosted/public-service layer for Lofgren Intelligence.

The certified core remains usable without network services.  This package adds
identity, durable run storage, quotas, OAuth, Stripe entitlement plumbing and a
stateless remote MCP surface for public operation.
"""

from .service import PublicService

__all__ = ["PublicService"]
