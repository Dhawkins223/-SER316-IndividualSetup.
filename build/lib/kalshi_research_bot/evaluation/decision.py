from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


DecimalInput = Decimal | int | float | str
ZERO = Decimal("0")
ONE = Decimal("1")
ONE_HUNDRED = Decimal("100")

VALIDATED_MODEL_STATE = "validated_research"
FRESH_SOURCE_STATES = {"fresh"}
OPEN_MARKET_STATES = {"active", "open"}


def _decimal(value: DecimalInput, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}_must_be_numeric")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}_must_be_numeric") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return normalized


def _optional_decimal(value: DecimalInput | None, *, field_name: str) -> Decimal | None:
    return None if value is None else _decimal(value, field_name=field_name)


def _probability(value: DecimalInput, *, field_name: str) -> Decimal:
    probability = _decimal(value, field_name=field_name)
    if probability < ZERO or probability > ONE:
        raise ValueError(f"{field_name}_outside_unit_interval")
    return probability


@dataclass(frozen=True)
class ResearchDecisionPolicy:
    version: str = "calibration_cost_uncertainty_v1"
    minimum_test_rows: int = 100
    minimum_point_edge_cents: Decimal = Decimal("1.00")
    minimum_lower_bound_edge_cents: Decimal = Decimal("0.50")
    maximum_spread_cents: Decimal = Decimal("12.00")
    kelly_multiplier: Decimal = Decimal("0.25")
    maximum_bankroll_fraction: Decimal = Decimal("0.01")

    def validate(self) -> None:
        if self.minimum_test_rows < 1:
            raise ValueError("minimum_test_rows_must_be_positive")
        if self.minimum_point_edge_cents < ZERO or self.minimum_lower_bound_edge_cents < ZERO:
            raise ValueError("edge_thresholds_must_be_nonnegative")
        if self.maximum_spread_cents <= ZERO:
            raise ValueError("maximum_spread_cents_must_be_positive")
        if self.kelly_multiplier < ZERO or self.kelly_multiplier > ONE:
            raise ValueError("kelly_multiplier_outside_unit_interval")
        if self.maximum_bankroll_fraction < ZERO or self.maximum_bankroll_fraction > ONE:
            raise ValueError("maximum_bankroll_fraction_outside_unit_interval")


def contract_expected_value_cents(
    *,
    probability: DecimalInput,
    market_price_cents: DecimalInput,
    fee_cents: DecimalInput,
    slippage_cents: DecimalInput,
) -> Decimal:
    probability_value = _probability(probability, field_name="probability")
    market_price = _decimal(market_price_cents, field_name="market_price_cents")
    fee = _decimal(fee_cents, field_name="fee_cents")
    slippage = _decimal(slippage_cents, field_name="slippage_cents")
    if market_price <= ZERO or market_price >= ONE_HUNDRED:
        raise ValueError("market_price_cents_outside_open_contract_range")
    if fee < ZERO or slippage < ZERO:
        raise ValueError("execution_costs_must_be_nonnegative")
    return probability_value * ONE_HUNDRED - market_price - fee - slippage


def uncertainty_adjusted_kelly_fraction(
    *,
    lower_probability: DecimalInput,
    market_price_cents: DecimalInput,
    fee_cents: DecimalInput,
    slippage_cents: DecimalInput,
    multiplier: DecimalInput = Decimal("0.25"),
    maximum_fraction: DecimalInput = Decimal("0.01"),
) -> Decimal:
    probability = _probability(lower_probability, field_name="lower_probability")
    effective_price = (
        _decimal(market_price_cents, field_name="market_price_cents")
        + _decimal(fee_cents, field_name="fee_cents")
        + _decimal(slippage_cents, field_name="slippage_cents")
    ) / ONE_HUNDRED
    multiplier_value = _probability(multiplier, field_name="multiplier")
    maximum_value = _probability(maximum_fraction, field_name="maximum_fraction")
    if effective_price <= ZERO or effective_price >= ONE:
        return ZERO
    full_kelly = (probability - effective_price) / (ONE - effective_price)
    if full_kelly <= ZERO:
        return ZERO
    return min(maximum_value, full_kelly * multiplier_value)


