# Current Infrastructure

Measured against the live Railway account on **2026-09-08**. Every number here
came from Railway's metrics and deployment APIs rather than from configuration,
because the deployed system and the repository had drifted apart in ways that
only measurement showed.

Read `docs/TARGET_INFRASTRUCTURE.md` for where this is going and why, and
`docs/INFRASTRUCTURE_COSTS.md` for the arithmetic behind the money.

## Headline: production is down, and has been since 2026-09-01

The production database filled its 5 GB volume and PostgreSQL took itself down
mid-write:

```
2026-09-01 22:48:31 UTC [16755] PANIC:  could not write to file "pg_wal/xlogtemp.16755": No space left on device
2026-09-01 22:48:32 UTC [16771] FATAL:  could not write to file "pg_wal/xlogtemp.16771": No space left on device
2026-09-01 22:48:32 UTC [27]    LOG:  startup process (PID 16771) exited with exit code 1
2026-09-01 22:48:32 UTC [27]    LOG:  shutting down due to startup process failure
```

That second line is the one that matters. WAL recovery needs to write, and a
volume at 100% has nowhere to write, so the database could not restart itself.
Its container was stopped on 2026-09-04 and everything downstream followed:

| Symptom | Where | Since |
| --- | --- | --- |
| `PANIC: No space left on device`, cannot recover | `Postgres-gxQB` | 2026-09-01 |
| Deploy fails at `PRE_DEPLOY_COMMAND` (`database-migrate`) | `HawkNeticSportsTools` | every deploy since 2026-09-02 |
| `failed to resolve host 'postgres-gxqb.railway.internal'` | `HawkNeticSportsTools` | 2026-09-04 |
| `worker_cycle_crashed … database_connection_failed`, restarting forever | all three production workers | 2026-09-04 |

The three workers report deployment status `SUCCESS`. They are not healthy —
they are crash-looping. A `SUCCESS` deployment means the container started, not
that the process inside it is doing anything.

### Update, 2026-09-09: everything is now stopped

Re-measured a day later. The crash-looping ended, and not by recovering:

| Service | Then (2026-09-08) | Now (2026-09-09) |
| --- | --- | --- |
| `SportsResearchProduction` | crash-looping | stopped 16:18:16 UTC, deployment `REMOVED` |
| `SettlementWorkerProduction` | crash-looping | stopped 16:18:17 UTC, deployment `REMOVED` |
| `RawRetentionProduction` | crash-looping | stopped 16:18:18 UTC, deployment `REMOVED` |
| `HawkNeticSportsTools` | failing every deploy | unchanged, last deployment still `FAILED` |
| `Postgres-gxQB` | stopped, cannot restart | unchanged, 4.99 GB still on the volume |
| `postgres` (`ravishing-elegance`) | idle, 0.046 GB RAM | 0 GB RAM — stopped as well |

The workers went down cleanly rather than crashing — a SIGTERM, then
`worker_stopped` in their own logs, after `consecutive_crashes` reached 11.
All three within 1.5 seconds, which makes it one action rather than three
failures. Whether that was the account owner or Railway reclaiming a workload
that had been failing for 40 minutes is not visible from the API.

**Nothing in either project is running now.** Every compute metric reads 0 and
only volumes remain, which bills at:

| Volume | Used | Monthly |
| --- | ---: | ---: |
| `Postgres-gxQB` | 4.995 GB | $0.75 |
| `Postgres` (staging) | 4.995 GB | $0.75 |
| `Postgres-GDG0` (staging) | 4.987 GB | $0.75 |
| `HawkNeticSportsTools` `/data` | 0.769 GB | $0.12 |
| `postgres` (`ravishing-elegance`) | 0.184 GB | $0.03 |
| **Total** | **15.93 GB** | **$2.39** |

That is under Hobby's $5 included usage, so the bill is now just the $5
subscription — the cheapest this account has been, and only because none of it
works. It also means the two obsolete staging databases now cost $1.50/month
rather than the $25.38/month they cost while running: deleting them is still
right, but it is no longer the urgent saving. Recovering the production
database is.

