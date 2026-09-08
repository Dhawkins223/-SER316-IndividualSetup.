# HawkNeticSportsTools

Private, research-only decision support for Kalshi, crypto, and sports workflows. It never places orders, uploads slips, enables automatic trading, or promotes models.

## Cloud development

GitHub Codespaces is the canonical development environment and GitHub is the
version-control source of truth. A Windows checkout, WSL, Docker Desktop, and a
machine-local Python installation are not required.

Open the repository's **Code -> Codespaces** menu and create a Codespace on a
feature branch. The devcontainer supplies Python 3.12, an isolated
Docker-in-Docker daemon, Docker Compose, GitHub and Railway CLIs, PostgreSQL
client tools, and the required gstack install. Its setup hook installs the
package, starts the disposable PostgreSQL 18 Compose service, applies migrations,
and prepares an isolated test database.

PostgreSQL is the only supported database engine. Codespace credentials remain
in the untracked `.env`; staging and production credentials remain separate in
Railway Variables. A Codespace must never receive the Railway production
`DATABASE_URL`.

```bash
./scripts/local.sh dev
```

Open the private forwarded dashboard port `8765` from the Codespaces Ports panel.

Useful commands:

```bash
./scripts/local.sh db-start
./scripts/local.sh db-status
./scripts/local.sh migration-status
./scripts/local.sh test
./scripts/local.sh test-integration
./scripts/local.sh smoke
./scripts/local.sh verify
./scripts/local.sh stop
```

`db-reset` destroys only the Codespace Compose volume and requires the explicit
`RESET` confirmation. It never contacts Railway.

### Developing without Docker

Compose is one backend, not a prerequisite. To work with no Docker daemon at all
— a managed development database such as two Neon branches, or a PostgreSQL
installed directly on the machine — set both URLs and Compose is never invoked:

```bash
export HAWKNETIC_DATABASE_URL='postgresql://.../dev'
export HAWKNETIC_TEST_DATABASE_URL='postgresql://.../dev_test'
./scripts/local.sh test
```

Both are required and must name different databases: `test` writes to the test
database, so sharing one would destroy development data. A Railway or Render host
is refused unless `HAWKNETIC_ALLOW_HOSTED_DATABASE` is set. Never point either at
production.

`compose.yml` is retained as the offline fallback and is what CI uses.

See [Cloud development](docs/cloud-development.md) for setup, Secrets, ports,
tests, database commands, the Railway configuration audit, staging proposal,
logs, rollback, and the complete Windows/Docker Desktop retirement boundary.

## Infrastructure

| Document | Contents |
| --- | --- |
| [Current infrastructure](docs/CURRENT_INFRASTRUCTURE.md) | What runs today, measured footprints, security posture, and what cannot be settled without account access |
| [Target infrastructure](docs/TARGET_INFRASTRUCTURE.md) | The provider decision for Railway, Cloudflare, Neon, and Render, with the evidence for each |
| [Infrastructure costs](docs/INFRASTRUCTURE_COSTS.md) | Rates, measured consumption, savings, and what would make costs rise |
| [Deployment](docs/DEPLOYMENT.md) | How a reviewed commit reaches production, and per-service configuration |
| [Rollback](docs/ROLLBACK.md) | Recovering a deploy, a migration, or a worker |

In short: Railway stays as the application platform and the production database;
five hourly-or-slower workers become scheduled scale-to-zero services rather than
containers that sleep; Neon covers development and CI databases only; Cloudflare
provides DNS, TLS, and WAF in front of Railway; Render is not used. The database
does **not** move to Neon — at this workload's cadence Neon's scale-to-zero cannot
engage, which makes it roughly $17/month more expensive, not less.

`scripts/railway_inventory.sh` reports the deployed services read-only, printing
variable names without values.

## Database contract

All application state uses PostgreSQL and versioned migrations in `migrations/postgres/`.

- `app`: active research, prediction, simulation, and dashboard writes.
- `raw`: immutable collection batches, payload evidence, and rejection records.
- `core`: source market identity and observations.
- `research`: model and feature lineage.
- `ops`: worker state, source health, quality results, and private operator messages.
- `reporting`: read-only reporting views.
- `auth`: users, sessions, and login audits.

`raw.source_payloads` grows by one response body per collection cycle and needs a retention window. `raw-retention` ages out payload bodies past that window while preserving the row, its batch lineage, its timestamps, and its `content_hash`; it defaults to a dry run and never touches a source's newest payload. See `docs/raw-payload-retention.md`.

Runtime connections use the deterministic search path `app, pg_catalog`; cross-domain statements use explicitly qualified schema names. Exact financial and probability values remain `NUMERIC` until an API or UI serialization boundary, where fixed-point decimal strings preserve their scale without binary-float loss.

