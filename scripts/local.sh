#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command_name="${1:-help}"

# Keep discovery usable in a clean checkout. Configuration and Docker are
# runtime prerequisites, not prerequisites for asking what the script does.
if [[ "$command_name" == "help" || "$command_name" == "--help" || "$command_name" == "-h" ]]; then
  cat <<'EOF'
Usage: scripts/local.sh <command>

setup              Install the package and initialize local PostgreSQL.
dev                Initialize PostgreSQL and start the local research dashboard.
stop               Stop only the local PostgreSQL service.
logs               Follow local PostgreSQL logs.
db-start           Start and health-check local PostgreSQL.
db-stop            Stop local PostgreSQL without deleting its volume.
db-status          Show local PostgreSQL service status.
db-reset           Recreate only the local PostgreSQL volume after confirmation.
migrate            Apply versioned migrations to the local application database.
migration-status   Show the current migration status.
test               Run the full test suite against the isolated test database.
test-integration   Run PostgreSQL integration tests.
smoke              Apply migrations and run the application readiness smoke check.
verify             Run non-destructive configuration, migration, test, and smoke checks.
research-status    Show worker, connector, and queued operator-message status.
research-once      Run one research-only cycle for each core worker.

PostgreSQL source (HAWKNETIC_LOCAL_DB):
  auto      Use Compose when Docker is present, otherwise an external server (default).
  compose   Require Docker and run PostgreSQL from compose.yml.
  external  Use a PostgreSQL that is already running. Point it with POSTGRES_HOST,
            POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD (or .env). The script
            creates its two databases but never starts or stops the server.
EOF
  exit 0
fi

case "$command_name" in
  setup|dev|stop|db-stop|logs|db-start|db-status|db-reset|migrate|migration-status|test|test-integration|smoke|verify|research-status|research-once) ;;
  *) echo "Unknown local workflow command: $command_name" >&2; exit 2 ;;
esac

# Docker is one way to get a local PostgreSQL, not the only one. A Codespace
# runs Docker-in-Docker and Compose is the documented default there, but the
# same workflow has to run against a PostgreSQL that already exists -- a
# Codespaces service container, a system package, or a managed development
# database -- so that developing this project never requires a container
# runtime on someone's laptop.
#
#   HAWKNETIC_LOCAL_DB=auto      use Compose when Docker is present (default)
#   HAWKNETIC_LOCAL_DB=compose   require Compose, fail if Docker is missing
#   HAWKNETIC_LOCAL_DB=external  use an already-running PostgreSQL
db_mode="${HAWKNETIC_LOCAL_DB:-auto}"
case "$db_mode" in
  auto)
    # `docker compose version` rather than `command -v docker`: the binary
    # existing proves nothing useful. Docker without the Compose plugin, or with
    # no reachable daemon, would be selected by a which-style check and then
    # fail on the first `docker compose` call -- which is worse than falling
    # back, because the fallback works.
    if docker compose version >/dev/null 2>&1; then db_mode="compose"; else db_mode="external"; fi
    ;;
  compose)
    if ! command -v docker >/dev/null 2>&1; then
      echo "HAWKNETIC_LOCAL_DB=compose needs Docker. Install Docker, run this in the repository Codespace, or set HAWKNETIC_LOCAL_DB=external to use a PostgreSQL that is already running." >&2
      exit 127
    fi
    ;;
  external) ;;
  *)
    echo "Unknown HAWKNETIC_LOCAL_DB mode: $db_mode (expected auto, compose, or external)" >&2
    exit 2
    ;;
esac

local_env_value() {
  local key="$1"
  local fallback="$2"
  local value=""
  if [[ -f "$repo_root/.env" ]]; then
    value="$(grep -m 1 -E "^${key}=" "$repo_root/.env" | cut -d '=' -f2- || true)"
    value="${value%$'\r'}"
    if [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ ${#value} -ge 2 && "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "${value:-$fallback}"
}
compose=(docker compose -f "$repo_root/compose.yml" --project-name hawknetic-local)
postgres_user="${POSTGRES_USER:-$(local_env_value POSTGRES_USER hawknetic)}"
postgres_password="${POSTGRES_PASSWORD:-$(local_env_value POSTGRES_PASSWORD '')}"
postgres_port="${POSTGRES_PORT:-$(local_env_value POSTGRES_PORT 54329)}"
app_database="${POSTGRES_DB:-$(local_env_value POSTGRES_DB hawknetic)}"
test_database="${POSTGRES_TEST_DB:-$(local_env_value POSTGRES_TEST_DB hawknetic_test)}"
dashboard_host="${DASHBOARD_HOST:-$(local_env_value DASHBOARD_HOST 127.0.0.1)}"
dashboard_port="${PORT:-$(local_env_value PORT 8765)}"
postgres_host="${POSTGRES_HOST:-$(local_env_value POSTGRES_HOST 127.0.0.1)}"
# Compose always provisions a password-authenticated server, so an empty
# password there is a misconfiguration worth catching early. An external server
# may legitimately use trust or peer authentication, and demanding a password it
# does not want is how a workflow ends up requiring Docker again.
if [[ -z "$postgres_password" && "$db_mode" == "compose" ]]; then
  echo "POSTGRES_PASSWORD must be set in the untracked .env file." >&2
  exit 2
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x "$repo_root/.venv/bin/python" ]]; then
  python_bin="$repo_root/.venv/bin/python"
