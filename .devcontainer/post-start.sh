#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -x .venv/bin/python && -f .env && -x "$(command -v docker 2>/dev/null || true)" && docker info >/dev/null 2>&1 ]]; then
  export PYTHON_BIN="$repo_root/.venv/bin/python"
  ./scripts/local.sh db-start
elif [[ -x .venv/bin/python && -f .env ]]; then
  echo "Docker is unavailable; cloud Codespace startup does not start local PostgreSQL."
fi