One operational consequence: a `REMOVED` deployment does not restart when its
dependency comes back. The workers will not return on their own once PostgreSQL
is running — they have to be redeployed. `docs/ROLLBACK.md` step 3 covers it.

## Providers in use

| Provider | Role today | Authenticated for this audit |
| --- | --- | --- |
| GitHub | Source of truth, CI, Railway deploy trigger | yes |
| Railway | All compute and all PostgreSQL, two projects | yes |
| Cloudflare | none | no credentials available |
| Neon | none | no credentials available |
| Render | none | no credentials available |

Cloudflare, Neon and Render hold no resources for this project, so nothing had
to be read from them to map what exists. `docs/TARGET_INFRASTRUCTURE.md` records
whether each one should.

## Railway: two projects, fifteen services

### `jubilant-liberation` — this repository

**production** (`cd5e7bc2-b6e5-4c1a-a442-8e1a2b9cb64a`)

| Service | Purpose | Runtime | RAM (7d avg) | CPU (7d avg) | Volume | State |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `HawkNeticSportsTools` | Web dashboard, `/healthz`, `/readyz` | Python 3.12, Railpack | 0.206 GB | — | 5 GB provisioned, 0.77 GB used, `/data` | **failing every deploy** |
| `SportsResearchProduction` | `sports-research` worker, hourly | Python 3.12 | 0.039 GB | 0.00010 vCPU | none | crash-looping |
| `SettlementWorkerProduction` | `settlement-worker`, hourly | Python 3.12 | 0.041 GB | 0.00010 vCPU | none | crash-looping |
| `RawRetentionProduction` | `raw-retention`, hourly | Python 3.12 | 0.036 GB | 0.00007 vCPU | none | crash-looping |
| `KalshiIngestionProduction` | `kalshi-market-ingestion`, 5-minutely | Python 3.12 | — | — | none | **never deployed** |
| `Postgres-gxQB` | Application database | `postgres-ssl:18` | 0.908 GB | 0.00023 vCPU | 5 GB provisioned, **4.99 GB used (99.9%)** | **stopped, cannot restart** |

**staging** (`14f937a9-34e4-4720-afc4-509e910c64dc`)

| Service | RAM (7d avg) | Volume | Last deployment |
| --- | ---: | ---: | --- |
| `Postgres` | 2.46 GB while running | 5 GB provisioned, **4.99 GB used** (us-west2) | stopped 2026-09-04 |
| `Postgres-GDG0` | 0 (stopped) | 5 GB provisioned, **4.99 GB used** (iad) | stopped |
| `HawkNeticResearchStaging` | — | none | **FAILED 2026-07-13** |
| `SportsResearchStaging` | — | none | **FAILED 2026-08-16** |
| `KalshiIngestionStaging` | — | none | **never deployed** |

The staging environment also carries a staged, never-applied change patch of
**122 changes**.

### `ravishing-elegance` — a different product

This project does not build this repository. Both of its application services
deploy `Dhawkins223/hawknetic-office`, a Node/pnpm monorepo with its own auth,
payments and Redis configuration.

| Service | Source | RAM (7d avg) | Volume | State |
| --- | --- | ---: | ---: | --- |
| `hawknetic-office` | `hawknetic-office`, `main` | — | none | FAILED 2026-09-05 |
| `hawknetic-workers` | `hawknetic-office`, `main` | — | none | FAILED 2026-08-26 |
| `postgres` | `postgres-ssl` image | 0.046 GB | 0.18 GB used | idle |
| `redis` | Redis image | — | none | never deployed |

It is listed because it spends from the same Railway subscription. Nothing in
this audit changes it: its source repository was not available here, so its
services cannot be verified, and an unverified service is not one to delete.

## Three databases, all full, none serving

The single most important structural fact about this account:

