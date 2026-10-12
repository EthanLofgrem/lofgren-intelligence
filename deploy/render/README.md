# LI staging worker deployment

Prepared for Render workspace `tea-db3i5o7avr4c739tg4lg` (Lofgren Intelligence).
Create a new worker; do not convert or redeploy the failed Docker web service.

Application revision: `b28cdb8a6c412d2c976e7eb9a4b97ad6c345ab4e`.
The Blueprint commit is configuration identity, not application identity.
The build explicitly fetches/checks out the application revision and verifies it
before installing dependencies from its frozen uv.lock.

One Starter worker (512 MB), approximately $7/month, subject to Render's
creation screen and current pricing. No database, Redis, disk, or other service
is provisioned. Automatic deployment is off. Paid creation requires owner
approval of the displayed recurring cost.

## Creation
1. Use Render New Blueprint, repository EthanLofgrem/lofgren-intelligence,
   branch deploy/li-staging-worker, root render.yaml.
2. Verify workspace, one worker, plan and total cost before creation.
3. Enter SUPABASE_SERVICE_ROLE_KEY directly into Render's secret field.
   Never place it in this file, source control, chat, screenshots or logs.
4. Configure the same private key in the dedicated Vercel project's Preview
   environment and redeploy the same application SHA.
5. Leave external execution disabled and provider keys unset for the first
   bounded synthetic hosted case. Provider-backed quality is not established.
   Align any subsequently approved provider, runtime-plan and cost settings
   between API and worker before using them.

## Acceptance evidence
Record Render service/deploy identity, actual build checkout SHA, worker start
and lease activity, API readyz, persisted approval/job, findings, receipt and
usage settlement. Retrieve the result after disconnecting. Exercise worker
termination/reclaim, replay, competing workers, cancellation, provider failure,
expired/stale authorization and cross-tenant denial before claiming readiness.

## Containment and rollback
Suspend the new worker to stop new claims; account for in-flight work and lease
expiry. Do not delete cases or reverse migrations. For a code rollback, use an
explicit previously verified compatible SHA and matching API revision. Do not
reenable automatic deployment or external execution as a workaround.

This configuration is deployment preparation, not proof of hosted execution,
real client compatibility, public readiness or medically reliable research.
