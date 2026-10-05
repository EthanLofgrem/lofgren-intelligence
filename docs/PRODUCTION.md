# Production Intelligence (V3)

V2 decides *what* to build and hands over a typed specification. V3 builds it: it compiles the handoff into
numbered requirements, plans which file satisfies each one, generates the artifact and its acceptance tests, runs
those tests in a separate interpreter, has an independent verifier re-derive everything, and issues a
tamper-evident receipt and a V4 handoff that grants no authority.

```
validated V2 handoff ── bound value by value to the discovery receipt and context
        ↓
specification compiler ── REQ-SPEC / REQ-OUT / REQ-ACC / REQ-CON / REQ-TST / REQ-DEP requirements
        ↓
artifact plan ── requirement → file coverage (plan.json)
        ↓
generation ── manifest, checks, README, Python implementation, acceptance tests
        ↓
independent verifier ── structure · regeneration · fingerprint · provenance · traceability
                        · re-evaluated checks · generated tests in an isolated interpreter
        ↓
production receipt (lofgren.production-receipt/1) ── V4 handoff (lofgren.v4-handoff/1, authority not granted)
```

## Running it

```bash
# Research (V1), discover (V2), then build and verify an artifact (V3), written to a new directory
lofgren produce "Is industrial construction in the Phoenix metro increasing?" --files examples/sample-sources \
  --goal "Choose a warehouse size (fictional)" --design examples/designs/warehouse.json --out-dir build/warehouse

# Re-verify that directory independently at any time (exit 0 when it verifies, 1 otherwise)
lofgren verify-artifact build/warehouse

# The artifact's own tests run anywhere Python runs; they need nothing but the standard library
cd build/warehouse && python -m unittest -v test_artifact
```

`produce` exits 2 with what discovery still needs when no candidate was selected (`infeasible`, `contradicted`,
`insufficient_evidence`, ...): there is nothing to build, and V3 never builds from an unselected candidate.

From Python:

```python
from lofgren_intelligence.production import build_artifact, verify_artifact, write_artifact
result = build_artifact(discovery.handoff, discovery_receipt=discovery.receipt, context=discovery.context)
result.artifact, result.verification, result.receipt, result.v4_handoff
write_artifact(result, "build/warehouse")
```

From an AI client over MCP (contract `lofgren.mcp/3`): `discover` → `build_artifact` (returns an `artifact_id`) →
`verify_artifact`, `get_artifact_file`, `get_production_receipt`, `create_v4_handoff`. Artifacts live for the MCP
session.

## What V3 refuses

| Input | Result |
| --- | --- |
| A handoff whose discovery receipt does not verify | refused |
| A handoff V2's validator rejects (hidden numbers, a hypothesis as evidence, an unselected candidate, ...) | refused |
| A handoff whose numbers or criteria differ from the discovery objects they cite (an edited rent, outcome, constraint or success criterion) | refused, value by value |
| A specification that does not compile (duplicate or invalid name, non-finite value, bad unit, a criterion over an undefined variable or with inconsistent units, no criteria) | refused, every problem listed |
| Any check that does not hold for the selected values | refused |
| An unsupported artifact kind | refused |

The upstream binding closes a gap V2 leaves open: V2's handoff validator checks ids, fingerprints and typing but
not that each number equals the object it cites. V3 first checks the discovery context against the object digests
in the receipt, then each specification against its assumption, scenario parameter or V1 known claim, each
expected outcome against the selected candidate's simulation, each constraint against the context, and the
`success` criterion against the design recorded in the receipt.

## Artifact kinds and files

| File | structured_bundle | markdown | python_module | What it is |
| --- | --- | --- | --- | --- |
| `artifact.json` | ✓ | ✓ | ✓ | The manifest: the handoff's typed content, numbered requirements, values and checks |
| `plan.json` | ✓ | ✓ | ✓ | Which file covers which requirement (`lofgren.artifact-plan/1`) |
| `acceptance.json` | ✓ | ✓ | ✓ | Machine-readable checks plus reference probes (`lofgren.acceptance/1`) |
| `README.md` | ✓ | ✓ | | Human-readable specification, requirements, open verification work, provenance |
| `artifact.py` | ✓ | | ✓ | The implementation: `VALUES`, `UNITS`, one generated function per check, `check_all()` |
| `test_artifact.py` | ✓ | | ✓ | Executable acceptance and integrity tests (`python -m unittest test_artifact`) |

Every file is a pure function of the manifest (`production.codegen/1`), so the verifier can regenerate the bundle
and require byte equality. Acceptance criteria and constraints are compiled from V2's structured expression trees
into plain Python arithmetic with V2's comparison tolerance; V2 expressions carry units but no conversions, and
units are checked at compile time, so the generated checks compute exactly what V2 computes.

**Reference probes.** For every variable a check reads, `acceptance.json` records that variable rescaled by 0, −1,
0.5 and 2 with each check's expected result *computed by V2's own evaluator*. The generated tests must reproduce
every expected result, including the failures, so a generated check can be neither unfaithful to V2 nor vacuously
true. The README flags any check no probe makes fail.

**Open verification work.** Test requirements and dependencies from the handoff (for example "pre-leasing
commitments for the first phase") become requirements too. They are not executable here, so they are listed as open
work in the README and the V4 handoff rather than presented as passed.

## The independent verifier

`verify_artifact(artifact)` trusts nothing it is given:

1. **Structure:** safe canonical relative paths, no duplicates, text content, SHA-256 per file, valid JSON and
   Python, and an artifact id that commits to the files and results.
2. **Re-derivation from the manifest alone:** the specification is recompiled; every file is regenerated and must
   match byte for byte; the fingerprint, provenance and source discovery must match; every requirement must be
   covered by the plan and every check by an executable test; the checks are re-evaluated with V2's evaluator.
3. **Execution:** only after step 2 passes, the generated tests run in a fresh temporary directory in a separate
   interpreter (`-E -s -B`, minimal environment, 120 s timeout). Because the code was just regenerated and
   compared, the verifier executes only code V3 itself generates. The test count must equal the expected count.

It returns problems instead of raising, for any input. `check_production_receipt(receipt, artifact)` then binds a
receipt to exactly one artifact, and `verify_directory(path)` does all of this for a directory written by
`write_artifact`, also reporting undeclared files (bytecode caches from running the tests are ignored).

## Receipt and V4 handoff

`lofgren.production-receipt/1` commits to: artifact id, fingerprint and kind; discovery id, discovery fingerprint
and evidence fingerprint; every file hash; the requirement ids; the check results; the test run (runner, count,
failures, errors); the verification result; and algorithm versions. `receipt_hash` is the hash of all of it, and the
receipt is identical for identical inputs.

`lofgren.v4-handoff/1` carries the verified artifact's id, fingerprint, kind, files and hashes, test results, the
receipt hash, open verification work, risks and dependencies. It always has `requested_actions: []`,
`authority_required: true` and `authority.granted: false`, and lists the action classes that need a V4 capability
grant and authorization: deploy, publish, purchase, send_message, modify_infrastructure, control_device and
write_external_repository. `validate_v4_handoff` refuses any handoff that requests or grants anything, or that
disagrees with its receipt or artifact.

## The boundary with V4

V3 builds and verifies. It never deploys, publishes, purchases, sends, changes infrastructure, controls a device or
writes to an external repository; committing an artifact to GitHub is a V4 action. The production package imports
no network client, adapter, hosted-service or authority module (certified by an import walk). Writing to a local
directory the user names, and running the artifact's own tests in a temporary directory, are the only effects.

See [V3 certification](V3_CERTIFICATION.md) for how each property is proven.
