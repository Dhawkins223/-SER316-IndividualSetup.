"""Rank multi-leg combos, taking the joint probability from a copula.

The model this replaced multiplied the leg probabilities and then subtracted a
flat 3%-per-repeated-context *penalty*. That is wrong in sign, not just in
magnitude. For a combo where every leg must hit, positive correlation raises the
joint probability, and there is a closed form that says so exactly: two standard
normals both below their medians co-occur with probability

    1/4 + arcsin(rho) / (2*pi)

which is 0.2500 at rho=0 and 0.3155 at rho=0.40 -- up 26%, where the old penalty
moved the same pair down to 0.2425. Both directions cannot be right, and the
closed form is not a matter of opinion.

Two structural points about what this ranker will and will not report:

Same-event combos are excluded from the ranking entirely. Correlation lifts
their joint probability, which is real, but the expected value beside it is
computed from *standalone* leg prices, and no book prices a same-game combo by
multiplying those. Ranking on that number would put the least achievable combos
at the top of the list. ``slip_analysis.analyze_slip`` will still report on such
a slip, with the achievability warning attached, which is where that belongs.

Once same-event combos are gone, the correlation model usually has nothing left
to work with: it relates legs by shared team or shared league-and-slate, and a
``TotalLeg`` carries those only if the caller supplied them. When every
off-diagonal correlation is zero the joint *is* the product, exactly, so no
simulation runs -- replacing an exact number with a noisy one is not an
improvement.
"""

from __future__ import annotations

from itertools import combinations
from math import prod

from ..contracts import ComboResult, TotalLeg
from ..slip_analysis import (
    DEFAULT_SEED,
    CorrelationModel,
    SlipLeg,
    correlation_matrix,
    independent_probability,
    simulate_correlation_adjustment,
)

# Chosen from the standard error on the *adjustment*, which is the quantity that
# decides the ranking, not from the hit count. On a two-leg slip at 0.93 and
# 0.91 with rho=0.25 the adjustment is +0.0067; at 4,000 draws its standard
# error is 0.0025, so the estimate crosses zero between seeds, and at 20,000 it
# is 0.0011, which resolves the sign every time. Only genuinely correlated
# candidates reach the simulator -- the rest are exact -- so this is affordable.
COMBO_DRAWS = 20_000

# How many survivors get the copula. Candidates are pre-ranked by the
# independent product, which is a lower bound on the joint whenever every
# modelled correlation is non-negative, so this refines the plausible top of the
# list rather than an arbitrary slice. Wider than max_results because
# refinement reorders.
CANDIDATE_MULTIPLE = 4
MIN_CANDIDATE_POOL = 40


def _slip_leg(leg: TotalLeg) -> SlipLeg | None:
    """Convert a leg for the copula, or refuse it.

    A Kalshi contract bought at ``c`` cents pays 100, so its decimal odds are
    ``100/c``. At 100 cents there is nothing to win and the odds are 1.0, which
    ``SlipLeg`` rejects -- correctly, since such a leg cannot contribute value.
    A model probability of exactly 0 or 1 is a certainty claim and is refused
    for the same reason: the copula maps probabilities through a normal
    quantile, and the quantile of a certainty is infinite.
    """

    if not 0.0 < leg.entry_price_cents < 100.0:
        return None
    if not 0.0 < leg.model_probability < 1.0:
        return None
    return SlipLeg(
        leg_id=leg.leg_id,
        selection=leg.selection,
        decimal_odds=100.0 / leg.entry_price_cents,
        fair_probability=leg.model_probability,
        # The same key the superseded model used as its "context", so the set of
        # combos treated as same-event is unchanged; only the handling is.
        event_id=f"{leg.sport}:{leg.event_name}",
        league=leg.league,
        market=leg.market_title,
        team=leg.team,
        slate=leg.slate,
    )


def _shares_an_event(legs: list[SlipLeg]) -> bool:
    seen: set[str] = set()
    for leg in legs:
        if leg.event_id in seen:
            return True
        seen.add(leg.event_id)
    return False


def _has_modelled_correlation(legs: list[SlipLeg], model: CorrelationModel) -> bool:
    matrix = correlation_matrix(legs, model)
    size = len(matrix)
    return any(matrix[i][j] != 0.0 for i in range(size) for j in range(i + 1, size))


