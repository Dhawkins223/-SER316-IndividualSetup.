from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..config import repo_path
from ..connectors.http import HttpClient
from ..connectors.status import build_connectors_status, connector_status_report_lines
from ..database import as_decimal, json_default
from ..storage import PostgresStore
from .backtest import MIN_SETTLED_FOR_BUCKET, MIN_SETTLED_FOR_PERFORMANCE, UNRESOLVED_STATES, _normal_state, _outcome_settlement_state
from .logging import extract_prediction_logs_from_payload
from .quality import parse_timestamp


MODEL_VERSION_LOCK = {
    "primary_80": "market_implied_slip_v1",
    "leverage_75": "market_implied_slip_v1",
    "all_day_75_85": "market_implied_slip_v1",
    "research_edge": "research_edge_v1",
}

DEFAULT_STAGE3A_CONFIG = {
    "stage": "3A_private_live_paper",
    "performance_min_settled_predictions": MIN_SETTLED_FOR_PERFORMANCE,
    "roi_policy": "fee_excluded",
    "legacy_rows_policy": "exclude_rows_where_run_id_is_null",
    "invalid_prediction_policy": "reject_from_prediction_logs_and_store_rejection",
    "settlement_policy": "calculate_profit_loss_only_after_settlement",
    "max_payload_age_seconds": 1800,
}

KALSHI_PUBLIC_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKET_DETAIL_SETTLEMENT_SOURCE = "kalshi_public_market_detail"
ZERO_PROFIT_SETTLEMENT_STATES = {"push", "void", "cancelled"}
REPORTING_TIMEZONE = ZoneInfo("America/New_York")


def _jsonb(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _decimal(value: Any, *, default: Decimal = Decimal("0")) -> Decimal:
    return as_decimal(value, default=default) or default


def _round_decimal(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_EVEN)


def stable_json_hash(payload: Any) -> str:
    return __import__("hashlib").sha256(json.dumps(payload, sort_keys=True, default=json_default).encode("utf-8")).hexdigest()


def now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_report_date(value: str | None) -> str | None:
    if not value:
        return None
    clean = value.strip()
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:]}"
    return clean


def build_run_lock(
    *,
    run_id: str | None = None,
    started_at: str | None = None,
    config: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = started_at or now_timestamp()
    resolved_run_id = run_id or f"stage3a_{started[:10].replace('-', '')}_{started[11:19].replace(':', '')}"
    resolved_config = {**DEFAULT_STAGE3A_CONFIG, **(config or {})}
    resolved_models = {**MODEL_VERSION_LOCK, **(model_versions or {})}
    locked_payload = {
        "run_id": resolved_run_id,
        "started_at": started,
        "status": "active",
        "model_versions": resolved_models,
        "config": resolved_config,
    }
    locked_payload["config_hash"] = stable_json_hash(
        {
            "run_id": resolved_run_id,
            "started_at": started,
            "model_versions": resolved_models,
            "config": resolved_config,
        }
    )
    return locked_payload


def start_paper_test_run(
    store: PostgresStore,
    *,
    run_id: str | None = None,
    config: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
    lock_path: str | Path | None = None,
) -> dict[str, Any]:
    run = build_run_lock(run_id=run_id, config=config, model_versions=model_versions)
    run["created"] = store.create_paper_test_run(run)
    if lock_path:
        output = Path(lock_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(run, indent=2, sort_keys=True, default=json_default), encoding="utf-8")
    return run


def _rejection_from_log(run_id: str, log: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp": log.get("timestamp", ""),
        "event": log.get("event", ""),
        "event_id": log.get("event_id"),
        "market": log.get("market", ""),
        "market_id": log.get("market_id"),
        "side": log.get("side", ""),
        "strategy": log.get("strategy") or log.get("slip_name"),
        "validation_errors": log.get("validation_errors") or [],
        "raw_log": log,
    }


def _payload_age_errors(log: dict[str, Any], *, prediction_timestamp: str, max_payload_age_seconds: int) -> list[str]:
    fetched_at = log.get("api_fetched_at")
    fetched = parse_timestamp(fetched_at)
    predicted = parse_timestamp(prediction_timestamp)
    if fetched is None:
        return ["missing_api_fetched_at"]
    if predicted is None:
        return []
    age_seconds = (predicted.astimezone(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds()
    if age_seconds > max_payload_age_seconds:
        return ["stale_payload"]
    return []


def _stable_features_json(log: dict[str, Any]) -> str:
    return json.dumps(log.get("reason_features") or {}, sort_keys=True, default=json_default)


def _numbers_equal(first: Any, second: Any) -> bool:
    if first in {None, ""} and second in {None, ""}:
        return True
    first_decimal = as_decimal(first)
    second_decimal = as_decimal(second)
    if first_decimal is not None and second_decimal is not None:
        return abs(first_decimal - second_decimal) <= Decimal("0.00000001")
    return first == second


def _timestamps_equal(first: Any, second: Any) -> bool:
    first_timestamp = parse_timestamp(first)
    second_timestamp = parse_timestamp(second)
    if first_timestamp is None or second_timestamp is None:
        return first == second
    return first_timestamp == second_timestamp


def _mark_unchanged_repeat_snapshots(store: PostgresStore, logs: list[dict[str, Any]], *, run_id: str) -> None:
    candidates = [log for log in logs if log.get("validation_status") == "valid"]
    if not candidates:
        return
    store.initialize()
    with store.connect() as connection:
        for log in candidates:
            rows = connection.execute(
                """
                SELECT prediction_timestamp, source_snapshot_hash, api_fetched_at, source_updated_at,
                       entry_price_cents, implied_probability, confidence_score,
                       reason_features_json
                FROM app.prediction_logs
                WHERE run_id = %s
                  AND validation_status = 'valid'
                  AND strategy = %s
                  AND event_id = %s
                  AND market_id = %s
                  AND side = %s
                """,
                (
                    run_id,
                    log.get("strategy"),
                    log.get("event_id"),
                    log.get("market_id"),
                    log.get("side"),
                ),
            ).fetchall()
            for row in rows:
                if _timestamps_equal(row["prediction_timestamp"], log.get("timestamp")):
                    continue
                same_values = (
                    _numbers_equal(row["entry_price_cents"], log.get("entry_price_cents"))
                    and _numbers_equal(row["implied_probability"], log.get("implied_probability"))
                    and _numbers_equal(row["confidence_score"], log.get("confidence_score"))
                    and _jsonb(row["reason_features_json"], default={}) == _jsonb(_stable_features_json(log), default={})
                )
                same_snapshot = bool(row["source_snapshot_hash"] and row["source_snapshot_hash"] == log.get("source_snapshot_hash"))
                same_fetch = bool(
                    row["api_fetched_at"]
                    and _timestamps_equal(row["api_fetched_at"], log.get("api_fetched_at"))
                )
                same_source_update = bool(
                    row["source_updated_at"]
                    and log.get("source_updated_at")
                    and _timestamps_equal(row["source_updated_at"], log.get("source_updated_at"))
                )
                if same_fetch or (same_values and (same_snapshot or same_source_update)):
                    log["validation_errors"] = sorted(set([*(log.get("validation_errors") or []), "unchanged_repeat_snapshot"]))
                    log["validation_status"] = "invalid"
                    break


def _snapshot_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("run_id") or ""),
        str(row.get("strategy") or row.get("slip_name") or ""),
        str(row.get("event_id") or ""),
        str(row.get("market_id") or row.get("market") or ""),
        str(row.get("side") or ""),
    )


def _market_exposure_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("event_id") or ""),
        str(row.get("market_id") or row.get("market") or ""),
        str(row.get("side") or ""),
    )


