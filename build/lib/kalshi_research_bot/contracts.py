from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Game:
    game_id: str
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: str
    signals: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketQuote:
    ticker: str
    game_id: str
    title: str
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None


@dataclass(frozen=True)
class SourceRecord:
    source: str
    kind: str
    url: str
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelPrediction:
    game_id: str
    sport: str
    target: str
    probability: float
    fair_price_cents: float
    model_name: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EdgeResult:
    ticker: str
    game_id: str
    side: str
    model_probability: float
    entry_price_cents: float
    fair_price_cents: float
    expected_value_cents: float
    title: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TotalLeg:
    leg_id: str
    sport: str
    league: str
    event_name: str
    market_title: str
    selection: str
    line: float
    model_probability: float
    entry_price_cents: float
    source_notes: list[str] = field(default_factory=list)
    # Optional, and inert when absent. The correlation model treats two legs as
    # related when they share a team, or share a league *and* a slate; with
    # neither supplied it can only see the event, so a caller that has these
    # should pass them. Defaulted so existing leg files keep loading.
    team: str = ""
    slate: str = ""


@dataclass(frozen=True)
class ComboResult:
    combo_id: str
    legs: list[TotalLeg]
    # Product of the leg probabilities, treating the legs as unrelated.
    raw_probability: float
    # Joint probability under the correlation model. Equal to raw_probability
    # when the model finds no relationship among the legs.
    adjusted_probability: float
    # adjusted - raw. Signed, and positive whenever the legs are positively
    # correlated: for a combo where every leg must hit, correlation raises the
    # joint probability. The field it replaced was named "penalty" and was
    # subtracted, which had the sign backwards.
    correlation_adjustment: float
    # What the legs cost bought as a unit: the product of the leg prices. The
    # field it replaced averaged the leg prices, which is not comparable with a
    # joint probability.
    synthetic_cost_cents: float
    fair_price_cents: float
    expected_value_cents: float
    meets_target: bool
    notes: list[str] = field(default_factory=list)
