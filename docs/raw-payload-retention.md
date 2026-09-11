# Raw Payload Retention

## Why this exists

`raw.source_payloads` stores one full response body per collection cycle. Kalshi
ingestion runs every five minutes, so the table grows without bound. It filled
the staging volume outright, and production has been growing at roughly 230 MB
per day. See `docs/staging-sports-worker-verification.md` for the measurements.

## What it does and does not remove

Deleting those rows is not an option. `core.markets`, `core.market_observations`,
`core.events`, `core.series`, `core.trades`, `core.settlements`,
`core.orderbook_snapshots`, and `raw.rejected_records` all carry foreign keys
into `raw.source_payloads`, and the project's integrity contract treats a
payload's content hash as evidence that a collection happened and what it
contained.

So retention prunes the **body** and keeps everything that proves the collection:

| Preserved | Removed |
| --- | --- |
| The row and its id | The payload body |
| `batch_id` and its batch lineage | |
| `source`, `entity_type`, `source_identifier` | |
| `observed_at`, `received_at` | |
| `content_hash` | |
| `parser_version` | |

The body is replaced by a tombstone recording the prune time, the original byte
size, and the content hash, so a pruned row stays self-describing. Collection
dedup and import lineage compare the `content_hash` column and never re-hash the
body, so pruning disturbs neither.

**The body itself is not recoverable.** Take a backup first.

## Guards

- The window has a hard floor of seven days. A shorter request is refused with
  `RetentionWindowTooShort` rather than honoured.
- The newest payload per `(source, entity_type)` is never a candidate at any
  window. That is the row the dashboard reads for its current snapshot, so the
  guard means retention can never blank the live view.
- Every run defaults to a dry run. Nothing is written without `--apply`.
- `--limit` bounds a single pass, and the result reports how much remains
  eligible, so a large backlog can be worked through in controlled batches.

## Run it as a worker

Storage is a collection concern, not an occasional chore: raw payload bodies
accumulate on every cycle of every collector, and a volume that fills stops all
of them. `raw-retention` is a worker role, deployed like any other:

```text
HAWKNETIC_SERVICE=raw-retention
```

It runs hourly and applies by default — a retention worker that only ever
reported would leave the volume filling. A bounded prune is cheap, and the worker
guarding the volume should not leave a long blind window after every deploy: a
restart forfeits the rest of the current cadence bucket, so a six-hour cadence
cost six hours of no measurement every time it was redeployed. The same guards
still hold: the window floor, the newest-payload-per-source exemption, and the
per-pass limit.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAW_RETENTION_DAYS` | 10 | Window in days. Anything under the seven-day floor is raised to it. See the arithmetic below before widening it. |
| `RAW_RETENTION_BATCH_LIMIT` | 5000 | Maximum bodies pruned in one pass. |
| `RAW_RETENTION_DRY_RUN` | false | Set true to report without writing. |

Each pass reports:

| Field | Meaning |
| --- | --- |
| `payload_bodies_pruned` | Bodies replaced by a tombstone this pass |
| `reclaimable_bytes` | Space those bodies occupied |
| `still_eligible` | Bodies past the window this pass did not reach |
| `retained_span_days` | How many days of payloads the table holds |
| `oldest_received_at` | Age of the oldest retained payload |
| `window_bites` | Whether the window can reach anything at all |
| `database_bytes` + `largest_relations` | Where the space actually is |

A pass with nothing left to prune is the healthy steady state and is recorded as
`no_material_change`, not a failure. A pass with `window_bites: false` is
different and worth acting on: the window is wider than the data's own age, so
this configuration will prune nothing until the table ages into it — which on a
bounded volume may never happen.

Point the service's config-as-code path at `railway.worker.json` like every other
worker, so it carries no pre-deploy migration.

## Usage

```bash
# Where the space is going, and how much is eligible.
PYTHONPATH=src python -m kalshi_research_bot raw-retention --report-only

# Dry run against production's ten-day window.
PYTHONPATH=src python -m kalshi_research_bot raw-retention --older-than-days 10

# Apply, one source at a time, in bounded passes.
PYTHONPATH=src python -m kalshi_research_bot raw-retention \
    --older-than-days 10 --source kalshi_public_api --limit 2000 --apply
