from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .agents import ScrapeBot
from .config import repo_path
from .crypto_research import crypto_cycle
from .evaluation.kalshi_decomposition import (
    build_kalshi_return_decomposition,
    default_kalshi_return_decomposition_path,
    write_kalshi_return_decomposition,
)
from .evaluation.paper_live import (
    build_daily_report,
    build_stage3b_audit_report,
    default_daily_report_path,
    default_stage3b_audit_path,
    fetch_official_kalshi_settlements,
    import_settlements,
    write_daily_report,
    write_stage3b_audit_report,
)
from .sports_research import sports_cycle
from .business_store import create_store, finish_report_refresh, start_report_refresh
from .monitoring import build_internal_status, send_monitoring_alerts
from .kalshi_ingestion import persist_kalshi_snapshot
from .retention import (
    DEFAULT_RETENTION_DAYS,
    MINIMUM_RETENTION_DAYS,
    database_capacity_state,
    database_storage_census,
    prune_source_payload_bodies,
    source_payload_duplication_report,
)
from .research_modeling import refresh_market_consensus_baseline
from .today import write_today_payload
from .worker_runtime import NonRetryableWorkerError, WorkerSpec, current_worker_idempotency_key


SERVICE_SPECS: dict[str, WorkerSpec] = {
    "kalshi-market-ingestion": WorkerSpec(
        name="kalshi-market-ingestion",
        asset_class="kalshi",
        cadence_seconds=300,
        expect_records=True,
    ),
    "external-source-ingestion": WorkerSpec(
        name="external-source-ingestion",
        asset_class="external",
        cadence_seconds=900,
    ),
    "crypto-research": WorkerSpec(
        name="crypto-research",
        asset_class="crypto",
        cadence_seconds=900,
        expect_records=True,
    ),
    "sports-research": WorkerSpec(
        name="sports-research",
        asset_class="sports",
        cadence_seconds=3600,
        expect_records=True,
    ),
    "research-model-refresh": WorkerSpec(
        name="research-model-refresh",
        asset_class="kalshi",
        cadence_seconds=3600,
        expect_records=True,
    ),
    "settlement-worker": WorkerSpec(
        name="settlement-worker",
        asset_class="kalshi",
        cadence_seconds=3600,
    ),
    "reporting-evaluation": WorkerSpec(
        name="reporting-evaluation",
        asset_class="all",
        cadence_seconds=21600,
    ),
    # Storage is a collection concern, not an occasional chore: raw payload bodies
    # accumulate every cycle of every collector, and a volume that fills stops all
    # of them. Hourly rather than six-hourly: a bounded prune is cheap, and the
    # worker guarding the volume should not leave a six-hour blind window after
    # every deploy, which is exactly how long a restart costs when the cadence
    # bucket has already been claimed.
    "raw-retention": WorkerSpec(
        name="raw-retention",
        asset_class="all",
        cadence_seconds=3600,
    ),
}


SERVICE_COMMANDS = {
    "web": "HAWKNETIC_SERVICE=web python -m kalshi_research_bot service-start",
    **{
        service: f"HAWKNETIC_SERVICE={service} python -m kalshi_research_bot service-start"
        for service in SERVICE_SPECS
    },
}


def _kalshi_ingestion_operation(output_path: str | Path) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        payload = write_today_payload(output_path)
        if payload.get("refresh_error"):
            raise RuntimeError(str(payload.get("refresh_error")))
        persistence = persist_kalshi_snapshot(
            payload,
            worker_name="kalshi-market-ingestion",
            idempotency_key=current_worker_idempotency_key(),
        )
        count = int(persistence["records_accepted"]) + int(persistence["records_duplicated"])
        return {
            "records_processed": count,
            "records_received": int(persistence["records_received"]),
            "records_accepted": int(persistence["records_accepted"]),
            "records_rejected": int(persistence["records_rejected"]),
            "records_duplicated": int(persistence["records_duplicated"]),
            "ingestion_batch_id": persistence["batch_id"],
            "no_material_change": not persistence["created"],
            "data_fresh_at": payload.get("generated_at"),
            "source_fresh_at": payload.get("generated_at"),
            "source_snapshot_hash": persistence["payload_hash"],
        }

    return operation


