# Service map: Railway → AWS

Every service that exists today, what it does, and exactly where it lands.

Cadences are measured from `worker_services.SERVICE_SPECS`, not inferred.
Railway service names and IDs are read from the live project
`jubilant-liberation` (`dfc58505-d45f-4093-8050-35f5371bbf37`).

## Scope boundary

**Hawknetic Office is out of scope and must not be touched.** It lives in a
separate Railway project, `ravishing-elegance`
(`a45378b2-ea1c-4a53-966a-1d22ec2336ea`), with its own services
(`hawknetic-office`, `hawknetic-workers`, `redis`, `postgres`). Nothing in this
migration reads, modifies, redeploys, or retires any of it.

## The application is one image

All nine roles are the same Python package. `HAWKNETIC_SERVICE` selects the
role at start; `HAWKNETIC_SERVICE_MODE` chooses `loop` (run forever on the
worker's own cadence) or `once` (run a single cycle and exit).

That is what makes the scheduled-task design possible: a hourly worker does not
need a code change to become a scheduled task, only `MODE=once` and a schedule.

## Production services

| Railway service | Role | Class | Cadence | AWS destination | Why |
| --- | --- | --- | ---: | --- | --- |
| `HawkNeticSportsTools` | `web` | HTTP API + server-rendered dashboard | continuous | **ECS service** behind ALB, 2 tasks | Needs a stable endpoint, health checks and zero-downtime deploys |
| `KalshiIngestionProduction` | `kalshi-market-ingestion` | Ingestion | 300 s | **ECS service**, 1 task, Spot | 288 starts/day — container boot would be a real fraction of the cycle |
| — (not currently deployed) | `external-source-ingestion` | Ingestion | 900 s | **ECS service**, 1 task, Spot | 96 starts/day; marginal either way, kept resident for simplicity |
| — (not currently deployed) | `crypto-research` | Batch modelling | 900 s | **ECS service**, 1 task, Spot | Same |
| `SportsResearchProduction` | `sports-research` | Batch modelling | 3600 s | **EventBridge Scheduler → RunTask** `cron(5 * * * ? *)` | 24 runs/day of seconds each |
| — (not currently deployed) | `research-model-refresh` | Batch modelling | 3600 s | **Scheduler** `cron(20 * * * ? *)` | Same |
| `SettlementWorkerProduction` | `settlement-worker` | Reconciliation | 3600 s | **Scheduler** `cron(35 * * * ? *)` | Same |
| `RawRetentionProduction` | `raw-retention` | Storage maintenance | 3600 s | **Scheduler** `cron(50 * * * ? *)` | Staggered last, and deliberately not queued behind a research cycle — this is the worker that guards storage |
| — (not currently deployed) | `reporting-evaluation` | Reporting | 21600 s | **Scheduler** `cron(15 */6 * * ? *)` | Resident 21,600 s for a few seconds of work is the clearest waste in the current design |
| `Postgres-gxQB` | PostgreSQL 18.6 | Database | continuous | **RDS PostgreSQL**, `db.t4g.small`, isolated subnets | See below |
| `Postgres-GDG0`, `Postgres` | Unidentified PostgreSQL services | — | — | **Investigate before migrating** | Two further PostgreSQL services exist in the project. Neither is the production database. Their contents and purpose are unresolved and must be established before anything is retired |

### A discrepancy worth stating plainly

`docs/CURRENT_INFRASTRUCTURE.md` recorded an unresolved question about which
workers are actually deployed. The live project answers it: **five worker
services exist in production**, not eight. `external-source-ingestion`,
`crypto-research`, `research-model-refresh` and `reporting-evaluation` have no
Railway service.

Two readings, and they need different responses:

- They were never deployed, and the cadences in `SERVICE_SPECS` describe
  intent rather than production. Then the AWS environment is adding capacity,
  and that should be a deliberate choice rather than a side effect of a
  migration.
- They were deployed and removed. Then something is missing from production
  today and the migration is the moment to notice.

The Terraform for prod defines all eight because the code defines all eight.
**Confirm which reading is correct before the first apply** — it changes the
cost baseline and, in the second case, means production is currently degraded.

## Staging services

`SportsResearchStaging`, `KalshiIngestionStaging`, `HawkNeticResearchStaging`
and their PostgreSQL exist in the `staging` environment
(`14f937a9-34e4-4720-afc4-509e910c64dc`).

The AWS `dev` environment replaces them, with a reduced worker set: one
always-on collector and one scheduled worker, which exercises both code paths
without paying for eight. Railway staging is **not** retired until AWS dev is
proven — and, like everything else here, only on owner approval.

## Per-service detail

### `web`

| | |
| --- | --- |
| Command | `HAWKNETIC_SERVICE=web python -m kalshi_research_bot service-start` |
| AWS | ECS Fargate service, ALB target group, 2 tasks across 2 AZs |
| Sizing | 512 CPU / 1024 MiB. Measured import-time floor is 27.2 MB; the working set with a connection pool is well above that, so this is headroom, not measurement |
| Health | ALB checks `/healthz`. **Not `/readyz`** — readiness includes database and migration state, so a brief database blip would make the ALB kill every task and turn a recoverable dependency failure into a full outage |
| Secrets | `POSTGRES_USER`, `POSTGRES_PASSWORD` (RDS-managed secret), `DASHBOARD_AUTH_PASSWORD` |
| Network | Private subnets (prod), public with no inbound rules (dev) |
| Rollback | ECS deployment circuit breaker with automatic rollback; Railway stays live throughout |

### Always-on workers

| | |
| --- | --- |
| AWS | ECS service, 1 task, `FARGATE_SPOT` |
| Why Spot | A reclaimed collector re-collects on its next cadence. Interruption costs a cycle, not data |
| Sizing | 256 CPU / 512 MiB |
| Replicas | Exactly 1. `deployment_maximum_percent = 100` replaces rather than doubles — two collectors are safe (the idempotency claim makes the second a `skipped_duplicate`) but pointless |

### Scheduled workers

| | |
| --- | --- |
| AWS | EventBridge Scheduler → `ecs:RunTask` |
| Mode | `HAWKNETIC_SERVICE_MODE=once` — one cycle, then exit |
| Safety | `run_worker_once` claims a cadence-derived idempotency key, so a scheduled run overlapping a still-running loop worker records `skipped_duplicate` instead of collecting twice. This is what makes the cutover safe to do incrementally |
| Staggering | Minutes 5/20/35/50 rather than all on the hour, plus a 5-minute flexible window. Eight jobs hitting the database and the external sources simultaneously is a self-inflicted load spike |
| Failure | 2 retries, 10-minute max event age. Beyond that the next schedule handles it; a long retry tail would overlap the following run |
| Visibility | A scheduled task that fails to *start* produces no application logs at all. The `AWS/Scheduler` alarm in the observability module is the only thing that catches it |

### PostgreSQL

| | Railway today | AWS target |
| --- | --- | --- |
| Version | 18.6 | 18.x (verify the exact minor is offered in us-east-2 before applying) |
| Storage | 5 GB fixed volume, **currently 100% full and crashed** | 50 GB, autoscaling to 200 GB |
| Network | Private networking within the project | Isolated subnets, no internet route, security-group ingress only |
| Encryption | Provider default | KMS, customer-managed key, rotation enabled |
| Backups | **Existence unverified** — see database-recovery.md | 14-day automated retention, final snapshot on delete, deletion protection |
| Credentials | Railway environment variables | RDS-managed Secrets Manager secret; no password passes through Terraform |
| Capacity alarm | None — which is why it filled silently | `FreeStorageSpace` below 20 GB |

The storage line is the whole point. A fixed 5 GB volume with raw payloads
growing at ~166 MB/day is a ~30-day fuse. Autoscaling plus an alarm with room
to act replaces the fuse with a warning.

## Other providers

### Render — retire after AWS is stable, do not delete yet

| | |
| --- | --- |
| Service | `HawkNeticSports` (`srv-d88a10jtqb8s73883gi0`), Ohio, tracking `Master` |
| State | **Broken.** Start command is `.`, so every deploy fails with `bash: line 1: .: filename argument required` |
| Assessment | A duplicate that has never run. It serves no traffic and is not a rollback path |
| Action | None now. Not repaired — repairing it would create a second live deployment of the same branch, which is worse than a broken one. Retire on owner approval once AWS is stable |

### Neon — nothing to migrate

Organization `David` (`org-silent-cherry-01851292`) has **0 projects**. Nothing
to move, and no reason to create anything there.

## What is deliberately not in the Terraform

**The Route 53 record that points production traffic at the ALB.** Cutover is
an explicit, owner-approved act, and putting the record in the configuration
would make it a consequence of `terraform apply` instead. The ALB DNS name is
exposed as an output so the record can be created when — and only when — the
parity report passes and the owner approves.

## Rollback mapping

| AWS component | Rollback |
| --- | --- |
| ECS web service | Circuit breaker auto-rollback; Railway web service stays live |
| ECS workers | Scale to 0; Railway workers resume |
| Scheduled workers | Disable the schedule (`enabled = false`); Railway workers resume |
| RDS | Railway PostgreSQL remains the system of record until cutover |
| DNS | Not changed until approved; reverting is a record change |

Railway is not retired, scaled down, or reconfigured at any point in this plan.
It is the rollback platform, and it stays that way until the owner says
otherwise.