```

## Reclaiming space on a full volume

Pruning frees space *inside* the table. PostgreSQL reuses it for new rows, but it
does not return it to the filesystem without a `VACUUM FULL`, and `VACUUM FULL`
needs free space of its own to rewrite the table. On a volume that is already at
its ceiling there is nothing to rewrite into.

The workable order on a full volume is:

1. Raise the volume size first. Nothing else can succeed while writes fail.
2. Back up.
3. Prune in bounded passes, verifying counts between them.
4. `VACUUM (ANALYZE) raw.source_payloads` to make the freed space reusable.
5. Only run `VACUUM FULL` if the space must return to the filesystem, and only
   with headroom equal to the table's current size.

After that, ordinary autovacuum plus a scheduled prune keeps the table flat
without further intervention.

## Measured cost of a collector

One `sports-research` cycle stores about 2.46 MB of raw payload bodies — fifteen
ESPN summary responses plus one scoreboard. At its hourly cadence that is roughly
**59 MB per day**, on top of the ~230 MB per day production already grows.

That is the case for running retention alongside a new collector rather than
after it: each collector added without a retention window shortens the runway,
and `raw.source_payloads` is where nearly all of the mass sits.

## Choosing a window: it is arithmetic, not taste

A retention window sets the table's steady-state size:

```text
steady_state_size  =  daily_growth  x  window_days
```

That makes most windows impossible rather than merely generous. Production
measured on 2026-08-17:

| Relation | Size | Share |
| --- | ---: | ---: |
| `raw.source_payloads` | 1.98 GB | 67% |
| `core.markets` | 0.42 GB | 14% |
| `core.events` | 0.28 GB | 9% |
| `app.prediction_rejections` | 0.10 GB | 3% |
| `core.market_observations` | 0.10 GB | 3% |
| `app.prediction_logs` | 0.05 GB | 2% |
| **Database total** | **2.95 GB** | |

The retained span, measured the same pass, was **12.1 days** holding 2.01 GB of
payload bodies — about **166 MB per day of raw payloads** specifically, against
~230-280 MB per day of total database growth. On a 5 GB volume:

| Window | Steady-state raw | Plus ~1 GB core | Verdict |
| ---: | ---: | ---: | --- |
| 30 days | ~5.0 GB | ~6.0 GB | impossible — and unreachable, the data is only 12 days old |
| 14 days | ~2.3 GB | ~3.3 GB | workable but close to the current span |
| 10 days | ~1.7 GB | ~2.7 GB | production's setting |
| 7 days | ~1.2 GB | ~2.2 GB | ample headroom |

Ten days is the default, and what production runs. A thirty-day window was tried
first and pruned
nothing — not because retention was broken, but because the table only held 12
days of data and never would hold thirty: the volume fills first. That is not a
hypothetical: on 2026-09-01 the volume reached 100%, PostgreSQL PANICked
mid-write, and could not restart, because WAL recovery needs to write too. The
default is now ten rather than thirty for exactly this reason, and
`database_capacity` alarms at 75% so the next approach to the ceiling is noticed
rather than discovered. A window that
never becomes eligible is indistinguishable from having no retention at all,
which is why `window_bites` is reported.

The first pass at ten days pruned **438 bodies and freed 344 MB**, and drained
the eligible backlog to zero in that single pass.

Compute the window from the volume and the measured growth rate. Widen it only
after the volume grows, and re-measure with
`raw-retention --report-only` rather than assuming.

## What pruning does and does not do to disk usage

Pruning frees space *inside* the table for PostgreSQL to reuse. Reported disk
usage does not fall; it stops rising. Expect a plateau, not a drop. Returning
space to the filesystem needs `VACUUM FULL`, which needs free space of its own —
see the ordering above.

## Where the floor actually is: measured, not assumed

Retention reached its steady state — `still_eligible: 0` and `window_bites: true`
at a seven-day window — while `raw.source_payloads` was still 2.02 GB of a
3.29 GB database. The remaining mass sits *inside* the window, so a shorter
window cannot reach it, and seven days is the module's floor anyway.

One explanation was worth testing. Uniqueness on the table is
`(batch_id, source_identifier, content_hash)` and every cycle opens a new batch,
so a source returning an unchanged response stores the body again with nothing
objecting. If that were common, de-duplication would be the obvious fix.

It is not common. Measured in production on 2026-08-21:

| Source | Redundant bytes | Retained bytes | Most copies of one body |
| --- | ---: | ---: | ---: |
| `espn_scoreboard` | 14,386,807 | 31,797,595 | 17 |
| `espn_summary` | 70,007 | 114,504,860 | 2 |
| `kalshi_public_api` | **0** | 1,118,555,646 | 1 |

2,903 retained bodies, 2,801 of them distinct: a redundant share of **1.14%**,
or about 14.5 MB — roughly 0.4% of the database. **De-duplication is not worth
building.** The measurement is kept because the negative result is the useful
part: it says the payload floor is real, irreducible data rather than the same
data stored repeatedly.

The table above also says where the floor comes from. `kalshi_public_api` holds
1.12 GB of retained bodies with *zero* duplicate content — every five-minute
Kalshi payload is genuinely different, which is what a live order book should
look like. The floor is therefore set by Kalshi's collection cadence across the
retention window, and the levers on it are the cadence, the window, and the
volume size. It is not a redundancy problem and cannot be tidied away.

The one real duplication is `espn_scoreboard`, whose most-repeated body appears
17 times. That is the prior-day finals lookback re-fetching completed
scoreboards that can no longer change (`SPORTS_FINALS_LOOKBACK_DAYS`, see
`docs/sports-data-upload.md`). At 14 MB it is not worth optimising today, but it
grows with the lookback window, so widening that window has a storage cost as
well as a request cost.

### Running the measurement again

It reads every retained body, which is far more expensive than the
catalog-statistics census in each cycle, so it is off by default:

```text
RAW_RETENTION_DUPLICATION_CENSUS=true
```

Set it, let one cycle run, read `redundant_share` and `duplication_by_source`
from the worker's metrics, then set it back to `false`. It only measures — it
never deletes, rewrites, or deduplicates a row.
