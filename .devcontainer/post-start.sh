#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# `[[ ]]` tests conditions; it does not run commands. `docker info` inside it is
# a parse error, not a daemon check, so this whole file failed to parse -- and
# the CI gate that should have said so was checking only its first argument.
# Command checks belong outside the brackets, chained with &&.
if [[ -x .venv/bin/python && -f .env ]] \
  && command -v docker >/dev/null 2>&1 \
  && docker info >/dev/null 2>&1; then
  export PYTHON_BIN="$repo_root/.venv/bin/python"
  ./scripts/local.sh db-start
elif [[ -x .venv/bin/python && -f .env ]]; then
  echo "Docker is unavailable; cloud Codespace startup does not start local PostgreSQL."
fi