def evaluate_binary_contract_decision(
    *,
    model_probability: DecimalInput | None,
    confidence_interval_low: DecimalInput | None,
    confidence_interval_high: DecimalInput | None,
    market_price_cents: DecimalInput | None,
    model_state: str,
    test_sample_size: int,
    source_freshness_state: str,
    market_status: str,
    spread_cents: DecimalInput | None,
    fee_cents: DecimalInput | None,
    slippage_cents: DecimalInput | None,
    market_product_type: str,
    validated_product_type: str | None,
    market_horizon_bucket: str | None = None,
    validated_horizon_bucket: str | None = None,
    rejected: bool = False,
    unresolved: bool = False,
    blocked: bool = False,
    duplicate: bool = False,
    model_version: str = "unversioned",
    feature_version: str = "unversioned",
    dataset_version: str = "unversioned",
    policy: ResearchDecisionPolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or ResearchDecisionPolicy()
    active_policy.validate()
    reasons: list[str] = []
    waiting_reasons: list[str] = []

    freshness = str(source_freshness_state or "unknown").strip().lower()
    status = str(market_status or "unknown").strip().lower()
    normalized_model_state = str(model_state or "experimental").strip().lower()
    product_type = str(market_product_type or "unknown").strip().lower()
    validation_product = str(validated_product_type or "").strip().lower()
    market_horizon = str(market_horizon_bucket or "").strip().lower()
    validation_horizon = str(validated_horizon_bucket or "").strip().lower()
    normalized_model_version = str(model_version or "unversioned").strip()
    normalized_feature_version = str(feature_version or "unversioned").strip()
    normalized_dataset_version = str(dataset_version or "unversioned").strip()

    if freshness not in FRESH_SOURCE_STATES:
        waiting_reasons.append(f"source_not_fresh:{freshness}")
    if status not in OPEN_MARKET_STATES:
        reasons.append(f"market_not_open:{status}")
    if rejected:
        reasons.append("rejected_record")
    if unresolved:
        reasons.append("unresolved_record")
    if blocked:
        reasons.append("blocked_record")
    if duplicate:
        reasons.append("duplicate_record")
    if normalized_model_state != VALIDATED_MODEL_STATE:
        reasons.append(f"model_not_validated:{normalized_model_state}")
    if normalized_model_version.lower() == "unversioned":
        reasons.append("missing_model_version")
    if normalized_feature_version.lower() == "unversioned":
        reasons.append("missing_feature_version")
    if normalized_dataset_version.lower() == "unversioned":
        reasons.append("missing_dataset_version")
    if int(test_sample_size or 0) < active_policy.minimum_test_rows:
        reasons.append(f"test_sample_below_threshold:{int(test_sample_size or 0)}/{active_policy.minimum_test_rows}")
    if not validation_product:
        reasons.append("missing_validation_product_type")
    elif validation_product != product_type:
        reasons.append(f"validation_product_mismatch:{validation_product}/{product_type}")
    if market_horizon and validation_horizon and market_horizon != validation_horizon:
        reasons.append(f"validation_horizon_mismatch:{validation_horizon}/{market_horizon}")
    elif market_horizon and not validation_horizon:
        reasons.append("missing_validation_horizon_bucket")

    point_probability = _optional_decimal(model_probability, field_name="model_probability")
    lower_probability = _optional_decimal(confidence_interval_low, field_name="confidence_interval_low")
    upper_probability = _optional_decimal(confidence_interval_high, field_name="confidence_interval_high")
    price = _optional_decimal(market_price_cents, field_name="market_price_cents")
    spread = _optional_decimal(spread_cents, field_name="spread_cents")
    fee = _optional_decimal(fee_cents, field_name="fee_cents")
    slippage = _optional_decimal(slippage_cents, field_name="slippage_cents")

    if point_probability is None:
        waiting_reasons.append("missing_model_probability")
    else:
        point_probability = _probability(point_probability, field_name="model_probability")
    if lower_probability is None or upper_probability is None:
        waiting_reasons.append("missing_probability_interval")
    else:
        lower_probability = _probability(lower_probability, field_name="confidence_interval_low")
        upper_probability = _probability(upper_probability, field_name="confidence_interval_high")
        if lower_probability > upper_probability:
            reasons.append("invalid_probability_interval")
        if point_probability is not None and not lower_probability <= point_probability <= upper_probability:
            reasons.append("model_probability_outside_interval")
    if price is None:
        waiting_reasons.append("missing_market_price")
    elif price <= ZERO or price >= ONE_HUNDRED:
        reasons.append("market_price_outside_open_contract_range")
    if spread is None:
        waiting_reasons.append("missing_spread")
    elif spread < ZERO:
        reasons.append("negative_spread")
    elif spread > active_policy.maximum_spread_cents:
        reasons.append("wide_spread")
    if fee is None or slippage is None:
        waiting_reasons.append("execution_cost_model_missing")
    elif fee < ZERO or slippage < ZERO:
        reasons.append("negative_execution_cost")

    point_edge: Decimal | None = None
    lower_bound_edge: Decimal | None = None
    break_even_probability: Decimal | None = None
    kelly_fraction = ZERO
    complete_inputs = all(
        value is not None
        for value in (point_probability, lower_probability, upper_probability, price, spread, fee, slippage)
    )
    if complete_inputs and price is not None and fee is not None and slippage is not None:
        if ZERO < price < ONE_HUNDRED and fee >= ZERO and slippage >= ZERO:
            break_even_probability = (price + fee + slippage) / ONE_HUNDRED
            if break_even_probability >= ONE:
                reasons.append("execution_cost_exceeds_contract_payout")
            else:
                point_edge = contract_expected_value_cents(
                    probability=point_probability,
                    market_price_cents=price,
                    fee_cents=fee,
                    slippage_cents=slippage,
                )
                lower_bound_edge = contract_expected_value_cents(
                    probability=lower_probability,
                    market_price_cents=price,
                    fee_cents=fee,
                    slippage_cents=slippage,
                )
                if point_edge < active_policy.minimum_point_edge_cents:
                    reasons.append("point_edge_below_threshold")
                if lower_bound_edge < active_policy.minimum_lower_bound_edge_cents:
                    reasons.append("lower_bound_edge_below_threshold")

    all_reasons = reasons + waiting_reasons
    if reasons:
        action = "NO_BET"
    elif waiting_reasons:
        action = "WAIT_FOR_DATA"
    else:
        action = "BET_CANDIDATE"
        kelly_fraction = uncertainty_adjusted_kelly_fraction(
            lower_probability=lower_probability,
            market_price_cents=price,
            fee_cents=fee,
            slippage_cents=slippage,
            multiplier=active_policy.kelly_multiplier,
            maximum_fraction=active_policy.maximum_bankroll_fraction,
        )

    return {
        "action": action,
        "policy_version": active_policy.version,
        "research_only": True,
        "execution_allowed": False,
        "model_state": normalized_model_state,
        "model_version": normalized_model_version,
        "feature_version": normalized_feature_version,
        "dataset_version": normalized_dataset_version,
        "market_product_type": product_type,
        "validated_product_type": validation_product or None,
        "market_horizon_bucket": market_horizon or None,
        "validated_horizon_bucket": validation_horizon or None,
        "test_sample_size": int(test_sample_size or 0),
        "model_probability": point_probability,
        "confidence_interval_low": lower_probability,
        "confidence_interval_high": upper_probability,
        "market_price_cents": price,
        "spread_cents": spread,
        "fee_cents": fee,
        "slippage_cents": slippage,
        "break_even_probability": break_even_probability,
        "point_net_edge_cents": point_edge,
        "lower_bound_net_edge_cents": lower_bound_edge,
        "uncertainty_adjusted_kelly_fraction": kelly_fraction,
        "reasons": all_reasons,
    }