def _assign_snapshot_sequences(store: PostgresStore, logs: list[dict[str, Any]], *, run_id: str) -> None:
    valid_logs = [log for log in logs if log.get("validation_status") == "valid"]
    if not valid_logs:
        return
    store.initialize()
    next_sequence_by_key: dict[tuple[str, str, str, str, str], int] = {}
    with store.connect() as connection:
        for log in valid_logs:
            key = _snapshot_key({**log, "run_id": run_id})
            if key not in next_sequence_by_key:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(snapshot_sequence), 0) AS max_sequence
                    FROM app.prediction_logs
                    WHERE run_id = %s
                      AND validation_status = 'valid'
                      AND strategy = %s
                      AND event_id = %s
                      AND market_id = %s
                      AND side = %s
                    """,
                    key,
                ).fetchone()
                next_sequence_by_key[key] = int(row["max_sequence"] or 0)
            next_sequence_by_key[key] += 1
            log["snapshot_sequence"] = next_sequence_by_key[key]


def log_forward_predictions(
    store: PostgresStore,
    payload: dict[str, Any],
    *,
    run_id: str,
    logged_at: str | None = None,
    max_payload_age_seconds: int = 1800,
) -> dict[str, Any]:
    prediction_timestamp = logged_at or now_timestamp()
    logs = extract_prediction_logs_from_payload(payload, prediction_timestamp=prediction_timestamp, run_id=run_id)
    for log in logs:
        age_errors = _payload_age_errors(
            log,
            prediction_timestamp=prediction_timestamp,
            max_payload_age_seconds=max_payload_age_seconds,
        )
        if age_errors:
            log["validation_errors"] = sorted(set([*(log.get("validation_errors") or []), *age_errors]))
            log["validation_status"] = "invalid"
    _mark_unchanged_repeat_snapshots(store, logs, run_id=run_id)
    _assign_snapshot_sequences(store, logs, run_id=run_id)
    valid_logs = [log for log in logs if log.get("validation_status") == "valid"]
    rejected_logs = [_rejection_from_log(run_id, log) for log in logs if log.get("validation_status") != "valid"]
    inserted_count = 0
    if valid_logs:
        inserted_count = store.insert_prediction_logs(valid_logs)
    if rejected_logs:
        store.insert_prediction_rejections(rejected_logs)
    return {
        "run_id": run_id,
        "prediction_timestamp": prediction_timestamp,
        "attempted_predictions": len(logs),
        "logged_predictions": inserted_count,
        "rejected_predictions": len(rejected_logs),
        "duplicate_rows_ignored": max(0, len(valid_logs) - inserted_count),
        "rejection_reasons": sorted(
            {
                reason
                for rejection in rejected_logs
                for reason in rejection.get("validation_errors", [])
            }
        ),
    }


def load_json_payload(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fetch_official_kalshi_settlements(
    store: PostgresStore,
    *,
    run_id: str,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    store.initialize()
    max_markets = max(1, int(os.environ.get("SETTLEMENT_MAX_MARKETS_PER_RUN", "50")))
    timeout_seconds = max(1, int(os.environ.get("SETTLEMENT_HTTP_TIMEOUT_SECONDS", "8")))
    max_consecutive_errors = max(1, int(os.environ.get("SETTLEMENT_MAX_CONSECUTIVE_FETCH_ERRORS", "3")))
    with store.connect() as connection:
        all_market_ids = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT market_id
                FROM app.prediction_logs
                WHERE run_id = %s
                  AND validation_status = 'valid'
                  AND settlement_state = 'unresolved'
                  AND market_id IS NOT NULL
                  AND market_id != ''
                ORDER BY market_id
                """,
                (run_id,),
            ).fetchall()
        ]
    market_ids = all_market_ids[:max_markets]
    client = http or HttpClient(cache_ttl_seconds=0, max_retries=1)
    outcomes: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, str]] = []
    consecutive_errors = 0
    for market_id in market_ids:
        url = f"{KALSHI_PUBLIC_BASE_URL}/markets/{quote(market_id, safe='')}"
        try:
            response = client.get_text(url, timeout=timeout_seconds)
            payload = response.json()
            market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
            if not isinstance(market, dict):
                fetch_errors.append({"market_id": market_id, "error": "missing_market_payload"})
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    break
                continue
            market = dict(market)
            market.setdefault("market_id", market.get("ticker") or market_id)
            market.setdefault("ticker", market_id)
            market["_api_fetched_at"] = response.fetched_at
            market["_source_url"] = response.url
            outcomes.append(market)
            consecutive_errors = 0
        except Exception as exc:
            fetch_errors.append({"market_id": market_id, "error": f"{type(exc).__name__}: {exc}"})
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                break
    return {
        "source": KALSHI_MARKET_DETAIL_SETTLEMENT_SOURCE,
        "fetched_at": now_timestamp(),
        "outcomes": outcomes,
        "fetch_errors": fetch_errors,
        "markets_pending": len(all_market_ids),
        "markets_requested": len(market_ids),
        "markets_deferred": max(0, len(all_market_ids) - len(market_ids)),
        "stopped_after_consecutive_errors": consecutive_errors >= max_consecutive_errors,
    }


