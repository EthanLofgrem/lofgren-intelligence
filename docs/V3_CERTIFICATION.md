# V3 certification: the gate before V4

V4 (Execution Intelligence) consumes V3's handoff. It may rely on it only once one exact, pushed commit passes
`V3ReadyForV4`, reproduced from a fresh clone.

```
V3ReadyForV4 = (every V3 code term) ∧ V3RegressionPassing ∧ PackageGatePassing ∧ GitHubCIPassing
             ∧ WorkingTreeClean ∧ ExactSHAPinned
```

Unknown is false.

## Code terms: `lofgren certify --v3`

`lofgren_intelligence/production/certification.py` runs executable scenarios over fictional fixtures (heuristic
provider, no network, no model). A scenario passes only by returning; an assertion or any crash fails it.

| Term | What must hold |
| --- | --- |
| V2HandoffValidated | a validated handoff builds; a tampered one is refused |
| ArtifactBuildDeterministic | the same handoff gives the same artifact, receipt and V4 handoff |
| ArtifactProvenanceComplete | discovery id, discovery fingerprint and evidence fingerprint carried through |
| AcceptanceCriteriaEvaluated | every acceptance criterion and evaluable constraint is checked and holds |
| ArtifactIndependentVerification | the verifier re-derives all three artifact kinds |
| ProductionReceiptReproducible | the receipt verifies, is reproduced exactly and detects edits |
| UnsafePathsRejected | traversal, absolute, Windows, empty, non-canonical and spaced paths refused |
| PythonSyntaxValidated | generated Python compiles and imports only json, pathlib, unittest and itself |
| TamperDetected | an edited file is caught |
| V4HandoffValidated | the V4 handoff grants nothing, matches the receipt, and five forged variants are refused |
| V3E2ECertificationPassing | the full journey for all kinds (warehouse) and for three more domains: software dependencies, an LP optimum, a measured sensor fact |
| V2CertifiedUpstream | all V2 code terms still hold |
| HandoffBoundToDiscovery | edited specifications, outcomes, criteria, constraints, objective, assumptions (edited or dropped), candidate and verified facts are refused (V2's validator alone accepts them) |
| SpecificationCompiled | numbered typed requirements; six malformed specifications refused |
| ArtifactPlanTraceable | every requirement covered; a plan that drops the tests is refused |
| GeneratedTestsExecuted | the generated tests run in an isolated interpreter, and a broken check makes them fail |
| ChecksFaithfulToV2 | reference probes include expected failures for every check and the generated code reproduces them |
| RegenerationVerified | edits to the README, implementation or tests are caught even with recomputed hashes and id |
| ReceiptBoundToArtifact | a receipt describes exactly one artifact |
| ArtifactDiskRoundTrip | write, reload and verify; overwrite, undeclared files and a forged receipt refused |
| V3CLIUsable | `lofgren produce` and `lofgren verify-artifact` work end to end, and an edit makes verification fail |
| V3MCPToolsTyped | the five V3 tools are typed and the whole journey runs over MCP; bad ids and kinds are errors |
| ExecutionAuthorityAbsent | the production package imports no network, adapter, hosted or authority module; authority is not granted |
| FailClosedWithoutSelection | an infeasible discovery produces nothing; a broken upstream receipt is refused |
| V3AdversarialSuitePassing | thirteen malformed or forged artifacts are refused, none crashes the verifier |

## Process terms: `scripts/v3_gate.py`

From a fresh clone, at the exact commit, with the declared dependencies installed, after CI for that commit has
finished:

```bash
git clone -c core.longpaths=true https://github.com/EthanLofgrem/lofgren-intelligence.git fresh
cd fresh
git checkout <sha>
python -m pip install ".[hosted]"
python scripts/v3_gate.py --sha <sha>
```

It runs `certify --v3`, the whole test suite in normal and UTF-8 mode (nothing may be skipped), reads GitHub
Actions for the exact commit (every required step on Python 3.10, 3.11 and 3.12, including the clean-wheel package
steps, the V3 CLI smoke test and every certification), and checks that the tree is clean before and after and that
HEAD is the requested commit. A CI run id is never evidence on its own: inside the dependent `v3-ready-for-v4`
workflow job the gate reads that run's completed matrix jobs from the API; anywhere else it finds the completed run
for the SHA.

## Tests

| File | Covers |
| --- | --- |
| `tests/test_v3_production.py` | the original V3 contract: all kinds build, verify and receipt; determinism; tampering; fail-closed upstream |
| `tests/test_v3_complete.py` | the specification compiler, code generation against V2 for every operator and tolerance, probes, regeneration, sandboxed failure, upstream binding in four domains, receipt and V4 binding, disk round trip, CLI and MCP |
