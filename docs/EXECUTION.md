# V4 Execution Intelligence

V4 is the authority boundary between thinking and acting.

A V4 action can run only when the exact verified V3 artifact, the authenticated
subject, a bounded capability grant, and an explicit approval record all agree.

## Transaction

```
PLAN
  ↓
PREFLIGHT
  ↓
SNAPSHOT
  ↓
AUTHORIZE
  ↓
EXECUTE
  ↓
VERIFY
  ↓
COMMIT
```

If verification fails and the action is reversible, V4 attempts rollback and
does not create a V5 handoff.

## Authority objects

- `ActionRequest`: exact artifact, target, payload, cost and reversibility.
- `CapabilityGrant`: subject, action kind, target prefix, budget, expiry.
- `ApprovalRecord`: subject + exact action id/hash + artifact id.
- `ActionReceipt`: the execution facts, response, verification and rollback.

An approval for one payload cannot be replayed onto a modified payload.

## Adapters

The certified engine includes:

- a deterministic transactional in-memory adapter used for certification and
  embedding;
- a bounded HTTPS JSON webhook adapter for real external execution.

The webhook adapter requires public HTTPS, rejects local/private targets,
pins the resolved address for the connection, caps request/response sizes,
does not follow redirects, and sends an idempotency key.

No shell, arbitrary filesystem mutation, payments, email, deployments or
device-control capability is implicitly granted. Those require dedicated
adapters and corresponding capability grants.

## Certification

`lofgren certify --v4` proves the V4 code terms. The exact process gate is
`scripts/v4_gate.py`. V5 may start only after one pushed SHA prints
`V4ReadyForV5 = TRUE`.

V4 certification does not mean a hosted deployment has authority to act. Public
execution remains disabled until identity, storage, audit, deployment and
operator policies are certified on the hosted release.
