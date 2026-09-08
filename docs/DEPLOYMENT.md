# Deployment

How this project gets from a commit to production, what runs where, and what to
check when it does not. For recovery, see `docs/ROLLBACK.md`. For why the
architecture is shaped this way, see `docs/TARGET_INFRASTRUCTURE.md`.

Related, more specific documents: `docs/deployment-readiness-checklist.md` for
the pre-release gate, `docs/railway-postgresql-deployment-and-rollback.md` for
database-specific procedure, and `docs/environment-variables.md` for the full
variable inventory.

## The path

```
commit → push to Master → GitHub
                            │
                            ├── GitHub Actions: lint · wheel · migrations · 1047 tests · browser checks
                            │
                            └── Railway: build (Railpack) → pre-deploy migration → start
```

Both branches start on the same push. **Railway does not wait for CI** — every
service has `checkSuites: false`, so a red build does not stop a release. Fixing
that is a dashboard setting, per [Wait for CI](#turning-on-wait-for-ci) below.

## Services

All in Railway project `jubilant-liberation`, environment `production`,
region `iad`, all deploying `Dhawkins223/HawkNeticSportsTools` on `Master`.

| Service | Role | Config file | `HAWKNETIC_SERVICE` | Cadence |
| --- | --- | --- | --- | --- |
| `HawkNeticSportsTools` | Web dashboard | `railway.json` | `web` | always on |
| `KalshiIngestionProduction` | Market collector | `railway.worker.json` | `kalshi-market-ingestion` | 300 s |
| `SportsResearchProduction` | Sports research | `railway.worker.json` | `sports-research` | 3600 s |
| `SettlementWorkerProduction` | Settlement import | `railway.worker.json` | `settlement-worker` | 3600 s |
| `RawRetentionProduction` | Payload prune, capacity watch | `railway.worker.json` | `raw-retention` | 3600 s |
| `Postgres-gxQB` | Application database | — | — | always on |

A worker's role comes entirely from `HAWKNETIC_SERVICE`. All of them run the
same image and the same start command; only the variable differs.

Only the web service carries `railway.json`, and only `railway.json` declares a
pre-deploy migration. Workers point at `railway.worker.json` precisely so that
they do not each try to migrate the database on every deploy.

### Roles that exist in code but have no service

`external-source-ingestion`, `crypto-research`, `research-model-refresh` and
`reporting-evaluation` are defined in `SERVICE_SPECS` and have no Railway
service. They are runnable locally with `worker --service <name> --once`.
Deploying one means creating a service, pointing it at `railway.worker.json`,
and setting `HAWKNETIC_SERVICE`. Budget about $0.40/month each.

## Migrations

Migrations are versioned, forward-only, and live in `migrations/postgres`
(currently 0001-0016). They run as the web service's **pre-deploy command**:

```
PYTHONPATH=src python -m kalshi_research_bot database-migrate
```

Consequences worth knowing:

- A failing migration fails the deploy, and the previous version keeps serving.
- A database that is unreachable also fails the deploy. This is what the
  2026-09-01 outage looks like from the deploy log:
  `failure stage: PRE_DEPLOY_COMMAND`.
- Workers run `DATABASE_MIGRATION_MODE=check` and refuse to start against a
  database missing a version they need, so a merged-but-unapplied migration
  crash-loops every worker. `/internal/status.json` reports this as
  `pending_migrations`, distinct from `database_failure`.
- There is no down-migration. Reversing a migration means restoring a backup.

Volumes are not mounted during pre-deploy, so a pre-deploy command must never
read or write the volume.

## Health and readiness

| Endpoint | Meaning |
| --- | --- |
| `/healthz` | Process is up. `{"status": "ok"}` |
| `/readyz` | Serving-ready: database reachable, migrations applied, data fresh. `503` while any gate is unmet |
| `/internal/status.json` | Workers, heartbeats, migration state, `storage`, and `anomalies`. Never publicly exposed |

`/readyz` returning `503` with `database.ready: true` and `data_gate` not ready
is a healthy cold start, not a fault — the collectors have not yet produced
fresh data. CI asserts exactly this shape.

### Storage anomalies

`/internal/status.json` carries a `storage` block and raises a
`database_capacity` anomaly at 75% of `DATABASE_VOLUME_CAPACITY_BYTES`
(warning) and 90% (critical). A critical anomaly also drops the top-level
status to `degraded`. This exists because the database filled its volume to
100% while reporting its own size every hour and nothing ever compared that
number to the ceiling. Treat a critical capacity anomaly as an outage in
progress: a full volume stops PostgreSQL and then blocks its own recovery.

The measurement covers the **volume** — every database in the cluster plus
`pg_ls_waldir()` — not `pg_database_size()` of one database, because WAL is
what filled it. The block reports `cluster_bytes` and `wal_bytes` separately, so
a rising ratio says whether to prune rows or to look at WAL recycling. Reading
WAL needs superuser or `pg_monitor`; without it `wal_measured` is `false` and
the figure is a floor rather than the truth.

## Configuration precedence, and the drift to be aware of

Four files in this repository describe how to start the application, and they do
not agree:

| File | Start command | Used by |
| --- | --- | --- |
| `railway.json` | `service-start`, healthcheck `/healthz`, pre-deploy migrate | the web service |
| `railway.worker.json` | `service-start`, no pre-deploy | the four workers |
| `Procfile` | `paper … --refresh-seconds 900` | nothing on Railway |
| `nixpacks.toml` | `paper … --refresh-seconds 900` | nothing (the builder is Railpack) |

The live web service additionally has a dashboard start command of
`paper --host 0.0.0.0 --port ${PORT:-8000}` with no healthcheck, while its public
domain routes to **port 8080** and `PORT` is not among its variables.

None of this was changed during the 2026-09-08 audit: production is down, and a
start-command change that cannot be verified against a running system is a
change made blind. It is recorded here because the next person to deploy the web
service should reconcile it deliberately — `railway.json` is the file that
should win, and the port mismatch should be resolved by setting `PORT=8080` or
by pointing the domain at the port the process actually binds.

## Environment variables

Full inventory in `docs/environment-variables.md`; CI fails if a key in
`.env.example` is missing from it.

Never in the repository, always in Railway Variables:
`DATABASE_URL`, `DATABASE_MIGRATION_URL`, `DASHBOARD_AUTH_PASSWORD`, and any
provider credential.

The safety flags below are `false` in production and asserted `false` by CI.
They are what makes this a research-only system:

```
RESEARCH_ONLY=true
LIVE_EXECUTION_ENABLED=false
AUTO_TRADE_ENABLED=false
AUTO_UPLOAD_ENABLED=false
KALSHI_ORDER_UPLOAD_ENABLED=false
MODEL_PROMOTION_ENABLED=false
```

## Turning on Wait for CI

Railway's GitHub integration currently deploys on push without regard to check
status. To gate it, for each of the five services:

> Railway dashboard → `jubilant-liberation` → `production` → service →
> **Settings** → **Source** → enable **Wait for CI**

After that a failing `PostgreSQL validation` workflow blocks the deploy.

This is a dashboard setting rather than something the repository can assert,
and it is not available through the Railway API used by this audit.

The alternative — deploying from a GitHub Actions job with a `RAILWAY_TOKEN` —
is deliberately rejected. This repository is **public**, and the CI workflow
asserts that no Railway credential exists in it:

```yaml
- name: Verify CI cannot target hosted infrastructure
  run: |
    test -z "${RAILWAY_TOKEN:-}"
    test -z "${RAILWAY_API_TOKEN:-}"
```

Keep that property. Use Wait for CI instead.

## Deploying by hand

Railway builds from GitHub, so the normal path is a push. To redeploy without a
new commit, use **Redeploy** on the service in the dashboard, or:

```bash
railway up --project dfc58505-d45f-4093-8050-35f5371bbf37 \
           --environment cd5e7bc2-b6e5-4c1a-a442-8e1a2b9cb64a \
           --service <service-id>
```

A service that has never deployed cannot be started by redeploy — it needs
`railway up` or a repository connection made in the dashboard.

## Pre-deploy checklist

1. `python -m ruff check .`
2. `./scripts/local.sh test` — the full suite, 1047 tests
3. `./scripts/local.sh verify` — configuration, migrations, tests, smoke
4. Confirm any new `.env.example` key is in `docs/environment-variables.md`
5. Confirm new migrations apply twice cleanly (CI does this; it catches
   non-idempotent migrations)
6. Check `/internal/status.json` for a `database_capacity` anomaly before adding
   a collector — a new source shortens the volume runway
