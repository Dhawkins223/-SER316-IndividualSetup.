#!/usr/bin/env python3
"""Capture and compare PostgreSQL parity snapshots.

Two jobs, deliberately kept in one file so the thing that captures a snapshot
and the thing that compares two of them can never drift apart:

    --source URL --out FILE     read a database, write a JSON snapshot
    --compare BEFORE AFTER      diff two snapshots, exit non-zero on mismatch

The snapshot is what a migration has to preserve: applied migration versions,
the table inventory, exact row counts, and sequence positions. Everything is
read through ordinary catalog queries in a read-only transaction.

Row counts come from `count(*)`, not `pg_stat_user_tables.n_live_tup`. The
statistics view is an estimate refreshed by autovacuum and is routinely wrong
by thousands of rows on a freshly restored database -- which is exactly when
this script runs, and exactly the discrepancy it would have to explain away.
Counting is slower and is the only thing that actually settles the question.

Credentials are never printed. The connection URL is accepted on the command
line or, preferably, from the environment, and only ever echoed with its
userinfo stripped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    print("error: psycopg is required (pip install 'psycopg[binary]')", file=sys.stderr)
    raise SystemExit(2) from None

SNAPSHOT_VERSION = 1

# Schemas owned by the server, not by this application. Counting them would
# make every snapshot depend on the server's own catalog size and produce
# differences that mean nothing.
EXCLUDED_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def redact(url: str) -> str:
    """Return `url` with any username/password removed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _fetch_migration_state(conn: psycopg.Connection) -> dict[str, Any]:
    """Find the applied-migration record, whatever this project calls it.

    The table name is not fixed across projects, and guessing wrong is worse
    than reporting honestly that it was not found: a parity check that
    silently omits migration state would pass a database that is behind.
    """
    candidates = (
        ("public", "schema_migrations"),
        ("ops", "schema_migrations"),
        ("public", "migrations"),
        ("ops", "migrations"),
    )
    with conn.cursor() as cur:
        for schema, table in candidates:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            if cur.fetchone() is None:
                continue

            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            columns = {row[0] for row in cur.fetchall()}
            version_column = next(
                (c for c in ("version", "id", "name", "filename") if c in columns),
                None,
            )
            if version_column is None:
                continue

            cur.execute(
                f'SELECT "{version_column}"::text FROM "{schema}"."{table}" '
                f'ORDER BY 1'
            )
            applied = [row[0] for row in cur.fetchall()]
            return {
                "found": True,
                "table": f"{schema}.{table}",
                "column": version_column,
                "count": len(applied),
                "latest": applied[-1] if applied else None,
                "applied": applied,
            }

    return {"found": False, "table": None, "column": None, "count": 0, "latest": None, "applied": []}


def _fetch_tables(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema <> ALL(%s)
            ORDER BY table_schema, table_name
            """,
            (list(EXCLUDED_SCHEMAS),),
        )
        names = cur.fetchall()

        tables: list[dict[str, Any]] = []
        for schema, table in names:
            cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
            row = cur.fetchone()
            count = int(row[0]) if row else 0

            cur.execute(
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            row = cur.fetchone()
            columns = int(row[0]) if row else 0

            tables.append(
                {
                    "schema": schema,
                    "name": table,
                    "qualified": f"{schema}.{table}",
                    "rows": count,
                    "columns": columns,
                }
            )
    return tables


def _fetch_sequences(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Record each sequence's position.

    A restore that rebuilds rows but leaves sequences at 1 looks perfect on
    row counts and then collides on the first insert. This is the check that
    catches it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, sequencename
            FROM pg_sequences
            WHERE schemaname <> ALL(%s)
            ORDER BY schemaname, sequencename
            """,
            (list(EXCLUDED_SCHEMAS),),
        )
        names = cur.fetchall()

        sequences: list[dict[str, Any]] = []
        for schema, sequence in names:
            # last_value is NULL until the sequence is first used; that is a
            # real, reportable state and is preserved as None rather than 0.
            cur.execute(f'SELECT last_value FROM "{schema}"."{sequence}"')
            row = cur.fetchone()
            value = row[0] if row else None
            sequences.append(
                {
                    "schema": schema,
                    "name": sequence,
                    "qualified": f"{schema}.{sequence}",
                    "last_value": int(value) if value is not None else None,
                }
            )
    return sequences


def capture(url: str) -> dict[str, Any]:
    with psycopg.connect(url, connect_timeout=30) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database()")
            row = cur.fetchone()
            version, database = (row[0], row[1]) if row else ("unknown", "unknown")

        migrations = _fetch_migration_state(conn)
        tables = _fetch_tables(conn)
        sequences = _fetch_sequences(conn)

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": redact(url),
        "database": database,
        "server_version": version,
        "migrations": migrations,
        "tables": tables,
        "sequences": sequences,
        "totals": {
            "tables": len(tables),
            "rows": sum(t["rows"] for t in tables),
            "sequences": len(sequences),
        },
    }


