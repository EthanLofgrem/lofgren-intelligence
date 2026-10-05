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

import json
import math
import re
import warnings
import weakref
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, ClassVar

from ..evidence.types import Scope, make_id, utcnow
from .errors import (
    DependencyCycle,
    DuplicateId,
    FalseNovelty,
    ImpossibleTimestamp,
    InputTooLarge,
    InvalidScope,
    InvalidTransition,
    MalformedInput,
    NegativeCost,
    NonFiniteValue,
    PromotionRefused,
    UnknownReference,
    UnknownStatus,
)
from .expr import Expr, Relation, Unit, Var, from_json as expr_from_json

SCHEMA_VERSION = "lofgren.discovery/1"
MAX_TEXT = 5_000
MAX_ITEMS = 1_000
MAX_JSON_DEPTH = 16
MAX_ITERATIONS = 1_000_000

# Id prefixes. V1 ids name evidence-graph objects; V2 ids name discovery objects.
# Fields that must hold evidence refuse V2 idea ids as an attempted promotion.
V2_IDEA_PREFIXES = frozenset({"HYP", "CHYP", "CAND", "SIM", "SCN", "SENS", "OPT", "OPTR", "CONN", "GAP", "ASM", "REQ",
                              "DF", "DEC", "FRAME", "DOBJ", "PA"})
MISSING_EVIDENCE_PREFIXES = frozenset({"UNK", "MISS"})
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


class PriorArtConclusion(str, Enum):
    """The only three things a prior-art assessment may conclude. None of them is "novel"."""

    MATCH_FOUND = "match_found"
    NO_MATCH_WITHIN_COVERAGE = "no_match_within_coverage"  # says nothing about novelty
    INCOMPLETE = "incomplete"  # part of the requested coverage was not searched


# Explicit novelty predicates. Applied only to text V2 itself asserts about a subject (the subject and its
# distinctive features), never to evidence such as titles, provider limitations, place or domain names.
# Bare "new" and "first" are not judged: "New Mexico", "first-in first-out" and "the first 100 results"
# are not claims. The structural guarantees carry the rule: no conclusion value means novel, the no-match
# statement always disclaims novelty, and distinctiveness can only be asserted against actual matches.
NOVELTY_CLAIMS = re.compile(
    r"\b(novel|unprecedented|never[\s-]+(?:been[\s-]+)?(?:attempted|done|tried|seen)|never[\s-]+before|"
    r"first[\s-]+of[\s-]+its[\s-]+kind|first[\s-]+ever|(?:world|industry|market)(?:'|’)?s[\s-]+first|"
    r"brand[\s-]+new|nobody[\s-]+has|no[\s-]+one[\s-]+has)\b", re.IGNORECASE)


def check_no_novelty_claim(value: str, where: str) -> str:
    """Refuse a V2-authored assertion of novelty. Only for text V2 asserts, never for evidence text."""
    m = NOVELTY_CLAIMS.search(value)
    if m:
        raise FalseNovelty(f"{m.group(0)!r} asserts novelty; a prior-art search can only state its coverage", where)
    return value


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


HYPOTHESIS_TRANSITIONS = {
    HypothesisStatus.PROPOSED: frozenset({HypothesisStatus.CHALLENGED, HypothesisStatus.SURVIVES,
                                          HypothesisStatus.REFUTED_BY_ANALYSIS, HypothesisStatus.REQUIRES_RESEARCH}),
    # The discovery verifier only lowers standing.
    HypothesisStatus.SURVIVES: frozenset({HypothesisStatus.CHALLENGED, HypothesisStatus.REFUTED_BY_ANALYSIS,
                                          HypothesisStatus.REQUIRES_RESEARCH}),
    HypothesisStatus.CHALLENGED: frozenset({HypothesisStatus.REFUTED_BY_ANALYSIS, HypothesisStatus.REQUIRES_RESEARCH}),
    # Settled only by a new V1 run verifying a separate claim, never by changing this status.
    HypothesisStatus.REQUIRES_RESEARCH: frozenset(),
    HypothesisStatus.REFUTED_BY_ANALYSIS: frozenset(),
}

CANDIDATE_TRANSITIONS = {
    CandidateStatus.PROPOSED: frozenset({CandidateStatus.VIABLE, CandidateStatus.INFEASIBLE, CandidateStatus.DOMINATED,
                                         CandidateStatus.REQUIRES_RESEARCH}),
    CandidateStatus.VIABLE: frozenset({CandidateStatus.SELECTED, CandidateStatus.INFEASIBLE, CandidateStatus.DOMINATED,
                                       CandidateStatus.REQUIRES_RESEARCH}),
    CandidateStatus.SELECTED: frozenset({CandidateStatus.INFEASIBLE, CandidateStatus.DOMINATED,
                                         CandidateStatus.REQUIRES_RESEARCH}),
    CandidateStatus.REQUIRES_RESEARCH: frozenset(),
    CandidateStatus.INFEASIBLE: frozenset(),
    CandidateStatus.DOMINATED: frozenset(),
}


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


