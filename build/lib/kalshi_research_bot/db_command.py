from __future__ import annotations

import argparse
import json

from .database import database_startup_status
from .db_migrations import apply_postgres_migrations


def main() -> int:
    parser = argparse.ArgumentParser(prog="hawknetic-database")
    parser.add_argument("action", choices=("migrate", "status"))
    args = parser.parse_args()
    if args.action == "migrate":
        from .database import DatabaseSettings

        settings = DatabaseSettings.from_env()
        result = apply_postgres_migrations(
            settings.require_url(),
            statement_timeout_ms=settings.migration_statement_timeout_ms,
        )
    else:
        result = database_startup_status()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
