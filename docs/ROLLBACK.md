# Rollback

What to do when a deploy, a migration, or a scheduled worker goes wrong.

Record these five facts **before** any production mutation. Recovering without
them is guesswork:

- previously deployed commit SHA
- current migration revision (`python -m kalshi_research_bot.db_command status`)
- target migration revision
- most recent verified backup timestamp
- service name and environment

## Rolling back code

Railway keeps previous deployments. **Service → Deployments →** the last known
good one **→ Redeploy**. This restores the image; it does not undo a migration.

From the repository, revert and let the pipeline redeploy:

```bash
git revert <bad-commit>
git push origin Master
```

Prefer the revert when the bad commit is already merged: a Railway-side redeploy
leaves `Master` still carrying the defect, so the next unrelated merge ships it
again.

## Rolling back a migration

**Migrations are forward-only. There are no down-migrations.** Restoring a
previous schema means restoring the database, so the sequence is:

1. Stop writers: set every worker to zero replicas or disable its cron schedule.
   Leave the web service up — it is read-mostly and a maintenance page is better
   than a mystery.
2. Confirm a verified backup exists from *before* the migration. If it does not,
   stop and take a snapshot of the current state first: a bad schema you can
   still read beats a restore you cannot undo.
3. Restore into a **new** database, never over the live one.
4. Validate the restore: row counts on the largest tables, the migration
   revision, and a spot check of recent `raw.source_payloads`.
5. Repoint `DATABASE_URL` to the restored database.
6. Redeploy the previous image.
7. Re-enable workers one at a time, watching `ops.worker_status`.

Never treat an untested restore as a recovery plan. Test the restore path outside
production before you need it.

Forward-fixing is usually correct for an additive migration that merely did the
wrong thing — a new column with a bad default, a missing index. Restoring costs
data written since the migration; a corrective migration costs one deploy.

## Rolling back a scheduled worker

Converting a worker between always-on and scheduled changes no data and no
schema. It is one variable.

| To revert | Do |
| --- | --- |
| Cron → always-on | Set `HAWKNETIC_SERVICE_MODE=loop`, clear the cron schedule, redeploy |
| Always-on → cron | Set `HAWKNETIC_SERVICE_MODE=once`, set the schedule, redeploy |
| Unsure what is running | Read `HAWKNETIC_SERVICE_MODE`; unset means `loop` |

Overlap during a cutover is safe. `run_worker_once` claims a cadence-derived
idempotency key, so a cron run overlapping a still-running loop worker records
`skipped_duplicate` rather than collecting the same evidence twice.

A worker that stopped is not always a worker that failed: an unapplied migration
presents as a stopped worker, because workers run with
`DATABASE_MIGRATION_MODE=check` and fail their cycle against a schema they do not
recognise. Check migration status before assuming the service is broken.

## Rolling back the local database backend

`scripts/local.sh` picks its backend from the environment:

```bash
unset HAWKNETIC_DATABASE_URL HAWKNETIC_TEST_DATABASE_URL
```

That returns to the Compose service. `compose.yml` is retained precisely so this
fallback exists. No application code depends on which backend is in use.

## Rolling back the deployment pipeline

Removing the `RAILWAY_TOKEN` secret disables automated deployment: the gate job
emits a notice and skips, and validation keeps running. That is a clean off
switch — it does not break the build.

To disable more explicitly, set the workflow to `workflow_dispatch` only, or
disable it under **Actions → Deploy to Railway → ⋯ → Disable workflow**.

## Triage

| Symptom | First thing to check |
| --- | --- |
| `/healthz` fails | Deploy logs. The process is not starting — usually a bad variable |
| `/healthz` ok, `/readyz` 503 | Read the body. `database.ready: false` is schema or connectivity; a non-ready data gate with `database.ready: true` means no fresh evidence, which is expected on an empty database |
| A worker has no `ops.worker_status` row | It has never completed a cycle. Check the service exists and has run |
| Stale `heartbeat_at` | Stopped or crash-looping. Check `worker_cycle_crashed` in logs and the migration revision |
| Repeated `worker_cycle_crashed` | After three consecutive crashes the pool is dropped and redialled. A database in recovery produces this legitimately |
| Deploy skipped | `RAILWAY_TOKEN` unset, or validation did not conclude `success` |
| Dashboard shows stale data | The web role does not collect on a timer by design. Check the collector workers, not the web service |

## Never

- Delete the Railway database, or any volume, before a replacement is verified.
- Restore over a live database.
- Rewrite history on a shared branch to undo a deploy.
- Remove a service because it looks idle. Trace what depends on it first —
  `scripts/railway_inventory.sh` distinguishes "never ran" from "not running now".
