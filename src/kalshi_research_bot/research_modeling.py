from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .collection_ledger import canonical_json, canonical_timestamp, content_hash
from .database import DatabaseSettings, connection_pool


MODEL_NAME = "kalshi_market_consensus_baseline"
MODEL_VERSION = "v1"
FEATURE_SCHEMA_VERSION = "kalshi_market_snapshot_v1"
METRIC_VERSION = "baseline_coverage_v1"
SOURCE_NAME = "kalshi_public_api"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _utc_timestamp(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    timestamp = canonical_timestamp(value)
    if timestamp is None:
        raise ValueError("research_baseline_as_of_time_required")
    return timestamp


def _implied_probability(row: Any) -> tuple[Decimal | None, Decimal | None]:
    bid = row.get("yes_bid")
    ask = row.get("yes_ask")
    last = row.get("last_price")
    if bid is not None and ask is not None:
        probability = (Decimal(bid) + Decimal(ask)) / Decimal("2")
        spread = Decimal(ask) - Decimal(bid)
    elif ask is not None:
        probability = Decimal(ask)
        spread = None
    elif bid is not None:
        probability = Decimal(bid)
        spread = None
    elif last is not None:
        probability = Decimal(last)
        spread = None
    else:
        return None, None
    if probability < 0 or probability > 1:
        return None, None
    if spread is not None and spread < 0:
        return None, None
    return probability, spread


def refresh_market_consensus_baseline(
    *,
    run_id: str,
    settings: DatabaseSettings | None = None,
    as_of_time: datetime | str | None = None,
    maximum_age_seconds: int | None = None,
    maximum_markets: int | None = None,
) -> dict[str, Any]:
    """Persist a point-in-time, market-only baseline into the research ledger.

    This is deliberately not an independent predictive model. It copies the
    latest fresh market consensus into versioned feature and prediction rows,
    records zero edge, and marks every decision no_edge. The result makes
    normalized research lineage observable without creating a betting claim.
    """

    configured = settings or DatabaseSettings.from_env()
    as_of = _utc_timestamp(as_of_time)
    max_age = maximum_age_seconds or _bounded_int(
        "RESEARCH_BASELINE_MAX_AGE_SECONDS", 1800, 60, 86400
    )
    market_limit = maximum_markets or _bounded_int(
        "RESEARCH_BASELINE_MAX_MARKETS", 1000, 1, 10000
    )
    code_commit = str(os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local").strip() or "local"
    with connection_pool(configured).connection() as connection:
        health = connection.execute(
            """
            SELECT freshness_state, last_successful_at, freshness_deadline
            FROM ops.source_health
            WHERE source = %s
            """,
            (SOURCE_NAME,),
        ).fetchone()
        source_state = str(health["freshness_state"] if health else "missing")
        if (
            health
            and health.get("freshness_deadline")
            and health["freshness_deadline"] < datetime.fromisoformat(as_of)
        ):
            source_state = "stale"
        if source_state != "fresh":
            return {
                "created": False,
                "records_processed": 0,
                "no_material_change": True,
                "source_freshness_state": source_state,
                "source_fresh_at": health.get("last_successful_at") if health else None,
                "data_fresh_at": None,
                "model_state": "baseline_only",
                "reason": f"kalshi_source_not_fresh:{source_state}",
            }

        rows = connection.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (observations.market_id)
                       observations.id AS observation_id,
                       observations.market_id,
                       observations.observed_at,
                       observations.source_received_at,
                       observations.status,
                       observations.yes_bid,
                       observations.yes_ask,
                       observations.last_price,
                       observations.volume,
                       observations.volume_24h,
                       observations.open_interest,
                       observations.liquidity,
                       observations.raw_payload_id,
                       markets.market_ticker,
                       payloads.content_hash AS raw_content_hash
                FROM core.market_observations AS observations
                JOIN core.markets AS markets ON markets.id = observations.market_id
                JOIN raw.source_payloads AS payloads ON payloads.id = observations.raw_payload_id
                WHERE LOWER(markets.status) IN ('active', 'open')
                  AND LOWER(observations.status) IN ('active', 'open')
                  AND observations.observed_at >= %s::timestamptz - (%s * INTERVAL '1 second')
                  AND observations.observed_at <= %s::timestamptz
                ORDER BY observations.market_id, observations.observed_at DESC, observations.id DESC
            )
            SELECT *
            FROM latest
            ORDER BY observed_at DESC, market_id
            LIMIT %s
            """,
            (as_of, max_age, as_of, market_limit),
        ).fetchall()

        usable: list[tuple[Any, Decimal, Decimal | None, str]] = []
        for row in rows:
            probability, spread = _implied_probability(row)
            if probability is None:
                continue
            snapshot_hash = content_hash(
                {
                    "market_ticker": row["market_ticker"],
                    "observed_at": row["observed_at"],
                    "raw_content_hash": row["raw_content_hash"],
                    "yes_bid": row.get("yes_bid"),
                    "yes_ask": row.get("yes_ask"),
                    "last_price": row.get("last_price"),
                    "volume": row.get("volume"),
                    "volume_24h": row.get("volume_24h"),
                    "open_interest": row.get("open_interest"),
                    "liquidity": row.get("liquidity"),
                }
            )
            usable.append((row, probability, spread, snapshot_hash))

        if not usable:
            return {
                "created": False,
                "records_processed": 0,
                "no_material_change": True,
                "source_freshness_state": source_state,
                "source_fresh_at": health.get("last_successful_at") if health else None,
                "data_fresh_at": None,
                "model_state": "baseline_only",
                "reason": "no_fresh_open_markets",
            }

        data_cutoff = max(row["observed_at"] for row, _, _, _ in usable)
        dataset_hash = content_hash(
            [
                {
                    "market_id": int(row["market_id"]),
                    "snapshot_hash": snapshot_hash,
                }
                for row, _, _, snapshot_hash in sorted(usable, key=lambda item: int(item[0]["market_id"]))
            ]
        )
        # Serialize creation for this exact model and dataset. A completed-run
        # lookup alone is not idempotent: two workers can both see no row and
        # then append duplicate prediction lineage. The transaction lock stays
        # held through all writes and releases on commit or rollback.
        # The configured statement timeout also bounds this wait. An overlong
        # overlap fails the worker cycle for retry; it must never bypass the
        # lock or claim a completed refresh merely to report success.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"research-baseline:{MODEL_NAME}:{MODEL_VERSION}:{code_commit}:{dataset_hash}",),
        )
        model_configuration = canonical_json(
            {
                "kind": "market_consensus_baseline",
                "independent_model": False,
                "decision_status": "no_edge",
                "probability": "midpoint_of_yes_bid_and_yes_ask_with_one_sided_fallback",
            }
        )
        model = connection.execute(
            """
            INSERT INTO research.model_versions (
                model_name, model_version, code_commit, configuration,
                feature_schema_version, promotion_status
            ) VALUES (%s, %s, %s, %s::jsonb, %s, 'baseline_only')
            ON CONFLICT (model_name, model_version, code_commit) DO NOTHING
            RETURNING id
            """,
            (MODEL_NAME, MODEL_VERSION, code_commit, model_configuration, FEATURE_SCHEMA_VERSION),
        ).fetchone()
        if model is None:
            model = connection.execute(
                """
                SELECT id, configuration, feature_schema_version, promotion_status
                FROM research.model_versions
                WHERE model_name = %s
                  AND model_version = %s
                  AND code_commit = %s
                """,
                (MODEL_NAME, MODEL_VERSION, code_commit),
            ).fetchone()
            expected_configuration = model_configuration
            actual_configuration = canonical_json(model["configuration"]) if model else None
            if (
                model is None
                or actual_configuration != expected_configuration
                or model["feature_schema_version"] != FEATURE_SCHEMA_VERSION
                or model["promotion_status"] != "baseline_only"
            ):
                raise RuntimeError("research_model_version_content_conflict")
        model_version_id = int(model["id"])

        existing = connection.execute(
            """
            SELECT id, status
            FROM research.prediction_runs
            WHERE model_version_id = %s
              AND configuration ->> 'dataset_hash' = %s
              AND status IN ('completed', 'completed_with_rejections')
            ORDER BY id DESC
            LIMIT 1
            """,
            (model_version_id, dataset_hash),
        ).fetchone()
        if existing is not None:
            return {
                "created": False,
                "prediction_run_id": int(existing["id"]),
                "records_processed": 0,
                "no_material_change": True,
                "predictions_written": 0,
                "source_freshness_state": source_state,
                "source_fresh_at": health.get("last_successful_at") if health else None,
                "data_fresh_at": data_cutoff,
                "model_state": "baseline_only",
                "dataset_hash": dataset_hash,
            }

        run_configuration = canonical_json(
            {
                "dataset_hash": dataset_hash,
                "requested_run_id": str(run_id),
                "maximum_age_seconds": max_age,
                "maximum_markets": market_limit,
                "independent_model": False,
            }
        )
        prediction_run = connection.execute(
            """
            INSERT INTO research.prediction_runs (
                model_version_id, run_type, started_at, as_of_time,
                data_cutoff_time, code_commit, configuration, status
            ) VALUES (%s, 'forward', %s, %s, %s, %s, %s::jsonb, 'started')
            RETURNING id
            """,
            (model_version_id, as_of, as_of, data_cutoff, code_commit, run_configuration),
        ).fetchone()
        prediction_run_id = int(prediction_run["id"])
        predictions_written = 0
        for row, probability, spread, snapshot_hash in usable:
            features = canonical_json(
                {
                    "observation_id": int(row["observation_id"]),
                    "market_ticker": row["market_ticker"],
                    "yes_bid": row.get("yes_bid"),
                    "yes_ask": row.get("yes_ask"),
                    "last_price": row.get("last_price"),
                    "volume": row.get("volume"),
                    "volume_24h": row.get("volume_24h"),
                    "open_interest": row.get("open_interest"),
                    "liquidity": row.get("liquidity"),
                    "baseline_only": True,
                }
            )
            feature = connection.execute(
                """
                INSERT INTO research.feature_snapshots (
                    market_id, feature_time, source_cutoff_time,
                    feature_schema_version, source_data_hash,
                    market_implied_probability, spread, liquidity, feature_values
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (market_id, feature_time, feature_schema_version, source_data_hash)
                DO NOTHING
                RETURNING id
                """,
                (
                    int(row["market_id"]),
                    row["observed_at"],
                    row["observed_at"],
                    FEATURE_SCHEMA_VERSION,
                    snapshot_hash,
                    probability,
                    spread,
                    row.get("liquidity"),
                    features,
                ),
            ).fetchone()
            if feature is None:
                feature = connection.execute(
                    """
                    SELECT id
                    FROM research.feature_snapshots
                    WHERE market_id = %s
                      AND feature_time = %s
                      AND feature_schema_version = %s
                      AND source_data_hash = %s
                    """,
                    (
                        int(row["market_id"]),
                        row["observed_at"],
                        FEATURE_SCHEMA_VERSION,
                        snapshot_hash,
                    ),
                ).fetchone()
            if feature is None:
                raise RuntimeError("research_feature_snapshot_missing")
            inserted = connection.execute(
                """
                INSERT INTO research.predictions (
                    prediction_run_id, market_id, feature_snapshot_id,
                    predicted_yes_probability, market_implied_probability,
                    calculated_edge, decision_status, rejection_reason,
                    source_freshness_state
                ) VALUES (%s, %s, %s, %s, %s, 0, 'no_edge',
                          'market_consensus_baseline_only', 'fresh')
                ON CONFLICT (prediction_run_id, market_id) DO NOTHING
                RETURNING id
                """,
                (
                    prediction_run_id,
                    int(row["market_id"]),
                    int(feature["id"]),
                    probability,
                    probability,
                ),
            ).fetchone()
            predictions_written += int(inserted is not None)

        completed_at = _utc_timestamp(datetime.now(timezone.utc))
        connection.execute(
            """
            UPDATE research.prediction_runs
            SET completed_at = %s, status = 'completed'
            WHERE id = %s
            """,
            (completed_at, prediction_run_id),
        )
        connection.execute(
            """
            INSERT INTO research.metric_results (
                metric_name, metric_version, run_identifier, segment,
                sample_count, value, calculated_at, data_cutoff_time
            ) VALUES (
                'sample_size', %s, %s, %s::jsonb,
                %s, %s, %s, %s
            )
            ON CONFLICT (metric_name, metric_version, run_identifier, segment)
            DO NOTHING
            """,
            (
                METRIC_VERSION,
                f"{MODEL_NAME}:{dataset_hash}",
                canonical_json(
                    {
                        "model_state": "baseline_only",
                        "decision_status": "no_edge",
                        "independent_model": False,
                    }
                ),
                predictions_written,
                predictions_written,
                completed_at,
                data_cutoff,
            ),
        )
        return {
            "created": True,
            "prediction_run_id": prediction_run_id,
            "records_processed": predictions_written,
            "no_material_change": predictions_written == 0,
            "predictions_written": predictions_written,
            "source_freshness_state": source_state,
            "source_fresh_at": health.get("last_successful_at") if health else None,
            "data_fresh_at": data_cutoff,
            "model_state": "baseline_only",
            "dataset_hash": dataset_hash,
        }
