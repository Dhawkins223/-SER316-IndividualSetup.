"""What a multi-leg slip is actually worth, computed rather than asserted.

This is the engine behind the Algorithm Result screen, and the reason it can
exist without contradicting the research program: **none of it claims to beat a
market price.** Every number here is arithmetic on prices the market already
posted, or a simulation whose assumptions are declared. The rejected hypothesis
was "our model forecasts better than the close"; the statement "your nineteen-leg
combo needs 4.69% to break even and hits 4.2% of the time" is a different claim
entirely, and it is one the prices themselves support.

Three quantities do the work, and the gap between the first two is the finding:

**Break-even probability** is exact. A slip paying decimal odds ``D`` must hit
more than ``1/D`` of the time to profit. Nothing is estimated.

**Independent probability** multiplies the de-vigged leg probabilities as though
the legs were unrelated. This is what almost every parlay calculator reports and
it is *wrong whenever two legs touch the same game* — which, on a long slip, is
most of them.

**Correlation-adjusted probability** is the same slip through a Gaussian copula.
Each leg keeps its marginal probability exactly; what changes is how the legs
fail together. Positively correlated legs make a parlay *more* likely to hit
than independence suggests, and simultaneously make the outcome far more
volatile — both halves matter, and reporting only the first is how correlated
slips get sold as safe.

## The honesty boundary, and what ``independence_error`` is not

The correlation inputs are **structural assumptions, not measurements**. Two
legs in one game are correlated; nobody here has measured by how much on this
platform's own data. ``CorrelationModel.source`` carries that distinction into
every report, ``measured`` is False until backlog E-45 (correlation stability)
replaces the defaults with estimates, and the numbers are deliberately round so
nobody mistakes them for findings.

That boundary is easy to walk across without noticing, and this module did.
``independence_error`` looks like an answer to backlog E-44 -- does independent
multiplication materially misprice combinations? -- and it is not one. The gap
it reports is a monotone function of the assumed ``same_event`` coefficient: on
one fixed slip it reads 1.01x at rho=0, 5.33x at rho=0.4, and 15.67x at rho=0.8.
Reporting the middle figure as a measured effect restates the assumption and
calls it evidence.

So ``independence_error`` is a **sensitivity**: it says what the naive product
costs *if* legs are correlated as assumed. Answering E-44 needs settled joint
outcomes for legs that actually shared a game. The machinery is exact; the
inputs are declared; the difference between those two is the whole point.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from math import isfinite, prod, sqrt
from typing import Any, Iterable, Mapping, Sequence

from .math.normal import normal_quantile

# 10,000 draws puts the Monte Carlo standard error on a 4% probability near
# 0.2 points -- finer than the quantity is meaningful to, and fast enough to run
# inside a request.
DEFAULT_DRAWS = 10_000

# Fixed so the same slip always produces the same report. A verdict that moves
# when nobody changed anything is not a verdict.
DEFAULT_SEED = 20260828

_MIN_P = 1e-9
_MAX_P = 1.0 - 1e-9


def _clamp(probability: float) -> float:
    return min(_MAX_P, max(_MIN_P, float(probability)))


@dataclass(frozen=True)
class SlipLeg:
    """One selection, priced by the market and de-vigged before it arrives here.

    ``fair_probability`` must already have the bookmaker margin removed --
    ``math.devig`` does that -- because multiplying raw implied probabilities
    manufactures an edge equal to the compounded margin, which on a long slip is
    most of the apparent value.
    """

    leg_id: str
    selection: str
    decimal_odds: float
    fair_probability: float
    event_id: str = ""
    league: str = ""
    market: str = ""
    team: str = ""
    # The day the games are played. League correlation is a *slate* effect --
    # shared weather, shared officiating pool, shared news cycle -- so it applies
    # only within a slate. Without this, two NBA games in different seasons would
    # be treated as correlated, which manufactures an error out of nothing.
    slate: str = ""
    # When the price was observed, and whether the source was healthy. A verdict
    # computed from a stale board is worse than no verdict, because it looks
    # current.
    quoted_at: str = ""
    source_state: str = ""

    def __post_init__(self) -> None:
        if self.decimal_odds <= 1.0:
            raise ValueError(f"decimal_odds_must_exceed_one:{self.leg_id}")
        if not 0.0 < self.fair_probability < 1.0:
            raise ValueError(f"fair_probability_out_of_range:{self.leg_id}")

    @property
    def break_even(self) -> float:
        """What this leg alone must hit to be worth its price."""
        return 1.0 / self.decimal_odds

    @property
    def edge(self) -> float:
        """De-vigged probability less the price's break-even.

        Against a de-vigged price this is a *residual*, not a forecast: it is
        near zero for an efficiently priced leg by construction. A large positive
        value means the books disagree with each other, not that a model knows
        better.
        """
        return self.fair_probability - self.break_even


@dataclass(frozen=True)
class CorrelationModel:
    """How legs are assumed to move together.

    Deliberately coarse. Precision here would be false: these are structural
    priors about sport, not estimates from this platform's data, and the round
    numbers are a signal to that effect.
    """

    same_event: float = 0.40
    same_team: float = 0.25
    same_league_same_slate: float = 0.05
    unrelated: float = 0.0
    source: str = "structural_assumption"
    measured: bool = False

    def between(self, a: SlipLeg, b: SlipLeg) -> float:
        if a.event_id and a.event_id == b.event_id:
            return self.same_event
        if a.team and a.team == b.team:
            return self.same_team
        # Same league is not enough: the coefficient is a same-*slate* effect, so
        # it needs both legs on the same day. Two NBA games a season apart share
        # a league and nothing else.
        if a.league and a.league == b.league and a.slate and a.slate == b.slate:
            return self.same_league_same_slate
        return self.unrelated

    def as_dict(self) -> dict[str, Any]:
        return {
            "same_event": self.same_event,
            "same_team": self.same_team,
            "same_league_same_slate": self.same_league_same_slate,
            "unrelated": self.unrelated,
            "source": self.source,
            "measured": self.measured,
        }


class UnmodellableSlip(ValueError):
    """Raised when this engine cannot honestly report on a slip.

    Two causes. Legs whose *relationship* a copula cannot express -- mutually
    exclusive or deterministically linked selections on one market of one game.
    And a slip whose combined odds exceed what a float can hold, where the
    break-even collapses to exactly zero and every figure derived from it stops
    meaning anything.
    """


def conflicting_pairs(legs: Sequence[SlipLeg]) -> list[tuple[str, str]]:
    """Legs on the same market of the same game, which cannot simply co-occur.

    A Gaussian copula models legs that are *dependent*. It cannot model legs that
    are mutually exclusive: home and away in one moneyline, or over and under on
    one total, are assigned a positive correlation by the same-event rule and
    come back with a cheerful joint probability for an outcome that can never
    happen. Two opposing 50% legs report roughly a third rather than zero, and
    everything downstream inherits that.

    Nested selections on one market -- over 152.5 and over 157.5 -- are not
    mutually exclusive but are deterministically linked, which a correlation
    coefficient also cannot express. Both cases are the same failure of the
    model's vocabulary, so both are refused rather than approximated.
    """

    conflicts: list[tuple[str, str]] = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            a, b = legs[i], legs[j]
            if a.event_id and a.event_id == b.event_id and a.market and a.market == b.market:
                conflicts.append((a.leg_id, b.leg_id))
    return conflicts


def _require_representable(legs: Sequence[SlipLeg]) -> None:
    """Refuse a slip whose payout overflows a float.

    ``prod(odds)`` runs away with the leg count: at 2.0 per leg it passes the
    double-precision ceiling around 1,025 legs, and at 100.0 per leg around 155.
    Past that the product is ``inf``, so ``break_even_probability`` is exactly
    0.0 -- and a break-even of zero is not a small number, it is a broken one.
    The verdict then divides by it and raises ZeroDivisionError, which is at
    least loud; the quieter failure is a slip reported as needing 0% to break
    even, which reads as a certainty.
    """

    odds = prod(leg.decimal_odds for leg in legs)
    if not isfinite(odds):
        raise UnmodellableSlip(
            f"slip_payout_exceeds_float_range:{len(legs)}_legs. The combined "
            "odds overflow double precision, so the break-even probability "
            "collapses to zero and no figure derived from it is meaningful."
        )


def _require_modellable(legs: Sequence[SlipLeg]) -> None:
    _require_representable(legs)
    conflicts = conflicting_pairs(legs)
    if conflicts:
        pairs = ", ".join(f"{a}+{b}" for a, b in conflicts[:4])
        raise UnmodellableSlip(
            "slip_contains_same_market_legs_on_one_event:"
            f"{pairs}. Selections from one market of one game are mutually "
            "exclusive or deterministically linked; correlation cannot express "
            "either, so no joint probability is reported."
        )


# ── exact arithmetic ────────────────────────────────────────────────────────


def break_even_probability(legs: Sequence[SlipLeg]) -> float:
    """Exact. The slip pays ``prod(odds)``, so it must hit ``1/prod(odds)``."""

    if not legs:
        raise ValueError("slip_requires_at_least_one_leg")
    return 1.0 / prod(leg.decimal_odds for leg in legs)


def independent_probability(legs: Sequence[SlipLeg]) -> float:
    """The naive product, kept explicitly so its error can be reported."""

    if not legs:
        raise ValueError("slip_requires_at_least_one_leg")
    return prod(_clamp(leg.fair_probability) for leg in legs)


def combined_decimal_odds(legs: Sequence[SlipLeg]) -> float:
    return prod(leg.decimal_odds for leg in legs)


# ── correlation machinery ───────────────────────────────────────────────────


def correlation_matrix(
    legs: Sequence[SlipLeg], model: CorrelationModel | None = None
) -> list[list[float]]:
    settings = model or CorrelationModel()
    size = len(legs)
    matrix = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            value = settings.between(legs[i], legs[j])
            matrix[i][j] = matrix[j][i] = value
    return matrix


def _cholesky(matrix: Sequence[Sequence[float]]) -> list[list[float]] | None:
    """Lower-triangular factor, or ``None`` when the matrix is not positive definite."""

    size = len(matrix)
    lower = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1):
            total = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                residual = matrix[i][i] - total
                if residual <= 1e-12:
                    return None
                lower[i][j] = sqrt(residual)
            else:
                lower[i][j] = (matrix[i][j] - total) / lower[j][j]
    return lower


def factor_correlation(
    matrix: Sequence[Sequence[float]],
) -> tuple[list[list[float]], float]:
    """Factor the matrix, shrinking toward the identity until it factors.

    A correlation matrix assembled from pairwise rules is not guaranteed to be
    positive semi-definite -- three legs can each be 0.4 correlated with the
    other two in a way no joint distribution permits. Rather than fail or
    silently produce nonsense, shrink toward independence until the matrix is
    admissible and report how much shrinkage that took, because a large value
    means the assumed structure was close to incoherent.
    """

    weight = 0.0
    size = len(matrix)
    while weight <= 1.0:
        candidate = [
            [
                matrix[i][j] * (1.0 - weight) + (1.0 if i == j else 0.0) * weight
                for j in range(size)
            ]
            for i in range(size)
        ]
        factored = _cholesky(candidate)
        if factored is not None:
            return factored, weight
        weight = round(weight + 0.02, 4)
    return [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)], 1.0


# ── simulation ──────────────────────────────────────────────────────────────


def _precision(hits: int) -> str:
    """How much of the reported probability is signal.

    Relative standard error on a simulated proportion is roughly 1/sqrt(hits),
    independent of the draw count -- so the hit count, not the draw count, is
    what says whether a figure is usable. Under 30 hits the estimate moves by
    more than a fifth between seeds and should not be quoted to two decimals.
    """

    if hits >= 400:
        return "good"
    if hits >= 100:
        return "usable"
    if hits >= 30:
        return "coarse"
    return "insufficient_draws"


def simulate_correlation_adjustment(
    legs: Sequence[SlipLeg],
    *,
    model: CorrelationModel | None = None,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """The joint probability as the exact product plus a simulated difference.

    ``simulate_slip`` estimates the joint directly, which is what you want when
    the joint itself is the answer. It is the wrong tool for the *adjustment*,
    because that is a difference of two numbers near 0.85 whose own Monte Carlo
    error at a few thousand draws is around 0.006 -- larger than the adjustment
    at the correlations this model uses. Measured on a two-leg slip at 0.93 and
    0.91 with rho=0.25, the true adjustment is +0.0067, while ten seeds at 4,000
    draws returned anything from -0.0018 to +0.0137. The sign was wrong on the
    first seed tried, which is how this was found.

    Here the correlated and independent outcomes are evaluated on the *same*
    normal draws. Both are functions of one random vector, so their difference
    is non-zero only on draws where the two disagree -- a small fraction at
    small rho -- and its variance is proportional to that disagreement rate
    rather than to the probability itself. The independent case is then supplied
    by the exact product rather than by its own estimate, which is a control
    variate: the part of the answer that is known exactly is never simulated.

    Both quantities are reported with the uncertainty that belongs to them: the
    adjustment carries its own standard error, and ``adjustment_resolved`` says
    whether it is distinguishable from zero at all.
    """

    if not legs:
        raise ValueError("slip_requires_at_least_one_leg")
    if draws < 1:
        raise ValueError("draws_must_be_positive")
    _require_modellable(legs)

    settings = model or CorrelationModel()
    matrix = correlation_matrix(legs, settings)
    size = len(legs)
    # Whether the two paths are the *same function* is a property of the matrix,
    # not of the draws. With an identity matrix the correlated path reduces to
    # the independent one, they cannot disagree on any draw, and the product is
    # the exact answer. Reading that off the draw count instead would call a
    # correlated slip "exact" whenever too few draws happened to disagree.
    structurally_independent = not any(
        matrix[i][j] != 0.0 for i in range(size) for j in range(i + 1, size)
    )
    lower, shrinkage = factor_correlation(matrix)
    thresholds = [normal_quantile(_clamp(leg.fair_probability)) for leg in legs]
    rng = random.Random(seed)

    difference_sum = 0
    disagreements = 0
    correlated_hits = 0
    independent_hits = 0
    for _ in range(draws):
        normals = [rng.gauss(0.0, 1.0) for _ in range(size)]
        correlated_all = True
        independent_all = True
        for i in range(size):
            if normals[i] > thresholds[i]:
                independent_all = False
            row = lower[i]
            value = 0.0
            for k in range(i + 1):
                value += row[k] * normals[k]
            if value > thresholds[i]:
                correlated_all = False
            if not correlated_all and not independent_all:
                # Both are conjunctions, so neither can recover once false.
                break
        correlated_hits += correlated_all
        independent_hits += independent_all
        difference = int(correlated_all) - int(independent_all)
        if difference:
            difference_sum += difference
            disagreements += 1

    mean_difference = difference_sum / draws
    # The per-draw difference is in {-1, 0, 1}, so its second moment is exactly
    # the disagreement rate. No separate accumulator is needed.
    variance = max(disagreements / draws - mean_difference * mean_difference, 0.0)
    standard_error = sqrt(variance / draws)
    exact_independent = independent_probability(legs)
    adjusted = min(1.0, max(0.0, exact_independent + mean_difference))
    half_width = 1.959964 * standard_error
    return {
        "independent_probability": exact_independent,
        "hit_probability": adjusted,
        "correlation_adjustment": mean_difference,
        "adjustment_standard_error": standard_error,
        "adjustment_confidence_interval": [
            mean_difference - half_width,
            mean_difference + half_width,
        ],
        # True when the adjustment is known well enough to act on: either the
        # legs are structurally independent, so it is exactly zero, or the draws
        # established its sign. False means the point estimate is still the best
        # one available but its sign is not established, so nothing should be
        # ranked or decided on it.
        "adjustment_resolved": structurally_independent or abs(mean_difference) > half_width,
        "disagreement_rate": disagreements / draws,
        "hit_probability_interval": [
            max(0.0, adjusted - half_width),
            min(1.0, adjusted + half_width),
        ],
        # "exact" is not flattery. With no modelled correlation the factor is
        # the identity, the two paths are the same function of the same draws,
        # and they cannot disagree -- so the adjustment is exactly zero and the
        # answer is the closed-form product, carrying no Monte Carlo error at
        # all. Grading that by hit count would report a deep-tail product as
        # unusable when it is known precisely.
        "precision": "exact" if structurally_independent else _precision(correlated_hits),
        "structurally_independent": structurally_independent,
        "correlated_hits": correlated_hits,
        "independent_hits": independent_hits,
        "draws": draws,
        "seed": seed,
        "correlation_shrinkage": shrinkage,
    }


def simulate_slip(
    legs: Sequence[SlipLeg],
    *,
    model: CorrelationModel | None = None,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Hit probability under a Gaussian copula, with the marginals preserved.

    Each leg becomes a threshold on a standard normal: leg ``i`` hits when its
    normal draw falls below ``Phi^-1(p_i)``. Correlating the normals correlates
    the outcomes while every leg keeps exactly the probability the market gave
    it -- which is what makes this an adjustment to the *joint* distribution
    rather than a second opinion on any single price.
    """

    if not legs:
        raise ValueError("slip_requires_at_least_one_leg")
    if draws < 1:
        raise ValueError("draws_must_be_positive")
    _require_modellable(legs)

    settings = model or CorrelationModel()
    lower, shrinkage = factor_correlation(correlation_matrix(legs, settings))
    thresholds = [normal_quantile(_clamp(leg.fair_probability)) for leg in legs]
    size = len(legs)
    rng = random.Random(seed)

    hits = 0
    leg_hits = [0] * size
    for _ in range(draws):
        normals = [rng.gauss(0.0, 1.0) for _ in range(size)]
        all_hit = True
        for i in range(size):
            row = lower[i]
            value = 0.0
            for k in range(i + 1):
                value += row[k] * normals[k]
            if value <= thresholds[i]:
                leg_hits[i] += 1
            else:
                all_hit = False
        if all_hit:
            hits += 1

    hit_probability = hits / draws
    # Binomial standard error on the simulated proportion. Reported so nobody
    # reads more precision into the figure than the draw count supports.
    standard_error = sqrt(max(hit_probability * (1.0 - hit_probability), 0.0) / draws)
    return {
        "hit_probability": hit_probability,
        "standard_error": standard_error,
        # A long slip is a deep-tail event, and a fixed draw count that is ample
        # for a 4% parlay returns a handful of hits for a 0.05% one -- where the
        # estimate swings by a third between seeds. Precision is reported from
        # the hit count rather than assumed from the draw count.
        "hits": hits,
        "precision": _precision(hits),
        "relative_standard_error": (standard_error / hit_probability) if hit_probability > 0 else None,
        "confidence_interval": [
            max(0.0, hit_probability - 1.959964 * standard_error),
            min(1.0, hit_probability + 1.959964 * standard_error),
        ],
        "draws": draws,
        "seed": seed,
        "correlation_shrinkage": shrinkage,
        "realized_leg_rates": [count / draws for count in leg_hits],
    }


