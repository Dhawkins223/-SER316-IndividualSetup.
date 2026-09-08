from __future__ import annotations

import math

from .implied import clamp_probability


def poisson_probability(k: int, expected: float) -> float:
    return math.exp(-expected) * expected**k / math.factorial(k)


def poisson_home_win_probability(
    home_expected: float,
    away_expected: float,
    max_score: int = 12,
) -> float:
    home_win = 0.0
    for home_score in range(max_score + 1):
        home_prob = poisson_probability(home_score, home_expected)
        for away_score in range(max_score + 1):
            if home_score > away_score:
                home_win += home_prob * poisson_probability(away_score, away_expected)
    return clamp_probability(home_win)
