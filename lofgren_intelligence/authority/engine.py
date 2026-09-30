"""Authority engine: thinking and acting are separate.

    Execute = Authorized AND Validated AND WithinBudget AND WithinPolicy

V1 uses this to gate research spend and any paid data. V4 (Execution
Intelligence) routes every real-world action through the same check:
deployments, purchases, messages, publishing and device control.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..intent.compiler import OutcomeContract

# Never allowed, whatever the contract says.
FORBIDDEN = frozenset({
    "access_unauthorized_system",
    "control_third_party_satellite",
    "track_private_individual",
    "bypass_access_control",
})


@dataclass
class Action:
    kind: str  # e.g. research, purchase_data, deploy, send_message, control_device
    description: str
    cost_usd: float = 0.0
    reversible: bool = True
    validated: bool = True  # tests/verification that the action depends on have passed


@dataclass
class Decision:
    allowed: bool
    needs_approval: bool
    reasons: list[str] = field(default_factory=list)


def decide(action: Action, contract: OutcomeContract, spent_usd: float = 0.0,
           approved: bool = False) -> Decision:
    reasons: list[str] = []
    if action.kind in FORBIDDEN:
        return Decision(False, False, [f"'{action.kind}' is never permitted"])

    within_policy = action.kind in contract.allowed_actions or action.kind in contract.approval_required
    if not within_policy:
        reasons.append(f"'{action.kind}' is not in the contract's allowed actions")
    within_budget = spent_usd + action.cost_usd <= contract.max_spend_usd + 1e-9
    if not within_budget:
        reasons.append(f"would spend ${spent_usd + action.cost_usd:.2f}, over the ${contract.max_spend_usd:.2f} cap")
    if not action.validated:
        reasons.append("the checks this action depends on have not passed")

    needs_approval = action.kind in contract.approval_required or not action.reversible
    authorized = approved or not needs_approval
    if not authorized:
        reasons.append("requires explicit user approval")

    allowed = within_policy and within_budget and action.validated and authorized
    return Decision(allowed, needs_approval and not approved, reasons)
