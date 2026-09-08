from __future__ import annotations

from .implied import clamp_probability


def weighted_probability(parts: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in parts if weight > 0)
    if total_weight <= 0:
        raise ValueError("at least one positive model weight is required")
    probability = sum(probability * weight for probability, weight in parts if weight > 0) / total_weight
    return clamp_probability(probability)
