from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Literal, Sequence


OrderType = Literal["market", "limit"]
ContractSide = Literal["yes", "no"]
FillState = Literal["filled", "partial_fill", "no_fill", "rejected"]
DecimalInput = Decimal | int | float | str
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _decimal(value: DecimalInput, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name}_must_be_numeric")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name}_must_be_numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{name}_must_be_finite")
    return number


def _price(value: DecimalInput, *, name: str) -> Decimal:
    price = _decimal(value, name=name)
    if price < ZERO or price > ONE_HUNDRED:
        raise ValueError(f"{name}_outside_0_100")
    return price


def _amount(value: DecimalInput, *, name: str) -> Decimal:
    amount = _decimal(value, name=name)
    if amount < ZERO:
        raise ValueError(f"{name}_must_not_be_negative")
    return amount


@dataclass(frozen=True)
class PriceLevel:
    price_cents: DecimalInput
    quantity: int

    def normalized(self) -> "PriceLevel":
        if int(self.quantity) < 0:
            raise ValueError("negative_depth_quantity")
        return PriceLevel(_price(self.price_cents, name="depth_price"), int(self.quantity))


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    snapshot_timestamp: str
    market_status: str
    yes_bid_cents: DecimalInput | None = None
    yes_ask_cents: DecimalInput | None = None
    no_bid_cents: DecimalInput | None = None
    no_ask_cents: DecimalInput | None = None
    yes_ask_depth: Sequence[PriceLevel] = field(default_factory=tuple)
    no_ask_depth: Sequence[PriceLevel] = field(default_factory=tuple)
    close_timestamp: str | None = None
    source_snapshot_hash: str | None = None

    def ask_levels(self, side: ContractSide) -> list[PriceLevel]:
        raw_levels = self.yes_ask_depth if side == "yes" else self.no_ask_depth
        levels = [level.normalized() for level in raw_levels if int(level.quantity) > 0]
        if not levels:
            ask = self.yes_ask_cents if side == "yes" else self.no_ask_cents
            if ask is not None:
                levels = [PriceLevel(_price(ask, name=f"{side}_ask"), 1)]
        return sorted(levels, key=lambda level: _price(level.price_cents, name="depth_price"))

    def as_record(self) -> dict[str, Any]:
        value = asdict(self)
        value["yes_ask_depth"] = [asdict(level) for level in self.yes_ask_depth]
        value["no_ask_depth"] = [asdict(level) for level in self.no_ask_depth]
        return value


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    market_id: str
    signal_timestamp: str
    order_timestamp: str
    contract_side: ContractSide
    order_type: OrderType
    quantity: int
    intended_price_cents: DecimalInput
    limit_price_cents: DecimalInput | None = None
    resting_fill_quantity: int = 0

    def validate(self) -> None:
        if self.contract_side not in {"yes", "no"}:
            raise ValueError("unsupported_contract_side")
        if self.order_type not in {"market", "limit"}:
            raise ValueError("unsupported_order_type")
        if int(self.quantity) <= 0:
            raise ValueError("quantity_must_be_positive")
        _price(self.intended_price_cents, name="intended_price")
        if self.order_type == "limit" and self.limit_price_cents is None:
            raise ValueError("limit_price_required")
        if self.limit_price_cents is not None:
            _price(self.limit_price_cents, name="limit_price")
        if int(self.resting_fill_quantity) < 0:
            raise ValueError("negative_resting_fill_quantity")
        if _timestamp(self.order_timestamp) < _timestamp(self.signal_timestamp):
            raise ValueError("order_before_signal")


@dataclass(frozen=True)
class ExecutionConfig:
    taker_fee_rate: DecimalInput = Decimal("0.07")
    maker_fee_rate: DecimalInput = Decimal("0.0175")
    fee_schedule_version: str = "kalshi_general_2026-02-05"
    market_slippage_cents: DecimalInput = Decimal("1")
    signal_to_order_move_cents: DecimalInput = ZERO
    maximum_price_cents: DecimalInput = Decimal("99")
    maximum_position_contracts: int = 100
    maximum_capital_cents: DecimalInput = Decimal("10000")

    def validate(self) -> None:
        _amount(self.taker_fee_rate, name="taker_fee_rate")
        _amount(self.maker_fee_rate, name="maker_fee_rate")
        _amount(self.market_slippage_cents, name="market_slippage")
        _amount(self.signal_to_order_move_cents, name="signal_to_order_move")
        _price(self.maximum_price_cents, name="maximum_price")
        if self.maximum_position_contracts < 1 or _amount(self.maximum_capital_cents, name="maximum_capital") <= ZERO:
            raise ValueError("invalid_position_or_capital_limit")


