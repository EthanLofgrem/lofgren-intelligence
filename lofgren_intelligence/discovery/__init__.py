"""Discovery Intelligence (V2).

V2 turns verified V1 evidence and explicitly represented uncertainty into
possibilities that can be analysed, challenged, simulated and optimized. It
reads V1 state only through an exported knowledge map: `kernel.knowledge_map.export_knowledge_map`
(knowledge-map/2, with its receipt) or, degraded, `kernel.state.export_state` (knowledge-map/1).

The invariant enforced in code, from the first V2 commit:

    V2-generated idea != verified fact

* A hypothesis enters a V1 evidence graph only as origin=hypothesis, which the
  V1 verifier never verifies.
* `promote` refuses every V2 object. A hypothesis becomes knowledge only when
  a new V1 investigation verifies a separate claim.
* The evidence graph accepts only V1 `Claim` objects.
* Novelty, feasibility, expected value and robustness stay separate measures.
"""

from __future__ import annotations

from ..evidence.graph import EvidenceGraph
from ..evidence.types import Claim
from .errors import (
    ContextMismatch,
    DependencyCycle,
    DiscoveryError,
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
    ReceiptTampered,
    UnitMismatch,
    UnknownReference,
    UnknownStatus,
    UnsafeName,
    UnsupportedAlgorithm,
)
from .context import (
    AssuranceLevel,
    ContextAssurance,
    DiscoveryContext,
    KnowledgeMapRefused,
    ResolvedReference,
    knowledge_map_fingerprint,
)
from .types import (
    ALL_TYPES,
    SCHEMA_VERSION,
    Assumption,
    Candidate,
    CandidateStatus,
    ConfidenceKind,
    Connection,
    ConnectionStrength,
    Constraint,
    ConstraintKind,
    CounterHypothesis,
    DecisionVariable,
    DiscoveryDecision,
    DiscoveryFinding,
    DiscoveryObjective,
    DiscoveryOutcome,
    EvidenceRequirement,
    FindingKind,
    Gap,
    GapBasis,
    GapType,
    Hypothesis,
    HypothesisStatus,
    KnownFact,
    MissingEvidence,
    OptimizationProblem,
    OptimizationResult,
    OptimizationStatus,
    PriorArt,
    PriorArtAssessment,
    PriorArtConclusion,
    ProblemFrame,
    Robustness,
    Scenario,
    SensitivityResult,
    Simulation,
    Uncertainty,
    UncertaintyReason,
    from_dict,
)
from .frame import FrameResult, frame_problem
from .gaps import GapResult, detect_gaps
from .principles import ground_constraint, state_assumption
from .prior_art import FixturePriorArtProvider, PriorArtProvider, ProviderCoverage, assess_prior_art
from .requirements import evidence_requirement, requirement_for_gap


def add_hypothesis(graph: EvidenceGraph, hyp: Hypothesis) -> Claim:
    """Record a hypothesis in a V1 graph without giving it evidential weight."""
    return graph.add_claim(hyp.as_claim())


def promote(obj: object) -> None:
    """There is no path from any V2 object to a verified finding except new V1 evidence."""
    if isinstance(obj, Hypothesis):
        raise PromotionRefused("a hypothesis becomes knowledge only when a new V1 investigation verifies a separate claim")
    if isinstance(obj, (Candidate, Simulation, OptimizationResult, Connection, Gap)):
        raise PromotionRefused(f"a {type(obj).__name__} is a discovery object, never evidence")
    raise PromotionRefused("claims are verified by the V1 verifier, not promoted")