# AWS Migration — Live Provider Inventory

Captured: 2026-09-11
Repository: `Dhawkins223/HawkNeticSportsTools`
Branch used for migration work: `aws/migration-foundation`

This file records live provider state observed through connected provider APIs. It is migration evidence, not an architectural assumption.

## Classification

| Component | Provider | Live state | Migration disposition |
| --- | --- | --- | --- |
| Source of truth | GitHub | Active; default branch `Master`; repository is public | RETAIN |
| Production web/API | Railway | `HawkNeticSportsTools`; latest production deploy FAILED | MIGRATE to ECS/Fargate |
| Production PostgreSQL | Railway | `Postgres-gxQB`; PostgreSQL 18.6; CRASHED | RECOVER first, then MIGRATE to RDS PostgreSQL |
| Kalshi ingestion | Railway | `KalshiIngestionProduction`; latest deploy FAILED | MIGRATE after DB recovery |
| Sports research | Railway | `SportsResearchProduction`; latest deploy SUCCESS | MIGRATE; candidate for scheduled ECS task |
| Settlement worker | Railway | `SettlementWorkerProduction`; latest deploy SUCCESS | MIGRATE; candidate for scheduled ECS task |
| Raw retention | Railway | `RawRetentionProduction`; latest deploy SUCCESS | MIGRATE; candidate for scheduled ECS task |
| Render duplicate | Render | `HawkNeticSports`; tied to `Master`; recent deploys fail at runtime | RETIRE after AWS parity |
| Neon Sports resources | Neon | No projects found in connected org `David` | NONE TO MIGRATE |
| Hawknetic Office | Railway | Separate project `ravishing-elegance` contains `hawknetic-office` | DO NOT TOUCH |

## Railway production evidence

Project:
- name: `jubilant-liberation`
- project id: `dfc58505-d45f-4093-8050-35f5371bbf37`
- production environment id: `cd5e7bc2-b6e5-4c1a-a442-8e1a2b9cb64a`

### Database incident

`Postgres-gxQB`:
- service id: `14f05b1e-ea2a-4aef-9aba-d79c11d6e143`
- volume: 5,000 MB
- current disk usage observed over the last 24h: about 4.9948 GB
- utilization: about 99.9% of the nominal 5 GB volume
- latest deployment state: `CRASHED`

Observed PostgreSQL recovery failure:

```text
FATAL: could not write to file "pg_wal/xlogtemp.70": No space left on device
LOG: startup process exited with exit code 1
LOG: shutting down due to startup process failure
```

The log also warns that recovery was interrupted and that corruption may require recovery from the last backup. Therefore this database must not be treated as healthy merely because credentials or a mounted volume exist.

### Production service state

Observed at the same production environment:

| Service | Latest state |
| --- | --- |
| HawkNeticSportsTools | FAILED |
| Postgres-gxQB | CRASHED |
| KalshiIngestionProduction | FAILED |
| SportsResearchProduction | SUCCESS |
| SettlementWorkerProduction | SUCCESS |
| RawRetentionProduction | SUCCESS |

The web service source is GitHub `Dhawkins223/HawkNeticSportsTools`, branch `Master`.
The web service currently mounts a separate 5 GB volume at `/data`.

The safety-variable names present on the live web service include:
- `RESEARCH_ONLY`
- `LIVE_EXECUTION_ENABLED`
- `AUTO_UPLOAD_ENABLED`
- `AUTO_TRADE_ENABLED`
- `KALSHI_ORDER_UPLOAD_ENABLED`
- `MODEL_PROMOTION_ENABLED`
- `STALE_CACHE_AS_FRESH`

Values are intentionally not recorded here.

## Render evidence

Workspace:
- `My Workspace`
- workspace id: `tea-d86rtgjtqb8s73ftab6g`

Service:
- `HawkNeticSports`
- service id: `srv-d88a10jtqb8s73883gi0`
- repo: `Dhawkins223/HawkNeticSportsTools`
- branch: `Master`
- region: Ohio
- auto deploy: enabled
- no Render Postgres instances found

Recent deploys are `update_failed`.
The build itself succeeds, then Render attempts to run the configured start command `.` and exits:

```text
Running '.'
bash: line 1: .: filename argument required
.: usage: . filename [arguments]
Exited with status 2
```

Do not repair this duplicate merely to keep two production platforms alive. Preserve it only until AWS parity/rollback policy says it can be retired.

## Neon evidence

Connected organization:
- name: `David`
- id: `org-silent-cherry-01851292`
- plan: free

No Neon projects were returned for this organization at capture time.

## GitHub evidence

- default branch: `Master`
- active repository permissions include push/admin for the connected account
- current CI validates PostgreSQL, migrations, tests, browser checks, and safety flags
- current production deploy workflow targets Railway only after successful CI on `Master`
- this AWS migration work is isolated on a feature branch, so creating these files does not itself deploy production

## Repository runtime facts

Existing repository documentation and configuration establish:
- Python 3.12 application
- PostgreSQL-only persistence
- forward-only migrations
- `HAWKNETIC_SERVICE` selects service role
- `HAWKNETIC_SERVICE_MODE=once` exists for one-shot/scheduled workers
- `RAW_RETENTION_DAYS=10` is the current safe default for the historical 5 GB Railway constraint
- production safety posture is research-only

## Immediate gates

1. Do not delete or truncate production data to create emergency space.
2. Do not restart-loop the only database copy without creating headroom/recovery evidence.
3. Increase/recover Railway database headroom through a provider-supported path before attempting a logical dump.
4. Verify backup/recovery integrity before using the source as migration truth.
5. Do not run AWS production traffic until database parity passes.
6. Do not retire Railway, Render, or any rollback path until AWS stabilization passes.
7. Do not touch Hawknetic Office resources.

## Current blocker

AWS provisioning cannot be executed from this repository branch until an authenticated AWS administrative session/connector is available. The repository can be prepared safely in parallel, but no AWS resource should be claimed as created until provider-side evidence confirms it.