@dataclass(frozen=True)
class PaperExecution:
    order_id: str
    market_id: str
    signal_timestamp: str
    order_timestamp: str
    snapshot_timestamp: str
    market_snapshot: dict[str, Any]
    contract_side: ContractSide
    order_type: OrderType
    fill_state: FillState
    intended_price_cents: Decimal
    simulated_fill_price_cents: Decimal | None
    requested_quantity: int
    filled_quantity: int
    unfilled_quantity: int
    fee_estimate_cents: Decimal
    slippage_cents: Decimal
    fee_schedule_version: str
    liquidity_role: str | None
    rejection_reason: str | None = None
    final_payout_cents: Decimal | None = None
    gross_return_cents: Decimal | None = None
    net_return_cents: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kalshi_fee_cents(
    *,
    price_cents: DecimalInput,
    quantity: int,
    liquidity_role: Literal["taker", "maker"],
    config: ExecutionConfig,
) -> Decimal:
    """Apply the general Kalshi fee formula and round up to the next cent."""

    if int(quantity) <= 0:
        return ZERO
    probability = _price(price_cents, name="fee_price") / ONE_HUNDRED
    rate = _amount(config.taker_fee_rate if liquidity_role == "taker" else config.maker_fee_rate, name="fee_rate")
    raw_fee_cents = rate * int(quantity) * probability * (Decimal("1") - probability) * ONE_HUNDRED
    return raw_fee_cents.to_integral_value(rounding=ROUND_CEILING)


def _rejected(order: PaperOrder, snapshot: MarketSnapshot, config: ExecutionConfig, reason: str) -> PaperExecution:
    return PaperExecution(
        order_id=order.order_id,
        market_id=order.market_id,
        signal_timestamp=order.signal_timestamp,
        order_timestamp=order.order_timestamp,
        snapshot_timestamp=snapshot.snapshot_timestamp,
        market_snapshot=snapshot.as_record(),
        contract_side=order.contract_side,
        order_type=order.order_type,
        fill_state="rejected",
        intended_price_cents=_price(order.intended_price_cents, name="intended_price"),
        simulated_fill_price_cents=None,
        requested_quantity=int(order.quantity),
        filled_quantity=0,
        unfilled_quantity=int(order.quantity),
        fee_estimate_cents=ZERO,
        slippage_cents=ZERO,
        fee_schedule_version=config.fee_schedule_version,
        liquidity_role=None,
        rejection_reason=reason,
    )


def _no_fill(order: PaperOrder, snapshot: MarketSnapshot, config: ExecutionConfig, reason: str) -> PaperExecution:
    rejected = _rejected(order, snapshot, config, reason)
    return PaperExecution(**{**rejected.to_dict(), "fill_state": "no_fill"})


