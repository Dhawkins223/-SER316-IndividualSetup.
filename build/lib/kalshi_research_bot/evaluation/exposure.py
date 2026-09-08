from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


DecimalInput = Decimal | int | float | str
ZERO = Decimal("0")


def _decimal(value: DecimalInput, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name}_must_be_numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name}_must_be_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name}_must_be_finite")
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-") or "unknown"


def correlation_group_id(
    *,
    event_id: str,
    category: str,
    underlying_ids: Sequence[str],
) -> str:
    payload = {
        "event_id": _slug(event_id),
        "category": _slug(category),
        "underlying_ids": sorted(_slug(value) for value in underlying_ids if value),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"corr:{digest[:20]}"


@dataclass(frozen=True)
class ExposureCandidate:
    prediction_id: str
    prediction_timestamp: str
    event_id: str
    market_id: str
    category: str
    contract_side: str
    capital_at_risk_cents: DecimalInput
    underlying_ids: Sequence[str] = field(default_factory=tuple)
    threshold: DecimalInput | None = None
    confidence: DecimalInput | None = None
    correlation_group: str | None = None

    def normalized(self) -> "ExposureCandidate":
        capital_at_risk = _decimal(self.capital_at_risk_cents, name="capital_at_risk")
        if capital_at_risk <= ZERO:
            raise ValueError("capital_at_risk_must_be_positive")
        side = str(self.contract_side).strip().lower()
        if side not in {"yes", "no", "up", "down"}:
            raise ValueError("unsupported_contract_side")
        group = self.correlation_group or correlation_group_id(
            event_id=self.event_id,
            category=self.category,
            underlying_ids=self.underlying_ids,
        )
        return ExposureCandidate(
            prediction_id=str(self.prediction_id),
            prediction_timestamp=str(self.prediction_timestamp),
            event_id=_slug(self.event_id),
            market_id=_slug(self.market_id),
            category=_slug(self.category),
            contract_side=side,
            capital_at_risk_cents=capital_at_risk,
            underlying_ids=tuple(sorted({_slug(value) for value in self.underlying_ids if value})),
            threshold=None if self.threshold is None else _decimal(self.threshold, name="threshold"),
            confidence=None if self.confidence is None else _decimal(self.confidence, name="confidence"),
            correlation_group=group,
        )


@dataclass(frozen=True)
class ExposureLimits:
    maximum_simulated_capital_cents: DecimalInput = Decimal("100000")
    maximum_position_cents: DecimalInput = Decimal("10000")
    maximum_event_exposure_cents: DecimalInput = Decimal("15000")
    maximum_category_exposure_cents: DecimalInput = Decimal("40000")
    maximum_underlying_exposure_cents: DecimalInput = Decimal("20000")
    maximum_correlated_exposure_cents: DecimalInput = Decimal("15000")
    maximum_markets_per_event: int = 1

    def validate(self) -> None:
        numeric_limits = (
            self.maximum_simulated_capital_cents,
            self.maximum_position_cents,
            self.maximum_event_exposure_cents,
            self.maximum_category_exposure_cents,
            self.maximum_underlying_exposure_cents,
            self.maximum_correlated_exposure_cents,
        )
        if any(_decimal(value, name="exposure_limit") <= ZERO for value in numeric_limits) or self.maximum_markets_per_event < 1:
            raise ValueError("exposure_limits_must_be_positive")


@dataclass(frozen=True)
class ExposureDecision:
    prediction_id: str
    accepted: bool
    reasons: Sequence[str]
    correlation_group: str
    raw_capital_at_risk_cents: Decimal
    accepted_capital_at_risk_cents: Decimal
    candidate: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, ZERO)


def _limit(value: DecimalInput) -> Decimal:
    return _decimal(value, name="exposure_limit")


