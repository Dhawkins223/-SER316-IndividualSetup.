# Environment variable inventory

This inventory classifies every key in `.env.example`. `LOCAL/CODESPACE`,
`STAGING`, and `PRODUCTION` describe where a key may be used. `SECRET` values
must be supplied through Codespaces Secrets or Railway Variables and must never
be committed, logged, copied between environments, or placed in a devcontainer
image. `NON-SECRET` values may be committed when they are safe defaults.

Each environment gets its own database credentials and source credentials. A
Codespace must use the PostgreSQL instance in `compose.yml`; it must never be
given `DATABASE_URL` or `POSTGRES_PASSWORD` from Railway.

## Secrets

| Variables | Scope | Classification | Notes |
| --- | --- | --- | --- |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | LOCAL/CODESPACE, STAGING, PRODUCTION | SECRET | The path names a private key file mounted or created outside Git. Use distinct source credentials where the provider permits it. |
| `ODDS_API_KEY`, `THE_ODDS_API_KEY`, `SPORTSDATA_API_KEY`, `FIRECRAWL_API_KEY` | LOCAL/CODESPACE, STAGING, PRODUCTION | SECRET | Optional source credentials. A clean Codespace and the test suite do not require them. |
| `POSTGRES_PASSWORD`, `DATABASE_URL` | LOCAL/CODESPACE, STAGING, PRODUCTION | SECRET | Different value in each environment. Codespace values target only its Compose database. Railway injects the hosted URL. |
| `TEST_DATABASE_URL` | LOCAL/CODESPACE, CI | SECRET | Must target the isolated test database, never staging or production. CI creates an ephemeral value. |
| `DASHBOARD_AUTH_PASSWORD` | STAGING, PRODUCTION | SECRET | Hosted dashboard credential; blank is permitted only for non-hosted development. |
| `AUTH_NEW_USER_PASSWORD` | LOCAL/CODESPACE, STAGING, PRODUCTION | SECRET | One-command bootstrap input. Remove it immediately after use. |
| `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`, `SLACK_WEBHOOK_URL` | LOCAL/CODESPACE, STAGING, PRODUCTION | SECRET | Optional integrations; keep disabled when credentials are absent. |

## Environment identity, paths, and database settings

