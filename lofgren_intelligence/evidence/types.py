"""Typed evidence.

The evidence graph, not the language model, is the source of truth. Every
item keeps its provenance: source + time + place + license + confidence +
transformation history. A newspaper claim, a sensor reading, a satellite
observation and a model inference are different types and are never merged
into one blob of text.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:10]
    return f"{prefix}-{digest}"


class SourceKind(str, Enum):
    WEB = "web"
    DOCUMENT = "document"
    USER_FILE = "user_file"
    DATASET = "dataset"
    ORBITAL = "orbital"
    IMAGERY = "imagery"
    SENSOR = "sensor"
    MODEL = "model"


class EvidenceKind(str, Enum):
    DOCUMENT = "document"
    OBSERVATION = "observation"
    MEASUREMENT = "measurement"
    IMAGE = "image"
    DATASET = "dataset"
    TIME_SERIES = "time_series"
    CALCULATION = "calculation"


class ClaimOrigin(str, Enum):
    EXTRACTED = "extracted"  # stated in a source
    OBSERVED = "observed"  # measured by a sensor or orbital computation
    INFERRED = "inferred"  # produced by a model; weakest standing
    USER = "user"  # asserted by the user; to be checked
    # Produced by Discovery Intelligence (V2). A hypothesis is never evidence:
    # the verifier refuses to mark it verified, whatever supports it.
    HYPOTHESIS = "hypothesis"


class ClaimType(str, Enum):
    """What kind of statement a claim is. Each type has its own sufficiency policy."""

    ATTRIBUTION = "attribution"  # "X said / reported Y": true if X said it
    QUANTITATIVE = "quantitative"  # carries a number
    TREND = "trend"  # direction of change
    PHYSICAL = "physical"  # about the physical world, measurable directly
    GENERAL = "general"


class ClaimStatus(str, Enum):
    UNVERIFIED = "unverified"
    SUPPORTED = "supported"
    PARTIALLY_VERIFIED = "partially_verified"
    VERIFIED = "verified"
    CONTESTED = "contested"
    INSUFFICIENT = "insufficient_evidence"


@dataclass
class Location:
    lat: float | None = None
    lon: float | None = None
    name: str | None = None


@dataclass
class Scope:
    """Where and when a claim applies. Evidence from another time or place is
    not automatically about the same fact."""

    valid_from: str | None = None  # ISO date
    valid_to: str | None = None  # ISO date
    geography: str | None = None
    lat: float | None = None
    lon: float | None = None

    def overlaps_time(self, other: "Scope") -> bool | None:
        """True/False when both have periods, None when unknown."""
        if not (self.valid_from and other.valid_from):
            return None
        a0, a1 = self.valid_from, self.valid_to or self.valid_from
        b0, b1 = other.valid_from, other.valid_to or other.valid_from
        return a0 <= b1 and b0 <= a1

    def same_place(self, other: "Scope") -> bool | None:
        if self.geography and other.geography:
            return self.geography.lower() == other.geography.lower()
        return None


@dataclass
class Source:
    kind: SourceKind
    title: str
    uri: str = ""
    publisher: str = ""
    published_at: str | None = None
    retrieved_at: str = field(default_factory=utcnow)
    license: str = "unknown"
    quality: float = 0.5  # prior reliability of this source, 0..1
    # Sources sharing an independence group are not independent confirmations
    # of each other (e.g. two articles quoting one press release). Lineage
    # analysis may merge groups after collection (see evidence/lineage.py).
    independence_group: str = ""
    # Provenance links: ids of sources this one copies, quotes or syndicates.
    derived_from: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        self.kind = SourceKind(self.kind)
        if not self.id:
            self.id = make_id("SRC", self.kind.value, self.uri or self.title)
        if not self.independence_group:
            self.independence_group = self.publisher or self.uri or self.id


@dataclass
class Evidence:
    source_id: str
    kind: EvidenceKind
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    observed_at: str | None = None
    location: Location | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    transformations: list[str] = field(default_factory=list)
    content_hash: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        self.kind = EvidenceKind(self.kind)
        if isinstance(self.location, dict):
            self.location = Location(**self.location)
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()
        if not self.id:
            self.id = make_id("EV", self.source_id, self.content[:200], self.observed_at)


_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "and", "or", "to", "is", "are", "was",
    "were", "be", "by", "for", "with", "at", "as", "it", "its", "this", "that",
    "from", "has", "have", "had", "not", "no", "than", "about", "per", "into",
}


# ---- claim identity -------------------------------------------------------
#
# A claim id names a *proposition*: what the claim says. It is not the question that gathered the claim,
# nor the evidence that currently supports or contradicts it; those are associations and observations.
#
# Version 1 (legacy): the normalized statement only. Kept so that ids in earlier receipts and saved graphs
# keep their original meaning; they are never recomputed under version 2.
#
# Version 2: the canonical JSON of
#     {"identity": "lofgren.claim-identity/2",
#      "statement": statement, lowercased, whitespace collapsed,
#      "subject": explicit subject key or null,
#      "value": structured value as a float or null,
#      "unit": structured unit or null,
#      "scope": {"valid_from", "valid_to": ISO date/time text or null,
#                "geography": lowercased, whitespace collapsed, or null,
#                "lat", "lon": float or null}}
# Only structure the claim already carries is used; nothing is parsed out of the statement text.

CLAIM_IDENTITY_VERSIONS = (1, 2)
CLAIM_IDENTITY_VERSION = 2
CLAIM_IDENTITY_SCHEMA = "lofgren.claim-identity/2"


class ClaimIdentityError(ValueError):
    """A claim's identity cannot be computed from its fields (non-finite number, malformed date, unknown version)."""