def apply_exposure_limits(
    candidates: Sequence[ExposureCandidate],
    *,
    limits: ExposureLimits | None = None,
) -> dict[str, Any]:
    configured_limits = limits or ExposureLimits()
    configured_limits.validate()
    normalized = sorted(
        (candidate.normalized() for candidate in candidates),
        key=lambda candidate: (candidate.prediction_timestamp, candidate.prediction_id),
    )
    accepted: list[ExposureCandidate] = []
    decisions: list[ExposureDecision] = []
    seen_market_keys: set[tuple[str, str, str]] = set()
    for candidate in normalized:
        reasons: list[str] = []
        market_key = (candidate.event_id, candidate.market_id, candidate.contract_side)
        event_positions = [row for row in accepted if row.event_id == candidate.event_id]
        category_positions = [row for row in accepted if row.category == candidate.category]
        correlated_positions = [row for row in accepted if row.correlation_group == candidate.correlation_group]
        underlying_positions = [
            row
            for row in accepted
            if set(row.underlying_ids).intersection(candidate.underlying_ids)
        ]
        if market_key in seen_market_keys:
            reasons.append("duplicate_market_exposure")
        if candidate.capital_at_risk_cents > _limit(configured_limits.maximum_position_cents):
            reasons.append("position_limit_exceeded")
        if len({row.market_id for row in event_positions}.union({candidate.market_id})) > configured_limits.maximum_markets_per_event:
            reasons.append("event_market_count_limit_exceeded")
        if _sum(row.capital_at_risk_cents for row in accepted) + candidate.capital_at_risk_cents > _limit(configured_limits.maximum_simulated_capital_cents):
            reasons.append("portfolio_capital_limit_exceeded")
        if _sum(row.capital_at_risk_cents for row in event_positions) + candidate.capital_at_risk_cents > _limit(configured_limits.maximum_event_exposure_cents):
            reasons.append("event_exposure_limit_exceeded")
        if _sum(row.capital_at_risk_cents for row in category_positions) + candidate.capital_at_risk_cents > _limit(configured_limits.maximum_category_exposure_cents):
            reasons.append("category_exposure_limit_exceeded")
        if candidate.underlying_ids and _sum(row.capital_at_risk_cents for row in underlying_positions) + candidate.capital_at_risk_cents > _limit(configured_limits.maximum_underlying_exposure_cents):
            reasons.append("underlying_exposure_limit_exceeded")
        if _sum(row.capital_at_risk_cents for row in correlated_positions) + candidate.capital_at_risk_cents > _limit(configured_limits.maximum_correlated_exposure_cents):
            reasons.append("correlated_exposure_limit_exceeded")
        opposite_sides = {"yes": "no", "no": "yes", "up": "down", "down": "up"}
        if any(
            row.market_id == candidate.market_id
            and row.contract_side == opposite_sides[candidate.contract_side]
            for row in event_positions
        ):
            reasons.append("opposing_same_market_exposure")
        accepted_flag = not reasons
        if accepted_flag:
            accepted.append(candidate)
            seen_market_keys.add(market_key)
        decisions.append(
            ExposureDecision(
                prediction_id=candidate.prediction_id,
                accepted=accepted_flag,
                reasons=tuple(sorted(set(reasons))),
                correlation_group=str(candidate.correlation_group),
                raw_capital_at_risk_cents=candidate.capital_at_risk_cents,
                accepted_capital_at_risk_cents=candidate.capital_at_risk_cents if accepted_flag else ZERO,
                candidate=asdict(candidate),
            )
        )
    return {
        "raw_prediction_count": len(normalized),
        "accepted_position_count": len(accepted),
        "rejected_position_count": len(normalized) - len(accepted),
        "raw_capital_at_risk_cents": _sum(row.capital_at_risk_cents for row in normalized),
        "accepted_capital_at_risk_cents": _sum(row.capital_at_risk_cents for row in accepted),
        "correlation_group_count": len({row.correlation_group for row in accepted}),
        "decisions": [decision.to_dict() for decision in decisions],
        "limits": asdict(configured_limits),
    }


def exposure_adjusted_performance(
    rows: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    decision_by_id = {str(decision["prediction_id"]): decision for decision in decisions}
    settled = [row for row in rows if row.get("settlement_state") in {"win", "loss"}]
    adjusted = [
        row
        for row in settled
        if decision_by_id.get(str(row.get("prediction_id")), {}).get("accepted") is True
    ]

    def value(row: Mapping[str, Any], *names: str) -> Decimal:
        for name in names:
            raw = row.get(name)
            if raw is not None:
                return _decimal(raw, name=name)
        return ZERO

    def metrics(values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        wins = sum(row.get("settlement_state") == "win" for row in values)
        losses = sum(row.get("settlement_state") == "loss" for row in values)
        net = _sum(value(row, "net_return_cents", "profit_loss_cents") for row in values)
        risked = _sum(value(row, "capital_at_risk_cents", "entry_price_cents") for row in values)
        return {
            "settled_count": len(values),
            "wins": wins,
            "losses": losses,
            "accuracy": Decimal(wins) / Decimal(len(values)) if values else None,
            "net_return_cents": net,
            "return_on_risk": net / risked if risked else None,
        }

    return {
        "raw_prediction_performance": metrics(settled),
        "exposure_adjusted_portfolio_performance": metrics(adjusted),
        "excluded_correlated_or_limited_positions": len(settled) - len(adjusted),
    }