def _settlement_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("outcomes"), list):
        return payload["outcomes"]
    if isinstance(payload.get("markets"), list):
        return payload["markets"]
    if isinstance(payload.get("settlements"), list):
        return payload["settlements"]
    if isinstance(payload.get("market"), dict):
        return [payload["market"]]
    return []


def _settlement_market_id(row: dict[str, Any]) -> str:
    return str(row.get("market_id") or row.get("market") or row.get("market_ticker") or row.get("ticker") or "")


def _settlement_source(payload: dict[str, Any]) -> str:
    return str(payload.get("source") or "provided_settlement_payload")


def _settlement_source_fetched_at(payload: dict[str, Any], outcome: dict[str, Any] | None = None) -> str:
    if outcome:
        for field in ["_api_fetched_at", "api_fetched_at", "fetched_at"]:
            if outcome.get(field):
                return str(outcome[field])
    return str(payload.get("fetched_at") or now_timestamp())


def _settlement_result_signal(outcome: dict[str, Any]) -> str:
    return _normal_state(
        outcome.get("winning_side")
        or outcome.get("result")
        or outcome.get("resolution")
        or outcome.get("expiration_value")
        or outcome.get("settlement_state")
        or outcome.get("state")
    )


def _settlement_status(outcome: dict[str, Any]) -> str:
    return _normal_state(outcome.get("status"))


def _settlement_price(outcome: dict[str, Any], fields: list[str]) -> Any:
    for field in fields:
        value = outcome.get(field)
        if value not in {None, ""}:
            return value
    return None


def _settlement_decision(
    prediction: dict[str, Any],
    outcome: dict[str, Any],
) -> tuple[str, bool | None, Decimal | None, str | None]:
    signal = _settlement_result_signal(outcome)
    status = _settlement_status(outcome)
    if signal in {"fair market", "fair-market", "fair_market"} or status in {"fair market", "fair-market", "fair_market"}:
        price = _settlement_price(outcome, ["fair_market_price_cents", "fair_market_value_cents", "settlement_price_cents"])
        if price is None:
            return "unresolved", None, None, "missing_fair_market_settlement_price"
    if signal in {"early exit", "early-exit", "early_exit"} or status in {"early exit", "early-exit", "early_exit"}:
        price = _settlement_price(outcome, ["exit_price_cents", "early_exit_price_cents", "settlement_price_cents"])
        if price is None:
            return "unresolved", None, None, "missing_exit_price"
    state, actual, explicit_profit = _outcome_settlement_state(prediction, outcome)
    if state == "unresolved":
        if status in UNRESOLVED_STATES or status == "inactive" or (not status and not signal):
            return "unresolved", None, None, None
        return "unresolved", None, None, "unknown_settlement_state"
    return state, actual, as_decimal(explicit_profit), None


def _pl_for_settlement(
    *,
    state: str,
    actual: bool | None,
    explicit_profit: Decimal | None,
    entry_price_cents: Decimal,
) -> Decimal | None:
    if explicit_profit is not None:
        return explicit_profit
    if state in ZERO_PROFIT_SETTLEMENT_STATES:
        return Decimal("0")
    if actual is not None:
        return _round_decimal(Decimal("100") - entry_price_cents if actual else -entry_price_cents, 2)
    return None


def _insert_settlement_audit(
    connection: Any,
    *,
    row: Any,
    new_state: str,
    new_actual: bool | None,
    new_profit: Decimal | None,
    source: str,
    source_fetched_at: str,
    issue: str | None,
    raw_settlement: dict[str, Any],
) -> None:
    raw_json = json.dumps(raw_settlement, sort_keys=True, default=json_default)
    connection.execute(
        """
        INSERT INTO app.settlement_audit
            (prediction_log_id, run_id, market_id, previous_settlement_state,
             new_settlement_state, previous_actual_outcome, new_actual_outcome,
             previous_profit_loss_cents, new_profit_loss_cents, source,
             source_fetched_at, issue, raw_settlement_hash, raw_settlement_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (prediction_log_id, source, issue, new_settlement_state, raw_settlement_hash)
        DO NOTHING
        """,
        (
            row["id"],
            row["run_id"],
            row["market_id"] or row["market"] or "",
            row["settlement_state"],
            new_state,
            row["actual_outcome"],
            new_actual,
            row["profit_loss_cents"],
            new_profit,
            source,
            source_fetched_at,
            issue,
            stable_json_hash(raw_settlement or {}),
            raw_json,
        ),
    )


