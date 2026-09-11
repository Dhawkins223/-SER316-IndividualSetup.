# Hosted Worker Services

The web service runs the dashboard only. Configure existing worker entry points independently for Kalshi ingestion, external sources, crypto research, sports research, settlement, and reporting as reliability needs require.

A worker does not have to be always-on. `HAWKNETIC_SERVICE_MODE=once` runs a
single cycle and exits, which makes the worker deployable as a Railway cron
service that costs its execution rather than its residency. Five of the eight
workers run hourly or slower and are better shaped that way; the per-service
recommendation and the cutover procedure are in `docs/DEPLOYMENT.md`. The default
remains `loop`, so an existing service is unaffected.

The source-backed player/team/Polymarket catalog and pregame refresh coordinator
are separate one-shot Railway cron services. Their commands, cadence, API
contract, and deployment gate are documented in `docs/cloud-source-data.md`.

Every worker uses PostgreSQL-backed idempotency, heartbeat, failure counts, source freshness, bounded retry/backoff, structured logs, graceful shutdown, and transactional checkpoints. A failure in one worker must not alter records owned by another worker or stop the web service.

## Normalized research data

The research-model-refresh worker is the bridge between fresh normalized
Kalshi market observations and the otherwise-empty research schema. Every hour
it selects the latest fresh observation for each open market and writes:

- a code-commit-pinned baseline-only model version;
- immutable point-in-time feature snapshots;
- a completed forward prediction run;
- one zero-edge, no-edge prediction per market; and
- a sample-size coverage metric.

The worker intentionally sets the predicted probability equal to market
consensus. This establishes complete lineage and gives the platform real
normalized research data without presenting an exchange quote as an
independently validated algorithm. A repeat cycle over the same dataset hash is
a no-op.

Deploy it with railway.worker.json, DATABASE_MIGRATION_MODE=check, and:

    HAWKNETIC_SERVICE=research-model-refresh
    RESEARCH_BASELINE_MAX_AGE_SECONDS=1800
    RESEARCH_BASELINE_MAX_MARKETS=1000

It requires the same research-only safety flags as every other hosted worker.
Verify it in staging before production and require fresh kalshi_public_api
source health.

Do not start workers from a migration pre-deploy command. Optional connector failures appear in worker status and block only their dependent workflow.

## Worker services use `railway.worker.json`

The repository-root `railway.json` carries the web service's pre-deploy migration, and Railway applies a root config to every service built from the repository. A worker inheriting it runs `database-migrate` on each deploy, which contradicts the rule above and couples the worker's deployment to database availability: when the staging database went into recovery, the worker's pre-deploy migration failed and the deployment failed with it, leaving an older build running.

Point every worker service at `railway.worker.json` instead (Railway service settings, "Config as code" path). It has no pre-deploy command. Migrations stay with the web service, and workers verify schema readiness at runtime through `DATABASE_MIGRATION_MODE=check` — a worker facing an unready or unavailable database fails its cycle and backs off, which is recoverable, rather than failing to deploy at all.

Note that "migrations stay with the web service" describes the intended design, not the deployed one: the production web service is not currently connected to the repository, so neither its config-as-code nor its pre-deploy migration is applied, and a merged migration reaches the database only when someone applies it. See `docs/schema-migration-application.md`.

## Surviving a transient database error

A worker's per-cycle bookkeeping — claiming ownership and recording the outcome — runs outside the operation's own retry loop, so a PostgreSQL connection dropped there is not an operation failure and is not retried by the operation. Left unhandled it ends the process, which is how a momentary blip turns into a permanently stopped collector: `KalshiIngestionStaging` crashed exactly this way with `psycopg.OperationalError: the connection is lost`.

`run_worker_forever` now absorbs a crashed cycle, logs `worker_cycle_crashed` with the error code and the consecutive-crash count, and retries on the next tick. After three consecutive crashes the connection pool is dropped, because a pool holding broken connections keeps failing the same way. A cycle that completes clears the streak.

Backoff doubles from `initial_backoff_seconds` until it reaches the worker's own cadence, then holds there. A database that stays down is never retried faster than the worker would have run anyway:

| Worker | Cadence | Retry schedule (seconds) |
| --- | ---: | --- |
| `kalshi-market-ingestion` | 300 | 2, 4, 8, 16, 32, 64, 128, 256, then 300 |
| `research-model-refresh` | 3600 | 2, 4, 8, …, 1024, 2048, then 3600 |
| `sports-research` | 3600 | 2, 4, 8, …, 1024, 2048, then 3600 |
| `reporting-evaluation` | 21600 | 2, 4, 8, …, 8192, 16384, then 21600 |

The first version of this capped the *exponent* at four steps rather than clamping the delay, which pinned an hourly worker to a 16-second retry. Against a staging database stuck in recovery that produced over eleven hundred crash cycles. Clamp the delay by the cadence; bound the exponent only to keep the arithmetic sane.

This does not mask failures. An operation that fails is still recorded through the existing failure path, still increments `consecutive_failures`, and still alerts. Only the loop's survival changed.

Production currently runs the web service, kalshi-market-ingestion,
sports-research, settlement-worker, and raw-retention. Crypto, external-source,
research-model-refresh, and reporting-evaluation remain undeployed.
docs/sports-data-upload.md documents the sports worker and the states its board
reports.
## Which workers are deployed is currently unsettled

This file and `docs/sports-data-upload.md` disagree, and neither can be trusted
until someone looks at Railway.

- **This file recorded:** production runs only the web service and
  `kalshi-market-ingestion`; sports, crypto, settlement, external-source and
  reporting are not deployed, so their tables receive no hosted rows.
- **`docs/sports-data-upload.md` records:** `SportsResearchProduction`,
  `RawRetentionProduction` and `SettlementWorkerProduction` are deployed and
  running, with five consecutive hourly cycles from 2026-08-16 tabulated as
  evidence.

Both cannot be true. The second carries measured cycles and is the later of the
two, which makes it the more likely, but "more likely" is not a deployment
record. Settle it by reading the service list, then delete the losing claim
rather than softening it.

`scripts/railway_inventory.sh` does both halves of that in one command: it lists
the services and reports the `ops.worker_status` rows below, read-only, printing
variable names without their values. It is also what closes the cost question in
`docs/INFRASTRUCTURE_COSTS.md`, whose estimate is a range only because this
disagreement is open.

```sql
SELECT worker_name, status, consecutive_failures, last_error_code, heartbeat_at
FROM ops.worker_status ORDER BY worker_name;
```

A worker that has never run has no row. A worker that stopped has a stale
`heartbeat_at`. Note that an unapplied migration presents as a stopped worker —
see `docs/schema-migration-application.md` — so check `preflight` before
concluding a service was never deployed.

`docs/sports-data-upload.md` documents the readiness-gated steps for the sports
worker and the states its board reports while it settles.
