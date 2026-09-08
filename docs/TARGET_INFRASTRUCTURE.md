# Target Infrastructure

The decisions below are the output of the 2026-09-08 infrastructure audit. Each
one names the evidence it rests on. `docs/CURRENT_INFRASTRUCTURE.md` records what
was measured; `docs/INFRASTRUCTURE_COSTS.md` does the arithmetic.

## The short version

**Everything stays on Railway.** No workload moves to Cloudflare, Neon or
Render, because for this workload each of those is either more expensive, less
reliable, or both. The changes worth making are to stop paying for three
databases where one is needed, to make the retention window able to bite, and to
alarm on volume capacity before it is spent.

```
GitHub  (source of truth, public repository)
  │
  ├── GitHub Actions ─ lint · migrations · 1045 tests · browser checks
  │                    (free for public repositories)
  │
  └── Railway  ── Hobby plan, project `jubilant-liberation`, region iad
        │
        ├── HawkNeticSportsTools ....... web dashboard + Kalshi collection (300 s)
        ├── SportsResearchProduction ... hourly sports research
        ├── SettlementWorkerProduction . hourly settlement import
        ├── RawRetentionProduction ..... hourly payload-body prune + capacity watch
        └── Postgres-gxQB .............. the one application database

Cloudflare  not used
Neon        not used
Render      not used
```

Deliberately removed: two of the three PostgreSQL instances, three staging
services that have never run successfully, and the staging environment's
never-applied 122-change patch.

`KalshiIngestionProduction` stays undeployed — see below. It is not a gap.

## The decision that dominates everything else

The account is on Railway's **Hobby** plan. Hobby provisions 5 GB volumes and
5 GB is the plan ceiling. The production database reached 4.994 GB, PostgreSQL
PANICked mid-write on 2026-09-01, and then could not restart because WAL
recovery needs to write too.

So the binding constraint on this architecture is not compute cost. Compute is
almost free here: the three workers together average **0.117 GB of RAM and
0.00027 vCPU**, which bills at about **$1.16/month**. The constraint is 5 GB of
volume, and the question every provider decision has to answer is what the
database costs and where it can grow.

## Database: stays on Railway PostgreSQL

Neon was evaluated seriously and rejected on numbers.

The workload writes continuously: `kalshi-market-ingestion` runs every 300
seconds, forever. Neon's economics depend on scale-to-zero, and a database
written to every five minutes never scales to zero.

| | Railway PostgreSQL | Neon Free | Neon Launch |
| --- | --- | --- | --- |
| Storage limit | 5 GB on Hobby, 50 GB on Pro, 1 TB self-serve | **0.5 GB** | unlimited |
| Storage price | $0.15/GB-month, billed on used | — | $0.35/GB-month |
| Compute price | $10/GB-RAM + $20/vCPU per month, billed on used | — | $0.106/CU-hour |
| Scale-to-zero useful here? | n/a | n/a | **no** — writes every 300 s |
| Cost of ~2.5 GB + always-warm compute | **~$9.50/month** | does not fit | **$20-77/month** |

Neon's free tier holds 0.5 GB. This database needs roughly 2.5 GB at a healthy
ten-day retention window — five times the free allowance — so "it is free" was
never available as an argument. On Neon Launch, storage alone costs 2.3x
Railway's rate, and compute that cannot suspend runs from **$19/month** at a
quarter CU to **$77/month** at one CU. Railway's equivalent is about $9.50.

Migrating would also mean moving a database that currently cannot start, off a
platform where its private networking, its pre-deploy migration hook, and its
five dependent services already work.

**Decision: keep PostgreSQL on Railway.** Revisit only if the database exceeds
Railway Pro's ceilings or the write cadence becomes genuinely intermittent.

## Cloudflare: not used

Cloudflare is excellent at the things this project does not have. There is no
separate frontend — the dashboard is server-rendered by the same Python process
that serves `/healthz` and `/readyz`, reading from PostgreSQL behind
authentication. There is no static bundle to put on a CDN, no public asset
traffic to cache, and no custom domain in play: the service is reached at
`hawkneticsportstools-production.up.railway.app`.

Workers is the wrong runtime for every workload here. The collectors make
long-running outbound HTTP requests on a schedule, hold PostgreSQL connections,
and depend on `psycopg` — a compiled C extension.

**Decision: do not adopt Cloudflare.** Reconsider when a custom domain is
registered, at which point Cloudflare DNS plus proxy is worth it for TLS, DDoS
protection and WAF — none of which requires moving any workload.

## Render: not used

The brief asks five questions for any Render workload. For every workload here
the answers are the same:

- **Why Render?** No reason found.
- **Why not Railway?** Railway already runs it, with private networking to the
  database, config-as-code, and a working pre-deploy migration hook.
- **How much does it save?** Nothing. Render's free web services sleep after 15
  minutes of inactivity and its free PostgreSQL expires after 30 days. Paid
  Render starts at $7/service/month, which is *more* than the ~$0.40/month each
  worker costs on Railway's metered billing.
- **What complexity does it add?** A second provider, a second deploy pipeline,
  a second secret store, and cross-provider network latency to the database.
- **What happens as it grows?** Render's per-service pricing scales linearly
  with service count; Railway's metered model does not.

**Decision: use no Render services.** The final architecture contains zero.

## Workers stay always-on rather than becoming scheduled jobs

The obvious cost move — the workers only run hourly, so make them cron jobs —
does not survive measurement.

