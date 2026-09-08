from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .business_store import create_store
from .evaluation.model_validation import wilson_interval
from .database import DatabaseSession


MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE = 100
UNRESOLVED_STATES = {"", "unresolved", "open", "active", "pending", "unknown", "none"}
PUSH_OR_VOID_STATES = {"push", "void", "canceled", "cancelled", "no_edge", "fair_market", "early_exit"}

# The record's fixed prose, named rather than inlined at the return statement.
#
# These strings are read by more than the renderer: the browser fixture stands
# in for this producer and has to say what it says. Written twice they drift --
# the fixture carried its own paraphrase of the guardrail and a `next_action`
# this module never returns, and both read as deliberate. Named once, the
# fixture imports them and the drift cannot be written.
METRIC_POLICY = (
    "Research-only. Hit rate is sample-gated and uses settled win/loss rows only; "
    "unresolved, rejected, invalid, push/no-edge, and duplicate exposure rows are excluded from performance claims."
)
NEXT_ACTION = "Keep collecting and settling; do not tune or train from unresolved/rejected rows."
TRACK_METRIC_GUARDRAIL = (
    "rejected, invalid, unresolved, and repeated exposure rows are not allowed to inflate hit-rate decisions"
)
MISSING_TABLE_GUARDRAIL = "database table missing; no performance metric is available"


TRACK_SPECS = (
    {
        "bot_name": "Kalshi Slip Engine",
        "asset_class": "kalshi",
        "table": "prediction_logs",
        "rejection_table": "prediction_rejections",
        "dedupe_fields": ("market_id", "side", "strategy"),
        "win_state_field": "settlement_state",
        "actual_field": "actual_outcome",
    },
    {
        "bot_name": "Crypto Research Bot",
        "asset_class": "crypto",
        "table": "crypto_prediction_logs",
        "rejection_table": "crypto_prediction_rejections",
        "dedupe_fields": ("exchange", "symbol", "horizon", "side", "settlement_time"),
        "win_state_field": "actual_outcome",
        "actual_field": "actual_outcome",
    },
    {
        "bot_name": "Sports Odds Research Bot",
        "asset_class": "sports",
        "table": "sports_prediction_logs",
        "rejection_table": "sports_prediction_rejections",
        "dedupe_fields": ("event_id", "market_type", "selection", "line", "bookmaker"),
        "win_state_field": "actual_outcome",
        "actual_field": "actual_outcome",
    },
)


