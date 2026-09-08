from __future__ import annotations

from .implied import clamp_probability


def elo_home_probability(
    home_rating: float,
    away_rating: float,
    home_field_points: float = 0.0,
    scale: float = 400.0,
) -> float:
    adjusted_diff = (home_rating + home_field_points) - away_rating
    probability = 1.0 / (1.0 + 10.0 ** (-adjusted_diff / scale))
    return clamp_probability(probability)
