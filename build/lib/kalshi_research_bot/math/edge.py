from __future__ import annotations

from .implied import clamp_probability


def buy_yes_ev_cents(probability: float, yes_price_cents: float, fee_cents: float = 0.0) -> float:
    probability = clamp_probability(probability)
    return round((probability * 100.0) - yes_price_cents - fee_cents, 2)


def buy_no_ev_cents(probability: float, no_price_cents: float, fee_cents: float = 0.0) -> float:
    probability = clamp_probability(probability)
    return round(((1.0 - probability) * 100.0) - no_price_cents - fee_cents, 2)
