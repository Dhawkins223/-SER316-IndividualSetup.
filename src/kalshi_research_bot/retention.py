"""Age out raw payload bodies without losing the evidence they prove.

`raw.source_payloads` keeps one full response body per collection cycle. Kalshi
ingestion runs every five minutes, so the table grows without bound: it filled
the staging volume outright and production is on the same curve.

Deleting those rows is not an option. Core observations carry foreign keys into
them, and the project's integrity contract treats a payload's content hash as
authoritative evidence that a collection happened and what it contained. So this
prunes the *body* and keeps everything that proves the collection: the row, its
batch lineage, its timestamps, its parser version, and its `content_hash`.

The body is replaced by a tombstone recording when it was pruned and how large it
was. Dedup and import lineage compare the `content_hash` column and never re-hash
the body, so pruning does not disturb either. The body itself is not recoverable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .business_store import ensure_database_ready
from .database import DatabaseSettings, connection_pool


TOMBSTONE_KEY = "__retention__"
TOMBSTONE_STATE = "body_pruned"
# Ten days, not thirty. The window sets the table's steady-state size
# (`daily_growth x window_days`), and production measured ~166 MB/day of raw
# payloads against a 5 GB volume: thirty days wants ~5 GB of payloads alone,
# which the volume cannot hold. A window wider than the data's own age is also
# indistinguishable from having no retention at all, because nothing ever
# becomes eligible -- that is exactly how production reached 100% of its volume
# and PANICked on 2026-09-01. Ten days is the setting production proved:
# ~1.7 GB of payloads, and the first pass drained the eligible backlog to zero.
# See docs/raw-payload-retention.md for the arithmetic.
DEFAULT_RETENTION_DAYS = 10
MINIMUM_RETENTION_DAYS = 7
DEFAULT_BATCH_LIMIT = 5000

# Railway's Hobby plan provisions 5 GB volumes and that is the plan's ceiling,
# so it is the capacity this project actually runs against. Override it with
# DATABASE_VOLUME_CAPACITY_BYTES when the volume is resized or the database
# moves somewhere with a different ceiling.
DEFAULT_VOLUME_CAPACITY_BYTES = 5_000_000_000
CAPACITY_WARNING_RATIO = 0.75
CAPACITY_CRITICAL_RATIO = 0.90


class RetentionWindowTooShort(ValueError):
    """Raised when a requested window would prune recent evidence."""


def _now(now: datetime | str | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _span_days(oldest: Any, newest: Any) -> float | None:
    """How many days of payloads the table currently holds."""
    if not isinstance(oldest, datetime) or not isinstance(newest, datetime):
        return None
    return round((newest - oldest).total_seconds() / 86400.0, 2)


def _window_bites(oldest: Any, now: datetime, window_days: int) -> bool:
    """Whether anything is old enough for this window to reach.

    False means the window is wider than the data's own age: it will prune
    nothing, and will keep pruning nothing until the table ages into it -- which
    on a bounded volume may never happen.
    """
    if not isinstance(oldest, datetime):
        return False
    return (now - oldest).total_seconds() > window_days * 86400.0


def _tombstone(*, pruned_at: str, original_bytes: int, content_hash: str) -> str:
    return json.dumps(
        {
            TOMBSTONE_KEY: {
                "state": TOMBSTONE_STATE,
                "pruned_at": pruned_at,
                "original_bytes": int(original_bytes),
                "content_hash": content_hash,
            }
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# One definition of "prunable", shared by the pruner and the report so the two can
# never disagree about how much is eligible. The newest payload per
# (source, entity_type) is what the dashboard reads for its current snapshot, so it
# is never a candidate no matter how the window is configured.
_PRUNABLE_SQL = """
    WITH newest AS (
        SELECT DISTINCT ON (source, entity_type) id
        FROM raw.source_payloads
        ORDER BY source, entity_type, observed_at DESC NULLS LAST, id DESC
    )
    SELECT {columns}
    FROM raw.source_payloads
    WHERE received_at < %s
      AND id NOT IN (SELECT id FROM newest)
      AND NOT jsonb_exists(payload_json, %s)
      AND (%s::text IS NULL OR source = %s)
