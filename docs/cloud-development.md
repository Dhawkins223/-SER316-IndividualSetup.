# Cloud development

GitHub Codespaces is the canonical development environment for this repository.
GitHub is the source of truth and Railway remains the hosted runtime. A Windows
checkout, WSL, and Docker Desktop are no longer required to build, run, test, or
prepare a deployment.

Docker remains part of CI and optional local validation, but the canonical
Codespace no longer depends on a nested Docker daemon. This keeps the cloud
workspace reliable and avoids Docker Desktop entirely. Use Railway's injected
`DATABASE_URL` for cloud data; GitHub Actions continues to exercise the
Compose-backed PostgreSQL tests. Docker is not used to build the Railway
application; Railway continues to use Nixpacks.

## Architecture and data boundary

```text
GitHub Codespace (Python 3.12 + pinned cloud tools)
  -> feature branch / pull request -> GitHub Actions
  -> reviewed Master revision -> existing Railway services

Codespace Compose PostgreSQL: development and isolated tests only
Railway PostgreSQL: staging or production only; never copied into .env
```

Compose is the default development backend, not a requirement. Setting both
`HAWKNETIC_DATABASE_URL` and `HAWKNETIC_TEST_DATABASE_URL` points
`scripts/local.sh` at a managed development database — two Neon branches, or a
PostgreSQL installed directly on the machine — and Compose is never invoked, so
no Docker daemon is needed at all. The full suite has been run this way. Both
URLs are required and must name different databases, and a Railway or Render host
is refused unless `HAWKNETIC_ALLOW_HOSTED_DATABASE` is set. See
`docs/TARGET_INFRASTRUCTURE.md` for why Neon is the right fit for a development
database and the wrong one for this production database.

The one Python web process in `paper_server.py` serves both the dashboard and
`/api/v1`. Workers are always-on Python processes whose internal `WorkerSpec`
loops provide cadence; they use the PostgreSQL `ops` schema and do not use a
queue or Redis. Migrations are raw, forward-only SQL in `migrations/postgres/`.

Production data already lives in Railway. This migration does not dump, restore,
copy, seed, reset, or otherwise modify it. A fresh Codespace receives a fresh
development database and applies the schema to that database only.

## Open the repository

1. In GitHub, open the repository, choose **Code**, **Codespaces**, then
   **Create codespace** on the feature branch you intend to use.
2. Wait for `postCreateCommand` to finish. It installs Python dependencies and
   pinned cloud tools. Local Compose setup is optional and is skipped when Docker
   is unavailable in the Codespace.
3. The generated `.env` is ignored by Git and contains development placeholders.
   Replace only the optional source credentials needed for the work.

The setup hook is idempotent. On later starts, `postStartCommand` starts local
PostgreSQL only when a Docker daemon is available; cloud-only sessions remain
unblocked.

## Included tools

The container supplies Python 3.12, PostgreSQL client tools, shellcheck, and the
VS Code Python/Ruff/GitHub Actions extensions. The setup script installs
checksum-pinned Bun and Railway CLI releases and a commit-pinned gstack checkout.
No Node.js application, package manager workflow, JavaScript framework, or
frontend build exists in this repository; Bun is installed only because gstack
requires it.

## Start development

```bash
./scripts/local.sh dev
```

The Codespace sets `DASHBOARD_HOST=0.0.0.0` and `PORT=8765`. Open the forwarded
**HawkNetic research dashboard** port from the Ports panel. The port is private by
default. PostgreSQL is forwarded privately on `54329` for operator inspection;
Compose itself binds it only to the Codespace loopback interface.

Useful commands:

```bash
./scripts/local.sh db-start
./scripts/local.sh db-status
./scripts/local.sh logs
./scripts/local.sh migration-status
./scripts/local.sh research-status
./scripts/local.sh research-once
./scripts/local.sh stop
```

## Migrations, tests, and reset

Apply all forward-only migrations to the development database:

```bash
./scripts/local.sh migrate
```

Initialize/migrate the isolated test database and run the complete test suite:

```bash
./scripts/local.sh test
```

Run the PostgreSQL-focused tests or the complete local gate:

```bash
./scripts/local.sh test-integration
./scripts/local.sh smoke
./scripts/local.sh verify
```

Reset only the disposable Codespace development volume:

```bash
./scripts/local.sh db-reset
# Type RESET only after checking that .env contains the Codespace Compose values.
./scripts/local.sh migrate
```

`db-reset` targets the explicit `hawknetic-local` Compose project and never
contacts Railway. There is intentionally no production seed or database-copy
command.

## Health and readiness

The existing endpoints remain authoritative:

- `/healthz` proves that the Python process can answer.
- `/readyz` verifies PostgreSQL reachability and migration state, source-data
  freshness, hosted authentication configuration, and all research-only safety
  controls. It returns `503` when any gate blocks.

With the dashboard running:

```bash
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/readyz
```