def import_settlements(
    store: PostgresStore,
    *,
    run_id: str,
    settlements_payload: dict[str, Any],
) -> dict[str, Any]:
    source = _settlement_source(settlements_payload)
    outcomes = {
        _settlement_market_id(row): row
        for row in _settlement_rows(settlements_payload)
        if _settlement_market_id(row)
    }
    if not outcomes:
        return {
            "run_id": run_id,
            "settlement_source": source,
            "settlements_available": 0,
            "rows_updated": 0,
            "issue_rows_updated": 0,
            "settlement_issue_counts": {},
            "fetch_errors": settlements_payload.get("fetch_errors") or [],
        }
    store.initialize()
    settled_updates = 0
    issue_updates = 0
    issue_counts: dict[str, int] = defaultdict(int)
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT id, run_id, side, market_id, market, entry_price_cents,
                   settlement_state, actual_outcome, profit_loss_cents,
                   settlement_issue
            FROM app.prediction_logs
            WHERE run_id = %s
              AND validation_status = 'valid'
            FOR UPDATE SKIP LOCKED
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            market_id = str(row["market_id"] or row["market"] or "")
            outcome = outcomes.get(market_id)
            if not outcome:
                continue
            prediction = {
                "side": row["side"],
                "entry_price_cents": row["entry_price_cents"] or Decimal("0"),
            }
            state, actual, explicit_profit, issue = _settlement_decision(prediction, outcome)
            if state == "unresolved":
                if issue:
                    issue_counts[issue] += 1
                if (row["actual_outcome"] is not None or row["profit_loss_cents"] is not None or row["settlement_state"] != "unresolved" or row["settlement_issue"] != issue):
                    _insert_settlement_audit(
                        connection,
                        row=row,
                        new_state="unresolved",
                        new_actual=None,
                        new_profit=None,
                        source=source,
                        source_fetched_at=_settlement_source_fetched_at(settlements_payload, outcome),
                        issue=issue,
                        raw_settlement=outcome,
                    )
                    connection.execute(
                        """
                        UPDATE app.prediction_logs
                        SET settlement_state = 'unresolved',
                            actual_outcome = NULL,
                            profit_loss_cents = NULL,
                            settlement_issue = %s,
                            settlement_source = %s,
                            settlement_updated_at = %s
                        WHERE id = %s
                        """,
                        (issue, source, now_timestamp(), row["id"]),
                    )
                    issue_updates += 1 if issue else 0
                continue
            entry = _decimal(row["entry_price_cents"])
            actual_value = None if actual is None else bool(actual)
            profit_loss_cents = _pl_for_settlement(
                state=state,
                actual=actual,
                explicit_profit=explicit_profit,
                entry_price_cents=entry,
            )
            if (
                row["settlement_state"] == state
                and row["actual_outcome"] == actual_value
                and row["profit_loss_cents"] == profit_loss_cents
                and not row["settlement_issue"]
            ):
                continue
            _insert_settlement_audit(
                connection,
                row=row,
                new_state=state,
                new_actual=actual_value,
                new_profit=profit_loss_cents,
                source=source,
                source_fetched_at=_settlement_source_fetched_at(settlements_payload, outcome),
                issue=None,
                raw_settlement=outcome,
            )
            connection.execute(
                """
                UPDATE app.prediction_logs
                SET settlement_state = %s,
                    actual_outcome = %s,
                    profit_loss_cents = %s,
                    settlement_issue = NULL,
                    settlement_source = %s,
                    settlement_updated_at = %s
                WHERE id = %s
                """,
                (
                    state,
                    actual_value,
                    profit_loss_cents,
                    source,
                    now_timestamp(),
                    row["id"],
                )
            )
            settled_updates += 1
    return {
        "run_id": run_id,
        "settlement_source": source,
        "settlements_available": len(outcomes),
        "rows_updated": settled_updates,
        "issue_rows_updated": issue_updates,
        "settlement_issue_counts": dict(sorted(issue_counts.items())),
        "fetch_errors": settlements_payload.get("fetch_errors") or [],
    }


def _fetch_run(connection: Any, run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT run_id, started_at, status, model_versions_json, config_json, config_hash
        FROM app.paper_test_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "started_at": row["started_at"],
        "status": row["status"],
        "model_versions": _jsonb(row["model_versions_json"], default={}),
        "config": _jsonb(row["config_json"], default={}),
        "config_hash": row["config_hash"],
    }


def _date_clause(date: str | None, column: str = "prediction_timestamp") -> tuple[str, list[Any]]:
    report_date = normalize_report_date(date)
    if not report_date:
        return "", []
    return f" AND (({column} AT TIME ZONE 'America/New_York')::date = %s::date)", [report_date]


def _is_unresolved_state(state: Any) -> bool:
    return _normal_state(state) in UNRESOLVED_STATES


def _duplicate_exposure_warnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_market_exposure_key(row)].append(row)
    warnings = []
    for key, group_rows in grouped.items():
        if len(group_rows) <= 1:
            continue
        warnings.append(
            {
                "event_id": key[0],
                "market_id": key[1],
                "side": key[2],
                "count": len(group_rows),
                "strategies": sorted({str(row.get("strategy") or row.get("slip_name") or "unknown") for row in group_rows}),
                "unique_snapshot_hashes": len({str(row.get("source_snapshot_hash") or "") for row in group_rows}),
            }
        )
    return warnings


def _unique_market_exposure_count(rows: list[dict[str, Any]]) -> int:
    return len({_market_exposure_key(row) for row in rows})


def _snapshot_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_snapshot_key(row)].append(row)
    repeated_groups = [group_rows for group_rows in grouped.values() if len(group_rows) > 1]
    return {
        "repeated_snapshot_groups": len(repeated_groups),
        "changed_snapshots": sum(len(group_rows) - 1 for group_rows in repeated_groups),
    }


def _sample_status(count: int) -> str:
    if count >= MIN_SETTLED_FOR_PERFORMANCE:
        return "sufficient_sample"
    return f"insufficient_sample ({count}/{MIN_SETTLED_FOR_PERFORMANCE})"


def _heartbeat_status(
    *,
    valid_rows: int,
    settled_rows: int,
    rejection_reason_counts: dict[str, int],
) -> str:
    if valid_rows > 0:
        return "material_change"
    if settled_rows > 0:
        return "settlement_only_change"
    if rejection_reason_counts.get("unchanged_repeat_snapshot"):
        return "no_material_change"
    if rejection_reason_counts:
        return "blocked_by_source"
    return "no_material_change"


