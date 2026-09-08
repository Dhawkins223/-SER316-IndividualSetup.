from __future__ import annotations

import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlencode
from decimal import Decimal, ROUND_HALF_UP

from .business_store import create_store
from .database import DatabaseRow, DatabaseSession, as_decimal
from .config import repo_path
from .connectors.http import HttpClient
from .connectors.lifecycle import apply_post_report_connectors
from .connectors.status import build_connectors_status, connector_status_report_lines
from .private_research import (
    accuracy_status,
    deterministic_hash,
    gate_result,
    isoformat_utc,
    parse_aware_timestamp,
    parse_horizon,
    sample_status,
    stable_json,
    utc_now_iso,
    write_csv,
    write_json,
    write_text,
)


CRYPTO_MODEL_VERSION = "crypto_research_v1"
CRYPTO_STRATEGY = "ohlcv_momentum_v1"
CRYPTO_SYMBOLS = ("BTC-USD", "ETH-USD")
CRYPTO_HORIZONS = ("15m", "1h")
CRYPTO_SETTLEMENT_BAND_BPS = Decimal("5")
CRYPTO_BPS_QUANTUM = Decimal("0.000001")
CRYPTO_STALE_SECONDS = 15 * 60
CRYPTO_FEATURE_FORBIDDEN_FIELDS = {"actual_outcome", "profit_loss", "profit_loss_cents", "settlement_price", "return_bps"}


def default_crypto_daily_report_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_daily_report.txt")


def _round_bps(value: Decimal) -> Decimal:
    return value.quantize(CRYPTO_BPS_QUANTUM, rounding=ROUND_HALF_UP)


def default_crypto_all_report_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_all_report.txt")


def default_crypto_payload_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_source.json")


def default_crypto_features_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_features.csv")


def default_crypto_labels_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_labels.csv")


def default_crypto_stage3b_audit_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_stage3b_audit.txt")


def default_crypto_stage4_diagnostic_path(run_id: str) -> Path:
    return repo_path("data", "crypto_runs", f"{run_id}_stage4_diagnostic.txt")


@contextmanager
def _connect():
    with create_store().connect() as connection:
        yield connection


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    parsed = json.loads(str(value), parse_float=Decimal)
    return parsed if isinstance(parsed, dict) else {}


def normalize_coinbase_candles(
    payload: list[Any],
    *,
    symbol: str,
    api_fetched_at: str,
    timeframe: str = "1m",
) -> list[dict[str, Any]]:
    seconds = 60 if timeframe == "1m" else int(timeframe.rstrip("m")) * 60
    records: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 6:
            continue
        open_time = datetime.fromtimestamp(float(item[0]), tz=timezone.utc)
        record = {
            "asset_class": "crypto",
            "exchange": "coinbase",
            "symbol": symbol,
            "timeframe": timeframe,
            "candle_open_time": isoformat_utc(open_time),
            "candle_close_time": isoformat_utc(open_time + timedelta(seconds=seconds)),
            "low": Decimal(str(item[1])),
            "high": Decimal(str(item[2])),
            "open": Decimal(str(item[3])),
            "close": Decimal(str(item[4])),
            "volume": Decimal(str(item[5])),
            "api_fetched_at": api_fetched_at,
            "source_updated_at": isoformat_utc(open_time + timedelta(seconds=seconds)),
            "source_payload_ref": f"coinbase:{symbol}:{timeframe}:{int(float(item[0]))}",
        }
        record["source_snapshot_hash"] = deterministic_hash(record)
        records.append(record)
    return sorted(records, key=lambda row: row["candle_open_time"])


def normalize_kraken_ohlc(
    payload: dict[str, Any],
    *,
    pair_key: str,
    symbol: str,
    api_fetched_at: str,
    timeframe: str = "1m",
) -> list[dict[str, Any]]:
    result = payload.get("result") or {}
    candles = result.get(pair_key) or []
    records: list[dict[str, Any]] = []
    for item in candles:
        if not isinstance(item, list) or len(item) < 7:
            continue
        open_time = datetime.fromtimestamp(float(item[0]), tz=timezone.utc)
        close_time = open_time + timedelta(minutes=1 if timeframe == "1m" else int(timeframe.rstrip("m")))
        record = {
            "asset_class": "crypto",
            "exchange": "kraken",
            "symbol": symbol,
            "timeframe": timeframe,
            "candle_open_time": isoformat_utc(open_time),
            "candle_close_time": isoformat_utc(close_time),
            "open": Decimal(str(item[1])),
            "high": Decimal(str(item[2])),
            "low": Decimal(str(item[3])),
            "close": Decimal(str(item[4])),
            "volume": Decimal(str(item[6])),
            "api_fetched_at": api_fetched_at,
            "source_updated_at": isoformat_utc(close_time),
            "source_payload_ref": f"kraken:{pair_key}:{timeframe}:{int(float(item[0]))}",
        }
        record["source_snapshot_hash"] = deterministic_hash(record)
        records.append(record)
    return sorted(records, key=lambda row: row["candle_open_time"])