def build_research_record(
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        store = create_store()
        with store.connect() as connection:
            tracks = [_build_track_record(connection, spec) for spec in TRACK_SPECS]
    except Exception as exc:
        return {
            "status": "WATCH",
            "db_available": False,
            "message": f"Research database is unavailable: {type(exc).__name__}",
            "metric_policy": METRIC_POLICY,
            "tracks": [],
            "current_slip_rationale": _current_slip_rationale(payload or {}),
        }
    return {
        "status": "OK" if any(track["valid_rows"] for track in tracks) else "WATCH",
        "db_available": True,
        "metric_policy": METRIC_POLICY,
        "tracks": tracks,
        "current_slip_rationale": _current_slip_rationale(payload or {}),
        "next_action": NEXT_ACTION,
    }


def _build_track_record(connection: DatabaseSession, spec: dict[str, Any]) -> dict[str, Any]:
    if not _table_exists(connection, str(spec["table"])):
        return _missing_track(spec, "prediction_table_missing")
    columns = _table_columns(connection, str(spec["table"]))
    selected_columns = [
        column
        for column in {
            "id",
            "validation_status",
            "settlement_state",
            "actual_outcome",
            "profit_loss_cents",
            "return_bps",
            *spec["dedupe_fields"],
        }
        if column in columns
    ]
    rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT {', '.join(selected_columns)} FROM app.{spec['table']}"
        ).fetchall()
    ]
    valid_rows = [row for row in rows if str(row.get("validation_status") or "valid").lower() == "valid"]
    invalid_log_rows = len(rows) - len(valid_rows)
    wins = 0
    losses = 0
    push_no_edge = 0
    unresolved = 0
    settled_rows: list[dict[str, Any]] = []
    for row in valid_rows:
        state = _normalized_outcome(row, str(spec["win_state_field"]))
        if state in UNRESOLVED_STATES:
            unresolved += 1
            continue
        settled_rows.append(row)
    deduped_settled_rows = _dedupe_rows(settled_rows, spec["dedupe_fields"])
    for row in deduped_settled_rows:
        state = _normalized_outcome(row, str(spec["win_state_field"]))
        if state == "win":
            wins += 1
        elif state == "loss":
            losses += 1
        elif state in PUSH_OR_VOID_STATES:
            push_no_edge += 1
        else:
            push_no_edge += 1
    win_loss_count = wins + losses
    rejected_rows, rejection_reasons = _rejection_summary(connection, spec)
    deduped_settled = len(deduped_settled_rows)
    sample_ready = win_loss_count >= MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE
    observed_hit_rate = round(wins / win_loss_count, 6) if sample_ready and win_loss_count else None
    observed_hit_rate_raw = round(wins / win_loss_count, 6) if win_loss_count else None
    # A hit rate without its interval invites the reader to believe the decimals.
    # At 104 settled rows a 66% rate carries roughly +/-9 points, so quoting it
    # to four significant figures overstates the evidence by three orders of
    # magnitude -- exactly what every other number on this platform is careful
    # not to do. Wilson rather than normal-approximation because it stays inside
    # [0, 1] and behaves at the small samples this gate is designed to catch.
    interval = wilson_interval(wins, win_loss_count) if win_loss_count else None
    observed_hit_rate_interval = (
        [float(interval[0]), float(interval[1])] if interval is not None else None
    )
    return {
        "bot_name": spec["bot_name"],
        "asset_class": spec["asset_class"],
        "valid_rows": len(valid_rows),
        "settled_rows": len(settled_rows),
        "deduped_settled_exposures": deduped_settled,
        "unresolved_rows": unresolved,
        "invalid_log_rows": invalid_log_rows,
        "rejected_rows": rejected_rows,
        "rejection_reasons": rejection_reasons,
        "wins": wins,
        "losses": losses,
        "push_no_edge_or_void": push_no_edge,
        "win_loss_count": win_loss_count,
        "observed_hit_rate": observed_hit_rate,
        "observed_hit_rate_raw": observed_hit_rate_raw,
        "observed_hit_rate_interval": observed_hit_rate_interval,
        "hit_rate_status": hit_rate_status(win_loss_count),
        "sample_gate_required": MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE,
        "dedupe_policy": " + ".join(spec["dedupe_fields"]),
        "metric_guardrail": TRACK_METRIC_GUARDRAIL,
    }


def _missing_track(spec: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "bot_name": spec["bot_name"],
        "asset_class": spec["asset_class"],
        "valid_rows": 0,
        "settled_rows": 0,
        "deduped_settled_exposures": 0,
        "unresolved_rows": 0,
        "invalid_log_rows": 0,
        "rejected_rows": 0,
        "rejection_reasons": {reason: 1},
        "wins": 0,
        "losses": 0,
        "push_no_edge_or_void": 0,
        "win_loss_count": 0,
        "observed_hit_rate": None,
        "observed_hit_rate_interval": None,
        "observed_hit_rate_raw": None,
        "hit_rate_status": hit_rate_status(0),
        "sample_gate_required": MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE,
        "dedupe_policy": " + ".join(spec["dedupe_fields"]),
        "metric_guardrail": MISSING_TABLE_GUARDRAIL,
    }


