# AWS migration status

Last updated: 2026-09-12

Format per the migration brief: COMPLETED / IN PROGRESS / BLOCKED / NEEDS OWNER
APPROVAL / NEXT. Nothing is marked COMPLETED without evidence.

---

## BLOCKED — the two things that need the owner

### 1. Production PostgreSQL is down and cannot be recovered from here

`Postgres-gxQB` has been crash-looping since 2026-09-11 18:24 UTC. The volume
is **4.994777088 GB of 5.0 GB** (measured, 1441 samples over 24 h) — about
5 MB free.

Diagnosis, from the deployment logs: WAL redo **completes cleanly**
(`redo done at D/C2FFF350`, identically on two consecutive restarts), and the
FATAL comes *after* it, creating a new 16 MB WAL segment:
`could not write to file "pg_wal/xlogtemp.70": No space left on device`. So
this is a disk fault, not the corruption the startup `HINT` suggests, and the
remedy is headroom rather than a restore.

**Minimum owner action** — two dashboard steps, ~2 minutes:

1. Railway → `jubilant-liberation` → `Postgres-gxQB` → Settings → **Backups**.
   Record whether any snapshot exists. Pure information, changes nothing.
2. Same service → Settings → **Volume** → grow **5000 MB → 20000 MB**.

Why this session cannot do it: the Railway MCP surface here exposes volume
rename and remount but **not resize**, and no Railway CLI or API token is
present. It is a dashboard action.

Cost: approximately **$0** — Railway bills volumes on *used* storage, not
provisioned, so raising the ceiling does not raise the bill until the space is
used.

**Possible escalation:** the volume sits at exactly 5000 MB, which is Railway's
Hobby plan size. If the dashboard refuses the resize it needs a **Pro upgrade
($20/seat/month)** — a recurring cost decision, and therefore the owner's. It
is also the only route: space cannot be freed inside a volume whose database
will not start.

Full procedure, including why `pg_resetwal`, WAL deletion and `DELETE`-to-free-
space are each forbidden here: `docs/aws-migration/database-recovery.md`.

### 2. No AWS credentials in this environment

`aws sts get-caller-identity` fails with `InvalidClientTokenId`. The
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` present in the environment are
14-character sandbox-proxy placeholders beginning `prox`, not AWS credentials.

Consequently **none** of the following could be verified, and nothing in this
document claims otherwise:

- account identity, Organizations structure, member accounts
- IAM Identity Center configuration and permission sets
- root MFA state, existing IAM users, existing access keys
- existing VPC / RDS / ECS / ECR / S3 resources
- current spend

**Minimum owner action:** an authenticated non-root session — IAM Identity
Center (`aws sso login`) is the documented path and no static key should be
created. Per the brief, this session will not ask for a password, MFA code,
access key, secret, or session token.

---

## COMPLETED

Everything below was executed and verified in this session.

### Container packaging — verified by building and running it

`Dockerfile`, `.dockerignore`.

Evidence, against a real PostgreSQL 18.4 in Docker:

| Check | Result |
| --- | --- |
| Image builds | Yes, 237 MB |
| Runs as non-root | uid/gid 10001 |
| Migrations discovered | 16 `.sql` files at `/app/migrations/postgres` |
| `database-migrate` | Applied 0001–0016, `"ready": true`, no pending |
| `/healthz` | HTTP 200 |
| `/readyz` | database `configured_healthy`, auth required+configured |
| Research-only flags | `RESEARCH_ONLY: true`; live execution, auto-upload, auto-trade, order upload, model promotion all `false` |
| Docker healthcheck | `healthy` |

**A real defect was caught and fixed during this.** The first version installed
the wheel. `config.repo_path()` resolves repository files as
`Path(__file__).parents[2] / <parts>`, which under a wheel install points into
the interpreter's lib directory — `discover_migrations()` would have found
nothing and `database-migrate` would have reported success having applied zero
migrations. Silent, and it would have reached production as an empty schema.
The image now keeps the source layout and runs with `PYTHONPATH=/app/src`,
matching how `Procfile`, `nixpacks.toml` and `railway.json` already start it.

### Database parity tooling — verified in both directions

`scripts/db_parity.py`.

| Check | Result |
| --- | --- |
| Capture against real schema | 74 tables, 51 sequences, migration head `0016` from `ops.schema_migrations` |
| Credential redaction | Endpoint printed as `postgresql://127.0.0.1:54399/hawknetic` — no user or password |
| Identical snapshots | `PARITY OK`, exit 0 |
| Planted drift | All 4 detected (migration head, migration count, unexpected table, row count), exit 1 |

Row counts use `count(*)` rather than `pg_stat_user_tables.n_live_tup`: the
statistics view is an autovacuum-refreshed estimate and is routinely wrong on a
freshly restored database, which is exactly when this runs.