def build_daily_report(store: PostgresStore, *, run_id: str, date: str | None = None) -> dict[str, Any]:
    store.initialize()
    with store.connect() as connection:
        run = _fetch_run(connection, run_id)
        if run is None:
            raise ValueError(f"Unknown paper test run: {run_id}")
        prediction_clause, prediction_params = _date_clause(date)
        rejection_clause, rejection_params = _date_clause(date)
        prediction_rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT prediction_timestamp, event, event_id, market, market_id,
                       side, strategy, model_version, confidence_score,
                       event_start_time, market_close_time, api_fetched_at,
                       source_updated_at, source_snapshot_hash,
                       snapshot_sequence,
                       entry_price_cents, implied_probability,
                       settlement_state, actual_outcome, profit_loss_cents,
                       slip_name, validation_status, settlement_issue
                FROM app.prediction_logs
                WHERE run_id = %s{prediction_clause}
                ORDER BY prediction_timestamp, market_id
                """,
                [run_id, *prediction_params],
            ).fetchall()
        ]
        rejection_rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT prediction_timestamp, event, event_id, market, market_id,
                       side, strategy, validation_errors_json
                FROM app.prediction_rejections
                WHERE run_id = %s{rejection_clause}
                ORDER BY prediction_timestamp, market_id
                """,
                [run_id, *rejection_params],
            ).fetchall()
        ]
        legacy_rows_excluded = connection.execute(
            "SELECT COUNT(*) FROM app.prediction_logs WHERE run_id IS NULL"
        ).fetchone()[0]
    valid_prediction_rows = [row for row in prediction_rows if row.get("validation_status") == "valid"]
    invalid_log_rows = [row for row in prediction_rows if row.get("validation_status") != "valid"]
    settled_rows = [row for row in valid_prediction_rows if not _is_unresolved_state(row.get("settlement_state"))]
    unresolved_rows = [row for row in valid_prediction_rows if _is_unresolved_state(row.get("settlement_state"))]
    win_loss_rows = [row for row in settled_rows if _normal_state(row.get("settlement_state")) in {"win", "loss"}]
    rows_with_pl = [row for row in settled_rows if row.get("profit_loss_cents") is not None]
    wins = sum(1 for row in win_loss_rows if _normal_state(row.get("settlement_state")) == "win")
    risked = sum((_decimal(row.get("entry_price_cents")) for row in rows_with_pl), Decimal("0"))
    profit = sum((_decimal(row.get("profit_loss_cents")) for row in rows_with_pl), Decimal("0"))
    rejection_reason_counts: dict[str, int] = defaultdict(int)
    for row in rejection_rows:
        for reason in _jsonb(row.get("validation_errors_json"), default=[]):
            rejection_reason_counts[str(reason)] += 1
    settlement_issue_counts: dict[str, int] = defaultdict(int)
    for row in valid_prediction_rows:
        issue = str(row.get("settlement_issue") or "")
        if issue:
            settlement_issue_counts[issue] += 1
    snapshot_summary = _snapshot_summary(valid_prediction_rows)
    settled_deduped_market_exposures = _unique_market_exposure_count(settled_rows)
    unique_market_exposures = _unique_market_exposure_count(valid_prediction_rows)
    sample_status = _sample_status(len(win_loss_rows))
    deduped_sample_status = _sample_status(settled_deduped_market_exposures)
    stage3b_gate_status = (
        "ready_for_stage3b_audit"
        if len(settled_rows) >= MIN_SETTLED_FOR_PERFORMANCE
        and settled_deduped_market_exposures >= MIN_SETTLED_FOR_PERFORMANCE
        else "blocked_by_sample_size"
    )
    heartbeat_status = _heartbeat_status(
        valid_rows=len(valid_prediction_rows),
        settled_rows=len(settled_rows),
        rejection_reason_counts=rejection_reason_counts,
    )
    if len(settled_rows) == 0:
        win_rate_status = "unavailable (no settled rows)"
        roi_status = "unavailable (no settled rows; fee-excluded)"
        calibration_status = "unavailable (no settled rows)"
    elif len(win_loss_rows) < MIN_SETTLED_FOR_PERFORMANCE:
        win_rate_status = f"sample too small; research-only ({len(win_loss_rows)}/{MIN_SETTLED_FOR_PERFORMANCE})"
        roi_status = f"sample too small; research-only; fee-excluded ({len(win_loss_rows)}/{MIN_SETTLED_FOR_PERFORMANCE})"
        calibration_status = f"sample too small ({len(win_loss_rows)}/{MIN_SETTLED_FOR_PERFORMANCE})"
    else:
        win_rate_status = "research-only; settled win/loss rows only"
        roi_status = "research-only; fee-excluded; settled rows only"
        calibration_status = "research-only"
    return {
        "run": run,
        "report_date": normalize_report_date(date),
        "new_predictions_logged": len(valid_prediction_rows),
        "new_valid_rows": len(valid_prediction_rows),
        "heartbeat_status": heartbeat_status,
        "changed_snapshots": snapshot_summary["changed_snapshots"],
        "unchanged_repeat_snapshot_rejections": rejection_reason_counts.get("unchanged_repeat_snapshot", 0),
        "unique_market_exposures": unique_market_exposures,
        "repeated_snapshot_groups": snapshot_summary["repeated_snapshot_groups"],
        "settled_predictions": len(settled_rows),
        "settled_raw_rows": len(settled_rows),
        "settled_deduped_market_exposures": settled_deduped_market_exposures,
        "win_loss_predictions": len(win_loss_rows),
        "unresolved_predictions": len(unresolved_rows),
        "invalid_prediction_log_rows": len(invalid_log_rows),
        "invalid_rejected_predictions": len(rejection_rows),
        "legacy_rows_excluded": legacy_rows_excluded,
        "settled_profit_loss_cents_fee_excluded": _round_decimal(profit, 2),
        "settled_risked_cents": _round_decimal(risked, 2),
        "win_rate": _round_decimal(Decimal(wins) / Decimal(len(win_loss_rows)), 6) if len(win_loss_rows) >= MIN_SETTLED_FOR_PERFORMANCE else None,
        "roi_fee_excluded": _round_decimal(profit / risked, 6) if risked and len(win_loss_rows) >= MIN_SETTLED_FOR_PERFORMANCE else None,
        "sample_status": sample_status,
        "deduped_sample_status": deduped_sample_status,
        "stage3b_gate_status": stage3b_gate_status,
        "win_rate_status": win_rate_status,
        "roi_status": roi_status,
        "calibration_status": calibration_status,
        "duplicate_exposure_warnings": _duplicate_exposure_warnings(valid_prediction_rows),
        "settlement_issue_counts": dict(sorted(settlement_issue_counts.items())),
        "rejection_reason_counts": dict(sorted(rejection_reason_counts.items())),
        "rejection_reasons": sorted(rejection_reason_counts),
        "connector_status": build_connectors_status(),
    }


