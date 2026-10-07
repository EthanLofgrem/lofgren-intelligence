# Hosted public sources (Q32)

Hosted MCP source parameters are `europepmc` (literature query), `trials`
(ClinicalTrials.gov query), `max_records` (integer 1–20 per adapter, default 20),
and `sources_manifest` (an inline JSON object using `lofgren.sources-manifest/1`).
Server-local manifest paths are rejected. Operator-selected manifests accept at
most 12 public URLs; the existing protected fetcher also checks redirects and DNS.

Pass these parameters to `clarify_objective` / `plan_research` and, once the
server-side charter is approved, use `start_research` with an idempotency key.
Poll `get_job_status` and retrieve the saved result and receipt. Source settings
are included in the charter hash and frozen job input. Each registry adapter has
at most two pages, 15-second fetch timeouts, and one retry. Public-source tasks
are refused by synchronous investigation: they require the durable worker.

Literature remains abstract-only. Registered trials are not efficacy evidence;
registry results are not fetched. Operator-selected URLs remain labelled as such.
The adapters' retraction, correction, partial-retrieval and provenance handling
is preserved. These sources do not configure a reasoning model or establish
clinical, legal or financial authority.

## Release verification

The worker regression replays a recorded literature fixture through the hosted
queue and saves a completed run. Additional tests enforce query/record limits,
inline manifest intake, source-sensitive charter hashes and refusal of inline
network research. This is local integration evidence, not a deployed-client proof.

On 2026-10-07 Q30 was applied to LI Development (`pzoxinhycjquaalflbwe`).
Migration history records `20261006090000_li_revoke_public_defaults`; the recorded
SQL MD5 equals the repository file (`416b87cce48268d6c57af552a658a1e4`).
All three LI sequences deny USAGE, SELECT and UPDATE to `anon` and `authenticated`
and retain all three privileges for `service_role`. Production and hosted-worker
verification remain separate requirements.
