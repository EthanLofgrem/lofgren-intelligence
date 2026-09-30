# V1 certification: the gate before V2

V2 (Discovery Intelligence) generates hypotheses, candidates and simulations from V1's state. If that state is weak, V2 multiplies the weakness. So V2 construction starts only when this passes:

```
V1Ready = StructuredEvidence ∧ StructuredAnswers ∧ Provenance ∧ Contradictions ∧ Gaps
          ∧ TemporalScope ∧ CostLedger ∧ ResearchReceipts ∧ ProviderAbstraction
          ∧ MCPContracts ∧ E2ECertification
```

Run it:

```bash
lofgren certify            # prints each scenario and the gate; exit code 0 only if V1Ready
lofgren certify --out certification.json
```

## Scenarios

All run offline on fixed, fictional fixtures (network providers are replaced by deterministic fakes), so certification is reproducible in CI.

| Scenario | Proves | Criteria |
| --- | --- | --- |
| Document research | One typed finding per question; findings cite only graph claims; quantitative claims verified by ≥ 2 independent sources | StructuredEvidence, StructuredAnswers, Provenance |
| Discovered web research | Search finds pages; duplicate URLs collapse; robots.txt refusals recorded; government sources classified | Provenance |
| Conflicting evidence | Contradictions recorded with a resolution plan; contested status; resolution becomes an unknown | Contradictions, Gaps |
| Syndicated sources | Two copies of one article count as one confirmation and cannot verify a claim | Provenance |
| Stale evidence | Claims about old periods flagged; factual ones downgraded | TemporalScope |
| Scope mismatch | 2024 vs 2026 figures are a scope difference, not a conflict; every claim carries a period | TemporalScope, Contradictions |
| Satellite metadata | Passes labelled as predictions; scenes normalized; count calculations recorded | StructuredEvidence |
| Authorized sensors | Unauthorized readings refused; °F → °C; unit-less series rejected | StructuredEvidence, Provenance |
| Budget exhaustion | Over-cap job stops before research with no charge; plan limits enforced | CostLedger |
| Offline behavior | Search and imagery outages become unknowns; the run completes | Gaps |
| MCP contract | Structured tools; investigate → receipt intact | MCPContracts |
| Reproducible receipt | Same inputs → same inputs hash and state hash; tampering detected; ledger complete | ResearchReceipts, CostLedger |
| Hypothesis guard | A supported hypothesis stays unverified; promotion refused; absent from the knowledge map | StructuredAnswers |
| Calibration infrastructure | Every confidence logged; outcomes recordable; Brier score; scores labelled provisional | StructuredAnswers |
| Provider abstraction | Any ReasoningProvider plugs in; provider name, version and usage land in the receipt | ProviderAbstraction |

## The hard rule for V2

> V2 may generate hypotheses from uncertainty, but it may never silently promote a hypothesis into a verified V1 finding.

Enforced in code: hypotheses enter the graph only as `origin = hypothesis`; the verifier never verifies them; `discovery.promote` always refuses. A hypothesis becomes knowledge only when new evidence, gathered through V1, verifies a separate claim.

## What V2 reads

`kernel.state.export_state(run)` → `lofgren.knowledge-map/1`:

- `known` — claims that meet their policy
- `uncertain` — supported but short of the policy
- `contradicted` — contested claims and their contradictions
- `unknowns` — open gaps with acquisition plans
- `calculations` — reproducible numbers simulations may inherit
- `findings` — one per research question
