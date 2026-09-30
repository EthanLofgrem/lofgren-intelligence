"""Intent compiler: a free-text objective becomes an Outcome Contract.

The Outcome Contract is the project's governing specification (its
"constitution"): what success means, what is off limits, how much may be
spent, how strong the evidence must be, and which actions need approval.
Every later stage is checked against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..evidence.types import make_id

# Lofgren Enterprise venture stages. A venture objective is investigated
# through these seven lenses so the output plugs straight into the
# Lofgren Enterprise assembly process.
LOFGREN_VENTURE_STAGES: tuple[tuple[str, str], ...] = (
    ("Qualify", "Is this objective worth pursuing, and does it fit the stated constraints?"),
    ("Discover", "What demand, gap or unmet problem exists, and where?"),
    ("Diligence", "What evidence supports or contradicts the opportunity, and who already does this?"),
    ("Blueprint", "What model, economics and structure would make it work?"),
    ("Assemble", "Which partners, skills, assets and contracts are required to form it?"),
    ("Pilot", "What is the smallest real test that would prove or disprove it?"),
    ("Operate", "What must be measured once it runs, and what defines success?"),
)

# Words that mark a finding as relevant to each venture stage.
STAGE_KEYWORDS: dict[str, frozenset[str]] = {
    "Qualify": frozenset({"budget", "cost", "costs", "legal", "regulation", "regulations", "license", "licensing",
                          "permit", "permits", "zoning", "feasible", "risk", "fit"}),
    "Discover": frozenset({"demand", "market", "growth", "customers", "customer", "population", "need", "gap",
                           "unmet", "underserved", "construction", "opportunity", "increased", "rising"}),
    "Diligence": frozenset({"competitor", "competitors", "competition", "existing", "already", "incumbent",
                            "vacancy", "supply", "decreased", "decline", "risk", "risks"}),
    "Blueprint": frozenset({"price", "pricing", "revenue", "margin", "margins", "cost", "costs", "profit",
                            "economics", "model", "rent", "rents", "fees", "wage", "wages"}),
    "Assemble": frozenset({"partner", "partners", "supplier", "suppliers", "vendor", "vendors", "contract",
                           "contracts", "labor", "workforce", "skills", "assets", "equipment", "land"}),
    "Pilot": frozenset({"test", "trial", "pilot", "prototype", "sample", "experiment", "launch"}),
    "Operate": frozenset({"measure", "metric", "metrics", "kpi", "operations", "monitor", "retention",
                          "utilization", "occupancy"}),
}
PRIOR_WORDS = frozenset({"previously", "study", "studies", "studied", "research", "attempted", "launched",
                         "founded", "existing", "already", "history", "historical", "patent", "patents"})

_VENTURE_WORDS = {
    "business", "venture", "startup", "company", "market", "launch", "opportunity",
    "customers", "revenue", "profit", "franchise", "partnership", "llc", "brand",
}
_VERIFY_WORDS = {"true", "verify", "claim", "claims", "fact", "accurate", "real", "prove", "check"}
_PHYSICAL_WORDS = {
    "satellite", "satellites", "orbit", "orbital", "imagery", "image", "land", "farmland",
    "crop", "crops", "field", "fields", "construction", "flood", "flooding", "fire",
    "wildfire", "drought", "vegetation", "water", "coast", "port", "factory", "site",
    "property", "parcel", "building", "sensor", "sensors", "iot", "temperature", "soil",
}
_SENSOR_WORDS = {"sensor", "sensors", "iot", "temperature", "humidity", "soil", "meter", "telemetry"}


@dataclass
class Question:
    text: str
    needs: list[str]  # adapter capabilities that could answer it
    role: str = "state"  # state | support | contradict | gap | prior | claim | stage
    stage: str = ""  # Lofgren venture stage, when applicable
    weight: float = 1.0  # how much this question matters to the decision
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("Q", self.text)


@dataclass
class EvidenceStandard:
    min_independent_sources: int = 2
    min_confidence: float = 0.7


@dataclass
class OutcomeContract:
    objective: str
    mode: str  # investigate | venture | verify
    desired_outcome: str
    questions: list[Question]
    constraints: list[str] = field(default_factory=list)
    project_budget_usd: float | None = None  # money the venture itself may use
    max_spend_usd: float = 5.0  # platform research spend cap for this run
    evidence_standard: EvidenceStandard = field(default_factory=EvidenceStandard)
    allowed_actions: list[str] = field(default_factory=lambda: ["research", "analyze", "report"])
    approval_required: list[str] = field(
        default_factory=lambda: ["spend_money", "purchase_data", "deploy", "publish", "send_message", "control_device"]
    )
    location: dict | None = None
    physical: bool = False
    success_criteria: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id("OC", self.objective)


_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k|m|thousand|million)?", re.I)
_LATLON = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
_PLACE = re.compile(r"\b(?:in|near|around|at)\s+([A-Z][a-zA-Z]+(?:[ ,]+[A-Z][a-zA-Z]+)*)")


def _parse_money(text: str) -> float | None:
    m = _MONEY.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    scale = (m.group(2) or "").lower()
    if scale in ("k", "thousand"):
        value *= 1_000
    elif scale in ("m", "million"):
        value *= 1_000_000
    return value


def _parse_location(text: str) -> dict | None:
    m = _LATLON.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"lat": lat, "lon": lon, "name": None}
    m = _PLACE.search(text)
    if m:
        return {"lat": None, "lon": None, "name": m.group(1).strip(" ,")}
    return None


def compile_intent(
    objective: str,
    max_spend_usd: float = 5.0,
    location: dict | None = None,
) -> OutcomeContract:
    """Deterministic compilation. A model provider may refine it later, but
    the contract must never depend on a model to exist."""
    text = objective.strip()
    if not text:
        raise ValueError("objective is empty")
    words = set(re.findall(r"[a-z]+", text.lower()))

    loc = location or _parse_location(text)
    # Coordinates mean a physical place: bring in orbital and imagery evidence.
    physical = bool(words & _PHYSICAL_WORDS) or bool(loc and loc.get("lat") is not None)
    sensors = bool(words & _SENSOR_WORDS)
    if words & _VERIFY_WORDS and not words & _VENTURE_WORDS:
        mode = "verify"
    elif words & _VENTURE_WORDS:
        mode = "venture"
    else:
        mode = "investigate"

    base_needs = ["text"]
    physical_needs = ["orbital_passes", "imagery_catalog"] + (["sensor"] if sensors else [])

    questions: list[Question] = []
    if mode == "venture":
        for stage, q in LOFGREN_VENTURE_STAGES:
            needs = list(base_needs)
            if physical and stage in ("Discover", "Diligence"):
                needs += physical_needs
            questions.append(Question(f"{stage}: {q}", needs, role="stage", stage=stage,
                                      weight=1.5 if stage in ("Discover", "Diligence") else 1.0))
    elif mode == "verify":
        phys = physical_needs if physical else []
        questions += [
            Question("What exactly is being claimed, and by whom?", base_needs, role="claim", weight=1.0),
            Question("What independent evidence supports the claim?", base_needs + phys, role="support", weight=2.0),
            Question("What evidence contradicts the claim?", base_needs + phys, role="contradict", weight=2.0),
            Question("What is still missing to settle it?", base_needs, role="gap", weight=1.0),
        ]
    else:
        phys = physical_needs if physical else []
        questions += [
            Question("What is the current, verifiable state of this subject?", base_needs + phys, role="state", weight=1.5),
            Question("What evidence supports the leading explanation?", base_needs, role="support", weight=1.5),
            Question("What evidence contradicts it, or suggests another explanation?", base_needs, role="contradict", weight=1.5),
            Question("What is missing, and what would resolve it?", base_needs, role="gap", weight=1.0),
            Question("Has this been studied or attempted before?", base_needs, role="prior", weight=1.0),
        ]

    constraints: list[str] = []
    budget = _parse_money(text)
    if budget is not None:
        constraints.append(f"Project budget must not exceed ${budget:,.2f}")
    if loc and loc.get("name"):
        constraints.append(f"Scope limited to {loc['name']}")

    desired = {
        "venture": "A verified opportunity assessment, structured through the Lofgren Enterprise stages, ready to blueprint.",
        "verify": "A verdict on the claim with cited supporting and contradicting evidence and a calibrated confidence.",
        "investigate": "A cited, verified understanding of the subject, with contradictions and gaps made explicit.",
    }[mode]

    return OutcomeContract(
        objective=text,
        mode=mode,
        desired_outcome=desired,
        questions=questions,
        constraints=constraints,
        project_budget_usd=budget,
        max_spend_usd=max_spend_usd,
        location=loc,
        physical=physical,
        success_criteria=[
            "Every key finding cites at least the minimum number of independent sources",
            "Contradictions are reported, not averaged away",
            "Gaps are listed with the evidence that would close them",
        ],
    )
