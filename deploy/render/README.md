# LI staging worker deployment

Prepared for Render workspace `tea-db3i5o7avr4c739tg4lg` (Lofgren Intelligence).
Create a new worker; do not convert or redeploy the failed Docker web service.

Application revision: `e232ced165a31ab66124decb1f0fe9ea4b9057ac`.
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

## Repeatable offline verification

The deployment branch's root `render.yaml` is authoritative. The copy at
`deploy/render/worker.yaml` is a review fixture, not another deployed service.
From a verification environment with PyYAML installed, run:

```sh
python scripts/verify_worker_deployment.py deploy/render/worker.yaml \
  --application-sha e232ced165a31ab66124decb1f0fe9ea4b9057ac
```

The script makes no network requests and performs no deployment. It rejects
an unpinned application, duplicate environment keys, inline service credentials,
external execution, automatic deployment, and unsafe heartbeat/shutdown timing.
A PASS is configuration proof only. Render schema validation and actual host
execution remain separate evidence.

## Shutdown limit and recovery

SIGTERM stops new claims and signals active jobs to stop at the next cooperative
stage boundary. The application waits up to 25 seconds for that boundary.
This is **not a guaranteed process-exit deadline**: Python executor threads can
remain alive while blocked work finishes. Render's configured 45-second
shutdown allowance supplies the eventual forced-termination boundary.

After forced termination, a new worker must reclaim expired leases, reconcile
saved results and actual usage, and avoid replaying committed outputs. Test a
termination during active work on staging before claiming graceful shutdown or
recovery. The 120-second lease and five-second default polling interval imply a
reclaim delay; they do not promise an exact recovery time during host outages.

## Budget and evidence boundaries

Worker hosting is a recurring infrastructure cost, separate from user pricing,
model-provider charges, search charges and bandwidth. No paid resource is
created by this configuration or verifier. Keep provider keys unset for the
initial approved synthetic case; record findings as rule-based where applicable.

No public-release date is established by a worker build. Record the API SHA,
worker application SHA, configuration SHA, dependency-lock hash and Render deploy
ID separately. Validate results, receipts, usage settlement and reconnect before
moving the hosted-execution requirement from NOT RUN to PASS.
