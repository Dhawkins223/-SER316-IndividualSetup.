"""Bookmaker margin removal.

A posted price is not a probability. The inverted prices of a market's
selections sum to more than one, and the excess is the bookmaker's margin. How
that margin is attributed back to the selections changes the resulting
probabilities, and the choice is not cosmetic: it moves the baseline every model
in this repository is graded against.

Proportional (multiplicative) normalization is the common default and the one
this repository shipped first. It assumes every selection carries the margin in
proportion to its own price, which is known to be wrong in the direction of the
favourite-longshot bias: books load proportionally more margin onto longshots.

Five methods are implemented so the assumption is explicit and testable rather
than implied:

- ``multiplicative``  p_i = pi_i / S
- ``additive``        p_i = pi_i - (S - 1) / n
- ``power``           p_i = pi_i ** k,      solve k for sum(p) = 1
- ``shin``            insider-trading model, solve z for sum(p) = 1
- ``odds_ratio``      constant odds ratio,  solve OR for sum(p) = 1

Evidence: Strumbelj (2014), "On determining probability forecasts from betting
odds" (International Journal of Forecasting 30(4)), finds Shin probabilities are
better calibrated forecasts than basic normalization across five team sports.
Clarke (2017), "Adjusting bookmaker's odds to allow for overround", surveys the
same family. Neither result licenses treating a de-vigged price as a model
output; it is a better baseline, not an edge.

Values stay ``Decimal`` end to end, matching the repository's rule that exact
probability values keep their scale until a serialization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow
from functools import lru_cache

ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
FOUR = Decimal(4)

DEVIG_METHODS: tuple[str, ...] = ("multiplicative", "additive", "power", "shin", "odds_ratio")
DEFAULT_DEVIG_METHOD = "shin"

_TOLERANCE = Decimal("1e-15")
_MAX_ITERATIONS = 300
_PROBABILITY_FLOOR = Decimal("1e-9")
_POWER_UPPER_BOUND = Decimal(60)
_SHIN_UPPER_BOUND = Decimal("0.99")
_ODDS_RATIO_LOWER_BOUND = Decimal("1e-9")


@dataclass(frozen=True)
class DevigResult:
    """One margin-removal outcome, including how it was reached.

    ``converged`` is false when the solver could not bracket a root; callers must
    treat that as a failed de-vig rather than silently using the numbers.
    """

    method: str
    probabilities: tuple[Decimal, ...]
    booksum: Decimal
    overround: Decimal
    converged: bool
    iterations: int
    parameter: Decimal | None
    notes: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.converged and bool(self.probabilities)

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "probabilities": [str(value) for value in self.probabilities],
            "booksum": str(self.booksum),
            "overround": str(self.overround),
            "converged": self.converged,
            "iterations": self.iterations,
            "parameter": None if self.parameter is None else str(self.parameter),
            "notes": list(self.notes),
        }


def _clean(implied: list[Decimal] | tuple[Decimal, ...]) -> list[Decimal]:
    if len(implied) < 2:
        raise ValueError("devig_requires_at_least_two_selections")
    cleaned: list[Decimal] = []
    for value in implied:
        probability = value if isinstance(value, Decimal) else Decimal(str(value))
        if probability <= ZERO:
            raise ValueError("devig_requires_positive_implied_probabilities")
        if probability >= ONE:
            raise ValueError("devig_requires_implied_probabilities_below_one")
        cleaned.append(probability)
    return cleaned


def american_odds_to_implied(odds: Decimal | int | float | str) -> Decimal:
    """Convert an American price to its implied probability, margin included."""

    value = odds if isinstance(odds, Decimal) else Decimal(str(odds))
    if value == ZERO:
        raise ValueError("american_odds_cannot_be_zero")
    if value < ZERO:
        return abs(value) / (abs(value) + Decimal(100))
    return Decimal(100) / (value + Decimal(100))


def decimal_odds_to_implied(odds: Decimal | int | float | str) -> Decimal:
    """Convert decimal odds to their implied probability, margin included."""

    value = odds if isinstance(odds, Decimal) else Decimal(str(odds))
    if value <= ONE:
        raise ValueError("decimal_odds_must_exceed_one")
    return ONE / value


def booksum(implied: list[Decimal]) -> Decimal:
    """Sum of inverted prices. Above one by the bookmaker's margin."""

    return sum(_clean(implied), ZERO)