| Database | Environment | Region | Used | Capacity | Serving |
| --- | --- | --- | ---: | ---: | --- |
| `Postgres-gxQB` | production | iad | 4.994 GB | 5 GB | no |
| `Postgres` | staging | us-west2 | 4.995 GB | 5 GB | no |
| `Postgres-GDG0` | staging | iad | 4.987 GB | 5 GB | no |

Three PostgreSQL instances, each independently grown to within 13 MB of the
same 5 GB ceiling. The repository's own audit
(`docs/railway-volume-storage-audit.md`, 2026-07-25) recorded the production
volume at 778 MB and a staging volume at 341 MB. Both reached the ceiling in the
six weeks after.

5 GB is not an arbitrary number: it is the volume size Railway's **Hobby** plan
provisions, and the plan's ceiling. Growing past it requires the Pro plan.

## Why it filled: the retention window could never bite

`raw.source_payloads` stores one full response body per collection cycle.
`docs/raw-payload-retention.md` measured the growth at roughly **166 MB/day** of
payload bodies against **230-280 MB/day** of total database growth, and worked
out what that implies:

```
steady_state_size = daily_growth x window_days
```

At 166 MB/day a thirty-day window wants ~5.0 GB of payload bodies alone, on a
volume that holds 5 GB in total. The window was therefore never reachable: rows
never aged into eligibility, every retention pass pruned nothing, and the volume
filled anyway. A window wider than the data's own age is indistinguishable from
having no retention at all, which is what `window_bites: false` was added to
report.

The shipped defaults made this the expected outcome rather than an accident:

| Setting | Value before this audit | Implied steady state | Fits 5 GB? |
| --- | ---: | ---: | --- |
| `DEFAULT_RETENTION_DAYS` (code) | 30 | ~5.0 GB payloads | no |
| `RAW_RETENTION_DAYS` (`.env.example`) | 45 | ~7.5 GB payloads | no |
| Production's documented setting | 10 | ~1.7 GB payloads | yes |

And nothing escalated. The retention worker reported `database_bytes` on every
cycle while that number climbed to the ceiling; the value was recorded and never
compared against anything.

## Workload inventory

| # | Component | Class | Where it runs | Cadence |
| --- | --- | --- | --- | --- |
| 1 | Dashboard / `/healthz` / `/readyz` | HTTP API + server-rendered UI | Railway `HawkNeticSportsTools` | always on |
| 2 | Kalshi market collection | Scraper / ingestion | **the web service's refresh**, not the worker named for it | 300 s |
| 3 | `external-source-ingestion` | Scraper / ingestion | not deployed | 900 s |
| 4 | `crypto-research` | Batch compute | not deployed | 900 s |
| 5 | `sports-research` | Scraper + batch compute | Railway `SportsResearchProduction` | 3600 s |
| 6 | `research-model-refresh` | Batch compute | not deployed | 3600 s |
| 7 | `settlement-worker` | Background worker | Railway `SettlementWorkerProduction` | 3600 s |
| 8 | `reporting-evaluation` | Batch reporting | not deployed | 21600 s |
| 9 | `raw-retention` | Maintenance | Railway `RawRetentionProduction` | 3600 s |
| 10 | PostgreSQL | Database | Railway `Postgres-gxQB` | always on |
| 11 | Migrations | Schema | Railway pre-deploy command | per deploy |
| 12 | Tests, lint, browser checks | CI | GitHub Actions | per push/PR |

There is no queue, no cache service, no object storage, no payment processing,
no WebSocket transport and no ML serving in the deployed system. `STRIPE_ENABLED`,
`VERCEL_ENABLED`, `AIRTABLE_ENABLED`, `POSTHOG_ENABLED`, `GOOGLE_DRIVE_ENABLED`
and `SLACK_ALERTS_ENABLED` exist as feature flags on the web service; the Redis
service in the other project belongs to the other product.

Six of the nine worker roles are defined in `SERVICE_SPECS` but have no Railway
service. They are code paths, not running infrastructure.

### Kalshi collection actually happens in the web service

`KalshiIngestionProduction` has never deployed because it has **no source
repository connected** — its config carries variables, private networking and a
region, and no `source` at all. It has never collected anything.

