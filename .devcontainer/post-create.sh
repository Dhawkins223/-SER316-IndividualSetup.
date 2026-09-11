#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! bash .devcontainer/install-cloud-tools.sh; then
  echo "Optional cloud-tool bootstrap failed; continuing with the Python workspace."
  echo "Re-run .devcontainer/install-cloud-tools.sh after the Codespace is ready."
fi

python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

export PYTHON_BIN="$repo_root/.venv/bin/python"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ./scripts/local.sh setup
else
  echo "Docker is unavailable in this Codespace; skipping local PostgreSQL setup."
  echo "Use Railway DATABASE_URL for cloud data, or run CI for Compose-backed tests."
fi

echo
echo "Codespace setup complete."
echo "Start the dashboard with: ./scripts/local.sh dev"
echo "Run all validation with: ./scripts/local.sh verify"
