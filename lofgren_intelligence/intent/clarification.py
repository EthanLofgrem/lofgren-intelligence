"""Deterministic clarification layer for large Intelligence Case objectives.

A vague, consequential objective is not treated as a research request yet.
This module asks a bounded set of high-information questions, records answers
explicitly (including unknown/skip/default states), and produces a Case Charter
only when the objective is scoped enough to plan research responsibly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

UNKNOWN_VALUES = {"unknown", "i don't know", "i dont know", "skip", "later", "ask me later"}
DEFAULT_VALUES = {"use a reasonable default", "reasonable default", "default"}

_DESIGN_WORDS = {
    "design", "build", "create", "develop", "invent", "prototype", "architecture",
    "system", "device", "solution", "product",
}
_HIGH_CONSEQUENCE_WORDS = {
    "water", "medical", "health", "safety", "public", "chemical", "drinking",
    "treatment", "energy", "infrastructure", "food",
}


@dataclass(frozen=True)
class ClarificationQuestion:
    key: str
    prompt: str
    why: str
    required: bool = True
    examples: tuple[str, ...] = ()


@dataclass
class ClarificationResult:
    status: str
    objective: str
    round: int
    questions: list[ClarificationQuestion] = field(default_factory=list)
    accepted_answers: dict[str, Any] = field(default_factory=dict)
    critical_unknowns: list[str] = field(default_factory=list)
    case_charter: dict[str, Any] | None = None


BASE_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "location",
        "Where is this intended to be used or built?",
        "Location can change climate, regulations, available materials, logistics, source conditions, and what evidence is relevant.",
        examples=("country/region", "climate/terrain", "or 'unknown'"),
    ),
    ClarificationQuestion(
        "users",
        "Who will use, operate, or maintain it, and about how many people must it serve?",
        "Capacity, maintenance skill, accessibility, and operating model depend on the intended users.",
        examples=("households", "school or clinic", "village utility", "field team"),
    ),
    ClarificationQuestion(
        "problem",
        "What specific problem must the system solve?",
        "A design cannot be evaluated correctly until the failure or need being addressed is explicit.",
        examples=("microbial contamination", "sediment", "chemicals", "salinity", "cost", "reliability"),
    ),
    ClarificationQuestion(
        "success",
        "What measurable outcome would count as success?",
        "Success criteria determine what evidence, design alternatives, and tests are actually relevant.",
        examples=("liters/day", "target removal", "operating life", "price target"),
    ),
    ClarificationQuestion(
        "cost",
        "What does 'low cost' mean here?",
        "An explicit cost boundary prevents LI from optimizing for a design that is technically plausible but economically unusable.",
        examples=("maximum installed cost", "cost per liter", "monthly operating cost"),
    ),
    ClarificationQuestion(
        "constraints",
        "What constraints matter most?",
        "Constraints can change the feasible design space before research begins.",
        examples=("no grid electricity", "local repair only", "limited replacement parts", "transport limits"),
    ),
    ClarificationQuestion(
        "source_or_environment",
        "What source, operating environment, or starting conditions should LI assume?",
        "Input conditions determine which mechanisms are relevant and which claims can safely transfer.",
        examples=("water source", "existing system", "available data", "environmental conditions"),
    ),
)


def _words(text: str) -> set[str]:
    return {w.strip(".,:;!?()[]{}").lower() for w in text.split() if w.strip()}


def requires_clarification(objective: str) -> bool:
    words = _words(objective)
    return bool(words & _DESIGN_WORDS) and (
        len(words) < 40 or bool(words & _HIGH_CONSEQUENCE_WORDS)
    )


def _normalize_answers(answers: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (answers or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            v = value.strip()
            if not v:
                continue
            low = v.lower()
            if low in UNKNOWN_VALUES:
                out[key] = {"state": "unknown", "value": None}
            elif low in DEFAULT_VALUES:
                out[key] = {"state": "default_requested", "value": None}
            else:
                out[key] = {"state": "answered", "value": v}
        else:
            out[key] = {"state": "answered", "value": value}
    return out


def _missing(answers: dict[str, Any]) -> list[ClarificationQuestion]:
    missing: list[ClarificationQuestion] = []
    for q in BASE_QUESTIONS:
        state = answers.get(q.key, {}).get("state")
        if state not in {"answered", "unknown", "default_requested"}:
            missing.append(q)
    return missing


def clarify_objective(
    objective: str,
    answers: dict[str, Any] | None = None,
    *,
    max_first_round: int = 7,
) -> ClarificationResult:
    text = objective.strip()
    if not text:
        raise ValueError("objective is empty")

    normalized = _normalize_answers(answers)
    if not requires_clarification(text):
        charter = {
            "objective": text,
            "status": "ready_for_scope_approval",
            "answers": normalized,
            "critical_unknowns": [],
            "approval_required_before_research": True,
        }
        return ClarificationResult(
            "READY_FOR_SCOPE_APPROVAL", text, 0,
            accepted_answers=normalized, case_charter=charter,
        )

    missing = _missing(normalized)
    if missing:
        round_no = 1 if not normalized else 2
        # Ask no more than 3-7 questions; preserve deterministic high-information order.
        qs = missing[: max(3, min(max_first_round, 7))]
        critical_unknowns = [
            key for key, item in normalized.items() if item.get("state") == "unknown"
        ]
        return ClarificationResult(
            "CLARIFICATION_REQUIRED",
            text,
            round_no,
            questions=qs,
            accepted_answers=normalized,
            critical_unknowns=critical_unknowns,
        )

    unknowns = [
        key for key, item in normalized.items() if item.get("state") == "unknown"
    ]
    defaults = [
        key for key, item in normalized.items() if item.get("state") == "default_requested"
    ]
    charter = {
        "objective": text,
        "status": "ready_for_scope_approval",
        "decision": "Define the exact decision before substantial research begins.",
        "scope": {
            key: item.get("value")
            for key, item in normalized.items()
            if item.get("state") == "answered"
        },
        "critical_unknowns": unknowns,
        "defaults_requested": defaults,
        "excluded_scope": [
            "Do not claim real-world performance without appropriate testing.",
            "Do not silently convert an unknown or assumption into a verified fact.",
            "Do not perform consequential external actions without explicit authorization.",
        ],
        "approval_required_before_research": True,
    }
    return ClarificationResult(
        "READY_FOR_SCOPE_APPROVAL",
        text,
        2,
        accepted_answers=normalized,
        critical_unknowns=unknowns,
        case_charter=charter,
    )


def to_dict(result: ClarificationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "objective": result.objective,
        "round": result.round,
        "questions": [
            {
                "key": q.key,
                "question": q.prompt,
                "why_it_matters": q.why,
                "required": q.required,
                "examples": list(q.examples),
            }
            for q in result.questions
        ],
        "accepted_answers": result.accepted_answers,
        "critical_unknowns": result.critical_unknowns,
        "case_charter": result.case_charter,
    }