def _collapse(text: str) -> str:
    return " ".join(text.lower().split())


def _identity_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaimIdentityError(f"claim {field_name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ClaimIdentityError(f"claim {field_name} {value!r} is not finite")
    return float(value)


def _identity_time(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimIdentityError(f"scope {field_name} must be ISO date text, got {type(value).__name__}")
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ClaimIdentityError(f"scope {field_name} {value!r} is not a valid ISO date or time") from None
    return value


def claim_identity_key(claim: "Claim") -> dict[str, Any]:
    """The version 2 identity of a claim's proposition, as plain JSON data."""
    s = claim.scope
    geography = _collapse(s.geography) if s.geography else ""
    return {
        "identity": CLAIM_IDENTITY_SCHEMA,
        "statement": _collapse(claim.statement),
        "subject": claim.subject.strip() or None,
        "value": _identity_number(claim.value, "value"),
        "unit": claim.unit.strip() or None,
        "scope": {"valid_from": _identity_time(s.valid_from, "valid_from"),
                  "valid_to": _identity_time(s.valid_to, "valid_to"),
                  "geography": geography or None,
                  "lat": _identity_number(s.lat, "latitude"),
                  "lon": _identity_number(s.lon, "longitude")},
    }


def claim_id(claim: "Claim") -> str:
    """The id of a claim under its declared identity version."""
    if claim.identity_version == 1:
        return make_id("CL", claim.statement.lower().strip())
    if claim.identity_version == 2:
        key = json.dumps(claim_identity_key(claim), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False)
        return make_id("CL", key)
    raise ClaimIdentityError(f"unsupported claim identity version {claim.identity_version!r}; "
                             f"supported: {CLAIM_IDENTITY_VERSIONS}")


def topic_tokens(text: str) -> list[str]:
    """Content words, lowercased, numbers and stopwords removed."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]+", text.lower())
    return sorted({w for w in words if w not in _STOPWORDS and len(w) > 2})


@dataclass
class Claim:
    statement: str
    origin: ClaimOrigin = ClaimOrigin.EXTRACTED
    value: float | None = None
    unit: str = ""
    polarity: int = 1  # -1 when the statement is a negation
    supporting: list[str] = field(default_factory=list)  # evidence ids
    contradicting: list[str] = field(default_factory=list)  # evidence ids
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    confidence: float = 0.0
    topic: list[str] = field(default_factory=list)
    question_id: str | None = None
    # Exact subject key for computed observations (e.g. "passes:40697:33.448,-112.074").
    # Claims with a subject key only ever match claims with the same key.
    subject: str = ""
    claim_type: ClaimType = ClaimType.GENERAL
    scope: Scope = field(default_factory=Scope)
    confidence_method: str = "v1-heuristic"
    sufficiency: str = ""  # name of the evidence policy applied
    issues: list[str] = field(default_factory=list)  # raised by the skeptic pass
    calculation_id: str | None = None  # when the value comes from a calculation
    identity_version: int = CLAIM_IDENTITY_VERSION  # the rule that produced `id` (see claim_id)
    id: str = ""

    def __post_init__(self) -> None:
        self.origin = ClaimOrigin(self.origin)
        self.status = ClaimStatus(self.status)
        self.claim_type = ClaimType(self.claim_type)
        if isinstance(self.scope, dict):
            self.scope = Scope(**self.scope)
        if not self.topic:
            self.topic = topic_tokens(self.statement)
        if self.identity_version not in CLAIM_IDENTITY_VERSIONS or isinstance(self.identity_version, bool):
            raise ClaimIdentityError(f"unsupported claim identity version {self.identity_version!r}; "
                                     f"supported: {CLAIM_IDENTITY_VERSIONS}")
        if not self.id:
            self.id = claim_id(self)


@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    reason: str
    # incompatible: both cannot be true in the same scope.
    # scope_mismatch: they disagree but cover different times or places.
    kind: str = "incompatible"
    scope_note: str = ""
    resolution: str = ""  # the evidence that would settle it
    severity: float = 1.0  # 0..1, how much it matters to the objective
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            a, b = sorted([self.claim_a, self.claim_b])
            self.id = make_id("CX", a, b)


@dataclass
class Calculation:
    """A derived number with its receipt: formula, inputs, units, result."""

    name: str
    formula: str
    inputs: list[dict[str, Any]]  # {name, value, unit, evidence_id}
    result: float
    unit: str = ""
    method: str = "deterministic"
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("CALC", self.name, self.formula, self.result)


@dataclass
class Unknown:
    """A gap as executable work: what is missing and how it could be acquired."""

    description: str
    question_id: str | None = None
    capability: str = ""
    source_types: list[str] = field(default_factory=list)
    expected_gain: float = 0.5  # 0..1 share of the question's uncertainty it would resolve
    est_cost_usd: float = 0.0
    needs_approval: bool = False
    status: str = "open"
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("UNK", self.description, self.question_id, self.capability)


@dataclass
class Finding:
    """The answer to one research question, built only from typed state."""

    question_id: str
    question: str
    answer: str
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[str] = field(default_factory=list)
    unknown_ids: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    confidence: float = 0.0
    confidence_status: str = "provisional"  # provisional | calibrated
    confidence_method: str = "v1-heuristic"
    affects: list[str] = field(default_factory=list)
    next_best_evidence: str = ""
    issues: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.scope, dict):
            self.scope = Scope(**self.scope)
        if not self.id:
            self.id = make_id("F", self.question_id)


def to_dict(obj: Any) -> dict[str, Any]:
    def convert(v: Any) -> Any:
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, dict):
            return {k: convert(x) for k, x in v.items()}
        if isinstance(v, list):
            return [convert(x) for x in v]
        return v

    return convert(asdict(obj))