`DATABASE_STATEMENT_TIMEOUT` limits ordinary runtime queries. Migrations use the
separate `DATABASE_MIGRATION_STATEMENT_TIMEOUT` setting (300000 ms by default)
so a production-sized reviewed migration is not constrained by the shorter
request-path timeout.

## Safety controls

The application starts in research-only mode. Keep these values in `.env` and hosted variables:

```text
RESEARCH_ONLY=true
LIVE_EXECUTION_ENABLED=false
AUTO_TRADE_ENABLED=false
AUTO_UPLOAD_ENABLED=false
KALSHI_ORDER_UPLOAD_ENABLED=false
MODEL_PROMOTION_ENABLED=false
STALE_CACHE_AS_FRESH=false
DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED=true
```

Freshness, source evidence, rejection, unresolved-state, and duplicate-exposure gates remain enforced. A blocked sports source does not fabricate rows or affect Kalshi or crypto metrics.

Probability evaluation and the research-only `BET_CANDIDATE` / `NO_BET` / `WAIT_FOR_DATA` contract are documented in `docs/probability-and-decision-policy.md`. Market-implied probability alone is always a baseline and cannot create a research candidate.

Bookmaker margin removal (`math/devig.py`) supports multiplicative, additive, power, Shin, and odds-ratio methods; the sports board defaults to Shin and publishes both the method used and the disagreement between methods, because on skewed markets that disagreement can exceed the minimum edge the decision gate requires. Probability calibration (`evaluation/calibration.py`) selects among Platt, beta, and isotonic calibrators by out-of-fold log loss and leaves an already-calibrated model alone. Research hypotheses and their results — including rejections — are recorded in the hash-chained registry in `research_registry.py`. Statistical power (`evaluation/power.py`) states how many resolved predictions a claim needs and the smallest effect a given sample could detect, and `research_registry.significance_review()` applies Benjamini-Hochberg across every recorded experiment so a finding selected from a large backlog is demoted rather than cited.

These commands make this reachable from the terminal:

```bash
python -m kalshi_research_bot devig-compare --american -900 600
python -m kalshi_research_bot research-power --edge 0.01 --sample-size 300
python -m kalshi_research_bot research-registry --negative-results
python -m kalshi_research_bot venue-compare
python -m kalshi_research_bot sports-ratings --league nba
python -m kalshi_research_bot sports-ratings --historical --record
python -m kalshi_research_bot market-blend --historical --record
python -m kalshi_research_bot power-audit --historical --pooled
```

`devig-compare` shows what every margin-removal method makes of one market and how far apart they are. `research-power` answers how many resolved predictions a claim needs, and the smallest edge a given sample could have detected. `research-registry` summarizes recorded experiments, verifies the hash chain, and lists accepted findings demoted by family-wise correction.

`venue-compare` pairs the sportsbook board against Polymarket on the games both venues price, and reports where they disagree. A market is compared only when the start times agree and both teams correspond, derivative and three-way-collapsed markets are excluded by name, and a gap is flagged only when it exceeds the estimated execution cost plus the board's de-vig method disagreement. Gaps are observations of two posted prices, never a validated edge.

`sports-ratings` (`sports_ratings.py`) is the first model in the platform rather than another reading of the market. It reconstructs finished games from the settled rows the collector already wrote, walks Elo forward in start-time order so no game can inform its own forecast, and grades the result by paired Brier difference against two baselines: the home base rate and the de-vigged closing consensus across books. It states a verdict — including `inconclusive` and `rejected` — with a confidence interval and how many games the observed effect would need, and `--record` appends that verdict to the research registry. The report stays `track_only`: nothing here promotes a model, and an interval containing zero is not a result.

`--historical` grades the same rating against `nflverse/nfldata`, a public archive of every NFL game since 1999 with closing moneylines from 2006. Live collection produces a few hundred graded games a year and the paired tests this program specifies need thousands, so the archive is what makes the question answerable now. Those rows are a third party's record, not collected evidence: they never enter the collection tables, and the report carries the file's content hash so a verdict stays attached to the data that produced it.

Run against 7,159 NFL games it returns two answers. Elo beats the home base rate by 0.0171 paired Brier, CI [0.0137, 0.0206] — real signal. Elo *loses* to the de-vigged closing line by 0.0183 paired Brier, CI [-0.0219, -0.0148], on 5,266 games — the market is the better forecast, by a margin no sample size will overturn. Section O of `docs/sports-prediction-research-program.md` records both.

`market-blend` (`sports_market_model.py`) asks the question that follows from that: not model versus market, but whether the model adds anything to the price it starts from. It fits `logit(p) = a + b·logit(market) + c·(logit(elo) − logit(market))` walk-forward, each season's coefficients estimated only from seasons that had already finished. Over 4,780 games and 18 refits the blend scores −0.0001 paired Brier against the closing price alone, CI [−0.00060, +0.00039] — an interval tight enough to exclude any improvement above 0.0006 — while the fitted weight on the market term converges to 1.04 and the weight on the model term decays to 0.076.

