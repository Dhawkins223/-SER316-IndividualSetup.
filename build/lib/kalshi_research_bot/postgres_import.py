from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .database import DatabaseSession, json_default


_QUALIFIED_TABLE = re.compile(r"^(app|auth|core|ops|raw|research)\.[a-z_][a-z0-9_]*$")
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$", re.IGNORECASE)


class ImportConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    identical_duplicates: int
    conflicts: int


def _canonical_json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        rendered = format(normalized, "f")
        return "0" if rendered == "-0" else rendered
    return json_default(value)


def _canonicalize_hash_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        rendered = format(normalized, "f")
        return "0" if rendered == "-0" else rendered
    if isinstance(value, datetime):
        timestamp = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return timestamp.isoformat()
    if isinstance(value, str) and "T" in value:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        return timestamp.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonicalize_hash_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_hash_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize_hash_value(item) for item in value]
    return value


def canonical_row_hash(row: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonicalize_hash_value(dict(row)),
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_identifier(value: str, *, table: bool = False) -> str:
    pattern = _QUALIFIED_TABLE if table else _COLUMN
    if not pattern.fullmatch(value):
        raise ValueError("invalid_import_identifier")
    return value


def import_canonical_rows(
    connection: DatabaseSession,
    *,
    table: str,
    key_columns: tuple[str, ...],
    rows: Iterable[Mapping[str, Any]],
    json_columns: tuple[str, ...] = (),
    override_system_identity: bool = False,
) -> ImportResult:
    """Import neutral JSON/CSV-shaped rows with content-verified duplicate handling."""
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - dependency validation covers this path
        raise RuntimeError("postgres_json_adapter_unavailable") from exc
    qualified_table = _validated_identifier(table, table=True)
    keys = tuple(_validated_identifier(column) for column in key_columns)
    json_fields = frozenset(_validated_identifier(column) for column in json_columns)
    if not keys:
        raise ValueError("import_business_key_required")
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"canonical-import:{qualified_table}",),
    )
    inserted = 0
    identical_duplicates = 0
    conflicts = 0
    for source_row in rows:
        row = dict(source_row)
        columns = tuple(_validated_identifier(column) for column in sorted(row))
        if not set(keys).issubset(columns):
            raise ValueError("import_row_missing_business_key")
        values = tuple(Jsonb(row[column], dumps=lambda value: json.dumps(value, default=json_default)) if column in json_fields else row[column] for column in columns)
        predicates = " AND ".join(f"{column} IS NOT DISTINCT FROM %s" for column in keys)
        existing = connection.execute(
            f"SELECT {', '.join(columns)} FROM {qualified_table} WHERE {predicates} FOR UPDATE",
            tuple(row[column] for column in keys),
        ).fetchone()
        if existing is not None:
            if canonical_row_hash(dict(existing)) != canonical_row_hash(row):
                conflicts += 1
                raise ImportConflictError(f"import_content_conflict:{qualified_table}:{':'.join(str(row[column]) for column in keys)}")
            identical_duplicates += 1
            continue
        placeholders = ", ".join("%s" for _ in columns)
        identity_clause = " OVERRIDING SYSTEM VALUE" if override_system_identity else ""
        connection.execute(
            f"INSERT INTO {qualified_table} ({', '.join(columns)}){identity_clause} VALUES ({placeholders})",
            values,
        )
        inserted += 1
    return ImportResult(inserted=inserted, identical_duplicates=identical_duplicates, conflicts=conflicts)


def record_import_lineage(
    connection: DatabaseSession,
    *,
    import_id: str,
    source_system: str,
    source_table: str,
    source_key: str,
    target_table: str,
    target_key: str,
    content_hash: str,
) -> bool:
    """Record a neutral import mapping and fail if the same source key changes content."""
    values = {
        "import_id": import_id,
        "source_system": source_system,
        "source_table": source_table,
        "source_key": source_key,
        "target_table": target_table,
        "target_key": target_key,
        "content_hash": content_hash,
    }
    if not all(_TOKEN.fullmatch(str(value)) for value in values.values()):
        raise ValueError("invalid_import_lineage_value")
    lock_key = f"import-lineage:{source_system}:{source_table}"
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
    existing = connection.execute(
        """
        SELECT source_system, target_table, target_key, content_hash
        FROM ops.import_lineage
        WHERE import_id = %s AND source_table = %s AND source_key = %s
        FOR UPDATE
        """,
        (import_id, source_table, source_key),
    ).fetchone()
    if existing is not None:
        if (
            existing["source_system"] != source_system
            or existing["target_table"] != target_table
            or existing["target_key"] != target_key
            or existing["content_hash"] != content_hash
        ):
            raise ImportConflictError(f"import_lineage_conflict:{source_table}:{source_key}")
        return False
    connection.execute(
        """
        INSERT INTO ops.import_lineage (
            import_id, source_system, source_table, source_key,
            target_table, target_key, content_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (import_id, source_system, source_table, source_key, target_table, target_key, content_hash),
    )
    return True