def render_daily_report(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [
        "Stage 3A Private Live Paper Report",
        f"Run ID: {run['run_id']}",
        f"Started at: {run['started_at']}",
        f"Config hash: {run['config_hash']}",
        f"Report date: {report.get('report_date') or 'all'}",
        "",
        f"Heartbeat status: {report['heartbeat_status']}",
        f"New predictions logged: {report['new_predictions_logged']}",
        f"Changed snapshots: {report['changed_snapshots']}",
        f"Unchanged repeat snapshot rejections: {report['unchanged_repeat_snapshot_rejections']}",
        f"Unique market exposures: {report['unique_market_exposures']}",
        f"Repeated snapshot groups: {report['repeated_snapshot_groups']}",
        f"Settled predictions: {report['settled_predictions']}",
        f"Settled raw rows: {report['settled_raw_rows']}",
        f"Settled de-duped market exposures: {report['settled_deduped_market_exposures']}",
        f"Unresolved predictions: {report['unresolved_predictions']}",
        f"Invalid/rejected predictions: {report['invalid_rejected_predictions']}",
        f"Invalid prediction-log rows: {report['invalid_prediction_log_rows']}",
        f"Legacy rows excluded: {report['legacy_rows_excluded']}",
        f"Raw sample status: {report['sample_status']}",
        f"De-duped sample status: {report['deduped_sample_status']}",
        f"Stage 3B gate: {report['stage3b_gate_status']}",
        "",
        f"Settled P/L fee-excluded: {report['settled_profit_loss_cents_fee_excluded']}c",
        f"Win rate: {report['win_rate']} ({report['win_rate_status']})",
        f"ROI fee-excluded: {report['roi_fee_excluded']} ({report['roi_status']})",
        f"Calibration: unavailable ({report['calibration_status']})",
        "",
        "Duplicate exposure warnings:",
    ]
    duplicate_warnings = report.get("duplicate_exposure_warnings") or []
    if duplicate_warnings:
        for warning in duplicate_warnings:
            lines.append(
                f"- {warning['market_id']} {warning['side']}: count={warning['count']} "
                f"strategies={', '.join(warning['strategies'])} snapshots={warning['unique_snapshot_hashes']}"
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Settlement issues:")
    settlement_issue_counts = report.get("settlement_issue_counts") or {}
    if settlement_issue_counts:
        for issue, count in settlement_issue_counts.items():
            lines.append(f"- {issue}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Rejected prediction reasons by missing/invalid proof field:")
    rejection_reason_counts = report.get("rejection_reason_counts") or {}
    if rejection_reason_counts:
        for reason, count in rejection_reason_counts.items():
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    lines.extend(connector_status_report_lines(report.get("connector_status", {})))
    lines.append("")
    lines.append("No profitability, edge, or calibration claim is made by this report.")
    return "\n".join(lines)


def write_daily_report(report: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_daily_report(report), encoding="utf-8")


def _dedupe_market_level(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_market_exposure_key(row)].append(row)
    deduped = []
    for group_rows in grouped.values():
        deduped.append(
            sorted(
                group_rows,
                key=lambda row: (
                    str(row.get("prediction_timestamp") or ""),
                    int(row.get("snapshot_sequence") or 1),
                    str(row.get("strategy") or ""),
                ),
            )[0]
        )
    return deduped


def _confidence_bucket(score: Decimal) -> str:
    if score >= Decimal("0.85"):
        return "85-100"
    if score >= Decimal("0.75"):
        return "75-85"
    if score >= Decimal("0.65"):
        return "65-75"
    return "0-65"


def _win_loss_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if _normal_state(row.get("settlement_state")) in {"win", "loss"}]


def _pl_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("profit_loss_cents") is not None and not _is_unresolved_state(row.get("settlement_state"))]


