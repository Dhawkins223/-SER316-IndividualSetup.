"""Statistical power and multiple-testing control.

This module exists to answer one question before any research result is
believed: *could this study have detected the effect it claims to have found,
and would it still look significant after accounting for every other hypothesis
the project tested?*

Two failure modes are being defended against, and both are ordinary rather than
exotic:

**Underpowered claims.** Detecting a small edge takes far more resolved
predictions than a season provides. The arithmetic is unforgiving: required
sample scales with the inverse square of the effect, so halving the edge you
hope to detect quadruples the evidence you need. A strategy evaluated on three
hundred bets cannot distinguish a 2% edge from zero, and reporting its win rate
as though it could is the most common error in applied betting research.

**Multiple testing.** A backlog of fifty experiments run at a 5% threshold
produces around two or three "discoveries" when nothing is real. Confidence
intervals do not protect against this — every one of those false positives will
have an interval excluding zero, which is exactly what makes them convincing.
Benjamini-Hochberg control is applied across the family of tests, not to each
test in isolation.

No dependency on scipy: the normal quantile function is implemented directly
in ``math.normal`` (Acklam's rational approximation, refined with a Halley
step against the stdlib error function), accurate to roughly machine
precision across the range these tests use.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erfc, sqrt
from typing import Any, Iterable, Sequence

# Re-exported: these lived here first and are imported from here across the
# project. They now live in ``math.normal`` so ``slip_analysis`` can use them
# without importing this package. Written in the redundant-alias form, which is
# how a deliberate re-export is spelled -- an __all__ here would claim these two
# are the module's API, and everything below is public too.
from ..math.normal import normal_cdf as normal_cdf
from ..math.normal import normal_quantile as normal_quantile

DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.80
# Break-even win probability for a standard -110 price.
STANDARD_BREAK_EVEN = 110.0 / 210.0


def two_sided_p_value(z_statistic: float) -> float:
    """Two-sided normal p-value for a z statistic."""

    return erfc(abs(z_statistic) / sqrt(2.0))


@dataclass(frozen=True)
class SampleSizeResult:
    """Required evidence for one hypothesis, with the assumptions stated."""

    required_sample: int
    effect: float
    baseline: float
    alpha: float
    power: float
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_sample": self.required_sample,
            "effect": self.effect,
            "baseline": self.baseline,
            "alpha": self.alpha,
            "power": self.power,
            "note": self.note,
        }


def required_sample_for_edge(
    *,
    edge: float,
    break_even_probability: float = STANDARD_BREAK_EVEN,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> SampleSizeResult:
    """Resolved bets needed to show a win rate above break-even.

    ``edge`` is in probability points above the break-even rate: 0.01 means a
    true win probability one point above the price's implied break-even. The
    inverse-square scaling is the whole story — a 1% edge needs roughly
    twenty-five times the evidence of a 5% edge.
    """

    if not 0.0 < edge < 1.0:
        raise ValueError("edge_must_be_between_zero_and_one")
    if not 0.0 < break_even_probability < 1.0:
        raise ValueError("break_even_probability_must_be_between_zero_and_one")
    alternative = break_even_probability + edge
    if not 0.0 < alternative < 1.0:
        raise ValueError("edge_implies_impossible_win_probability")
    z_alpha = normal_quantile(1.0 - alpha / 2.0)
    z_power = normal_quantile(power)
    null_spread = sqrt(break_even_probability * (1.0 - break_even_probability))
    alternative_spread = sqrt(alternative * (1.0 - alternative))
    numerator = (z_alpha * null_spread + z_power * alternative_spread) ** 2
    required = numerator / (edge**2)
    return SampleSizeResult(
        required_sample=int(-(-required // 1)),
        effect=edge,
        baseline=break_even_probability,
        alpha=alpha,
        power=power,
        note="one_proportion_against_break_even",
    )


def minimum_detectable_edge(
    *,
    sample_size: int,
    break_even_probability: float = STANDARD_BREAK_EVEN,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> float:
    """The smallest edge a given sample could detect.

    The inverse of the question above, and usually the more honest one to ask:
    given the predictions actually available, what is the smallest real effect
    this study could have found? An edge below this number is not absent, it is
    invisible.
    """

    if sample_size < 1:
        raise ValueError("sample_size_must_be_positive")
    z_alpha = normal_quantile(1.0 - alpha / 2.0)
    z_power = normal_quantile(power)
    spread = sqrt(break_even_probability * (1.0 - break_even_probability))
    # Solved with the null spread on both terms; the alternative spread differs
    # only in the third decimal for edges of this size, and using it would
    # require iterating for no practical gain.
    return (z_alpha + z_power) * spread / sqrt(sample_size)


def required_sample_for_score_improvement(
    *,
    mean_difference: float,
    difference_std: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> SampleSizeResult:
    """Predictions needed to show a paired proper-score improvement.

    This is the test that matters for a probability system: the paired
    per-prediction difference in Brier or log loss between model and baseline.
    ``difference_std`` is the standard deviation of that paired difference and
    must be estimated from data, not assumed.
    """

    if mean_difference <= 0:
        raise ValueError("mean_difference_must_be_positive")
    if difference_std <= 0:
        raise ValueError("difference_std_must_be_positive")
    z_alpha = normal_quantile(1.0 - alpha / 2.0)
    z_power = normal_quantile(power)
    required = ((z_alpha + z_power) * difference_std / mean_difference) ** 2
    return SampleSizeResult(
        required_sample=int(-(-required // 1)),
        effect=mean_difference,
        baseline=0.0,
        alpha=alpha,
        power=power,
        note="paired_difference_in_proper_score",
    )


def minimum_detectable_score_improvement(
    *,
    sample_size: int,
    difference_std: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> float:
    """The smallest paired proper-score improvement a given sample could detect.

    The counterpart of ``minimum_detectable_edge`` for the test that actually
    grades a probability system, and the question to ask of any result that came
    back inconclusive. ``required_sample_for_score_improvement`` cannot answer it:
    that function needs a positive observed effect, and an inconclusive result is
    frequently negative — the case where "how much evidence would this have
    needed?" is least meaningful and "what could this ever have seen?" is most.

    An improvement below the returned value is not absent from the data. It is
    invisible to it, and no amount of restating the same sample will change that.
    """

    if sample_size < 1:
        raise ValueError("sample_size_must_be_positive")
    if difference_std <= 0:
        raise ValueError("difference_std_must_be_positive")
    z_alpha = normal_quantile(1.0 - alpha / 2.0)
    z_power = normal_quantile(power)
    return (z_alpha + z_power) * difference_std / sqrt(sample_size)


def effective_sample_size(
    *, sample_size: int, cluster_size: float, intraclass_correlation: float
) -> float:
    """Discount a sample for correlated observations.

    Predictions from the same game or the same slate are not independent. Ten
    correlated legs are worth less than ten independent bets, and treating them
    as ten inflates every significance claim built on them.
    """

    if sample_size < 0:
        raise ValueError("sample_size_must_not_be_negative")
    if cluster_size < 1:
        raise ValueError("cluster_size_must_be_at_least_one")
    if not -1.0 <= intraclass_correlation <= 1.0:
        raise ValueError("intraclass_correlation_out_of_range")
    inflation = 1.0 + (cluster_size - 1.0) * intraclass_correlation
    if inflation <= 0:
        raise ValueError("design_effect_must_be_positive")
    return sample_size / inflation


@dataclass(frozen=True)
class CorrectedTest:
    """One hypothesis after family-wise correction."""

    label: str
    p_value: float
    adjusted_p_value: float
    significant: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "p_value": self.p_value,
            "adjusted_p_value": self.adjusted_p_value,
            "significant": self.significant,
        }


def benjamini_hochberg(
    p_values: Sequence[float],
    *,
    false_discovery_rate: float = DEFAULT_ALPHA,
    labels: Sequence[str] | None = None,
) -> list[CorrectedTest]:
    """Benjamini-Hochberg false-discovery-rate control.

    Controls the expected proportion of false positives among the hypotheses
    declared significant, which is the appropriate target for a research backlog
    where the goal is to prioritize follow-up rather than to make one
    irreversible decision. Results are returned in the input order.
    """

    if not 0.0 < false_discovery_rate < 1.0:
        raise ValueError("false_discovery_rate_must_be_between_zero_and_one")
    values = [float(value) for value in p_values]
    if any(value < 0.0 or value > 1.0 for value in values):
        raise ValueError("p_values_must_be_between_zero_and_one")
    count = len(values)
    if count == 0:
        return []
    names = list(labels) if labels is not None else [f"test_{index + 1}" for index in range(count)]
    if len(names) != count:
        raise ValueError("labels_must_align_with_p_values")

    ordered = sorted(range(count), key=lambda index: values[index])
    adjusted = [0.0] * count
    running_minimum = 1.0
    for rank in range(count - 1, -1, -1):
        index = ordered[rank]
        candidate = values[index] * count / (rank + 1)
        running_minimum = min(running_minimum, candidate)
        adjusted[index] = min(1.0, running_minimum)
    return [
        CorrectedTest(
            label=names[index],
            p_value=values[index],
            adjusted_p_value=adjusted[index],
            significant=adjusted[index] <= false_discovery_rate,
        )
        for index in range(count)
    ]


def bonferroni(
    p_values: Sequence[float],
    *,
    alpha: float = DEFAULT_ALPHA,
    labels: Sequence[str] | None = None,
) -> list[CorrectedTest]:
    """Bonferroni correction. Stricter than Benjamini-Hochberg and less powerful.

    Appropriate when a single false positive is costly enough to justify missing
    real effects — for example, before a model is allowed to influence a
    decision gate.
    """

    values = [float(value) for value in p_values]
    if any(value < 0.0 or value > 1.0 for value in values):
        raise ValueError("p_values_must_be_between_zero_and_one")
    count = len(values)
    if count == 0:
        return []
    names = list(labels) if labels is not None else [f"test_{index + 1}" for index in range(count)]
    if len(names) != count:
        raise ValueError("labels_must_align_with_p_values")
    return [
        CorrectedTest(
            label=names[index],
            p_value=values[index],
            adjusted_p_value=min(1.0, values[index] * count),
            significant=min(1.0, values[index] * count) <= alpha,
        )
        for index in range(count)
    ]


def p_value_from_interval(
    effect: float, interval: Sequence[float], *, confidence: float = 0.95
) -> float | None:
    """Approximate a two-sided p-value from a reported confidence interval.

    A screening device, not a substitute for the original test. It assumes the
    interval is symmetric and normal-based, which is reasonable for the mean
    paired-score differences this project records and wrong for skewed or
    bootstrap-percentile intervals. Returns ``None`` when the interval carries no
    width to work with.
    """

    if interval is None or len(interval) != 2:
        return None
    lower, upper = float(interval[0]), float(interval[1])
    if upper < lower:
        return None
    half_width = (upper - lower) / 2.0
    if half_width <= 0:
        return None
    critical = normal_quantile(1.0 - (1.0 - confidence) / 2.0)
    standard_error = half_width / critical
    if standard_error <= 0:
        return None
    return two_sided_p_value(float(effect) / standard_error)


def review_experiment_significance(
    experiments: Iterable[dict[str, Any]],
    *,
    false_discovery_rate: float = DEFAULT_ALPHA,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Apply family-wise correction across recorded experiments.

    Reads registry entries, derives an approximate p-value from each recorded
    effect and interval, and applies Benjamini-Hochberg across the whole family.
    The output that matters is ``demoted``: accepted findings that stop being
    significant once the rest of the backlog is taken into account. Those are not
    necessarily wrong, but they can no longer be cited as discoveries without
    being re-run.

    Entries without an effect and interval are reported as ``unscored`` rather
    than dropped, so the family size is never quietly understated.
    """

    entries = list(experiments)
    scored: list[tuple[dict[str, Any], float]] = []
    unscored: list[dict[str, Any]] = []
    for entry in entries:
        effect = entry.get("effect_size")
        interval = entry.get("confidence_interval")
        p_value = None
        if effect is not None and interval:
            p_value = p_value_from_interval(float(effect), interval, confidence=confidence)
        if p_value is None:
            unscored.append(entry)
        else:
            scored.append((entry, p_value))

    corrected = benjamini_hochberg(
        [p_value for _, p_value in scored],
        false_discovery_rate=false_discovery_rate,
        labels=[str(entry.get("hypothesis_key") or entry.get("hypothesis") or "unlabelled") for entry, _ in scored],
    )

    results = []
    demoted = []
    for (entry, _), test in zip(scored, corrected):
        row = test.as_dict()
        row["decision"] = entry.get("decision")
        row["hypothesis"] = entry.get("hypothesis")
        results.append(row)
        if entry.get("decision") == "accepted" and not test.significant:
            demoted.append(row)

    return {
        "family_size": len(entries),
        "scored_count": len(scored),
        "unscored_count": len(unscored),
        "false_discovery_rate": false_discovery_rate,
        "results": results,
        "demoted": demoted,
        "expected_false_positives_uncorrected": len(scored) * false_discovery_rate,
    }
