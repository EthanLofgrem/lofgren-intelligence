# Always-on worker deployment

This service definition runs the existing portable worker on a Linux VM. It
provisions no machine, purchases no hosting, and contains no credentials. The
service uses a dedicated unprivileged `liworker` user, a read-only filesystem,
private temporary files, a restart limit and a 45-second SIGTERM grace period.

1. Select a VM able to reach Supabase and the configured source/model providers.
2. Build a wheel from a clean checkout of the same **full release SHA** as the
   hosted API. Record the SHA and wheel SHA-256 in the deployment evidence.
3. Create `/opt/lofgren-intelligence/releases/FULL_SHA/venv`, install the wheel
   with its `[hosted]` dependencies, and make that release readable by `liworker`.
   Point `/opt/lofgren-intelligence/current` to that exact release directory.
4. Create `/etc/lofgren-intelligence/worker.env`, readable only by root, with
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, the service's runtime-plan,
   provider and cost-rate settings, and `LI_RELEASE_SHA=FULL_SHA`.
   Obtain credentials through the host's secret manager; never commit them.
5. Install the unit in `/etc/systemd/system/`, reload systemd and start it.
   Do not run this worker as a Vercel request or a scheduled short-lived function.
6. Verify process status, then enqueue a bounded synthetic job through hosted
   MCP. Record job/receipt IDs, exact client version, completion, usage settlement
   and receipt verification. Stop/restart the worker and retrieve the result again.
   Run a cancellation and lease-reclaim test before public traffic.

The worker is a trusted service-role process. Its database key bypasses RLS;
retain the application tenant-boundary tests and never expose the key to clients.
The unit is deployment preparation; passing its syntax check is not proof of a
running worker, provider connectivity or hosted-client compatibility.

## Rollback

Stop the service, switch `current` to the previous verified release and restart.
Observe pending jobs and lease reclamation. Do not reverse applied migrations or
reuse an older worker against an incompatible job schema. Keep API and worker
release identities aligned. Existing recovery and signed-public-gate requirements
remain in force; this unit does not establish PublicMCPReady.
