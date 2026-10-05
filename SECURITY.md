# Security

Report vulnerabilities privately to the repository owner (GitHub: @EthanLofgrem) rather than in a public issue.

Design boundaries that security reports should test against:

- Adapters are read-only; the registry refuses adapters that request write or control access.
- Web discovery honours robots.txt, never logs in, and caps pages per run.
- Sensors are read only after the user confirms ownership or authorization.
- Any action with real-world effect must pass the authority engine; forbidden actions (unauthorized access, third-party satellite control, tracking private individuals, bypassing access control) are refused even when "approved".
- Research receipts are tamper-evident: `verify_receipt` detects edits. Discovery receipts likewise: `verify_discovery_receipt`.
- Production (V3) cannot act either: it imports no network client, adapter, hosted-service or authority module (certified by an import walk), writes only to a new or empty directory the user names, and its V4 handoff grants no authority. Its verifier executes an artifact's tests only after regenerating every file from the manifest and finding it byte identical, so it never runs code it did not generate; the run is a separate interpreter (`-E -s -B`) in a temporary directory with a minimal environment and a timeout.
- Discovery (V2) cannot act: it imports no writing adapter, network client or authority module (a test walks its imports), never writes to a V1 evidence graph, and outputs specifications only. Provider text never becomes evidence.
- API keys are read from environment variables and never written to receipts, logs or reports.