def _joint_probability(
    legs: list[SlipLeg], model: CorrelationModel, *, draws: int, seed: int
) -> tuple[float, str]:
    """The joint hit probability, simulated only when there is anything to simulate."""

    if not _has_modelled_correlation(legs, model):
        return independent_probability(legs), "exact_product_no_modelled_correlation"
    simulation = simulate_correlation_adjustment(legs, model=model, draws=draws, seed=seed)
    # The basis records whether the adjustment's sign was actually established.
    # An unresolved adjustment still gives the best available point estimate,
    # but a combo ranked above another on an unresolved difference is ranked on
    # noise, and the note is what lets a reader see that.
    basis = "copula_resolved" if simulation["adjustment_resolved"] else "copula_unresolved"
    return simulation["hit_probability"], basis


def _synthetic_cost_cents(legs: list[TotalLeg]) -> float:
    """What the legs cost bought as one unit.

    A combo contract paying 100 is worth the product of its legs' prices when
    the legs are priced independently, so that product is the cost to beat. The
    superseded field averaged the leg prices instead, which compares a
    per-contract price against a joint probability and means nothing.
    """

    return 100.0 * prod(leg.entry_price_cents / 100.0 for leg in legs)


class ComboBot:
    def build_ranked_combos(
        self,
        legs: list[TotalLeg],
        target_probability: float = 0.80,
        min_legs: int = 2,
        max_legs: int = 5,
        max_results: int = 20,
        min_leg_probability: float = 0.75,
        *,
        model: CorrelationModel | None = None,
        draws: int = COMBO_DRAWS,
        seed: int = DEFAULT_SEED,
    ) -> list[ComboResult]:
        settings = model or CorrelationModel()
        eligible: list[tuple[TotalLeg, SlipLeg]] = []
        for leg in legs:
            if leg.model_probability < min_leg_probability:
                continue
            slip_leg = _slip_leg(leg)
            if slip_leg is not None:
                eligible.append((leg, slip_leg))

        candidates: list[tuple[float, tuple[tuple[TotalLeg, SlipLeg], ...]]] = []
        same_event_skipped = 0
        upper_bound = min(max_legs, len(eligible))
        for size in range(max(min_legs, 1), upper_bound + 1):
            for selected in combinations(eligible, size):
                slip_legs = [pair[1] for pair in selected]
                if _shares_an_event(slip_legs):
                    same_event_skipped += 1
                    continue
                # Every leg must hit, so the joint can never exceed the weakest
                # leg. Exact, and it prunes without touching the simulator.
                if min(leg.fair_probability for leg in slip_legs) < target_probability:
                    continue
                candidates.append((independent_probability(slip_legs), selected))

        candidates.sort(key=lambda item: item[0], reverse=True)
        pool_size = max(max_results * CANDIDATE_MULTIPLE, MIN_CANDIDATE_POOL)
        refined = candidates[:pool_size]
        ranking_is_complete = len(refined) == len(candidates)

        results: list[ComboResult] = []
        for raw_probability, selected in refined:
            total_legs = [pair[0] for pair in selected]
            slip_legs = [pair[1] for pair in selected]
            adjusted, joint_source = _joint_probability(
                slip_legs, settings, draws=draws, seed=seed
            )
            if adjusted < target_probability:
                continue
            cost = _synthetic_cost_cents(total_legs)
            fair_price = 100.0 * adjusted
            notes = [
                f"target={target_probability:.0%}",
                f"min_leg_probability={min_leg_probability:.0%}",
                f"joint={joint_source}",
                "ev_basis=fair_value_less_product_of_leg_prices",
            ]
            if same_event_skipped:
                notes.append(f"same_event_combos_excluded={same_event_skipped}")
            if not ranking_is_complete:
                # Said out loud because the caller is holding a list that looks
                # exhaustive and is not.
                notes.append(
                    f"ranking_refined_top={len(refined)}_of_{len(candidates)}_candidates"
                )
            results.append(
                ComboResult(
                    combo_id="+".join(leg.leg_id for leg in total_legs),
                    legs=total_legs,
                    raw_probability=raw_probability,
                    adjusted_probability=adjusted,
                    correlation_adjustment=adjusted - raw_probability,
                    synthetic_cost_cents=round(cost, 2),
                    fair_price_cents=round(fair_price, 2),
                    expected_value_cents=round(fair_price - cost, 2),
                    meets_target=True,
                    notes=notes,
                )
            )
        return sorted(
            results,
            key=lambda result: (result.expected_value_cents, result.adjusted_probability),
            reverse=True,
        )[:max_results]