def overround(implied: list[Decimal]) -> Decimal:
    """Bookmaker margin expressed in probability points."""

    return booksum(implied) - ONE


def _renormalized(values: list[Decimal]) -> list[Decimal]:
    """Force an exact sum of one, absorbing the solver's residual.

    Bisection leaves a residual far below any decision threshold, but downstream
    code compares these numbers against prices and expects a proper
    distribution, so the residual is removed rather than carried.
    """

    floored = [value if value > _PROBABILITY_FLOOR else _PROBABILITY_FLOOR for value in values]
    total = sum(floored, ZERO)
    if total <= ZERO:
        raise ValueError("devig_produced_non_positive_total")
    return [value / total for value in floored]


def _solve(objective, low: Decimal, high: Decimal) -> tuple[Decimal | None, int, bool]:
    """Bisect a monotone objective for its root, reporting failure honestly."""

    try:
        low_value = objective(low)
        high_value = objective(high)
    except (InvalidOperation, DivisionByZero, Overflow, ValueError):
        return None, 0, False
    if low_value == ZERO:
        return low, 0, True
    if high_value == ZERO:
        return high, 0, True
    if (low_value > ZERO) == (high_value > ZERO):
        return None, 0, False
    middle = (low + high) / TWO
    for iteration in range(1, _MAX_ITERATIONS + 1):
        middle = (low + high) / TWO
        try:
            middle_value = objective(middle)
        except (InvalidOperation, DivisionByZero, Overflow, ValueError):
            return None, iteration, False
        if middle_value == ZERO or (high - low) < _TOLERANCE:
            return middle, iteration, True
        if (middle_value > ZERO) == (low_value > ZERO):
            low, low_value = middle, middle_value
        else:
            high, high_value = middle, middle_value
    return middle, _MAX_ITERATIONS, False


def multiplicative_probabilities(implied: list[Decimal]) -> DevigResult:
    """Proportional normalization: every selection keeps its share of the book.

    Simple and always defined, but it removes margin in proportion to price, so
    it inherits whatever favourite-longshot bias the book priced in.
    """

    cleaned = _clean(implied)
    total = sum(cleaned, ZERO)
    return DevigResult(
        method="multiplicative",
        probabilities=tuple(_renormalized([value / total for value in cleaned])),
        booksum=total,
        overround=total - ONE,
        converged=True,
        iterations=0,
        parameter=None,
        notes=(),
    )


def additive_probabilities(implied: list[Decimal]) -> DevigResult:
    """Remove an equal number of probability points from every selection.

    This is the method most likely to fail on longshots: subtracting a flat
    margin share from a small probability can drive it to or below zero, which
    is reported rather than hidden.
    """

    cleaned = _clean(implied)
    total = sum(cleaned, ZERO)
    share = (total - ONE) / Decimal(len(cleaned))
    raw = [value - share for value in cleaned]
    notes: tuple[str, ...] = ()
    if any(value <= ZERO for value in raw):
        notes = ("additive_produced_non_positive_probability",)
    return DevigResult(
        method="additive",
        probabilities=tuple(_renormalized(raw)),
        booksum=total,
        overround=total - ONE,
        converged=not notes,
        iterations=0,
        parameter=share,
        notes=notes,
    )


