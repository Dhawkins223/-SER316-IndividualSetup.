"""Probability calibration.

A model that says 70% should be right about 70% of the time. Discrimination
(ranking) and calibration (level) are different properties, and for a
price-comparison system calibration is the one that decides whether a stated
edge is real: a 60% forecast that lands 50% of the time does not lose slowly, it
loses on every comparison against a fair price.

Walsh and Joshi (2024), "Machine learning for sports betting: Should model
selection be based on accuracy or calibration?" (Machine Learning with
Applications), select NBA models by calibration rather than accuracy and report
materially different outcomes, which is the evidence behind treating calibration
as a first-class selection criterion here rather than a post-hoc cosmetic.

Three calibrators are implemented because the right one depends on sample size:

- ``PlattCalibrator``   two-parameter logistic fit on the logit. Data-efficient,
  and the standard choice on small calibration sets.
- ``BetaCalibrator``    three-parameter family of Kull, Silva Filho and Flach
  (2017). Parametric like Platt, but able to fit skewed miscalibration and to
  represent the identity map exactly, which Platt cannot.
- ``IsotonicCalibrator`` non-parametric, monotone, no shape assumption. The
  strongest fit when there is enough data, and a reliable overfitter when there
  is not; Niculescu-Mizil and Caruana (2005) put the practical crossover around
  a thousand rows.

Two rules are enforced rather than documented and hoped for:

1. A calibrator must be fit on rows disjoint from the rows used to measure it.
   Fitting and scoring on the same predictions makes any calibrator look good.
2. ``select_calibrator`` includes the identity map as a candidate and keeps it
   unless a calibrator beats it on out-of-fold log loss. Recalibration is a
   change that has to earn its place, not a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import exp, log
from typing import Any, Sequence

from .model_validation import DecimalInput, calibration_buckets

ZERO = Decimal("0")
ONE = Decimal("1")

_EPSILON = 1e-12
_MAX_ITERATIONS = 100
_CONVERGENCE_TOLERANCE = 1e-11
_RIDGE = 1e-8

DEFAULT_MINIMUM_CALIBRATION_ROWS = 200
DEFAULT_ISOTONIC_MINIMUM_ROWS = 1000


def _as_float(value: DecimalInput, *, field_name: str = "probability") -> float:
    number = float(value)
    if number != number:
        raise ValueError(f"{field_name}_must_be_a_number")
    return number


def _clipped(probability: float) -> float:
    return min(1.0 - _EPSILON, max(_EPSILON, probability))


def _logit(probability: float) -> float:
    clipped = _clipped(probability)
    return log(clipped / (1.0 - clipped))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + exp(-value))
    scaled = exp(value)
    return scaled / (1.0 + scaled)


def _aligned(probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> tuple[list[float], list[int]]:
    if len(probabilities) != len(outcomes):
        raise ValueError("probability_outcome_length_mismatch")
    if not probabilities:
        raise ValueError("calibration_requires_rows")
    cleaned_outcomes = [int(outcome) for outcome in outcomes]
    if any(outcome not in {0, 1} for outcome in cleaned_outcomes):
        raise ValueError("outcome_must_be_binary")
    return [_as_float(value) for value in probabilities], cleaned_outcomes


def log_loss(probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> Decimal:
    """Mean negative log likelihood. Lower is better; it punishes confident errors."""

    cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
    total = 0.0
    for probability, outcome in zip(cleaned_probabilities, cleaned_outcomes):
        clipped = _clipped(probability)
        total -= log(clipped) if outcome == 1 else log(1.0 - clipped)
    return Decimal(str(total / len(cleaned_probabilities)))


def expected_calibration_error(
    probabilities: Sequence[DecimalInput], outcomes: Sequence[int], *, bucket_count: int = 10
) -> Decimal:
    """Sample-weighted mean gap between stated probability and observed rate.

    ECE depends on the bucket count, so it is a comparison tool at a fixed bucket
    count and never an absolute quality score.
    """

    buckets = calibration_buckets(probabilities, outcomes, bucket_count=bucket_count)
    total = sum(bucket["count"] for bucket in buckets)
    if total == 0:
        return ZERO
    weighted = sum(
        Decimal(bucket["count"]) * bucket["absolute_gap"] for bucket in buckets if bucket["absolute_gap"] is not None
    )
    return weighted / Decimal(total)


def maximum_calibration_error(
    probabilities: Sequence[DecimalInput], outcomes: Sequence[int], *, bucket_count: int = 10
) -> Decimal:
    """Worst single-bucket gap. Surfaces a broken region an averaged ECE hides."""

    buckets = calibration_buckets(probabilities, outcomes, bucket_count=bucket_count)
    gaps = [bucket["absolute_gap"] for bucket in buckets if bucket["absolute_gap"] is not None]
    return max(gaps) if gaps else ZERO


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for the small Newton systems."""

    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot_row][column]) < 1e-14:
            return None
        augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        pivot = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / pivot
            if factor == 0.0:
                continue
            for position in range(column, size + 1):
                augmented[row][position] -= factor * augmented[column][position]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        total = augmented[row][size]
        for column in range(row + 1, size):
            total -= augmented[row][column] * solution[column]
        solution[row] = total / augmented[row][row]
    return solution


