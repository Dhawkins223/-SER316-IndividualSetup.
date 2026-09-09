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

Database backends
-----------------
By default this script runs PostgreSQL through Docker Compose. To develop with
no Docker daemon at all -- a managed development database such as a Neon branch,
or a PostgreSQL installed directly on the machine -- export both:

  HAWKNETIC_DATABASE_URL       development database
  HAWKNETIC_TEST_DATABASE_URL  isolated test database (destroyed by `test`)

Compose is then never invoked and Docker is not required. The db-start, db-stop,
db-status, db-reset, and logs commands manage the Compose service only, and
report that there is nothing to manage in external mode.
EOF
  exit 0
fi

case "$command_name" in
  setup|dev|stop|db-stop|logs|db-start|db-status|db-reset|migrate|migration-status|test|test-integration|smoke|verify|research-status|research-once) ;;
  *) echo "Unknown local workflow command: $command_name" >&2; exit 2 ;;
esac

# `tests/test_local_workflow.py` runs this script as a subprocess, and it
# inherits the caller's environment. Before external database mode existed the
# child always stopped at the Docker guard; with a database configured it would
# instead run the suite again, and that child would run it again. The result is
# a fork bomb, not a test failure. Refuse to re-enter rather than trusting every
# caller to scrub the environment.
#
# Only the three commands that invoke `unittest discover` are listed, because
# only those load the test module that re-enters this script. `research-once`
# runs worker cycles and never starts the suite, so guarding it would refuse a
# legitimate nested call for no protective benefit.
case "$command_name" in
  test|test-integration|verify)
    if [[ -n "${HAWKNETIC_LOCAL_SH_ACTIVE:-}" ]]; then
      echo "Refusing to run '$command_name' from inside scripts/local.sh: this would recurse." >&2
      exit 3
    fi
    ;;
esac
export HAWKNETIC_LOCAL_SH_ACTIVE=1

# Docker is a backend, not a prerequisite of the workflow. When an external
# development database is configured the Compose service is never started, so
# requiring a daemon here would block the very setup that removes the daemon.
external_database_url="${HAWKNETIC_DATABASE_URL:-}"
external_test_database_url="${HAWKNETIC_TEST_DATABASE_URL:-}"
if [[ -n "$external_database_url" || -n "$external_test_database_url" ]]; then
  database_mode="external"
else
  database_mode="compose"
fi

if [[ "$database_mode" == "external" ]]; then
  if [[ -z "$external_database_url" || -z "$external_test_database_url" ]]; then
    echo "External database mode needs both HAWKNETIC_DATABASE_URL and HAWKNETIC_TEST_DATABASE_URL." >&2
    echo "The test suite writes to the test database; pointing it at the development database would destroy it." >&2
    exit 2
  fi
  if [[ "$external_database_url" == "$external_test_database_url" ]]; then
    echo "HAWKNETIC_DATABASE_URL and HAWKNETIC_TEST_DATABASE_URL must name different databases." >&2
    exit 2
  fi
  # CLAUDE.md: never point a Codespace or a test process at Railway production.
  # A typo that pastes a hosted connection string here would otherwise run the
  # destructive test suite against it.
  if [[ -z "${HAWKNETIC_ALLOW_HOSTED_DATABASE:-}" ]]; then
    for candidate in "$external_database_url" "$external_test_database_url"; do
      case "$candidate" in
        *railway.internal*|*rlwy.net*|*.railway.app*|*render.com*)
          echo "Refusing to use a hosted Railway/Render database for local development or tests." >&2
          echo "Set HAWKNETIC_ALLOW_HOSTED_DATABASE=1 only for a database you are certain is disposable." >&2
          exit 2
          ;;
      esac
    done
  fi
elif ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the Compose-backed local PostgreSQL service." >&2
  echo "Either start a Docker daemon, or set HAWKNETIC_DATABASE_URL and HAWKNETIC_TEST_DATABASE_URL" >&2
  echo "to use a managed development database instead. See 'scripts/local.sh help'." >&2
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
if [[ -z "$postgres_password" && "$database_mode" == "compose" ]]; then
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
  if [[ "$database_mode" == "external" ]]; then
    if [[ "$database_name" == "$test_database" ]]; then
      printf '%s' "$external_test_database_url"
    else
      printf '%s' "$external_database_url"
    fi
    return 0
  fi
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