# ── grading ─────────────────────────────────────────────────────────────────

_RISK_ORDER = ("low", "moderate", "high", "very_high")


def risk_tier(
    *, leg_count: int, correlated_pairs: int, hit_probability: float
) -> str:
    """A label, from the three things that actually make a slip fragile.

    Length compounds failure, correlation hides it, and a low hit probability
    means variance dominates whatever edge exists. Thresholds are product
    judgement rather than statistics, and are named as a tier rather than a
    number so they are not mistaken for a measurement.
    """

    score = 0
    if leg_count >= 6:
        score += 1
    if leg_count >= 12:
        score += 1
    if correlated_pairs >= 3:
        score += 1
    if hit_probability < 0.10:
        score += 1
    return _RISK_ORDER[min(score, len(_RISK_ORDER) - 1)]


def count_correlated_pairs(
    legs: Sequence[SlipLeg], model: CorrelationModel | None = None, *, threshold: float = 0.2
) -> int:
    settings = model or CorrelationModel()
    return sum(
        1
        for i in range(len(legs))
        for j in range(i + 1, len(legs))
        if settings.between(legs[i], legs[j]) >= threshold
    )


def analyze_slip(
    legs: Sequence[SlipLeg],
    *,
    stake: float = 1.0,
    model: CorrelationModel | None = None,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """The full report: what it costs, what it needs, and what it does.

    The joint comes from ``simulate_correlation_adjustment`` rather than from a
    direct simulation, which matters most where the legs share nothing: there
    the correlated and independent paths are the same function of the same
    draws, so the adjustment is exactly zero and the reported probability is the
    closed-form product. A direct simulation instead returns the product plus a
    few tenths of a point of noise, and that noise lands in
    ``edge_over_break_even`` and ``expected_value`` as if it were signal.
    """

    if not legs:
        raise ValueError("slip_requires_at_least_one_leg")
    # Finiteness is checked before the sign, because ``nan <= 0`` is False and a
    # NaN stake therefore walks straight past a positivity guard -- taking every
    # number downstream with it, and producing a payload that is not even valid
    # JSON, since ``NaN`` and ``Infinity`` are not JSON literals.
    if not isfinite(stake):
        raise ValueError("stake_must_be_finite")
    if stake <= 0:
        raise ValueError("stake_must_be_positive")
    _require_modellable(legs)

    settings = model or CorrelationModel()
    odds = combined_decimal_odds(legs)
    break_even = break_even_probability(legs)
    independent = independent_probability(legs)
    simulation = simulate_correlation_adjustment(legs, model=settings, draws=draws, seed=seed)
    adjusted = simulation["hit_probability"]

    payout = stake * odds
    profit_if_won = payout - stake
    expected_value = adjusted * profit_if_won - (1.0 - adjusted) * stake
    correlated_pairs = count_correlated_pairs(legs, settings)
    repricing = same_event_repricing_warning(legs, settings)

    return {
        "leg_count": len(legs),
        "combined_decimal_odds": odds,
        "stake": stake,
        "payout_if_won": payout,
        "break_even_probability": break_even,
        "independent_probability": independent,
        "hit_probability": adjusted,
        "hit_probability_interval": simulation["hit_probability_interval"],
        # Surfaced at the top level because a consumer rendering the headline
        # probability must be able to see that it is not yet worth quoting.
        "precision": simulation["precision"],
        # E-44 in one line: what treating the legs as unrelated would have cost.
        # Taken from the paired estimator, so it is the measured difference
        # rather than a subtraction of two separately simulated numbers.
        "independence_error": simulation["correlation_adjustment"],
        # False when the draws could not separate that difference from zero.
        "independence_error_resolved": simulation["adjustment_resolved"],
        "independence_error_ratio": (adjusted / independent) if independent > 0 else None,
        "expected_value": expected_value,
        "expected_value_ratio": expected_value / stake,
        # False whenever the slip mixes standalone leg prices with a correlated
        # joint distribution, which no book would actually sell at these prices.
        "expected_value_is_achievable": repricing is None,
        "same_event_repricing_warning": repricing,
        "edge_over_break_even": adjusted - break_even,
        "correlated_pairs": correlated_pairs,
        "risk_tier": risk_tier(
            leg_count=len(legs),
            correlated_pairs=correlated_pairs,
            hit_probability=adjusted,
        ),
        # A verdict is a claim about value, so it is withheld where the value is
        # not takeable rather than dressed up from an unachievable number.
        "verdict": (
            "not_priceable_from_standalone_legs"
            if repricing is not None and adjusted > break_even
            else _verdict(adjusted, break_even)
        ),
        "legs": [
            {
                "leg_id": leg.leg_id,
                "selection": leg.selection,
                "league": leg.league,
                "market": leg.market,
                "decimal_odds": leg.decimal_odds,
                "fair_probability": leg.fair_probability,
                "break_even": leg.break_even,
                "edge": leg.edge,
            }
            for leg in legs
        ],
        "simulation": simulation,
        "correlation_model": settings.as_dict(),
        # Carried on every report so a consumer cannot render these numbers as a
        # validated forecast without also having the disclaimer to hand.
        "evidence_class": "market_derived_arithmetic",
        "model_state": "baseline_only",
        "decision_status": "track_only",
    }


def same_event_repricing_warning(
    legs: Sequence[SlipLeg], model: CorrelationModel | None = None
) -> dict[str, Any] | None:
    """Flag a slip whose expected value is an artifact of mixing pricing regimes.

    This guard exists because the engine, working correctly, produces a wildly
    attractive number on a wildly unrealistic input. Correlation lifts a
    same-game parlay's hit probability well above the independent product -- that
    part is real, and it is the E-44 effect. But the leg prices it was computed
    from are *standalone* prices, and no book prices a same-game parlay by
    multiplying those: they reprice it jointly, which is exactly why same-game
    parlays carry their own restrictions and their own margin.

    Combining standalone prices with a correlated joint distribution therefore
    manufactures value that cannot be taken. Reporting the resulting expected
    value as an edge would be the single most misleading thing this module could
    do, so a slip with same-event legs says so instead.
    """

    settings = model or CorrelationModel()
    grouped: dict[str, int] = {}
    for entry in legs:
        if entry.event_id:
            grouped[entry.event_id] = grouped.get(entry.event_id, 0) + 1
    shared = {event: count for event, count in grouped.items() if count > 1}
    if not shared:
        return None
    return {
        "events_with_multiple_legs": len(shared),
        "legs_sharing_an_event": sum(shared.values()),
        "assumed_within_event_correlation": settings.same_event,
        "reason": "same_event_legs_priced_standalone",
        "detail": (
            "This slip has legs from the same game. The correlation-adjusted hit "
            "probability is the honest joint estimate, but the expected value "
            "beside it is computed from standalone leg prices, and a book prices "
            "a same-game parlay jointly rather than by multiplying those. Treat "
            "the probability as informative and the expected value as not "
            "achievable at these prices."
        ),
        "expected_value_is_achievable": False,
    }


def _verdict(adjusted: float, break_even: float) -> str:
    if adjusted <= 0.0:
        return "no_realistic_path"
    ratio = adjusted / break_even
    if ratio >= 1.15:
        return "strong_value"
    if ratio >= 1.02:
        return "playable"
    if ratio >= 0.90:
        return "thin"
    return "trim_slip"


def recommend_trim(
    legs: Sequence[SlipLeg],
    *,
    stake: float = 1.0,
    model: CorrelationModel | None = None,
    draws: int = 2_000,
    seed: int = DEFAULT_SEED,
    minimum_legs: int = 2,
) -> dict[str, Any]:
    """Which legs to drop, by dropping the worst-priced ones first.

    Legs are ranked by their own edge against their own price and removed from
    the bottom, evaluating the slip at every length. This is a greedy search
    rather than an exhaustive one: nineteen legs is half a million subsets, and
    the ranking is a good proxy because a leg priced badly on its own is what
    drags a parlay down. Fewer draws are used per candidate because the choice
    only needs the ordering, not a precise level; the winner is re-scored at
    full resolution by the caller through ``analyze_slip``.
    """

    if len(legs) < minimum_legs:
        raise ValueError("slip_too_short_to_trim")

    settings = model or CorrelationModel()
    ranked = sorted(legs, key=lambda leg: leg.edge, reverse=True)

    best: dict[str, Any] | None = None
    ladder: list[dict[str, Any]] = []
    for size in range(len(ranked), minimum_legs - 1, -1):
        subset = ranked[:size]
        report = analyze_slip(subset, stake=stake, model=settings, draws=draws, seed=seed)
        entry = {
            "leg_count": size,
            "hit_probability": report["hit_probability"],
            "break_even_probability": report["break_even_probability"],
            "expected_value_ratio": report["expected_value_ratio"],
            "expected_value_is_achievable": report["expected_value_is_achievable"],
            "risk_tier": report["risk_tier"],
        }
        ladder.append(entry)
        # Ranking on an expected value the report itself marks unachievable
        # would recommend a trim built on the pricing artifact the guard exists
        # to withhold. Only takeable candidates compete.
        if not entry["expected_value_is_achievable"]:
            continue
        if best is None or entry["expected_value_ratio"] > best["expected_value_ratio"]:
            best = entry

    if best is None:
        # Every length still shares a game, so no subset can be priced from these
        # standalone legs. Say that rather than returning the least-bad artifact.
        return {
            "recommended_leg_count": None,
            "keep": [],
            "drop": [],
            "improvement_in_expected_value_ratio": None,
            "ladder": ladder,
            "note": (
                "No trim is recommendable: every candidate still contains legs "
                "from one game, so its expected value is not takeable at these "
                "standalone prices."
            ),
        }
    keep = ranked[: best["leg_count"]]
    dropped = ranked[best["leg_count"] :]
    return {
        "recommended_leg_count": best["leg_count"],
        "keep": [leg.leg_id for leg in keep],
        "drop": [leg.leg_id for leg in dropped],
        "improvement_in_expected_value_ratio": best["expected_value_ratio"]
        - ladder[0]["expected_value_ratio"],
        "ladder": ladder,
        "note": (
            "Ranked by each leg's own price-relative edge and trimmed from the "
            "bottom. Greedy, not exhaustive."
        ),
    }


# A price older than this is not a current quote. Matches the board's own
# one-hour freshness window, so the two cannot disagree about what "now" means.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 3600

# Source states that mean the price on the row cannot be trusted as current.
_UNUSABLE_SOURCE_STATES = frozenset({"stale", "blocked", "failed", "empty", "unavailable", "cached"})


def legs_from_board(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: Any = None,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    require_freshness: bool = True,
) -> list[SlipLeg]:
    """Build legs from board rows, refusing anything not fully priced *and* fresh.

    Two refusals, and both matter for the same reason. A row missing a de-vigged
    probability is skipped rather than defaulted, because substituting the raw
    implied price would quietly reintroduce the margin this calculation exists to
    remove. A row without a fresh, healthy quote is skipped too, because the
    output of this engine -- a hit probability, an edge, a verdict -- reads as a
    statement about right now, and computing it from a cached or blocked snapshot
    would be exactly the mislabelling the collection side refuses to do.

    ``require_freshness=False`` exists for grading a historical slip on purpose,
    where the rows are known to be old and nothing is being presented as current.
    """

    from datetime import datetime, timezone

    def parse(value: Any) -> Any:
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    moment = parse(now) or datetime.now(timezone.utc)

    legs: list[SlipLeg] = []
    for index, row in enumerate(rows):
        fair = row.get("no_vig_probability", row.get("fair_probability"))
        odds = row.get("decimal_odds")
        if fair is None or odds is None:
            continue

        if require_freshness:
            state = str(row.get("source_state") or row.get("freshness_state") or "").lower()
            if state in _UNUSABLE_SOURCE_STATES:
                continue
            quoted = parse(row.get("quoted_at") or row.get("api_fetched_at") or row.get("odds_timestamp"))
            if quoted is None:
                continue
            if (moment - quoted).total_seconds() > max_quote_age_seconds:
                continue
        try:
            legs.append(
                SlipLeg(
                    leg_id=str(row.get("leg_id") or row.get("id") or f"leg_{index + 1}"),
                    selection=str(row.get("selection") or ""),
                    decimal_odds=float(odds),
                    fair_probability=float(fair),
                    event_id=str(row.get("event_id") or ""),
                    league=str(row.get("league") or ""),
                    market=str(row.get("market_type") or row.get("market") or ""),
                    team=str(row.get("team") or ""),
                    slate=str(row.get("slate") or row.get("game_date") or ""),
                    quoted_at=str(row.get("quoted_at") or row.get("api_fetched_at") or ""),
                    source_state=str(row.get("source_state") or row.get("freshness_state") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return legs
