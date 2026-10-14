# Explicit public-source refresh

This operator CLI refreshes a bounded public watchlist using LI's existing SSRF-safe source adapter. It performs no scheduling, paid provider/model calls, code installation, policy changes or external actions. Retrieval is **offline by default**. A changed page triggers review; it never establishes that a law changed or is binding.

Create `watch.json`:

```json
{"schema":"lofgren.source-watch/1","sources":[{"url":"https://example.org/rules","jurisdiction":"Arizona","effective_date":"2026-10-12"}]}
```

Preview without networking:

```bash
python -m lofgren_intelligence.source_watch watch.json
```

Explicitly refresh, saving each new report separately:

```bash
python -m lofgren_intelligence.source_watch watch.json --fetch > snapshot-001.json
python -m lofgren_intelligence.source_watch watch.json --previous snapshot-001.json --fetch > snapshot-002.json
```

Never overwrite the previous report: retained snapshots preserve earlier hashes and operator metadata. Reports contain source URLs; use appropriately restricted storage. `jurisdiction` and `effective_date` are unverified operator assertions, not extracted legal conclusions.

Inputs are capped at 256 KB and 25 sources. Each source uses a five-second HTTP timeout with no retries, response-size ceilings, public DNS/address checks, pinned connections and revalidated bounded redirects inherited from the protected fetcher. Redirects may cause additional GET requests. These per-request bounds are not a guarantee of total elapsed time or a scheduling budget.

Successful refreshes distinguish first observation, unchanged bytes and changed bytes. Unavailable sources preserve the last successful hash and remain review-required. Metadata changes require review even if bytes are identical. Removed watchlist URLs are listed; this does not mean those sources were deleted. An offline preview does not refresh evidence.

The command prints a new report and does not modify input files. Operators must review applicability, dates, authority, source corrections and dependent claims before creating a new finding or approving action. It is not an MCP tool, tenant-aware hosted monitor, legal updater or freshness guarantee. Hosted monitoring still needs durable ownership, budgets, cadence, retention and deployed acceptance proof.
