from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import repo_path


MIGRATION_LOCK_KEY = 843195021447
MIGRATION_SCHEMA = "ops"
REQUIRED_SCHEMAS = ("app", "raw", "core", "research", "ops", "reporting", "auth")
REQUIRED_RELATIONS = (
    "app.prediction_logs",
    "raw.ingestion_batches",
    "core.markets",
    "research.predictions",
    "ops.schema_migrations",
    "auth.app_users",
    "reporting.latest_market_state",
)


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    sha256: str


def discover_migrations(directory: str | Path | None = None) -> list[Migration]:
    root = Path(directory) if directory is not None else repo_path("migrations", "postgres")
    migrations: list[Migration] = []
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=path.stem.split("_", 1)[0],
                path=path,
                sql=sql,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    return migrations


def _statements(script: str) -> Iterable[str]:
    statement: list[str] = []
    in_single = False
    in_double = False
    dollar_tag: str | None = None
    index = 0
    while index < len(script):
        character = script[index]
        if dollar_tag is None and not in_single and not in_double:
            if script.startswith("--", index):
                newline = script.find("\n", index)
                index = len(script) if newline == -1 else newline + 1
                continue
            if script.startswith("/*", index):
                closing = script.find("*/", index + 2)
                if closing == -1:
                    raise RuntimeError("unterminated_migration_comment")
                index = closing + 2
                continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                statement.extend(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            statement.append(character)
            index += 1
            continue
        if character == "$" and not in_single and not in_double:
            closing = script.find("$", index + 1)
            if closing != -1:
                candidate = script[index : closing + 1]
                if candidate == "$$" or candidate[1:-1].replace("_", "a").isalnum():
                    dollar_tag = candidate
                    statement.extend(candidate)
                    index = closing + 1
                    continue
        if character == "'" and not in_double:
            if in_single and index + 1 < len(script) and script[index + 1] == "'":
                statement.extend((character, script[index + 1]))
                index += 2
                continue
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        if character == ";" and not in_single and not in_double and dollar_tag is None:
            text = "".join(statement).strip()
            if text:
                yield text
            statement = []
        else:
            statement.append(character)
        index += 1
    text = "".join(statement).strip()
    if text:
        yield text


def _driver():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency validation covers this path
        raise RuntimeError("postgres_driver_unavailable") from exc
    return psycopg


def _validated_database_url(database_url: str) -> str:
    value = str(database_url or "").strip()
    if not value or urlparse(value).scheme.lower() not in {"postgres", "postgresql"}:
        raise RuntimeError("postgres_database_url_required")
    return value


def _ensure_migration_ledger(connection: Any) -> None:
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {MIGRATION_SCHEMA}")
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_SCHEMA}.schema_migrations (
            version TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def apply_postgres_migrations(
    database_url: str,
    *,
    directory: str | Path | None = None,
    statement_timeout_ms: int = 300000,
) -> dict[str, Any]:
    database_url = _validated_database_url(database_url)
    psycopg = _driver()
    newly_applied: list[str] = []
    with psycopg.connect(
        database_url,
        autocommit=False,
        connect_timeout=5,
        options=f"-c statement_timeout={max(1000, int(statement_timeout_ms))} -c timezone=UTC",
    ) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            _ensure_migration_ledger(connection)
            applied = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    f"SELECT version, sha256 FROM {MIGRATION_SCHEMA}.schema_migrations"
                ).fetchall()
            }
            for migration in discover_migrations(directory):
                existing_hash = applied.get(migration.version)
                if existing_hash:
                    if existing_hash != migration.sha256:
                        raise RuntimeError(f"applied_migration_hash_mismatch:{migration.version}")
                    continue
                for statement in _statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    f"INSERT INTO {MIGRATION_SCHEMA}.schema_migrations (version, sha256) VALUES (%s, %s)",
                    (migration.version, migration.sha256),
                )
                newly_applied.append(migration.version)
                applied[migration.version] = migration.sha256
            return {
                "dialect": "postgres",
                "newly_applied": newly_applied,
                "applied_versions": sorted(applied),
                "pending_versions": [],
                "ready": True,
            }


def postgres_migration_status(
    database_url: str,
    *,
    connect_timeout_seconds: int = 5,
    statement_timeout_ms: int = 30000,
) -> dict[str, Any]:
    try:
        database_url = _validated_database_url(database_url)
    except RuntimeError:
        return {
            "dialect": "postgres",
            "ready": False,
            "state": "missing_required",
            "reason": "postgres_database_url_required",
        }
    try:
        psycopg = _driver()
        with psycopg.connect(
            database_url,
            connect_timeout=max(1, int(connect_timeout_seconds)),
            options=f"-c statement_timeout={max(1000, int(statement_timeout_ms))} -c timezone=UTC",
        ) as connection:
            exists = connection.execute(
                "SELECT to_regclass('ops.schema_migrations') IS NOT NULL"
            ).fetchone()[0]
            if not exists:
                return {
                    "dialect": "postgres",
                    "ready": False,
                    "state": "configured_degraded",
                    "reason": "migration_ledger_missing",
                    "pending_versions": [migration.version for migration in discover_migrations()],
                }
            applied = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    f"SELECT version, sha256 FROM {MIGRATION_SCHEMA}.schema_migrations"
                ).fetchall()
            }
            present_schemas = {
                str(row[0])
                for row in connection.execute(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
                    (list(REQUIRED_SCHEMAS),),
                ).fetchall()
            }
            missing_schemas = [schema for schema in REQUIRED_SCHEMAS if schema not in present_schemas]
            missing_relations = [
                relation
                for relation in REQUIRED_RELATIONS
                if connection.execute("SELECT to_regclass(%s)", (relation,)).fetchone()[0] is None
            ]
    except Exception as exc:
        return {
            "dialect": "postgres",
            "ready": False,
            "state": "configured_failed",
            "reason": f"database_connection_failed:{type(exc).__name__}",
        }
    pending: list[str] = []
    for migration in discover_migrations():
        if migration.version not in applied:
            pending.append(migration.version)
        elif applied[migration.version] != migration.sha256:
            return {
                "dialect": "postgres",
                "ready": False,
                "state": "configured_failed",
                "reason": f"applied_migration_hash_mismatch:{migration.version}",
            }
    if missing_schemas or missing_relations:
        return {
            "dialect": "postgres",
            "ready": False,
            "state": "configured_failed",
            "reason": "required_schema_objects_missing",
            "missing_schemas": missing_schemas,
            "missing_relations": missing_relations,
            "pending_versions": pending,
            "applied_versions": sorted(applied),
        }
    return {
        "dialect": "postgres",
        "ready": not pending,
        "state": "configured_healthy" if not pending else "configured_degraded",
        "pending_versions": pending,
        "applied_versions": sorted(applied),
        "required_schemas": list(REQUIRED_SCHEMAS),
        "required_relations": list(REQUIRED_RELATIONS),
    }
