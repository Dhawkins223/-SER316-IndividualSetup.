"""Does anything this platform knows improve on the closing price?

Section O of `docs/sports-prediction-research-program.md` settled the first
question: a walk-forward Elo rating beats a home base rate and loses decisively
to the de-vigged closing line. That result rules out the model this platform
would otherwise have shipped, and it leaves exactly one question worth asking
next -- backlog E-24.

The right form of that question is not "model or market". It is: start from the
market's own probability, and ask whether adding what the model knows moves it
anywhere useful. In log-odds space that is a two-term regression,

    logit(p) = a + b * logit(market) + c * (logit(elo) - logit(market))

where `b` measures whether the market price should be trusted as posted (b = 1)
or shaded toward a coin flip (b < 1), and `c` is the only place a claim of edge
can live. If `c` is indistinguishable from zero, the rating adds nothing the
price did not already contain, and that is the answer.

Two disciplines make the answer usable rather than flattering:

1. **The fit is walk-forward too.** Coefficients for a season are estimated only
   from seasons that had already finished. A single regression fit over the whole
   history would report an in-sample improvement that no bettor could have had.

2. **The baseline is the market, not a coin flip.** The comparison that decides
   this is the blend against the closing price on identical games, paired, with
   an interval. Beating the base rate again would prove nothing.

Nothing here promotes a model. The output is a research verdict, and the two
coefficients are reported so the shape of the answer is visible rather than
summarized into a single number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import exp, log
from typing import Any, Mapping, Sequence

from .evaluation.calibration import _fit_logistic
from .evaluation.power import required_sample_for_score_improvement
from .sports_ratings import (
    EloConfig,
    GameResult,
    WalkForwardRow,
    _score_summary,
    paired_comparison,
    walk_forward,
)


MODEL_NAME = "market_blend_v1"
PROBABILITY_FLOOR = 1e-6
DEFAULT_MIN_TRAINING_ROWS = 300


@dataclass(frozen=True)
class MarketBlendConfig:
    """How much history a fit needs before its output is allowed to be scored.

    ``min_training_rows`` is a validity gate, not a tuning knob: a two-parameter
    logistic fit on a hundred noisy rows produces coefficients that describe the
    sample rather than the market.
    """

    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS
    elo: EloConfig = EloConfig()

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL_NAME,
            "min_training_rows": self.min_training_rows,
            "elo": self.elo.as_dict(),
        }


def _clip(probability: float) -> float:
    return min(1.0 - PROBABILITY_FLOOR, max(PROBABILITY_FLOOR, probability))


def logit(probability: Decimal | float) -> float:
    value = _clip(float(probability))
    return log(value / (1.0 - value))


def inverse_logit(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + exp(-value))
    exponential = exp(value)
    return exponential / (1.0 + exponential)


def _period(row: WalkForwardRow) -> str:
    """The refit boundary. NFL game ids lead with the season; else fall back to year."""
    leading = row.event_id.split("_", 1)[0]
    if len(leading) == 4 and leading.isdigit():
        return leading
    return str(row.start_time.year)


def _features(row: WalkForwardRow) -> list[float]:
    market_logit = logit(row.market_probability)  # type: ignore[arg-type]
    elo_logit = logit(row.elo_probability)
    return [1.0, market_logit, elo_logit - market_logit]


@dataclass(frozen=True)
class BlendFit:
    """One period's fit and the history it was estimated from."""

    period: str
    training_rows: int
    intercept: float
    market_weight: float
    model_weight: float
    converged: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "training_rows": self.training_rows,
            "intercept": self.intercept,
            "market_weight": self.market_weight,
            "model_weight": self.model_weight,
            "converged": self.converged,
        }