"""


def _prunable_rows(
    connection: Any,
    *,
    cutoff: datetime,
    limit: int,
    source: str | None,
) -> list[Any]:
    statement = (
        _PRUNABLE_SQL.format(columns="id, content_hash, pg_column_size(payload_json) AS body_bytes")
        + " ORDER BY received_at, id LIMIT %s"
    )
    return connection.execute(statement, (cutoff, TOMBSTONE_KEY, source, source, limit)).fetchall()


def _prunable_totals(connection: Any, *, cutoff: datetime, source: str | None) -> Any:
    statement = _PRUNABLE_SQL.format(
        columns="COUNT(*) AS rows, COALESCE(SUM(pg_column_size(payload_json)), 0) AS body_bytes"
    )
    return connection.execute(statement, (cutoff, TOMBSTONE_KEY, source, source)).fetchone()


def prune_source_payload_bodies(
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    source: str | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    dry_run: bool = True,
    settings: DatabaseSettings | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Replace payload bodies older than the window with a tombstone.

    Defaults to a dry run: the same rows are measured and reported, and nothing is
    written. Pass `dry_run=False` to apply.
    """
    days = int(older_than_days)
    if days < MINIMUM_RETENTION_DAYS:
        raise RetentionWindowTooShort(
            f"retention_window_below_minimum:{days}<{MINIMUM_RETENTION_DAYS}"
        )
    configured = ensure_database_ready(settings)
    moment = _now(now)
    cutoff = moment - timedelta(days=days)
    bounded_limit = max(1, min(int(limit), 100000))

    with connection_pool(configured).connection() as connection:
        candidates = _prunable_rows(connection, cutoff=cutoff, limit=bounded_limit, source=source)
        reclaimable_bytes = sum(int(row["body_bytes"] or 0) for row in candidates)
        pruned = 0
        if not dry_run and candidates:
            pruned_at = moment.isoformat()
            for row in candidates:
                connection.execute(
                    "UPDATE raw.source_payloads SET payload_json = %s::jsonb WHERE id = %s",
                    (
                        _tombstone(
                            pruned_at=pruned_at,
                            original_bytes=int(row["body_bytes"] or 0),
                            content_hash=str(row["content_hash"]),
                        ),
                        int(row["id"]),
                    ),
                )
                pruned += 1
        # Counted after the updates, so it already reflects what this pass wrote.
        remaining = _prunable_totals(connection, cutoff=cutoff, source=source)
        # The age span decides whether a window can ever bite. A window wider than
        # the data's own age prunes nothing and looks identical to a broken one.
        span = connection.execute(
            """
            SELECT MIN(received_at) AS oldest, MAX(received_at) AS newest
            FROM raw.source_payloads
            WHERE (%s::text IS NULL OR source = %s)
            """,
            (source, source),
        ).fetchone()

    return {
        "dry_run": bool(dry_run),
        "evaluated_at": moment.isoformat(),
        "cutoff": cutoff.isoformat(),
        "older_than_days": days,
        "source": source,
        "candidates": len(candidates),
        "pruned": pruned,
        "reclaimable_bytes": reclaimable_bytes,
        "remaining_after_limit": int(remaining["rows"] or 0),
        "limit": bounded_limit,
        "oldest_received_at": _iso(span["oldest"]),
        "newest_received_at": _iso(span["newest"]),
        "retained_span_days": _span_days(span["oldest"], span["newest"]),
        "window_bites": _window_bites(span["oldest"], moment, days),
        "note": (
            "Payload bodies are replaced by a tombstone. Rows, batch lineage, "
            "timestamps, parser versions, and content hashes are preserved; the "
            "body itself is not recoverable. PostgreSQL reuses the freed space for "
            "new rows, but returning it to the filesystem needs a VACUUM FULL, "
            "which requires free space of its own."
        ),
    }