def _external_source_operation(config_path: str | Path) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        store = create_store()
        path = Path(config_path)
        if not path.exists():
            return {
                "records_processed": 0,
                "no_material_change": True,
                "state": "unconfigured_optional",
                "reason": "external_sources_config_missing",
            }
        records = ScrapeBot().collect(path)
        store.insert_source_records(records)
        return {"records_processed": len(records), "source_fresh_at": None}

    return operation


def _crypto_operation(run_id: str) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        result = crypto_cycle(run_id=run_id)
        logged = int(result["log_result"].get("logged_predictions") or 0)
        settled = int(result["settle_result"].get("rows_updated") or 0)
        rejected = int(result["log_result"].get("rejected_predictions") or 0)
        report = result["report"]
        source_status = report.get("source_status") or {}
        if not source_status.get("fresh", True) and logged == 0:
            raise NonRetryableWorkerError(str(source_status.get("reason") or "crypto_source_not_fresh"))
        return {
            "records_processed": logged + settled,
            "no_material_change": logged == 0 and settled == 0 and rejected == 0,
            "logged_predictions": logged,
            "settled_predictions": settled,
            "rejected_predictions": rejected,
            "pending_settlements": int(report.get("unresolved_predictions") or 0),
            "data_fresh_at": report.get("generated_at"),
            "source_fresh_at": report.get("latest_source_fetched_at"),
            "model_state": "baseline_only",
        }

    return operation


def _sports_operation(run_id: str) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        result = sports_cycle(run_id=run_id)
        logged = int(result["log_result"].get("logged_predictions") or 0)
        settled = int(result["settle_result"].get("rows_updated") or 0)
        rejected = int(result["log_result"].get("rejected_predictions") or 0)
        report = result["report"]
        if report.get("blockers") and logged == 0:
            raise NonRetryableWorkerError(str(report["blockers"][0]))
        clv = result.get("clv_result") or {}
        closing_lines = int(clv.get("rows_updated") or 0)
        return {
            "records_processed": logged + settled + closing_lines,
            "no_material_change": logged == 0 and settled == 0 and rejected == 0 and closing_lines == 0,
            "logged_predictions": logged,
            "settled_predictions": settled,
            "rejected_predictions": rejected,
            "closing_lines_recorded": closing_lines,
            "markets_closed": int(clv.get("markets_closed") or 0),
            "closing_line_error": clv.get("error_code"),
            "pending_settlements": int(report.get("unresolved_predictions") or 0),
            "data_fresh_at": report.get("generated_at"),
            "source_fresh_at": report.get("latest_source_fetched_at"),
            "model_state": "baseline_only",
        }

    return operation


def _settlement_operation(run_id: str) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        store = create_store()
        payload = fetch_official_kalshi_settlements(store, run_id=run_id)
        if not payload.get("outcomes") and payload.get("fetch_errors"):
            raise NonRetryableWorkerError("kalshi_settlement_source_failed")
        result = import_settlements(store, run_id=run_id, settlements_payload=payload)
        report = build_daily_report(store, run_id=run_id)
        return {
            "records_processed": int(result.get("rows_updated") or 0),
            "no_material_change": int(result.get("rows_updated") or 0) == 0 and not result.get("fetch_errors"),
            "pending_settlements": int(report.get("unresolved_predictions") or 0),
            "settlement_issue_counts": result.get("settlement_issue_counts") or {},
            "source_fresh_at": payload.get("fetched_at"),
            "markets_requested": int(payload.get("markets_requested") or 0),
            "markets_deferred": int(payload.get("markets_deferred") or 0),
        }

    return operation