def _performance_summary(rows: list[dict[str, Any]], *, gate_count: int | None = None) -> dict[str, Any]:
    win_loss = _win_loss_rows(rows)
    pl_rows = _pl_rows(rows)
    wins = sum(1 for row in win_loss if _normal_state(row.get("settlement_state")) == "win")
    risked = sum((_decimal(row.get("entry_price_cents")) for row in pl_rows), Decimal("0"))
    profit = sum((_decimal(row.get("profit_loss_cents")) for row in pl_rows), Decimal("0"))
    sample_count = len(win_loss) if gate_count is None else gate_count
    sample_status = _sample_status(sample_count)
    can_show_metrics = sample_count >= MIN_SETTLED_FOR_PERFORMANCE
    entry_prices = [_decimal(row.get("entry_price_cents")) for row in win_loss if row.get("entry_price_cents") is not None]
    odds: list[Decimal] = []
    for price in entry_prices:
        probability = price / Decimal("100")
        if probability > 0:
            odds.append(Decimal("1") / probability)
    probabilities = [
        _decimal(row.get("implied_probability") if row.get("implied_probability") is not None else row.get("confidence_score"))
        for row in win_loss
    ]
    brier = None
    calibration_error = None
    if probabilities and win_loss:
        outcomes = [Decimal("1") if _normal_state(row.get("settlement_state")) == "win" else Decimal("0") for row in win_loss]
        brier = sum(((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)), Decimal("0")) / Decimal(len(probabilities))
        calibration_error = abs(sum(probabilities, Decimal("0")) / Decimal(len(probabilities)) - (Decimal(wins) / Decimal(len(win_loss))))
    return {
        "rows": len(rows),
        "win_loss_rows": len(win_loss),
        "pl_rows": len(pl_rows),
        "wins": wins,
        "losses": len(win_loss) - wins,
        "risked_cents": _round_decimal(risked, 2),
        "profit_loss_cents_fee_excluded": _round_decimal(profit, 2),
        "sample_status": sample_status,
        "win_rate": _round_decimal(Decimal(wins) / Decimal(len(win_loss)), 6) if win_loss and can_show_metrics else None,
        "roi_fee_excluded": _round_decimal(profit / risked, 6) if risked and can_show_metrics else None,
        "average_entry_price_cents": _round_decimal(sum(entry_prices, Decimal("0")) / Decimal(len(entry_prices)), 4) if entry_prices else None,
        "average_decimal_odds": _round_decimal(sum(odds, Decimal("0")) / Decimal(len(odds)), 4) if odds else None,
        "brier_score": _round_decimal(brier, 6) if brier is not None and can_show_metrics else None,
        "calibration_error": _round_decimal(calibration_error, 6) if calibration_error is not None and can_show_metrics else None,
    }


def _bucket_performance(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    bucket_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _win_loss_rows(rows):
        bucket_rows[_confidence_bucket(_decimal(row.get("confidence_score")))].append(row)
    output = {}
    for bucket, group_rows in sorted(bucket_rows.items()):
        summary = _performance_summary(group_rows, gate_count=len(group_rows))
        sample_status = "sufficient_sample" if len(group_rows) >= MIN_SETTLED_FOR_BUCKET else f"insufficient_sample ({len(group_rows)}/{MIN_SETTLED_FOR_BUCKET})"
        output[bucket] = {
            "picks": len(group_rows),
            "sample_status": sample_status,
            "win_rate": summary["win_rate"] if len(group_rows) >= MIN_SETTLED_FOR_BUCKET else None,
            "roi_fee_excluded": summary["roi_fee_excluded"] if len(group_rows) >= MIN_SETTLED_FOR_BUCKET else None,
        }
    return output


def _strategy_performance(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("strategy") or row.get("slip_name") or "unknown")].append(row)
    return {strategy: _performance_summary(group_rows) for strategy, group_rows in sorted(grouped.items())}


