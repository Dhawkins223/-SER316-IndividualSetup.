from __future__ import annotations


def clamp_probability(probability: float) -> float:
    return max(0.0, min(1.0, probability))


def probability_to_cents(probability: float) -> float:
    return round(clamp_probability(probability) * 100.0, 2)


def cents_to_probability(cents: float) -> float:
    return clamp_probability(cents / 100.0)


def decimal_odds_to_probability(decimal_odds: float) -> float:
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be greater than 1.0")
    return clamp_probability(1.0 / decimal_odds)


def probability_to_decimal_odds(probability: float) -> float:
    probability = clamp_probability(probability)
    if probability <= 0:
        raise ValueError("probability must be greater than 0")
    return round(1.0 / probability, 6)


def american_odds_to_probability(american_odds: int | float) -> float:
    if american_odds == 0:
        raise ValueError("american odds cannot be zero")
    if american_odds > 0:
        return clamp_probability(100.0 / (american_odds + 100.0))
    return clamp_probability(abs(american_odds) / (abs(american_odds) + 100.0))


def probability_to_american_odds(probability: float) -> int:
    probability = clamp_probability(probability)
    if probability <= 0 or probability >= 1:
        raise ValueError("probability must be between 0 and 1")
    if probability >= 0.5:
        return round(-(probability / (1.0 - probability)) * 100.0)
    return round(((1.0 - probability) / probability) * 100.0)


def no_vig_probabilities(probabilities: list[float]) -> list[float]:
    if not probabilities:
        raise ValueError("at least one probability is required")
    cleaned = [clamp_probability(probability) for probability in probabilities]
    total = sum(cleaned)
    if total <= 0:
        raise ValueError("probabilities must sum to more than zero")
    return [probability / total for probability in cleaned]


def binary_no_vig_probability(yes_price_cents: float, no_price_cents: float) -> float:
    yes_probability, _ = no_vig_probabilities([cents_to_probability(yes_price_cents), cents_to_probability(no_price_cents)])
    return yes_probability
