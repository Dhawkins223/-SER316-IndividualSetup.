from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from .db_migrations import postgres_migration_status


APPLICATION_SCHEMA = "app"


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str | None
    pool_min_size: int
    pool_max_size: int
    migration_mode: str
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 30000
    migration_statement_timeout_ms: int = 300000

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        migration_mode = str(os.environ.get("DATABASE_MIGRATION_MODE") or "check").strip().lower()
        if migration_mode not in {"check", "apply"}:
            raise ValueError("database_migration_mode_must_be_check_or_apply")
        return cls(
            database_url=os.environ.get("DATABASE_URL") or None,
            pool_min_size=max(1, int(os.environ.get("DATABASE_POOL_MIN_SIZE", "1"))),
            pool_max_size=max(1, int(os.environ.get("DATABASE_POOL_MAX_SIZE", "5"))),
            migration_mode=migration_mode,
            connect_timeout_seconds=max(1, int(os.environ.get("DATABASE_CONNECT_TIMEOUT", "5"))),
            statement_timeout_ms=max(1000, int(os.environ.get("DATABASE_STATEMENT_TIMEOUT", "30000"))),
            migration_statement_timeout_ms=max(
                1000,
                int(os.environ.get("DATABASE_MIGRATION_STATEMENT_TIMEOUT", "300000")),
            ),
        )

    def require_url(self) -> str:
        database_url = str(self.database_url or "").strip()
        if not database_url or urlparse(database_url).scheme.lower() not in {"postgres", "postgresql"}:
            raise RuntimeError("postgres_database_url_required")
        return database_url

    def safe_description(self) -> dict[str, Any]:
        parsed = urlparse(self.database_url or "")
        return {
            "backend": "postgres",
            "schema": APPLICATION_SCHEMA,
            "host": parsed.hostname,
            "port": parsed.port,
            "database": parsed.path.lstrip("/") or None,
            "credentials_present": bool(parsed.username or parsed.password),
        }


class DatabaseRow(Mapping[str, Any]):
    """A PostgreSQL result row with deterministic name and position access."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(str(column) for column in columns)
        self._values = tuple(values)
        self._data = dict(zip(self._columns, self._values, strict=True))

    def __getitem__(self, key: str | int) -> Any:  # type: ignore[override]
        if isinstance(key, int):
            return self._values[key]
        return self._data[key]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()


def database_row_factory(cursor: Any):
    if cursor.description is None:
        return lambda values: values
    columns = tuple(str(getattr(column, "name", column[0])) for column in cursor.description)

    def make_row(values: Sequence[Any]) -> DatabaseRow:
        return DatabaseRow(columns, values)

    return make_row


class DatabaseCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.rowcount = int(getattr(cursor, "rowcount", 0) or 0)
        self.description = cursor.description or []

    def fetchone(self) -> DatabaseRow | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[DatabaseRow]:
        return list(self._cursor.fetchall())


def _compile_statement(statement: str) -> str:
    text = str(statement).strip()
    if not text:
        raise ValueError("database_statement_required")
    normalized = " ".join(text.lower().split())
    forbidden = {
        "pragma": "database_pragma_not_supported",
        "begin immediate": "database_manual_lock_not_supported",
        "insert or ignore": "database_conflict_target_required",
    }
    for fragment, error in forbidden.items():
        if fragment in normalized:
            raise RuntimeError(error)
    if "?" in text:
        raise RuntimeError("postgres_parameter_style_required")
    return text


class DatabaseSession:
    """A PostgreSQL-only transactional session for the application schema."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, statement: str, parameters: Sequence[Any] | Mapping[str, Any] | None = None) -> DatabaseCursor:
        cursor = self._connection.execute(_compile_statement(statement), parameters or ())
        return DatabaseCursor(cursor)

    def executemany(
        self,
        statement: str,
        parameters: Sequence[Sequence[Any] | Mapping[str, Any]],
    ) -> DatabaseCursor:
        cursor = self._connection.cursor()
        cursor.executemany(_compile_statement(statement), parameters)
        return DatabaseCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class PostgresPool:
    def __init__(self, settings: DatabaseSettings) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency validation covers this path
            raise RuntimeError("postgres_pool_unavailable") from exc
        self.settings = settings
        self._open_lock = Lock()
        self._opened = False
        self._pool = ConnectionPool(
            conninfo=settings.require_url(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open=False,
            # Validate a pooled connection before handing it out. A worker on an
            # hourly cadence leaves its connection idle long enough for the server
            # or an intermediary to drop it, and without this check the first
            # statement of every cycle fails with "the connection is lost".
            check=ConnectionPool.check_connection,
            kwargs={
                "autocommit": False,
                "connect_timeout": settings.connect_timeout_seconds,
                "row_factory": database_row_factory,
                "options": (
                    f"-c statement_timeout={settings.statement_timeout_ms} "
                    "-c timezone=UTC "
                    f"-c search_path={APPLICATION_SCHEMA},pg_catalog"
                ),
            },
        )

    def open(self) -> None:
        # psycopg's initialization wait has a single waiter. Run it once,
        # before concurrent borrowers can consume the initial connections.
        with self._open_lock:
            if not self._opened:
                self._pool.open(wait=True)
                self._opened = True

    def close(self) -> None:
        with self._open_lock:
            self._pool.close()
            self._opened = False

    @contextmanager
    def connection(self) -> Iterator[DatabaseSession]:
        self.open()
        with self._pool.connection() as connection:
            session = DatabaseSession(connection)
            try:
                yield session
                connection.commit()
            except Exception:
                connection.rollback()
                raise


_pools: dict[DatabaseSettings, PostgresPool] = {}
_pools_lock = Lock()


def connection_pool(settings: DatabaseSettings | None = None) -> PostgresPool:
    configured = settings or DatabaseSettings.from_env()
    with _pools_lock:
        pool = _pools.get(configured)
        if pool is None:
            pool = PostgresPool(configured)
            _pools[configured] = pool
        return pool


def close_connection_pools() -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


def database_startup_status(settings: DatabaseSettings | None = None) -> dict[str, Any]:
    configured = settings or DatabaseSettings.from_env()
    status = postgres_migration_status(
        configured.database_url or "",
        connect_timeout_seconds=configured.connect_timeout_seconds,
        statement_timeout_ms=configured.statement_timeout_ms,
    )
    status["description"] = configured.safe_description()
    return status


def production_safety_status() -> dict[str, Any]:
    hosted = bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or str(os.environ.get("APP_ENV") or "").lower() in {"staging", "production"}
    )
    required = {
        "RESEARCH_ONLY": True,
        "LIVE_EXECUTION_ENABLED": False,
        "AUTO_UPLOAD_ENABLED": False,
        "AUTO_TRADE_ENABLED": False,
        "KALSHI_ORDER_UPLOAD_ENABLED": False,
        "MODEL_PROMOTION_ENABLED": False,
        "STALE_CACHE_AS_FRESH": False,
    }
    failures = []
    for name, expected in required.items():
        default = not expected if hosted else expected
        value = str(os.environ.get(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}
        if value != expected:
            failures.append(name)
    if hosted:
        require_dashboard_auth = str(
            os.environ.get("DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not require_dashboard_auth:
            failures.append("DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED")
    return {
        "hosted": hosted,
        "ready": not failures,
        "required": required,
        "failed_controls": failures,
    }


def as_decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    """Return an exact decimal without accepting binary-float arithmetic downstream."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def json_default(value: Any) -> Any:
    """Serialize exact values at API/report boundaries without losing precision."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not_json_serializable:{type(value).__name__}")