An empty fresh development database can make `/readyz` correctly return `503`
until current source-backed data exists; do not weaken that freshness gate to
turn the response green.

## Environment variables and secrets

Copy no production value into a Codespace. The complete scope and sensitivity
inventory is in [environment-variables.md](environment-variables.md).

The base setup, migrations, lint, and tests require no external-provider secret.
The owner may add Codespaces Secrets for Kalshi, Firecrawl, an odds source,
Airtable, Slack, and a narrowly scoped Railway audit token when that integration
is needed. The Kalshi private key must be stored as a file outside the repository
and referenced by `KALSHI_PRIVATE_KEY_PATH`; never paste private-key material into
`.env`.

Codespaces Secrets and Railway Variables are separate stores. Staging and
production receive separate databases, credentials, auth passwords, source
credentials, and readiness evidence. GitHub Actions receives only its ephemeral
PostgreSQL service credentials and has no Railway token.

## Existing Railway configuration audit

This is a repository/configuration audit, not a deployment change. The last
read-only hosted checkpoint is `docs/cloud-runtime-checkpoint-2026-08-28.md` and
must be refreshed before any hosted action.

| Concern | Existing configuration |
| --- | --- |
| Root directory | Repository root for the tracked config files; the actual per-service Railway root/config-path selection requires a live read-only verification. |
| Build | `railway.json` and `railway.worker.json` select `NIXPACKS`; `nixpacks.toml` supplies the direct paper-server fallback command. |
| Web start | `railway.json` runs `PYTHONPATH=src python -m kalshi_research_bot service-start`; `HAWKNETIC_SERVICE=web` selects the single dashboard + API process. |
| Worker start | `railway.worker.json` runs the same launcher; each service selects its worker through `HAWKNETIC_SERVICE`. |
| Health | Both configs use existing `/healthz`, 300-second timeout. Workers expose a health server on `PORT`; web also exposes `/readyz` for readiness. |
| Port | Railway-provided `PORT`; hosted web binds `0.0.0.0`. |
| Database dependency | Every stateful service receives its environment's PostgreSQL `DATABASE_URL`; no Redis or other queue exists. |
| Migration behavior | `railway.json` has the forward-only database migration pre-deploy command. `railway.worker.json` intentionally has no migration pre-deploy command, avoiding concurrent per-worker migrations. |
| Restart | `ON_FAILURE`, maximum 10 retries in both Railway configs; worker cycle failures also use internal runtime backoff. |

The latest recorded production services are `HawkNeticSportsTools` (web),
`SettlementWorkerProduction`, `SportsResearchProduction`,
`RawRetentionProduction`, `KalshiIngestionProduction`, and `Postgres-gxQB`.
Several recorded revisions were behind `Master` and the recorded database volume
was about 77% full. Do not align deploy triggers, revisions, retention, or volumes
without repeating the readiness and backup/restore gates.

## Staging proposal (not applied)

Create or confirm one Railway staging environment in the existing project, with
a separate PostgreSQL service, separate secrets, and no shared production
volumes. Mirror the production service roles but point each to the reviewed
branch/revision and staging config path. Run the migration pre-deploy once through
the web release, then validate `/healthz`, `/readyz`, each worker heartbeat,
freshness/rejection gates, parity, backup, and restore. Restrict automatic
deployment to reviewed branches and promote the exact tested commit to
production. This remains an owner action; this change does not create or alter a
Railway environment.

## Deploy, logs, and rollback

The intended path is pull request -> GitHub Actions -> reviewed merge -> existing
Railway GitHub deploy trigger. Do not deploy an unreviewed Codespace working tree
with `railway up`, and do not let CI hold a Railway token.

For an authorized audit after authenticating the Railway CLI in the Codespace,
select the existing project/environment/service explicitly before reading logs or
status. Do not rely on a stale linked-service context. A rollback means selecting
the last known-good Railway deployment/revision and verifying migrations remain
forward-compatible; never roll the database backward by deleting migrations or
restoring production without the documented restore gate.

## Network audit decisions

- `127.0.0.1` in `compose.yml`, `scripts/local.sh`, tests, and local health
  examples is valid because the Docker daemon and callers run inside the same
  Codespace. It prevents public database exposure.
- The development dashboard bind is environment-driven and Codespaces sets it to
  `0.0.0.0`; the safe local fallback remains loopback.
- Railway uses its injected `DATABASE_URL` and `PORT`, not Compose hostnames or a
  local connection string.
- `host.docker.internal`, Windows drive paths, and a canonical WSL checkout are
  not part of the cloud workflow. Historical documents may retain past evidence,
  but active instructions must not require them.

## Migration status

Completed by this repository change: repeatable Codespace image, Python 3.12
alignment, cloud tools, environment inventory,
Codespace-aware binding, and CI configuration checks. Remaining owner gates are
repository privacy, Codespaces Secret entry, a real Codespace build/run, and the
read-only Railway/staging/deploy-trigger review. Production service or data
changes are intentionally outside this migration.