def source_payload_storage_report(
    *,
    settings: DatabaseSettings | None = None,
    now: datetime | str | None = None,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """Where the raw table's space is going, and how much a prune would free."""
    configured = ensure_database_ready(settings)
    moment = _now(now)
    cutoff = moment - timedelta(days=int(older_than_days))
    with connection_pool(configured).connection() as connection:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(*) FILTER (WHERE jsonb_exists(payload_json, %s)) AS tombstoned,
                   COALESCE(SUM(pg_column_size(payload_json)), 0) AS body_bytes,
                   MIN(received_at) AS oldest,
                   MAX(received_at) AS newest
            FROM raw.source_payloads
            """,
            (TOMBSTONE_KEY,),
        ).fetchone()
        prunable = _prunable_totals(connection, cutoff=cutoff, source=None)
        by_source = connection.execute(
            """
            SELECT source,
                   COUNT(*) AS rows,
                   COALESCE(SUM(pg_column_size(payload_json)), 0) AS body_bytes
            FROM raw.source_payloads
            GROUP BY source
            ORDER BY SUM(pg_column_size(payload_json)) DESC
            """
        ).fetchall()
    return {
        "evaluated_at": moment.isoformat(),
        "cutoff": cutoff.isoformat(),
        "older_than_days": int(older_than_days),
        "total_rows": int(totals["rows"] or 0),
        "tombstoned_rows": int(totals["tombstoned"] or 0),
        "total_body_bytes": int(totals["body_bytes"] or 0),
        "oldest_received_at": totals["oldest"].isoformat() if totals["oldest"] else None,
        "newest_received_at": totals["newest"].isoformat() if totals["newest"] else None,
        "prunable_rows": int(prunable["rows"] or 0),
        "prunable_body_bytes": int(prunable["body_bytes"] or 0),
        "by_source": [
            {
                "source": str(row["source"]),
                "rows": int(row["rows"]),
                "body_bytes": int(row["body_bytes"]),
            }
            for row in by_source
        ],
    }


def source_payload_duplication_report(
    *,
    settings: DatabaseSettings | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """How much of the raw table is the same body stored more than once.

    The table's uniqueness constraint is `(batch_id, source_identifier,
    content_hash)`, and every collection cycle opens a new batch. A source that
    returns an unchanged response therefore stores a byte-identical body again
    each cycle under a fresh `batch_id`, and no constraint notices.

    Whether that is material is a question about production, not about the
    schema, so this measures it rather than assuming. Redundant bytes are the
    copies past the first within each `(source, content_hash)` group: identical
    content from two different sources is two independent observations and is
    not counted as redundant.

    This reads every retained body and is far more expensive than
    `database_storage_census`, which answers from catalog statistics. Run it to
    diagnose, not on a schedule.
    """

    configured = ensure_database_ready(settings)
    moment = _now(now)
    with connection_pool(configured).connection() as connection:
        rows = connection.execute(
            """
            WITH bodies AS (
                SELECT source,
                       content_hash,
                       pg_column_size(payload_json) AS body_bytes
                FROM raw.source_payloads
                WHERE NOT jsonb_exists(payload_json, %s)
            ), grouped AS (
                SELECT source,
                       content_hash,
                       COUNT(*) AS copies,
                       MIN(body_bytes) AS body_bytes
                FROM bodies
                GROUP BY source, content_hash
            )
            SELECT source,
                   SUM(copies) AS retained_rows,
                   COUNT(*) AS distinct_bodies,
                   SUM(copies - 1) AS redundant_rows,
                   SUM((copies - 1) * body_bytes) AS redundant_bytes,
                   SUM(copies * body_bytes) AS body_bytes,
                   MAX(copies) AS most_copies_of_one_body
            FROM grouped
            GROUP BY source
            ORDER BY SUM((copies - 1) * body_bytes) DESC
            """,
            (TOMBSTONE_KEY,),
        ).fetchall()

    by_source = [
        {
            "source": str(row["source"]),
            "retained_rows": int(row["retained_rows"] or 0),
            "distinct_bodies": int(row["distinct_bodies"] or 0),
            "redundant_rows": int(row["redundant_rows"] or 0),
            "redundant_bytes": int(row["redundant_bytes"] or 0),
            "body_bytes": int(row["body_bytes"] or 0),
            "most_copies_of_one_body": int(row["most_copies_of_one_body"] or 0),
        }
        for row in rows
    ]
    body_bytes = sum(entry["body_bytes"] for entry in by_source)
    redundant_bytes = sum(entry["redundant_bytes"] for entry in by_source)
    return {
        "evaluated_at": moment.isoformat(),
        "retained_rows": sum(entry["retained_rows"] for entry in by_source),
        "distinct_bodies": sum(entry["distinct_bodies"] for entry in by_source),
        "redundant_rows": sum(entry["redundant_rows"] for entry in by_source),
        "redundant_bytes": redundant_bytes,
        "body_bytes": body_bytes,
        # What share of retained body bytes a perfect de-duplication would free.
        # Stated as a plain ratio so a caller can decide whether it is worth
        # building; it is a measurement of storage, not a plan to change any row.
        "redundant_share": (
            format(redundant_bytes / body_bytes, ".4f") if body_bytes else None
        ),
        "by_source": by_source,
    }


def volume_capacity_bytes() -> int:
    """The volume ceiling this database is running against, in bytes."""

    raw = os.environ.get("DATABASE_VOLUME_CAPACITY_BYTES")
    if raw is None or not str(raw).strip():
        return DEFAULT_VOLUME_CAPACITY_BYTES
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_VOLUME_CAPACITY_BYTES
    return parsed if parsed > 0 else DEFAULT_VOLUME_CAPACITY_BYTES


def volume_usage_bytes(connection: Any) -> dict[str, Any]:
    """What is actually on the volume, not just what is in this database.

    `pg_database_size(current_database())` is the obvious measurement and the
    wrong one. It excludes every other database in the cluster and all of
    `pg_wal` -- and `pg_wal` is what actually ran the volume out of space on
    2026-09-01: the PANIC names `pg_wal/xlogtemp.16755`. Measured on an idle
    development cluster, the current database was 13 MB of the 191 MB the
    volume was really holding, and WAL alone was 134 MB of it. An alarm reading
    only the first number would have reported 0.3% of a 5 GB ceiling while the
    volume was already 4% gone, and would have stayed quiet through the outage
    it exists to prevent.

    WAL is read through `pg_ls_waldir()`, which needs superuser or `pg_monitor`.
    Where that is refused the probe degrades to the cluster total and says so in
    `wal_measured`, rather than failing the status build outright.
    """

    row = connection.execute(
        """
        SELECT pg_database_size(current_database()) AS database_bytes,
               (SELECT COALESCE(sum(pg_database_size(datname)), 0)::bigint
                  FROM pg_database) AS cluster_bytes
        """
    ).fetchone()
    database_bytes = int(row["database_bytes"] or 0)
    cluster_bytes = int(row["cluster_bytes"] or 0)

    # A savepoint, because a permission failure would otherwise poison the
    # surrounding transaction and take the whole status build down with it.
    wal_bytes = 0
    wal_measured = False
    try:
        connection.execute("SAVEPOINT hawknetic_wal_probe")
        wal_row = connection.execute(
            "SELECT COALESCE(sum(size), 0)::bigint AS bytes FROM pg_ls_waldir()"
        ).fetchone()
        wal_bytes = int(wal_row["bytes"] or 0)
        wal_measured = True
        connection.execute("RELEASE SAVEPOINT hawknetic_wal_probe")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT hawknetic_wal_probe")

    return {
        "database_bytes": database_bytes,
        "cluster_bytes": cluster_bytes,
        "wal_bytes": wal_bytes,
        "wal_measured": wal_measured,
        "volume_bytes": cluster_bytes + wal_bytes,
    }


def database_capacity_state(
    volume_bytes: int,
    *,
    capacity_bytes: int | None = None,
) -> dict[str, Any]:
    """How close the volume is to full, and whether to shout.

    Pass the number from `volume_usage_bytes()["volume_bytes"]` -- everything on
    the volume, not `pg_database_size` of one database. Retention already
    reported a byte count every cycle and the number rose steadily to the
    ceiling without anything ever escalating it. On 2026-09-01 PostgreSQL ran
    out of space mid-write, PANICked, and then could not finish WAL recovery to
    restart -- a full volume is unrecoverable in place, so the only useful alarm
    is one that fires while there is still room to act. Warning at 75% and
    critical at 90% leaves roughly four and two days of headroom respectively at
    the growth rate production measured.
    """

    capacity = capacity_bytes if capacity_bytes and capacity_bytes > 0 else volume_capacity_bytes()
    used = max(0, int(volume_bytes))
    ratio = used / capacity if capacity else 0.0
    if ratio >= CAPACITY_CRITICAL_RATIO:
        state = "critical"
    elif ratio >= CAPACITY_WARNING_RATIO:
        state = "warning"
    else:
        state = "ok"
    return {
        "volume_bytes": used,
        "capacity_bytes": capacity,
        "headroom_bytes": max(0, capacity - used),
        "used_ratio": round(ratio, 4),
        "state": state,
    }


def database_storage_census(
    *,
    settings: DatabaseSettings | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Where the database's space actually is, largest relation first.

    Retention only helps if the mass is in raw payload bodies. A census says
    whether it is, instead of assuming -- production reported nothing eligible at
    a thirty-day window while still growing, which only a measurement explains.
    """
    configured = ensure_database_ready(settings)
    bounded_limit = max(1, min(int(limit), 100))
    with connection_pool(configured).connection() as connection:
        usage = volume_usage_bytes(connection)
        relations = connection.execute(
            """
            SELECT n.nspname AS schema_name,
                   c.relname AS table_name,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   pg_relation_size(c.oid) AS heap_bytes,
                   pg_indexes_size(c.oid) AS index_bytes,
                   COALESCE(s.n_live_tup, 0) AS live_rows
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables AS s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT %s
            """,
            (bounded_limit,),
        ).fetchall()
    return {
        "database_bytes": usage["database_bytes"],
        # The volume is what has a ceiling, so it is what the alarm compares
        # against. `database_bytes` stays for the relation breakdown below,
        # which is scoped to this database by definition.
        "cluster_bytes": usage["cluster_bytes"],
        "wal_bytes": usage["wal_bytes"],
        "wal_measured": usage["wal_measured"],
        "volume_bytes": usage["volume_bytes"],
        "largest_relations": [
            {
                "relation": f"{row['schema_name']}.{row['table_name']}",
                "total_bytes": int(row["total_bytes"]),
                "heap_bytes": int(row["heap_bytes"]),
                "index_bytes": int(row["index_bytes"]),
                "live_rows": int(row["live_rows"]),
            }
            for row in relations
        ],
    }


def _megabytes(value: object) -> str:
    try:
        return f"{float(value) / (1024 * 1024):.1f} MB"
    except (TypeError, ValueError):
        return "n/a"


def render_storage_report(report: dict[str, Any]) -> str:
    lines = [
        "Raw Source Payload Storage",
        f"Rows: {report.get('total_rows')} ({report.get('tombstoned_rows')} already pruned)",
        f"Payload bodies: {_megabytes(report.get('total_body_bytes'))}",
        f"Oldest / newest: {report.get('oldest_received_at')} / {report.get('newest_received_at')}",
        "",
        f"Older than {report.get('older_than_days')} days:",
        f"  Rows: {report.get('prunable_rows')}",
        f"  Reclaimable: {_megabytes(report.get('prunable_body_bytes'))}",
        "",
        "By source:",
    ]
    for entry in report.get("by_source") or []:
        lines.append(f"  {entry['source']}: {entry['rows']} rows, {_megabytes(entry['body_bytes'])}")
    return "\n".join(lines)