def walk_forward_blend(
    rows: Sequence[WalkForwardRow],
    *,
    config: MarketBlendConfig | None = None,
) -> tuple[list[tuple[WalkForwardRow, float]], list[BlendFit], dict[str, int]]:
    """Blend each period's forecasts using coefficients from earlier periods only.

    Rows without a market price cannot be blended and are reported as skipped
    rather than filled in with a substitute.
    """
    settings = config or MarketBlendConfig()
    priced = [row for row in rows if row.market_probability is not None]
    ordered = sorted(priced, key=lambda row: (row.start_time, row.event_id))

    skipped: dict[str, int] = {}
    if len(rows) != len(priced):
        skipped["no_market_price"] = len(rows) - len(priced)

    blended: list[tuple[WalkForwardRow, float]] = []
    fits: list[BlendFit] = []
    training_design: list[list[float]] = []
    training_outcomes: list[int] = []
    current_period: str | None = None
    coefficients: list[float] | None = None

    pending_design: list[list[float]] = []
    pending_outcomes: list[int] = []

    for row in ordered:
        period = _period(row)
        if period != current_period:
            # Everything from the period that just ended becomes training data
            # for the next one, and never for itself.
            training_design.extend(pending_design)
            training_outcomes.extend(pending_outcomes)
            pending_design = []
            pending_outcomes = []
            current_period = period
            if len(training_design) >= settings.min_training_rows and len(set(training_outcomes)) > 1:
                estimated, converged = _fit_logistic(training_design, training_outcomes)
                coefficients = estimated
                fits.append(
                    BlendFit(
                        period=period,
                        training_rows=len(training_design),
                        intercept=estimated[0],
                        market_weight=estimated[1],
                        model_weight=estimated[2],
                        converged=converged,
                    )
                )
            else:
                coefficients = None

        features = _features(row)
        pending_design.append(features)
        pending_outcomes.append(row.home_win)

        if coefficients is None:
            skipped["period_below_minimum_training_rows"] = (
                skipped.get("period_below_minimum_training_rows", 0) + 1
            )
            continue
        linear = sum(weight * feature for weight, feature in zip(coefficients, features, strict=True))
        blended.append((row, inverse_logit(linear)))

    return blended, fits, skipped


def build_market_blend_report(
    games: Sequence[GameResult],
    market_probabilities: Mapping[str, Decimal],
    *,
    config: MarketBlendConfig | None = None,
    source: str = "collected_settled_games",
    dataset_version: str | None = None,
    league: str | None = None,
    market_baseline_name: str = "devigged_closing_consensus",
) -> dict[str, Any]:
    """Grade the blend against the market price it started from."""
    settings = config or MarketBlendConfig()
    rows, rating_skipped = walk_forward(
        games, config=settings.elo, market_probabilities=market_probabilities
    )
    blended, fits, blend_skipped = walk_forward_blend(rows, config=settings)

    outcomes = [row.home_win for row, _ in blended]
    blend_probabilities = [Decimal(repr(probability)) for _, probability in blended]
    market = [row.market_probability for row, _ in blended]  # type: ignore[misc]
    elo = [row.elo_probability for row, _ in blended]

    comparisons = []
    if blended:
        comparisons.append(
            {
                "model": MODEL_NAME,
                "baseline": market_baseline_name,
                **paired_comparison(
                    model_probabilities=blend_probabilities,
                    baseline_probabilities=market,
                    outcomes=outcomes,
                ),
            }
        )
        comparisons.append(
            {
                "model": MODEL_NAME,
                "baseline": "elo_alone",
                **paired_comparison(
                    model_probabilities=blend_probabilities,
                    baseline_probabilities=elo,
                    outcomes=outcomes,
                ),
            }
        )

    market_comparison = comparisons[0] if comparisons else {}
    verdict = str(market_comparison.get("verdict") or "insufficient_sample")
    if verdict == "model_better":
        decision = "accepted"
        decision_reason = "blend_beats_the_market_price_with_an_interval_excluding_zero"
    elif verdict == "baseline_better":
        decision = "rejected"
        decision_reason = "the_market_price_alone_scored_better_than_the_blend"
    elif verdict == "inconclusive":
        decision = "inconclusive"
        decision_reason = "the_blend_interval_against_the_market_contains_zero"
    else:
        decision = "insufficient_evidence"
        decision_reason = f"no_interval_could_be_computed:{verdict}"

    weights = [fit.model_weight for fit in fits]
    market_weights = [fit.market_weight for fit in fits]
    return {
        "asset_class": "sports",
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": source,
        "dataset_version": dataset_version,
        "evidence_class": (
            "collected_evidence" if source == "collected_settled_games" else "reference_data"
        ),
        "performance_metric_eligible": False,
        "performance_metric_note": (
            "Research output over past games. Not a performance metric, not the "
            "platform's realized record, and never a settled result."
        ),
        "league": league,
        "configuration": settings.as_dict(),
        "games_available": len(games),
        "forecasts_skipped": {**rating_skipped, **blend_skipped},
        "evaluated_games": len(blended),
        "periods_fitted": len(fits),
        "fits": [fit.as_dict() for fit in fits],
        "coefficient_summary": {
            "market_weight_first": market_weights[0] if market_weights else None,
            "market_weight_last": market_weights[-1] if market_weights else None,
            "model_weight_first": weights[0] if weights else None,
            "model_weight_last": weights[-1] if weights else None,
            "model_weight_mean": (sum(weights) / len(weights)) if weights else None,
        },
        "metrics": {
            MODEL_NAME: _score_summary(blend_probabilities, outcomes),
            market_baseline_name: _score_summary(market, outcomes),
            "elo_alone": _score_summary(elo, outcomes),
        },
        "comparisons": comparisons,
        "decision": decision,
        "decision_reason": decision_reason,
        "market_baseline": market_baseline_name,
        "model_state": "track_only",
        "promotion": "blocked_by_policy",
        "disclaimer": (
            "Walk-forward research output. The blend is graded against the price it "
            "started from; it is not a validated production model, not an edge, and "
            "never a betting recommendation."
        ),
    }


