from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from ..database import json_default


DecimalInput = Decimal | int | float | str
ZERO = Decimal("0")
ONE = Decimal("1")
HALF = Decimal("0.5")
EPSILON = Decimal("1e-15")
CONFIDENCE_Z = Decimal("1.96")

MODEL_STATES = {
    "experimental",
    "insufficient_sample",
    "failed_validation",
    "baseline_only",
    "validated_research",
    "drift_detected",
    "disabled",
}

CATEGORY_POLICIES: dict[str, dict[str, Any]] = {
    "sports": {"minimum_test_rows": 100, "feature_namespace": "sports"},
    "crypto": {"minimum_test_rows": 100, "feature_namespace": "crypto"},
    "weather": {"minimum_test_rows": 100, "feature_namespace": "weather"},
    "economics": {"minimum_test_rows": 100, "feature_namespace": "economics"},
    "politics": {"minimum_test_rows": 100, "feature_namespace": "politics"},
    "company": {"minimum_test_rows": 100, "feature_namespace": "company"},
    "event": {"minimum_test_rows": 100, "feature_namespace": "event"},
}

TARGET_LEAKAGE_FIELDS = {
    "actual_outcome",
    "closed_price",
    "closing_price",
    "final_price",
    "final_score",
    "future_return",
    "gross_return",
    "is_winner",
    "label",
    "net_return",
    "outcome",
    "payout",
    "profit",
    "profit_loss",
    "profit_loss_cents",
    "result",
    "settlement",
    "settlement_state",
    "settlement_value",
    "target",
    "winner",
}


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _probability(value: DecimalInput, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}_outside_unit_interval")
    try:
        probability = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}_outside_unit_interval") from exc
    if not probability.is_finite() or probability < ZERO or probability > ONE:
        raise ValueError(f"{field_name}_outside_unit_interval")
    return probability


@dataclass(frozen=True)
class EvaluationRecord:
    record_id: str
    category: str
    prediction_timestamp: str
    outcome: int
    market_probability: DecimalInput
    model_probability: DecimalInput | None = None
    settlement_timestamp: str | None = None
    model_version: str = "unversioned"
    feature_version: str = "unversioned"
    features: Mapping[str, Any] = field(default_factory=dict)
    feature_observed_at: Mapping[str, str] = field(default_factory=dict)

    def normalized(self) -> "EvaluationRecord":
        category = self.category.strip().lower()
        if category not in CATEGORY_POLICIES:
            raise ValueError(f"unsupported_category:{category or 'missing'}")
        if int(self.outcome) not in {0, 1}:
            raise ValueError("outcome_must_be_binary")
        prediction_timestamp = _parse_timestamp(self.prediction_timestamp).isoformat()
        settlement_timestamp = None
        if self.settlement_timestamp:
            settlement = _parse_timestamp(self.settlement_timestamp)
            if settlement <= _parse_timestamp(prediction_timestamp):
                raise ValueError("settlement_not_after_prediction")
            settlement_timestamp = settlement.isoformat()
        model_probability = None
        if self.model_probability is not None:
            model_probability = _probability(self.model_probability, field_name="model_probability")
        return EvaluationRecord(
            record_id=str(self.record_id),
            category=category,
            prediction_timestamp=prediction_timestamp,
            outcome=int(self.outcome),
            market_probability=_probability(self.market_probability, field_name="market_probability"),
            model_probability=model_probability,
            settlement_timestamp=settlement_timestamp,
            model_version=str(self.model_version or "unversioned"),
            feature_version=str(self.feature_version or "unversioned"),
            features=dict(self.features),
            feature_observed_at=dict(self.feature_observed_at),
        )


