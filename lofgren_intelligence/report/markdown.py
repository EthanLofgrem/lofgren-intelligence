"""Reports: a view of the run's typed state, for people (Markdown) and machines (JSON).

Every sentence here is rendered from findings, claims, contradictions,
unknowns, calculations and the ledger. The report adds no facts.
"""

from __future__ import annotations

from ..evidence.types import ClaimStatus, Scope, to_dict
from ..kernel.pipeline import RunResult
from ..kernel.receipt import evidence_dates, evidence_labels

_STATUS_LABEL = {
    ClaimStatus.VERIFIED: "Verified",
    ClaimStatus.PARTIALLY_VERIFIED: "Partially verified",
    ClaimStatus.SUPPORTED: "Single-source",
    ClaimStatus.CONTESTED: "Contested",
    ClaimStatus.INSUFFICIENT: "Insufficient evidence",
    ClaimStatus.UNVERIFIED: "Unverified",
}


def _scope(s: Scope) -> str:
    parts = []
    if s.valid_from:
        parts.append(s.valid_from if s.valid_from == s.valid_to or not s.valid_to else f"{s.valid_from} to {s.valid_to}")
    if s.geography:
        parts.append(s.geography)
    return ", ".join(parts) or "not established"


def render_markdown(r: RunResult, include_receipt: bool = True) -> str:
    c = r.contract
    g = r.graph
    src_index = {sid: i + 1 for i, sid in enumerate(g.sources)}
    q_index = {q.id: i + 1 for i, q in enumerate(c.questions)}
    out: list[str] = []
    w = out.append

    w("# Lofgren Intelligence report\n")
    w(f"**Objective:** {c.objective}  ")
    w(f"**Mode:** {c.mode} · **Finished:** {r.finished_at} · **Reasoning provider:** {r.provider}  ")
    if include_receipt and r.receipt:
        w(f"**Research ID:** `{r.receipt.get('research_id')}`  ")
    cal = "calibrated against recorded outcomes" if r.calibrated else \
        "provisional (v1 heuristic; not yet calibrated against real outcomes)"
    w(f"**Confidence scores:** {cal}\n")

    if r.stopped_reason:
        w(f"> **Stopped before research:** {r.stopped_reason}\n")

    w("## Outcome contract\n")
    w(f"- Desired outcome: {c.desired_outcome}")
    for con in c.constraints:
        w(f"- Constraint: {con}")
    std = c.evidence_standard
    w(f"- Evidence standard: {std.min_independent_sources} independent sources, confidence ≥ {std.min_confidence:.0%} "
      "(attributions need one primary source; physical claims need a direct observation)")
    w(f"- Research spend cap: ${c.max_spend_usd:.2f}; actions needing approval: {', '.join(c.approval_required)}")
    w("- Question graph: " + "; ".join(
        f"Q{q_index[q.id]}" + (f" ← {', '.join('Q' + str(q_index[d]) for d in q.depends_on)}" if q.depends_on else "")
        for q in c.questions) + "\n")

    if not r.stopped_reason:
        w("## Findings\n")
        for f in r.findings:
            w(f"### Q{q_index[f.question_id]} · {f.question}\n")
            w(f"**{f.answer}**\n")
            if f.confidence:
                w(f"- Confidence {f.confidence:.0%} ({f.confidence_status}, {f.confidence_method}) · "
                  f"scope: {_scope(f.scope)} · finding `{f.id}`")
            for cid in f.claim_ids:
                cl = g.claims[cid]
                refs = ", ".join(f"[{src_index[s.id]}]" for s in g.sources_for_claim(cl.id))
                flag = f" ⚠ {'; '.join(cl.issues)}" if cl.issues else ""
                label = _STATUS_LABEL[cl.status]
                if cl.claim_type.value == "attribution" and cl.status == ClaimStatus.VERIFIED:
                    label = "Verified attribution (what the source says, not that it is true)"
                w(f"- **{label} · {cl.confidence:.0%}** — {cl.statement} {refs} "
                  f"_({cl.claim_type.value}; {cl.sufficiency or 'no policy applied'})_{flag}")
            if f.contradiction_ids and f.claim_ids:
                w(f"- Conflicts: {', '.join(f'`{x}`' for x in f.contradiction_ids)}")
            if f.next_best_evidence:
                w(f"- Next best evidence: {f.next_best_evidence}")
            w("")

        w("## Contradiction graph\n")
        if g.contradictions:
            for cx in g.contradictions.values():
                a, b = g.claims[cx.claim_a], g.claims[cx.claim_b]
                label = "Incompatible" if cx.kind == "incompatible" else "Different scope (lead, not conflict)"
                w(f"- `{cx.id}` **{label}** — {cx.reason}")
                w(f"  - “{a.statement}” ({_scope(a.scope)})")
                w(f"  - “{b.statement}” ({_scope(b.scope)})")
                if cx.scope_note:
                    w(f"  - Scope: {cx.scope_note}")
                if cx.resolution:
                    w(f"  - Would resolve it: {cx.resolution}")
            w("")
        else:
            w("_None detected among the collected evidence._\n")

    w("## Unknowns and how to close them\n")
    open_unknowns = sorted((u for u in r.unknowns if u.status == "open"), key=lambda u: -u.expected_gain)
    if open_unknowns:
        w("| Unknown | Possible sources | Expected gain | Est. cost | Approval |")
        w("| --- | --- | --- | --- | --- |")
        for u in open_unknowns:
            w(f"| {u.description} | {', '.join(u.source_types) or '—'} | {u.expected_gain:.0%} | "
              f"${u.est_cost_usd:.4f} | {'required' if u.needs_approval else 'no'} |")
    else:
        w("_No open unknowns._")
    w("")

    if g.calculations:
        w("## Calculations\n")
        w("| Id | Calculation | Formula | Inputs | Result |")
        w("| --- | --- | --- | --- | --- |")
        for calc in g.calculations.values():
            ins = ", ".join(f"{i['name']}={i['value']:g}{(' ' + i['unit']) if i.get('unit') else ''}"
                            for i in calc.inputs if isinstance(i.get("value"), (int, float)))
            w(f"| `{calc.id}` | {calc.name} | `{calc.formula}` | {ins} | {calc.result:g} {calc.unit} |")
        w("")

    if r.lineage:
        w("## Source lineage\n")
        for link in r.lineage:
            w(f"- [{src_index.get(link.source, '?')}] {link.relation} from [{src_index.get(link.derived_from, '?')}]"
              f"{' (' + link.detail + ')' if link.detail else ''} — counted as one confirmation")
        w("")

    if g.sources:
        w("## Sources\n")
        for sid, n in src_index.items():
            s = g.sources[sid]
            meta = ", ".join(x for x in (s.kind.value, s.publisher, s.license) if x)
            w(f"{n}. {s.title} — {s.uri} ({meta}; retrieved {s.retrieved_at})")
            # Public-source evidence keeps its provenance labels and passage next to its source.
            for e in g.evidence.values():
                labels = evidence_labels(e.data) if e.source_id == sid else {}
                if labels:
                    shown = "; ".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                                      for k, v in labels.items())
                    passage = " ".join(e.content.split())
                    dates = ", ".join(f"{k} {v}" for k, v in evidence_dates(e).items()) or "no date"
                    w(f"   - evidence `{e.id}` (source `{sid}`; {dates}): {shown}")
                    w(f"     - passage: “{passage}”")
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
        w(f"- Charged: ${r.charge.total_usd:.4f} for {r.charge.work_units:g} work units (never more than the estimate)")
    if r.ledger and r.ledger.entries:
        parts = [f"{k}: {v['operations']} ops, {v['work_units']:g} WU" for k, v in r.ledger.by_kind().items()]
        w(f"- Ledger: {'; '.join(parts)}")
    w("")

    if include_receipt and r.receipt:
        w("## Research receipt\n")
        w(f"- Research ID: `{r.receipt['research_id']}`")
        w(f"- Inputs hash: `{r.receipt['inputs_hash'][:16]}…` · State hash: `{r.receipt['state_hash'][:16]}…`")
        w(f"- Provider: {r.receipt['provider'].get('name')} {r.receipt['provider'].get('version')} · "
          f"extraction template {r.receipt['extraction_template']}")
        w("")
    return "\n".join(out)


def render_json(r: RunResult) -> dict:
    return {
        "research_id": r.receipt.get("research_id"),
        "contract": to_dict(r.contract),
        "stopped_reason": r.stopped_reason,
        "provider": r.provider_info or r.provider,
        "calibrated": r.calibrated,
        "finished_at": r.finished_at,
        "stages": [{"stage": s.stage.value, "status": s.status, "detail": s.detail, "work_units": s.work_units}
                   for s in r.stages],
        "findings": [to_dict(f) for f in r.findings],
        "unknowns": [to_dict(u) for u in r.unknowns],
        "estimate": r.estimate.as_dict() if r.estimate else None,
        "charge": r.charge.as_dict() if r.charge else None,
        "ledger": r.ledger.to_json() if r.ledger else None,
        "confidence_factors": {k: f.as_dict() for k, f in r.factors.items()},
        "evidence_graph": r.graph.to_json(),
        "receipt": r.receipt,
    }
