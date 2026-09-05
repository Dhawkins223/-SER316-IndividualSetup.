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
EOF
  exit 0
fi

case "$command_name" in
  setup|dev|stop|db-stop|logs|db-start|db-status|db-reset|migrate|migration-status|test|test-integration|smoke|verify|research-status|research-once) ;;
  *) echo "Unknown local workflow command: $command_name" >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the isolated local PostgreSQL service; run this workflow in the repository Codespace." >&2
  exit 127
fi

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
if [[ -z "$postgres_password" ]]; then
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

database_url() {
  local database_name="$1"
  printf 'postgresql://%s:%s@127.0.0.1:%s/%s' \
    "$postgres_user" "$postgres_password" "$postgres_port" "$database_name"
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

wait_for_database() {
  local attempts=30
  until "${compose[@]}" exec -T postgres pg_isready -U "$postgres_user" -d "$app_database" >/dev/null; do
    attempts=$((attempts - 1))
    if [[ "$attempts" -le 0 ]]; then
      echo "Local PostgreSQL did not become healthy." >&2
      return 1
    fi
    sleep 2
  done
  if ! "${compose[@]}" exec -T postgres psql -U "$postgres_user" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname = '$test_database'" | grep -q 1; then
    "${compose[@]}" exec -T postgres psql -U "$postgres_user" -d postgres -c \
      "CREATE DATABASE \"$test_database\"" >/dev/null
  fi
}

db_start() {
  "${compose[@]}" up -d postgres
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
    "${compose[@]}" stop postgres
    ;;
  logs)
    "${compose[@]}" logs -f postgres
    ;;
  db-start)
    db_start
    ;;
  db-status)
    "${compose[@]}" ps
    ;;
  db-reset)
    read -r -p "Delete only the local PostgreSQL volume? Type RESET to continue: " confirmation
    [[ "$confirmation" == "RESET" ]] || { echo "Local database reset cancelled."; exit 1; }
    "${compose[@]}" down -v
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
    "${compose[@]}" config >/dev/null
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