def _fit_logistic(design: list[list[float]], outcomes: list[int]) -> tuple[list[float], bool]:
    """Newton-Raphson logistic regression with a small ridge for stability.

    The ridge keeps separable calibration sets, which do occur when a model is
    confident and correct on every row of a fold, from producing an unbounded
    fit.
    """

    parameter_count = len(design[0])
    coefficients = [0.0] * parameter_count
    converged = False
    for _ in range(_MAX_ITERATIONS):
        gradient = [0.0] * parameter_count
        hessian = [[_RIDGE if row == column else 0.0 for column in range(parameter_count)] for row in range(parameter_count)]
        for features, outcome in zip(design, outcomes):
            linear = sum(coefficient * feature for coefficient, feature in zip(coefficients, features))
            predicted = _sigmoid(linear)
            residual = outcome - predicted
            weight = max(predicted * (1.0 - predicted), 1e-10)
            for row in range(parameter_count):
                gradient[row] += residual * features[row]
                for column in range(parameter_count):
                    hessian[row][column] += weight * features[row] * features[column]
        for row in range(parameter_count):
            gradient[row] -= _RIDGE * coefficients[row]
        step = _solve_linear_system(hessian, gradient)
        if step is None:
            break
        coefficients = [coefficient + change for coefficient, change in zip(coefficients, step)]
        if max(abs(change) for change in step) < _CONVERGENCE_TOLERANCE:
            converged = True
            break
    return coefficients, converged


