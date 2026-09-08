#!/usr/bin/env bash
#
# Read-only Railway inventory.
#
# Two questions this repository cannot answer from its own contents:
#
#   1. Which worker services are actually deployed. `docs/railway-worker-services.md`
#      and `docs/sports-data-upload.md` record different answers and neither is a
#      deployment record.
#   2. What the project actually costs, and which resource drives it.
#
# Both are answerable only from the account. This script asks, and writes a
# report you can paste into an issue or hand back to an agent. It never mutates
# anything: no deploy, no delete, no variable write, no restart.
#
# Variable VALUES are never printed -- only names. A report containing a
# connection string is a leaked credential, and reports get pasted into chat.
#
# Usage:
#   scripts/railway_inventory.sh                 # writes to stdout
#   scripts/railway_inventory.sh report.md       # writes to a file
#
set -euo pipefail

output_path="${1:-}"

if ! command -v railway >/dev/null 2>&1; then
  cat >&2 <<'EOF'
The Railway CLI is not installed.

  npm install -g @railway/cli@4

Then authenticate with `railway login` (browser) and re-run this script from
the linked project directory (`railway link`).
EOF
  exit 127
fi

if ! railway whoami >/dev/null 2>&1; then
  echo "Railway CLI is installed but not authenticated. Run: railway login" >&2
  exit 1
fi

emit() {
  if [[ -n "$output_path" ]]; then
    cat >>"$output_path"
  else
    cat
  fi
}

if [[ -n "$output_path" ]]; then
  : >"$output_path"
fi

# `railway status --json` fails when the working directory is not linked to a
# project, which is a common and recoverable mistake rather than a crash.
if ! status_json="$(railway status --json 2>/dev/null)"; then
  echo "This directory is not linked to a Railway project. Run: railway link" >&2
  exit 1
fi

{
  echo "# Railway inventory"
  echo
  echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Account: $(railway whoami 2>/dev/null | tr -d '\r')"
  echo
  echo "Read-only. No service, variable, volume, or deployment was modified."
  echo
} | emit

# Services and their environments, from the linked project.
python3 - "$status_json" <<'PY' | emit
import json
import sys

try:
    status = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("Could not parse `railway status --json`.")
    raise SystemExit(0)

print("## Project")
print()
print(f"- Name: {status.get('name', 'unknown')}")
print(f"- Id: {status.get('id', 'unknown')}")
print()

services = status.get("services") or {}
edges = services.get("edges") if isinstance(services, dict) else None
print("## Services")
print()
if not edges:
    print("No services reported. Check that the linked environment is the one you expect.")
else:
    print("| Service | Id |")
    print("| --- | --- |")
    for edge in edges:
        node = (edge or {}).get("node") or {}
        print(f"| {node.get('name', '?')} | `{node.get('id', '?')}` |")
print()

environments = status.get("environments") or {}
env_edges = environments.get("edges") if isinstance(environments, dict) else None
print("## Environments")
print()
if not env_edges:
    print("No environments reported.")
else:
    for edge in env_edges:
        node = (edge or {}).get("node") or {}
        print(f"- {node.get('name', '?')} (`{node.get('id', '?')}`)")
print()
PY

{
  echo "## Variable names for the linked service"
  echo
  echo "Names only. Values are deliberately omitted."
  echo
} | emit

if variables_json="$(railway variables --json 2>/dev/null)"; then
  python3 - "$variables_json" <<'PY' | emit
import json
import sys

try:
    variables = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("Could not parse `railway variables --json`.")
    raise SystemExit(0)

if not isinstance(variables, dict) or not variables:
    print("No variables reported for the linked service.")
    raise SystemExit(0)

interesting = ("HAWKNETIC_SERVICE", "HAWKNETIC_SERVICE_MODE", "DATABASE_URL", "PORT")
for name in sorted(variables):
    marker = "  <-- role/database selector" if name in interesting else ""
    print(f"- `{name}`{marker}")
print()
role = variables.get("HAWKNETIC_SERVICE")
mode = variables.get("HAWKNETIC_SERVICE_MODE")
if role:
    print(f"This service's role is `{role}`" + (f", mode `{mode}`." if mode else ", mode `loop` (default)."))
    print()
PY
else
  echo "Could not read variables. Link a service with \`railway service\` and re-run." | emit
  echo | emit
fi

{
  echo "## Which workers have actually run"
  echo
  echo "This is the authoritative answer to the disagreement recorded in"
  echo "\`docs/railway-worker-services.md\`. A worker that has never run has no row."
  echo "A worker that stopped has a stale \`heartbeat_at\`."
  echo
} | emit

# Ask the database rather than the service list: a service can exist and never
# have completed a cycle, and that distinction is the whole question.
if worker_rows="$(railway run -- python -c '
import os, sys
sys.path.insert(0, "src")
import psycopg

url = os.environ.get("DATABASE_URL")
if not url:
    print("DATABASE_URL is not present in this service environment.")
    raise SystemExit(0)
with psycopg.connect(url, connect_timeout=10) as connection:
    rows = connection.execute(
        "SELECT worker_name, status, consecutive_failures, last_error_code, heartbeat_at "
        "FROM ops.worker_status ORDER BY worker_name"
    ).fetchall()
if not rows:
    print("No rows in ops.worker_status: no worker has completed a cycle.")
else:
    print("| Worker | Status | Consecutive failures | Last error | Heartbeat |")
    print("| --- | --- | ---: | --- | --- |")
    for row in rows:
        cells = ["" if value is None else str(value) for value in row]
        print("| " + " | ".join(cells) + " |")
' 2>/dev/null)"; then
  echo "$worker_rows" | emit
else
  {
    echo "Could not reach the database through \`railway run\`."
    echo
    echo "Run this against the production database yourself:"
    echo
    echo '```sql'
    echo "SELECT worker_name, status, consecutive_failures, last_error_code, heartbeat_at"
    echo "FROM ops.worker_status ORDER BY worker_name;"
    echo '```'
  } | emit
fi

{
  echo
  echo "## Next"
  echo
  echo "1. Compare the service list against the worker rows above."
  echo "2. A service with no row and no recent deployment is a candidate for deletion;"
  echo "   confirm it is obsolete before removing it, not merely idle."
  echo "3. Check per-service usage in the Railway dashboard under Usage, and record"
  echo "   the figures in \`docs/INFRASTRUCTURE_COSTS.md\`."
  echo "4. Delete whichever claim in \`docs/railway-worker-services.md\` this disproves."
} | emit

if [[ -n "$output_path" ]]; then
  echo "Wrote $output_path" >&2
fi
