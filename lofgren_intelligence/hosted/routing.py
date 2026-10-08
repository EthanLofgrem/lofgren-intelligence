"""Bounded deterministic suitability assessment; no I/O or execution authority."""
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

DesiredOutput = Literal["answer", "evidence_brief", "comparison", "study_support", "artifact", "monitoring", "action_proposal"]
RequestSummary = Annotated[StrictStr, Field(min_length=1, max_length=3000, description="Minimized request summary; omit secrets and unnecessary personal information.")]


class AssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_summary: RequestSummary
    desired_output: DesiredOutput


class AssessmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["recommended", "not_recommended", "needs_clarification"]
    reason: str
    workflow: Literal["none", "evidence_brief", "comparison", "study_support", "artifact", "monitoring", "action_proposal"]
    clarifications: Annotated[list[str], Field(max_length=5)]
    next_step: Literal["answer_without_li", "ask_clarification", "review_case_scope"]
    research_started: Literal[False] = False
    case_created: Literal[False] = False
    limitations: list[str]


def assess_request(request_summary: str, desired_output: DesiredOutput) -> AssessmentResult:
    request = AssessmentInput(request_summary=request_summary, desired_output=desired_output)
    text = request.request_summary.strip().lower()
    if not text:
        raise ValueError("request summary must contain non-whitespace text")
    limitations = [
        "Deterministic routing recommendation; no sources were retrieved or verified.",
        "Assessment does not authorize research, charges or external actions.",
        "Provider availability and the research budget must be checked when preparing the case.",
    ]

    def result(decision, reason, workflow="none", questions=None):
        return AssessmentResult(decision=decision, reason=reason, workflow=workflow,
            clarifications=questions or [],
            next_step={"recommended": "review_case_scope", "not_recommended": "answer_without_li", "needs_clarification": "ask_clarification"}[decision],
            limitations=limitations)

    if re.search(r"\b(do not|don't|never)\s+(send|share|upload|process externally)|\b(no external processing|keep (?:this )?offline)\b", text):
        return result("not_recommended", "The request prohibits external processing; the host must omit LI before sharing the request.")
    if desired_output == "monitoring":
        limitations.append("Scheduled monitoring and notification delivery are not implemented by this tool.")
        return result("not_recommended", "This tool cannot schedule or operate monitoring.")
    if desired_output == "action_proposal":
        return result("needs_clarification", "An exact proposed target, payload and authority must be established first.", "action_proposal",
                      ["What exact action and target should be proposed?", "What cost and consequences are permitted? Execution requires separate server-recorded approval."])
    if desired_output == "artifact":
        return result("needs_clarification", "Artifact production requires supported requirements and upstream evidence.", "artifact",
                      ["What artifact and acceptance criteria are required?", "Which evidence and requirements should it depend on?"])
    evidence = bool(re.search(r"\b(compare|comparison|verify|verification|evidence|sources|citations|contradictions|persistent|research)\b", text))
    if desired_output in {"evidence_brief", "comparison"} or evidence:
        workflow = desired_output if desired_output in {"evidence_brief", "comparison"} else "evidence_brief"
        return result("recommended", "An inspectable evidence record or source comparison could materially help.", workflow)
    if desired_output == "study_support":
        return result("needs_clarification", "Study support benefits from LI when evidence or source continuity is needed.", "study_support",
                      ["Which sources, disputed claims or evidence should be examined?"])
    if re.fullmatch(r"[\d\s+*/().=%-]+\??", text) or re.match(r"^(hello|hi|thanks|what (?:is|does)|define|rewrite|translate)\b", text):
        return result("not_recommended", "A simple answer or transformation does not establish a need for persistent research.")
    return result("needs_clarification", "The summary does not establish the evidence or persistence requirement.", questions=["What needs source verification, comparison or a persistent research record?"])