def _extreme_rows(rows: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    with_pl = _pl_rows(rows)
    sorted_rows = sorted(with_pl, key=lambda row: _decimal(row.get("profit_loss_cents")), reverse=reverse)
    return [
        {
            "event": row.get("event"),
            "market_id": row.get("market_id") or row.get("market"),
            "side": row.get("side"),
            "strategy": row.get("strategy"),
            "confidence_score": row.get("confidence_score"),
            "entry_price_cents": row.get("entry_price_cents"),
            "profit_loss_cents": row.get("profit_loss_cents"),
        }
        for row in sorted_rows[:10]
    ]


def _concentration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pl_rows = _pl_rows(rows)
    total_abs = sum((abs(_decimal(row.get("profit_loss_cents"))) for row in pl_rows), Decimal("0"))
    if not total_abs:
        return {"top_5_abs_pl_share": None, "note": "no settled P/L rows"}
    top_5 = sorted((abs(_decimal(row.get("profit_loss_cents"))) for row in pl_rows), reverse=True)[:5]
    return {
        "top_5_abs_pl_share": _round_decimal(sum(top_5, Decimal("0")) / total_abs, 6),
        "note": "concentration risk is descriptive only; no edge claim",
    }


def build_stage3b_audit_report(store: PostgresStore, *, run_id: str) -> dict[str, Any]:
    daily = build_daily_report(store, run_id=run_id)
    store.initialize()
    with store.connect() as connection:
        prediction_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT prediction_timestamp, event, event_id, market, market_id,
                       side, strategy, model_version, confidence_score,
                       entry_price_cents, implied_probability, source_snapshot_hash,
                       snapshot_sequence, settlement_state, actual_outcome,
                       profit_loss_cents, validation_status, settlement_issue
                FROM app.prediction_logs
                WHERE run_id = %s AND validation_status = 'valid'
                ORDER BY prediction_timestamp, market_id
                """,
                (run_id,),
            ).fetchall()
        ]
    settled_rows = [row for row in prediction_rows if not _is_unresolved_state(row.get("settlement_state"))]
    deduped_rows = _dedupe_market_level(prediction_rows)
    deduped_settled_rows = [row for row in deduped_rows if not _is_unresolved_state(row.get("settlement_state"))]
    duplicate_warnings = daily.get("duplicate_exposure_warnings") or []
    raw_summary = _performance_summary(settled_rows)
    deduped_summary = _performance_summary(deduped_settled_rows, gate_count=len(deduped_settled_rows))
    gate_status = (
        "stage3b_audit_ready"
        if len(settled_rows) >= MIN_SETTLED_FOR_PERFORMANCE
        and len(deduped_settled_rows) >= MIN_SETTLED_FOR_PERFORMANCE
        else "blocked_by_sample_size"
    )
    return {
        "stage": "Stage 3B settled performance audit",
        "run_id": run_id,
        "gate_status": gate_status,
        "raw_settled_rows": len(settled_rows),
        "deduped_settled_market_exposures": len(deduped_settled_rows),
        "unresolved_predictions": daily["unresolved_predictions"],
        "invalid_rejected_predictions": daily["invalid_rejected_predictions"],
        "invalid_prediction_log_rows": daily["invalid_prediction_log_rows"],
        "legacy_rows_excluded": daily["legacy_rows_excluded"],
        "unique_market_exposures": daily["unique_market_exposures"],
        "repeated_snapshot_groups": daily["repeated_snapshot_groups"],
        "duplicate_exposure_warnings": duplicate_warnings,
        "duplicate_exposure_warning_count": len(duplicate_warnings),
        "settlement_issue_counts": daily["settlement_issue_counts"],
        "rejection_reason_counts": daily["rejection_reason_counts"],
        "raw_row_performance": raw_summary,
        "deduped_market_performance": deduped_summary,
        "by_strategy_performance": _strategy_performance(settled_rows),
        "confidence_bucket_performance": _bucket_performance(deduped_settled_rows),
        "biggest_wins": _extreme_rows(deduped_settled_rows, reverse=True),
        "biggest_losses": _extreme_rows(deduped_settled_rows, reverse=False),
        "concentration_risk": _concentration_summary(deduped_settled_rows),
        "duplicate_exposure_impact": {
            "raw_roi_fee_excluded": raw_summary["roi_fee_excluded"],
            "deduped_roi_fee_excluded": deduped_summary["roi_fee_excluded"],
            "raw_win_rate": raw_summary["win_rate"],
            "deduped_win_rate": deduped_summary["win_rate"],
            "warning": "raw row metrics can be inflated by repeated snapshots or multi-strategy exposure",
        },
        "status_note": "research-only; no profitability, edge, or calibration claim",
    }


def render_stage3b_audit_report(report: dict[str, Any]) -> str:
    raw = report["raw_row_performance"]
    deduped = report["deduped_market_performance"]
    lines = [
        "Stage 3B Settled Performance Audit",
        f"Run ID: {report['run_id']}",
        f"Gate status: {report['gate_status']}",
        f"Raw settled rows: {report['raw_settled_rows']}",
        f"De-duped settled market exposures: {report['deduped_settled_market_exposures']}",
        f"Unresolved predictions: {report['unresolved_predictions']}",
        f"Rejected predictions: {report['invalid_rejected_predictions']}",
        f"Invalid prediction-log rows: {report['invalid_prediction_log_rows']}",
        f"Legacy/null-run rows excluded: {report['legacy_rows_excluded']}",
        f"Duplicate exposure warnings: {report['duplicate_exposure_warning_count']}",
        "",
        "Raw row performance (research-only, fee-excluded):",
        f"- sample={raw['sample_status']} win_rate={raw['win_rate']} roi_fee_excluded={raw['roi_fee_excluded']} avg_entry={raw['average_entry_price_cents']}c avg_odds={raw['average_decimal_odds']}",
        "",
        "De-duped market-level performance (primary audit view, research-only, fee-excluded):",
        f"- sample={deduped['sample_status']} win_rate={deduped['win_rate']} roi_fee_excluded={deduped['roi_fee_excluded']} avg_entry={deduped['average_entry_price_cents']}c avg_odds={deduped['average_decimal_odds']}",
        f"- brier_score={deduped['brier_score']} calibration_error={deduped['calibration_error']}",
        "",
        "By-strategy performance:",
    ]
    for strategy, summary in report.get("by_strategy_performance", {}).items():
        lines.append(
            f"- {strategy}: rows={summary['rows']} sample={summary['sample_status']} "
            f"win_rate={summary['win_rate']} roi_fee_excluded={summary['roi_fee_excluded']}"
        )
    lines.append("")
    lines.append("Confidence buckets:")
    buckets = report.get("confidence_bucket_performance") or {}
    if buckets:
        for bucket, summary in buckets.items():
            lines.append(
                f"- {bucket}: picks={summary['picks']} sample={summary['sample_status']} "
                f"win_rate={summary['win_rate']} roi_fee_excluded={summary['roi_fee_excluded']}"
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Duplicate exposure impact:")
    impact = report["duplicate_exposure_impact"]
    lines.append(f"- raw_roi={impact['raw_roi_fee_excluded']} deduped_roi={impact['deduped_roi_fee_excluded']}")
    lines.append(f"- raw_win_rate={impact['raw_win_rate']} deduped_win_rate={impact['deduped_win_rate']}")
    lines.append(f"- {impact['warning']}")
    lines.append("")
    lines.append("Biggest losses:")
    for row in report.get("biggest_losses") or []:
        lines.append(f"- {row['market_id']} {row['side']} strategy={row['strategy']} P/L={row['profit_loss_cents']}c confidence={row['confidence_score']}")
    lines.append("")
    lines.append("Biggest wins:")
    for row in report.get("biggest_wins") or []:
        lines.append(f"- {row['market_id']} {row['side']} strategy={row['strategy']} P/L={row['profit_loss_cents']}c confidence={row['confidence_score']}")
    lines.append("")
    lines.append("Data-quality and settlement notes:")
    lines.append(f"- rejection reasons: {report['rejection_reason_counts']}")
    lines.append(f"- settlement issues: {report['settlement_issue_counts']}")
    lines.append(f"- concentration risk: {report['concentration_risk']}")
    lines.append("")
    lines.append("No profitability, edge, or calibration claim is made by this audit.")
    return "\n".join(lines)


def write_stage3b_audit_report(report: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_stage3b_audit_report(report), encoding="utf-8")


def default_stage3b_audit_path(run_id: str) -> Path:
    return repo_path("data", "paper_runs", f"{run_id}_stage3b_audit.txt")


def default_run_lock_path(run_id: str) -> Path:
    return repo_path("data", "paper_runs", f"{run_id}_config.json")


def default_daily_report_path(run_id: str) -> Path:
    return repo_path("data", "paper_runs", f"{run_id}_daily_report.txt")
