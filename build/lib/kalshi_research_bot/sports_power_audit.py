"""Whether the claims this project wants to make are answerable at all.

Backlog entry E-09, and the backlog puts it second in priority order for a
reason: it does not test a hypothesis, it tests whether the *rest of the
program* can be tested. Every other experiment grades a model against the
market and reports a confidence interval. This one asks what those intervals
could ever have excluded, given how much sport actually gets played.

The arithmetic is unforgiving and runs the wrong way from intuition. Required
sample scales with the inverse square of the effect, so an edge half as large
needs four times the evidence. Below some size an effect is not weak, it is
undetectable: no amount of restating the same season makes it visible, and a
result reported as "inconclusive, needs more data" may be describing a wait
measured in centuries.

Two conversions do the work here, and both are measured rather than assumed:

**Volume.** The independent unit is the game, not the quote. A moneyline market
resolves once; five books quoting it produce five prices and one outcome, and a
model that beat all five beat one game. Counting quotes as evidence inflates
every interval built on them, which is why the volume figures below are game
counts taken from the historical archive.

**Variance.** The spread of the paired per-game difference decides how much
evidence an effect needs, and it is a property of the sport rather than of the
model — NFL outcomes are noisy, so paired Brier differences against the closing
line scatter far more widely than the differences themselves. It is read off an
actual comparison, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kalshi_research_bot.evaluation.power import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    STANDARD_BREAK_EVEN,
    effective_sample_size,
    minimum_detectable_score_improvement,
    required_sample_for_edge,
    required_sample_for_score_improvement,
)

# Paired Brier improvements worth pricing out. The smallest is the scale of the
# blend result already recorded against the closing line; the largest is the
# scale of Elo over a base rate, an effect nobody is selling because the market
# already contains it.
DEFAULT_SCORE_EFFECTS: tuple[float, ...] = (0.0001, 0.0005, 0.001, 0.005, 0.01)

# Win rates above the break-even implied by a standard -110 price.
DEFAULT_WIN_RATE_EDGES: tuple[float, ...] = (0.01, 0.02, 0.03, 0.05)


@dataclass(frozen=True)
class LeagueVolume:
    """How many independent gradable observations a league yields per season.

    ``games_per_season`` is the count of games actually played, and
    ``gradable_fraction`` the share of them carrying the closing prices a
    market-relative comparison needs. Their product, not the raw schedule, is
    the evidence a season delivers.

    ``measured`` separates a figure counted from a dataset this project holds
    from one taken off a published schedule. Both are usable; only the first is
    evidence, and a report that blurs them invites exactly the overstatement
    this module exists to prevent.
    """

    league: str
    games_per_season: float
    gradable_fraction: float
    source: str
    measured: bool = False

    def gradable_games_per_season(self) -> float:
        return self.games_per_season * self.gradable_fraction

    def seasons_for(self, required_sample: float) -> float:
        per_season = self.gradable_games_per_season()
        if per_season <= 0:
            raise ValueError("league_yields_no_gradable_games")
        return required_sample / per_season

    def as_dict(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "games_per_season": self.games_per_season,
            "gradable_fraction": self.gradable_fraction,
            "gradable_games_per_season": self.gradable_games_per_season(),
            "source": self.source,
            "measured": self.measured,
        }


# Counted from `data/http_cache` nflverse `games.csv` over the five complete
# seasons 2021-2025: 1,424 games, every one of them carrying both closing
# moneylines. The other leagues are published schedule sizes, not counts from
# data this project holds, and are marked accordingly.
NFL_VOLUME = LeagueVolume(
    league="nfl",
    games_per_season=284.8,
    gradable_fraction=1.0,
    source="nflverse games.csv, complete seasons 2021-2025",
    measured=True,
)

PUBLISHED_SCHEDULES: tuple[LeagueVolume, ...] = (
    NFL_VOLUME,
    LeagueVolume("nba", 1230, 1.0, "published regular-season schedule size", measured=False),
    LeagueVolume("nhl", 1312, 1.0, "published regular-season schedule size", measured=False),
    LeagueVolume("mlb", 2430, 1.0, "published regular-season schedule size", measured=False),
)


def volume_for_league(
    league: str | None, leagues: Sequence[LeagueVolume] = PUBLISHED_SCHEDULES
) -> LeagueVolume | None:
    """The season volume for one league, or ``None`` when it is not known.

    Returning ``None`` rather than a default is the point. Pricing an NBA
    comparison against the NFL schedule would report the wrong number of seasons
    and label the record `league=nfl`, which is the measured-versus-assumed
    confusion this module exists to prevent — and it would do it silently.
    """

    if not league:
        return None
    wanted = str(league).strip().lower()
    for entry in leagues:
        if entry.league == wanted:
            return entry
    return None


def combined_volume(
    leagues: Sequence[LeagueVolume] = PUBLISHED_SCHEDULES, *, label: str = "all_leagues"
) -> LeagueVolume:
    """Pool several leagues into one season's worth of gradable games.

    Pooling buys volume and spends an assumption: that the effect being tested
    is the same size in every league pooled. It usually is not — a rating edge
    on NHL says little about NFL — so a pooled result answers "is there an edge
    somewhere in this basket" rather than "is there an edge in this league".
    Splitting the basket afterwards to find which league carried it is the
    multiple-testing failure ``benjamini_hochberg`` exists to correct.
    """

    if not leagues:
        raise ValueError("at_least_one_league_required")
    total = sum(entry.gradable_games_per_season() for entry in leagues)
    return LeagueVolume(
        league=label,
        games_per_season=total,
        gradable_fraction=1.0,
        source="pooled: " + ", ".join(entry.league for entry in leagues),
        measured=all(entry.measured for entry in leagues),
    )


def score_effect_costs(
    *,
    difference_std: float,
    volume: LeagueVolume,
    effects: Sequence[float] = DEFAULT_SCORE_EFFECTS,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> list[dict[str, Any]]:
    """What each candidate paired-score improvement would cost to demonstrate."""

    rows: list[dict[str, Any]] = []
    for effect in effects:
        required = required_sample_for_score_improvement(
            mean_difference=effect, difference_std=difference_std, alpha=alpha, power=power
        )
        rows.append(
            {
                "paired_brier_improvement": effect,
                "required_games": required.required_sample,
                "seasons_required": volume.seasons_for(required.required_sample),
            }
        )
    return rows


def win_rate_edge_costs(
    *,
    volume: LeagueVolume,
    edges: Sequence[float] = DEFAULT_WIN_RATE_EDGES,
    break_even_probability: float = STANDARD_BREAK_EVEN,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> list[dict[str, Any]]:
    """What each candidate win-rate edge would cost to demonstrate.

    One bet per game, because that is the independent unit. Betting the same
    game at several books multiplies the tickets and not the evidence.
    """

    rows: list[dict[str, Any]] = []
    for edge in edges:
        required = required_sample_for_edge(
            edge=edge,
            break_even_probability=break_even_probability,
            alpha=alpha,
            power=power,
        )
        rows.append(
            {
                "edge_above_break_even": edge,
                "required_bets": required.required_sample,
                "seasons_required": volume.seasons_for(required.required_sample),
            }
        )
    return rows


def quote_inflation(
    *, gradable_games: float, quotes_per_game: float, intraclass_correlation: float = 1.0
) -> dict[str, Any]:
    """What a quote count is worth once its correlation is accounted for.

    Prices on one game share that game's single outcome. At the limiting
    correlation of 1.0 the whole quote count collapses back to the game count,
    which is the honest reading: a market resolves once however many books
    quoted it.
    """

    if quotes_per_game < 1:
        raise ValueError("quotes_per_game_must_be_at_least_one")
    raw = gradable_games * quotes_per_game
    effective = effective_sample_size(
        sample_size=int(raw),
        cluster_size=quotes_per_game,
        intraclass_correlation=intraclass_correlation,
    )
    return {
        "raw_quote_count": raw,
        "quotes_per_game": quotes_per_game,
        "intraclass_correlation": intraclass_correlation,
        "effective_sample": effective,
        "inflation_factor": raw / effective if effective > 0 else None,
    }


def audit_detectability(
    comparison: Mapping[str, Any],
    *,
    volume: LeagueVolume,
    score_effects: Sequence[float] = DEFAULT_SCORE_EFFECTS,
    win_rate_edges: Sequence[float] = DEFAULT_WIN_RATE_EDGES,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> dict[str, Any]:
    """Grade one realized comparison for what it could ever have detected.

    ``comparison`` is the output of ``sports_ratings.paired_comparison``: the
    realized sample size and the measured spread of the paired difference come
    from it, so this reports on evidence that exists rather than on an assumed
    study design.
    """

    sample_size = comparison.get("sample_size")
    difference_std = comparison.get("difference_std")
    mean_difference = comparison.get("mean_difference")
    if not isinstance(sample_size, int) or sample_size < 1:
        return {
            "status": "no_realized_sample",
            "reason": "comparison_carries_no_sample_size",
            "metric": comparison.get("metric"),
        }
    if not isinstance(difference_std, (int, float)) or difference_std <= 0:
        # A degenerate or refused comparison has no spread to reason from, and
        # inventing one would be the exact failure this module exists to catch.
        return {
            "status": "no_measured_variance",
            "reason": "comparison_carries_no_usable_difference_std",
            "realized_sample": sample_size,
            "verdict_of_comparison": comparison.get("verdict"),
        }

    detectable = minimum_detectable_score_improvement(
        sample_size=sample_size, difference_std=float(difference_std), alpha=alpha, power=power
    )
    observed = float(mean_difference) if isinstance(mean_difference, (int, float)) else None
    return {
        "status": "audited",
        "metric": comparison.get("metric"),
        "verdict_of_comparison": comparison.get("verdict"),
        "realized_sample": sample_size,
        "difference_std": float(difference_std),
        "observed_mean_difference": observed,
        # Carried through so a verdict recorded from this audit can cite the
        # interval the comparison actually produced, rather than an interval
        # invented from a power calculation, which has none.
        "confidence_interval": comparison.get("confidence_interval"),
        "minimum_detectable_improvement": detectable,
        # The ratio that decides whether the result is a finding or a floor.
        "observed_over_detectable": (abs(observed) / detectable) if observed is not None else None,
        "realized_sample_seasons": volume.seasons_for(sample_size),
        "alpha": alpha,
        "power": power,
        "league_volume": volume.as_dict(),
        "score_effect_costs": score_effect_costs(
            difference_std=float(difference_std),
            volume=volume,
            effects=score_effects,
            alpha=alpha,
            power=power,
        ),
        "win_rate_edge_costs": win_rate_edge_costs(
            volume=volume, edges=win_rate_edges, alpha=alpha, power=power
        ),
    }


def record_power_audit_experiment(
    report: Mapping[str, Any],
    *,
    path: str | Any | None = None,
    recorded_at: Any | None = None,
) -> dict[str, Any] | None:
    """Record E-09's verdict, which is a statement about evidence, not a model.

    The decision is ``rejected`` when the effect the platform would want to sell
    sits below what its realized volume could ever detect. That is the honest
    reading: not "no edge exists" — this test cannot say that — but "an edge of
    the size observed is not demonstrable here", which is what a buyer of a
    performance claim is actually being asked to believe.
    """

    from .research_registry import ExperimentRecord, record_experiment

    if report.get("status") != "audited":
        return None

    detectable = float(report["minimum_detectable_improvement"])
    observed = report.get("observed_mean_difference")
    volume = report["league_volume"]
    # Priced from the observed effect itself. Reading it off the nearest row of
    # the effect grid would quote the wait for a larger effect than was seen,
    # which is the flattering direction and off by an order of magnitude.
    seasons_for_observed: float | None = None
    if observed is not None and abs(float(observed)) > 0:
        needed = required_sample_for_score_improvement(
            mean_difference=abs(float(observed)),
            difference_std=float(report["difference_std"]),
            alpha=float(report["alpha"]),
            power=float(report["power"]),
        )
        seasons_for_observed = LeagueVolume(
            league=volume["league"],
            games_per_season=volume["games_per_season"],
            gradable_fraction=volume["gradable_fraction"],
            source=volume["source"],
        ).seasons_for(needed.required_sample)

    interval = report.get("confidence_interval")
    # Demonstrable means two things at once, and the registry enforces the
    # second: the effect clears the detectable floor, and the comparison that
    # measured it produced an interval excluding zero.
    demonstrable = (
        observed is not None
        and abs(float(observed)) >= detectable
        and interval is not None
        and not (float(interval[0]) <= 0 <= float(interval[1]))
    )
    notes = [
        f"league={volume['league']}",
        f"volume_measured={volume['measured']}",
        f"gradable_games_per_season={volume['gradable_games_per_season']:.1f}",
        f"realized_sample_seasons={report['realized_sample_seasons']:.1f}",
        f"minimum_detectable_improvement={detectable:.6g}",
        f"observed_mean_difference={observed}",
        f"power={report['power']}",
    ]
    if seasons_for_observed is not None:
        notes.append(f"seasons_to_detect_observed_effect={seasons_for_observed:.0f}")

    return record_experiment(
        ExperimentRecord(
            hypothesis=(
                "Sample sizes required for the edges this platform would claim are "
                f"attainable at realized {volume['league']} volume"
            ),
            rationale=(
                "Required sample scales with the inverse square of the effect, so an "
                "effect below the detectable floor is not weak but invisible. Establishing "
                "that floor decides which claims the rest of the program can ever support."
            ),
            dataset_version=str(report.get("dataset_version") or volume["source"]),
            target="demonstrability_of_claimed_edge",
            baseline="realized_gradable_game_volume",
            test_method="minimum_detectable_paired_brier_improvement",
            decision="accepted" if demonstrable else "rejected",
            effect_size=detectable,
            effect_metric="minimum_detectable_paired_brier_improvement",
            confidence_interval=interval if demonstrable else None,
            sample_size=int(report["realized_sample"]),
            model_version=str(report.get("model_version") or "market_blend_v1"),
            notes="; ".join(notes),
            tags=("E-09", "power", "research_only"),
        ),
        path=path,
        recorded_at=recorded_at,
    )


def _seasons_text(seasons: float) -> str:
    if seasons < 1:
        return f"{seasons:.2f} seasons"
    if seasons < 100:
        return f"{seasons:.1f} seasons"
    return f"{seasons:,.0f} seasons"


def render_power_audit_report(report: Mapping[str, Any]) -> str:
    """Plain-text rendering, for the CLI and for pasting into a review."""

    status = report.get("status")
    if status != "audited":
        return f"Power audit unavailable: {status} ({report.get('reason')})"

    volume = report["league_volume"]
    lines = [
        "Detectability audit (E-09)",
        "",
        f"League                     {volume['league']}",
        f"Games per season           {volume['games_per_season']:,.0f}"
        f" ({volume['gradable_games_per_season']:,.0f} gradable)",
        f"Realized sample            {report['realized_sample']:,} games"
        f" ({_seasons_text(report['realized_sample_seasons'])})",
        f"Paired difference std      {report['difference_std']:.6f}",
        "",
        f"Smallest detectable gain   {report['minimum_detectable_improvement']:.6f} paired Brier"
        f" (alpha {report['alpha']}, power {report['power']})",
    ]
    observed = report.get("observed_mean_difference")
    if observed is not None:
        ratio = report.get("observed_over_detectable")
        lines.append(
            f"Observed difference        {observed:+.6f}"
            + (f"  ({ratio:.2f}x the detectable floor)" if ratio is not None else "")
        )
    lines += ["", "Cost of a paired Brier improvement:", ""]
    for row in report["score_effect_costs"]:
        lines.append(
            f"  {row['paired_brier_improvement']:.4f}"
            f"  ->  {row['required_games']:>10,} games"
            f"  ({_seasons_text(row['seasons_required'])})"
        )
    lines += ["", "Cost of a win-rate edge over a -110 price, one bet per game:", ""]
    for row in report["win_rate_edge_costs"]:
        lines.append(
            f"  {row['edge_above_break_even']:.0%}"
            f"  ->  {row['required_bets']:>10,} bets"
            f"  ({_seasons_text(row['seasons_required'])})"
        )
    return "\n".join(lines)
