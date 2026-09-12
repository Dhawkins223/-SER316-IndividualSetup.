# Production PostgreSQL recovery — Railway `Postgres-gxQB`

Status: **DOWN. Owner action required.** Nothing in this document has been
applied. No data has been written, deleted, or moved.

This is the critical-path dependency for the AWS migration: a database that
will not start cannot be dumped, and a database that cannot be dumped cannot
be migrated or verified.

## 1. The incident

| Field | Value |
| --- | --- |
| Railway project | `jubilant-liberation` (`dfc58505-d45f-4093-8050-35f5371bbf37`) |
| Environment | `production` (`cd5e7bc2-b6e5-4c1a-a442-8e1a2b9cb64a`) |
| Service | `Postgres-gxQB` (`14f05b1e-ea2a-4aef-9aba-d79c11d6e143`) |
| Image | `ghcr.io/railwayapp-templates/postgres-ssl:18` (PostgreSQL 18.6) |
| Volume | `postgres-volume-kIIV` (`2821d25a-0372-4e23-b990-b6112c01c95d`), **5000 MB**, region `iad` |
| Latest deployment | `9a321d78-753a-4573-b2ae-e9a6210d2eed` — **CRASHED** |
| Disk usage | **4.994777088 GB of 5.0 GB** (measured, 1441 samples over 24 h) |
| Free space | **≈ 5 MB** |

Measured, not inferred: the disk figure is the `DISK_USAGE_GB` series read
from Railway metrics; `min` over the window is 0 only because the service was
restarting. The `average` is 4.9913 GB — the volume has been effectively full
for the entire window, not momentarily.

### Blast radius

| Service | Latest deployment |
| --- | --- |
| `Postgres-gxQB` | CRASHED |
| `HawkNeticSportsTools` (web) | FAILED |
| `KalshiIngestionProduction` | FAILED |
| `SportsResearchProduction` | SUCCESS (deploy status only — it has no database to reach) |
| `SettlementWorkerProduction` | SUCCESS (same caveat) |
| `RawRetentionProduction` | SUCCESS (same caveat) |

A `SUCCESS` deployment status means the container started, not that the
workload is healthy. `raw-retention` is the worker whose job is to prune the
raw payload table — the one thing that would relieve the pressure — and it
cannot run, because pruning requires a database connection.

That is the trap this incident is in: **the mechanism that would free space
requires the database that has no space.**

## 2. The exact failure

From the crashed deployment's logs, two consecutive restart attempts
(18:27:47 and 18:28:01 UTC), reproduced identically:

```
LOG:  database system was interrupted while in recovery at 2026-09-11 18:27:34 UTC
HINT: This probably means that some data is corrupted and you will have to
      use the last backup for recovery.
LOG:  database system was not properly shut down; automatic recovery in progress
LOG:  redo starts at D/BE279468
LOG:  redo done at D/C2FFF350  system usage: CPU: user: 0.13 s, system: 0.19 s, elapsed: 3.88 s
FATAL:  could not write to file "pg_wal/xlogtemp.70": No space left on device
LOG:  startup process (PID 70) exited with exit code 1
LOG:  shutting down due to startup process failure
LOG:  database system is shut down
```

## 3. Reading the evidence — this is a disk fault, not a corruption fault

The scary line is the `HINT` about corruption. It is almost certainly a red
herring, and the reason matters, because it decides whether the next step is
"add disk" or "restore from a backup that may not exist".

Three things in the log say the data is intact:

1. **Redo completed.** `redo done at D/C2FFF350` is PostgreSQL reporting that
   it replayed every WAL record from the last checkpoint to the end of the log
   without encountering a bad record. A genuinely corrupt WAL stream fails
   *during* redo with an invalid-record or CRC error. This one did not.
2. **It completed identically twice.** Both restarts start redo at
   `D/BE279468` and finish at exactly `D/C2FFF350`, in ~3.87 s. Replay is
   deterministic and reproducible, which is what a healthy WAL chain looks
   like.
3. **The failure is after redo, and it is an ENOSPC on a temp file.**
   `xlogtemp.NN` is the temporary name PostgreSQL uses while creating a *new*
   16 MB WAL segment. That happens at the end-of-recovery checkpoint, once
   replay has already succeeded. With ~5 MB free, a 16 MB allocation cannot
   succeed.

The `HINT` is emitted unconditionally whenever the server finds it was
interrupted while already in recovery. It describes a possibility, not a
diagnosis — and here, the interruption that triggered it was the previous
restart of this same ENOSPC loop.

**Conclusion: the recovery needs roughly 16–64 MB of headroom to finish.** The
committed data is very likely whole. This should still be *verified* after the
server starts (§6), not assumed — but it changes the remedy from "restore" to
"add space", which is the difference between a routine fix and a data-loss
event.

## 4. What is NOT to be done

Each of these turns a recoverable incident into an unrecoverable one:

- Do not delete or move anything under `pg_wal/`. Those segments are what
  redo just replayed successfully. Deleting them destroys the thing that is
  currently working.
- Do not `initdb`, re-create, or "reset" the database or the volume.
- Do not run `pg_resetwal`. It discards transactions and is for a database
  that cannot replay — this one replays fine.
- Do not delete the volume. Railway queues deletion and purges within 48
  hours; after that it is permanent.
- Do not wipe the volume. Railway's documentation is explicit that **wiping a
  volume deletes all of its backups too.**
- Do not attempt `DELETE`/`TRUNCATE` to free space, even if the server
  starts. A `DELETE` in PostgreSQL *increases* short-term usage: it writes WAL
  and leaves dead tuples until vacuum, and the space is returned to the table,
  not the filesystem. See §7 for the correct order.

## 5. Backups: what is known and what is not

**Unverified — and it must be checked before anything else.**

The project has **no Railway buckets**, so there is no pgBackRest
object-storage archive. The container log line
`pgbackrest: restore-gate WAL_RECOVER_FROM_BUCKET= ... RESTORED_MARKER=missing`
confirms no bucket is wired up.

That is *not* the same as having no backups. Railway's **volume backups** are
a separate feature — snapshots, manual or scheduled daily/weekly/monthly,
listed under the service's **Backups** tab. That tab is not exposed through
the API surface available to this session, so **whether snapshots exist is
unknown and must be read from the dashboard.**

Two consequences:

- If a recent snapshot exists, it is a genuine rollback path and the risk of
  everything below drops sharply. **Check this first.**
- A manual backup **cannot be taken right now regardless.** Railway limits
  manual backups to 50% of volume capacity, and this volume is 99.9% full.
  Railway's own guidance for that case is to grow the volume first.

So growing the volume is a prerequisite for *both* recovery and backup. There
is no ordering in which a backup comes first.

## 6. Recovery procedure

### Step 1 — Read the Backups tab (owner, 30 seconds, no risk)

Railway dashboard → project `jubilant-liberation` → `Postgres-gxQB` →
Settings → Backups. Record whether any snapshot exists and its timestamp.
This is pure information; it changes nothing.

### Step 2 — Grow the volume (owner, ~1 minute)

Railway dashboard → `Postgres-gxQB` → Settings → Volume → increase size.

**5000 MB → 20000 MB** is the recommendation. Rationale:

- Recovery needs tens of MB; 15 GB is not for recovery, it is so the database
  can run normally afterwards while raw-payload retention is re-established
  and a dump is taken. A dump needs room; so does `VACUUM FULL` if it is ever
  needed (it rewrites the table, needing space for both copies).
- Railway bills volumes on **used** storage, not provisioned. Growing the
  ceiling from 5 GB to 20 GB does **not** cost 4× — the bill tracks the
  ~5 GB actually used, so the immediate cost change is approximately **$0**.
  Headroom on Railway is free until you use it.
- Down-sizing is not supported, so this is one-way. It is still the right
  call: the alternative is an unbootable production database.

**Plan caveat — the likely blocker.** Railway's per-plan volume sizes are
0.5 GB (Free/Trial), **5 GB (Hobby)**, 50 GB (Pro). This volume sits at
exactly 5000 MB, which strongly suggests a **Hobby** plan at its ceiling. If
the dashboard refuses the resize, it requires a **Pro upgrade ($20/seat/month)**
— and that is a recurring cost increase, so it is the owner's decision, not
one this session will make. It is also the only route: there is no way to free
space inside a volume whose database will not start.

Railway performs the resize live, except at 100% capacity, where it
automatically does an **offline** resize with integrity checks and restarts
the service. That is this case, and it is the sanctioned path — the service is
already down, so the restart costs nothing.

### Step 3 — Confirm clean start

Watch the deploy logs. Success looks like:

```
LOG:  database system was not properly shut down; automatic recovery in progress
LOG:  redo starts at D/BE279468
LOG:  redo done at D/C2FFF350
LOG:  checkpoint starting: end-of-recovery
LOG:  checkpoint complete: ...
LOG:  database system is ready to accept connections
```

The line that matters is **`database system is ready to accept connections`**.
If ENOSPC appears again, the resize did not take effect — do not proceed.

### Step 4 — Integrity verification (before trusting the data)

Run against the recovered database, read-only:

```sql
-- Server is up and writable
SELECT version(), pg_is_in_recovery();

-- No unexpected WAL backlog
SELECT pg_current_wal_lsn();

-- Per-table sanity. Compare against docs/ recorded counts.
SELECT schemaname, relname, n_live_tup
FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 40;

-- Migration state: must match the repository's forward-only migration set
SELECT * FROM schema_migrations ORDER BY version;  -- adjust to actual table
```

Then a full physical read, which is the real corruption test — it forces every
page of every table through the server and will raise on a bad checksum or an
unreadable block:

```bash
pg_dump --schema-only "$DATABASE_URL" > /dev/null   # fast structural check
pg_dumpall --globals-only "$DATABASE_URL" > globals.sql
```

`scripts/db_parity.py --source "$DATABASE_URL" --out source.json` captures the
machine-readable baseline (migration version, table inventory, row counts,
sequence values) used later for AWS parity. See §8.

### Step 5 — Relieve the pressure, correctly

Only once the server is up and verified. The repository's own analysis
(`docs/raw-payload-retention.md`) records raw payload growth at ~166 MB/day,
which is what filled the volume.

Correct order — this matters, because the naive order makes things worse:

1. Set `RAW_RETENTION_DAYS=10` and run `raw-retention` with
   `RAW_RETENTION_DRY_RUN=true` first, to see what it *would* remove.
2. Run it for real. It deletes in bounded batches
   (`RAW_RETENTION_BATCH_LIMIT`), which is what you want — a single huge
   `DELETE` writes a matching volume of WAL and can re-fill the disk.
3. Only then `VACUUM` (plain, not `FULL`) to make the space reusable by the
   table.
4. `VACUUM FULL` returns space to the filesystem but needs room for a second
   copy of the table. Do not run it until there is verified headroom — which
   is another reason Step 2 grows to 20 GB rather than 6 GB.

### Step 6 — Take the backup that does not exist yet

With the server healthy and space available:

```bash
pg_dump --format=custom --compress=9 --verbose \
  --file=hawknetic-prod-$(date -u +%Y%m%dT%H%M%SZ).dump "$DATABASE_URL"
```

Verify it before trusting it — an unverified dump is not a backup:

```bash
pg_restore --list hawknetic-prod-*.dump | head -50
```

Then restore it into a throwaway database and re-run `scripts/db_parity.py`
against that restore. If the parity JSON matches the source, the dump is
proven. Also enable a Railway **scheduled daily backup** at this point; its
absence is what made this incident dangerous rather than routine.

## 7. Why this recurs unless the design changes

The volume did not fill by accident. `raw.source_payloads` accretes every
cycle of every collector at ~166 MB/day against a 5 GB ceiling — roughly a
30-day fuse from empty. Retention shortens the fuse's reach but does not
change the shape: an always-growing table on a fixed volume, where the pruner
is itself a database client.

The AWS target must not reproduce it. See `docs/aws-migration/service-map.md`
and the RDS module: storage autoscaling (`max_allocated_storage`), a
CloudWatch `FreeStorageSpace` alarm that fires well before the cliff, and
archival of aged raw payloads to S3 rather than indefinite retention in
PostgreSQL. Relational operational data stays in PostgreSQL; only the raw
payload bodies move.

## 8. Parity procedure (source → RDS)

`scripts/db_parity.py` emits a machine-readable snapshot and diffs two of
them. It takes a read-only connection and prints no credentials.

```bash
# Baseline from the recovered Railway source
python scripts/db_parity.py --source "$SOURCE_URL" --out source.json

# After restoring into RDS
python scripts/db_parity.py --source "$TARGET_URL" --out target.json

# Compare; non-zero exit on any discrepancy
python scripts/db_parity.py --compare source.json target.json
```

It reports: migration version, schema/table inventory, per-table row counts,
sequence `last_value`, and per-schema totals. Cutover is blocked while any
discrepancy is unexplained.

## 9. Rollback

Railway stays running and untouched throughout the AWS migration. Until the
owner approves retirement:

- Railway remains the system of record and the rollback target.
- The AWS environment runs as a shadow, reading a restored copy.
- No DNS change occurs without explicit approval.
- If AWS parity fails, the action is to stop, not to force cutover.

For the incident itself, the rollback path is a Railway volume snapshot — if
Step 1 finds one. If it does not, there is no rollback, which is precisely why
Steps 1 and 6 are ordered where they are.

## 10. Owner action summary

| # | Action | Who | Risk | Cost |
| --- | --- | --- | --- | --- |
| 1 | Read the Backups tab; record what exists | Owner | None | $0 |
| 2 | Grow volume 5000 MB → 20000 MB | Owner | Low; offline resize restarts an already-down service | ~$0 (billed on used, not provisioned) |
| 2a | If refused: upgrade Hobby → Pro | **Owner decision** | None technical | $20/seat/month |
| 3 | Confirm `ready to accept connections` | Either | None | $0 |
| 4 | Integrity + parity baseline | This session | Read-only | $0 |
| 5 | Retention prune, then `VACUUM` | This session | Bounded batches | $0 |
| 6 | Verified `pg_dump` + enable scheduled backups | This session | None | pennies |

Steps 1 and 2 cannot be performed from this session: the Railway MCP surface
available here exposes volume rename and remount, but not resize, and no
Railway CLI or API token is present. Step 2 is a dashboard action.