def calendar_date(value: Any, where: str) -> date | None:
    """An ISO date (or the date part of a timestamp) that exists on the calendar."""
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise InvalidScope(f"{value!r} is not an ISO date", where)
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise InvalidScope(f"{value!r} is not a real calendar date", where) from None


def scope(value: Any, where: str) -> Scope:
    if isinstance(value, Scope):
        s = value
    elif isinstance(value, dict):
        try:
            s = Scope(**value)
        except TypeError:
            raise InvalidScope(f"unexpected scope fields {sorted(map(str, value))}", where) from None
    else:
        raise InvalidScope("scope must be a Scope or a mapping", where)
    start = calendar_date(s.valid_from, f"{where}.valid_from")
    end = calendar_date(s.valid_to, f"{where}.valid_to")
    if start and end and start > end:
        raise InvalidScope(f"period starts after it ends ({s.valid_from} > {s.valid_to})", where)
    if s.geography is not None:
        text(s.geography, f"{where}.geography")
    for name, limit in (("lat", 90), ("lon", 180)):
        v = finite(getattr(s, name), f"{where}.{name}")
        if v is not None and not -limit <= v <= limit:
            raise InvalidScope(f"{'latitude' if name == 'lat' else 'longitude'} {v} out of range", where)
    return s


