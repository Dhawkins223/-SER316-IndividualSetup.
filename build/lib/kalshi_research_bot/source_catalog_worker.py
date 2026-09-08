"""Standalone Railway worker entry point for source-backed catalog collection."""

from __future__ import annotations

import argparse
import os
import socket

from .database import close_connection_pools
from .source_catalog import collect_kalshi_catalog, collect_polymarket_catalog
from .worker_runtime import WorkerSpec, run_worker_forever, run_worker_once, start_worker_health_server


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def worker_spec(source: str) -> WorkerSpec:
    if source == "polymarket":
        return WorkerSpec(
            name="polymarket-sports-ingestion",
            asset_class="sports_external_market",
            cadence_seconds=_positive_int("POLYMARKET_COLLECTION_CADENCE_SECONDS", 3600),
            expect_records=True,
        )
    if source == "kalshi":
        return WorkerSpec(
            name="kalshi-reference-ingestion",
            asset_class="sports_reference_data",
            cadence_seconds=_positive_int("KALSHI_REFERENCE_CADENCE_SECONDS", 21600),
            expect_records=True,
        )
    raise ValueError(f"unknown_source_catalog_worker:{source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("polymarket", "kalshi"), required=True)
    parser.add_argument("--once", action="store_true", help="run one collection cycle and exit")
    args = parser.parse_args(argv)
    spec = worker_spec(args.source)
    operation = collect_polymarket_catalog if args.source == "polymarket" else collect_kalshi_catalog
    run_id = str(os.environ.get("RAILWAY_REPLICA_ID") or f"{spec.name}:{socket.gethostname()}")
    if args.once:
        try:
            result = run_worker_once(spec, operation, run_id=run_id)
            return 0 if result.get("status") in {"success", "skipped_duplicate"} else 1
        finally:
            close_connection_pools()
    port = _positive_int("PORT", 8080)
    server = start_worker_health_server(spec.name, port)
    try:
        return run_worker_forever(spec, operation, run_id=run_id)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