def power_probabilities(implied: list[Decimal]) -> DevigResult:
    """Raise each price to a common exponent k solving sum(pi ** k) = 1.

    Because every input is below one, a larger k shrinks longshots faster than
    favourites, which removes margin regressively in the direction the
    favourite-longshot literature expects.
    """

    cleaned = _clean(implied)
    total = sum(cleaned, ZERO)
    if total <= ONE:
        result = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="power",
            probabilities=result.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=True,
            iterations=0,
            parameter=ONE,
            notes=("non_positive_overround_normalized",),
        )

    def objective(exponent: Decimal) -> Decimal:
        return sum((value**exponent for value in cleaned), ZERO) - ONE

    exponent, iterations, converged = _solve(objective, ONE, _POWER_UPPER_BOUND)
    if exponent is None or not converged:
        fallback = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="power",
            probabilities=fallback.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=False,
            iterations=iterations,
            parameter=None,
            notes=("power_solver_did_not_converge",),
        )
    return DevigResult(
        method="power",
        probabilities=tuple(_renormalized([value**exponent for value in cleaned])),
        booksum=total,
        overround=total - ONE,
        converged=True,
        iterations=iterations,
        parameter=exponent,
        notes=(),
    )


def shin_probabilities(implied: list[Decimal]) -> DevigResult:
    """Shin's insider-trading model.

    Shin (1993) derives the book from a market maker pricing against a fraction
    ``z`` of informed traders. Solving

        p_i = (sqrt(z**2 + 4 * (1 - z) * pi_i**2 / S) - z) / (2 * (1 - z))

    for the z that makes the probabilities sum to one removes proportionally
    more margin from longshots than from favourites. ``parameter`` returns that
    z, which is itself a useful diagnostic: an implausibly large z usually means
    the two prices came from different books or different timestamps.
    """

    cleaned = _clean(implied)
    total = sum(cleaned, ZERO)
    if total <= ONE:
        result = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="shin",
            probabilities=result.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=True,
            iterations=0,
            parameter=ZERO,
            notes=("non_positive_overround_normalized",),
        )

    def selection_probability(insider_share: Decimal, price: Decimal) -> Decimal:
        radicand = insider_share**2 + FOUR * (ONE - insider_share) * price**2 / total
        return (radicand.sqrt() - insider_share) / (TWO * (ONE - insider_share))

    def objective(insider_share: Decimal) -> Decimal:
        return sum((selection_probability(insider_share, value) for value in cleaned), ZERO) - ONE

    insider_share, iterations, converged = _solve(objective, ZERO, _SHIN_UPPER_BOUND)
    if insider_share is None or not converged:
        fallback = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="shin",
            probabilities=fallback.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=False,
            iterations=iterations,
            parameter=None,
            notes=("shin_solver_did_not_converge",),
        )
    solved = [selection_probability(insider_share, value) for value in cleaned]
    return DevigResult(
        method="shin",
        probabilities=tuple(_renormalized(solved)),
        booksum=total,
        overround=total - ONE,
        converged=True,
        iterations=iterations,
        parameter=insider_share,
        notes=(),
    )


def odds_ratio_probabilities(implied: list[Decimal]) -> DevigResult:
    """Cheung's constant odds-ratio method.

    Assumes one odds ratio ``OR`` links fair odds to posted odds for every
    selection, so ``p / (1 - p) = OR * pi / (1 - pi)``, and solves for the OR
    that makes the probabilities sum to one.
    """

    cleaned = _clean(implied)
    total = sum(cleaned, ZERO)
    if total <= ONE:
        result = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="odds_ratio",
            probabilities=result.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=True,
            iterations=0,
            parameter=ONE,
            notes=("non_positive_overround_normalized",),
        )

    def selection_probability(ratio: Decimal, price: Decimal) -> Decimal:
        return ratio * price / (ONE - price + ratio * price)

    def objective(ratio: Decimal) -> Decimal:
        return sum((selection_probability(ratio, value) for value in cleaned), ZERO) - ONE

    ratio, iterations, converged = _solve(objective, _ODDS_RATIO_LOWER_BOUND, ONE)
    if ratio is None or not converged:
        fallback = multiplicative_probabilities(cleaned)
        return DevigResult(
            method="odds_ratio",
            probabilities=fallback.probabilities,
            booksum=total,
            overround=total - ONE,
            converged=False,
            iterations=iterations,
            parameter=None,
            notes=("odds_ratio_solver_did_not_converge",),
        )
    solved = [selection_probability(ratio, value) for value in cleaned]
    return DevigResult(
        method="odds_ratio",
        probabilities=tuple(_renormalized(solved)),
        booksum=total,
        overround=total - ONE,
        converged=True,
        iterations=iterations,
        parameter=ratio,
        notes=(),
    )


