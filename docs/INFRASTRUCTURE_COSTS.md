# Infrastructure Costs

Measured 2026-09-08 from Railway's metrics API over a 7-day window, priced at
Railway's published rates. Every figure below is arithmetic on a measured
number, not an estimate from configuration.

## Price list

| Resource | Rate |
| --- | --- |
| RAM | $10 / GB / month |
| CPU | $20 / vCPU / month |
| Volume storage | $0.15 / GB / month, **billed on used space, not provisioned** |
| Network egress | $0.05 / GB |
| Hobby subscription | $5 / month, **including $5 of usage** |
| Pro subscription | $20 / month, **including $20 of usage** |

Billing is metered on actual consumption. The monthly bill is
`max(subscription, actual usage)` — usage under the included allowance is not
charged twice, and usage over it is charged as the difference.

That billing model is the reason several intuitions about this account are
wrong. An idle service costs almost nothing. A provisioned-but-empty volume
costs almost nothing. What costs money is memory actually held and disk actually
occupied.

## What it costs when it is working

The run rate before the 2026-09-01 outage — the honest baseline, because the
current bill is low only because production is down.

### `jubilant-liberation` / production

| Service | RAM | RAM $/mo | CPU | CPU $/mo | Volume used | Volume $/mo | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `Postgres-gxQB` | 0.908 GB | 9.08 | 0.00023 | 0.00 | 4.995 GB | 0.75 | **9.83** |
| `HawkNeticSportsTools` | 0.206 GB | 2.06 | — | — | 0.770 GB | 0.12 | **2.18** |
| `SettlementWorkerProduction` | 0.041 GB | 0.41 | 0.00010 | 0.00 | — | — | **0.41** |
| `SportsResearchProduction` | 0.039 GB | 0.39 | 0.00010 | 0.00 | — | — | **0.40** |
| `RawRetentionProduction` | 0.036 GB | 0.36 | 0.00007 | 0.00 | — | — | **0.37** |
| `KalshiIngestionProduction` | never deployed | — | — | — | — | — | **0.00** |
| | | | | | | | **$13.19** |

### `jubilant-liberation` / staging

| Service | RAM | RAM $/mo | Volume used | Volume $/mo | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| `Postgres` | 2.463 GB | 24.63 | 4.994 GB | 0.75 | **25.38** |
| `Postgres-GDG0` | stopped | 0.00 | 4.987 GB | 0.75 | **0.75** |
| `HawkNeticResearchStaging` | FAILED since 2026-07-13 | — | — | — | **0.00** |
| `SportsResearchStaging` | FAILED since 2026-08-16 | — | — | — | **0.00** |
| `KalshiIngestionStaging` | never deployed | — | — | — | **0.00** |
| | | | | | **$26.13** |

### `ravishing-elegance` (the separate `hawknetic-office` product)

| Service | RAM $/mo | Volume $/mo | Total |
| --- | ---: | ---: | ---: |
| `postgres` | 0.46 | 0.03 | **0.49** |
| `hawknetic-office`, `hawknetic-workers`, `redis` | not running | — | **0.00** |
| | | | **$0.49** |

### Baseline total

| | Monthly |
| --- | ---: |
| Production (this repository) | $13.19 |
| Staging (this repository) | $26.13 |
| `hawknetic-office` project | $0.49 |
| **Usage** | **$39.81** |
| **Bill** (Hobby, usage exceeds the $5 allowance) | **$39.81** |

## The single most expensive thing in the account

**An unused staging database.** `Postgres` in the staging environment held
2.46 GB of RAM, which bills at **$24.63/month** — 62% of the entire account. It
backs three services: one that has not deployed successfully since 2026-07-13,
one since 2026-08-16, and one that has never deployed at all.

Second is the production database at $9.83/month, which is a real cost for a
real thing.

Everything else in this account — the web service and all three workers
combined — is **$3.36/month**.

That ratio is the whole cost story. Compute here is nearly free; databases are
not, and there are three of them where there should be one.

## Where it lands

