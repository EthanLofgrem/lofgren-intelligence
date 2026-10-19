# Runtime process probe: hosted acceptance requirements

Gate: hosted subprocess launch, timeout, kill and reap on Vercel, proven by
`POST /internal/runtime-probe` (`lofgren_intelligence/hosted/runtime_probe.py`)
at one exact deployed SHA. Status until a run meets every item below: **NOT RUN**.

## What the probe does

Two fixed, credential-free children of `runtime_probe_child`, no client input:

1. `launch`: starts with only `PYTHONPATH`, installs a socket audit hook,
   confirms `socket.socket()` is refused, prints a fixed JSON line, exits 0.
2. `timeout`: same startup, ignores SIGTERM and sleeps; the parent's 1 s
   deadline expires, the process group gets SIGKILL, the child is reaped.

The response is `runtime-process/1` with `passed` and the observed flags.

## Preconditions (operator)

- Deployment is a Vercel **Preview** (`VERCEL_ENV=preview`); production always
  answers 404.
- `LI_RUNTIME_PROBE_TOKEN` (32+ random characters) is set in the hosting secret
  store for that Preview only, never in chat, Git, logs or screenshots. Remove
  it after the run.
- The deployment's commit is the exact candidate SHA under review.

## Run

One request, empty body, from a trusted machine:

    POST https://<preview-host>/internal/runtime-probe
    Authorization: Bearer <token from the secret store>
    Content-Length: 0

## PASS requires all of

| Field | Required |
|---|---|
| HTTP status | 200 |
| `probe` | `runtime-process/1` |
| `passed`, `launch`, `network_denied`, `clean_environment`, `timeout`, `kill`, `reap`, `pid_absent` | all `true` |
| `release_sha` | equals the candidate SHA (40 hex) |
| `elapsed_ms` | below 5000 |
| `Cache-Control` | `no-store` |
| A second identical request to the same warm instance | 429 `probe_already_used` |

Negative checks on the same deployment, each before the positive run:

| Request | Required |
|---|---|
| no `Authorization` | 401 |
| wrong token | 401 |
| non-ASCII bytes in `Authorization` | 401 (fixed on `build/li-probe-auth-bytes`; 5b6ebf9 raises a server fault) |
| non-empty body or `Transfer-Encoding` | 400 |
| same route on the production deployment | 404 |

Any other status, a missing field, a different SHA, or a 503 is **FAIL**.

## Evidence record (no secrets)

Deployment id and URL, `release_sha`, UTC timestamp, the full JSON body, the
response headers, each negative-check status, who ran it and from where. Never
record the token or the `Authorization` header.

## What a PASS does not prove

- The audit hook is a Python-level self-check, not an operating-system sandbox;
  native code or a different interpreter could open sockets.
- `_USED` is per warm instance, not a distributed quota; a new instance allows
  another attempt.
- It proves the runtime can launch, bound, kill and reap one fixed child. It
  does not prove trial isolation under real research load, provider calls,
  concurrency across instances, or anything about the Render worker.
- A token being configured, or `/readyz` answering, is not a PASS.
