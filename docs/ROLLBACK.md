# Recovery and Rollback

Two different situations, in the order you are likely to need them:

1. [Recovering the production database from a full volume](#1-recovering-a-full-volume) — the live incident as of 2026-09-08
2. [Rolling back a bad deploy](#2-rolling-back-a-deploy)
3. [Undoing the changes made by the 2026-09-08 audit](#3-undoing-the-audit-changes)

---

## 1. Recovering a full volume

### What happened

On 2026-09-01 the production PostgreSQL volume reached 100% and the database
PANICked mid-write:

```
PANIC:  could not write to file "pg_wal/xlogtemp.16755": No space left on device
FATAL:  could not write to file "pg_wal/xlogtemp.16771": No space left on device
LOG:    startup process (PID 16771) exited with exit code 1
LOG:    shutting down due to startup process failure
```

### Why a restart will not fix it

Crash recovery replays the write-ahead log, and replaying it requires writing.
On a volume with no free space PostgreSQL cannot finish recovery, so it exits
during startup — every time. Restarting or redeploying the service reproduces
the failure exactly. **The volume has to grow before anything else can work.**

### Prerequisites

Railway's Hobby plan provisions 5 GB volumes and 5 GB is the plan ceiling, so
step 1 requires the **Pro** plan ($20/month, includes $20 of usage). This is the
one step with no alternative: the data cannot be read, backed up, dumped or
pruned while the database cannot start.

### The order that works

Do not reorder these. Steps 2 and 5 exist because a prune frees space *inside*
the table and `VACUUM FULL` is what returns it to the filesystem — and
`VACUUM FULL` needs free space of its own to rewrite into.

1. **Grow the volume.** Railway dashboard → project `jubilant-liberation` →
   environment `production` → service `Postgres-gxQB` → the volume → **Live
   Resize**. Take it to **15 GB**. Because volumes bill on *used* space, not
   provisioned, the extra headroom costs nothing until it is occupied.
   At 100% capacity Railway performs an offline resize with a brief restart,
   which is expected here.

2. **Confirm the database starts and take a backup.** Watch the service logs for
   `database system is ready to accept connections`. Then, in the service's
   **Backups** tab, take a manual backup. Do not skip this: everything after
   this point deletes data. Manual backups are capped at 50% of volume size,
   which is why the resize comes first.

3. **Verify the application recovers.** The three workers and the web service
   should stop crash-looping on their own once
   `postgres-gxqb.railway.internal` resolves again. Redeploy
   `HawkNeticSportsTools` if its pre-deploy migration does not retry, and check:

   ```
   GET /healthz   → {"status": "ok"}
   GET /readyz    → database.ready: true
   ```

4. **Prune, in bounded passes.** `RawRetentionProduction` already has
   `RAW_RETENTION_DAYS=10` and `RAW_RETENTION_DRY_RUN=false` set (applied by the
   2026-09-08 audit), so its hourly cycle will start draining the backlog. To
   drive it manually and watch the numbers:

   ```bash
   PYTHONPATH=src python -m kalshi_research_bot raw-retention --report-only
   PYTHONPATH=src python -m kalshi_research_bot raw-retention \
       --older-than-days 10 --limit 2000 --apply
   ```

   Repeat until `still_eligible` reaches 0. Check `window_bites: true` — if it
   is false the window is wider than the data's age and nothing will ever be
   pruned.

5. **Return the space to the filesystem.**

   ```sql
   VACUUM (ANALYZE) raw.source_payloads;   -- makes freed space reusable
   VACUUM FULL raw.source_payloads;        -- returns it to the volume
   ```

   `VACUUM FULL` takes an exclusive lock and needs free space equal to the
   table's current size. This is why the volume was grown first.

6. **Confirm the steady state.** A ten-day window should settle the database
   around 2.5-2.7 GB. Read it back:

   ```bash
   PYTHONPATH=src python -m kalshi_research_bot raw-retention --report-only
   ```

   `/internal/status.json` now carries a `storage` block and will raise a
   `database_capacity` anomaly at 75% used and a critical one at 90%.

7. **Decide on the plan.** With the database at ~2.5 GB, Hobby's 5 GB ceiling is
   viable again and is $6.29/month cheaper. Downgrading requires the volume to
   be within Hobby's limits; volumes cannot be shrunk, so a 15 GB volume keeps
   the account on Pro. Staying on Pro is the more conservative choice for a
   database that has hit its ceiling twice.

### Deleting the two obsolete staging databases

Worth **$25.38/month** — the largest single saving available in this account —
but it destroys data that could not be inspected during the audit.

`Postgres` (staging, us-west2, 4.994 GB) and `Postgres-GDG0` (staging, iad,
4.987 GB) back `HawkNeticResearchStaging` (last successful deploy: never, FAILED
2026-07-13), `SportsResearchStaging` (FAILED 2026-08-16) and
`KalshiIngestionStaging` (never deployed).

Before deleting either one:

1. Start it (it must be running to be backed up or dumped).
2. Take a Railway backup **and** a `pg_dump` you have downloaded and can open.
3. Confirm the dump restores into a scratch database and the row counts match.
4. Only then delete the service.

Railway queues deleted volumes for 48 hours and emails a restoration link, so a
mistake is recoverable for two days. After that it is not.

---

## 2. Rolling back a deploy

Railway retains deployment images for 72 hours on Hobby and 120 hours on Pro.

**Within the retention window** — dashboard → service → Deployments → the last
known-good deployment → **Rollback**. This restores that image, its settings and
its variables as a new deployment. No rebuild.

**Outside it** — use **Redeploy** on the old deployment, which rebuilds from the
original commit with that deployment's variables.

**From git** — every production service deploys `Master`:

```bash
git revert <bad-commit>
git push origin Master
```

Note that a push deploys immediately: Railway's GitHub integration is configured
with `checkSuites: false`, so CI runs in parallel with the deploy rather than
gating it. Until that is changed (dashboard → service → Settings → Source →
**Wait for CI**), a revert is faster than a fix-forward.

### If a migration is the problem

Migrations run as the pre-deploy command, so a failing migration fails the
deploy and the previous version keeps serving. That is the intended behaviour
and it is why the current outage shows as `FAILED` deploys rather than a broken
application.

Migrations are versioned and forward-only (`migrations/postgres`, currently
0001-0016). There is no down-migration path: recovering from a bad migration
means restoring the backup taken before it.

---

## 3. Undoing the audit changes

Everything the 2026-09-08 audit changed, and how to reverse it.

### Repository

One revert covers all of it:

```bash
git revert <audit-commit>
```

| Change | Effect if reverted |
| --- | --- |
| `DEFAULT_RETENTION_DAYS` 30 → 10 | Window returns to a size the volume cannot hold |
| `.env.example` `RAW_RETENTION_DAYS` 45 → 10 | Same, for new environments |
| `database_capacity_state()` + `database_capacity` anomaly | Volume capacity stops being alarmed on |
| `DATABASE_VOLUME_CAPACITY_BYTES` | Capacity ceiling becomes unconfigurable |
| `scripts/local.sh` `HAWKNETIC_LOCAL_DB` modes | Local development requires Docker again |
| `tests/test_local_workflow.py` | Reverts to asserting the Docker-required message |
| `.github/dependabot.yml` | Dependency updates stop being proposed |
| CI `concurrency` block | Superseded CI runs stop being cancelled |

No migration was added, so no schema change needs undoing. The `storage` key
added to `/internal/status.json` is additive.

### Railway

Variables set on `RawRetentionProduction` in `production`:

| Variable | Set to | Was |
| --- | --- | --- |
| `RAW_RETENTION_DAYS` | `10` | not readable — this connection returns values redacted |
| `RAW_RETENTION_DRY_RUN` | `false` | not readable |
| `DATABASE_VOLUME_CAPACITY_BYTES` | `5000000000` | not previously set |

These were applied with `skipDeploys`, so they take effect on that service's
next deployment. Setting `RAW_RETENTION_DRY_RUN=true` restores report-only
behaviour; deleting `DATABASE_VOLUME_CAPACITY_BYTES` falls back to the 5 GB
default compiled in.

No service, volume, database or deployment was created, deleted or restarted by
the audit.
