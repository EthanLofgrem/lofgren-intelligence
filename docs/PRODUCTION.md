# V3 Production Intelligence

V3 consumes only a validated `lofgren.v3-handoff/1` from certified V2. It turns
that selected candidate into a deterministic, inspectable artifact bundle and
refuses to grant execution authority.

## Supported artifact forms

The initial certified production domain is deliberately bounded:

- `structured_bundle`: JSON implementation manifest + Markdown + Python representation;
- `markdown`: human-readable implementation artifact plus typed JSON manifest;
- `python_module`: importable Python representation plus typed JSON manifest.

Unsupported artifact kinds fail closed rather than pretending a generic model
output is a verified build.

## Production pipeline

```
validated V2 handoff
        ↓
typed specifications / outcomes / criteria
        ↓
deterministic artifact generation
        ↓
machine-evaluable acceptance criteria
        ↓
independent file/hash/JSON/Python verification
        ↓
artifact fingerprint
        ↓
tamper-evident production receipt
        ↓
V4 handoff (authority_required = true)
```

V3 never deploys, sends, purchases, publishes, modifies production state, or
controls a device. Those actions belong to V4 and require a capability grant
and authorization.

## Identity and provenance

Each artifact binds to:

- its V2 discovery id;
- the discovery fingerprint;
- the evidence fingerprint inherited by the V2 handoff;
- file paths and SHA-256 content hashes;
- machine-evaluated acceptance results.

The production receipt commits to the artifact fingerprint, upstream discovery,
file hashes, acceptance results, verifier result, and algorithm versions.

## Certification

`lofgren certify --v3` proves the V3 code terms offline. The exact-SHA process
gate is `scripts/v3_gate.py` and may print `V3ReadyForV4 = TRUE` only when
the full regression suite, package gate, CI, clean-tree and exact-SHA terms are
also true.

A green V3 gate means the bounded V3 production contract is certified. It does
not authorize V4 execution and does not establish public hosted readiness.