def _rejection_summary(connection: DatabaseSession, spec: dict[str, Any]) -> tuple[int, dict[str, int]]:
    table = str(spec["rejection_table"])
    if not _table_exists(connection, table):
        return 0, {}
    columns = _table_columns(connection, table)
    reasons: Counter[str] = Counter()
    if "rejection_reason" in columns:
        rows = connection.execute(f"SELECT rejection_reason FROM app.{table}").fetchall()
        for row in rows:
            reasons[str(row["rejection_reason"] or "unknown_rejection")] += 1
        return sum(reasons.values()), dict(sorted(reasons.items()))
    if "validation_errors_json" in columns:
        rows = connection.execute(f"SELECT validation_errors_json FROM app.{table}").fetchall()
        rejected_count = 0
        for row in rows:
            rejected_count += 1
            try:
                raw = row["validation_errors_json"]
                values = raw if isinstance(raw, list) else json.loads(str(raw or "[]"))
            except (TypeError, json.JSONDecodeError):
                values = ["parse_failed_validation_errors"]
            if not values:
                values = ["unknown_rejection"]
            for value in values:
                reasons[str(value)] += 1
        return rejected_count, dict(sorted(reasons.items()))
    count = int(connection.execute(f"SELECT COUNT(*) FROM app.{table}").fetchone()[0])
    return count, {"unknown_rejection": count} if count else {}


def _current_slip_rationale(payload: dict[str, Any]) -> list[dict[str, Any]]:
    slip_specs = (
        ("80% Slip", "custom_slip"),
        ("75% Leverage", "leverage_slip"),
        ("All-Day", "all_day_slip"),
        ("Research Edge", "research_edge_slip"),
    )
    rows = []
    for label, key in slip_specs:
        slip = payload.get(key) or {}
        rows.append(
            {
                "label": label,
                "action": slip.get("action") or "NO_SLIP",
                "leg_count": int(slip.get("leg_count") or 0),
                "eligible_leg_count": int(slip.get("eligible_leg_count") or 0),
                "skipped_overlap_count": int(slip.get("skipped_overlap_count") or 0),
                "min_leg_probability": slip.get("min_leg_probability") or slip.get("min_research_probability"),
                "max_leg_probability": slip.get("max_leg_probability"),
                "combo_probability": slip.get("adjusted_probability"),
                "estimated_payout_if_right": slip.get("estimated_payout_if_right"),
                "reason": slip.get("reason") or slip.get("note") or "live filters, overlap control, and timestamped source data",
            }
        )
    return rows


def hit_rate_status(win_loss_count: int) -> str:
    """The record's word for what may be said about a track's hit rate.

    Public because it is the rule, not an implementation detail: anything that
    claims to speak for this record -- the browser fixture included -- has to
    reach the same three answers from the same input rather than restate them.
    """

    if win_loss_count == 0:
        return "unavailable / no settled rows"
    if win_loss_count < MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE:
        return f"withheld / sample too small ({win_loss_count}/{MIN_SETTLED_WIN_LOSS_FOR_HIT_RATE})"
    return "available / research-only settled win-loss rows"


def _normalized_outcome(row: dict[str, Any], preferred_field: str) -> str:
    value = row.get(preferred_field)
    if value in {1, True}:
        return "win"
    if value in {0, False}:
        return "loss"
    text = str(value or row.get("settlement_state") or row.get("actual_outcome") or "").strip().lower()
    if text == "settled":
        actual = str(row.get("actual_outcome") or "").strip().lower()
        return actual or "settled"
    return text


def _dedupe_key(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(str(row.get(field) or "") for field in fields)
    if any(values):
        return values
    return (str(row.get("id") or ""),)


def _dedupe_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        deduped[_dedupe_key(row, fields)] = row
    return list(deduped.values())


def _table_exists(connection: DatabaseSession, table_name: str) -> bool:
    row = connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS present",
        (f"app.{table_name}",),
    ).fetchone()
    return bool(row and row["present"])


def _table_columns(connection: DatabaseSession, table_name: str) -> set[str]:
    return {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = %s
            """,
            (table_name,),
        ).fetchall()
    }