else
  python_bin="python3"
fi

# A password is not a URL component until it is escaped. `p@ss/word` splices a
# new host and path into the connection string and either fails to parse or,
# worse, parses as something else entirely. Byte-wise under LC_ALL=C so that
# multi-byte characters encode per byte, which is what percent-encoding means.
uri_encode() {
  local raw="$1" out="" index char
  local LC_ALL=C
  for (( index = 0; index < ${#raw}; index++ )); do
    char="${raw:index:1}"
    case "$char" in
      [A-Za-z0-9._~-]) out+="$char" ;;
      *) printf -v char '%%%02X' "'$char"; out+="$char" ;;
    esac
  done
  printf '%s' "$out"
}

database_url() {
  local database_name="$1"
  local user_part
  user_part="$(uri_encode "$postgres_user")"
  if [[ -n "$postgres_password" ]]; then
    user_part="${user_part}:$(uri_encode "$postgres_password")"
  fi
  printf 'postgresql://%s@%s:%s/%s' \
    "$user_part" "$postgres_host" "$postgres_port" "$(uri_encode "$database_name")"
}

run_app() {
  local database_name="$1"
  shift
  PYTHONPATH="$repo_root/src" \
  DATABASE_URL="$(database_url "$database_name")" \
  TEST_DATABASE_URL="$(database_url "$test_database")" \
  DATABASE_MIGRATION_MODE=apply \
  APP_ENV="${APP_ENV:-local}" \
  "$@"
}

# Ask over the wire rather than through the container, so the same check works
# for a Compose server and an external one. psycopg is already a dependency of
# the package, which keeps this from requiring psql on the host.
ensure_databases_ready() {
  local attempts="$1"
  DATABASE_BOOTSTRAP_URL="$(database_url postgres)" \
  DATABASE_BOOTSTRAP_NAMES="$app_database,$test_database" \
  DATABASE_BOOTSTRAP_ATTEMPTS="$attempts" \
  "$python_bin" - <<'PY'
import os
import sys
import time

import psycopg
from psycopg import sql

url = os.environ["DATABASE_BOOTSTRAP_URL"]
names = [name for name in os.environ["DATABASE_BOOTSTRAP_NAMES"].split(",") if name]
attempts = max(1, int(os.environ["DATABASE_BOOTSTRAP_ATTEMPTS"]))

last_error = None
for _ in range(attempts):
    try:
        with psycopg.connect(url, connect_timeout=5, autocommit=True) as connection:
            for name in names:
                exists = connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
                ).fetchone()
                if not exists:
                    # Identifier(), not an f-string: a database name is
                    # configuration, and configuration is not trusted syntax.
                    connection.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                    )
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - report whatever kept us out
        last_error = exc
        time.sleep(2)