| Variable | Scope | Classification | Purpose |
| --- | --- | --- | --- |
| `APP_ENV` | LOCAL/CODESPACE, CI, STAGING, PRODUCTION | NON-SECRET | Runtime identity and hosted-safety selection. |
| `DASHBOARD_HOST` | LOCAL/CODESPACE | NON-SECRET | Local launcher bind address. Codespaces sets `0.0.0.0`; the default remains loopback. |
| `PORT` | LOCAL/CODESPACE, STAGING, PRODUCTION | NON-SECRET | Dashboard or worker health server port. Railway supplies it when hosted. |
| `RESEARCH_DATA_DIR` | LOCAL/CODESPACE | NON-SECRET | Optional local generated-data directory. |
| `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_TEST_DB`, `POSTGRES_PORT` | LOCAL/CODESPACE, CI | NON-SECRET | Compose and test database names/port. Railway supplies its own connection URL instead. |
| `DATABASE_POOL_MIN_SIZE`, `DATABASE_POOL_MAX_SIZE`, `DATABASE_CONNECT_TIMEOUT`, `DATABASE_STATEMENT_TIMEOUT`, `DATABASE_MIGRATION_STATEMENT_TIMEOUT`, `DATABASE_MIGRATION_MODE` | LOCAL/CODESPACE, CI, STAGING, PRODUCTION | NON-SECRET | PostgreSQL pool, timeout, and forward-only migration controls. |
| `POSTGRES_PARITY_VALIDATED`, `RAILWAY_STAGING_VALIDATED`, `RAILWAY_BACKUP_VERIFIED`, `RAILWAY_VOLUME_HEALTHY` | STAGING, PRODUCTION | NON-SECRET | Explicit readiness evidence flags. They stay false until independently verified. |
| `RAILWAY_ENVIRONMENT`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE_ID`, `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_REPLICA_ID` | STAGING, PRODUCTION | NON-SECRET | Railway-provided or operator-set deployment identity. IDs and the public domain are metadata, not API tokens. |

## Source and collection configuration

All variables in this table are `NON-SECRET` and may be used in
`LOCAL/CODESPACE`, `STAGING`, and `PRODUCTION`. Values in hosted environments
must be reviewed per service because collection volume affects source rate
limits and PostgreSQL storage.

| Variables | Purpose |
| --- | --- |
| `KALSHI_ENV` | Kalshi API environment selector. |
| `FIRECRAWL_BASE_URL`, `FIRECRAWL_MODE` | Optional scraper endpoint and required/optional/disabled policy. |
| `KALSHI_HTTP_CACHE_TTL_SECONDS`, `KALSHI_HTTP_MAX_RETRIES`, `KALSHI_HTTP_BACKOFF_SECONDS`, `KALSHI_HTTP_MIN_INTERVAL_SECONDS`, `KALSHI_HTTP_ALLOW_STALE_ON_ERROR`, `KALSHI_HTTP_MAX_STALE_SECONDS`, `KALSHI_HTTP_MAX_RESPONSE_BYTES`, `KALSHI_HTTP_RETRY_JITTER_SECONDS`, `KALSHI_HTTP_CACHE_MAX_AGE_SECONDS`, `KALSHI_HTTP_CACHE_MAX_BYTES` | HTTP throttling, retry, response, and local cache limits. Stale responses remain ineligible as current evidence. |
| `KALSHI_SOURCE_FRESHNESS_SECONDS`, `KALSHI_PAPER_MAX_PAYLOAD_AGE_SECONDS` | Freshness gates for source and dashboard payloads. |
| `KALSHI_PLAYER_TARGET_TYPES`, `KALSHI_PLAYER_PAGE_SIZE`, `KALSHI_MILESTONE_PAGE_SIZE`, `KALSHI_EVENT_METADATA_LIMIT`, `KALSHI_LIVE_MILESTONE_LIMIT` | Bounded source-catalog coverage controls. |
| `KALSHI_REFERENCE_CADENCE_SECONDS`, `POLYMARKET_COLLECTION_CADENCE_SECONDS` | Always-on source-catalog worker cadence. |
| `POLYMARKET_SPORTS_PAGE_SIZE`, `POLYMARKET_SPORTS_PAGES`, `POLYMARKET_TEAM_PAGE_SIZE`, `POLYMARKET_TEAM_PAGES` | Bounded Polymarket catalog pagination. |
| `SOURCE_REFRESH_MAX_REQUESTS`, `SOURCE_REFRESH_POLL_SECONDS` | Bounded on-demand source refresh behavior. |
| `SETTLEMENT_MAX_MARKETS_PER_RUN`, `SETTLEMENT_HTTP_TIMEOUT_SECONDS`, `SETTLEMENT_MAX_CONSECUTIVE_FETCH_ERRORS` | Settlement worker batch and failure limits. |
| `KALSHI_RUNTIME_CLEANUP_ENABLED` | Enables bounded local runtime-cache cleanup. |
| `SPORTS_SOURCE_MODE`, `SPORTS_SCRAPER_ENABLED`, `SPORTS_RETRIEVAL_PLAN`, `SPORTS_SOURCE_TIMEOUT_SECONDS`, `SPORTS_MAX_SUMMARY_REQUESTS`, `SPORTS_FINALS_LOOKBACK_DAYS` | Sports source selection, request bounds, and finals lookback. |
| `RAW_RETENTION_DAYS`, `RAW_RETENTION_BATCH_LIMIT`, `RAW_RETENTION_DRY_RUN`, `RAW_RETENTION_DUPLICATION_CENSUS` | Raw-payload retention and measurement. Production changes require the readiness gate; the template defaults to dry-run. |
| `EXTERNAL_SOURCES_CONFIG` | Repository-relative configuration for the optional external-source worker. |

## Dashboard, authentication, integrations, and runtime roles

Except where the secret table says otherwise, all variables below are
`NON-SECRET` and may be used in `LOCAL/CODESPACE`, `STAGING`, and `PRODUCTION`.
Hosted auth and integrations must be enabled deliberately, per environment.

| Variables | Purpose |
| --- | --- |
| `DASHBOARD_AUTH_ENABLED`, `DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED`, `DASHBOARD_AUTH_USERNAME`, `DASHBOARD_BASIC_FALLBACK_ENABLED`, `DASHBOARD_BASIC_AUTH_ROLE`, `DASHBOARD_USER_AUTH_ENABLED` | Dashboard authentication posture. The Basic-auth path is the documented emergency fallback. |
| `DASHBOARD_PAYLOAD_SOURCE`, `DASHBOARD_REFRESH_SECONDS`, `DASHBOARD_MAX_SLIP_AGE_SECONDS` | PostgreSQL payload source, refresh cadence, and freshness gate. |
| `AUTH_SESSION_MINUTES`, `AUTH_MAX_FAILED_LOGINS`, `AUTH_LOCK_MINUTES`, `AUTH_REGISTRATION_ENABLED` | Session and account lockout policy. Hosted registration stays disabled unless explicitly staged. |
| `OPERATOR_NAME` | Audit attribution for private operator messages. |
| `GOOGLE_DRIVE_ENABLED`, `GOOGLE_DRIVE_REPORT_FOLDER` | Optional report/archive integration. Provider credentials are owner-managed outside this template. |
| `AIRTABLE_ENABLED`, `AIRTABLE_BOT_RUNS_TABLE`, `AIRTABLE_SOURCE_HEALTH_TABLE`, `AIRTABLE_STAGE_GATES_TABLE`, `AIRTABLE_OPEN_ISSUES_TABLE` | Optional Airtable integration and table names. |
| `SLACK_ALERTS_ENABLED` | Optional Slack alert switch. |
| `RESEARCH_DAEMON_ENABLED`, `RESEARCH_DASHBOARD_PORT` | Legacy local scheduler switch and local dashboard port. Codespaces normally uses the service launcher instead. |
| `CRYPTO_RUN_ID`, `SPORTS_RUN_ID`, `KALSHI_RUN_ID` | Research lineage identifiers. |
| `BOT_COMPANY_ENABLED` | Private bot-company orchestration switch. |
| `HAWKNETIC_SERVICE` | Selects the single web role or one documented always-on worker role. |
| `HAWKNETIC_SERVICE_MODE` | `loop` (default) keeps a worker running with its own cadence; `once` runs a single cycle and exits so the worker can be a scheduled, scale-to-zero Railway cron service. Ignored by the `web` role. An unrecognised value logs and falls back to `loop`. |
| `HAWKNETIC_DATABASE_URL`, `HAWKNETIC_TEST_DATABASE_URL` | LOCAL/CODESPACE only, and `SECRET`. Set both to run `scripts/local.sh` against a managed development database instead of Compose, which removes the local Docker requirement. They must name different databases, and the script refuses a Railway or Render host unless `HAWKNETIC_ALLOW_HOSTED_DATABASE` is set. Never point either at production. |
| `HAWKNETIC_ALLOW_HOSTED_DATABASE` | LOCAL/CODESPACE escape hatch that disables the hosted-host refusal above. Set it only for a database you are certain is disposable. |

## Research-only safety and intentionally disabled connectors

Every key in this table is `NON-SECRET` and required in
`LOCAL/CODESPACE`, `CI`, `STAGING`, and `PRODUCTION`. Hosted values are checked
by readiness. These are safety controls, not features to relax.

| Variables | Required posture |
| --- | --- |
| `KALSHI_ORDER_UPLOAD_ENABLED`, `LIVE_EXECUTION_ENABLED`, `AUTO_UPLOAD_ENABLED`, `AUTO_TRADE_ENABLED`, `MODEL_PROMOTION_ENABLED`, `STALE_CACHE_AS_FRESH` | `false` |
| `RESEARCH_ONLY`, `DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED` | `true` |
| `VERCEL_ENABLED`, `POSTHOG_ENABLED`, `STRIPE_ENABLED` | `false` until each connector is intentionally designed, reviewed, and staged. |

## Codespaces-only owner secrets

The base application, migrations, lint, and tests need no provider secrets.
Add only the integrations being exercised: `KALSHI_API_KEY_ID` plus a private
key file referenced by `KALSHI_PRIVATE_KEY_PATH`, `FIRECRAWL_API_KEY`, the
optional odds-provider key, `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`, and
`SLACK_WEBHOOK_URL`. Railway tokens are tooling credentials and intentionally
do not appear in `.env.example`; if cloud agents need read-only Railway audit
access, add an appropriately scoped `RAILWAY_TOKEN` as a Codespaces Secret.
