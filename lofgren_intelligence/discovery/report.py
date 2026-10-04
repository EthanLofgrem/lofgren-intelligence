"""Views of a DiscoveryResult: Markdown for people, JSON for machines. Views only: nothing here decides anything.

The wording rules hold in every view: a simulation is a prediction, a hypothesis is not established, a prior-art
miss states its coverage and never novelty, and verified facts are only V1 known facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import DiscoveryResult


def _fmt(v) -> str:
    if v is None:
        return "unknown"
    return f"{v:g}" if isinstance(v, float) else str(v)


def render_discovery_markdown(result: "DiscoveryResult") -> str:
    r, lines = result, []
    lines += [f"# Discovery: {r.objective.objective}", "",
              f"**Outcome:** `{r.outcome.value}`  ", f"**Rests on V1 research** `{r.context.research_id}` "
              f"(knowledge map `{r.context.schema}`, assurance `{r.context.assurance.level.value}`)  ",
              f"**Discovery receipt** `{r.receipt.get('discovery_id', '')}` · fingerprint "
              f"`{r.receipt.get('discovery_fingerprint', '')}`", ""]
    sel = r.selected
    if r.decision is not None:
        lines += ["## Decision", "", f"Rule: {r.decision.rule}", ""]
        if sel is not None:
            lines += [f"**Selected:** {sel.description} (`{sel.id}`)", ""]
        for cid, why in sorted(r.decision.alternatives.items()):
            lines.append(f"- not selected `{cid}`: {why}")
        lines.append("")
    if r.candidates.candidates:
        lines += ["## Candidates (order is not ranking)", "",
                  "| Candidate | Status | Technical feasibility | Economic feasibility | Expected value | Robustness |",
                  "|---|---|---|---|---|---|"]
        for c in sorted(r.candidates.candidates, key=lambda c: c.id):
            lines.append(f"| {c.description} | {c.status.value} | {_fmt(c.technical_feasibility)} | "
                         f"{_fmt(c.economic_feasibility)} | {_fmt(c.expected_value)} | {c.robustness.value} |")
        lines += ["", "Expected values are simulated predictions, not observations.", ""]
    if r.candidates.simulations:
        lines += ["## Simulations (predictions)", ""]
        for s in r.candidates.simulations:
            for m, x in sorted(s.outcomes.items()):
                lines.append(f"- `{s.candidate_id}` {m}: mean {_fmt(x['mean'])}, p5 {_fmt(x['p5'])}, p95 "
                             f"{_fmt(x['p95'])} {x['unit']} ({s.model} v{s.model_version}, seed {s.seed}, "
                             f"{s.iterations} draws)")
        lines.append("")
    if r.candidates.sensitivities:
        lines += ["## Sensitivity", ""]
        for s in r.candidates.sensitivities:
            lines.append(f"- `{s.candidate_id}` robustness **{s.robustness.value}**; most sensitive to "
                         f"{', '.join(s.high_sensitivity) or 'nothing above elasticity 1'}")
            for var, be in sorted(s.break_even.items()):
                lines.append(f"  - break-even {var} = {_fmt(be['value'])} {be['unit']}")
        lines.append("")
    if r.candidates.optimization is not None:
        o = r.candidates.optimization
        lines += ["## Optimization", "", f"Status `{o.status.value}` — {o.proof}", ""]
        if o.solution:
            lines.append("Solution: " + ", ".join(f"{k} = {_fmt(v)}" for k, v in sorted(o.solution.items())))
        for v in o.violations:
            lines.append(f"- {v}")
        lines.append("")
    if r.hypotheses.all:
        lines += ["## Hypotheses (not established)", ""]
        for h in sorted(r.hypotheses.all, key=lambda h: h.id):
            lines.append(f"- [{h.status.value}] {h.statement} — origin `{h.origin}`, provisional score "
                         f"{_fmt(h.provisional_score)} (hypothesis)")
        lines.append("")
    if r.prior_art:
        lines += ["## Prior art", ""] + [f"- {a.statement}" for a in r.prior_art] + [""]
    if r.framed.known_facts:
        lines += ["## Verified by V1", ""] + [f"- {f.statement} (`{f.claim_id}`)" for f in r.framed.known_facts] + [""]
    if r.requirements:
        lines += ["## Evidence V1 would need next", ""]
        lines += [f"- {q.description}" for q in sorted(r.requirements, key=lambda q: q.id)[:15]] + [""]
    if r.verifier.issues:
        lines += ["## Discovery verifier", ""]
        lines += [f"- `{i.code}` {i.object_id}: {i.reason}" + (f" ({i.lowered})" if i.lowered else "")
                  for i in r.verifier.issues] + [""]
    lines += ["## Cost", "", f"{len(r.ledger.entries)} operations, {r.ledger.total_units:g} work units, "
              f"${r.ledger.total_usd:.4f}", ""]
    if r.handoff is not None:
        lines += ["## V3 handoff", "", f"Ready: `{r.handoff['schema']}` with {len(r.handoff['specifications'])} "
                  f"specifications and {len(r.handoff['acceptance_criteria'])} acceptance criteria.", ""]
    else:
        lines += ["## V3 handoff", "", f"None: the outcome is `{r.outcome.value}`.", ""]
    return "\n".join(lines)


def discovery_summary(result: "DiscoveryResult") -> dict:
    """The typed summary MCP clients and the CLI receive."""
    r = result
    sel = r.selected
    return {
        "discovery_id": r.receipt.get("discovery_id"),
        "discovery_fingerprint": r.receipt.get("discovery_fingerprint"),
        "research_id": r.context.research_id,
        "assurance": r.context.assurance.level.value,
        "outcome": r.outcome.value,
        "selected_candidate": None if sel is None else {"id": sel.id, "description": sel.description,
                                                        "kind": "candidate", "status": sel.status.value},
        "decision_rule": r.decision.rule if r.decision else None,
        "candidates": [{"id": c.id, "description": c.description, "status": c.status.value,
                        "technical_feasibility": c.technical_feasibility,
                        "economic_feasibility": c.economic_feasibility, "expected_value": c.expected_value,
                        "expected_value_kind": "simulated", "robustness": c.robustness.value,
                        "confidence_kind": "candidate_robustness"} for c in sorted(r.candidates.candidates, key=lambda c: c.id)],
        "hypotheses": [{"id": h.id, "statement": h.statement, "status": h.status.value, "origin": h.origin,
                        "kind": "hypothesis", "confidence_kind": "hypothesis", "provisional_score": h.provisional_score}
                       for h in sorted(r.hypotheses.all, key=lambda h: h.id)],
        "findings": [{"id": f.id, "statement": f.statement, "kind": f.kind.value,
                      "confidence_kind": f.confidence_kind.value, "rests_on": f.rests_on} for f in r.findings],
        "verifier_issues": [i.__dict__ for i in r.verifier.issues],
        "requirements": [{"id": q.id, "description": q.description, "capability": q.capability, "place": q.place,
                          "period": list(q.period)} for q in sorted(r.requirements, key=lambda q: q.id)],
        "handoff_ready": r.handoff is not None,
        "cost_usd": r.ledger.total_usd,
        "stages": dict(r.stages),
        "notes": list(r.notes),
    }