def render_market_blend_report(report: Mapping[str, Any]) -> str:
    market_name = str(report.get("market_baseline") or "market")
    lines = [
        "Market blend (walk-forward logistic on the closing price)",
        f"  source: {report.get('source')} ({report.get('dataset_version')})",
        f"  games evaluated: {report.get('evaluated_games')} across {report.get('periods_fitted')} refits",
        f"  decision: {report.get('decision')} ({report.get('decision_reason')})",
        f"  model state: {report.get('model_state')} / promotion {report.get('promotion')}",
    ]
    skipped = report.get("forecasts_skipped") or {}
    if skipped:
        lines.append("  skipped: " + ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items())))
    metrics = report.get("metrics") or {}
    for name in (MODEL_NAME, market_name, "elo_alone"):
        entry = metrics.get(name) or {}
        if entry.get("sample_size"):
            lines.append(
                f"  {name}: brier={entry.get('brier_score')} log_loss={entry.get('log_loss')} "
                f"accuracy={entry.get('accuracy')} n={entry.get('sample_size')}"
            )
    for comparison in report.get("comparisons") or []:
        interval = comparison.get("confidence_interval")
        interval_text = "n/a" if not interval else f"[{interval[0]:.6f}, {interval[1]:.6f}]"
        lines.append(
            f"  vs {comparison.get('baseline')}: {comparison.get('verdict')} "
            f"mean={comparison.get('mean_difference')} ci={interval_text} n={comparison.get('sample_size')}"
        )
    summary = report.get("coefficient_summary") or {}
    if summary.get("model_weight_mean") is not None:
        lines.append(
            "  coefficients: market weight "
            f"{summary.get('market_weight_first'):.4f} -> {summary.get('market_weight_last'):.4f}, "
            f"model weight {summary.get('model_weight_first'):.4f} -> {summary.get('model_weight_last'):.4f} "
            f"(mean {summary.get('model_weight_mean'):.4f})"
        )
        lines.append(
            "  a model weight indistinguishable from zero means the rating adds nothing "
            "the price did not already contain."
        )
    lines.append(f"  {report.get('disclaimer')}")
    return "\n".join(lines)


def required_games_for_blend_effect(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """How many games an observed but inconclusive improvement would need.

    Only an `inconclusive` comparison has a sample requirement to state. A
    degenerate one carries a deviation that is floating-point residue rather than
    spread, and dividing an effect by it would answer "no games at all" — which
    is the same false certainty the comparison itself refused to report.
    """
    for comparison in report.get("comparisons") or []:
        if comparison.get("baseline") != report.get("market_baseline"):
            continue
        if comparison.get("verdict") != "inconclusive":
            return None
        mean = comparison.get("mean_difference")
        deviation = comparison.get("difference_std")
        if not mean or not deviation or mean <= 0:
            return None
        return required_sample_for_score_improvement(
            mean_difference=float(mean), difference_std=float(deviation)
        ).as_dict()
    return None