class IdentityCalibrator:
    """Leave probabilities alone. The candidate every calibrator must beat."""

    name = "identity"

    def fit(self, probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> "IdentityCalibrator":
        _aligned(probabilities, outcomes)
        return self

    def predict(self, probabilities: Sequence[DecimalInput]) -> list[Decimal]:
        return [Decimal(str(_clipped(_as_float(value)))) for value in probabilities]

    def parameters(self) -> dict[str, Any]:
        return {}


class PlattCalibrator:
    """Logistic recalibration on the logit: p' = sigmoid(a + b * logit(p)).

    ``b`` below one means the model was overconfident and its probabilities are
    being pulled toward the base rate; ``b`` above one means underconfident.
    """

    name = "platt"

    def __init__(self) -> None:
        self.intercept: float | None = None
        self.slope: float | None = None
        self.converged = False

    def fit(self, probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> "PlattCalibrator":
        cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
        design = [[1.0, _logit(probability)] for probability in cleaned_probabilities]
        coefficients, converged = _fit_logistic(design, cleaned_outcomes)
        self.intercept, self.slope = coefficients[0], coefficients[1]
        self.converged = converged
        return self

    def predict(self, probabilities: Sequence[DecimalInput]) -> list[Decimal]:
        if self.intercept is None or self.slope is None:
            raise RuntimeError("calibrator_not_fitted")
        calibrated = []
        for value in probabilities:
            linear = self.intercept + self.slope * _logit(_as_float(value))
            calibrated.append(Decimal(str(_clipped(_sigmoid(linear)))))
        return calibrated

    def parameters(self) -> dict[str, Any]:
        return {"intercept": self.intercept, "slope": self.slope, "converged": self.converged}


class BetaCalibrator:
    """Beta calibration of Kull, Silva Filho and Flach (2017).

    Fits p' = sigmoid(c + a * ln(p) - b * ln(1 - p)). Unlike Platt it can express
    the identity map exactly, so recalibrating an already-calibrated model does
    not distort it.
    """

    name = "beta"

    def __init__(self) -> None:
        self.coefficients: list[float] | None = None
        self.converged = False

    def fit(self, probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> "BetaCalibrator":
        cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
        design = []
        for probability in cleaned_probabilities:
            clipped = _clipped(probability)
            design.append([1.0, log(clipped), -log(1.0 - clipped)])
        coefficients, converged = _fit_logistic(design, cleaned_outcomes)
        self.coefficients = coefficients
        self.converged = converged
        return self

    def predict(self, probabilities: Sequence[DecimalInput]) -> list[Decimal]:
        if self.coefficients is None:
            raise RuntimeError("calibrator_not_fitted")
        intercept, first, second = self.coefficients
        calibrated = []
        for value in probabilities:
            clipped = _clipped(_as_float(value))
            linear = intercept + first * log(clipped) + second * (-log(1.0 - clipped))
            calibrated.append(Decimal(str(_clipped(_sigmoid(linear)))))
        return calibrated

    def parameters(self) -> dict[str, Any]:
        if self.coefficients is None:
            return {"converged": self.converged}
        return {
            "intercept": self.coefficients[0],
            "log_p_coefficient": self.coefficients[1],
            "log_1_minus_p_coefficient": self.coefficients[2],
            "converged": self.converged,
        }


class IsotonicCalibrator:
    """Pool-adjacent-violators isotonic regression.

    Assumes only that the calibrated probability is non-decreasing in the raw
    probability. Powerful and, on small samples, an efficient way to memorize
    noise, which is why ``select_calibrator`` gates it behind a row count.
    """

    name = "isotonic"

    def __init__(self) -> None:
        self.thresholds: list[float] = []
        self.values: list[float] = []

    def fit(self, probabilities: Sequence[DecimalInput], outcomes: Sequence[int]) -> "IsotonicCalibrator":
        cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
        paired = sorted(zip(cleaned_probabilities, cleaned_outcomes), key=lambda item: item[0])
        blocks: list[list[float]] = []
        for probability, outcome in paired:
            blocks.append([float(outcome), 1.0, probability, probability])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
                last = blocks.pop()
                previous = blocks.pop()
                blocks.append(
                    [previous[0] + last[0], previous[1] + last[1], previous[2], last[3]]
                )
        self.thresholds = [block[3] for block in blocks]
        self.values = [block[0] / block[1] for block in blocks]
        return self

    def predict(self, probabilities: Sequence[DecimalInput]) -> list[Decimal]:
        if not self.thresholds:
            raise RuntimeError("calibrator_not_fitted")
        calibrated = []
        for value in probabilities:
            probability = _as_float(value)
            index = 0
            while index < len(self.thresholds) - 1 and probability > self.thresholds[index]:
                index += 1
            calibrated.append(Decimal(str(_clipped(self.values[index]))))
        return calibrated

    def parameters(self) -> dict[str, Any]:
        return {"block_count": len(self.values)}


def calibration_slope_intercept(
    probabilities: Sequence[DecimalInput], outcomes: Sequence[int]
) -> dict[str, Any]:
    """Diagnostic fit of outcome on logit(p).

    Perfect calibration is slope 1 and intercept 0. Slope below 1 is the usual
    finding for an overfit model: its probabilities are too extreme in both
    directions.
    """

    calibrator = PlattCalibrator().fit(probabilities, outcomes)
    return {
        "slope": None if calibrator.slope is None else Decimal(str(calibrator.slope)),
        "intercept": None if calibrator.intercept is None else Decimal(str(calibrator.intercept)),
        "converged": calibrator.converged,
        "sample_size": len(probabilities),
    }


def calibration_report(
    probabilities: Sequence[DecimalInput], outcomes: Sequence[int], *, bucket_count: int = 10
) -> dict[str, Any]:
    """Everything needed to judge whether probabilities mean what they say."""

    cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
    fit = calibration_slope_intercept(cleaned_probabilities, cleaned_outcomes)
    return {
        "sample_size": len(cleaned_probabilities),
        "expected_calibration_error": expected_calibration_error(
            cleaned_probabilities, cleaned_outcomes, bucket_count=bucket_count
        ),
        "maximum_calibration_error": maximum_calibration_error(
            cleaned_probabilities, cleaned_outcomes, bucket_count=bucket_count
        ),
        "log_loss": log_loss(cleaned_probabilities, cleaned_outcomes),
        "calibration_slope": fit["slope"],
        "calibration_intercept": fit["intercept"],
        "calibration_fit_converged": fit["converged"],
        "buckets": calibration_buckets(cleaned_probabilities, cleaned_outcomes, bucket_count=bucket_count),
        "base_rate": Decimal(sum(cleaned_outcomes)) / Decimal(len(cleaned_outcomes)),
    }


@dataclass(frozen=True)
class CalibrationSelection:
    """Which calibrator was chosen, and the out-of-fold evidence for choosing it."""

    method: str
    calibrator: Any
    sample_size: int
    out_of_fold_log_loss: dict[str, Decimal]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "sample_size": self.sample_size,
            "out_of_fold_log_loss": {name: str(value) for name, value in self.out_of_fold_log_loss.items()},
            "reason": self.reason,
            "parameters": self.calibrator.parameters(),
        }


def _contiguous_folds(row_count: int, fold_count: int) -> list[tuple[int, int]]:
    """Contiguous, order-preserving folds.

    Calibration rows arrive in time order and neighbouring rows share games,
    weather and roster state. Shuffling them would leak that shared context
    across the fold boundary and flatter every calibrator.
    """

    size = row_count // fold_count
    folds = []
    start = 0
    for index in range(fold_count):
        end = row_count if index == fold_count - 1 else start + size
        folds.append((start, end))
        start = end
    return folds


def select_calibrator(
    probabilities: Sequence[DecimalInput],
    outcomes: Sequence[int],
    *,
    minimum_rows: int = DEFAULT_MINIMUM_CALIBRATION_ROWS,
    isotonic_minimum_rows: int = DEFAULT_ISOTONIC_MINIMUM_ROWS,
    fold_count: int = 5,
) -> CalibrationSelection:
    """Choose a calibrator by out-of-fold log loss, defaulting to no calibration.

    Rows must already be in prediction-time order. Identity is always a
    candidate and wins ties, so a model whose probabilities are already honest is
    left alone instead of being fitted into looking better.
    """

    cleaned_probabilities, cleaned_outcomes = _aligned(probabilities, outcomes)
    row_count = len(cleaned_probabilities)
    identity = IdentityCalibrator()
    if row_count < minimum_rows:
        return CalibrationSelection(
            method="identity",
            calibrator=identity,
            sample_size=row_count,
            out_of_fold_log_loss={},
            reason=f"insufficient_rows_for_calibration:{row_count}<{minimum_rows}",
        )
    if len(set(cleaned_outcomes)) < 2:
        return CalibrationSelection(
            method="identity",
            calibrator=identity,
            sample_size=row_count,
            out_of_fold_log_loss={},
            reason="single_outcome_class",
        )

    candidates: dict[str, Any] = {"identity": IdentityCalibrator, "platt": PlattCalibrator, "beta": BetaCalibrator}
    if row_count >= isotonic_minimum_rows:
        candidates["isotonic"] = IsotonicCalibrator

    folds = _contiguous_folds(row_count, fold_count)
    scores: dict[str, Decimal] = {}
    for name, factory in candidates.items():
        fold_losses: list[Decimal] = []
        for start, end in folds:
            training_probabilities = cleaned_probabilities[:start] + cleaned_probabilities[end:]
            training_outcomes = cleaned_outcomes[:start] + cleaned_outcomes[end:]
            holdout_probabilities = cleaned_probabilities[start:end]
            holdout_outcomes = cleaned_outcomes[start:end]
            if not holdout_probabilities or not training_probabilities:
                continue
            if len(set(training_outcomes)) < 2:
                continue
            try:
                fitted = factory().fit(training_probabilities, training_outcomes)
                predicted = fitted.predict(holdout_probabilities)
            except (ValueError, RuntimeError):
                fold_losses = []
                break
            fold_losses.append(log_loss(predicted, holdout_outcomes))
        if fold_losses:
            scores[name] = sum(fold_losses, ZERO) / Decimal(len(fold_losses))

    if not scores or "identity" not in scores:
        return CalibrationSelection(
            method="identity",
            calibrator=identity,
            sample_size=row_count,
            out_of_fold_log_loss=scores,
            reason="cross_validation_unavailable",
        )

    best_name = min(scores, key=lambda name: (scores[name], name != "identity"))
    if scores[best_name] >= scores["identity"]:
        best_name = "identity"
    fitted = candidates[best_name]().fit(cleaned_probabilities, cleaned_outcomes)
    reason = (
        "identity_not_beaten_out_of_fold"
        if best_name == "identity"
        else f"lowest_out_of_fold_log_loss:{scores[best_name]:.6f}_vs_identity:{scores['identity']:.6f}"
    )
    return CalibrationSelection(
        method=best_name,
        calibrator=fitted,
        sample_size=row_count,
        out_of_fold_log_loss=scores,
        reason=reason,
    )