Collection happens anyway, in the web service. `paper_server.py` persists a
snapshot on every dashboard refresh:

```python
source_persistence = persist_kalshi_snapshot(
    payload,
    worker_name="paper-dashboard-refresh",
)
```

The hosted refresh runs every 300 seconds, which is the same cadence
`kalshi-market-ingestion` is specified at. So the 1.12 GB of `kalshi_public_api`
payload bodies measured in production came from the dashboard, not from the
worker named after the job.

This matters before anyone "fixes" the missing service. The worker persists
under `worker_name="kalshi-market-ingestion"` with a cadence idempotency key;
the dashboard persists under `paper-dashboard-refresh` with none. Uniqueness on
`raw.source_payloads` is `(batch_id, source_identifier, content_hash)` and every
cycle opens a new batch, so the two would **not** deduplicate against each
other. Connecting a source to `KalshiIngestionProduction` while the dashboard
keeps refreshing would roughly double the payload growth rate that filled the
volume in the first place.

## Configuration drift between repository and deployment

| Setting | `railway.json` | Live service |
| --- | --- | --- |
| Builder | `NIXPACKS` | `RAILPACK` |
| Start command | `service-start` | `paper --host 0.0.0.0 --port ${PORT:-8000}` |
| Healthcheck | `/healthz`, 300 s | none configured |
| Pre-deploy | `database-migrate` | applied (this is what fails) |

The public domain routes to **port 8080** while the start command falls back to
**8000** when `PORT` is unset, and `PORT` is not among the service's variables.
`Procfile` and `nixpacks.toml` describe a third start command again, with a
900-second refresh where the live service uses 300.

Four files describe how to start this application and no two agree. Only
`railway.json`'s pre-deploy command is demonstrably reaching production.

## CI and deployment path

`.github/workflows/ci.yml` runs on pull requests and pushes to `Master`: a
browser job (Playwright/Chromium against the dashboard) and a `validate` job
that lints, builds a wheel, applies migrations twice, serializes concurrent
migrators, runs the endpoint smoke tests, and executes the Codespaces workflow
end to end against a PostgreSQL service container.

The workflow deliberately asserts that CI holds no hosted credentials:

```yaml
- name: Verify CI cannot target hosted infrastructure
  run: |
    test -z "${RAILWAY_TOKEN:-}"
    test -z "${RAILWAY_API_TOKEN:-}"
```

**Deployment does not wait for any of it.** Railway's GitHub integration is
configured with `checkSuites: false` on every service, so a push to `Master`
starts a deploy immediately and in parallel with CI. A red build does not stop a
release.

## Security posture

| Check | Result |
| --- | --- |
| Secrets committed to the repository | none found; `.gitignore` covers `.env`, `.env.*`, `*.pem`, `*.key`, `data/secrets/` |
| Secret-shaped strings in tracked files | only `hawknetic_ci:hawknetic_ci_only@127.0.0.1` — a throwaway CI service-container credential |
| Repository visibility | **public** |
| Runtime dependency surface | two packages (`psycopg`, `psycopg_pool`) |
| Dependency update automation | **none** before this audit |
| Secrets in CI | none, asserted by the workflow itself |
| Provider credentials in this session | Railway via OAuth, values returned redacted |
| Structured logs | `test_structured_logs_omit_secret_named_fields` guards against leaking secret-named fields |
| Dashboard auth | `DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED` enforced; CI asserts a hosted dashboard demands auth |

The public repository and the deliberate absence of deploy credentials in CI are
a coherent pair, and worth preserving: any move to workflow-driven deployment
would put production database credentials into a public repository's secrets.

## Local development

`compose.yml` provides PostgreSQL 18 and `scripts/local.sh` drives it. Before
this audit, `local.sh` exited 127 without Docker, so every command — including
running tests — required a container runtime.

The full suite was verified during this audit against a system PostgreSQL with
no Docker at all: **1047 tests, 128 seconds**. Docker is a convenience for this
project, not a requirement.