After recovering the production database and deleting the two obsolete staging
databases and their three dead services:

| Service | Monthly |
| --- | ---: |
| `Postgres-gxQB` (RAM 0.908 GB + ~2.5 GB volume after prune and vacuum) | $9.46 |
| `HawkNeticSportsTools` (web dashboard **and** Kalshi collection) | $2.18 |
| `SettlementWorkerProduction` | $0.41 |
| `SportsResearchProduction` | $0.40 |
| `RawRetentionProduction` | $0.37 |
| `hawknetic-office` project (unchanged, not this repository's) | $0.49 |
| **Usage** | **$13.31** |
| **Bill** (Hobby) | **$13.31** |

`KalshiIngestionProduction` is not in this table and should not be deployed
as-is: the web service already collects on the same 300-second cadence, and the
two paths do not deduplicate. See `docs/TARGET_INFRASTRUCTURE.md`.

## Savings

| | |
| --- | ---: |
| Current baseline monthly cost | **$39.81** |
| Target monthly cost | **$13.31** |
| **Monthly saving** | **$26.50** |
| **Annualised saving** | **$318.00** |

Roughly 66% of the bill, and essentially all of it comes from deleting a staging
database nothing has used since July.

The volume prune contributes about $0.37/month directly. Its value is not the
money — it is that a database which fits its volume keeps running.

## Hobby or Pro

Recovering the database needs volume headroom, and Hobby caps volumes at 5 GB.

| | Hobby | Pro |
| --- | ---: | ---: |
| Subscription | $5 | $20 |
| Included usage | $5 | $20 |
| Default volume size | 5 GB | 50 GB |
| Self-serve volume ceiling | 5 GB | 1 TB |
| Bill at the target $13.31 of usage | **$13.31** | **$20.00** |

Pro costs **$6.69/month more** at this usage level and buys the ability to grow
a volume — which, on a database that has now hit its ceiling twice, is worth
considering on reliability grounds rather than cost grounds. Both options remain
far below the $39.81 baseline.

If the database is rebuilt compactly to ~2.5 GB and retention holds it there,
Hobby remains viable and is the cheaper choice.

## What would make costs rise

Ordered by how likely each is to happen here.

1. **Database growth.** The dominant term is PostgreSQL RAM at $10/GB-month plus
   volume at $0.15/GB-month. Total database growth was measured at
   **230-280 MB/day**, of which ~166 MB/day is raw payload bodies. Retention
   bounds the payload share; nothing currently bounds `core.markets`,
   `core.events` or `core.market_observations`, which were ~0.95 GB combined on
   2026-08-17 and grow without a window.
2. **More collectors.** Each new source adds its payload volume to the same
   table. `sports-research` alone was measured at ~59 MB/day. A collector added
   without widening the volume shortens the runway proportionally.
3. **Deploying the six worker roles that exist in code but have no service.**
   About $0.40/month each on current sizing.
4. **Replicas.** `multiRegionConfig` is `{iad: 1}` everywhere. Replicas multiply
   usage per replica, and volumes cannot be used with replicas at all.
5. **Egress**, at $0.05/GB. Currently negligible: the busiest worker averaged
   5.2e-8 GB/minute of transmit.
6. **Backups**, billed as incremental volume storage at the same $0.15/GB-month.
   Worth paying for, and cheap because they are copy-on-write.

## Free-tier dependencies

| Dependency | Status |
| --- | --- |
| GitHub Actions | Free — the repository is public, so standard-runner minutes are unmetered |
| GitHub Dependabot | Free |
| Railway | **Paid, and deliberately so** — no free tier is in use |
| Cloudflare / Neon / Render | Not used, so no free-tier limits apply |

The architecture depends on exactly one free tier: GitHub Actions, whose free
status follows from the repository being public. If the repository were made
private, CI would begin consuming the account's included Actions minutes — a
reason to weigh that change on cost as well as disclosure.

Nothing else here is free-tier-dependent, which is the intended outcome: the
paid Railway subscription is doing real work, and no part of the system is one
provider policy change away from breaking.
