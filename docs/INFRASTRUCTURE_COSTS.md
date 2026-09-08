# Infrastructure costs

Published rates, what this workload actually consumes, and what the proposed
changes are worth.

## Rates used

Checked September 2026. Confirm before acting on a large number; providers change
these.

| Provider | Rate |
| --- | --- |
| Railway | $20/vCPU-month, $10/GB RAM-month, $0.15/GB-month volume, $0.05/GB egress. Metered per second except egress and storage. Hobby $5/mo including $5 usage; Pro $20/seat including $20 |
| Neon | Free: 0.5 GB storage, 100 CU-hours/month, suspends after 5 min idle. Launch: $0.106/CU-hour + $0.35/GB-month, no monthly minimum. Minimum autoscaling size 0.25 CU |
| Cloudflare | DNS, TLS, CDN, WAF, rate limiting: $0 on the free plan |
| Render | Always-on services from $7/month each. Cron jobs from $1/month each. Free tier spins down on inactivity; free PostgreSQL not for production |
| GitHub Actions | Free for public repositories; 2,000 minutes/month on the Free plan for private |

## Measured consumption

From this session, against a real database:

| Process | Resident set after import |
| --- | ---: |
| Python 3.12 interpreter | 9.2 MB |
| Worker stack | 25.8 MB |
| `paper_server` (web role) | 27.2 MB |

Import-time floors, not steady state. With a connection pool of up to 5 and a
cycle's working set, plan on **60–120 MB per running service**.

Other measurements: development database after all 16 migrations is **13.5 MB**;
static dashboard assets total **160 KB**; the test suite is 1057 tests in about two minutes.

Recorded from `docs/railway-volume-storage-audit.md` (2026-07-25): production
volume **778.44 MB of 5,000 MB**; staging PostgreSQL volume **341.11 MB of
5,000 MB**.

## Current cost

The repository contradicts itself about how many workers are deployed (see
`CURRENT_INFRASTRUCTURE.md`), so both readings are priced. Run
`scripts/railway_inventory.sh` to collapse this to one column.

| Line | All 8 workers | Web + kalshi only |
| --- | ---: | ---: |
| Worker memory | ~0.64 GB → $6.40 | ~0.08 GB → $0.80 |
| Web memory | $1.50 | $1.50 |
| PostgreSQL memory | $2.50 | $2.50 |
| CPU | $3–8 | $1–2 |
| Volume (0.78 GB) | $0.12 | $0.12 |
| Egress | $0.05–0.25 | $0.05–0.25 |
| **Resources** | **$14–19/mo** | **$6–7/mo** |
| Staging, if running | +$5–10 | +$5–10 |

Plus the plan fee less its included credit. **Realistic total today: $5–30/month.**
That range is set by unresolved facts, not measurement error.

## Target cost

| Line | Target |
| --- | ---: |
| Web (always-on) | $1.50 |
| 3 always-on workers | ~$2.40 |
| 5 scheduled workers | ~$0.15 |
| PostgreSQL | $2.50 |
| CPU | $3–8 |
| Volume + egress | ~$0.30 |
| Neon (dev/CI, free tier) | $0.00 |
| Cloudflare (DNS, WAF, CDN) | $0.00 |
| Render | $0.00 |
| GitHub Actions | $0.00 |
| **Resources** | **~$10–14/month** |

## Savings

| Change | Monthly | Annual |
| --- | ---: | ---: |
| Schedule 5 hourly-or-slower workers | ~$4 | ~$48 |
| Retire an idle staging environment | $5–10 | $60–120 |
| **Total** | **$4–14** | **$48–168** |

The annual column is the monthly column × 12, and the total is the sum of its
components: $4 alone if staging turns out not to be running, $14 if it is.

The scheduling number is firm. The staging number depends on whether that
environment is still running — verify before counting it.

Two things worth more than the money: production had **no automated deployment
path** (a merged migration reached the database only when someone applied it by
hand), and the repository had **no dependency or secret scanning**. Both are now
addressed.

## Why the database stays on Railway

Neon saves money by suspending an idle database. This workload never lets it
idle: `kalshi-market-ingestion` runs every **300 seconds** and Neon suspends
after **5 minutes**, so the collector's cadence *is* the suspend threshold. The
mechanism that makes Neon cheap cannot engage — and that holds even if the other
seven workers were all removed.

| | Neon Launch, never idle | Railway |
| --- | ---: | ---: |
| Compute | 0.25 CU × 730 h × $0.106 = **$19.35** | ~$2.50 |
| Storage | $0.35 | $0.12 |
| **Total** | **$19.70/month** | **$2.62/month** |

Migrating would cost about **$17/month more**, add a network hop, and add
cold-start latency whenever a request did catch it suspended. The free tier does
not help: 0.25 CU continuously is 182 CU-hours/month against a 100 CU-hour
allowance, exhausted around day 16.

Neon is used for **development and CI databases**, where idleness is the norm —
roughly 15 CU-hours/month for two hours of daily work, inside the free tier.

## Why Render is unused

Render charges a flat **$7/month per always-on service**. This architecture has
four always-on services plus a database. That is ~$35/month before storage,
against $6–19/month metered on Railway for the same workload. Render's $1/month
cron jobs total $5/month for the five scheduled workers, where Railway cron is
metered execution — cents.

Flat per-instance pricing only wins when instances are large and busy. This
topology is many small, mostly-idle services, which is the case metering is best
at. Render would be the answer if Railway's metering were the problem; it is the
thing making Railway cheap here.

## What would increase costs

| Trigger | Effect |
| --- | ---: |
| Shortening a collection cadence | Linear in CPU. A 300 s → 60 s change is 5× that worker's CPU |
| Converting scheduled workers back to always-on | +$4/month |
| Raw payload retention growth | `RAW_RETENTION_DAYS=45`. Volume is $0.15/GB-month; database storage is the real constraint |
| A staging environment left running | +$5–10/month, invisible unless you look |
| Adding replicas | Linear. The `/data` volume pins the web service to one replica today |
| Public dashboard traffic | Egress $0.05/GB. Cloudflare in front makes this mostly free |
| Playwright or ML libraries entering the runtime set | Would multiply the 26 MB import floor several times over, on every service |

## Free-tier dependencies

| Service | Tier | If it went away |
| --- | --- | --- |
| Neon | Free — dev/CI only | Fall back to `compose.yml`. No production impact |
| Cloudflare | Free — DNS/WAF/CDN | Point DNS at Railway directly. Lose WAF and rate limiting |
| GitHub Actions | Free tier minutes | CI runs less often, or the repository goes public |

No production path depends on a free tier. That is deliberate: the paid
dependency is Railway, which is where the reliability requirement is.

## Keeping this current

`scripts/railway_inventory.sh` reports services, roles, and which workers have
actually completed a cycle — read-only, and it prints variable names rather than
their values, except the role and mode selectors that are the point of the report. Per-service metered usage is in the Railway dashboard under **Usage**;
record it here when you read it, since nothing in the repository can substitute
for that number.