Two adequately powered experiments therefore point the same way: on NFL moneylines a team-strength rating adds nothing detectable to the closing line. What this platform can honestly offer is the price-comparison work — line shopping, the de-vigged consensus, closing line value, and the freshness and rejection discipline behind them — none of which requires beating the market to be useful.

`power-audit` (`sports_power_audit.py`) asks what that evidence could ever have detected, which is backlog entry E-09 and the question that decides whether the rest of the program is answerable. It reads the spread of the paired difference off the comparison above rather than assuming one, and counts gradable games from the archive: 284.8 per NFL season over 2021–2025, all with closing moneylines. At the realized 4,780 games — already 16.8 seasons — the smallest detectable paired Brier improvement is 0.000703, and the observed −0.000104 is fifteen percent of it. Showing an effect that size would take about 774 NFL seasons.

That reframes what to build next. The independent unit is the game, not the quote: a market resolves once however many books priced it, so five books on an NFL season give 1,424 prices and 285 outcomes, and citing stored quote counts as evidence inflates them fivefold. Pooling NFL, NBA, NHL and MLB raises gradable games from 285 to about 5,257 per season, which brings a 1% edge from 68.7 seasons within reach at 3.7. For the purpose of ever demonstrating a claim, league coverage buys roughly eighteen times more than any modelling change can. Section P of the research program records the verdict, and it is `rejected` for NFL-only volume.

The findings, evidence grading, red-team review, and open experiments behind these choices are in `docs/sports-prediction-research-program.md` and `docs/research-backlog.md`.

Password-only hosted deployments retain the emergency single-owner Basic-auth
path when `DASHBOARD_BASIC_FALLBACK_ENABLED=true`. That path resolves to
`read_only` unless `DASHBOARD_BASIC_AUTH_ROLE` names a role from
`read_only`, `researcher`, `admin` — an unset or unrecognised value takes the
floor, so operator access is something a deployment asks for rather than
something it gets by omission. Set `DASHBOARD_BASIC_AUTH_ROLE=admin` to keep the
refresh control, the operations panels, and the operator-only endpoints.

Disable the fallback only after PostgreSQL user accounts and session
authentication have been staged and verified.

## Hosted workflow

Hosted staging and production are separate from local development and must use distinct PostgreSQL services, credentials, and data. A production cutover requires successful migration, parity, backup, restore, readiness, and research-only safety checks. See:

- The web service runs with `HAWKNETIC_SERVICE=web` and reads the latest completed Kalshi snapshot from PostgreSQL.
- The Kalshi collector runs independently with `HAWKNETIC_SERVICE=kalshi-market-ingestion` and writes immutable raw evidence plus the normalized market state.
- The web service never treats its generated JSON file as the hosted source of truth and never displays a stale PostgreSQL snapshot as fresh.
- Authenticated clients can inspect bounded current detail through `/api/v1`, `/api/v1/games`, and `/api/v1/markets`. Collection routes accept `limit` and `offset`, cap pages at 200 rows, and withhold all rows when the public freshness gate is blocked. `/games.json` and `/markets.json` remain compatibility aliases.
- The sports board (`/sports.json` and the dashboard's sports panel) reads the rows the `sports-research` worker uploads. It reports `fresh`, `stale`, `blocked`, `empty`, or `unavailable` explicitly and withholds rows in every state except `fresh`. Each market publishes both the shopper's de-vig of the best available prices and the books' own consensus — each book de-vigged on its own, then the median — plus the signed gap between them. See `docs/sports-data-upload.md`.
- Closing line value (`/sports-clv.json`, `sports-clv`) grades each recorded price against the last pre-start quote posted by the same bookmaker for the same market. It is a price comparison in probability points, not profit and not a settled result.
- Other worker roles use the names documented by `python -m kalshi_research_bot worker --help`; they remain isolated from the web process.
- `HAWKNETIC_SERVICE_MODE` selects how a worker runs. `loop` (the default) keeps the process resident and uses the worker's own cadence. `once` runs a single cycle and exits, so an hourly or slower worker can be a scheduled scale-to-zero service instead of a container that spends almost all of its life asleep. Switching is one variable and involves no data or schema change; an overlapping cron run and loop worker record `skipped_duplicate` rather than collecting twice. See `docs/DEPLOYMENT.md`.

- `docs/data-sources.md`
- `docs/sports-data-upload.md`
- `docs/staging-sports-worker-verification.md`
- `docs/raw-payload-retention.md`
- `docs/operator-runbook.md`
- `docs/database-schema-audit.md`
- `docs/data-cutover-validation.md`
- `docs/postgresql-parity-validation.md`
- `docs/railway-postgresql-deployment-and-rollback.md`
- `docs/deployment-readiness-checklist.md`

Do not use the deployment environment as proof of model validity, edge, or profitability.