external_database_ready() {
  # No daemon and no Compose service to health-check: the only question worth
  # asking a managed database is whether it answers. A Neon branch that is
  # scaled to zero wakes up on this connection, so a slow first attempt is
  # expected rather than a failure.
  #
  # This also settles identity, which the string comparison above cannot. Two
  # URLs that differ only in credentials or connection options -- a different
  # user, an added sslmode -- name the same database, and `test` would then
  # migrate and truncate the development one. Ask each server what it actually
  # is and refuse when both answers match.
  local attempts=15
  until HAWKNETIC_APP_URL="$(database_url "$app_database")" \
        HAWKNETIC_TEST_URL="$(database_url "$test_database")" \
        "$python_bin" <<'PY'
import os
import sys

import psycopg


def identity(url):
    """The database's name, and the cluster it lives in.

    Addresses are not identity. The same database reached over a unix socket
    and over TCP reports inet_server_addr() as NULL and as 127.0.0.1, so
    comparing addresses would call one database two, and the destructive test
    suite would then run against the development database -- exactly the
    accident this check exists to stop. The cluster's system_identifier is
    stable across both.
    """

    with psycopg.connect(url, connect_timeout=10) as connection:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        try:
            cluster = connection.execute(
                "SELECT system_identifier FROM pg_control_system()"
            ).fetchone()[0]
        except psycopg.Error:
            # Restricted to superusers unless granted, so a managed provider
            # may refuse it. Absence is handled conservatively below.
            cluster = None
        return name, cluster


app_name, app_cluster = identity(os.environ["HAWKNETIC_APP_URL"])
test_name, test_cluster = identity(os.environ["HAWKNETIC_TEST_URL"])

if app_name != test_name:
    # Different names cannot be one database, wherever they live.
    raise SystemExit(0)

if app_cluster is not None and test_cluster is not None and app_cluster != test_cluster:
    # Same name, provably different clusters: two databases that happen to
    # share a name. Allowed.
    raise SystemExit(0)

if app_cluster is None or test_cluster is None:
    print(
        f"Both URLs name a database called {app_name!r}, and this server would not "
        "report its cluster identity, so they cannot be proven distinct. Give the "
        "development and test databases different names.",
        file=sys.stderr,
    )
    raise SystemExit(3)

print(
    "HAWKNETIC_DATABASE_URL and HAWKNETIC_TEST_DATABASE_URL resolve to the same "
    f"database ({app_name!r} in cluster {app_cluster}). The test suite would "
    "destroy your development data.",
    file=sys.stderr,
)
raise SystemExit(3)
PY
  do
    # An identity clash is a configuration error, not a database still waking
    # up. Retrying it fifteen times would only delay the message.
    if [[ $? -eq 3 ]]; then
      return 1
    fi
    attempts=$((attempts - 1))
    if [[ "$attempts" -le 0 ]]; then
      echo "External PostgreSQL did not answer. Check HAWKNETIC_DATABASE_URL and HAWKNETIC_TEST_DATABASE_URL." >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_database() {
  if [[ "$database_mode" == "external" ]]; then
    external_database_ready
    return $?
  fi
  local attempts=30
  until "${compose[@]}" exec -T postgres pg_isready -U "$postgres_user" -d "$app_database" >/dev/null; do
    attempts=$((attempts - 1))
    if [[ "$attempts" -le 0 ]]; then
      echo "Local PostgreSQL did not become healthy." >&2
      return 1
    fi
  fi
}

db_start() {
  if [[ "$database_mode" == "external" ]]; then
    wait_for_database
    return $?
  fi
  "${compose[@]}" up -d postgres
  wait_for_database
}

compose_only() {
  if [[ "$database_mode" == "external" ]]; then
    echo "Nothing to $1: this workflow is using an external database, not the Compose service."
    exit 0
  fi
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
    compose_only "stop"
    "${compose[@]}" stop postgres
    ;;
  logs)
    compose_only "show logs for"
    "${compose[@]}" logs -f postgres
    ;;
  db-start)
    db_start
    ;;
  db-status)
    compose_only "report status for"
    "${compose[@]}" ps
    ;;
  db-reset)
    compose_only "reset"
    read -r -p "Delete only the local PostgreSQL volume? Type RESET to continue: " confirmation
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
    if [[ "$database_mode" == "compose" ]]; then
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