### Terraform workspace — all of it validates

`infrastructure/aws/`: bootstrap, `environments/{dev,prod}`, and nine modules
(`network`, `rds`, `ecr`, `ecs`, `scheduler`, `storage`, `secrets`,
`github-oidc`, `observability`).

| Check | Result |
| --- | --- |
| `terraform fmt -check -recursive` | Clean |
| `validate` bootstrap | Success |
| `validate` environments/dev | Success |
| `validate` environments/prod | Success |
| `validate` all 9 modules standalone | All OK |

Terraform 1.13.1, AWS provider ~> 6.0. S3 native state locking
(`use_lockfile`) instead of a DynamoDB lock table — one fewer resource to
create and forget per environment.

Security properties built in rather than documented:

- RDS `publicly_accessible = false`, not exposed as a variable
- Database ingress is **security-group reference only** — there is no CIDR
  variable, so `5432` open to `0.0.0.0/0` is unrepresentable
- OIDC trust conditions anchored to `repo:Dhawkins223/HawkNeticSportsTools:`,
  with a validation rule rejecting a bare `*` subject
- No static AWS keys anywhere; the plan role is read-only and the deploy role
  is gated on a GitHub environment
- Secrets Manager holds containers only — no value passes through Terraform, so
  none lands in state
- Storage autoscaling required by a validation rule that rejects
  `max_allocated_storage == allocated_storage`

### Repository test gate

| Check | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `./scripts/local.sh test` | **1076 tests, OK**, 117.8 s |

Worth recording because the first three runs did not look like that. They
reported 310 errors and 1 failure, all `RuntimeError: postgres_pool_unavailable`.
The cause was not the changes here: `psycopg_pool` was absent from this
container. Confirmed by running the same suite on a pristine `origin/Master`
worktree, which produced the identical 310/1 result, and by the fact that no
commit on this branch touches `src/`, `tests/` or `migrations/`. Installing
`psycopg_pool>=3.2,<4` turned the suite green.

The one genuine failure in those runs
(`test_refresh_payload_keeps_slip_live_when_ledger_logging_fails`) was a
downstream effect of the same missing pool and passes with it installed.

### Review round: 48 bot findings triaged and fixed

Two automated review passes raised 48 findings. They were verified rather than
accepted wholesale, and the substantive ones were real. The three that would
have broken a deployment:

**The tasks could never have reached the database.** `DatabaseSettings.from_env()`
reads `DATABASE_URL` and nothing else, and `require_url()` rejects anything
without a postgres scheme -- there is no fallback to `POSTGRES_HOST`/`USER`/
`PASSWORD`. The task definitions injected the parts and no URL. ECS cannot
concatenate a URL out of a JSON secret's keys, so the image's entrypoint now
composes one when `DATABASE_URL` is unset. Verified by running
`database-migrate` with only the parts ECS injects and a password containing
`/`, `?` and `@`: all 16 migrations applied, `/healthz` 200, `/readyz` healthy.
An explicitly-set `DATABASE_URL` still wins, so Railway, Codespaces and CI are
unaffected.

**Nothing applied migrations.** Every service starts with `service-start`; a
fresh RDS database would have come up empty while the rollout reported success.
There is now a separate run-once migration task definition, deliberately not
folded into the shared entrypoint -- nine roles run this image, and migrating
on start would mean nine concurrent attempts on every deploy.

**The budget matched nothing.** `"user:Project$${var.project_tag}"` escapes the
dollar, which makes Terraform read the rest as literal text, so the cost filter
looked for a tag value of the string `${var.project_tag}`. A budget matching no
resources reports zero spend and never notifies.

Also fixed, with the reasoning recorded at each site: the PR-triggered plan role
could overwrite or delete every environment's Terraform state; the deploy role
could update any ECS service in the account; a worker's environment map could
override `HAWKNETIC_SERVICE` and run the wrong role; the scheduler role lacked
the `sqs:SendMessage` a dead-letter queue needs, so the queue would have stayed
silently empty; `bucket_key_enabled` was set alongside SSE-S3, where it does
nothing; AZ names were hardcoded to Ohio; the CIDR validation accepted a /24;
and the bootstrap script called `python`, cloned a second checkout over the one
it lives in, and ran `terraform validate` in a directory with no `.tf` files.

The parity tool grew the checks it was missing. It now compares the full
applied-migration set (head and count alone pass a target missing 0009 but
carrying an extra 0017), rejects a snapshot with no migration ledger, reports
target-only sequences, captures at `REPEATABLE READ` so one snapshot is one
consistent view, refuses to compare a database against itself, and -- the real
gap -- hashes every row of every table. Row counts and schema shape cannot
detect wrong *values*. Verified: one changed character in one of 1000 rows is
caught, and a clean dump/restore still reports parity.

