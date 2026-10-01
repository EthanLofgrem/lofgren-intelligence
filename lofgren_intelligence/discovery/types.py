"""Discovery Intelligence object model.

Every object has a content-derived `id` (identical inputs give identical ids),
`created_at`, `derived_from` (parent ids) and a schema `version`. Objects
validate themselves on construction and fail closed with typed errors.

The kinds are never blurred:

    KnownFact          verified V1 evidence          (confidence kind: evidence)
    Uncertainty        V1 claim short of its policy  (confidence kind: claim)
    Hypothesis         a V2 idea                     (confidence kind: hypothesis, provisional)
    Candidate          a possible solution           (robustness, never a fact)
    Simulation         a model run                   (kind: simulated, never observed)
    OptimizationResult a solver output               (optimal only with a proof)

There is no "verified" status for any V2 object.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..evidence.types import Scope, make_id, utcnow
from .errors import (
    ImpossibleTimestamp,
    InvalidScope,
    MalformedInput,
    NegativeCost,
    NonFiniteValue,
    UnknownStatus,
)
from .expr import Expr, Relation, from_json as expr_from_json

SCHEMA_VERSION = "lofgren.discovery/1"
MAX_TEXT = 5_000
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---- enums -------------------------------------------------------------------

class ConfidenceKind(str, Enum):
    EVIDENCE = "evidence"
    CLAIM = "claim"
    HYPOTHESIS = "hypothesis"
    SIMULATION_UNCERTAINTY = "simulation_uncertainty"
    CANDIDATE_ROBUSTNESS = "candidate_robustness"
    OUTCOME = "outcome"  # empty until V5 measures real outcomes


class UncertaintyReason(str, Enum):
    POLICY_UNMET = "policy_unmet"
    SINGLE_SOURCE = "single_source"
    INSUFFICIENT = "insufficient"
    STALE = "stale"
    CONTESTED = "contested"


class GapType(str, Enum):
    KNOWLEDGE = "knowledge"
    EVIDENCE = "evidence"
    CAPABILITY = "capability"
    MARKET = "market"
    TECHNICAL = "technical"
    SCIENTIFIC = "scientific"
    OPERATIONAL = "operational"
    DATA = "data"
    MEASUREMENT = "measurement"
    INTEGRATION = "integration"
    CONSTRAINT = "constraint"


class GapBasis(str, Enum):
    OBSERVED_ABSENCE = "observed_absence"  # evidence positively shows something is missing
    SEARCH_ABSENCE = "search_absence"  # we looked and found nothing: weak, coverage-limited
    CONTRADICTION = "contradiction"
    UNKNOWN = "unknown"  # a V1 unknown


SEARCH_ABSENCE_MAX_CONFIDENCE = 0.4


class ConstraintKind(str, Enum):
    PHYSICAL = "physical"
    LOGICAL = "logical"
    ECONOMIC = "economic"
    LEGAL = "legal"
    RESOURCE = "resource"


class ConnectionStrength(str, Enum):
    OBSERVED = "observed"  # stated in evidence between two known facts
    DERIVED = "derived"  # computed deterministically from known facts
    SPECULATIVE = "speculative"  # anything else; can only feed a hypothesis


class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    CHALLENGED = "challenged"
    SURVIVES = "survives"  # survived V2 analysis; still NOT a fact
    REFUTED_BY_ANALYSIS = "refuted_by_analysis"
    REQUIRES_RESEARCH = "requires_research"


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    INFEASIBLE = "infeasible"
    DOMINATED = "dominated"
    VIABLE = "viable"
    REQUIRES_RESEARCH = "requires_research"
    SELECTED = "selected"


class Robustness(str, Enum):
    UNKNOWN = "unknown"
    FRAGILE = "fragile"
    MODERATE = "moderate"
    ROBUST = "robust"


class OptimizationStatus(str, Enum):
    OPTIMAL = "optimal"  # requires a proof
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNBOUNDED = "unbounded"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


class FindingKind(str, Enum):
    VERIFIED_FACT = "verified_fact"  # only ever a V1 KnownFact restated
    HYPOTHESIS = "hypothesis"
    SIMULATION_RESULT = "simulation_result"
    OPTIMIZATION_RESULT = "optimization_result"
    GAP = "gap"


class DiscoveryOutcome(str, Enum):
    """First-class results. Several are the correct answer, not failures."""

    CANDIDATE_SELECTED = "candidate_selected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTED = "contradicted"
    INFEASIBLE = "infeasible"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    REQUIRES_RESEARCH = "requires_research"


# ---- validation helpers ------------------------------------------------------

def enum_value(enum: type[Enum], value: Any, where: str) -> Enum:
    if isinstance(value, enum):
        return value
    try:
        return enum(value)
    except ValueError:
        raise UnknownStatus(f"{value!r} is not a valid {enum.__name__}; allowed: "
                            f"{[e.value for e in enum]}", where) from None


def finite(value: Any, where: str, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MalformedInput(f"expected a number, got {type(value).__name__}", where)
    if not math.isfinite(value):
        raise NonFiniteValue(f"{value} is not finite", where)
    return float(value)


def unit_interval(value: Any, where: str) -> float | None:
    v = finite(value, where)
    if v is not None and not 0.0 <= v <= 1.0:
        raise MalformedInput(f"{v} must be between 0 and 1", where)
    return v


def cost(value: Any, where: str) -> float:
    v = finite(value, where, allow_none=False)
    if v < 0:
        raise NegativeCost(f"cost {v} is negative", where)
    return v


def text(value: Any, where: str, required: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise MalformedInput("text is required", where)
        return ""
    if not isinstance(value, str):
        raise MalformedInput(f"expected text, got {type(value).__name__}", where)
    if len(value) > MAX_TEXT:
        raise MalformedInput(f"text longer than {MAX_TEXT} characters", where)
    return value


def timestamp(value: Any, where: str, now: datetime | None = None) -> str:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise ImpossibleTimestamp(f"{value!r} is not an ISO timestamp", where)
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ImpossibleTimestamp(f"{value!r} is not a valid date", where) from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if t.year < 1900 or (t - now).days > 1:
        raise ImpossibleTimestamp(f"{value} is outside the plausible range", where)
    return value


def scope(value: Any, where: str) -> Scope:
    s = value if isinstance(value, Scope) else Scope(**value) if isinstance(value, dict) else None
    if s is None:
        raise InvalidScope("scope must be a Scope or a mapping", where)
    for name in ("valid_from", "valid_to"):
        v = getattr(s, name)
        if v is not None and not _ISO_DATE.match(str(v)):
            raise InvalidScope(f"{name}={v!r} is not an ISO date", where)
    if s.valid_from and s.valid_to and s.valid_from > s.valid_to:
        raise InvalidScope(f"period starts after it ends ({s.valid_from} > {s.valid_to})", where)
    if s.lat is not None and not -90 <= s.lat <= 90:
        raise InvalidScope(f"latitude {s.lat} out of range", where)
    if s.lon is not None and not -180 <= s.lon <= 180:
        raise InvalidScope(f"longitude {s.lon} out of range", where)
    return s


def ids(value: Any, where: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) and x for x in value):
        raise MalformedInput("expected a list of non-empty ids", where)
    return list(value)


# ---- base --------------------------------------------------------------------

@dataclass
class _Obj:
    """Shared metadata. Subclasses define `_id_prefix` and `_id_fields`."""

    _id_prefix = "OBJ"
    _id_fields = ()  # names of the fields that define this object's identity

    def _base_init(self) -> None:
        name = type(self).__name__
        self.derived_from = ids(self.derived_from, f"{name}.derived_from")
        self.created_at = timestamp(self.created_at, f"{name}.created_at")
        if not self.id:
            self.id = make_id(self._id_prefix, *(repr(getattr(self, f)) for f in self._id_fields))

    def to_dict(self) -> dict:
        def conv(v: Any) -> Any:
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, Expr):
                return v.to_json()
            if isinstance(v, Relation):
                return v.to_json()
            if isinstance(v, Scope):
                return asdict(v)
            if is_dataclass(v) and not isinstance(v, type):
                return conv(asdict(v))
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [conv(x) for x in v]
            return v

        return {f.name: conv(getattr(self, f.name)) for f in fields(self)}


# ---- objects -----------------------------------------------------------------

@dataclass
class DiscoveryObjective(_Obj):
    objective: str
    research_id: str  # the V1 receipt this discovery rests on
    mode: str = "investigate"
    config: dict = field(default_factory=dict)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "DOBJ"
    _id_fields = ("objective", "research_id", "mode")

    def __post_init__(self) -> None:
        self.objective = text(self.objective, "DiscoveryObjective.objective")
        self.research_id = text(self.research_id, "DiscoveryObjective.research_id")
        if not isinstance(self.config, dict):
            raise MalformedInput("config must be a mapping", "DiscoveryObjective.config")
        self._base_init()


@dataclass
class KnownFact(_Obj):
    claim_id: str
    statement: str
    scope: Scope = field(default_factory=Scope)
    value: float | None = None
    unit: str = ""
    policy: str = ""
    confidence: float | None = None
    confidence_kind: ConfidenceKind = ConfidenceKind.EVIDENCE
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "KF"
    _id_fields = ("claim_id",)

    def __post_init__(self) -> None:
        self.claim_id = text(self.claim_id, "KnownFact.claim_id")
        self.statement = text(self.statement, "KnownFact.statement")
        self.scope = scope(self.scope, "KnownFact.scope")
        self.value = finite(self.value, "KnownFact.value")
        self.confidence = unit_interval(self.confidence, "KnownFact.confidence")
        self.confidence_kind = enum_value(ConfidenceKind, self.confidence_kind, "KnownFact.confidence_kind")
        if self.confidence_kind != ConfidenceKind.EVIDENCE:
            raise MalformedInput("a KnownFact carries evidence confidence only", "KnownFact.confidence_kind")
        self._base_init()


@dataclass
class Uncertainty(_Obj):
    claim_id: str
    statement: str
    reason: UncertaintyReason
    scope: Scope = field(default_factory=Scope)
    confidence: float | None = None
    confidence_kind: ConfidenceKind = ConfidenceKind.CLAIM
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "UNC"
    _id_fields = ("claim_id", "reason")

    def __post_init__(self) -> None:
        self.claim_id = text(self.claim_id, "Uncertainty.claim_id")
        self.statement = text(self.statement, "Uncertainty.statement")
        self.reason = enum_value(UncertaintyReason, self.reason, "Uncertainty.reason")
        self.scope = scope(self.scope, "Uncertainty.scope")
        self.confidence = unit_interval(self.confidence, "Uncertainty.confidence")
        self.confidence_kind = enum_value(ConfidenceKind, self.confidence_kind, "Uncertainty.confidence_kind")
        self._base_init()


@dataclass
class MissingEvidence(_Obj):
    unknown_id: str
    description: str
    capability: str = ""
    sources: list[str] = field(default_factory=list)
    expected_gain: float = 0.0
    est_cost_usd: float = 0.0
    needs_approval: bool = False
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "MISS"
    _id_fields = ("unknown_id",)

    def __post_init__(self) -> None:
        self.unknown_id = text(self.unknown_id, "MissingEvidence.unknown_id")
        self.description = text(self.description, "MissingEvidence.description")
        self.expected_gain = unit_interval(self.expected_gain, "MissingEvidence.expected_gain")
        self.est_cost_usd = cost(self.est_cost_usd, "MissingEvidence.est_cost_usd")
        self._base_init()


@dataclass
class ProblemFrame(_Obj):
    objective_id: str
    known_ids: list[str] = field(default_factory=list)
    uncertain_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    success_metrics: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "FRAME"
    _id_fields = ("objective_id", "known_ids", "uncertain_ids", "contradiction_ids", "missing_ids")

    def __post_init__(self) -> None:
        self.objective_id = text(self.objective_id, "ProblemFrame.objective_id")
        for name in ("known_ids", "uncertain_ids", "contradiction_ids", "missing_ids"):
            setattr(self, name, ids(getattr(self, name), f"ProblemFrame.{name}"))
        self.scope = scope(self.scope, "ProblemFrame.scope")
        self._base_init()


@dataclass
class PriorArt(_Obj):
    """One prior-art search and what it covered. Absence of results is not novelty."""

    query: str
    sources_searched: list[str]
    results: list[dict] = field(default_factory=list)  # {title, uri, date, summary, outcome}
    time_range: tuple[str | None, str | None] = (None, None)
    domains: list[str] = field(default_factory=list)
    coverage: str = ""
    limitations: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "PA"
    _id_fields = ("query", "sources_searched", "time_range", "domains")

    def __post_init__(self) -> None:
        self.query = text(self.query, "PriorArt.query")
        if not self.sources_searched:
            raise MalformedInput("a prior-art record must name the sources it searched", "PriorArt.sources_searched")
        self.cost_usd = cost(self.cost_usd, "PriorArt.cost_usd")
        if not isinstance(self.results, list) or not all(isinstance(r, dict) for r in self.results):
            raise MalformedInput("results must be a list of objects", "PriorArt.results")
        if not self.results and not self.limitations:
            raise MalformedInput("an empty search must state its coverage limitations", "PriorArt.limitations")
        self.time_range = tuple(self.time_range)  # type: ignore[assignment]
        self._base_init()

    @property
    def found(self) -> bool:
        return bool(self.results)


@dataclass
class Gap(_Obj):
    type: GapType
    missing: str
    why_it_matters: str
    basis: GapBasis
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.5
    ways_to_close: list[str] = field(default_factory=list)
    information_gain: float = 0.0
    est_cost_usd: float = 0.0
    needs_authorization: bool = False
    coverage_statement: str = ""
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "GAP"
    _id_fields = ("type", "missing", "basis")

    def __post_init__(self) -> None:
        self.type = enum_value(GapType, self.type, "Gap.type")
        self.basis = enum_value(GapBasis, self.basis, "Gap.basis")
        self.missing = text(self.missing, "Gap.missing")
        self.why_it_matters = text(self.why_it_matters, "Gap.why_it_matters")
        self.evidence_ids = ids(self.evidence_ids, "Gap.evidence_ids")
        self.confidence = unit_interval(self.confidence, "Gap.confidence")
        self.information_gain = unit_interval(self.information_gain, "Gap.information_gain")
        self.est_cost_usd = cost(self.est_cost_usd, "Gap.est_cost_usd")
        if self.basis == GapBasis.SEARCH_ABSENCE:
            # Not finding something is weak evidence that it does not exist.
            if not self.coverage_statement:
                raise MalformedInput("a search-absence gap must state what was searched", "Gap.coverage_statement")
            self.confidence = min(self.confidence, SEARCH_ABSENCE_MAX_CONFIDENCE)
        self._base_init()


@dataclass
class Assumption(_Obj):
    statement: str
    name: str = ""  # variable it sets, if any
    value: float | None = None
    unit: str = ""
    why_assumed: str = ""
    replaceable: bool = True
    sensitivity: float | None = None  # filled by sensitivity analysis
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "ASM"
    _id_fields = ("statement", "name", "value", "unit")

    def __post_init__(self) -> None:
        self.statement = text(self.statement, "Assumption.statement")
        self.value = finite(self.value, "Assumption.value")
        self.sensitivity = finite(self.sensitivity, "Assumption.sensitivity")
        if not self.why_assumed:
            raise MalformedInput("every assumption must say why it is assumed", "Assumption.why_assumed")
        self._base_init()


@dataclass
class Constraint(_Obj):
    """A structured constraint. Its source is a V1 known fact or an explicit assumption."""

    name: str
    kind: ConstraintKind
    relation: Relation
    source_fact_id: str | None = None
    source_assumption_id: str | None = None
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "CON"
    _id_fields = ("name", "kind", "relation")

    def __post_init__(self) -> None:
        self.name = text(self.name, "Constraint.name")
        self.kind = enum_value(ConstraintKind, self.kind, "Constraint.kind")
        if isinstance(self.relation, dict):
            self.relation = Relation.from_json(self.relation)
        if not isinstance(self.relation, Relation):
            raise MalformedInput("relation must be a structured Relation", "Constraint.relation")
        if not (self.source_fact_id or self.source_assumption_id):
            raise MalformedInput("a constraint must come from a known fact or a named assumption",
                                 "Constraint.source")
        self._base_init()


@dataclass
class Connection(_Obj):
    a: str
    b: str
    relation: str
    strength: ConnectionStrength
    evidence_ids: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "CONN"
    _id_fields = ("a", "b", "relation", "strength")

    def __post_init__(self) -> None:
        self.a, self.b = text(self.a, "Connection.a"), text(self.b, "Connection.b")
        self.relation = text(self.relation, "Connection.relation")
        self.strength = enum_value(ConnectionStrength, self.strength, "Connection.strength")
        self.evidence_ids = ids(self.evidence_ids, "Connection.evidence_ids")
        if self.strength == ConnectionStrength.OBSERVED and not self.evidence_ids:
            raise MalformedInput("an observed connection must cite evidence", "Connection.evidence_ids")
        self._base_init()


@dataclass
class EvidenceRequirement(_Obj):
    """What V1 would have to establish to test a hypothesis. V2 emits these; only V1 can satisfy them."""

    description: str
    capability: str = "text"
    place: str | None = None
    period: tuple[str | None, str | None] = (None, None)
    threshold: dict | None = None  # serialized Relation when the test is quantitative
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "REQ"
    _id_fields = ("description", "capability", "place", "period")

    def __post_init__(self) -> None:
        self.description = text(self.description, "EvidenceRequirement.description")
        if self.threshold is not None:
            Relation.from_json(self.threshold)  # validates
        self.period = tuple(self.period)  # type: ignore[assignment]
        self._base_init()


@dataclass
class Hypothesis(_Obj):
    """A V2 idea. It can survive analysis; it can never be verified by V2."""

    statement: str
    originating: list[str] = field(default_factory=list)  # V1 unknowns/contradictions/claims or V2 gaps
    assumptions: list[str] = field(default_factory=list)  # Assumption ids (free text accepted for legacy use)
    test: str = ""
    origin: str = "engine"  # engine strategy name, or provider:<name>
    supporting_claim_ids: list[str] = field(default_factory=list)
    contradicting_claim_ids: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    mechanism: str = ""
    predicted_observations: list[str] = field(default_factory=list)  # EvidenceRequirement ids
    falsification_criteria: list[str] = field(default_factory=list)  # EvidenceRequirement ids
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    provisional_score: float | None = None
    confidence_kind: ConfidenceKind = ConfidenceKind.HYPOTHESIS
    parent_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "HYP"
    _id_fields = ("statement",)

    def __post_init__(self) -> None:
        self.statement = text(self.statement, "Hypothesis.statement")
        self.status = enum_value(HypothesisStatus, self.status, "Hypothesis.status")
        self.scope = scope(self.scope, "Hypothesis.scope")
        self.provisional_score = unit_interval(self.provisional_score, "Hypothesis.provisional_score")
        self.confidence_kind = enum_value(ConfidenceKind, self.confidence_kind, "Hypothesis.confidence_kind")
        if self.confidence_kind != ConfidenceKind.HYPOTHESIS:
            raise MalformedInput("hypothesis confidence is always of kind 'hypothesis'", "Hypothesis.confidence_kind")
        for name in ("originating", "supporting_claim_ids", "contradicting_claim_ids", "parent_ids", "candidate_ids",
                     "predicted_observations", "falsification_criteria"):
            setattr(self, name, ids(getattr(self, name), f"Hypothesis.{name}"))
        self._base_init()

    def as_claim(self):
        """The only form in which a hypothesis may enter a V1 graph: origin=hypothesis, never verified."""
        from ..evidence.types import Claim, ClaimOrigin

        return Claim(self.statement, origin=ClaimOrigin.HYPOTHESIS, scope=self.scope)


@dataclass
class CounterHypothesis(Hypothesis):
    counters: str = ""
    _id_prefix = "CHYP"
    _id_fields = ("statement", "counters")

    def __post_init__(self) -> None:
        self.counters = text(self.counters, "CounterHypothesis.counters")
        super().__post_init__()


@dataclass
class Candidate(_Obj):
    """A possible solution. Novelty, feasibility, value and robustness are separate measures."""

    description: str
    originating_gap: str
    problem: str = ""
    hypothesis_ids: list[str] = field(default_factory=list)
    prior_art: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    required_conditions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    constraints_satisfied: list[str] = field(default_factory=list)
    constraints_violated: list[str] = field(default_factory=list)
    evidence_support: list[str] = field(default_factory=list)  # V1 claim ids
    evidence_against: list[str] = field(default_factory=list)  # V1 claim ids
    novelty: float | None = None
    technical_feasibility: float | None = None
    economic_feasibility: float | None = None
    expected_value: float | None = None
    robustness: Robustness = Robustness.UNKNOWN
    legal_constraints: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    costs: dict = field(default_factory=dict)  # name -> {value, unit}
    risks: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    test_requirements: list[str] = field(default_factory=list)
    reversible: bool | None = None
    expected_outcome: str = ""
    uncertainty: str = ""
    estimated_cost_usd: float | None = None
    simulation_results: dict = field(default_factory=dict)
    sensitivity: dict = field(default_factory=dict)
    failure_modes: list[str] = field(default_factory=list)
    next_experiment: str = ""
    status: CandidateStatus = CandidateStatus.PROPOSED
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "CAND"
    _id_fields = ("description",)

    def __post_init__(self) -> None:
        self.description = text(self.description, "Candidate.description")
        self.originating_gap = text(self.originating_gap, "Candidate.originating_gap")
        for name in ("novelty", "technical_feasibility", "economic_feasibility"):
            setattr(self, name, unit_interval(getattr(self, name), f"Candidate.{name}"))
        self.expected_value = finite(self.expected_value, "Candidate.expected_value")
        if self.estimated_cost_usd is not None:
            self.estimated_cost_usd = cost(self.estimated_cost_usd, "Candidate.estimated_cost_usd")
        self.robustness = enum_value(Robustness, self.robustness, "Candidate.robustness")
        self.status = enum_value(CandidateStatus, self.status, "Candidate.status")
        overlap = set(self.constraints_satisfied) & set(self.constraints_violated)
        if overlap:
            raise MalformedInput(f"constraints both satisfied and violated: {sorted(overlap)}", "Candidate.constraints")
        if self.status == CandidateStatus.SELECTED and self.constraints_violated:
            raise MalformedInput("a candidate that violates constraints cannot be selected", "Candidate.status")
        self._base_init()


@dataclass
class Scenario(_Obj):
    candidate_id: str
    parameters: dict  # name -> {value, unit}
    assumption_ids: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "SCN"
    _id_fields = ("candidate_id", "parameters")

    def __post_init__(self) -> None:
        self.candidate_id = text(self.candidate_id, "Scenario.candidate_id")
        if not isinstance(self.parameters, dict):
            raise MalformedInput("parameters must be a mapping", "Scenario.parameters")
        for k, v in self.parameters.items():
            val = v.get("value") if isinstance(v, dict) else v
            finite(val, f"Scenario.parameters.{k}", allow_none=False)
        self._base_init()


@dataclass
class Simulation(_Obj):
    """A model run. Its outputs are predictions, never observations."""

    candidate_id: str
    model: str
    model_version: str
    parameters: dict
    initial_conditions: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    seed: int | None = None
    iterations: int = 1
    outcomes: dict = field(default_factory=dict)  # metric -> {mean, p5, p50, p95, unit}
    uncertainty: dict = field(default_factory=dict)
    failure_states: dict = field(default_factory=dict)  # constraint id -> share of draws violating it
    runtime_s: float = 0.0
    cost_usd: float = 0.0
    kind: str = "simulated"
    confidence_kind: ConfidenceKind = ConfidenceKind.SIMULATION_UNCERTAINTY
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "SIM"
    _id_fields = ("candidate_id", "model", "model_version", "parameters", "seed", "iterations")

    def __post_init__(self) -> None:
        if self.kind != "simulated":
            raise MalformedInput("a simulation's kind is always 'simulated'", "Simulation.kind")
        self.candidate_id = text(self.candidate_id, "Simulation.candidate_id")
        self.model = text(self.model, "Simulation.model")
        if not isinstance(self.iterations, int) or self.iterations < 1:
            raise MalformedInput("iterations must be a positive integer", "Simulation.iterations")
        if self.iterations > 1 and self.seed is None:
            raise MalformedInput("a stochastic simulation must record its seed", "Simulation.seed")
        self.runtime_s = cost(self.runtime_s, "Simulation.runtime_s")
        self.cost_usd = cost(self.cost_usd, "Simulation.cost_usd")
        self.confidence_kind = enum_value(ConfidenceKind, self.confidence_kind, "Simulation.confidence_kind")
        self._base_init()


@dataclass
class SensitivityResult(_Obj):
    candidate_id: str
    elasticities: dict = field(default_factory=dict)  # variable -> elasticity
    high_sensitivity: list[str] = field(default_factory=list)
    low_sensitivity: list[str] = field(default_factory=list)
    fragile_assumptions: list[str] = field(default_factory=list)
    break_even: dict = field(default_factory=dict)  # variable -> {value, unit}
    failure_thresholds: dict = field(default_factory=dict)
    robust_ranges: dict = field(default_factory=dict)  # variable -> [low, high]
    robustness: Robustness = Robustness.UNKNOWN
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "SENS"
    _id_fields = ("candidate_id", "elasticities")

    def __post_init__(self) -> None:
        self.candidate_id = text(self.candidate_id, "SensitivityResult.candidate_id")
        for k, v in self.elasticities.items():
            finite(v, f"SensitivityResult.elasticities.{k}", allow_none=False)
        self.robustness = enum_value(Robustness, self.robustness, "SensitivityResult.robustness")
        self._base_init()


@dataclass
class DecisionVariable:
    name: str
    unit: str = ""
    lower: float | None = None
    upper: float | None = None
    integer: bool = False

    def __post_init__(self) -> None:
        from .expr import Var

        Var(self.name)  # name safety
        self.lower = finite(self.lower, f"DecisionVariable.{self.name}.lower")
        self.upper = finite(self.upper, f"DecisionVariable.{self.name}.upper")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise MalformedInput(f"lower bound {self.lower} exceeds upper bound {self.upper}",
                                 f"DecisionVariable.{self.name}")


@dataclass
class OptimizationProblem(_Obj):
    variables: list[DecisionVariable]
    objective: Expr
    direction: str  # minimize | maximize
    constraints: list[Relation] = field(default_factory=list)
    solver: str = "auto"
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "OPT"
    _id_fields = ("variables", "objective_json", "direction", "constraints_json", "solver")

    @property
    def objective_json(self) -> dict:
        return self.objective.to_json()

    @property
    def constraints_json(self) -> list:
        return [c.to_json() for c in self.constraints]

    def __post_init__(self) -> None:
        if not self.variables:
            raise MalformedInput("an optimization problem needs decision variables", "OptimizationProblem.variables")
        self.variables = [v if isinstance(v, DecisionVariable) else DecisionVariable(**v) for v in self.variables]
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            from .errors import DuplicateId

            raise DuplicateId("decision variable names repeat", "OptimizationProblem.variables")
        if isinstance(self.objective, dict):
            self.objective = expr_from_json(self.objective)
        if not isinstance(self.objective, Expr):
            raise MalformedInput("objective must be a structured expression", "OptimizationProblem.objective")
        self.constraints = [c if isinstance(c, Relation) else Relation.from_json(c) for c in self.constraints]
        if self.direction not in ("minimize", "maximize"):
            raise MalformedInput("direction must be 'minimize' or 'maximize'", "OptimizationProblem.direction")
        unknown = (self.objective.variables() | {v for c in self.constraints for v in c.variables()}) - set(names)
        if unknown:
            from .errors import UnknownReference

            raise UnknownReference(f"expressions use undeclared variables {sorted(unknown)}", "OptimizationProblem")
        self._base_init()


@dataclass
class OptimizationResult(_Obj):
    problem_id: str
    status: OptimizationStatus
    solution: dict = field(default_factory=dict)  # variable -> value
    objective_value: float | None = None
    proof: str = ""  # what establishes optimality (required for OPTIMAL)
    verified: bool = False  # independent re-check of every constraint passed
    violations: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "OPTR"
    _id_fields = ("problem_id", "status", "solution")

    def __post_init__(self) -> None:
        self.problem_id = text(self.problem_id, "OptimizationResult.problem_id")
        self.status = enum_value(OptimizationStatus, self.status, "OptimizationResult.status")
        self.objective_value = finite(self.objective_value, "OptimizationResult.objective_value")
        for k, v in self.solution.items():
            finite(v, f"OptimizationResult.solution.{k}", allow_none=False)
        if self.status == OptimizationStatus.OPTIMAL and not self.proof:
            raise MalformedInput("'optimal' requires a proof of optimality", "OptimizationResult.proof")
        if self.status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE):
            if not self.solution:
                raise MalformedInput("a feasible result must include its solution", "OptimizationResult.solution")
            if self.violations:
                raise MalformedInput("a result with constraint violations cannot be feasible or optimal",
                                     "OptimizationResult.status")
        self._base_init()


@dataclass
class DiscoveryFinding(_Obj):
    statement: str
    kind: FindingKind
    rests_on: list[str]  # ids of the objects this finding is built from
    confidence_kind: ConfidenceKind = ConfidenceKind.HYPOTHESIS
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "DF"
    _id_fields = ("statement", "kind", "rests_on")

    _KIND_CONFIDENCE = {
        FindingKind.VERIFIED_FACT: ConfidenceKind.EVIDENCE,
        FindingKind.HYPOTHESIS: ConfidenceKind.HYPOTHESIS,
        FindingKind.SIMULATION_RESULT: ConfidenceKind.SIMULATION_UNCERTAINTY,
        FindingKind.OPTIMIZATION_RESULT: ConfidenceKind.CANDIDATE_ROBUSTNESS,
        FindingKind.GAP: ConfidenceKind.CLAIM,
    }
    _FORBIDDEN_FOR_SIMULATION = re.compile(r"\b(observed|measured|verified|confirmed|proven)\b", re.I)

    def __post_init__(self) -> None:
        self.statement = text(self.statement, "DiscoveryFinding.statement")
        self.kind = enum_value(FindingKind, self.kind, "DiscoveryFinding.kind")
        self.rests_on = ids(self.rests_on, "DiscoveryFinding.rests_on")
        if not self.rests_on:
            raise MalformedInput("every finding must trace to structured inputs", "DiscoveryFinding.rests_on")
        self.confidence_kind = self._KIND_CONFIDENCE[self.kind]
        if self.kind == FindingKind.SIMULATION_RESULT and self._FORBIDDEN_FOR_SIMULATION.search(self.statement):
            raise MalformedInput("a simulation result cannot be described as observed or verified",
                                 "DiscoveryFinding.statement")
        if self.kind == FindingKind.VERIFIED_FACT and not all(r.startswith("KF-") for r in self.rests_on):
            raise MalformedInput("a verified-fact finding may rest only on V1 known facts", "DiscoveryFinding.rests_on")
        self._base_init()


@dataclass
class DiscoveryDecision(_Obj):
    rule: str  # the explicit, recorded decision rule
    selected_candidate_id: str | None
    alternatives: dict = field(default_factory=dict)  # candidate id -> reason not selected
    outcome: DiscoveryOutcome = DiscoveryOutcome.UNKNOWN
    tie_break: str = ""
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "DEC"
    _id_fields = ("rule", "selected_candidate_id", "outcome")

    def __post_init__(self) -> None:
        self.rule = text(self.rule, "DiscoveryDecision.rule")
        self.outcome = enum_value(DiscoveryOutcome, self.outcome, "DiscoveryDecision.outcome")
        if (self.outcome == DiscoveryOutcome.CANDIDATE_SELECTED) != (self.selected_candidate_id is not None):
            raise MalformedInput("a selected candidate exists exactly when the outcome is candidate_selected",
                                 "DiscoveryDecision.outcome")
        self._base_init()


ALL_TYPES = {
    "discovery_objective": DiscoveryObjective, "known_fact": KnownFact, "uncertainty": Uncertainty,
    "missing_evidence": MissingEvidence, "problem_frame": ProblemFrame, "prior_art": PriorArt, "gap": Gap,
    "assumption": Assumption, "constraint": Constraint, "connection": Connection,
    "evidence_requirement": EvidenceRequirement, "hypothesis": Hypothesis, "counter_hypothesis": CounterHypothesis,
    "candidate": Candidate, "scenario": Scenario, "simulation": Simulation, "sensitivity_result": SensitivityResult,
    "optimization_problem": OptimizationProblem, "optimization_result": OptimizationResult,
    "discovery_finding": DiscoveryFinding, "discovery_decision": DiscoveryDecision,
}


def from_dict(type_name: str, data: dict) -> _Obj:
    """Rebuild a V2 object from its dict form, re-running all validation. Unknown keys fail closed."""
    if type_name not in ALL_TYPES:
        raise MalformedInput(f"unknown discovery type '{type_name}'")
    cls = ALL_TYPES[type_name]
    if not isinstance(data, dict):
        raise MalformedInput("expected an object", type_name)
    allowed = {f.name for f in fields(cls)}
    extra = set(data) - allowed
    if extra:
        raise MalformedInput(f"unexpected fields {sorted(extra)}", type_name)
    data = dict(data)
    if cls is OptimizationProblem:
        data["variables"] = [DecisionVariable(**v) if isinstance(v, dict) else v for v in data.get("variables", [])]
    for key in ("time_range", "period"):
        if key in data and isinstance(data[key], list):
            data[key] = tuple(data[key])
    return cls(**data)