def _iter_feature_paths(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            path = f"{prefix}.{key}" if prefix else key
            yield path
            yield from _iter_feature_paths(nested, path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            yield from _iter_feature_paths(nested, f"{prefix}[{index}]")


def leakage_failures(record: EvaluationRecord) -> list[str]:
    normalized = record.normalized()
    failures: list[str] = []
    for path in _iter_feature_paths(normalized.features):
        leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf in TARGET_LEAKAGE_FIELDS:
            failures.append(f"target_leakage_field:{path}")
    prediction_time = _parse_timestamp(normalized.prediction_timestamp)
    for feature_name, observed_at in normalized.feature_observed_at.items():
        try:
            observed_time = _parse_timestamp(observed_at)
        except (TypeError, ValueError):
            failures.append(f"invalid_feature_timestamp:{feature_name}")
            continue
        if observed_time > prediction_time:
            failures.append(f"future_feature:{feature_name}")
    return sorted(set(failures))


def dataset_version(records: Sequence[EvaluationRecord]) -> str:
    payload = [
        {
            "record_id": record.record_id,
            "category": record.category,
            "prediction_timestamp": record.prediction_timestamp,
            "outcome": record.outcome,
            "market_probability": record.market_probability,
            "model_probability": record.model_probability,
            "model_version": record.model_version,
            "feature_version": record.feature_version,
            "features": record.features,
            "feature_observed_at": record.feature_observed_at,
        }
        for record in sorted(records, key=lambda item: (item.prediction_timestamp, item.record_id))
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=json_default).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def time_aware_split(
    records: Sequence[EvaluationRecord],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> dict[str, list[EvaluationRecord]]:
    if train_fraction <= 0 or validation_fraction <= 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("invalid_split_fractions")
    ordered = sorted((record.normalized() for record in records), key=lambda item: (item.prediction_timestamp, item.record_id))
    if len({record.record_id for record in ordered}) != len(ordered):
        raise ValueError("duplicate_record_id")
    train_end = max(1, int(len(ordered) * train_fraction))
    validation_end = max(train_end + 1, int(len(ordered) * (train_fraction + validation_fraction)))
    validation_end = min(validation_end, max(0, len(ordered) - 1))
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }


def walk_forward_splits(
    records: Sequence[EvaluationRecord],
    *,
    minimum_train_rows: int,
    test_rows: int,
    step_rows: int | None = None,
) -> list[dict[str, list[EvaluationRecord]]]:
    if minimum_train_rows < 1 or test_rows < 1:
        raise ValueError("walk_forward_sizes_must_be_positive")
    step = step_rows or test_rows
    if step < 1:
        raise ValueError("walk_forward_step_must_be_positive")
    ordered = sorted((record.normalized() for record in records), key=lambda item: (item.prediction_timestamp, item.record_id))
    folds: list[dict[str, list[EvaluationRecord]]] = []
    start = minimum_train_rows
    while start + test_rows <= len(ordered):
        folds.append({"train": ordered[:start], "test": ordered[start : start + test_rows]})
        start += step
    return folds


def _mean_interval(values: Sequence[Decimal], *, confidence_z: Decimal = CONFIDENCE_Z) -> list[Decimal] | None:
    if not values:
        return None
    mean = sum(values, ZERO) / Decimal(len(values))
    if len(values) == 1:
        return [mean, mean]
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(len(values) - 1)
    margin = confidence_z * (variance / Decimal(len(values))).sqrt()
    return [mean - margin, mean + margin]


def wilson_interval(successes: int, total: int, *, confidence_z: Decimal = CONFIDENCE_Z) -> list[Decimal] | None:
    if total <= 0:
        return None
    decimal_total = Decimal(total)
    proportion = Decimal(successes) / decimal_total
    denominator = ONE + confidence_z**2 / decimal_total
    center = (proportion + confidence_z**2 / (Decimal("2") * decimal_total)) / denominator
    margin = confidence_z * (
        proportion * (ONE - proportion) / decimal_total + confidence_z**2 / (Decimal("4") * decimal_total**2)
    ).sqrt() / denominator
    return [max(ZERO, center - margin), min(ONE, center + margin)]


def calibration_buckets(
    probabilities: Sequence[DecimalInput], outcomes: Sequence[int], *, bucket_count: int = 10
) -> list[dict[str, Any]]:
    if len(probabilities) != len(outcomes):
        raise ValueError("probability_outcome_length_mismatch")
    if bucket_count < 1:
        raise ValueError("bucket_count_must_be_positive")
    normalized_probabilities = [_probability(value, field_name="probability") for value in probabilities]
    normalized_outcomes = [int(value) for value in outcomes]
    if any(value not in {0, 1} for value in normalized_outcomes):
        raise ValueError("outcome_must_be_binary")
    buckets: list[dict[str, Any]] = []
    for index in range(bucket_count):
        lower = Decimal(index) / Decimal(bucket_count)
        upper = Decimal(index + 1) / Decimal(bucket_count)
        members = [
            (probability, outcome)
            for probability, outcome in zip(normalized_probabilities, normalized_outcomes)
            if probability >= lower and (probability < upper or (index == bucket_count - 1 and probability <= upper))
        ]
        if not members:
            buckets.append(
                {
                    "bucket": f"{lower:.1f}-{upper:.1f}",
                    "count": 0,
                    "mean_probability": None,
                    "observed_rate": None,
                    "absolute_gap": None,
                }
            )
            continue
        mean_probability = sum((member[0] for member in members), ZERO) / Decimal(len(members))
        observed_rate = Decimal(sum(member[1] for member in members)) / Decimal(len(members))
        buckets.append(
            {
                "bucket": f"{lower:.1f}-{upper:.1f}",
                "count": len(members),
                "mean_probability": mean_probability,
                "observed_rate": observed_rate,
                "absolute_gap": abs(mean_probability - observed_rate),
            }
        )
    return buckets


def probability_metrics(probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> dict[str, Any]:
    if len(probabilities) != len(outcomes):
        raise ValueError("probability_outcome_length_mismatch")
    if not probabilities:
        return {
            "sample_size": 0,
            "brier_score": None,
            "brier_score_ci95": None,
            "log_loss": None,
            "log_loss_ci95": None,
            "calibration_error": None,
            "accuracy": None,
            "accuracy_ci95": None,
            "calibration_buckets": calibration_buckets([], []),
        }
    normalized_probabilities = [_probability(value, field_name="probability") for value in probabilities]
    normalized_outcomes = [int(value) for value in outcomes]
    if any(value not in {0, 1} for value in normalized_outcomes):
        raise ValueError("outcome_must_be_binary")
    brier_losses = [(probability - outcome) ** 2 for probability, outcome in zip(normalized_probabilities, normalized_outcomes)]
    log_losses = [
        -min(ONE - EPSILON, max(EPSILON, probability)).ln()
        if outcome
        else -(ONE - min(ONE - EPSILON, max(EPSILON, probability))).ln()
        for probability, outcome in zip(normalized_probabilities, normalized_outcomes)
    ]
    correct = sum((probability >= HALF) == bool(outcome) for probability, outcome in zip(normalized_probabilities, normalized_outcomes))
    buckets = calibration_buckets(normalized_probabilities, normalized_outcomes)
    calibration_error = sum(
        Decimal(bucket["count"]) / Decimal(len(normalized_probabilities)) * bucket["absolute_gap"]
        for bucket in buckets
        if bucket["count"]
    )
    return {
        "sample_size": len(normalized_probabilities),
        "brier_score": sum(brier_losses, ZERO) / Decimal(len(brier_losses)),
        "brier_score_ci95": _mean_interval(brier_losses),
        "log_loss": sum(log_losses, ZERO) / Decimal(len(log_losses)),
        "log_loss_ci95": _mean_interval(log_losses),
        "calibration_error": calibration_error,
        "accuracy": Decimal(correct) / Decimal(len(normalized_probabilities)),
        "accuracy_ci95": wilson_interval(correct, len(normalized_probabilities)),
        "calibration_buckets": buckets,
    }


def paired_probability_improvement(
    baseline_probabilities: Sequence[DecimalInput],
    challenger_probabilities: Sequence[DecimalInput],
    outcomes: Sequence[int],
) -> dict[str, Any]:
    if len(baseline_probabilities) != len(challenger_probabilities) or len(baseline_probabilities) != len(outcomes):
        raise ValueError("paired_probability_length_mismatch")
    if not outcomes:
        return {
            "sample_size": 0,
            "brier_improvement": None,
            "brier_improvement_ci95": None,
            "log_loss_improvement": None,
            "log_loss_improvement_ci95": None,
            "brier_skill_score": None,
        }
    baseline = [_probability(value, field_name="baseline_probability") for value in baseline_probabilities]
    challenger = [_probability(value, field_name="challenger_probability") for value in challenger_probabilities]
    normalized_outcomes = [int(value) for value in outcomes]
    if any(value not in {0, 1} for value in normalized_outcomes):
        raise ValueError("outcome_must_be_binary")
    baseline_brier_losses = [
        (probability - outcome) ** 2
        for probability, outcome in zip(baseline, normalized_outcomes)
    ]
    challenger_brier_losses = [
        (probability - outcome) ** 2
        for probability, outcome in zip(challenger, normalized_outcomes)
    ]
    baseline_log_losses = [
        -min(ONE - EPSILON, max(EPSILON, probability)).ln()
        if outcome
        else -(ONE - min(ONE - EPSILON, max(EPSILON, probability))).ln()
        for probability, outcome in zip(baseline, normalized_outcomes)
    ]
    challenger_log_losses = [
        -min(ONE - EPSILON, max(EPSILON, probability)).ln()
        if outcome
        else -(ONE - min(ONE - EPSILON, max(EPSILON, probability))).ln()
        for probability, outcome in zip(challenger, normalized_outcomes)
    ]
    brier_differences = [
        baseline_loss - challenger_loss
        for baseline_loss, challenger_loss in zip(baseline_brier_losses, challenger_brier_losses)
    ]
    log_loss_differences = [
        baseline_loss - challenger_loss
        for baseline_loss, challenger_loss in zip(baseline_log_losses, challenger_log_losses)
    ]
    baseline_brier = sum(baseline_brier_losses, ZERO) / Decimal(len(baseline_brier_losses))
    brier_improvement = sum(brier_differences, ZERO) / Decimal(len(brier_differences))
    log_loss_improvement = sum(log_loss_differences, ZERO) / Decimal(len(log_loss_differences))
    return {
        "sample_size": len(normalized_outcomes),
        "brier_improvement": brier_improvement,
        "brier_improvement_ci95": _mean_interval(brier_differences),
        "log_loss_improvement": log_loss_improvement,
        "log_loss_improvement_ci95": _mean_interval(log_loss_differences),
        "brier_skill_score": None if baseline_brier == ZERO else brier_improvement / baseline_brier,
    }


class HistoricalBaseRateModel:
    def __init__(self) -> None:
        self.probability: Decimal | None = None

    def fit(self, records: Sequence[EvaluationRecord]) -> None:
        if not records:
            raise ValueError("base_rate_requires_training_rows")
        self.probability = Decimal(sum(record.outcome for record in records)) / Decimal(len(records))

    def predict(self, records: Sequence[EvaluationRecord]) -> list[Decimal]:
        if self.probability is None:
            raise RuntimeError("base_rate_model_not_fitted")
        return [self.probability for _ in records]


class HistogramCalibrator:
    def __init__(self, *, bucket_count: int = 10, minimum_bucket_rows: int = 5) -> None:
        self.bucket_count = bucket_count
        self.minimum_bucket_rows = minimum_bucket_rows
        self.global_rate: Decimal | None = None
        self.bucket_rates: dict[int, Decimal] = {}

    def fit(self, probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> None:
        if len(probabilities) != len(outcomes) or not probabilities:
            raise ValueError("calibrator_requires_aligned_training_rows")
        self.global_rate = Decimal(sum(int(outcome) for outcome in outcomes)) / Decimal(len(outcomes))
        grouped: dict[int, list[int]] = {}
        for probability, outcome in zip(probabilities, outcomes):
            index = min(self.bucket_count - 1, int(_probability(probability, field_name="probability") * Decimal(self.bucket_count)))
            grouped.setdefault(index, []).append(int(outcome))
        self.bucket_rates = {
            index: Decimal(sum(values)) / Decimal(len(values))
            for index, values in grouped.items()
            if len(values) >= self.minimum_bucket_rows
        }

    def predict(self, probabilities: Sequence[DecimalInput]) -> list[Decimal]:
        if self.global_rate is None:
            raise RuntimeError("calibrator_not_fitted")
        calibrated = []
        for probability in probabilities:
            normalized = _probability(probability, field_name="probability")
            index = min(self.bucket_count - 1, int(normalized * Decimal(self.bucket_count)))
            calibrated.append(self.bucket_rates.get(index, normalized))
        return calibrated


def _model_probabilities(records: Sequence[EvaluationRecord]) -> list[Decimal] | None:
    if any(record.model_probability is None for record in records):
        return None
    return [record.model_probability for record in records if record.model_probability is not None]


def _period(records: Sequence[EvaluationRecord]) -> dict[str, Any]:
    if not records:
        return {"start": None, "end": None, "sample_size": 0}
    return {
        "start": records[0].prediction_timestamp,
        "end": records[-1].prediction_timestamp,
        "sample_size": len(records),
    }


def _candidate_set(
    train: Sequence[EvaluationRecord],
    validation: Sequence[EvaluationRecord],
) -> tuple[dict[str, list[Decimal]], dict[str, Any]]:
    validation_candidates: dict[str, list[Decimal]] = {
        "market_implied": [record.market_probability for record in validation],
    }
    fitted: dict[str, Any] = {}
    base_rate = HistoricalBaseRateModel()
    base_rate.fit(train)
    validation_candidates["historical_base_rate"] = base_rate.predict(validation)
    fitted["historical_base_rate"] = base_rate
    train_model = _model_probabilities(train)
    validation_model = _model_probabilities(validation)
    if train_model is None or validation_model is None:
        return validation_candidates, fitted
    validation_candidates["category_model"] = validation_model
    calibrator = HistogramCalibrator()
    calibrator.fit(train_model, [record.outcome for record in train])
    validation_candidates["calibrated_category_model"] = calibrator.predict(validation_model)
    fitted["calibrator"] = calibrator
    for model_weight, name in (
        (Decimal("0.25"), "market_model_ensemble_0.25"),
        (Decimal("0.5"), "market_model_ensemble_0.50"),
        (Decimal("0.75"), "market_model_ensemble_0.75"),
    ):
        market_weight = ONE - model_weight
        validation_candidates[name] = [
            market_weight * market_probability + model_weight * model_probability
            for market_probability, model_probability in zip(validation_candidates["market_implied"], validation_model)
        ]
        fitted[name] = model_weight
    return validation_candidates, fitted


def _test_predictions(
    name: str,
    records: Sequence[EvaluationRecord],
    fitted: Mapping[str, Any],
) -> list[Decimal]:
    if name == "market_implied":
        return [record.market_probability for record in records]
    if name == "historical_base_rate":
        return fitted[name].predict(records)
    model_probabilities = _model_probabilities(records)
    if model_probabilities is None:
        raise ValueError("selected_model_missing_probabilities")
    if name == "category_model":
        return model_probabilities
    if name == "calibrated_category_model":
        return fitted["calibrator"].predict(model_probabilities)
    if name.startswith("market_model_ensemble_"):
        model_weight = _probability(fitted[name], field_name="model_weight")
        return [
            (ONE - model_weight) * record.market_probability + model_weight * model_probability
            for record, model_probability in zip(records, model_probabilities)
        ]
    raise ValueError(f"unknown_model:{name}")


def evaluate_category_model(
    records: Sequence[EvaluationRecord],
    *,
    category: str,
    minimum_test_rows: int | None = None,
    minimum_brier_improvement: DecimalInput = Decimal("0.005"),
    minimum_log_loss_improvement: DecimalInput = Decimal("0.005"),
    disabled: bool = False,
) -> dict[str, Any]:
    normalized_category = category.strip().lower()
    if normalized_category not in CATEGORY_POLICIES:
        raise ValueError(f"unsupported_category:{normalized_category or 'missing'}")
    normalized = [record.normalized() for record in records]
    if any(record.category != normalized_category for record in normalized):
        raise ValueError("mixed_or_mismatched_categories")
    leakage = {
        record.record_id: failures
        for record in normalized
        if (failures := leakage_failures(record))
    }
    versions = {
        "model_versions": sorted({record.model_version for record in normalized}),
        "feature_versions": sorted({record.feature_version for record in normalized}),
        "dataset_version": dataset_version(normalized),
    }
    evaluated_at = datetime.now(timezone.utc).isoformat()
    if disabled:
        return {
            "category": normalized_category,
            "model_state": "disabled",
            "reason": "disabled_by_configuration",
            "leakage_failures": leakage,
            "evaluated_at": evaluated_at,
            **versions,
        }
    if leakage:
        return {
            "category": normalized_category,
            "model_state": "failed_validation",
            "reason": "leakage_detected",
            "leakage_failures": leakage,
            "evaluated_at": evaluated_at,
            **versions,
        }
    if len(versions["model_versions"]) > 1:
        return {
            "category": normalized_category,
            "model_state": "failed_validation",
            "reason": "mixed_model_versions",
            "sample_size": len(normalized),
            "leakage_failures": {},
            "evaluated_at": evaluated_at,
            **versions,
        }
    if len(versions["feature_versions"]) > 1:
        return {
            "category": normalized_category,
            "model_state": "failed_validation",
            "reason": "mixed_feature_versions",
            "sample_size": len(normalized),
            "leakage_failures": {},
            "evaluated_at": evaluated_at,
            **versions,
        }
    if len(normalized) < 3:
        return {
            "category": normalized_category,
            "model_state": "insufficient_sample",
            "reason": "requires_train_validation_test_rows",
            "sample_size": len(normalized),
            "leakage_failures": {},
            "evaluated_at": evaluated_at,
            **versions,
        }
    split = time_aware_split(normalized)
    train = split["train"]
    validation = split["validation"]
    test = split["test"]
    required_test_rows = minimum_test_rows or int(CATEGORY_POLICIES[normalized_category]["minimum_test_rows"])
    periods = {name: _period(rows) for name, rows in split.items()}
    validation_candidates, fitted = _candidate_set(train, validation)
    validation_outcomes = [record.outcome for record in validation]
    validation_metrics = {
        name: probability_metrics(probabilities, validation_outcomes)
        for name, probabilities in validation_candidates.items()
    }
    challenger_names = [name for name in validation_candidates if name not in {"market_implied", "historical_base_rate"}]
    selected_challenger = None
    if challenger_names:
        selected_challenger = min(
            challenger_names,
            key=lambda name: (
                validation_metrics[name]["brier_score"],
                validation_metrics[name]["log_loss"],
                validation_metrics[name]["calibration_error"],
            ),
        )
    test_outcomes = [record.outcome for record in test]
    baseline_predictions = _test_predictions("market_implied", test, fitted)
    baseline_metrics = probability_metrics(baseline_predictions, test_outcomes)
    base_rate_metrics = probability_metrics(_test_predictions("historical_base_rate", test, fitted), test_outcomes)
    challenger_metrics = None
    if selected_challenger:
        challenger_metrics = probability_metrics(_test_predictions(selected_challenger, test, fitted), test_outcomes)
    result: dict[str, Any] = {
        "category": normalized_category,
        "model_state": "baseline_only",
        "reason": "no_category_model_probabilities",
        "sample_size": len(normalized),
        "minimum_test_rows": required_test_rows,
        "periods": periods,
        "selected_challenger": selected_challenger,
        "validation_metrics": validation_metrics,
        "test_metrics": {
            "market_implied": baseline_metrics,
            "historical_base_rate": base_rate_metrics,
        },
        "leakage_failures": {},
        "evaluated_at": evaluated_at,
        **versions,
    }
    if challenger_metrics is not None and selected_challenger is not None:
        result["test_metrics"][selected_challenger] = challenger_metrics
        challenger_predictions = _test_predictions(selected_challenger, test, fitted)
        paired_improvement = paired_probability_improvement(
            baseline_predictions,
            challenger_predictions,
            test_outcomes,
        )
        brier_improvement = paired_improvement["brier_improvement"]
        log_loss_improvement = paired_improvement["log_loss_improvement"]
        calibration_change = challenger_metrics["calibration_error"] - baseline_metrics["calibration_error"]
        result["baseline_comparison"] = {
            **paired_improvement,
            "calibration_error_change": calibration_change,
            "probability_differences": [
                model_probability - record.market_probability
                for record, model_probability in zip(test, challenger_predictions)
            ],
        }
        brier_improvement_ci = paired_improvement["brier_improvement_ci95"] or [ZERO, ZERO]
        log_loss_improvement_ci = paired_improvement["log_loss_improvement_ci95"] or [ZERO, ZERO]
        if len(test) < required_test_rows:
            result["model_state"] = "insufficient_sample"
            result["reason"] = f"test_sample_below_threshold:{len(test)}/{required_test_rows}"
        elif (
            brier_improvement >= _probability(minimum_brier_improvement, field_name="minimum_brier_improvement")
            and log_loss_improvement >= _probability(minimum_log_loss_improvement, field_name="minimum_log_loss_improvement")
            and brier_improvement_ci[0] > ZERO
            and log_loss_improvement_ci[0] > ZERO
            and calibration_change <= Decimal("0.02")
        ):
            result["model_state"] = "validated_research"
            result["reason"] = "out_of_sample_baseline_improvement"
        else:
            result["model_state"] = "failed_validation"
            result["reason"] = "challenger_did_not_beat_market_baseline"
    elif len(test) < required_test_rows:
        result["model_state"] = "insufficient_sample"
        result["reason"] = f"test_sample_below_threshold:{len(test)}/{required_test_rows}"
    return result


def persist_category_evaluation(
    records: Sequence[EvaluationRecord],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    from ..business_store import create_store

    normalized = [record.normalized() for record in records]
    split = time_aware_split(normalized) if len(normalized) >= 3 else {"train": normalized, "validation": [], "test": []}
    identity_payload = {
        "category": result.get("category"),
        "dataset_version": result.get("dataset_version"),
        "model_versions": result.get("model_versions"),
        "feature_versions": result.get("feature_versions"),
        "selected_challenger": result.get("selected_challenger"),
    }
    identity = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"), default=json_default).encode("utf-8")
    ).hexdigest()
    evaluation_id = f"evaluation:{identity}"
    selected = result.get("selected_challenger") or "market_implied"
    test_metrics = (result.get("test_metrics") or {}).get(selected) or (result.get("test_metrics") or {}).get("market_implied") or {}
    accuracy_ci = test_metrics.get("accuracy_ci95") or [None, None]
    periods = result.get("periods") or {}
    model_version = ",".join(result.get("model_versions") or ["unversioned"])
    feature_version = ",".join(result.get("feature_versions") or ["unversioned"])
    store = create_store()
    inserted_predictions = 0
    with store.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO app.model_evaluations
                (evaluation_id, category, model_state, model_version, dataset_version,
                 feature_version, baseline_name, selected_model, evaluation_timestamp,
                 training_start, training_end, validation_start, validation_end,
                 test_start, test_end, sample_size, brier_score, log_loss,
                 calibration_error, accuracy, accuracy_ci_low, accuracy_ci_high,
                 evidence_json)
            VALUES (%s, %s, %s, %s, %s, %s, 'market_implied', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (evaluation_id) DO NOTHING
            """,
            (
                evaluation_id,
                result.get("category"),
                result.get("model_state"),
                model_version,
                result.get("dataset_version"),
                feature_version,
                selected,
                result.get("evaluated_at") or datetime.now(timezone.utc).isoformat(),
                (periods.get("train") or {}).get("start"),
                (periods.get("train") or {}).get("end"),
                (periods.get("validation") or {}).get("start"),
                (periods.get("validation") or {}).get("end"),
                (periods.get("test") or {}).get("start"),
                (periods.get("test") or {}).get("end"),
                int(result.get("sample_size") or len(normalized)),
                test_metrics.get("brier_score"),
                test_metrics.get("log_loss"),
                test_metrics.get("calibration_error"),
                test_metrics.get("accuracy"),
                accuracy_ci[0],
                accuracy_ci[1],
                json.dumps(dict(result), sort_keys=True, default=json_default),
            ),
        )
        evaluation_inserted = int(cursor.rowcount or 0) > 0
        for split_name, split_records in split.items():
            rows = [
                (
                    evaluation_id,
                    record.record_id,
                    split_name,
                    record.prediction_timestamp,
                    record.model_probability,
                    record.market_probability,
                    None if record.model_probability is None else record.model_probability - record.market_probability,
                    None if record.outcome is None else bool(record.outcome),
                    record.model_version,
                    result.get("dataset_version"),
                    record.feature_version,
                )
                for record in split_records
            ]
            if not rows:
                continue
            prediction_cursor = connection.executemany(
                """
                INSERT INTO app.model_evaluation_predictions
                    (evaluation_id, record_id, split_name, prediction_timestamp,
                     model_probability, market_implied_probability, probability_difference,
                     actual_outcome, model_version, dataset_version, feature_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (evaluation_id, record_id, split_name) DO NOTHING
                """,
                rows,
            )
            inserted_predictions += max(0, int(prediction_cursor.rowcount or 0))
    return {
        "evaluation_id": evaluation_id,
        "evaluation_inserted": evaluation_inserted,
        "prediction_rows_inserted": inserted_predictions,
        "idempotent": True,
    }


def detect_probability_drift(
    reference_probabilities: Sequence[DecimalInput],
    reference_outcomes: Sequence[int],
    recent_probabilities: Sequence[DecimalInput],
    recent_outcomes: Sequence[int],
    *,
    brier_degradation_threshold: DecimalInput = Decimal("0.03"),
    calibration_degradation_threshold: DecimalInput = Decimal("0.03"),
) -> dict[str, Any]:
    reference = probability_metrics(reference_probabilities, reference_outcomes)
    recent = probability_metrics(recent_probabilities, recent_outcomes)
    brier_change = recent["brier_score"] - reference["brier_score"]
    calibration_change = recent["calibration_error"] - reference["calibration_error"]
    drifted = (
        brier_change >= _probability(brier_degradation_threshold, field_name="brier_degradation_threshold")
        or calibration_change >= _probability(calibration_degradation_threshold, field_name="calibration_degradation_threshold")
    )
    return {
        "model_state": "drift_detected" if drifted else "experimental",
        "brier_change": brier_change,
        "calibration_error_change": calibration_change,
        "reference_metrics": reference,
        "recent_metrics": recent,
    }
