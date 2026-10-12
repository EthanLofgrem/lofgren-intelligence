# Change-aware findings and governed updates

LI should preserve why an answer was reached, detect when its basis may have changed, and produce a reviewable new version. It must not silently rewrite historical results, equate a changed page with changed law, or install new production code because a source says to do so.

## Current implementation and limits

On the reviewed baseline (`731c658`, application tree matching `e232ced`):

| Boundary | Implemented basis | Remaining limitation |
| --- | --- | --- |
| Source intake | `adapters/manifest.py`: operator-selected public URLs, bounded SSRF-protected retrieval, exact fetched-byte SHA-256 and retrieval time | No continuous source monitoring or freshness guarantee |
| Evidence | `evidence/types.py`: evidence identity/content hashes | Extracted passage identity differs from whole-page revision identity |
| Receipt | `kernel/receipt.py`: source URIs, evidence hashes, supported provenance dates, claim support/contradiction references | A receipt hash proves neither issuer identity nor source truth |
| Temporal review | `verification/skeptic.py`, `verification/engine.py`: temporal eligibility and stale issues | Not a jurisdiction-aware legal updater or live law feed |
| Scope history | `hosted/service.py`, `hosted/cases.py`: versioned charters and stale approval rejection | No autonomous monitoring subscription exposed here |
| Software release | Existing exact-SHA CI and deployment evidence | New code still requires reviewed release gates; no self-promoting production updater |

## New offline comparison helper

```bash
python scripts/evidence_change_check.py old-receipt.json new-receipt.json > change-review.json
python -m unittest discover -s tests -p test_evidence_change_check.py -v
```

The helper accepts existing research-receipt/1 or /2 JSON files, caps each input at 8 MB, performs no networking, and does not modify receipts. It groups evidence-content hashes by exact source URI, compares sets without treating generated IDs or record order as changes, and lists prior claims dependent on changed or absent evidence. Input-file hashes preserve which files were compared.

Statuses distinguish `recorded_evidence_changed`, `recorded_evidence_unchanged`, `source_added`, `source_absent_in_new_receipt`, and `insufficient_evidence_for_comparison`. Missing evidence triggers review rather than a claim that nothing changed. Both supporting and contradicting dependencies are included.

This is an **operator review aid**, not an MCP tool, freshness monitor, receipt verifier, legal interpretation engine or hosted change notification service. Validate trusted original receipts through LI's existing receipt verification before relying on a comparison. Extraction, provider or scope differences can change evidence sets even when a page did not change. Unchanged extracted evidence may miss new material elsewhere in a page. URI redirects and renamed sources are separate identities and require human reconciliation. Reports can contain private source URLs and claim IDs: retain them in the same restricted case workspace as the inputs.

## Bounded operating procedure

1. Select the authoritative source register for the case: publisher, source URL, jurisdiction where relevant, document/release identifier, publication and effective dates, retrieval time, known limitations, and the case/claim dependencies. Label operator-selected sources honestly. Define budget, maximum sources and review cadence explicitly.
2. Retrieve only through LI's protected fetch adapters. Keep the old receipt and full-source fingerprint where available. Record unavailable providers and incomplete retrievals as unknowns; do not infer unchanged content from failure.
3. Compare old/new receipt evidence with the helper. For whole-source revision detection, separately compare the existing adapter's exact-byte `content_sha256`; preserve both retrieval records. A byte change is a review trigger, not a conclusion.
4. Review affected claims. Check effective dates, jurisdiction, amendments, repeal/corrections/retractions and whether the changed provision applies to the case. Medical, legal or financial interpretations require appropriately scoped evidence and qualified review when warranted.
5. Create a new run/version linked to the prior case and receipt. Preserve the earlier answer and explain what changed, why it matters, affected assumptions, unresolved unknowns and the evidence cut-off. Use existing charter-version mechanisms; material scope/budget changes require renewed server approval.
6. Rerun bounded research through the existing approved job path. Reconcile actual usage. Never let a monitor grant external-action authority or call private tools on behalf of an anonymous user.
7. Review the new finding before replacing the current view. Historical results remain inspectable. Any downstream action needs its own current approval. No automatic production mutation or public claim update is performed by this helper.

## Example change-aware conclusion

> Earlier finding: vendor version A met requirement R using sources retrieved on date D. New finding: source S now documents version B; claim C is pending reassessment because its supporting evidence changed. Requirement applicability and migration cost remain unknown. The previous receipt is retained. No migration or purchase has been performed.

For a law or regulation, substitute a specific jurisdiction and effective date, and distinguish proposed, enacted and effective rules. Never state a rule became binding merely because a web page changed.

## Next implementation gate

Before offering scheduled monitoring, implement a durable monitor record owned by an authorized tenant, source/dependency versions, atomic budget reservation, bounded cadence/retry/concurrency, explicit retention, cancellation, failure visibility and an opt-in notification channel. Test change/no-change/unavailable/retracted/redirected sources, changed charter approvals, duplicate observations and cross-tenant isolation. Then prove that lifecycle on the deployed candidate. This increment adds none of those services and incurs no provider spending.