def _research_model_operation(run_id: str) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        result = refresh_market_consensus_baseline(run_id=run_id)
        if result.get("source_freshness_state") != "fresh":
            raise NonRetryableWorkerError(str(result.get("reason") or "kalshi_source_not_fresh"))
        return result

    return operation


def _reporting_operation(run_id: str) -> Callable[[], Mapping[str, Any]]:
    def operation() -> Mapping[str, Any]:
        from .monitoring import utc_now_iso

        store = create_store()

        refresh_id = f"reporting:{run_id}"
        started_at = utc_now_iso()
        store.initialize()
        with store.connect() as connection:
            start_report_refresh(
                connection,
                refresh_id=refresh_id,
                report_name="kalshi_reporting_evaluation",
                data_cutoff_at=started_at,
                started_at=started_at,
            )
        try:
            daily = build_daily_report(store, run_id=run_id)
            stage3b = build_stage3b_audit_report(store, run_id=run_id)
            decomposition = build_kalshi_return_decomposition(store, run_id=run_id)
            write_daily_report(daily, default_daily_report_path(run_id))
            write_stage3b_audit_report(stage3b, default_stage3b_audit_path(run_id))
            write_kalshi_return_decomposition(decomposition, default_kalshi_return_decomposition_path(run_id))
            monitoring_status = build_internal_status(store.settings)
            alert_results = send_monitoring_alerts(
                monitoring_status,
                run_id=run_id,
                report_path=str(default_daily_report_path(run_id)),
            )
        except Exception as exc:
            with store.connect() as connection:
                finish_report_refresh(
                    connection,
                    refresh_id=refresh_id,
                    completed_at=utc_now_iso(),
                    status="failed",
                    error_code=type(exc).__name__,
                )
            raise
        with store.connect() as connection:
            finish_report_refresh(
                connection,
                refresh_id=refresh_id,
                completed_at=utc_now_iso(),
                status="completed",
                row_count=3,
            )
        return {
            "records_processed": 3,
            "pending_settlements": int(daily.get("unresolved_predictions") or 0),
            "model_state": "baseline_only",
            "reports": [
                str(default_daily_report_path(run_id)),
                str(default_stage3b_audit_path(run_id)),
                str(default_kalshi_return_decomposition_path(run_id)),
            ],
            "monitoring_alert_results": [result.get("status") for result in alert_results],
        }

    return operation