def period(value: Any, where: str) -> tuple[str | None, str | None]:
    """A (from, to) pair of ISO dates or None, in order."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MalformedInput("a period is a pair (from, to)", where)
    start, end = calendar_date(value[0], f"{where}[0]"), calendar_date(value[1], f"{where}[1]")
    if start and end and start > end:
        raise InvalidScope(f"period starts after it ends ({value[0]} > {value[1]})", where)
    return (value[0], value[1])


def ref(value: Any, where: str, allowed: tuple[str, ...] = (), fact: bool = False) -> str:
    """One id. With `allowed`, its prefix must be one of them (prefix only: ids may be fixtures like CL-1).

    `fact=True` marks a field that must hold evidence: a V2 idea id there is an attempted promotion,
    and a V1 unknown there would turn missing evidence into evidence.
    """
    if not isinstance(value, str) or not value:
        raise MalformedInput("expected a non-empty id", where)
    if len(value) > 128:
        raise InputTooLarge("id longer than 128 characters", where)
    if not allowed:
        return value
    prefix = value.split("-", 1)[0]
    if prefix in allowed and "-" in value:
        return value
    if fact and prefix in V2_IDEA_PREFIXES:
        raise PromotionRefused(f"{value!r} is a V2 {prefix} object; only V1 evidence can stand here", where)
    if prefix in MISSING_EVIDENCE_PREFIXES:
        raise MalformedInput(f"{value!r} records missing evidence, which is neither support nor contradiction", where)
    raise MalformedInput(f"{value!r} is not one of the expected id kinds {list(allowed)}", where)


def ids(value: Any, where: str, allowed: tuple[str, ...] = (), fact: bool = False) -> list[str]:
    """A list of distinct ids (see `ref`)."""
    if not isinstance(value, (list, tuple)):
        raise MalformedInput("expected a list of non-empty ids", where)
    if len(value) > MAX_ITEMS:
        raise InputTooLarge(f"more than {MAX_ITEMS} ids", where)
    out = [ref(x, f"{where}[{i}]", allowed, fact) for i, x in enumerate(value)]
    if len(set(out)) != len(out):
        repeated = sorted({x for x in out if out.count(x) > 1})
        raise DuplicateId(f"ids repeat: {repeated}", where)
    return out


def texts(value: Any, where: str) -> list[str]:
    """A list of distinct, non-empty strings (names, notes, source types)."""
    if not isinstance(value, (list, tuple)):
        raise MalformedInput("expected a list of text", where)
    if len(value) > MAX_ITEMS:
        raise InputTooLarge(f"more than {MAX_ITEMS} items", where)
    out = [text(x, f"{where}[{i}]") for i, x in enumerate(value)]
    if len(set(out)) != len(out):
        raise DuplicateId("entries repeat", where)
    return out


def json_value(value: Any, where: str, _depth: int = 0) -> Any:
    """Plain JSON data with finite numbers only: what may sit in a free-form mapping field."""
    if _depth > MAX_JSON_DEPTH:
        raise InputTooLarge(f"nested deeper than {MAX_JSON_DEPTH}", where)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return text(value, where, required=False) if value else value
    if isinstance(value, (int, float)):
        return finite(value, where, allow_none=False) if isinstance(value, float) else value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise InputTooLarge(f"more than {MAX_ITEMS} items", where)
        return [json_value(v, f"{where}[{i}]", _depth + 1) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return mapping(value, where, _depth)
    raise MalformedInput(f"{type(value).__name__} is not plain data", where)


def mapping(value: Any, where: str, _depth: int = 0) -> dict:
    """A free-form mapping of plain JSON data with text keys and finite numbers."""
    if not isinstance(value, dict):
        raise MalformedInput(f"expected a mapping, got {type(value).__name__}", where)
    if len(value) > MAX_ITEMS:
        raise InputTooLarge(f"more than {MAX_ITEMS} entries", where)
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise MalformedInput("mapping keys must be non-empty text", where)
        out[k] = json_value(v, f"{where}.{k}", _depth + 1)
    return out


def integer(value: Any, where: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedInput(f"expected an integer, got {type(value).__name__}", where)
    if value < minimum:
        raise MalformedInput(f"{value} is below {minimum}", where)
    if maximum is not None and value > maximum:
        raise InputTooLarge(f"{value} exceeds {maximum}", where)
    return value


def boolean(value: Any, where: str, allow_none: bool = False) -> bool | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, bool):
        raise MalformedInput(f"expected true or false, got {type(value).__name__}", where)
    return value


def unit_map(value: Any, where: str) -> dict[str, str]:
    """Variable name -> unit text, every name safe and every unit parseable."""
    out = mapping(value, where)
    for name, unit in out.items():
        Var(name)
        if not isinstance(unit, str):
            raise MalformedInput("a unit is text ('' for dimensionless)", f"{where}.{name}")
        Unit.parse(unit)
    return out


def check_relation_units(relation: Relation, units: dict[str, str], where: str) -> None:
    """Every variable has a declared unit, no unit is declared for nothing, and both sides agree."""
    names = relation.variables()
    undeclared = names - set(units)
    if undeclared:
        raise MalformedInput(f"declare units for {sorted(undeclared)}", where)
    unused = set(units) - names
    if unused:
        raise MalformedInput(f"units declared for variables the relation does not use: {sorted(unused)}", where)
    relation.check_units({k: Unit.parse(v) for k, v in units.items()})


def _plain(v: Any) -> Any:
    """JSON-ready form of a field value."""
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (Expr, Relation)):
        return v.to_json()
    if isinstance(v, Scope):
        return asdict(v)
    if is_dataclass(v) and not isinstance(v, type):
        return _plain(asdict(v))
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


# ---- base --------------------------------------------------------------------

# Objects whose construction has finished, tracked outside the instance so `obj.__dict__` holds only fields.
_SEALED: set[int] = set()
# Fixed once an object exists; to change one, build a new object.
_IMMUTABLE_FIELDS = frozenset({"id", "created_at", "version", "kind", "confidence_kind"})


@dataclass
class _Obj:
    """Shared metadata. Subclasses define `_id_prefix` and `_id_fields`."""

    _id_prefix = "OBJ"
    _id_fields = ()  # names of the fields that define this object's identity
    _transitions: ClassVar[dict] = {}  # status -> statuses it may move to; empty when there is no lifecycle

    def _base_init(self) -> None:
        name = type(self).__name__
        self.derived_from = ids(self.derived_from, f"{name}.derived_from")
        self.created_at = timestamp(self.created_at, f"{name}.created_at")
        if self.version != SCHEMA_VERSION:
            raise MalformedInput(f"unsupported schema version {self.version!r}", f"{name}.version")
        if not isinstance(self.id, str):
            raise MalformedInput("id must be text", f"{name}.id")
        expected = self.compute_id()
        if self.id and self.id != expected:
            raise MalformedInput(f"id {self.id!r} does not match the object's content (expected {expected!r})",
                                 f"{name}.id")
        self.id = expected
        if self.id in self.derived_from:
            raise DependencyCycle("an object cannot derive from itself", f"{name}.derived_from")
        _SEALED.add(id(self))
        weakref.finalize(self, _SEALED.discard, id(self))

    def compute_id(self) -> str:
        """Canonical JSON of the identity fields: independent of key order and of 2 vs 2.0."""
        content = json.dumps([_plain(getattr(self, f)) for f in self._id_fields], sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return make_id(self._id_prefix, content)

    def __setattr__(self, name: str, value: Any) -> None:
        if id(self) in _SEALED:
            if name in _IMMUTABLE_FIELDS:
                raise MalformedInput(f"'{name}' is fixed once the object exists; build a new object instead",
                                     f"{type(self).__name__}.{name}")
            if name == "status" and self._transitions:
                self.transition(value)
                return
        object.__setattr__(self, name, value)

    def transition(self, status: Any) -> "_Obj":
        """Move to a new status when the lifecycle allows it. No lifecycle has a verified state."""
        where = f"{type(self).__name__}.status"
        if not self._transitions:
            raise InvalidTransition(f"{type(self).__name__} has no lifecycle", where)
        new = enum_value(type(self.status), status, where)
        if new is not self.status and new not in self._transitions[self.status]:
            raise InvalidTransition(f"{self.status.value} -> {new.value} is not allowed", where)
        object.__setattr__(self, "status", new)
        return self

    def to_dict(self) -> dict:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}


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
        self.research_id = ref(self.research_id, "DiscoveryObjective.research_id", ("RR",))
        self.mode = text(self.mode, "DiscoveryObjective.mode")
        self.config = mapping(self.config, "DiscoveryObjective.config")
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
        self.claim_id = ref(self.claim_id, "KnownFact.claim_id", ("CL",), fact=True)
        self.statement = text(self.statement, "KnownFact.statement")
        self.scope = scope(self.scope, "KnownFact.scope")
        self.value = finite(self.value, "KnownFact.value")
        self.unit = text(self.unit, "KnownFact.unit", required=False)
        self.policy = text(self.policy, "KnownFact.policy", required=False)
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
        self.claim_id = ref(self.claim_id, "Uncertainty.claim_id", ("CL",), fact=True)
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
        self.unknown_id = ref(self.unknown_id, "MissingEvidence.unknown_id", ("UNK",))
        self.description = text(self.description, "MissingEvidence.description")
        self.capability = text(self.capability, "MissingEvidence.capability", required=False)
        self.sources = texts(self.sources, "MissingEvidence.sources")
        self.needs_approval = boolean(self.needs_approval, "MissingEvidence.needs_approval")
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
    scope_note: str = ""  # how the scope was derived from V1, including any widening
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "FRAME"
    _id_fields = ("objective_id", "known_ids", "uncertain_ids", "contradiction_ids", "missing_ids")

    def __post_init__(self) -> None:
        self.objective_id = ref(self.objective_id, "ProblemFrame.objective_id", ("DOBJ",))
        self.known_ids = ids(self.known_ids, "ProblemFrame.known_ids", ("KF", "CL"), fact=True)
        self.uncertain_ids = ids(self.uncertain_ids, "ProblemFrame.uncertain_ids", ("UNC", "CL"), fact=True)
        self.contradiction_ids = ids(self.contradiction_ids, "ProblemFrame.contradiction_ids", ("CX",))
        self.missing_ids = ids(self.missing_ids, "ProblemFrame.missing_ids", ("MISS", "UNK"))
        both = set(self.known_ids) & set(self.uncertain_ids)
        if both:
            raise MalformedInput(f"ids cannot be both known and uncertain: {sorted(both)}", "ProblemFrame")
        self.scope = scope(self.scope, "ProblemFrame.scope")
        self.success_metrics = texts(self.success_metrics, "ProblemFrame.success_metrics")
        self.scope_note = text(self.scope_note, "ProblemFrame.scope_note", required=False)
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
    # A search observation: the request plus what it returned. The same deterministic search gives the same id;
    # a materially different result set (or a failed search, recorded in limitations) gives a different one.
    # No wall-clock time: created_at stays out of identity so observations remain reproducible.
    _id_fields = ("query", "sources_searched", "time_range", "domains", "results", "limitations")

    def __post_init__(self) -> None:
        self.query = text(self.query, "PriorArt.query")
        if not self.sources_searched:
            raise MalformedInput("a prior-art record must name the sources it searched", "PriorArt.sources_searched")
        self.sources_searched = texts(self.sources_searched, "PriorArt.sources_searched")
        self.cost_usd = cost(self.cost_usd, "PriorArt.cost_usd")
        if not isinstance(self.results, list):
            raise MalformedInput("results must be a list of objects", "PriorArt.results")
        self.results = [mapping(r, f"PriorArt.results[{i}]") for i, r in enumerate(self.results)]
        self.limitations = texts(self.limitations, "PriorArt.limitations")
        if not self.results and not self.limitations:
            raise MalformedInput("an empty search must state its coverage limitations", "PriorArt.limitations")
        self.time_range = period(self.time_range, "PriorArt.time_range")
        self.domains = texts(self.domains, "PriorArt.domains")
        self.coverage = text(self.coverage, "PriorArt.coverage", required=False)
        self._base_init()

    @property
    def found(self) -> bool:
        return bool(self.results)


@dataclass
class PriorArtAssessment(_Obj):
    """What a set of prior-art searches established about one subject, and what they did not cover.

    `conclusion` is the only verdict. "No match within coverage" is a statement about the search,
    never about novelty; see `statement`.
    """

    subject: str
    conclusion: PriorArtConclusion
    queries: list[str]
    search_ids: list[str]  # the PriorArt records, one per query
    sources_searched: list[str]
    domains: list[str] = field(default_factory=list)
    time_range: tuple[str | None, str | None] = (None, None)
    matches: list[dict] = field(default_factory=list)  # validated hits: title, source, uri, published, summary
    nearest_matches: list[str] = field(default_factory=list)  # titles of the closest matches
    distinctive_features: list[str] = field(default_factory=list)  # what the subject has that matches lack
    limitations: list[str] = field(default_factory=list)
    unsearched_areas: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "PAA"
    _id_fields = ("subject", "conclusion", "queries", "search_ids", "sources_searched", "domains", "time_range",
                  "matches", "unsearched_areas")

    def __post_init__(self) -> None:
        w = "PriorArtAssessment"
        self.subject = check_no_novelty_claim(text(self.subject, f"{w}.subject"), f"{w}.subject")
        self.conclusion = enum_value(PriorArtConclusion, self.conclusion, f"{w}.conclusion")
        self.queries = texts(self.queries, f"{w}.queries")
        self.search_ids = ids(self.search_ids, f"{w}.search_ids", ("PA",))
        self.sources_searched = texts(self.sources_searched, f"{w}.sources_searched")
        if not self.queries or not self.search_ids or not self.sources_searched:
            raise MalformedInput("an assessment records its queries, searches and sources", w)
        self.domains = texts(self.domains, f"{w}.domains")
        self.time_range = period(self.time_range, f"{w}.time_range")
        if not isinstance(self.matches, list):
            raise MalformedInput("matches must be a list of hits", f"{w}.matches")
        self.matches = [prior_art_hit(m, f"{w}.matches[{i}]") for i, m in enumerate(self.matches)]
        # Titles, limitations and unsearched areas are evidence and coverage text, not V2 assertions.
        for name in ("nearest_matches", "distinctive_features", "limitations", "unsearched_areas"):
            setattr(self, name, texts(getattr(self, name), f"{w}.{name}"))
        for i, v in enumerate(self.distinctive_features):
            check_no_novelty_claim(v, f"{w}.distinctive_features[{i}]")
        c = self.conclusion
        if self.distinctive_features and c != PriorArtConclusion.MATCH_FOUND:
            raise FalseNovelty("distinctive features are differences from matched prior art; with no matches they "
                               "would assert distinctiveness against nothing", f"{w}.distinctive_features")
        if c == PriorArtConclusion.MATCH_FOUND and not self.matches:
            raise MalformedInput("match_found needs at least one match", f"{w}.matches")
        if c != PriorArtConclusion.MATCH_FOUND and self.matches:
            raise MalformedInput(f"{c.value} cannot carry matches", f"{w}.matches")
        if c == PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE and not self.limitations:
            raise MalformedInput("a search that found nothing must state its coverage limitations", f"{w}.limitations")
        if c == PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE and self.unsearched_areas:
            raise MalformedInput("unsearched areas make the search incomplete", f"{w}.conclusion")
        if c == PriorArtConclusion.INCOMPLETE and not self.unsearched_areas:
            raise MalformedInput("an incomplete search names what was not searched", f"{w}.unsearched_areas")
        self._base_init()

    @property
    def statement(self) -> str:
        """The only wording V2 uses for prior art. It never asserts novelty."""
        if self.conclusion == PriorArtConclusion.MATCH_FOUND:
            titles = "; ".join(m["title"] for m in self.matches[:5])
            more = f" and {len(self.matches) - 5} more" if len(self.matches) > 5 else ""
            return f"Matching prior art found: {titles}{more}."
        coverage = ", ".join(self.sources_searched)
        if self.domains:
            coverage += "; domains: " + ", ".join(self.domains)
        if self.time_range != (None, None):
            coverage += f"; period: {self.time_range[0] or 'any'} to {self.time_range[1] or 'any'}"
        if self.conclusion == PriorArtConclusion.NO_MATCH_WITHIN_COVERAGE:
            return (f"No matching prior art was found within the searched sources ({coverage}) for "
                    f"{self.subject}; this does not establish novelty.")
        return (f"The prior-art search for {self.subject} was incomplete (not searched: "
                f"{'; '.join(self.unsearched_areas)}); no conclusion about prior art can be drawn.")


_HIT_FIELDS = {"title", "source", "uri", "published", "summary", "matched_terms"}


def prior_art_hit(value: Any, where: str) -> dict:
    """One prior-art search result, validated. Malformed results fail closed rather than being dropped."""
    if not isinstance(value, dict):
        raise MalformedInput("a prior-art hit is an object", where)
    extra = set(value) - _HIT_FIELDS
    if extra:
        raise MalformedInput(f"unexpected hit fields {sorted(map(str, extra))}", where)
    hit = {"title": text(value.get("title"), f"{where}.title"), "source": text(value.get("source"), f"{where}.source")}
    for key in ("uri", "summary"):
        hit[key] = text(value.get(key, ""), f"{where}.{key}", required=False)
    published = value.get("published")
    calendar_date(published, f"{where}.published")
    hit["published"] = published
    terms = texts(value.get("matched_terms", []), f"{where}.matched_terms")
    hit["matched_terms"] = sorted(terms)
    return hit


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
    _id_fields = ("type", "missing", "basis", "evidence_ids")

    def __post_init__(self) -> None:
        self.type = enum_value(GapType, self.type, "Gap.type")
        self.basis = enum_value(GapBasis, self.basis, "Gap.basis")
        self.missing = text(self.missing, "Gap.missing")
        self.why_it_matters = text(self.why_it_matters, "Gap.why_it_matters")
        self.evidence_ids = ids(self.evidence_ids, "Gap.evidence_ids")
        self.confidence = unit_interval(self.confidence, "Gap.confidence")
        if self.confidence is None:
            raise MalformedInput("a gap states its confidence", "Gap.confidence")
        self.ways_to_close = texts(self.ways_to_close, "Gap.ways_to_close")
        self.needs_authorization = boolean(self.needs_authorization, "Gap.needs_authorization")
        self.coverage_statement = text(self.coverage_statement, "Gap.coverage_statement", required=False)
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
        if self.name:
            Var(self.name)  # the variable it sets must be a safe name
        self.name = text(self.name, "Assumption.name", required=False)
        self.value = finite(self.value, "Assumption.value")
        Unit.parse(self.unit)
        self.replaceable = boolean(self.replaceable, "Assumption.replaceable")
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
    variable_units: dict = field(default_factory=dict)  # variable -> unit; required for every variable
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "CON"
    _id_fields = ("name", "kind", "relation", "variable_units")

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
        if self.source_fact_id is not None:
            ref(self.source_fact_id, "Constraint.source_fact_id", ("KF", "CL"), fact=True)
        if self.source_assumption_id is not None:
            ref(self.source_assumption_id, "Constraint.source_assumption_id", ("ASM",))
        self.variable_units = unit_map(self.variable_units, "Constraint.variable_units")
        check_relation_units(self.relation, self.variable_units, "Constraint.relation")
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
        if self.a == self.b:
            raise MalformedInput("a connection joins two different objects", "Connection")
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
    variable_units: dict = field(default_factory=dict)  # required for every threshold variable
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "REQ"
    _id_fields = ("description", "capability", "place", "period", "threshold", "variable_units")

    def __post_init__(self) -> None:
        self.description = text(self.description, "EvidenceRequirement.description")
        self.capability = text(self.capability, "EvidenceRequirement.capability")
        if self.place is not None:
            self.place = text(self.place, "EvidenceRequirement.place")
        self.variable_units = unit_map(self.variable_units, "EvidenceRequirement.variable_units")
        if self.threshold is not None:
            relation = Relation.from_json(self.threshold)
            check_relation_units(relation, self.variable_units, "EvidenceRequirement.threshold")
            self.threshold = relation.to_json()
        elif self.variable_units:
            raise MalformedInput("units are declared but there is no threshold", "EvidenceRequirement.variable_units")
        self.period = period(self.period, "EvidenceRequirement.period")
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
    # The same sentence about another place or period is a different hypothesis.
    _id_fields = ("statement", "scope")
    _transitions = HYPOTHESIS_TRANSITIONS

    def __post_init__(self) -> None:
        self.statement = text(self.statement, "Hypothesis.statement")
        self.status = enum_value(HypothesisStatus, self.status, "Hypothesis.status")
        self.scope = scope(self.scope, "Hypothesis.scope")
        self.provisional_score = unit_interval(self.provisional_score, "Hypothesis.provisional_score")
        self.confidence_kind = enum_value(ConfidenceKind, self.confidence_kind, "Hypothesis.confidence_kind")
        if self.confidence_kind != ConfidenceKind.HYPOTHESIS:
            raise MalformedInput("hypothesis confidence is always of kind 'hypothesis'", "Hypothesis.confidence_kind")
        h = type(self).__name__
        self.originating = ids(self.originating, f"{h}.originating")
        self.assumptions = texts(self.assumptions, f"{h}.assumptions")  # Assumption ids, or legacy free text
        self.test = text(self.test, f"{h}.test", required=False)
        self.origin = text(self.origin, f"{h}.origin")
        self.mechanism = text(self.mechanism, f"{h}.mechanism", required=False)
        self.supporting_claim_ids = ids(self.supporting_claim_ids, f"{h}.supporting_claim_ids", ("CL",), fact=True)
        self.contradicting_claim_ids = ids(self.contradicting_claim_ids, f"{h}.contradicting_claim_ids", ("CL",),
                                           fact=True)
        both = set(self.supporting_claim_ids) & set(self.contradicting_claim_ids)
        if both:
            raise MalformedInput(f"claims cannot both support and contradict: {sorted(both)}", h)
        self.parent_ids = ids(self.parent_ids, f"{h}.parent_ids", ("HYP", "CHYP"))
        self.candidate_ids = ids(self.candidate_ids, f"{h}.candidate_ids", ("CAND",))
        self.predicted_observations = ids(self.predicted_observations, f"{h}.predicted_observations", ("REQ",))
        self.falsification_criteria = ids(self.falsification_criteria, f"{h}.falsification_criteria", ("REQ",))
        self._base_init()
        if self.id in self.parent_ids:
            raise DependencyCycle("a hypothesis cannot be its own parent", f"{h}.parent_ids")

    def as_claim(self):
        """The only form in which a hypothesis may enter a V1 graph: origin=hypothesis, never verified."""
        from ..evidence.types import Claim, ClaimOrigin

        return Claim(self.statement, origin=ClaimOrigin.HYPOTHESIS, scope=self.scope)


@dataclass
class CounterHypothesis(Hypothesis):
    counters: str = ""
    _id_prefix = "CHYP"
    _id_fields = ("statement", "scope", "counters")

    def __post_init__(self) -> None:
        self.counters = ref(self.counters, "CounterHypothesis.counters", ("HYP",))
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
    prior_art_assessment_id: str | None = None  # the structured replacement for `novelty`
    derived_from: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    version: str = SCHEMA_VERSION
    id: str = ""
    _id_prefix = "CAND"
    _id_fields = ("description",)
    _transitions = CANDIDATE_TRANSITIONS

    def __post_init__(self) -> None:
        self.description = text(self.description, "Candidate.description")
        self.originating_gap = text(self.originating_gap, "Candidate.originating_gap")
        self.hypothesis_ids = ids(self.hypothesis_ids, "Candidate.hypothesis_ids", ("HYP", "CHYP"))
        self.evidence_support = ids(self.evidence_support, "Candidate.evidence_support", ("CL",), fact=True)
        self.evidence_against = ids(self.evidence_against, "Candidate.evidence_against", ("CL",), fact=True)
        both = set(self.evidence_support) & set(self.evidence_against)
        if both:
            raise MalformedInput(f"claims cannot be both for and against: {sorted(both)}", "Candidate.evidence")
        for name in ("prior_art", "assumptions", "constraints", "constraints_satisfied", "constraints_violated"):
            setattr(self, name, ids(getattr(self, name), f"Candidate.{name}"))
        for name in ("required_conditions", "legal_constraints", "benefits", "risks", "unknowns", "dependencies",
                     "test_requirements", "failure_modes"):
            setattr(self, name, texts(getattr(self, name), f"Candidate.{name}"))
        for name in ("costs", "simulation_results", "sensitivity"):
            setattr(self, name, mapping(getattr(self, name), f"Candidate.{name}"))
        self.reversible = boolean(self.reversible, "Candidate.reversible", allow_none=True)
        for name in ("novelty", "technical_feasibility", "economic_feasibility"):
            setattr(self, name, unit_interval(getattr(self, name), f"Candidate.{name}"))
        if self.novelty is not None:
            warnings.warn("Candidate.novelty is deprecated: a scalar cannot say what was searched. "
                          "Use prior_art_assessment_id (a PriorArtAssessment).", DeprecationWarning, stacklevel=3)
        if self.prior_art_assessment_id is not None:
            ref(self.prior_art_assessment_id, "Candidate.prior_art_assessment_id", ("PAA",))
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
        self.candidate_id = ref(self.candidate_id, "Scenario.candidate_id", ("CAND",))
        self.parameters = mapping(self.parameters, "Scenario.parameters")
        self.assumption_ids = ids(self.assumption_ids, "Scenario.assumption_ids", ("ASM",))
        for k, v in self.parameters.items():
            Var(k)  # parameter names are variable names
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
        self.candidate_id = ref(self.candidate_id, "Simulation.candidate_id", ("CAND",))
        self.model = text(self.model, "Simulation.model")
        self.model_version = text(self.model_version, "Simulation.model_version")
        self.iterations = integer(self.iterations, "Simulation.iterations", 1, MAX_ITERATIONS)
        if self.iterations > 1 and self.seed is None:
            raise MalformedInput("a stochastic simulation must record its seed", "Simulation.seed")
        if self.seed is not None:
            self.seed = integer(self.seed, "Simulation.seed", 0, 2 ** 63 - 1)
        for name in ("parameters", "initial_conditions", "outcomes", "uncertainty", "failure_states"):
            setattr(self, name, mapping(getattr(self, name), f"Simulation.{name}"))
        self.assumptions = ids(self.assumptions, "Simulation.assumptions")
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
        self.candidate_id = ref(self.candidate_id, "SensitivityResult.candidate_id", ("CAND",))
        for name in ("elasticities", "break_even", "failure_thresholds", "robust_ranges"):
            setattr(self, name, mapping(getattr(self, name), f"SensitivityResult.{name}"))
        for name in ("high_sensitivity", "low_sensitivity"):
            setattr(self, name, texts(getattr(self, name), f"SensitivityResult.{name}"))
        self.fragile_assumptions = ids(self.fragile_assumptions, "SensitivityResult.fragile_assumptions")
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
        Var(self.name)  # name safety
        Unit.parse(self.unit)
        self.integer = boolean(self.integer, f"DecisionVariable.{self.name}.integer")
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
        if not isinstance(self.variables, list) or not self.variables:
            raise MalformedInput("an optimization problem needs a list of decision variables",
                                 "OptimizationProblem.variables")
        self.variables = [v if isinstance(v, DecisionVariable) else _decision_variable(v) for v in self.variables]
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            raise DuplicateId("decision variable names repeat", "OptimizationProblem.variables")
        if isinstance(self.objective, dict):
            self.objective = expr_from_json(self.objective)
        if not isinstance(self.objective, Expr):
            raise MalformedInput("objective must be a structured expression", "OptimizationProblem.objective")
        if not isinstance(self.constraints, list):
            raise MalformedInput("constraints must be a list of relations", "OptimizationProblem.constraints")
        self.constraints = [c if isinstance(c, Relation) else Relation.from_json(c) for c in self.constraints]
        self.solver = text(self.solver, "OptimizationProblem.solver")
        if self.direction not in ("minimize", "maximize"):
            raise MalformedInput("direction must be 'minimize' or 'maximize'", "OptimizationProblem.direction")
        unknown = (self.objective.variables() | {v for c in self.constraints for v in c.variables()}) - set(names)
        if unknown:
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
        self.problem_id = ref(self.problem_id, "OptimizationResult.problem_id", ("OPT",))
        self.status = enum_value(OptimizationStatus, self.status, "OptimizationResult.status")
        self.objective_value = finite(self.objective_value, "OptimizationResult.objective_value")
        self.solution = mapping(self.solution, "OptimizationResult.solution")
        for k, v in self.solution.items():
            Var(k)
            finite(v, f"OptimizationResult.solution.{k}", allow_none=False)
        self.proof = text(self.proof, "OptimizationResult.proof", required=False)
        self.verified = boolean(self.verified, "OptimizationResult.verified")
        self.violations = texts(self.violations, "OptimizationResult.violations")
        if self.status == OptimizationStatus.OPTIMAL and not self.proof:
            raise MalformedInput("'optimal' requires a proof of optimality", "OptimizationResult.proof")
        if self.status in (OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE):
            if not self.solution:
                raise MalformedInput("a feasible result must include its solution", "OptimizationResult.solution")
            if self.violations:
                raise MalformedInput("a result with constraint violations cannot be feasible or optimal",
                                     "OptimizationResult.status")
            if not self.verified:
                raise MalformedInput("feasible or optimal requires an independent re-check of every constraint",
                                     "OptimizationResult.verified")
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
        if self.selected_candidate_id is not None:
            ref(self.selected_candidate_id, "DiscoveryDecision.selected_candidate_id", ("CAND",))
        self.alternatives = mapping(self.alternatives, "DiscoveryDecision.alternatives")
        for cid, reason in self.alternatives.items():
            ref(cid, "DiscoveryDecision.alternatives", ("CAND",))
            text(reason, f"DiscoveryDecision.alternatives.{cid}")
        if self.selected_candidate_id in self.alternatives:
            raise MalformedInput("the selected candidate cannot also be a rejected alternative",
                                 "DiscoveryDecision.alternatives")
        self.tie_break = text(self.tie_break, "DiscoveryDecision.tie_break", required=False)
        if (self.outcome == DiscoveryOutcome.CANDIDATE_SELECTED) != (self.selected_candidate_id is not None):
            raise MalformedInput("a selected candidate exists exactly when the outcome is candidate_selected",
                                 "DiscoveryDecision.outcome")
        self._base_init()


def _decision_variable(data: Any) -> DecisionVariable:
    if not isinstance(data, dict) or not set(data) <= {f.name for f in fields(DecisionVariable)} or "name" not in data:
        raise MalformedInput("a decision variable is an object with name, unit, lower, upper, integer",
                             "OptimizationProblem.variables")
    return DecisionVariable(**data)


ALL_TYPES = {
    "discovery_objective": DiscoveryObjective, "known_fact": KnownFact, "uncertainty": Uncertainty,
    "missing_evidence": MissingEvidence, "problem_frame": ProblemFrame, "prior_art": PriorArt, "gap": Gap,
    "assumption": Assumption, "constraint": Constraint, "connection": Connection,
    "evidence_requirement": EvidenceRequirement, "hypothesis": Hypothesis, "counter_hypothesis": CounterHypothesis,
    "candidate": Candidate, "scenario": Scenario, "simulation": Simulation, "sensitivity_result": SensitivityResult,
    "optimization_problem": OptimizationProblem, "optimization_result": OptimizationResult,
    "discovery_finding": DiscoveryFinding, "discovery_decision": DiscoveryDecision,
    "prior_art_assessment": PriorArtAssessment,
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
        raise MalformedInput(f"unexpected fields {sorted(map(str, extra))}", type_name)
    required = {f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING}
    missing = required - set(data)
    if missing:
        raise MalformedInput(f"missing fields {sorted(missing)}", type_name)
    data = dict(data)
    for key in ("time_range", "period"):
        if key in data and isinstance(data[key], list):
            data[key] = tuple(data[key])
    return cls(**data)
