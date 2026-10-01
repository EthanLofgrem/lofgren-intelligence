"""Typed evidence.

The evidence graph, not the language model, is the source of truth. Every
item keeps its provenance: source + time + place + license + confidence +
transformation history. A newspaper claim, a sensor reading, a satellite
observation and a model inference are different types and are never merged
into one blob of text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    # of each other (e.g. two articles quoting one press release).
    independence_group: str = ""
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
    id: str = ""

    def __post_init__(self) -> None:
        self.kind = EvidenceKind(self.kind)
        if isinstance(self.location, dict):
            self.location = Location(**self.location)
        if not self.id:
            self.id = make_id("EV", self.source_id, self.content[:200], self.observed_at)


_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "and", "or", "to", "is", "are", "was",
    "were", "be", "by", "for", "with", "at", "as", "it", "its", "this", "that",
    "from", "has", "have", "had", "not", "no", "than", "about", "per", "into",
}


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
    id: str = ""

    def __post_init__(self) -> None:
        self.origin = ClaimOrigin(self.origin)
        self.status = ClaimStatus(self.status)
        if not self.topic:
            self.topic = topic_tokens(self.statement)
        if not self.id:
            self.id = make_id("CL", self.statement.lower().strip())


@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    reason: str
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            a, b = sorted([self.claim_a, self.claim_b])
            self.id = make_id("CX", a, b)


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