print(f"Local PostgreSQL did not become reachable: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

wait_for_database() {
  if [[ "$db_mode" == "compose" ]]; then
    ensure_databases_ready 30
  else
    # An external server is not ours to start, so a short wait is a health
    # check rather than a boot delay, and its failure should say so.
    if ! ensure_databases_ready 3; then
      echo "Set POSTGRES_HOST/POSTGRES_PORT/POSTGRES_USER/POSTGRES_PASSWORD (or .env) to a PostgreSQL that is already running, or use HAWKNETIC_LOCAL_DB=compose to have Docker provide one." >&2
      return 1
    fi
  fi
}

db_start() {
  if [[ "$db_mode" == "compose" ]]; then
    "${compose[@]}" up -d postgres
  fi
  wait_for_database
}

migrate() {
  db_start
  run_app "$app_database" "$python_bin" -m kalshi_research_bot.db_command migrate
}

migration_status() {
  db_start
  run_app "$app_database" "$python_bin" -m kalshi_research_bot.db_command status
}

test_database_migrate() {
  db_start
  run_app "$test_database" "$python_bin" -m kalshi_research_bot.db_command migrate
}

research_status() {
  db_start
  run_app "$app_database" "$python_bin" -m kalshi_research_bot worker-status
  run_app "$app_database" "$python_bin" -m kalshi_research_bot connectors-status
  run_app "$app_database" "$python_bin" -m kalshi_research_bot \
    operator-message-list --status queued --limit 20
}

research_once() {
  local service=""
  local failures=()
  db_start
  for service in \
    kalshi-market-ingestion \
    crypto-research \
    sports-research \
    settlement-worker \
    reporting-evaluation; do
    if ! run_app "$app_database" "$python_bin" -m kalshi_research_bot \
      worker --service "$service" --once; then
      failures+=("$service")
    fi
  done
  research_status
  if [[ "${#failures[@]}" -gt 0 ]]; then
    echo "Research routine completed with blocked/failed services: ${failures[*]}" >&2
    return 1
  fi
}

case "$command_name" in
  setup)
    "$python_bin" -m pip install -e "$repo_root"
    migrate
    test_database_migrate
    ;;
  dev)
    migrate
    run_app "$app_database" "$python_bin" -m kalshi_research_bot paper \
      --host "$dashboard_host" --port "$dashboard_port"
    ;;
  stop|db-stop)
    if [[ "$db_mode" == "compose" ]]; then
      "${compose[@]}" stop postgres
    else
      echo "PostgreSQL is externally managed (HAWKNETIC_LOCAL_DB=$db_mode); this workflow did not start it and will not stop it."
    fi
    ;;
  logs)
    if [[ "$db_mode" == "compose" ]]; then
      "${compose[@]}" logs -f postgres
    else
      echo "PostgreSQL is externally managed (HAWKNETIC_LOCAL_DB=$db_mode); read its logs where it runs." >&2
      exit 2
    fi
    ;;
  db-start)
    db_start
    ;;
  db-status)
    if [[ "$db_mode" == "compose" ]]; then
      "${compose[@]}" ps
    else
      db_start
      run_app "$app_database" "$python_bin" -m kalshi_research_bot.db_command status
    fi
    ;;
  db-reset)
    # Dropping two databases is the external equivalent of deleting the Compose
    # volume: it discards exactly this project's local data and nothing else on
    # a server that may be hosting more than this project.
    read -r -p "Delete only the local PostgreSQL data? Type RESET to continue: " confirmation
    [[ "$confirmation" == "RESET" ]] || { echo "Local database reset cancelled."; exit 1; }
    if [[ "$db_mode" == "compose" ]]; then
      "${compose[@]}" down -v
    else
      # Dropping databases on a server this script did not start is a much
      # larger blast radius than deleting a Compose volume, and POSTGRES_HOST
      # is one typo away from a server that matters. Loopback is the only
      # target that is self-evidently a development database; anything else
      # has to be claimed explicitly.
      case "$postgres_host" in
        127.0.0.1|localhost|::1|"") ;;
        *)
          if [[ "${HAWKNETIC_ALLOW_EXTERNAL_RESET:-}" != "1" ]]; then
            echo "Refusing to drop databases on non-local host '$postgres_host'." >&2
            echo "This would DROP \"$app_database\" and \"$test_database\" there. If that is really a development server, re-run with HAWKNETIC_ALLOW_EXTERNAL_RESET=1." >&2
            exit 2
          fi
          ;;
      esac
      DATABASE_RESET_URL="$(database_url postgres)" \
      DATABASE_RESET_NAMES="$app_database,$test_database" \
      "$python_bin" - <<'PY'
import os

import psycopg
from psycopg import sql

url = os.environ["DATABASE_RESET_URL"]
names = [name for name in os.environ["DATABASE_RESET_NAMES"].split(",") if name]
with psycopg.connect(url, connect_timeout=5, autocommit=True) as connection:
    for name in names:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
PY
    fi
    db_start
    ;;
  migrate)
    migrate
    ;;
  migration-status)
    migration_status
    ;;
  test)
    test_database_migrate
    run_app "$test_database" "$python_bin" -m unittest discover -s "$repo_root/tests"
    ;;
  test-integration)
    test_database_migrate
    run_app "$test_database" "$python_bin" -m unittest discover -s "$repo_root/tests" -p 'test_postgres_*.py'
    ;;
  smoke)
    migrate
    run_app "$app_database" "$python_bin" -m kalshi_research_bot.db_command status
    ;;
  verify)
    if [[ "$db_mode" == "compose" ]]; then
      "${compose[@]}" config >/dev/null
    fi
    migrate
    test_database_migrate
    run_app "$test_database" "$python_bin" -m unittest discover -s "$repo_root/tests"
    run_app "$app_database" "$python_bin" -m kalshi_research_bot.db_command status
    ;;
  research-status)
    research_status
    ;;
  research-once)
    research_once
    ;;
esac
