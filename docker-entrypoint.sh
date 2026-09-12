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
#
# ## KNOWN GAP: this composes the URL once, and RDS rotates the password
#
# RDS master user password management "rotates the secret every seven days by
# default" (AWS, "Password management with Amazon RDS and AWS Secrets
# Manager"). ECS resolves a container's `secrets` entries only at task start,
# so a resident task -- the web service, and any worker running in `loop` mode
# -- keeps the password it was handed on day one.
#
# What that looks like in practice: connections already established keep
# working, because PostgreSQL authenticates once at connect time. New ones
# fail. So the symptom is not an outage at rotation but intermittent
# authentication failures as the pool turns over, roughly a week after deploy
# and weekly after that. Scheduled workers are unaffected -- a new task per run
# means freshly injected credentials every time.
#
# This is NOT fixed here, and it needs a decision before production traffic
# moves. The three real options:
#
#   1. React: EventBridge on the rotation event -> Lambda calling
#      ecs:UpdateService --force-new-deployment. Self-contained, but reactive,
#      so there is a window of failures before the redeploy lands.
#   2. Stop using the master user for the application. Provision a dedicated
#      role whose credentials the application owns. This is the conventional
#      answer and the one that removes the problem rather than reacting to it;
#      it needs a database-level provisioning step Terraform does not do today.
#   3. Resolve credentials in the application at connect time instead of
#      through ECS injection. Correct, and the largest change: it touches code
#      shared with Railway, Codespaces and CI.
#
# Recommended: 2, with 1 as the stopgap if production moves first. Not chosen
# here because nothing is deployed yet and the choice has cost and complexity
# implications that belong to the owner -- see docs/aws-migration/STATUS.md.

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