| Worker | Cadence | RAM (7d avg) | CPU (7d avg) | Monthly |
| --- | --- | ---: | ---: | ---: |
| `SportsResearchProduction` | 3600 s | 0.0394 GB | 0.00010 vCPU | $0.39 |
| `SettlementWorkerProduction` | 3600 s | 0.0413 GB | 0.00010 vCPU | $0.41 |
| `RawRetentionProduction` | 3600 s | 0.0364 GB | 0.00007 vCPU | $0.36 |

Railway bills actual consumption, not provisioned capacity, so an idle Python
process costs almost nothing. Converting all three to Railway cron or GitHub
Actions would save on the order of **$1/month** while adding scheduling
configuration, cold starts, and — for GitHub Actions — production database
credentials in a **public** repository's secrets, which the CI workflow
currently asserts do not exist:

```yaml
- name: Verify CI cannot target hosted infrastructure
  run: test -z "${RAILWAY_TOKEN:-}"
```

That assertion is a deliberate security property. Trading it for a dollar a
month is a bad exchange.

**Decision: leave the workers as always-on Railway services.** This is exactly
the case the brief warns about — a small saving that buys substantially more
operational complexity.

## Do not deploy `KalshiIngestionProduction` as-is

It looks like an obvious omission: the 5-minutely market collector, the most
important data path in the system, has never deployed — because no source
repository is connected to it.

Connecting one would be a mistake. The web service already collects Kalshi data
on exactly that cadence, persisting a snapshot on every dashboard refresh under
`worker_name="paper-dashboard-refresh"`. The 1.12 GB of `kalshi_public_api`
bodies measured in production came from there.

The two paths would not deduplicate. Uniqueness is
`(batch_id, source_identifier, content_hash)` and every cycle opens a new batch,
so running both would store two copies of every five-minute payload and roughly
**double the ~166 MB/day** growth that filled the volume.

If the dedicated worker is wanted — and there is a good argument for it, since
it carries a cadence idempotency key and proper batch lineage while the
dashboard path carries neither — then the dashboard's collection has to be
turned off in the same change, by setting `DASHBOARD_REFRESH_SECONDS=0` on the
web service. That variable exists for exactly this case: "a dashboard that reads
only what the collector workers write."

**Decision: leave it undeployed until someone makes that swap deliberately.**
Consolidating collection onto the worker is the better long-term shape; doing
half of it is worse than doing none.

## Retention: the window has to be able to bite

The window sets the table's steady state: `daily_growth x window_days`. Against
the measured ~166 MB/day of raw payload bodies on a 5 GB volume:

| Window | Steady-state payloads | Plus ~1 GB core | Verdict |
| ---: | ---: | ---: | --- |
| 45 days (`.env.example` shipped this) | ~7.5 GB | ~8.5 GB | impossible |
| 30 days (code default) | ~5.0 GB | ~6.0 GB | impossible |
| 10 days | ~1.7 GB | ~2.7 GB | **adopted** |
| 7 days (module floor) | ~1.2 GB | ~2.2 GB | emergency setting |

Both shipped defaults were unreachable, which is why retention pruned nothing
while the volume filled. Changed in this audit:

- `DEFAULT_RETENTION_DAYS` 30 → **10**
- `.env.example` `RAW_RETENTION_DAYS` 45 → **10**
- `RawRetentionProduction` on Railway set to `RAW_RETENTION_DAYS=10`,
  `RAW_RETENTION_DRY_RUN=false`

## Capacity alarm: the missing feedback loop

The retention worker reported `database_bytes` on every cycle for weeks while
that number climbed to the ceiling. Nothing compared it to anything.

`database_capacity_state()` now turns it into a state, `build_internal_status()`
raises a `database_capacity` anomaly, and `actionable_monitoring_events()`
escalates it:

| Used | State | Severity | Headroom at ~250 MB/day |
| ---: | --- | --- | --- |
| < 75% | ok | — | — |
| ≥ 75% | warning | warning | ~5 days |
| ≥ 90% | critical | **critical** | ~2 days |

The ceiling comes from `DATABASE_VOLUME_CAPACITY_BYTES`, defaulting to 5 GB.
Raise it after a volume resize.

## Local development: Docker optional

`scripts/local.sh` exited 127 without Docker, so running the tests required a
container runtime. It now supports three modes via `HAWKNETIC_LOCAL_DB`:

| Mode | Behaviour |
| --- | --- |
| `auto` (default) | Compose when Docker is present, external otherwise |
| `compose` | Require Docker; unchanged Codespaces path |
| `external` | Use a PostgreSQL that is already running |

Compose and the Codespaces flow are untouched and remain canonical. The full
suite — **1045 tests, 145 seconds** — was verified during this audit against a
system PostgreSQL 16 with no Docker running.

## What has to happen next, and who can do it

Three things need the account owner, because the Railway API available to this
audit cannot do them and because two of them destroy data.

1. **Recover the production database.** It cannot restart on a full volume.
   Check the service's Backups tab first — restoring a pre-2026-09-01 snapshot
   mounts a fresh volume and costs nothing. Failing that, the volume has to grow,
   which Railway's published limits put on the Pro plan. `docs/ROLLBACK.md` has
   the ordered runbook and the reason each step comes where it does.
2. **Delete the two obsolete staging databases** — after taking a backup. They
   hold ~10 GB between them and serve services that have not deployed
   successfully since July and August.
3. **Turn on "Wait for CI"** on each service. Railway's GitHub integration is
   set to `checkSuites: false`, so pushes to `Master` deploy in parallel with CI
   and a red build does not stop a release.

Nothing in this document was applied to the two obsolete databases. They are
5 GB volumes that could not be inspected or backed up from here, and an
unverified backup is not a backup.
