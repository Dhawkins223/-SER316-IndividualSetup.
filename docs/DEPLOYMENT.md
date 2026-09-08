# Deployment

How a reviewed commit reaches production, and how to change what runs there.

Read `TARGET_INFRASTRUCTURE.md` for *why* the architecture is shaped this way.

## The pipeline

```text
push / pull request
   -> PostgreSQL validation  (lint, wheel, migrations, 1053 tests, 40 browser checks)
   -> Dependency and secret scanning
   -> [Master only] Deploy to Railway
        -> web service first  (carries the pre-deploy migration)
        -> worker services
   -> post-deploy /healthz probe
```

`deploy.yml` triggers on `workflow_run`, not on `push`. It refuses any run whose
validation conclusion was not `success`, so production cannot advance from a red
check. A `push` trigger could not express that ordering.

If `RAILWAY_TOKEN` is absent the deploy job emits a notice and skips. That is
deliberate: an unconfigured repository should not show a red merge for a step
nobody has opted into.

## One-time setup

Nothing below is done for you; all of it needs account access.

### 1. Railway deploy credentials

In Railway: **Project → Settings → Tokens**, create a project token scoped to the
production environment. In GitHub: **Settings → Secrets and variables → Actions**.

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `RAILWAY_TOKEN` | The project token |
| Variable | `RAILWAY_WEB_SERVICE` | Exact name of the web service |
| Variable | `RAILWAY_WORKER_SERVICES` | Space-separated worker service names |
| Variable | `PRODUCTION_HEALTHCHECK_URL` | `https://<host>/healthz` (optional; skipped if unset) |

Service names must match Railway exactly — `railway up --service` takes the name.
`scripts/railway_inventory.sh` prints them.

### 2. Connect production to the repository

Recorded as disconnected since 2026-08-03. While that holds, neither
config-as-code nor the pre-deploy migration is applied, and **a merged migration
reaches the database only when someone runs it by hand.**

Either connect the service to the repository in Railway (**Service → Settings →
Source**), enabling "Wait for CI" so Railway does not deploy ahead of validation,
or leave it disconnected and let `deploy.yml` push builds. Do not do both — two
deploy paths racing on the same service produce an undefined winner.

### 3. Per-service configuration

Every service builds the same repository and picks its role from variables.

| Service | `HAWKNETIC_SERVICE` | `HAWKNETIC_SERVICE_MODE` | Config path | Schedule |
| --- | --- | --- | --- | --- |
| web | `web` | — | `railway.json` | always-on |
| kalshi ingestion | `kalshi-market-ingestion` | `loop` | `railway.worker.json` | always-on |
| external sources | `external-source-ingestion` | `loop` | `railway.worker.json` | always-on |
| crypto research | `crypto-research` | `loop` | `railway.worker.json` | always-on |
| sports research | `sports-research` | `once` | `railway.worker.json` | `0 * * * *` |
| model refresh | `research-model-refresh` | `once` | `railway.worker.json` | `0 * * * *` |
| settlement | `settlement-worker` | `once` | `railway.worker.json` | `0 * * * *` |
| raw retention | `raw-retention` | `once` | `railway.worker.json` | `0 * * * *` |
| reporting | `reporting-evaluation` | `once` | `railway.worker.json` | `0 */6 * * *` |

Set the config path under **Service → Settings → Config as code**. Only the web
service may use `railway.json`: it is the one that carries the pre-deploy
migration, and a worker inheriting it would run `database-migrate` on every
deploy and fail to deploy at all whenever the database was briefly unavailable.

Workers run with `DATABASE_MIGRATION_MODE=check`. A worker facing an unmigrated
schema fails its cycle and backs off, which is recoverable.

## Converting a worker to a scheduled service

The cutover is safe in either order because `run_worker_once` claims a
cadence-derived idempotency key: an overlapping loop worker and cron run make the
second record `skipped_duplicate` rather than collect twice.

1. Set `HAWKNETIC_SERVICE_MODE=once` on the service.
2. Set the cron schedule under **Service → Settings → Cron Schedule**.
3. Deploy.
4. Confirm one clean run: the deploy log ends with `worker_succeeded` and the
   container exits 0.
5. Confirm the heartbeat advanced:

   ```sql
   SELECT worker_name, status, consecutive_failures, last_error_code, heartbeat_at
   FROM ops.worker_status ORDER BY worker_name;
   ```

Reverting is one variable: set `HAWKNETIC_SERVICE_MODE=loop`, clear the schedule,
redeploy. No data or schema change is involved in either direction.

Railway does not start a new cron run while the previous one is still going, so a
cycle that overruns its schedule delays the next rather than overlapping it.

## Migrations

Forward-only SQL in `migrations/postgres/`, applied by the web service's
pre-deploy command:

```
PYTHONPATH=src python -m kalshi_research_bot database-migrate
```

It may run migrations **only** — never seed, collect, start workers, train
models, alter safety flags, or reset data. Application is serialized by an
advisory lock, so concurrent deploys cannot both apply, and CI proves both the
idempotence and the serialization on every run.

Check state without applying:

```bash
python -m kalshi_research_bot.db_command status
```

## Verifying a deploy

| Endpoint | Meaning |
| --- | --- |
| `/healthz` | The process answers. `status: ok` |
| `/readyz` | PostgreSQL reachable, migrations applied, source data fresh |

`/readyz` returning 503 with `"database": {"ready": true}` and a non-ready data
gate is a *correct* response on a fresh database with no collected evidence yet —
CI asserts exactly that. It means the schema is fine and no data has arrived.

Then confirm every deployed worker has a recent `heartbeat_at` in
`ops.worker_status`. A worker with no row has never completed a cycle; a stale
heartbeat means it stopped. An unapplied migration presents as a stopped worker,
so check migration status before concluding a service is broken.

## Local development

Two backends. Compose is the default; neither needs a change to application code.

```bash
# Docker-backed (default)
./scripts/local.sh dev

# No Docker daemon: a managed development database, e.g. two Neon branches
export HAWKNETIC_DATABASE_URL='postgresql://.../dev'
export HAWKNETIC_TEST_DATABASE_URL='postgresql://.../dev_test'
./scripts/local.sh test
```

Both URLs are required and must name different databases — `test` writes to the
test database. A Railway or Render host is refused unless
`HAWKNETIC_ALLOW_HOSTED_DATABASE` is set. Never point either at production.

## Rules

- Production must not advance from an unreviewed branch or a failing check.
- Never place credentials in tracked files. Railway Variables, GitHub Secrets, or
  Codespaces Secrets only.
- Never point a Codespace, a test process, or CI at production. `ci.yml` asserts
  it holds no Railway credentials.
- The pre-deploy command runs migrations only.
- Verify a replacement before removing what it replaces.