The migration script now writes artifacts at 0600 (a dump of production was
world-readable), keeps passwords out of `argv` entirely, proves the dump
decompresses in full rather than trusting its table of contents, and compares
the target against the parity baseline captured with the dump rather than a
later live source snapshot -- which on a source still taking writes would have
reported ordinary collector activity as migration defects.

Two claims were corrected rather than defended. The cost headline said "5-10x"
while citing a $5-30/month Railway baseline against ~$217/month, which is ~7x
at one end and ~43x at the other. And the `raw-archive` S3 bucket was described
as the fix for database growth when nothing writes to it: `raw-retention` still
deletes aged payloads. Storage autoscaling and the FreeStorageSpace alarm are
what actually prevent a repeat; the bucket is a prepared destination.

### Documentation

| Document | Contents |
| --- | --- |
| `database-recovery.md` | The live incident, evidence, why it is a disk fault, the forbidden actions, recovery and parity procedure |
| `service-map.md` | Every Railway service → AWS destination, with cadences from `SERVICE_SPECS` |
| `cost-model.md` | Line-by-line derivation, budget recommendation, savings levers |
| `STATUS.md` | This file |

### CI

`.github/workflows/terraform.yml` — fmt, validate per environment, validate
every module standalone. The plan job is conditional on an
`AWS_PLAN_ROLE_ARN` repository variable, so it stays skipped rather than
red until the role exists. There is no apply job.

---

## NEEDS OWNER APPROVAL

### AWS will cost roughly 7× Railway at best, over 40× at worst

Estimated **~$169/month production**, **~$48/month dev**, **~$217 combined**,
against a measured Railway bill of **$5–30/month**
(`docs/CURRENT_INFRASTRUCTURE.md`).

Two line items with no Railway equivalent account for most of it: the ALB
(~$18) and NAT Gateways (~$66) — about **$84/month before a container runs**.

Recommended change: **`single_nat_gateway = true`**, saving $32.85/month. AZ-
redundant egress is poor value for a collector whose worst case is a missed
cycle. That brings production to ~$136 and the combined figure to ~$184.

Full derivation and the other levers: `docs/aws-migration/cost-model.md`.

Flagging one more thing honestly: `docs/TARGET_INFRASTRUCTURE.md`, already
merged in this repository, concluded with evidence that **Railway should stay**
and that the fix was to schedule the slow workers. This migration is the
owner's call and the work proceeds, but the two documents disagree and the cost
figures above are why.

### Five workers are not deployed in production

The live Railway project has **five** worker services, not eight.
`external-source-ingestion`, `crypto-research`, `research-model-refresh` and
`reporting-evaluation` have no Railway service.

Either they were never deployed (so the AWS environment is *adding* capacity,
which should be deliberate), or they were removed (so production is currently
degraded). The prod Terraform defines all eight because the code does.
**Confirm which before the first apply** — it changes the cost baseline.

### Two unidentified PostgreSQL services

`Postgres-GDG0` and `Postgres` exist in the production environment alongside
`Postgres-gxQB`. Neither is the production database. Contents and purpose
unresolved; nothing will be migrated or retired until they are identified.

---

## NEXT

Once the database is recovered (blocker 1):

1. Capture the parity baseline with `scripts/db_parity.py`.
2. Run retention at `RAW_RETENTION_DAYS=10`, dry-run first, then `VACUUM`.
3. Take and **verify** a `pg_dump`; enable Railway scheduled backups.
4. Re-measure worker cycle durations to replace the 60-second estimate in the
   cost model.

Once AWS authentication exists (blocker 2):

5. Verify the security baseline: root MFA, no root access keys, Identity Center
   administrator, no stray IAM users.
6. `terraform apply` the bootstrap stack, migrate its state into its own bucket.
7. Apply `environments/dev` and prove the full path end to end.
8. Build and push the image; deploy dev; restore the dump into dev RDS; run
   parity.
9. Only then plan production.

Independent of both:

10. Design the autonomous agent control plane with explicit concurrency and
    budget bounds (`agent-control-plane.md`, not yet written).

---

## Untouched, by instruction

- **Hawknetic Office** (`ravishing-elegance`,
  `a45378b2-ea1c-4a53-966a-1d22ec2336ea`) — not read, modified, redeployed or
  reconfigured. Out of scope.
- **Railway production** — no service, variable, volume or deployment changed.
  It remains the rollback platform.
- **Render** `HawkNeticSports` — left broken and untouched. Repairing it would
  create a second live deployment of `Master`, which is worse than a failed
  one. Retire on approval once AWS is stable.
- **Neon** — organization `David` has 0 projects. Nothing created.
- **DNS** — no record created or changed. Cutover is a separate, approved act,
  and the Route 53 record is deliberately absent from the Terraform.