def collect_crypto_payload(
    *,
    symbols: tuple[str, ...] = CRYPTO_SYMBOLS,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    client = http or HttpClient(cache_ttl_seconds=0)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for symbol in symbols:
        product = symbol
        query = urlencode({"granularity": 60})
        url = f"https://api.exchange.coinbase.com/products/{product}/candles?{query}"
        try:
            response = client.get_text(url, timeout=20)
            records.extend(normalize_coinbase_candles(response.json(), symbol=symbol, api_fetched_at=response.fetched_at))
        except Exception as exc:  # pragma: no cover - network-dependent
            errors.append({"exchange": "coinbase", "symbol": symbol, "error": type(exc).__name__, "message": str(exc)})
    return {
        "asset_class": "crypto",
        "model_version": CRYPTO_MODEL_VERSION,
        "strategy": CRYPTO_STRATEGY,
        "generated_at": utc_now_iso(),
        "records": records,
        "errors": errors,
    }


def write_crypto_payload(path: str | Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def _latest_closed_records(payload: dict[str, Any], prediction_timestamp: str | None = None) -> list[dict[str, Any]]:
    records = payload.get("records") or []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    timestamp = parse_aware_timestamp(prediction_timestamp or payload.get("generated_at") or utc_now_iso())
    if timestamp is None:
        return []
    for record in records:
        close_time = parse_aware_timestamp(record.get("candle_close_time"))
        if close_time is None or close_time > timestamp:
            continue
        key = (str(record.get("exchange")), str(record.get("symbol")))
        if key not in by_key or str(record.get("candle_close_time")) > str(by_key[key].get("candle_close_time")):
            by_key[key] = dict(record)
    return list(by_key.values())


def _build_crypto_candidate(record: dict[str, Any], *, run_id: str, horizon: str, prediction_timestamp: str) -> dict[str, Any]:
    candle_open = parse_aware_timestamp(record.get("candle_open_time"))
    candle_close = parse_aware_timestamp(record.get("candle_close_time"))
    prediction_time = parse_aware_timestamp(prediction_timestamp)
    settlement_time = prediction_time + parse_horizon(horizon) if prediction_time else None
    close = as_decimal(record.get("close"), default=Decimal("0")) or Decimal("0")
    open_price = as_decimal(record.get("open"), default=Decimal("0")) or Decimal("0")
    return_bps = Decimal("0") if open_price == 0 else ((close - open_price) / open_price) * Decimal("10000")
    side = "UP" if return_bps >= 0 else "DOWN"
    confidence = min(Decimal("0.69"), Decimal("0.5") + min(abs(return_bps) / Decimal("1000"), Decimal("0.19")))
    features = {
        "open": open_price,
        "high": as_decimal(record.get("high")),
        "low": as_decimal(record.get("low")),
        "close": close,
        "volume": as_decimal(record.get("volume")),
        "last_candle_return_bps": round(return_bps, 6),
        "timeframe": record.get("timeframe"),
    }
    return {
        "asset_class": "crypto",
        "run_id": run_id,
        "model_version": CRYPTO_MODEL_VERSION,
        "strategy": CRYPTO_STRATEGY,
        "exchange": record.get("exchange"),
        "symbol": record.get("symbol"),
        "horizon": horizon,
        "side": side,
        "prediction_timestamp": prediction_timestamp,
        "entry_time": prediction_timestamp,
        "entry_price": close,
        "settlement_time": isoformat_utc(settlement_time) if settlement_time else None,
        "api_fetched_at": record.get("api_fetched_at"),
        "source_updated_at": record.get("source_updated_at"),
        "source_snapshot_hash": record.get("source_snapshot_hash"),
        "source_payload_ref": record.get("source_payload_ref"),
        "timeframe": record.get("timeframe"),
        "candle_open_time": isoformat_utc(candle_open) if candle_open else record.get("candle_open_time"),
        "candle_close_time": isoformat_utc(candle_close) if candle_close else record.get("candle_close_time"),
        "open": open_price,
        "high": as_decimal(record.get("high")),
        "low": as_decimal(record.get("low")),
        "close": close,
        "volume": as_decimal(record.get("volume")),
        "bid": record.get("bid"),
        "ask": record.get("ask"),
        "mid_price": record.get("mid_price") or close,
        "spread": record.get("spread"),
        "implied_probability": None,
        "confidence_score": round(confidence, 6),
        "features": features,
        "settlement_state": "unresolved",
    }


def build_crypto_prediction_candidates(
    payload: dict[str, Any],
    *,
    run_id: str,
    prediction_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    timestamp = prediction_timestamp or payload.get("generated_at") or utc_now_iso()
    candidates: list[dict[str, Any]] = []
    for record in _latest_closed_records(payload, timestamp):
        for horizon in CRYPTO_HORIZONS:
            candidates.append(_build_crypto_candidate(record, run_id=run_id, horizon=horizon, prediction_timestamp=timestamp))
    return candidates


def validate_crypto_prediction(candidate: dict[str, Any], *, now: str | None = None) -> list[str]:
    errors: list[str] = []
    required = [
        ("symbol", "missing_symbol"),
        ("horizon", "missing_horizon"),
        ("prediction_timestamp", "missing_prediction_timestamp"),
        ("api_fetched_at", "missing_api_fetched_at"),
        ("source_snapshot_hash", "missing_source_snapshot_hash"),
        ("entry_price", "missing_entry_price"),
        ("settlement_time", "missing_settlement_time"),
    ]
    for field, reason in required:
        if candidate.get(field) in {None, ""}:
            errors.append(reason)
    prediction_time = parse_aware_timestamp(candidate.get("prediction_timestamp"))
    settlement_time = parse_aware_timestamp(candidate.get("settlement_time"))
    api_fetched_at = parse_aware_timestamp(candidate.get("api_fetched_at"))
    candle_close = parse_aware_timestamp(candidate.get("candle_close_time"))
    if candidate.get("prediction_timestamp") and prediction_time is None:
        errors.append("invalid_timezone")
    if candidate.get("settlement_time") and settlement_time is None:
        errors.append("invalid_timezone")
    if candidate.get("api_fetched_at") and api_fetched_at is None:
        errors.append("invalid_timezone")
    if prediction_time and settlement_time and prediction_time >= settlement_time:
        errors.append("prediction_after_settlement_time")
    if prediction_time and candle_close and candle_close > prediction_time:
        errors.append("future_candle_leakage")
    compare_now = parse_aware_timestamp(now) if now else prediction_time
    if compare_now and api_fetched_at and (compare_now - api_fetched_at).total_seconds() > CRYPTO_STALE_SECONDS:
        errors.append("stale_payload")
    return sorted(set(errors))


def log_crypto_predictions(
    *,
    run_id: str,
    payload: dict[str, Any],
    prediction_timestamp: str | None = None,
) -> dict[str, Any]:
    candidates = build_crypto_prediction_candidates(payload, run_id=run_id, prediction_timestamp=prediction_timestamp)
    logged = 0
    rejected = 0
    duplicate_rows = 0
    rejection_reasons: Counter[str] = Counter()
    with _connect() as connection:
        for candidate in candidates:
            errors = validate_crypto_prediction(candidate)
            exact_duplicate = connection.execute(
                """
                SELECT 1
                FROM app.crypto_prediction_logs
                WHERE run_id = %s AND strategy = %s AND exchange = %s AND symbol = %s
                  AND horizon = %s AND side = %s AND prediction_timestamp = %s
                LIMIT 1
                """,
                (
                    run_id,
                    candidate["strategy"],
                    candidate["exchange"],
                    candidate["symbol"],
                    candidate["horizon"],
                    candidate["side"],
                    candidate["prediction_timestamp"],
                ),
            ).fetchone()
            if exact_duplicate:
                errors.append("exact_duplicate")
            latest = connection.execute(
                """
                SELECT source_snapshot_hash, entry_price, confidence_score, features_json, snapshot_sequence
                FROM app.crypto_prediction_logs
                WHERE run_id = %s AND strategy = %s AND exchange = %s AND symbol = %s
                  AND horizon = %s AND side = %s AND validation_status = 'valid'
                ORDER BY prediction_timestamp DESC, id DESC
                LIMIT 1
                """,
                (
                    run_id,
                    candidate["strategy"],
                    candidate["exchange"],
                    candidate["symbol"],
                    candidate["horizon"],
                    candidate["side"],
                ),
            ).fetchone()
            snapshot_sequence = 1
            if latest:
                snapshot_sequence = int(latest["snapshot_sequence"] or 1) + 1
                if "exact_duplicate" not in errors and (
                    latest["source_snapshot_hash"] == candidate["source_snapshot_hash"]
                    and as_decimal(latest["entry_price"]) == as_decimal(candidate["entry_price"])
                    and as_decimal(latest["confidence_score"]) == as_decimal(candidate["confidence_score"])
                    and stable_json(_json_object(latest["features_json"])) == stable_json(candidate["features"])
                ):
                    errors.append("unchanged_repeat_snapshot")
            if errors:
                rejected += 1
                reason = errors[0]
                if reason == "exact_duplicate":
                    duplicate_rows += 1
                rejection_reasons[reason] += 1
                connection.execute(
                    """
                    INSERT INTO app.crypto_prediction_rejections
                        (run_id, strategy, exchange, symbol, horizon, side, prediction_timestamp, rejection_reason, raw_log_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        run_id,
                        candidate.get("strategy"),
                        candidate.get("exchange"),
                        candidate.get("symbol"),
                        candidate.get("horizon"),
                        candidate.get("side"),
                        candidate.get("prediction_timestamp"),
                        reason,
                        stable_json(candidate),
                    ),
                )
                continue
            candidate["snapshot_sequence"] = snapshot_sequence
            cursor = connection.execute(
                """
                INSERT INTO app.crypto_prediction_logs
                    (asset_class, run_id, model_version, strategy, exchange, symbol, horizon, side,
                     prediction_timestamp, entry_time, entry_price, settlement_time, api_fetched_at,
                     source_updated_at, source_snapshot_hash, source_payload_ref, timeframe,
                     candle_open_time, candle_close_time, open, high, low, close, volume, bid, ask,
                     mid_price, spread, implied_probability, confidence_score, features_json,
                     validation_status, rejection_reason, snapshot_sequence, settlement_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (asset_class, run_id, strategy, exchange, symbol, horizon, side, prediction_timestamp)
                DO NOTHING
                """,
                (
                    "crypto",
                    run_id,
                    candidate["model_version"],
                    candidate["strategy"],
                    candidate["exchange"],
                    candidate["symbol"],
                    candidate["horizon"],
                    candidate["side"],
                    candidate["prediction_timestamp"],
                    candidate["entry_time"],
                    candidate["entry_price"],
                    candidate["settlement_time"],
                    candidate["api_fetched_at"],
                    candidate.get("source_updated_at"),
                    candidate["source_snapshot_hash"],
                    candidate.get("source_payload_ref"),
                    candidate["timeframe"],
                    candidate["candle_open_time"],
                    candidate["candle_close_time"],
                    candidate["open"],
                    candidate["high"],
                    candidate["low"],
                    candidate["close"],
                    candidate["volume"],
                    candidate.get("bid"),
                    candidate.get("ask"),
                    candidate.get("mid_price"),
                    candidate.get("spread"),
                    candidate.get("implied_probability"),
                    candidate["confidence_score"],
                    stable_json(candidate["features"]),
                    "valid",
                    None,
                    snapshot_sequence,
                    "unresolved",
                ),
            )
            if cursor.rowcount:
                logged += 1
            else:
                rejected += 1
                duplicate_rows += 1
                rejection_reasons["exact_duplicate"] += 1
        connection.commit()
    return {
        "asset_class": "crypto",
        "run_id": run_id,
        "attempted_predictions": len(candidates),
        "logged_predictions": logged,
        "rejected_predictions": rejected,
        "duplicate_rows_ignored": duplicate_rows,
        "rejection_reasons": dict(rejection_reasons),
    }


def _settlement_price_for(
    records: list[dict[str, Any]], *, exchange: str, symbol: str, settlement_time: datetime
) -> Decimal | None:
    candidates = []
    for record in records:
        if record.get("exchange") != exchange or record.get("symbol") != symbol:
            continue
        close_time = parse_aware_timestamp(record.get("candle_close_time"))
        if close_time and close_time >= settlement_time:
            close_price = as_decimal(record.get("close"))
            if close_price is not None:
                candidates.append((close_time, close_price))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def settle_crypto_predictions(*, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    records = payload.get("records") or []
    payload_time = parse_aware_timestamp(payload.get("generated_at") or utc_now_iso()) or datetime.now(timezone.utc)
    record_times = [
        parsed
        for record in records
        for parsed in [parse_aware_timestamp(record.get("candle_close_time"))]
        if parsed is not None
    ]
    now = max([payload_time, *record_times]) if record_times else payload_time
    updated = 0
    unresolved = 0
    issue_counts: Counter[str] = Counter()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM app.crypto_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid' AND settlement_state = 'unresolved'
            FOR UPDATE SKIP LOCKED
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            settlement_time = parse_aware_timestamp(row["settlement_time"])
            if settlement_time is None:
                issue_counts["invalid_timezone"] += 1
                continue
            if settlement_time > now:
                unresolved += 1
                continue
            settlement_price = _settlement_price_for(records, exchange=row["exchange"], symbol=row["symbol"], settlement_time=settlement_time)
            if settlement_price is None:
                issue_counts["settlement_price_missing"] += 1
                connection.execute(
                    "UPDATE app.crypto_prediction_logs SET settlement_issue = %s WHERE id = %s AND settlement_state = 'unresolved'",
                    ("settlement_price_missing", row["id"]),
                )
                continue
            entry = as_decimal(row["entry_price"], default=Decimal("0")) or Decimal("0")
            return_bps = ((settlement_price - entry) / entry) * Decimal("10000") if entry else Decimal("0")
            settlement_band = CRYPTO_SETTLEMENT_BAND_BPS
            if -settlement_band < return_bps < settlement_band:
                state = "push"
                actual_outcome = "no_edge"
            elif (row["side"] == "UP" and return_bps >= settlement_band) or (
                row["side"] == "DOWN" and return_bps <= -settlement_band
            ):
                state = "settled"
                actual_outcome = "win"
            else:
                state = "settled"
                actual_outcome = "loss"
            cursor = connection.execute(
                """
                UPDATE app.crypto_prediction_logs
                SET settlement_state = %s, actual_outcome = %s, settlement_price = %s, return_bps = %s,
                    settlement_updated_at = %s, settlement_source = %s, settlement_issue = NULL
                WHERE id = %s AND settlement_state = 'unresolved'
                """,
                (
                    state,
                    actual_outcome,
                    settlement_price,
                    _round_bps(return_bps),
                    utc_now_iso(),
                    "crypto_ohlcv",
                    row["id"],
                ),
            )
            updated += int(cursor.rowcount or 0)
        connection.commit()
    return {
        "asset_class": "crypto",
        "run_id": run_id,
        "rows_updated": updated,
        "unresolved_rows": unresolved,
        "settlement_issue_counts": dict(issue_counts),
    }


def _deduped_crypto_rows(connection: DatabaseSession, run_id: str, *, settled_only: bool = False) -> list[DatabaseRow]:
    state_filter = "AND settlement_state IN ('settled', 'push')" if settled_only else ""
    return connection.execute(
        f"""
        SELECT *
        FROM app.crypto_prediction_logs AS outer_row
        WHERE run_id = %s AND validation_status = 'valid' {state_filter}
          AND id = (
            SELECT id
            FROM app.crypto_prediction_logs AS inner_row
            WHERE inner_row.run_id = outer_row.run_id
              AND inner_row.strategy = outer_row.strategy
              AND inner_row.exchange = outer_row.exchange
              AND inner_row.symbol = outer_row.symbol
              AND inner_row.horizon = outer_row.horizon
              AND inner_row.side = outer_row.side
              AND inner_row.settlement_time = outer_row.settlement_time
              AND inner_row.validation_status = 'valid'
            ORDER BY prediction_timestamp ASC, id ASC
            LIMIT 1
          )
        """,
        (run_id,),
    ).fetchall()


def build_crypto_report(*, run_id: str) -> dict[str, Any]:
    with _connect() as connection:
        total_raw = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND validation_status = 'valid'",
            (run_id,),
        ).fetchone()[0]
        rejected = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_rejections WHERE run_id = %s",
            (run_id,),
        ).fetchone()[0]
        rejection_reasons = dict(
            connection.execute(
                """
                SELECT rejection_reason, COUNT(*) FROM app.crypto_prediction_rejections
                WHERE run_id = %s
                GROUP BY rejection_reason
                """,
                (run_id,),
            ).fetchall()
        )
        settled_raw = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND settlement_state IN ('settled', 'push')",
            (run_id,),
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND settlement_state = 'unresolved'",
            (run_id,),
        ).fetchone()[0]
        pushes = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND actual_outcome = 'no_edge'",
            (run_id,),
        ).fetchone()[0]
        wins = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND actual_outcome = 'win'",
            (run_id,),
        ).fetchone()[0]
        losses = connection.execute(
            "SELECT COUNT(*) FROM app.crypto_prediction_logs WHERE run_id = %s AND actual_outcome = 'loss'",
            (run_id,),
        ).fetchone()[0]
        unique_exposures = len(_deduped_crypto_rows(connection, run_id))
        settled_deduped_rows = _deduped_crypto_rows(connection, run_id, settled_only=True)
        settled_deduped = len(settled_deduped_rows)
        deduped_wins = sum(1 for row in settled_deduped_rows if row["actual_outcome"] == "win")
        deduped_losses = sum(1 for row in settled_deduped_rows if row["actual_outcome"] == "loss")
        deduped_pushes = sum(1 for row in settled_deduped_rows if row["actual_outcome"] == "no_edge")
        repeated_snapshot_groups = connection.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT strategy, exchange, symbol, horizon, side, COUNT(*) AS row_count
              FROM app.crypto_prediction_logs
              WHERE run_id = %s AND validation_status = 'valid'
              GROUP BY strategy, exchange, symbol, horizon, side
              HAVING COUNT(*) > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
        duplicate_exposures = connection.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT strategy, exchange, symbol, horizon, side, settlement_time, COUNT(*) AS row_count
              FROM app.crypto_prediction_logs
              WHERE run_id = %s AND validation_status = 'valid'
              GROUP BY strategy, exchange, symbol, horizon, side, settlement_time
              HAVING COUNT(*) > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
        avg_return_bps = connection.execute(
            "SELECT AVG(return_bps) FROM app.crypto_prediction_logs WHERE run_id = %s AND settlement_state IN ('settled', 'push')",
            (run_id,),
        ).fetchone()[0]
        exchange_rows = connection.execute(
            """
            SELECT exchange, horizon, COUNT(*) AS rows
            FROM app.crypto_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid'
            GROUP BY exchange, horizon
            ORDER BY exchange, horizon
            """,
            (run_id,),
        ).fetchall()
    accuracy_denominator = deduped_wins + deduped_losses
    directional_accuracy = None if accuracy_denominator == 0 else round(deduped_wins / accuracy_denominator, 6)
    next_automatic_action = (
        "continue private collection and review serious audit; do not start ML or model changes without controlled validation"
        if settled_deduped >= 300
        else "continue crypto-cycle until 300+ settled de-duped exposures"
    )
    return {
        "asset_class": "crypto",
        "run_id": run_id,
        "model_version": CRYPTO_MODEL_VERSION,
        "total_raw_predictions": total_raw,
        "new_valid_predictions": total_raw,
        "rejected_predictions": rejected,
        "rejection_reasons": rejection_reasons,
        "settled_predictions": settled_raw,
        "newly_settled_predictions": None,
        "unresolved_predictions": unresolved,
        "invalid_rows": 0,
        "unique_deduped_exposures": unique_exposures,
        "repeated_snapshot_groups": repeated_snapshot_groups,
        "exact_duplicates_rejected": rejection_reasons.get("exact_duplicate", 0),
        "duplicate_exposure_warnings": duplicate_exposures,
        "settled_raw_rows": settled_raw,
        "settled_deduped_exposures": settled_deduped,
        "push_no_edge_count": pushes,
        "void_count": 0,
        "raw_wins": wins,
        "raw_losses": losses,
        "deduped_wins": deduped_wins,
        "deduped_losses": deduped_losses,
        "deduped_pushes": deduped_pushes,
        "directional_accuracy": directional_accuracy,
        "average_return_bps": as_decimal(avg_return_bps),
        "metric_status": accuracy_status(settled_deduped),
        "roi_status": "ROI unavailable; no explicit fee/slippage model exists",
        "sample_size_status": sample_status(settled_deduped),
        "gate_result": gate_result(settled_deduped),
        "exchange_horizon_rows": [dict(row) for row in exchange_rows],
        "connector_status": build_connectors_status(),
        "connector_actions": {
            "google_drive_archive": "not_attempted",
            "airtable_status_sync": "not_attempted",
            "slack_alert": "not_attempted",
        },
        "blockers": [],
        "major_issues": [] if settled_deduped >= 100 else ["sample size below basic audit gate"],
        "minor_issues": ["research-only; no edge or profit claim"],
        "next_automatic_action": next_automatic_action,
    }


def render_crypto_report(report: dict[str, Any]) -> str:
    lines = [
        "Crypto Private Research Report",
        f"Asset class: {report['asset_class']}",
        f"Run ID: {report['run_id']}",
        f"Model version: {report['model_version']}",
        f"Heartbeat status: {report.get('heartbeat_status', 'material_change')}",
        "",
        f"Total raw predictions: {report['total_raw_predictions']}",
        f"Rejected predictions: {report['rejected_predictions']}",
        f"Rejection reasons: {report['rejection_reasons']}",
        f"Settled raw rows: {report['settled_raw_rows']}",
        f"Settled de-duped exposures: {report['settled_deduped_exposures']}",
        f"Unresolved predictions: {report['unresolved_predictions']}",
        f"Push/no_edge count: {report['push_no_edge_count']}",
        f"Unique de-duped exposures: {report['unique_deduped_exposures']}",
        f"Repeated snapshot groups: {report['repeated_snapshot_groups']}",
        f"Duplicate exposure warnings: {report['duplicate_exposure_warnings']}",
        "",
        f"Metric status: {report['metric_status']}",
        f"Directional accuracy: {report['directional_accuracy']}",
        f"Average return_bps: {report['average_return_bps']}",
        f"ROI status: {report['roi_status']}",
        f"Sample-size status: {report['sample_size_status']}",
        f"Gate result: {report['gate_result']}",
        "",
        "Exchange/horizon rows:",
    ]
    for row in report["exchange_horizon_rows"]:
        lines.append(f"- {row['exchange']} {row['horizon']}: {row['rows']}")
    lines.extend(["", *connector_status_report_lines(report.get("connector_status", {}))])
    lines.append(f"Connector actions: {report.get('connector_actions', {})}")
    lines.extend(
        [
            "",
            f"Source error count: {report.get('source_error_count', 0)}",
            f"Blockers: {report['blockers']}",
            f"Major issues: {report['major_issues']}",
            f"Minor issues: {report['minor_issues']}",
            f"Next automatic action: {report['next_automatic_action']}",
            "",
            "No profitability, edge, or model reliability claim is made by this report.",
        ]
    )
    return "\n".join(lines)


def write_crypto_report(report: dict[str, Any], path: str | Path) -> None:
    write_text(path, render_crypto_report(report))


def _crypto_breakdown(rows: list[DatabaseRow], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[dimension] for dimension in dimensions)
        bucket = buckets.setdefault(
            key,
            {
                **{dimension: row[dimension] for dimension in dimensions},
                "rows": 0,
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "average_return_bps": None,
                "median_return_bps": None,
                "best_return_bps": None,
                "worst_return_bps": None,
            },
        )
        bucket["rows"] += 1
        if row["actual_outcome"] == "win":
            bucket["wins"] += 1
        elif row["actual_outcome"] == "loss":
            bucket["losses"] += 1
        elif row["actual_outcome"] == "no_edge":
            bucket["pushes"] += 1
        returns = bucket.setdefault("_returns", [])
        if row["return_bps"] is not None:
            value = as_decimal(row["return_bps"])
            if value is not None:
                returns.append(value)
    for bucket in buckets.values():
        returns = bucket.pop("_returns", [])
        bucket["average_return_bps"] = None if not returns else _round_bps(sum(returns, Decimal("0")) / Decimal(len(returns)))
        bucket["median_return_bps"] = None if not returns else _round_bps(Decimal(median(returns)))
        bucket["best_return_bps"] = None if not returns else _round_bps(max(returns))
        bucket["worst_return_bps"] = None if not returns else _round_bps(min(returns))
        denominator = bucket["wins"] + bucket["losses"]
        bucket["directional_accuracy"] = None if denominator == 0 else _round_bps(Decimal(bucket["wins"]) / Decimal(denominator))
    return sorted(buckets.values(), key=lambda row: tuple(row[dimension] for dimension in dimensions))


def _crypto_return_summary(rows: list[DatabaseRow]) -> dict[str, Any]:
    returns = [value for row in rows if (value := as_decimal(row["return_bps"])) is not None]
    if not returns:
        return {"average_return_bps": None, "median_return_bps": None}
    return {
        "average_return_bps": _round_bps(sum(returns, Decimal("0")) / Decimal(len(returns))),
        "median_return_bps": _round_bps(Decimal(median(returns))),
    }


def _crypto_outcome_clusters(rows: list[DatabaseRow]) -> dict[str, list[dict[str, Any]]]:
    cluster_rows = _crypto_breakdown(rows, ("exchange", "symbol", "horizon", "side"))
    populated = [row for row in cluster_rows if row["average_return_bps"] is not None]
    return {
        "best": sorted(populated, key=lambda row: (row["average_return_bps"], row["rows"]), reverse=True)[:5],
        "worst": sorted(populated, key=lambda row: (row["average_return_bps"], -row["rows"]))[:5],
    }


def _crypto_leakage_checks(rows: list[DatabaseRow], settled_rows: list[DatabaseRow], report: dict[str, Any]) -> dict[str, Any]:
    future_candle_violations = 0
    forbidden_feature_fields: set[str] = set()
    for row in rows:
        prediction_time = parse_aware_timestamp(row["prediction_timestamp"])
        candle_close = parse_aware_timestamp(row["candle_close_time"])
        if prediction_time and candle_close and candle_close > prediction_time:
            future_candle_violations += 1
        try:
            feature_keys = set(_json_object(row["features_json"]))
        except (TypeError, json.JSONDecodeError):
            feature_keys = {"parse_failed"}
        forbidden_feature_fields.update(feature_keys.intersection(CRYPTO_FEATURE_FORBIDDEN_FIELDS))
    return {
        "unresolved_rows_excluded_from_metrics": len(settled_rows) == report["settled_deduped_exposures"],
        "rejected_rows_excluded_from_metrics": report["rejected_predictions"] == sum(report["rejection_reasons"].values()),
        "duplicate_snapshots_not_inflating_deduped_performance": report["settled_deduped_exposures"] <= report["settled_raw_rows"],
        "push_no_edge_handling": "counted as settled/push outcomes, excluded from directional accuracy denominator",
        "labels_are_settlement_only": True,
        "future_candle_leakage_count": future_candle_violations,
        "future_candle_leakage_status": "pass" if future_candle_violations == 0 else "fail",
        "feature_export_labels_separate": True,
        "feature_export_forbidden_fields": sorted(CRYPTO_FEATURE_FORBIDDEN_FIELDS),
        "feature_export_forbidden_fields_found": sorted(forbidden_feature_fields),
        "feature_export_leakage_status": "pass" if not forbidden_feature_fields else "fail",
    }


def _crypto_time_of_day_bucket(row: DatabaseRow) -> str:
    timestamp = parse_aware_timestamp(row["prediction_timestamp"])
    if timestamp is None:
        return "unknown"
    return f"{timestamp.hour:02d}:00-{timestamp.hour:02d}:59Z"


def _crypto_volatility_bucket(row: DatabaseRow) -> str:
    high = as_decimal(row["high"])
    low = as_decimal(row["low"])
    close = as_decimal(row["close"])
    if high is None or low is None or close is None:
        return "unavailable"
    if close <= 0:
        return "unavailable"
    range_bps = ((high - low) / close) * Decimal("10000")
    if range_bps < Decimal("10"):
        return "lt_10bps"
    if range_bps < Decimal("25"):
        return "10_to_25bps"
    if range_bps < Decimal("50"):
        return "25_to_50bps"
    return "gte_50bps"


def _crypto_spread_bucket(row: DatabaseRow) -> str:
    if row["spread"] is None:
        return "unavailable"
    spread = as_decimal(row["spread"])
    mid_price = as_decimal(row["mid_price"] or row["entry_price"])
    if spread is None or mid_price is None:
        return "unavailable"
    if mid_price <= 0:
        return "unavailable"
    spread_bps = (spread / mid_price) * Decimal("10000")
    if spread_bps < Decimal("1"):
        return "lt_1bps"
    if spread_bps < Decimal("2"):
        return "1_to_2bps"
    if spread_bps < Decimal("5"):
        return "2_to_5bps"
    return "gte_5bps"


def _crypto_segment_value(row: DatabaseRow, dimension: str) -> str:
    if dimension == "time_of_day_bucket":
        return _crypto_time_of_day_bucket(row)
    if dimension == "volatility_bucket":
        return _crypto_volatility_bucket(row)
    if dimension == "spread_bucket":
        return _crypto_spread_bucket(row)
    value = row[dimension]
    return "unknown" if value is None else str(value)


def _crypto_segment_key(row: DatabaseRow, dimensions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_crypto_segment_value(row, dimension) for dimension in dimensions)


def _crypto_sample_size_status(count: int) -> str:
    if count < 30:
        return "too_small_to_judge"
    if count < 100:
        return "diagnostic_only_under_basic_segment_gate"
    if count < 300:
        return "adequate_for_controlled_segment_diagnosis_research_only"
    return "large_segment_sample_research_only"


def _crypto_segment_classification(
    *,
    de_duped_settled_count: int,
    average_return_bps: float | None,
    median_return_bps: float | None,
    directional_accuracy: float | None,
    push_no_edge_rate: float | None,
    duplicate_exposure_groups: int,
) -> str:
    if de_duped_settled_count < 30:
        return "too_small_to_judge"
    if duplicate_exposure_groups > 0:
        return "contaminated_by_duplicate_exposures"
    if push_no_edge_rate is not None and push_no_edge_rate >= 0.35:
        return "push_no_edge_imbalance"
    if average_return_bps is None or median_return_bps is None:
        return "inconclusive_missing_return_metrics"
    if de_duped_settled_count >= 100 and average_return_bps < 0 and median_return_bps < 0:
        return "clearly_weak"
    if average_return_bps > 0 and median_return_bps >= 0 and (directional_accuracy is None or directional_accuracy >= 0.52):
        return "possibly_useful_future_challenger_candidate"
    if average_return_bps > 0 or (directional_accuracy is not None and directional_accuracy > 0.56):
        return "diagnostic_interest_not_actionable"
    return "mixed_inconclusive"


def _crypto_cost_sensitivity(average_return_bps: float | None, costs: tuple[int, ...] = (1, 2, 5)) -> dict[str, Any]:
    if average_return_bps is None:
        return {
            f"{cost}_bps_round_trip": {"net_average_return_bps": None, "survives_cost": False}
            for cost in costs
        }
    return {
        f"{cost}_bps_round_trip": {
            "net_average_return_bps": round(average_return_bps - cost, 6),
            "survives_cost": average_return_bps - cost > 0,
        }
        for cost in costs
    }


def _crypto_segment_table(
    raw_rows: list[DatabaseRow],
    settled_raw_rows: list[DatabaseRow],
    settled_deduped_rows: list[DatabaseRow],
    dimensions: tuple[str, ...],
) -> list[dict[str, Any]]:
    raw_counts = Counter(_crypto_segment_key(row, dimensions) for row in raw_rows)
    settled_counts = Counter(_crypto_segment_key(row, dimensions) for row in settled_raw_rows)
    deduped_counts = Counter(_crypto_segment_key(row, dimensions) for row in settled_deduped_rows)
    duplicate_exposure_counts: Counter[tuple[str, ...]] = Counter()
    repeated_snapshot_counts: Counter[tuple[str, ...]] = Counter()
    exposure_keys: dict[tuple[Any, ...], list[DatabaseRow]] = defaultdict(list)
    snapshot_keys: dict[tuple[Any, ...], list[DatabaseRow]] = defaultdict(list)
    for row in raw_rows:
        exposure_keys[
            (
                row["strategy"],
                row["exchange"],
                row["symbol"],
                row["horizon"],
                row["side"],
                row["settlement_time"],
            )
        ].append(row)
        snapshot_keys[(row["strategy"], row["exchange"], row["symbol"], row["horizon"], row["side"])].append(row)
    for rows in exposure_keys.values():
        if len(rows) > 1:
            duplicate_exposure_counts[_crypto_segment_key(rows[0], dimensions)] += 1
    for rows in snapshot_keys.values():
        if len(rows) > 1:
            repeated_snapshot_counts[_crypto_segment_key(rows[0], dimensions)] += 1
    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in settled_deduped_rows:
        key = _crypto_segment_key(row, dimensions)
        bucket = buckets.setdefault(
            key,
            {
                "segment": " | ".join(f"{dimension}={value}" for dimension, value in zip(dimensions, key)),
                **{dimension: value for dimension, value in zip(dimensions, key)},
                "raw_count": raw_counts[key],
                "settled_count": settled_counts[key],
                "de_duped_settled_count": deduped_counts[key],
                "win_count": 0,
                "loss_count": 0,
                "push_no_edge_count": 0,
                "_returns": [],
                "duplicate_exposure_groups": duplicate_exposure_counts[key],
                "repeated_snapshot_groups": repeated_snapshot_counts[key],
            },
        )
        if row["actual_outcome"] == "win":
            bucket["win_count"] += 1
        elif row["actual_outcome"] == "loss":
            bucket["loss_count"] += 1
        elif row["actual_outcome"] == "no_edge":
            bucket["push_no_edge_count"] += 1
        if (value := as_decimal(row["return_bps"])) is not None:
            bucket["_returns"].append(value)
    for key in set(raw_counts) - set(buckets):
        buckets[key] = {
            "segment": " | ".join(f"{dimension}={value}" for dimension, value in zip(dimensions, key)),
            **{dimension: value for dimension, value in zip(dimensions, key)},
            "raw_count": raw_counts[key],
            "settled_count": settled_counts[key],
            "de_duped_settled_count": deduped_counts[key],
            "win_count": 0,
            "loss_count": 0,
            "push_no_edge_count": 0,
            "_returns": [],
            "duplicate_exposure_groups": duplicate_exposure_counts[key],
            "repeated_snapshot_groups": repeated_snapshot_counts[key],
        }
    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        returns = bucket.pop("_returns")
        wins = bucket["win_count"]
        losses = bucket["loss_count"]
        pushes = bucket["push_no_edge_count"]
        denominator = wins + losses
        average_return_bps = None if not returns else _round_bps(sum(returns, Decimal("0")) / Decimal(len(returns)))
        median_return_bps = None if not returns else _round_bps(Decimal(median(returns)))
        push_rate = None if bucket["de_duped_settled_count"] == 0 else _round_bps(Decimal(pushes) / Decimal(bucket["de_duped_settled_count"]))
        directional_accuracy = None if denominator == 0 else _round_bps(Decimal(wins) / Decimal(denominator))
        bucket.update(
            {
                "directional_accuracy": directional_accuracy,
                "average_return_bps": average_return_bps,
                "median_return_bps": median_return_bps,
                "worst_return_bps": None if not returns else _round_bps(min(returns)),
                "best_return_bps": None if not returns else _round_bps(max(returns)),
                "push_no_edge_rate": push_rate,
                "sample_size_status": _crypto_sample_size_status(bucket["de_duped_settled_count"]),
                "fee_slippage_sensitivity": _crypto_cost_sensitivity(average_return_bps),
                "classification": _crypto_segment_classification(
                    de_duped_settled_count=bucket["de_duped_settled_count"],
                    average_return_bps=average_return_bps,
                    median_return_bps=median_return_bps,
                    directional_accuracy=directional_accuracy,
                    push_no_edge_rate=push_rate,
                    duplicate_exposure_groups=bucket["duplicate_exposure_groups"],
                ),
            }
        )
        rows.append(bucket)
    return sorted(rows, key=lambda row: (-row["de_duped_settled_count"], row["segment"]))


def _crypto_stage4_decision(segment_tables: dict[str, list[dict[str, Any]]], overall_average_return_bps: float | None, overall_median_return_bps: float | None) -> dict[str, Any]:
    all_segments = [row for rows in segment_tables.values() for row in rows]
    weak_segments = [
        row for row in all_segments
        if row["classification"] == "clearly_weak"
    ]
    promising_segments = [
        row for row in all_segments
        if row["classification"] == "possibly_useful_future_challenger_candidate"
    ]
    tiny_positive_segments = [
        row for row in all_segments
        if row["de_duped_settled_count"] < 100
        and row["average_return_bps"] is not None
        and row["average_return_bps"] > 0
    ]
    cost_sensitivity = _crypto_cost_sensitivity(overall_average_return_bps)
    cost_kills_overall = not any(item["survives_cost"] for item in cost_sensitivity.values())
    if overall_average_return_bps is not None and overall_median_return_bps is not None and overall_average_return_bps < 0 and overall_median_return_bps < 0:
        overall_result = "weak_or_inconclusive_negative_average_and_median_return_bps"
    else:
        overall_result = "mixed_inconclusive"
    return {
        "overall_result": overall_result,
        "fee_slippage_sensitivity": cost_sensitivity,
        "fee_slippage_decision": (
            "tested_costs_do_not_survive_average_return_bps_not_tradable_research_only"
            if cost_kills_overall
            else "some_cost_assumptions_survive_average_return_bps_research_only_not_roi"
        ),
        "suspected_weak_segments": [
            {
                "segment": row["segment"],
                "de_duped_settled_count": row["de_duped_settled_count"],
                "average_return_bps": row["average_return_bps"],
                "median_return_bps": row["median_return_bps"],
                "classification": row["classification"],
            }
            for row in sorted(weak_segments, key=lambda item: (item["average_return_bps"] or 0, -item["de_duped_settled_count"]))[:10]
        ],
        "suspected_promising_segments": [
            {
                "segment": row["segment"],
                "de_duped_settled_count": row["de_duped_settled_count"],
                "average_return_bps": row["average_return_bps"],
                "median_return_bps": row["median_return_bps"],
                "classification": row["classification"],
            }
            for row in sorted(promising_segments, key=lambda item: (-(item["average_return_bps"] or 0), -item["de_duped_settled_count"]))[:10]
        ],
        "tiny_positive_segments_do_not_act": [
            {
                "segment": row["segment"],
                "de_duped_settled_count": row["de_duped_settled_count"],
                "average_return_bps": row["average_return_bps"],
                "median_return_bps": row["median_return_bps"],
            }
            for row in sorted(tiny_positive_segments, key=lambda item: (-(item["average_return_bps"] or 0), item["de_duped_settled_count"]))[:10]
        ],
        "more_data_needed_before_rule_changes": True,
        "recommended_collection_target": "1000+ settled de-duped predictions before changing live rules if diagnostics remain mixed",
        "model_change_justified_now": False,
        "stage4_scope": "controlled diagnosis only; no ML training, model promotion, public claim, or live-rule change",
    }


def _crypto_probability_metrics(rows: list[DatabaseRow]) -> dict[str, Any]:
    probability_rows = [
        row
        for row in rows
        if row["implied_probability"] is not None and row["actual_outcome"] in {"win", "loss"}
    ]
    if not probability_rows:
        return {
            "brier_score": None,
            "brier_status": "unavailable_no_probability_predictions",
            "calibration_status": "unavailable_no_probability_predictions",
            "calibration_buckets": [],
        }
    brier_terms: list[Decimal] = []
    buckets: dict[int, dict[str, Any]] = {}
    for row in probability_rows:
        probability = max(Decimal("0"), min(Decimal("1"), as_decimal(row["implied_probability"], default=Decimal("0")) or Decimal("0")))
        outcome = Decimal("1") if row["actual_outcome"] == "win" else Decimal("0")
        brier_terms.append((probability - outcome) ** 2)
        bucket_index = min(9, int(probability * 10))
        bucket = buckets.setdefault(
            bucket_index,
            {
                "bucket": f"{bucket_index / 10:.1f}-{(bucket_index + 1) / 10:.1f}",
                "rows": 0,
                "wins": 0,
                "average_probability": Decimal("0"),
                "_probabilities": [],
            },
        )
        bucket["rows"] += 1
        bucket["wins"] += int(outcome)
        bucket["_probabilities"].append(probability)
    calibration_buckets = []
    for bucket_index in sorted(buckets):
        bucket = buckets[bucket_index]
        probabilities = bucket.pop("_probabilities")
        bucket["average_probability"] = _round_bps(sum(probabilities, Decimal("0")) / Decimal(len(probabilities)))
        bucket["observed_win_rate"] = _round_bps(Decimal(bucket["wins"]) / Decimal(bucket["rows"]))
        bucket["bucket_status"] = "ok" if bucket["rows"] >= 30 else "sample_too_small_under_30"
        calibration_buckets.append(bucket)
    return {
        "brier_score": _round_bps(sum(brier_terms, Decimal("0")) / Decimal(len(brier_terms))),
        "brier_status": "available_research_only",
        "calibration_status": "available_research_only_bucket_claims_require_n_30",
        "calibration_buckets": calibration_buckets,
    }


def _crypto_result_assessment(report: dict[str, Any]) -> str:
    settled = int(report["settled_deduped_exposures"])
    accuracy = report.get("directional_accuracy")
    average_return_bps = report.get("average_return_bps")
    if settled < 300:
        return "inconclusive_sample_below_preferred_checkpoint; continue private collection"
    if average_return_bps is None or accuracy is None:
        return "inconclusive_missing_core_metrics; continue private collection"
    if average_return_bps <= 0 and accuracy > 0.52:
        return (
            "mixed_inconclusive_worth_further_testing; directional accuracy is above coin-flip, "
            "but average return_bps is negative and ROI is unavailable without a fee/slippage model"
        )
    if average_return_bps <= 0:
        return "weak_or_inconclusive_return_profile; no edge or profitability claim"
    if accuracy <= 0.52:
        return "weak_directional_signal_despite_positive_return_bps; no model reliability claim"
    return "worth_further_testing_research_only; no edge, profitability, or public claim"


def build_crypto_stage3b_audit_report(*, run_id: str) -> dict[str, Any]:
    report = build_crypto_report(run_id=run_id)
    with _connect() as connection:
        settled_rows = _deduped_crypto_rows(connection, run_id, settled_only=True)
        valid_rows = connection.execute(
            """
            SELECT *
            FROM app.crypto_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid'
            """,
            (run_id,),
        ).fetchall()
    settled_count = len(settled_rows)
    return_summary = _crypto_return_summary(settled_rows)
    probability_metrics = _crypto_probability_metrics(settled_rows)
    calibration_bucket_sample_sizes = {
        row["bucket"]: row["rows"] for row in probability_metrics["calibration_buckets"]
    }
    duplicate_snapshot_impact = {
        "exact_duplicates_rejected": report["exact_duplicates_rejected"],
        "unchanged_repeat_snapshot_rejections": report["rejection_reasons"].get("unchanged_repeat_snapshot", 0),
        "repeated_snapshot_groups": report["repeated_snapshot_groups"],
        "duplicate_exposure_warnings": report["duplicate_exposure_warnings"],
        "metric_policy": "de-duped settled exposures are the primary audit view; repeated snapshots do not inflate readiness",
    }
    largest_wins = [
        {
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "horizon": row["horizon"],
            "side": row["side"],
            "prediction_timestamp": row["prediction_timestamp"],
            "return_bps": _round_bps(as_decimal(row["return_bps"], default=Decimal("0")) or Decimal("0")),
        }
        for row in sorted(
            [row for row in settled_rows if row["actual_outcome"] == "win" and row["return_bps"] is not None],
            key=lambda item: as_decimal(item["return_bps"], default=Decimal("0")) or Decimal("0"),
            reverse=True,
        )[:5]
    ]
    largest_losses = [
        {
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "horizon": row["horizon"],
            "side": row["side"],
            "prediction_timestamp": row["prediction_timestamp"],
            "return_bps": _round_bps(as_decimal(row["return_bps"], default=Decimal("0")) or Decimal("0")),
        }
        for row in sorted(
            [row for row in settled_rows if row["actual_outcome"] == "loss" and row["return_bps"] is not None],
            key=lambda item: as_decimal(item["return_bps"], default=Decimal("0")) or Decimal("0"),
        )[:5]
    ]
    concentration_counts = Counter((row["exchange"], row["symbol"], row["horizon"]) for row in settled_rows)
    concentration = [
        {"exchange": exchange, "symbol": symbol, "horizon": horizon, "settled_deduped_exposures": count}
        for (exchange, symbol, horizon), count in concentration_counts.most_common()
    ]
    top_concentration = concentration[0]["settled_deduped_exposures"] if concentration else 0
    concentration_risk = (
        "none_no_settled_exposures"
        if settled_count == 0
        else f"top_exchange_symbol_horizon_share={top_concentration}/{settled_count}"
    )
    if settled_count < 100:
        audit_status = "not_ready_blocked_sample_size"
        conclusion = "Insufficient settled de-duped sample for a basic audit."
    elif settled_count < 300:
        audit_status = "basic_audit_research_only_continue_to_300"
        conclusion = "Basic audit is available, but results remain research-only and not serious enough for conclusions."
    else:
        audit_status = "serious_research_audit_allowed_no_public_claim"
        conclusion = "Preferred sample size reached for a serious research audit; still no public claim without review."
    result_assessment_report = {**report, **return_summary}
    stage4_model_diagnosis_status = (
        "controlled_stage4_diagnosis_allowed_research_only_no_model_promotion"
        if settled_count >= 300
        else "blocked_until_300_settled_deduped_exposures"
    )
    model_change_recommendation = (
        "keep_collecting_and_diagnose_by_symbol_horizon_side_before_model_changes"
        if settled_count >= 300
        else "continue_collection_before_any_stage4_diagnosis"
    )
    return {
        **report,
        "stage": "Crypto Stage 3B settled performance audit",
        "audit_status": audit_status,
        "audit_conclusion": conclusion,
        "primary_view": "de-duped settled exposures",
        "total_deduped_predictions": report["unique_deduped_exposures"],
        "deduped_push_no_edge_count": sum(1 for row in settled_rows if row["actual_outcome"] == "no_edge"),
        "raw_push_no_edge_count": report["push_no_edge_count"],
        "average_return_bps": return_summary["average_return_bps"],
        "median_return_bps": return_summary["median_return_bps"],
        "result_assessment": _crypto_result_assessment(result_assessment_report),
        "stage4_model_diagnosis_status": stage4_model_diagnosis_status,
        "model_change_recommendation": model_change_recommendation,
        "brier_score": probability_metrics["brier_score"],
        "brier_status": probability_metrics["brier_status"],
        "calibration_status": probability_metrics["calibration_status"],
        "calibration_buckets": probability_metrics["calibration_buckets"],
        "calibration_bucket_sample_sizes": calibration_bucket_sample_sizes,
        "by_symbol_performance": _crypto_breakdown(settled_rows, ("symbol",)),
        "by_horizon_performance": _crypto_breakdown(settled_rows, ("horizon",)),
        "by_side_performance": _crypto_breakdown(settled_rows, ("side",)),
        "by_exchange_performance": _crypto_breakdown(settled_rows, ("exchange",)),
        "by_exchange_horizon_performance": _crypto_breakdown(settled_rows, ("exchange", "horizon")),
        "outcome_clusters": _crypto_outcome_clusters(settled_rows),
        "largest_wins": largest_wins,
        "largest_losses": largest_losses,
        "concentration": concentration,
        "concentration_risk": concentration_risk,
        "duplicate_snapshot_impact": duplicate_snapshot_impact,
        "data_quality_issues": {
            "rejection_reasons": report["rejection_reasons"],
            "duplicate_exposure_warnings": report["duplicate_exposure_warnings"],
            "repeated_snapshot_groups": report["repeated_snapshot_groups"],
            "source_error_count": report.get("source_error_count", 0),
        },
        "leakage_checks": _crypto_leakage_checks(valid_rows, settled_rows, report),
        "roi_status": "ROI unavailable; no explicit fee/slippage model exists",
        "next_automatic_action": report["next_automatic_action"],
    }


def render_crypto_stage3b_audit_report(report: dict[str, Any]) -> str:
    lines = [
        "Crypto Stage 3B Settled Performance Audit",
        f"Asset class: {report['asset_class']}",
        f"Run ID: {report['run_id']}",
        f"Model version: {report['model_version']}",
        f"Audit status: {report['audit_status']}",
        f"Primary view: {report['primary_view']}",
        "",
        f"Total raw predictions: {report['total_raw_predictions']}",
        f"Total de-duped predictions: {report['total_deduped_predictions']}",
        f"Rejected predictions: {report['rejected_predictions']}",
        f"Rejection reasons: {report['rejection_reasons']}",
        f"Settled raw rows: {report['settled_raw_rows']}",
        f"Settled de-duped exposures: {report['settled_deduped_exposures']}",
        f"Unresolved predictions: {report['unresolved_predictions']}",
        f"Raw push/no_edge count: {report['raw_push_no_edge_count']}",
        f"De-duped push/no_edge count: {report['deduped_push_no_edge_count']}",
        f"Repeated snapshot groups: {report['repeated_snapshot_groups']}",
        f"Duplicate exposure warnings: {report['duplicate_exposure_warnings']}",
        "",
        "De-duped performance (primary audit view):",
        f"- Directional accuracy: {report['directional_accuracy']}",
        f"- Average return_bps: {report['average_return_bps']}",
        f"- Median return_bps: {report['median_return_bps']}",
        f"- ROI status: {report['roi_status']}",
        f"- Metric status: {report['metric_status']}",
        f"- Sample-size status: {report['sample_size_status']}",
        f"- Gate result: {report['gate_result']}",
        f"- Brier score: {report['brier_score']}",
        f"- Brier status: {report['brier_status']}",
        f"- Calibration status: {report['calibration_status']}",
        f"- Result assessment: {report['result_assessment']}",
        "",
        "Symbol breakdown:",
    ]
    for row in report["by_symbol_performance"]:
        lines.append(
            f"- {row['symbol']}: rows={row['rows']} wins={row['wins']} losses={row['losses']} "
            f"pushes={row['pushes']} accuracy={row['directional_accuracy']} avg_return_bps={row['average_return_bps']}"
        )
    lines.extend(["", "Horizon breakdown:"])
    for row in report["by_horizon_performance"]:
        lines.append(
            f"- {row['horizon']}: rows={row['rows']} wins={row['wins']} losses={row['losses']} "
            f"pushes={row['pushes']} accuracy={row['directional_accuracy']} avg_return_bps={row['average_return_bps']}"
        )
    lines.extend(["", "Side breakdown:"])
    for row in report["by_side_performance"]:
        lines.append(
            f"- {row['side']}: rows={row['rows']} wins={row['wins']} losses={row['losses']} "
            f"pushes={row['pushes']} accuracy={row['directional_accuracy']} avg_return_bps={row['average_return_bps']} "
            f"median_return_bps={row['median_return_bps']}"
        )
    lines.extend(["", "Exchange/source breakdown:"])
    for row in report["by_exchange_performance"]:
        lines.append(
            f"- {row['exchange']}: rows={row['rows']} wins={row['wins']} losses={row['losses']} "
            f"pushes={row['pushes']} accuracy={row['directional_accuracy']} avg_return_bps={row['average_return_bps']}"
        )
    lines.extend(
        [
            "",
            "Calibration buckets:",
        ]
    )
    lines.extend(
        [
            f"- {row['bucket']}: rows={row['rows']} avg_probability={row['average_probability']} "
            f"observed_win_rate={row['observed_win_rate']} status={row['bucket_status']}"
            for row in report["calibration_buckets"]
        ]
        or ["- unavailable; no probability predictions"]
    )
    lines.append(f"Calibration bucket sample sizes: {report['calibration_bucket_sample_sizes'] or 'unavailable'}")
    lines.extend(
        [
            "",
            "By exchange/horizon:",
        ]
    )
    for row in report["by_exchange_horizon_performance"]:
        lines.append(
            f"- {row['exchange']} {row['horizon']}: rows={row['rows']} wins={row['wins']} losses={row['losses']} "
            f"pushes={row['pushes']} accuracy={row['directional_accuracy']} avg_return_bps={row['average_return_bps']}"
        )
    lines.extend(["", "Best outcome clusters:"])
    lines.extend(
        [
            f"- {row['exchange']} {row['symbol']} {row['horizon']} {row['side']}: rows={row['rows']} "
            f"wins={row['wins']} losses={row['losses']} pushes={row['pushes']} "
            f"avg_return_bps={row['average_return_bps']} median_return_bps={row['median_return_bps']} "
            f"best_return_bps={row['best_return_bps']} worst_return_bps={row['worst_return_bps']}"
            for row in report["outcome_clusters"]["best"]
        ]
        or ["- none"]
    )
    lines.extend(["", "Worst outcome clusters:"])
    lines.extend(
        [
            f"- {row['exchange']} {row['symbol']} {row['horizon']} {row['side']}: rows={row['rows']} "
            f"wins={row['wins']} losses={row['losses']} pushes={row['pushes']} "
            f"avg_return_bps={row['average_return_bps']} median_return_bps={row['median_return_bps']} "
            f"best_return_bps={row['best_return_bps']} worst_return_bps={row['worst_return_bps']}"
            for row in report["outcome_clusters"]["worst"]
        ]
        or ["- none"]
    )
    lines.extend(["", "Largest wins:"])
    lines.extend(
        [
            f"- {row['exchange']} {row['symbol']} {row['horizon']} {row['side']} {row['prediction_timestamp']} return_bps={row['return_bps']}"
            for row in report["largest_wins"]
        ]
        or ["- none"]
    )
    lines.extend(["", "Largest losses:"])
    lines.extend(
        [
            f"- {row['exchange']} {row['symbol']} {row['horizon']} {row['side']} {row['prediction_timestamp']} return_bps={row['return_bps']}"
            for row in report["largest_losses"]
        ]
        or ["- none"]
    )
    lines.extend(["", "Concentration:"])
    for row in report["concentration"][:10]:
        lines.append(f"- {row['exchange']} {row['symbol']} {row['horizon']}: {row['settled_deduped_exposures']}")
    lines.extend(
        [
            f"Concentration risk: {report['concentration_risk']}",
            "",
            f"Duplicate/repeated snapshot impact: {report['duplicate_snapshot_impact']}",
            "",
            "Validation/leakage checks:",
            f"- Unresolved rows excluded from metrics: {report['leakage_checks']['unresolved_rows_excluded_from_metrics']}",
            f"- Rejected rows excluded from metrics: {report['leakage_checks']['rejected_rows_excluded_from_metrics']}",
            f"- Duplicate snapshots not inflating de-duped performance: {report['leakage_checks']['duplicate_snapshots_not_inflating_deduped_performance']}",
            f"- Push/no_edge handling: {report['leakage_checks']['push_no_edge_handling']}",
            f"- Labels are settlement-only: {report['leakage_checks']['labels_are_settlement_only']}",
            f"- Future candle leakage count: {report['leakage_checks']['future_candle_leakage_count']} ({report['leakage_checks']['future_candle_leakage_status']})",
            f"- Feature export leakage status: {report['leakage_checks']['feature_export_leakage_status']}",
            f"- Feature export forbidden fields found: {report['leakage_checks']['feature_export_forbidden_fields_found']}",
            "",
            f"Data quality issues: {report['data_quality_issues']}",
            f"Audit conclusion: {report['audit_conclusion']}",
            f"Stage 4 model diagnosis status: {report['stage4_model_diagnosis_status']}",
            f"Model-change recommendation: {report['model_change_recommendation']}",
            f"Next automatic action: {report['next_automatic_action']}",
            "",
            "ROI is unavailable without an explicit fee/slippage model.",
            "This audit is not proof of tradable profitability.",
            "No profitability, edge, calibration, or model reliability claim is made by this audit.",
        ]
    )
    return "\n".join(lines)


def write_crypto_stage3b_audit_report(report: dict[str, Any], path: str | Path) -> None:
    write_text(path, render_crypto_stage3b_audit_report(report))


def build_crypto_stage4_diagnostic_report(*, run_id: str) -> dict[str, Any]:
    stage3b = build_crypto_stage3b_audit_report(run_id=run_id)
    with _connect() as connection:
        raw_rows = connection.execute(
            """
            SELECT *
            FROM app.crypto_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid'
            """,
            (run_id,),
        ).fetchall()
        settled_raw_rows = connection.execute(
            """
            SELECT *
            FROM app.crypto_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid' AND settlement_state IN ('settled', 'push')
            """,
            (run_id,),
        ).fetchall()
        settled_deduped_rows = _deduped_crypto_rows(connection, run_id, settled_only=True)
    segment_dimensions = {
        "symbol": ("symbol",),
        "horizon": ("horizon",),
        "side": ("side",),
        "symbol_horizon": ("symbol", "horizon"),
        "symbol_side": ("symbol", "side"),
        "horizon_side": ("horizon", "side"),
        "time_of_day_bucket": ("time_of_day_bucket",),
        "volatility_bucket": ("volatility_bucket",),
        "spread_bucket": ("spread_bucket",),
    }
    segment_tables = {
        name: _crypto_segment_table(raw_rows, settled_raw_rows, settled_deduped_rows, dimensions)
        for name, dimensions in segment_dimensions.items()
    }
    decision = _crypto_stage4_decision(
        segment_tables,
        stage3b["average_return_bps"],
        stage3b["median_return_bps"],
    )
    return {
        "stage": "Crypto Stage 4 controlled diagnostic report",
        "asset_class": "crypto",
        "run_id": run_id,
        "model_version": CRYPTO_MODEL_VERSION,
        "strategy": CRYPTO_STRATEGY,
        "generated_at": utc_now_iso(),
        "stage4_status": "controlled_diagnosis_only_no_model_change",
        "source_stage3b_audit_status": stage3b["audit_status"],
        "total_raw_predictions": stage3b["total_raw_predictions"],
        "total_deduped_predictions": stage3b["total_deduped_predictions"],
        "settled_raw_rows": stage3b["settled_raw_rows"],
        "settled_deduped_exposures": stage3b["settled_deduped_exposures"],
        "unresolved_predictions": stage3b["unresolved_predictions"],
        "rejected_predictions": stage3b["rejected_predictions"],
        "rejection_reasons": stage3b["rejection_reasons"],
        "push_no_edge_count": stage3b["deduped_push_no_edge_count"],
        "directional_accuracy": stage3b["directional_accuracy"],
        "average_return_bps": stage3b["average_return_bps"],
        "median_return_bps": stage3b["median_return_bps"],
        "roi_status": stage3b["roi_status"],
        "leakage_checks": stage3b["leakage_checks"],
        "duplicate_snapshot_impact": stage3b["duplicate_snapshot_impact"],
        "segment_tables": segment_tables,
        "decision": decision,
        "blockers": [],
        "major_issues": [
            "overall average and median return_bps are negative",
            "ROI unavailable without explicit fee/slippage model",
            "no model promotion or live-rule change justified",
        ],
        "minor_issues": [
            "calibration unavailable because strategy does not emit probability predictions",
            "spread bucket currently unavailable where source spread is missing",
            "diagnostic segmentation is research-only and may need 1000+ settled de-duped rows",
        ],
        "next_automatic_action": "continue Stage 3A collection while reviewing Stage 4 diagnostics; do not train ML or change live logic",
    }


def _render_crypto_segment_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(["", f"{title}:"])
    if not rows:
        lines.append("- none")
        return
    for row in rows:
        sensitivity = row["fee_slippage_sensitivity"]
        cost_summary = ", ".join(
            f"{name}=net {values['net_average_return_bps']} survives={values['survives_cost']}"
            for name, values in sensitivity.items()
        )
        lines.append(
            f"- {row['segment']}: raw={row['raw_count']} settled={row['settled_count']} "
            f"deduped_settled={row['de_duped_settled_count']} wins={row['win_count']} "
            f"losses={row['loss_count']} pushes={row['push_no_edge_count']} "
            f"push_rate={row['push_no_edge_rate']} accuracy={row['directional_accuracy']} "
            f"avg_return_bps={row['average_return_bps']} median_return_bps={row['median_return_bps']} "
            f"worst_return_bps={row['worst_return_bps']} best_return_bps={row['best_return_bps']} "
            f"sample={row['sample_size_status']} classification={row['classification']} "
            f"duplicate_exposure_groups={row['duplicate_exposure_groups']} "
            f"repeated_snapshot_groups={row['repeated_snapshot_groups']} cost_sensitivity=[{cost_summary}]"
        )


def render_crypto_stage4_diagnostic_report(report: dict[str, Any]) -> str:
    decision = report["decision"]
    lines = [
        "Crypto Stage 4 Controlled Diagnostic Report",
        f"Asset class: {report['asset_class']}",
        f"Run ID: {report['run_id']}",
        f"Model version: {report['model_version']}",
        f"Strategy: {report['strategy']}",
        f"Stage 4 status: {report['stage4_status']}",
        f"Generated at: {report['generated_at']}",
        "",
        "Stage 3B carry-forward:",
        f"- Source audit status: {report['source_stage3b_audit_status']}",
        f"- Total raw predictions: {report['total_raw_predictions']}",
        f"- Total de-duped predictions: {report['total_deduped_predictions']}",
        f"- Settled raw rows: {report['settled_raw_rows']}",
        f"- Settled de-duped exposures: {report['settled_deduped_exposures']}",
        f"- Unresolved predictions: {report['unresolved_predictions']}",
        f"- Rejected predictions: {report['rejected_predictions']}",
        f"- Rejection reasons: {report['rejection_reasons']}",
        f"- Push/no_edge count: {report['push_no_edge_count']}",
        f"- Directional accuracy: {report['directional_accuracy']}",
        f"- Average return_bps: {report['average_return_bps']}",
        f"- Median return_bps: {report['median_return_bps']}",
        f"- ROI status: {report['roi_status']}",
        "",
        "Fee/slippage sensitivity estimate (not ROI, not profitability):",
    ]
    for name, values in decision["fee_slippage_sensitivity"].items():
        lines.append(
            f"- {name}: net_average_return_bps={values['net_average_return_bps']} "
            f"survives_cost={values['survives_cost']}"
        )
    lines.extend(
        [
            f"- Fee/slippage decision: {decision['fee_slippage_decision']}",
            "",
            f"Overall diagnostic result: {decision['overall_result']}",
            f"More data needed before rule changes: {decision['more_data_needed_before_rule_changes']}",
            f"Recommended collection target: {decision['recommended_collection_target']}",
            f"Model change justified now: {decision['model_change_justified_now']}",
            f"Stage 4 scope: {decision['stage4_scope']}",
            "",
            f"Duplicate/repeated snapshot impact: {report['duplicate_snapshot_impact']}",
            "Validation/leakage checks:",
            f"- Unresolved rows excluded from metrics: {report['leakage_checks']['unresolved_rows_excluded_from_metrics']}",
            f"- Rejected rows excluded from metrics: {report['leakage_checks']['rejected_rows_excluded_from_metrics']}",
            f"- Duplicate snapshots not inflating de-duped performance: {report['leakage_checks']['duplicate_snapshots_not_inflating_deduped_performance']}",
            f"- Future candle leakage count: {report['leakage_checks']['future_candle_leakage_count']} ({report['leakage_checks']['future_candle_leakage_status']})",
            f"- Feature export leakage status: {report['leakage_checks']['feature_export_leakage_status']}",
            f"- Feature export forbidden fields found: {report['leakage_checks']['feature_export_forbidden_fields_found']}",
        ]
    )
    for name, title in [
        ("symbol", "Segment table: symbol"),
        ("horizon", "Segment table: horizon"),
        ("side", "Segment table: side"),
        ("symbol_horizon", "Segment table: symbol + horizon"),
        ("symbol_side", "Segment table: symbol + side"),
        ("horizon_side", "Segment table: horizon + side"),
        ("time_of_day_bucket", "Segment table: time-of-day bucket"),
        ("volatility_bucket", "Segment table: volatility bucket"),
        ("spread_bucket", "Segment table: spread bucket"),
    ]:
        _render_crypto_segment_table(lines, title, report["segment_tables"][name])
    lines.extend(["", "Suspected weak segments:"])
    lines.extend(
        [
            f"- {row['segment']}: n={row['de_duped_settled_count']} avg={row['average_return_bps']} "
            f"median={row['median_return_bps']} classification={row['classification']}"
            for row in decision["suspected_weak_segments"]
        ]
        or ["- none with adequate sample"]
    )
    lines.extend(["", "Suspected promising segments:"])
    lines.extend(
        [
            f"- {row['segment']}: n={row['de_duped_settled_count']} avg={row['average_return_bps']} "
            f"median={row['median_return_bps']} classification={row['classification']}"
            for row in decision["suspected_promising_segments"]
        ]
        or ["- none adequate enough to justify a challenger run"]
    )
    lines.extend(["", "Tiny positive segments (do not act):"])
    lines.extend(
        [
            f"- {row['segment']}: n={row['de_duped_settled_count']} avg={row['average_return_bps']} "
            f"median={row['median_return_bps']}"
            for row in decision["tiny_positive_segments_do_not_act"]
        ]
        or ["- none"]
    )
    lines.extend(
        [
            "",
            f"Blockers: {report['blockers']}",
            f"Major issues: {report['major_issues']}",
            f"Minor issues: {report['minor_issues']}",
            f"Next automatic action: {report['next_automatic_action']}",
            "",
            "No ML training was run.",
            "No live prediction logic was changed.",
            "No profitability, edge, calibration, or model reliability claim is made by this diagnostic.",
            "This diagnostic is not proof of tradable profitability.",
        ]
    )
    return "\n".join(lines)


def write_crypto_stage4_diagnostic_report(report: dict[str, Any], path: str | Path) -> None:
    write_text(path, render_crypto_stage4_diagnostic_report(report))


def apply_crypto_source_status(report: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    errors = payload.get("errors") or []
    if errors and not payload.get("records"):
        report = dict(report)
        report["heartbeat_status"] = "blocked_by_source"
        report["blockers"] = sorted({str(error.get("error") or "source_fetch_error") for error in errors})
        report["source_error_count"] = len(errors)
        report["source_errors"] = errors
        report["next_automatic_action"] = "retry crypto-cycle when network/source access is available"
    else:
        report.setdefault("heartbeat_status", "material_change" if payload.get("records") else "no_material_change")
        report.setdefault("source_error_count", len(errors))
        report.setdefault("source_errors", errors)
    return report


def export_crypto_features(*, run_id: str, output: str | Path, labels_output: str | Path | None = None) -> dict[str, Any]:
    feature_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    with _connect() as connection:
        rows = _deduped_crypto_rows(connection, run_id, settled_only=True)
        for row in rows:
            features = _json_object(row["features_json"])
            if CRYPTO_FEATURE_FORBIDDEN_FIELDS.intersection(features):
                raise ValueError("crypto feature export contains leakage fields")
            de_dupe_key = stable_json(
                {
                    "asset_class": "crypto",
                    "strategy": row["strategy"],
                    "exchange": row["exchange"],
                    "symbol": row["symbol"],
                    "horizon": row["horizon"],
                    "side": row["side"],
                    "settlement_time": row["settlement_time"],
                }
            )
            feature_rows.append(
                {
                    "asset_class": "crypto",
                    "run_id": run_id,
                    "model_version": row["model_version"],
                    "strategy": row["strategy"],
                    "exchange": row["exchange"],
                    "symbol": row["symbol"],
                    "horizon": row["horizon"],
                    "side": row["side"],
                    "prediction_timestamp": row["prediction_timestamp"],
                    "api_fetched_at": row["api_fetched_at"],
                    "source_updated_at": row["source_updated_at"],
                    "source_snapshot_hash": row["source_snapshot_hash"],
                    "de_dupe_key": de_dupe_key,
                    "entry_price": row["entry_price"],
                    "confidence_score": row["confidence_score"],
                    "feature_open": features.get("open"),
                    "feature_high": features.get("high"),
                    "feature_low": features.get("low"),
                    "feature_close": features.get("close"),
                    "feature_volume": features.get("volume"),
                    "feature_last_candle_return_bps": features.get("last_candle_return_bps"),
                }
            )
            label_rows.append(
                {
                    "de_dupe_key": de_dupe_key,
                    "actual_outcome": row["actual_outcome"],
                    "settlement_price": row["settlement_price"],
                    "return_bps": row["return_bps"],
                }
            )
    feature_fields = [
        "asset_class",
        "run_id",
        "model_version",
        "strategy",
        "exchange",
        "symbol",
        "horizon",
        "side",
        "prediction_timestamp",
        "api_fetched_at",
        "source_updated_at",
        "source_snapshot_hash",
        "de_dupe_key",
        "entry_price",
        "confidence_score",
        "feature_open",
        "feature_high",
        "feature_low",
        "feature_close",
        "feature_volume",
        "feature_last_candle_return_bps",
    ]
    write_csv(output, feature_rows, feature_fields)
    if labels_output:
        write_csv(labels_output, label_rows, ["de_dupe_key", "actual_outcome", "settlement_price", "return_bps"])
    return {"feature_rows": len(feature_rows), "label_rows": len(label_rows), "output": str(output), "labels_output": str(labels_output) if labels_output else None}


def crypto_cycle(*, run_id: str, output: str | Path | None = None) -> dict[str, Any]:
    payload = collect_crypto_payload()
    output_path = Path(output) if output else default_crypto_payload_path(run_id)
    write_crypto_payload(output_path, payload)
    log_result = log_crypto_predictions(run_id=run_id, payload=payload)
    settle_result = settle_crypto_predictions(run_id=run_id, payload=payload)
    daily_report_path = default_crypto_daily_report_path(run_id)
    all_report_path = default_crypto_all_report_path(run_id)
    report = apply_crypto_source_status(build_crypto_report(run_id=run_id), payload)
    report = apply_post_report_connectors(
        report,
        report_paths=[daily_report_path, all_report_path],
        bot_name="crypto",
        asset_class="crypto",
        run_id=run_id,
        stage="Stage 3A/3B",
        mode="private_research",
    )
    write_crypto_report(report, daily_report_path)
    write_crypto_report(report, all_report_path)
    return {
        "payload_path": str(output_path),
        "log_result": log_result,
        "settle_result": settle_result,
        "report": report,
    }
