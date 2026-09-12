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


def _content_hash(cur: psycopg.Cursor, schema: str, table: str) -> str | None:
    """Return an order-independent checksum over every row of one table.

    Row counts, sequences and schema shape can all match while the *values*
    differ -- a restore that silently mangled an encoding or a numeric would
    pass every other check here. This is the check that looks at the data.

    Each row is rendered to text, hashed, and two 32-bit slices of that hash are
    summed. Summing rather than concatenating makes the result independent of
    row order, which matters because a restored table is rarely in the source's
    physical order, and keeps memory constant: `string_agg` over a table with
    millions of rows would materialise the whole list.

    The session settings in `capture` are what make `t::text` comparable across
    two servers; without them a different DateStyle or float precision would
    produce a different hash for identical data.
    """
    cur.execute(
        f'''
        SELECT
            coalesce(sum(('x' || substr(h, 1, 8))::bit(32)::bigint), 0),
            coalesce(sum(('x' || substr(h, 9, 8))::bit(32)::bigint), 0)
        FROM (SELECT md5(t.*::text) AS h FROM "{schema}"."{table}" t) s
        '''
    )
    row = cur.fetchone()
    if row is None:
        return None
    return f"{int(row[0]):x}:{int(row[1]):x}"


def _fetch_tables(conn: psycopg.Connection, content_hash: bool = False) -> list[dict[str, Any]]:
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

            entry: dict[str, Any] = {
                "schema": schema,
                "name": table,
                "qualified": f"{schema}.{table}",
                "rows": count,
                "columns": columns,
            }
            if content_hash:
                entry["content_hash"] = _content_hash(cur, schema, table)
            tables.append(entry)
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


def capture(url: str, content_hash: bool = False) -> dict[str, Any]:
    with psycopg.connect(url, connect_timeout=30) as conn:
        conn.read_only = True
        # REPEATABLE READ, not the default READ COMMITTED. Counting 74 tables
        # takes many statements, and under READ COMMITTED each one sees a
        # different committed state -- so a snapshot taken while anything is
        # writing would record counts that never coexisted, and the diff
        # against it would report drift that is really just skew. This makes
        # the whole capture one consistent view.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

        with conn.cursor() as cur:
            # Pin every setting that affects how a row renders as text. Without
            # these, two servers with different DateStyle, TimeZone or float
            # precision produce different content hashes for identical data --
            # a false discrepancy that would block a correct cutover.
            cur.execute("SET extra_float_digits = 3")
            cur.execute("SET DateStyle = 'ISO, YMD'")
            cur.execute("SET TimeZone = 'UTC'")
            cur.execute("SET intervalstyle = 'iso_8601'")
            cur.execute("SET bytea_output = 'hex'")

            cur.execute("SELECT version(), current_database(), inet_server_addr(), inet_server_port()")
            row = cur.fetchone()
            if row:
                version, database, server_addr, server_port = row
            else:
                version, database, server_addr, server_port = ("unknown", "unknown", None, None)

        migrations = _fetch_migration_state(conn)
        tables = _fetch_tables(conn, content_hash=content_hash)
        sequences = _fetch_sequences(conn)

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": redact(url),
        "database": database,
        "server_version": version,
        # Identity of the server this came from, so a comparison of a database
        # against itself can be detected and rejected rather than reported as
        # perfect parity.
        "server_identity": {
            "address": str(server_addr) if server_addr is not None else None,
            "port": int(server_port) if server_port is not None else None,
            "database": database,
        },
        "content_hash": content_hash,
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

    # Comparing a database against itself passes every check trivially and
    # proves nothing about a migration. Catch it rather than reporting a
    # perfect score for a test that never happened.
    b_id, a_id = before.get("server_identity"), after.get("server_identity")
    if b_id and a_id and b_id == a_id and b_id.get("database") is not None:
        findings.append(
            {
                "kind": "same_database",
                "detail": (
                    "source and target are the same server and database "
                    f"({b_id.get('address')}:{b_id.get('port')}/{b_id.get('database')}); "
                    "this comparison proves nothing"
                ),
                "before": b_id,
                "after": a_id,
            }
        )

    # A content-hash comparison is only meaningful if both sides computed one.
    # Silently treating "not computed" as "matches" is the failure mode this
    # check exists to prevent.
    if before.get("content_hash") != after.get("content_hash"):
        findings.append(
            {
                "kind": "content_hash_asymmetric",
                "detail": "one snapshot has content hashes and the other does not; recapture both with --content-hash",
                "before": before.get("content_hash"),
                "after": after.get("content_hash"),
            }
        )

    b_mig, a_mig = before["migrations"], after["migrations"]

    # A snapshot with no recognised migration ledger cannot be compared, and
    # treating that as parity would pass a database whose migration state is
    # simply unknown. Fail instead of quietly skipping the check.
    for label, snapshot in (("source", b_mig), ("target", a_mig)):
        if not snapshot.get("found"):
            findings.append(
                {
                    "kind": "migration_ledger_missing",
                    "detail": f"{label} has no recognised migration table; migration state cannot be verified",
                    "before": b_mig.get("table"),
                    "after": a_mig.get("table"),
                }
            )

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

    # Head and count alone do not pin the ledger down: a target missing 0009
    # but carrying an extra 0017 has the same head and the same count as a
    # source with 0009 and no 0017. Compare the whole applied set, and name the
    # specific versions rather than reporting that two lists differ.
    b_applied, a_applied = set(b_mig.get("applied") or []), set(a_mig.get("applied") or [])
    if b_applied != a_applied:
        missing = sorted(b_applied - a_applied)
        extra = sorted(a_applied - b_applied)
        if missing:
            findings.append(
                {
                    "kind": "migration_missing",
                    "detail": "target has not applied migrations present in source: " + ", ".join(missing),
                    "before": missing,
                    "after": None,
                }
            )
        if extra:
            findings.append(
                {
                    "kind": "migration_unexpected",
                    "detail": "target has applied migrations absent from source: " + ", ".join(extra),
                    "before": None,
                    "after": extra,
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
        # The only check that looks at row values. Matching counts with a
        # differing hash means the same number of rows carrying different data.
        b_hash, a_hash = b_row.get("content_hash"), a_row.get("content_hash")
        if b_hash is not None and a_hash is not None and b_hash != a_hash:
            findings.append(
                {"kind": "content_hash", "detail": f"{name} row contents differ",
                 "before": b_hash, "after": a_hash}
            )

    b_seq, a_seq = _index(before["sequences"]), _index(after["sequences"])
    for name in sorted(set(b_seq) - set(a_seq)):
        findings.append(
            {"kind": "sequence_missing", "detail": f"{name} absent from target",
             "before": b_seq[name]["last_value"], "after": None}
        )
    # Checked in both directions, as tables are. A target-only sequence is
    # schema drift -- usually a leftover from an earlier restore -- and
    # omitting this check let the gate pass on a target that was not a faithful
    # copy.
    for name in sorted(set(a_seq) - set(b_seq)):
        findings.append(
            {"kind": "sequence_unexpected", "detail": f"{name} present only in target",
             "before": None, "after": a_seq[name]["last_value"]}
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
    if snapshot.get("content_hash"):
        print("  content hashes  computed for every table")
    else:
        print("  content hashes  NOT computed (pass --content-hash to compare row values)")


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
        "--content-hash",
        action="store_true",
        help="Also checksum every row of every table. This is the only check "
        "that compares row values rather than shape and counts, so a cutover "
        "gate should use it. It reads every table in full, so it is slower.",
    )
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

    snapshot = capture(url, content_hash=args.content_hash)
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
