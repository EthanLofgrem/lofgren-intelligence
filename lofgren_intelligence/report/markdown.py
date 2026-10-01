"""Reports: the evidence graph rendered for people (Markdown) and machines (JSON)."""

from __future__ import annotations

from ..evidence.types import ClaimStatus, to_dict
from ..kernel.answers import answer_all
from ..kernel.pipeline import RunResult

_STATUS_LABEL = {
    ClaimStatus.VERIFIED: "Verified",
    ClaimStatus.PARTIALLY_VERIFIED: "Partially verified",
    ClaimStatus.SUPPORTED: "Single-source",
    ClaimStatus.CONTESTED: "Contested",
    ClaimStatus.INSUFFICIENT: "Insufficient evidence",
    ClaimStatus.UNVERIFIED: "Unverified",
}
MAX_CLAIMS_PER_QUESTION = 5


def render_markdown(r: RunResult) -> str:
    c = r.contract
    src_index = {sid: i + 1 for i, sid in enumerate(r.graph.sources)}
    out: list[str] = []
    w = out.append

    w("# Lofgren Intelligence report\n")
    w(f"**Objective:** {c.objective}  ")
    w(f"**Mode:** {c.mode} · **Finished:** {r.finished_at} · **Reasoning provider:** {r.provider}  ")
    cal = "calibrated against recorded outcomes" if r.calibrated else \
        "uncalibrated starting estimates (no outcomes recorded yet)"
    w(f"**Confidence scores:** {cal}\n")

    if r.stopped_reason:
        w(f"> **Stopped before research:** {r.stopped_reason}\n")

    w("## Outcome contract\n")
    w(f"- Desired outcome: {c.desired_outcome}")
    for con in c.constraints:
        w(f"- Constraint: {con}")
    std = c.evidence_standard
    w(f"- Evidence standard: {std.min_independent_sources} independent sources, confidence ≥ {std.min_confidence:.0%}")
    w(f"- Research spend cap: ${c.max_spend_usd:.2f}; actions needing approval: {', '.join(c.approval_required)}\n")

    if not r.stopped_reason:
        w("## Findings\n")
        for ans in answer_all(r):
            q = ans.question
            w(f"### {q.text}\n")
            if q.role == "gap":
                w("\n".join(f"- {g}" for g in ans.gaps) if ans.gaps else "_Nothing outstanding from the sources connected._")
                w("")
                continue
            if not ans.claims:
                empty = ("_No contradictions found among the collected evidence._" if q.role == "contradict"
                         else "_No evidence found for this question yet._")
                w(empty + "\n")
                continue
            for cl in ans.claims:
                refs = ", ".join(f"[{src_index[s.id]}]" for s in r.graph.sources_for_claim(cl.id))
                w(f"- **{_STATUS_LABEL[cl.status]} · {cl.confidence:.0%}** — {cl.statement} {refs}")
            w("")

        w("## Contradictions\n")
        if r.graph.contradictions:
            for cx in r.graph.contradictions.values():
                a, b = r.graph.claims[cx.claim_a], r.graph.claims[cx.claim_b]
                w(f"- {cx.reason}:\n  - “{a.statement}”\n  - “{b.statement}”")
            w("\nContradictions are leads, not noise: each marks where a data error, a delay, "
              "a changed condition or a new finding may be hiding.\n")
        else:
            w("_None detected among the collected evidence._\n")

    w("## What is missing\n")
    if r.gaps:
        for g in r.gaps:
            w(f"- {g}")
    else:
        w("_No gaps recorded._")
    w("")

    if r.graph.sources:
        w("## Sources\n")
        for sid, n in src_index.items():
            s = r.graph.sources[sid]
            meta = ", ".join(x for x in (s.kind.value, s.publisher, s.license) if x)
            w(f"{n}. {s.title} — {s.uri} ({meta}; retrieved {s.retrieved_at})")
        w("")

    w("## Loop status\n")
    w("| Stage | Status | Detail |")
    w("| --- | --- | --- |")
    for st in r.stages:
        w(f"| {st.stage.value} | {st.status.replace('_', ' ')} | {st.detail} |")
    w("")

    w("## Cost\n")
    if r.estimate:
        w(f"- Estimate shown before running: {r.estimate.job_class} job, ${r.estimate.total_usd:.4f} "
          f"(plan: {r.estimate.plan})")
    if r.charge:
        w(f"- Charged: ${r.charge.total_usd:.4f} for {r.charge.work_units:g} work units "
          f"(never more than the estimate)")
    w("")
    return "\n".join(out)


def render_json(r: RunResult) -> dict:
    return {
        "contract": to_dict(r.contract),
        "stopped_reason": r.stopped_reason,
        "provider": r.provider,
        "calibrated": r.calibrated,
        "finished_at": r.finished_at,
        "stages": [{"stage": s.stage.value, "status": s.status, "detail": s.detail, "work_units": s.work_units}
                   for s in r.stages],
        "gaps": r.gaps,
        "estimate": r.estimate.as_dict() if r.estimate else None,
        "charge": r.charge.as_dict() if r.charge else None,
        "confidence_factors": {k: f.as_dict() for k, f in r.factors.items()},
        "evidence_graph": r.graph.to_json(),
    }