def simulate_order(
    order: PaperOrder,
    snapshot: MarketSnapshot,
    *,
    config: ExecutionConfig | None = None,
) -> PaperExecution:
    execution_config = config or ExecutionConfig()
    order.validate()
    execution_config.validate()
    if order.market_id != snapshot.market_id:
        return _rejected(order, snapshot, execution_config, "market_id_mismatch")
    order_time = _timestamp(order.order_timestamp)
    snapshot_time = _timestamp(snapshot.snapshot_timestamp)
    if snapshot_time > order_time:
        return _rejected(order, snapshot, execution_config, "future_execution_snapshot")
    status = str(snapshot.market_status or "").strip().lower()
    if status not in {"open", "active", "trading"}:
        return _rejected(order, snapshot, execution_config, f"market_not_open:{status or 'unknown'}")
    if snapshot.close_timestamp and order_time >= _timestamp(snapshot.close_timestamp):
        return _rejected(order, snapshot, execution_config, "market_expired")
    if order.quantity > execution_config.maximum_position_contracts:
        return _rejected(order, snapshot, execution_config, "position_limit_exceeded")
    levels = snapshot.ask_levels(order.contract_side)
    if not levels:
        return _no_fill(order, snapshot, execution_config, "empty_order_book")
    best_ask = _price(levels[0].price_cents, name="best_ask")
    maximum_price = _price(execution_config.maximum_price_cents, name="maximum_price")
    market_slippage = _amount(execution_config.market_slippage_cents, name="market_slippage")
    signal_move = _amount(execution_config.signal_to_order_move_cents, name="signal_to_order_move")
    maximum_capital = _amount(execution_config.maximum_capital_cents, name="maximum_capital")
    price_ceiling = maximum_price
    liquidity_role: Literal["taker", "maker"] = "taker"
    available_quantity = int(order.quantity)
    if order.order_type == "market":
        price_ceiling = min(price_ceiling, best_ask + market_slippage + signal_move)
    else:
        limit = _price(order.limit_price_cents, name="limit_price")
        price_ceiling = min(price_ceiling, limit)
        if limit < best_ask:
            liquidity_role = "maker"
            available_quantity = min(int(order.quantity), int(order.resting_fill_quantity))
            if available_quantity <= 0:
                return _no_fill(order, snapshot, execution_config, "resting_limit_not_filled")
            levels = [PriceLevel(limit, available_quantity)]
    fills: list[tuple[Decimal, int]] = []
    remaining = available_quantity
    for level in levels:
        effective_price = _price(level.price_cents, name="depth_price")
        if liquidity_role == "taker":
            effective_price += signal_move
        if effective_price > price_ceiling or effective_price > maximum_price:
            continue
        quantity = min(remaining, int(level.quantity))
        spent = sum((price * filled for price, filled in fills), ZERO)
        prospective_cost = spent + effective_price * quantity
        if prospective_cost > maximum_capital:
            affordable = quantity if effective_price == ZERO else int((maximum_capital - spent) // effective_price)
            quantity = min(quantity, max(0, affordable))
        if quantity <= 0:
            break
        fills.append((effective_price, quantity))
        remaining -= quantity
        if remaining <= 0:
            break
    filled_quantity = sum(quantity for _, quantity in fills)
    if filled_quantity <= 0:
        return _no_fill(order, snapshot, execution_config, "price_or_capital_limit_prevented_fill")
    fill_price = sum((price * quantity for price, quantity in fills), ZERO) / Decimal(filled_quantity)
    fee = kalshi_fee_cents(
        price_cents=fill_price,
        quantity=filled_quantity,
        liquidity_role=liquidity_role,
        config=execution_config,
    )
    slippage = (fill_price - _price(order.intended_price_cents, name="intended_price")) * filled_quantity
    fill_state: FillState = "filled" if filled_quantity == order.quantity else "partial_fill"
    return PaperExecution(
        order_id=order.order_id,
        market_id=order.market_id,
        signal_timestamp=order.signal_timestamp,
        order_timestamp=order.order_timestamp,
        snapshot_timestamp=snapshot.snapshot_timestamp,
        market_snapshot=snapshot.as_record(),
        contract_side=order.contract_side,
        order_type=order.order_type,
        fill_state=fill_state,
        intended_price_cents=_price(order.intended_price_cents, name="intended_price"),
        simulated_fill_price_cents=fill_price,
        requested_quantity=int(order.quantity),
        filled_quantity=filled_quantity,
        unfilled_quantity=int(order.quantity) - filled_quantity,
        fee_estimate_cents=fee,
        slippage_cents=slippage,
        fee_schedule_version=execution_config.fee_schedule_version,
        liquidity_role=liquidity_role,
    )


def settle_execution(execution: PaperExecution, *, winning_side: ContractSide | None) -> PaperExecution:
    if execution.fill_state not in {"filled", "partial_fill"} or execution.filled_quantity <= 0:
        return execution
    if winning_side not in {"yes", "no", None}:
        raise ValueError("unsupported_winning_side")
    fill_price = execution.simulated_fill_price_cents
    if fill_price is None:
        raise ValueError("filled_execution_missing_fill_price")
    if winning_side is None:
        payout = fill_price * execution.filled_quantity
    else:
        payout = ONE_HUNDRED * execution.filled_quantity if winning_side == execution.contract_side else ZERO
    cost = fill_price * execution.filled_quantity
    gross_return = payout - cost
    net_return = gross_return - execution.fee_estimate_cents
    return PaperExecution(
        **{
            **execution.to_dict(),
            "final_payout_cents": payout,
            "gross_return_cents": gross_return,
            "net_return_cents": net_return,
        }
    )