def _index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["qualified"]: item for item in items}


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one entry per discrepancy. Empty list means parity."""
    findings: list[dict[str, Any]] = []

    b_mig, a_mig = before["migrations"], after["migrations"]
    if b_mig.get("latest") != a_mig.get("latest"):
        findings.append(
            {
                "kind": "migration_version",
                "detail": "applied migration head differs",
                "before": b_mig.get("latest"),
                "after": a_mig.get("latest"),
            }
        )
    if b_mig.get("count") != a_mig.get("count"):
        findings.append(
            {
                "kind": "migration_count",
                "detail": "number of applied migrations differs",
                "before": b_mig.get("count"),
                "after": a_mig.get("count"),
            }
        )

    b_tables, a_tables = _index(before["tables"]), _index(after["tables"])
    for name in sorted(set(b_tables) - set(a_tables)):
        findings.append(
            {"kind": "table_missing", "detail": f"{name} absent from target",
             "before": b_tables[name]["rows"], "after": None}
        )
    for name in sorted(set(a_tables) - set(b_tables)):
        findings.append(
            {"kind": "table_unexpected", "detail": f"{name} present only in target",
             "before": None, "after": a_tables[name]["rows"]}
        )
    for name in sorted(set(b_tables) & set(a_tables)):
        b_row, a_row = b_tables[name], a_tables[name]
        if b_row["rows"] != a_row["rows"]:
            findings.append(
                {"kind": "row_count", "detail": f"{name} row count differs",
                 "before": b_row["rows"], "after": a_row["rows"]}
            )
        if b_row["columns"] != a_row["columns"]:
            findings.append(
                {"kind": "column_count", "detail": f"{name} column count differs",
                 "before": b_row["columns"], "after": a_row["columns"]}
            )

    b_seq, a_seq = _index(before["sequences"]), _index(after["sequences"])
    for name in sorted(set(b_seq) - set(a_seq)):
        findings.append(
            {"kind": "sequence_missing", "detail": f"{name} absent from target",
             "before": b_seq[name]["last_value"], "after": None}
        )
    for name in sorted(set(b_seq) & set(a_seq)):
        # A target sequence ahead of the source is safe (no collision); behind
        # is not. Both are reported, because an unexplained difference is a
        # difference -- but the detail says which way it leans.
        b_val, a_val = b_seq[name]["last_value"], a_seq[name]["last_value"]
        if b_val != a_val:
            direction = "behind source" if (a_val or 0) < (b_val or 0) else "ahead of source"
            findings.append(
                {"kind": "sequence_value", "detail": f"{name} is {direction}",
                 "before": b_val, "after": a_val}
            )

    return findings


def _print_summary(snapshot: dict[str, Any]) -> None:
    totals = snapshot["totals"]
    mig = snapshot["migrations"]
    print(f"  endpoint        {snapshot['endpoint']}")
    print(f"  database        {snapshot['database']}")
    print(f"  tables          {totals['tables']}")
    print(f"  rows            {totals['rows']}")
    print(f"  sequences       {totals['sequences']}")
    if mig["found"]:
        print(f"  migrations      {mig['count']} applied, head={mig['latest']} ({mig['table']})")
    else:
        print("  migrations      NOT FOUND -- no recognised migration table")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture or compare PostgreSQL parity snapshots.",
    )
    parser.add_argument(
        "--source",
        help="Connection URL to snapshot. Defaults to $PARITY_DATABASE_URL, "
        "then $DATABASE_URL.",
    )
    parser.add_argument("--out", help="Write the snapshot JSON to this path.")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="Compare two snapshot files. Exits 1 if they differ.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        with open(args.compare[0], encoding="utf-8") as fh:
            before = json.load(fh)
        with open(args.compare[1], encoding="utf-8") as fh:
            after = json.load(fh)

        findings = compare(before, after)
        print(f"source: {before['endpoint']}  ({before['totals']['rows']} rows)")
        print(f"target: {after['endpoint']}  ({after['totals']['rows']} rows)")

        if not findings:
            print("\nPARITY OK -- no discrepancies.")
            return 0

        print(f"\nPARITY FAILED -- {len(findings)} discrepancies:\n")
        for item in findings:
            print(f"  [{item['kind']}] {item['detail']}")
            print(f"      source={item['before']!r}  target={item['after']!r}")
        print("\nCutover is blocked while any discrepancy is unexplained.")
        return 1

    url = args.source or os.environ.get("PARITY_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("no database URL: pass --source or set PARITY_DATABASE_URL")

    snapshot = capture(url)
    print("captured parity snapshot:")
    _print_summary(snapshot)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\nwrote {args.out}")
    else:
        print()
        json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