_METHODS = {
    "multiplicative": multiplicative_probabilities,
    "additive": additive_probabilities,
    "power": power_probabilities,
    "shin": shin_probabilities,
    "odds_ratio": odds_ratio_probabilities,
}

# Solving a market is the expensive half of building a board, and the same
# market gets solved over and over. Every book on a slate posts from the same
# short list of standard prices, `method_disagreement` runs all five methods on
# each market, and the consensus solves each book separately -- so one slate asks
# for the same price vector dozens of times. Profiling a 60-game board put 870 ms
# of a 983 ms build inside these solvers, nearly all of it in the power method's
# bisection.
#
# Each solver is a pure function of the price vector returning a frozen result of
# tuples, so the answer can be remembered rather than recomputed. The cache is
# bounded because a long-running collector would otherwise accumulate one entry
# per distinct market it ever saw.
_SOLVER_CACHE_SIZE = 4096


@lru_cache(maxsize=_SOLVER_CACHE_SIZE)
def _solve_method(method: str, implied: tuple[Decimal, ...]) -> DevigResult:
    return _METHODS[method](list(implied))


def solver_cache_info() -> dict[str, int]:
    """Hits, misses, and size of the de-vig solver cache, for diagnostics."""
    info = _solve_method.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "maxsize": info.maxsize or 0,
        "currsize": info.currsize,
    }


def clear_solver_cache() -> None:
    """Drop every remembered solve. Only useful in tests and diagnostics."""
    _solve_method.cache_clear()


def remove_margin(
    implied: list[Decimal],
    *,
    method: str = DEFAULT_DEVIG_METHOD,
    fallback_method: str | None = "multiplicative",
) -> DevigResult:
    """Remove bookmaker margin with a named method.

    When the requested solver fails to converge and a ``fallback_method`` is
    given, the fallback's probabilities are returned under the fallback's own
    method name with a note recording the substitution. Nothing here reports a
    converged result it did not reach.
    """

    if method not in _METHODS:
        raise ValueError(f"unknown_devig_method:{method}")
    result = _solve_method(method, tuple(implied))
    if result.converged or not fallback_method:
        return result
    if fallback_method not in _METHODS:
        raise ValueError(f"unknown_devig_method:{fallback_method}")
    fallback = _solve_method(fallback_method, tuple(implied))
    return DevigResult(
        method=fallback.method,
        probabilities=fallback.probabilities,
        booksum=fallback.booksum,
        overround=fallback.overround,
        converged=fallback.converged,
        iterations=fallback.iterations,
        parameter=fallback.parameter,
        notes=tuple(fallback.notes) + (f"fell_back_from_{method}",) + tuple(result.notes),
    )


def compare_methods(implied: list[Decimal]) -> dict[str, DevigResult]:
    """Run every method on one market.

    The spread between methods is the honest size of the de-vig assumption. When
    it is wide relative to a candidate edge, the edge is a statement about the
    de-vig choice rather than about the event.
    """

    key = tuple(implied)
    return {name: _solve_method(name, key) for name in _METHODS}


def method_disagreement(implied: list[Decimal]) -> Decimal:
    """Largest gap between methods for any one selection, in probability points."""

    results = [result for result in compare_methods(implied).values() if result.usable]
    if len(results) < 2:
        return ZERO
    widest = ZERO
    for index in range(len(results[0].probabilities)):
        values = [result.probabilities[index] for result in results]
        gap = max(values) - min(values)
        if gap > widest:
            widest = gap
    return widest
