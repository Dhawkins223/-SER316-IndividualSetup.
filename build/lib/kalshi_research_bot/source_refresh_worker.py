"""Claim cloud refresh requests and update PostgreSQL-backed source data."""

from __future__ import annotations

import argparse
import os
import socket
import time
from typing import Any, Callable, Mapping

from .database import close_connection_pools
from .source_catalog import collect_kalshi_catalog, collect_polymarket_catalog
from .source_data_store import SourceDataStore
from .worker_services import build_service_operation


RefreshOperation = Callable[[], Mapping[str, Any]]


def operations_for_request(request_id: str) -> dict[str, RefreshOperation]:
    run_id = f"source_refresh_{request_id}"
    return {
        "polymarket": collect_polymarket_catalog,
        "kalshi_reference": collect_kalshi_catalog,
        "kalshi_current": build_service_operation(
            "kalshi-market-ingestion",
            kalshi_run_id=run_id,
            crypto_run_id=run_id,
            sports_run_id=run_id,
        ),
        "sports_current": build_service_operation(
            "sports-research",
            kalshi_run_id=run_id,
            crypto_run_id=run_id,
            sports_run_id=run_id,
        ),
    }


def process_refresh_request(
    request: Mapping[str, Any],
    *,
    store: SourceDataStore,
    operations: Mapping[str, RefreshOperation] | None = None,
) -> dict[str, Any]:
    request_id = str(request["request_id"])
    available = dict(operations or operations_for_request(request_id))
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for source in request.get("sources") or []:
        operation = available.get(str(source))
        if operation is None:
            errors[str(source)] = "operation_unavailable"
            continue
        try:
            results[str(source)] = dict(operation())
        except Exception as exc:  # noqa: BLE001 - every source outcome is retained
            errors[str(source)] = f"{type(exc).__name__}:{str(exc)[:300]}"
    outcome = {"sources": results, "errors": errors}
    if errors:
        error_code = ";".join(
            f"{source}={error}" for source, error in sorted(errors.items())
        )[:2000]
        return store.record_refresh_failure(
            request_id,
            result=outcome,
            error_code=error_code,
        )
    return store.finish_refresh(request_id, status="completed", result=outcome)


def run_refresh_cycle(
    *,
    store: SourceDataStore | None = None,
    worker_id: str | None = None,
    maximum_requests: int = 5,
    plan_pregame: bool = True,
) -> dict[str, Any]:
    source_store = store or SourceDataStore()
    identity = worker_id or f"source-refresh:{socket.gethostname()}"
    scheduled = source_store.schedule_pregame_refreshes() if plan_pregame else []
    completed: list[dict[str, Any]] = []
    for _ in range(max(1, min(int(maximum_requests), 50))):
        request = source_store.claim_refresh(worker_id=identity)
        if request is None:
            break
        completed.append(process_refresh_request(request, store=source_store))
    return {
        "scheduled": len(scheduled),
        "processed": len(completed),
        "completed": sum(1 for row in completed if row.get("status") == "completed"),
        "retried": sum(1 for row in completed if row.get("status") == "queued"),
        "failed": sum(1 for row in completed if row.get("status") == "failed"),
        "request_ids": [row.get("request_id") for row in completed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep polling; omit for a Railway cron run that exits",
    )
    parser.add_argument("--skip-pregame-plan", action="store_true")
    parser.add_argument(
        "--maximum-requests",
        type=int,
        default=int(os.environ.get("SOURCE_REFRESH_MAX_REQUESTS", "5")),
    )
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        while True:
            result = run_refresh_cycle(
                maximum_requests=args.maximum_requests,
                plan_pregame=not args.skip_pregame_plan,
            )
            print(result)
            exit_code = 1 if result["failed"] or result["retried"] else 0
            if not args.watch:
                return exit_code
            time.sleep(max(5, int(os.environ.get("SOURCE_REFRESH_POLL_SECONDS", "30"))))
    finally:
        close_connection_pools()


if __name__ == "__main__":
    raise SystemExit(main())
