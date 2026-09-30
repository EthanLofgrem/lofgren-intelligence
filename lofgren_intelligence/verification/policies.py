"""Evidence sufficiency policies: the bar depends on what kind of claim it is.

    attribution   "Company X reported Y"  -> one primary source establishes that X said it
    quantitative  carries a number        -> the contract's standard (default 2 independent sources)
    trend         direction of change     -> the contract's standard
    physical      direct observation      -> one direct measurement, no contradiction
    general       anything else           -> the contract's standard

A policy never lowers the bar below one source, and a hypothesis (V2) never
passes any policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..evidence.types import Claim, ClaimOrigin, ClaimType
from ..models.provider import trend

_ATTRIBUTION = re.compile(r"\b(said|says|stated|states|reported|reports|announced|announces|claims|claimed|"
                          r"according to|told|disclosed|estimates|estimated)\b", re.I)


@dataclass(frozen=True)
class Policy:
    name: str
    min_independent_sources: int
    min_confidence: float
    requires_observation: bool = False
    description: str = ""


def classify_claim(claim: Claim) -> ClaimType:
    if claim.origin == ClaimOrigin.OBSERVED:
        return ClaimType.PHYSICAL
    if _ATTRIBUTION.search(claim.statement):
        return ClaimType.ATTRIBUTION
    if claim.value is not None:
        return ClaimType.QUANTITATIVE
    if trend(set(claim.topic)):
        return ClaimType.TREND
    return ClaimType.GENERAL


def policy_for(claim_type: ClaimType, min_sources: int = 2, min_conf: float = 0.7) -> Policy:
    if claim_type == ClaimType.ATTRIBUTION:
        return Policy("attribution-1", 1, min(0.6, min_conf),
                      description="one primary source establishes what the source said, not that it is true")
    if claim_type == ClaimType.PHYSICAL:
        return Policy("direct-observation", 1, min_conf, requires_observation=True,
                      description="a direct measurement or computation with no contradiction")
    return Policy(f"{claim_type.value}-{min_sources}", max(1, min_sources), min_conf,
                  description=f"{min_sources} independent sources and confidence of at least {min_conf:.0%}")