def _retention_operation() -> Callable[[], Mapping[str, Any]]:
    """Prune raw payload bodies past the configured window.

    Applies by default -- a retention worker that only ever reported would leave
    the volume filling -- but the window still refuses anything under the module's
    floor, and a source's newest payload is never touched.
    """

    def operation() -> Mapping[str, Any]:
        window_days = _bounded_int("RAW_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, MINIMUM_RETENTION_DAYS, 3650)
        batch_limit = _bounded_int("RAW_RETENTION_BATCH_LIMIT", 5000, 1, 100000)
        dry_run = _env_flag("RAW_RETENTION_DRY_RUN", default=False)
        result = prune_source_payload_bodies(
            older_than_days=window_days,
            limit=batch_limit,
            dry_run=dry_run,
        )
        pruned = int(result["pruned"])
        remaining = int(result["remaining_after_limit"])
        # Report where the space is, not just what was pruned. A pass that frees
        # nothing while the volume keeps growing is only explicable with a census.
        census = database_storage_census(limit=6)
        capacity = database_capacity_state(census["volume_bytes"])
        return {
            "records_processed": pruned,
            "database_bytes": census["database_bytes"],
            # The ratio, not just the byte count: "3.9 GB" needs a ceiling to
            # mean anything, and this worker is the one watching the volume.
            # Measured across the volume, not this database: WAL is what filled
            # it, and pruning rows does not touch WAL.
            "volume_bytes": census["volume_bytes"],
            "wal_bytes": census["wal_bytes"],
            "volume_capacity_bytes": capacity["capacity_bytes"],
            "volume_used_ratio": capacity["used_ratio"],
            "volume_state": capacity["state"],
            "largest_relations": [
                f"{row['relation']}={row['total_bytes']}" for row in census["largest_relations"]
            ],
            # A pass with nothing left to prune is the healthy steady state, not a
            # failure, and a dry run never claims to have changed anything.
            "no_material_change": pruned == 0,
            "dry_run": result["dry_run"],
            "retention_days": window_days,
            "retained_span_days": result["retained_span_days"],
            "oldest_received_at": result["oldest_received_at"],
            # False means the window is wider than the data's own age, so this
            # worker will keep pruning nothing until the table ages into it.
            "window_bites": result["window_bites"],
            "payload_bodies_pruned": pruned,
            "reclaimable_bytes": int(result["reclaimable_bytes"]),
            "still_eligible": remaining,
            "model_state": "not_applicable",
            **_duplication_metrics(),
        }

    return operation


def _duplication_metrics() -> Mapping[str, Any]:
    """Measure repeated payload bodies, only when explicitly asked to.

    A source returning an unchanged response stores the body again each cycle,
    because the table's uniqueness is per batch. Knowing whether that is a
    material share of the volume needs a reading of every retained body, which
    is far too expensive to repeat hourly -- so it stays off unless an operator
    turns it on to answer the question, and turns it back off afterwards.
    """

    if not _env_flag("RAW_RETENTION_DUPLICATION_CENSUS", default=False):
        return {}
    report = source_payload_duplication_report()
    return {
        "duplication_measured": True,
        "retained_bodies": report["retained_rows"],
        "distinct_bodies": report["distinct_bodies"],
        "redundant_rows": report["redundant_rows"],
        "redundant_bytes": report["redundant_bytes"],
        "redundant_share": report["redundant_share"],
        "duplication_by_source": [
            f"{row['source']}={row['redundant_bytes']}/{row['body_bytes']}"
            f" x{row['most_copies_of_one_body']}"
            for row in report["by_source"]
        ],
    }


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def build_service_operation(
    service: str,
    *,
    kalshi_run_id: str,
    crypto_run_id: str,
    sports_run_id: str,
) -> Callable[[], Mapping[str, Any]]:
    """Build a worker's operation without touching the database.

    Every store is constructed inside the operation, not here. Building eagerly
    meant a worker could not even start while its database was down -- the process
    exited and the deployment failed, which is the one situation the cycle-level
    backoff exists to survive. A worker must be able to start against a dead
    database, fail its cycle, and recover when the database returns.
    """
    if service == "kalshi-market-ingestion":
        return _kalshi_ingestion_operation(repo_path("data", "today_paper_view.json"))
    if service == "external-source-ingestion":
        config_path = os.environ.get("EXTERNAL_SOURCES_CONFIG", str(repo_path("config", "sources.json")))
        return _external_source_operation(config_path)
    if service == "crypto-research":
        return _crypto_operation(crypto_run_id)
    if service == "sports-research":
        return _sports_operation(sports_run_id)
    if service == "settlement-worker":
        return _settlement_operation(kalshi_run_id)
    if service == "research-model-refresh":
        return _research_model_operation(kalshi_run_id)
    if service == "reporting-evaluation":
        return _reporting_operation(kalshi_run_id)
    if service == "raw-retention":
        return _retention_operation()
    raise ValueError(f"unknown_worker_service:{service}")


def service_run_id(
    service: str,
    *,
    kalshi_run_id: str,
    crypto_run_id: str,
    sports_run_id: str,
) -> str:
    if service == "crypto-research":
        return crypto_run_id
    if service == "sports-research":
        return sports_run_id
    if service == "external-source-ingestion":
        return "external_sources"
    return kalshi_run_id
