# Security

Report vulnerabilities privately to the repository owner (GitHub: @EthanLofgrem) rather than in a public issue.

Design boundaries that security reports should test against:

- Adapters are read-only; the registry refuses adapters that request write or control access.
- Web discovery honours robots.txt, never logs in, and caps pages per run.
- Sensors are read only after the user confirms ownership or authorization.
- Any action with real-world effect must pass the authority engine; forbidden actions (unauthorized access, third-party satellite control, tracking private individuals, bypassing access control) are refused even when "approved".
- Research receipts are tamper-evident: `verify_receipt` detects edits.
- API keys are read from environment variables and never written to receipts, logs or reports.
