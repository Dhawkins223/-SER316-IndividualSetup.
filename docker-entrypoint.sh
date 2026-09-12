#!/bin/sh
#
# Container entrypoint.
#
# Composes DATABASE_URL from its parts when it is not already set, then execs
# the application.
#
# Why this exists: the application reads DATABASE_URL and nothing else --
# DatabaseSettings.from_env() takes os.environ["DATABASE_URL"], and
# require_url() rejects anything without a postgres:// scheme. There is no
# fallback to POSTGRES_HOST/USER/PASSWORD.
#
# On ECS the credentials come from the RDS-managed Secrets Manager secret,
# which holds a JSON document with `username` and `password`. ECS can inject
# individual JSON keys as separate environment variables, but it cannot
# concatenate them into a URL. The alternatives were:
#
#   - Hand-write a DATABASE_URL into a second secret. Rejected: it duplicates
#     the password outside the secret RDS manages, and goes stale the moment
#     RDS rotates it.
#   - Change the application to accept parts. Rejected: it is used by Railway,
#     Codespaces, CI and the test suite, all of which already pass a URL.
#
# So the composition happens here, in packaging, where the ECS-specific shape
# of the problem belongs.
#
# Anything that already sets DATABASE_URL -- Railway, Codespaces, CI, a local
# run -- is unaffected: the block below is skipped entirely.

set -eu

if [ -z "${DATABASE_URL:-}" ] && [ -n "${POSTGRES_HOST:-}" ] && [ -n "${POSTGRES_USER:-}" ]; then
    # Percent-encode the credentials. RDS-generated passwords can contain
    # characters that are structural in a URL (/, ?, #, @), and pasting one in
    # raw yields a URL that parses into the wrong host or database rather than
    # failing loudly.
    DATABASE_URL="$(
        python - <<'PY'
import os
from urllib.parse import quote

user = quote(os.environ["POSTGRES_USER"], safe="")
password = quote(os.environ.get("POSTGRES_PASSWORD", ""), safe="")
host = os.environ["POSTGRES_HOST"]
port = os.environ.get("POSTGRES_PORT", "5432")
name = os.environ.get("POSTGRES_DB", "hawknetic")
sslmode = os.environ.get("POSTGRES_SSLMODE", "require")

credentials = f"{user}:{password}" if password else user
print(f"postgresql://{credentials}@{host}:{port}/{name}?sslmode={sslmode}")
PY
    )"
    export DATABASE_URL
fi

# exec so the application becomes PID 1's child in the same process slot and
# receives ECS's SIGTERM directly, rather than it stopping at this shell.
exec python -m kalshi_research_bot "$@"
