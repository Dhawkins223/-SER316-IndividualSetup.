"""Historical NFL games with their closing lines, from a public archive.

The sample-size arithmetic in `docs/sports-prediction-research-program.md`
section J is the constraint that decides whether this research program can
answer anything. Live collection accumulates a few hundred graded games a year;
the paired tests the backlog specifies need thousands. Waiting for the collector
to reach that is a multi-year plan.

nflverse publishes every NFL game since 1999 -- final scores, closing moneylines,
spreads and totals, rest days, roof, surface, temperature, wind, and officials --
as a plain CSV in a public GitHub repository. That is roughly 7,300 completed
games, over 5,200 of them carrying both closing moneylines, available now and at
no cost.

Two boundaries keep this honest:

1. **It is not collected evidence.** These rows are a third party's historical
   record, not something this platform observed with a timestamp and a payload
   hash of its own. They never enter `app.sports_prediction_logs`, so they cannot
   reach the board, the freshness gates, or any live metric. They are loaded for
   research and identified by the content hash of the file they came from.

2. **A closing line from an archive is a reported close, not a verified one.**
   Backlog E-03 asks whether a stored close is genuinely the last pre-start
   price. For this archive that question is unanswered: the file carries no quote
   timestamps. Results graded against it inherit that limitation, which is why
   the dataset version records the file hash -- so a later answer can be applied
   to exactly the rows it was computed from.

Source: https://github.com/nflverse/nfldata (public, MIT). Attribution belongs in
anything published from it.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from ..math.devig import DEFAULT_DEVIG_METHOD, remove_margin
from ..sports_board import american_implied_probability
from ..sports_ratings import GameResult
from .http import HttpClient, non_live_response_reason


NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
DATASET_NAME = "nflverse_nfldata_games"
LEAGUE = "nfl"
SPORT = "football"

# The columns this module reads. A file missing any of them is rejected outright
# rather than parsed into rows with silently absent fields.
REQUIRED_COLUMNS = (
    "game_id",
    "season",
    "game_type",
    "gameday",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
)


@dataclass(frozen=True)
class HistoricalDataset:
    """Games, their reported closing probabilities, and where they came from."""

    games: list[GameResult]
    market_probabilities: dict[str, Decimal]
    content_hash: str
    source_url: str
    rejections: dict[str, int]
    row_count: int

    def dataset_version(self) -> str:
        return f"{DATASET_NAME}:{self.content_hash[:16]}:{len(self.games)}"

    def evidence(self) -> dict[str, Any]:
        return {
            "dataset": DATASET_NAME,
            "source_url": self.source_url,
            "content_hash": self.content_hash,
            "rows_in_file": self.row_count,
            "games_loaded": len(self.games),
            "games_with_closing_market": len(self.market_probabilities),
            "rejections": dict(self.rejections),
            "dataset_version": self.dataset_version(),
            "collected_evidence": False,
            "note": (
                "Third-party historical archive. Not observed by this platform, never "
                "written to the collection tables, and its closing lines carry no quote "
                "timestamps to verify against."
            ),
        }


def _decimal_or_none(value: Any) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _start_time(gameday: str, gametime: str) -> datetime | None:
    """Kickoff as UTC, or None when the date itself is unusable.

    `gametime` is a local wall clock without a zone and is empty for older
    seasons. Rather than invent a timezone, games are anchored to the start of
    the day and every game on the same day is treated as one slate: the
    walk-forward then forecasts them all before any of their results move the
    ratings, which is the conservative reading when the true order is unknown.
    """
    date_text = str(gameday or "").strip()
    if not date_text:
        return None
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None
    return datetime.combine(parsed.date(), time(0, 0), tzinfo=timezone.utc)


def _closing_home_probability(home_moneyline: Decimal | None, away_moneyline: Decimal | None) -> Decimal | None:
    """De-vig the reported closing pair into a home-win probability."""
    if home_moneyline is None or away_moneyline is None:
        return None
    home_implied = american_implied_probability(home_moneyline)
    away_implied = american_implied_probability(away_moneyline)
    if home_implied is None or away_implied is None:
        return None
    try:
        result = remove_margin([home_implied, away_implied], method=DEFAULT_DEVIG_METHOD)
    except (ValueError, ArithmeticError):
        return None
    if not result.converged:
        return None
    return result.probabilities[0]


def normalize_nflverse_games(
    content: str,
    *,
    source_url: str = NFLVERSE_GAMES_URL,
    seasons: Sequence[int] | None = None,
    regular_season_only: bool = False,
) -> HistoricalDataset:
    """Parse the archive into games and reported closing probabilities.

    Every row that cannot be turned into a finished game is counted under an
    explicit reason instead of being dropped quietly. A row with no usable
    closing pair still yields a game: it can train and grade a rating, it just
    has no market baseline to be compared against.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    reader = csv.DictReader(io.StringIO(content))
    field_names = set(reader.fieldnames or ())
    missing = [column for column in REQUIRED_COLUMNS if column not in field_names]
    if missing:
        raise ValueError(f"nflverse_games_missing_columns:{','.join(missing)}")

    wanted_seasons = {int(season) for season in seasons} if seasons else None
    games: list[GameResult] = []
    market: dict[str, Decimal] = {}
    rejections: dict[str, int] = {}
    seen: set[str] = set()
    row_count = 0

    def reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    for row in reader:
        row_count += 1
        event_id = str(row.get("game_id") or "").strip()
        if not event_id:
            reject("missing_game_id")
            continue
        if event_id in seen:
            reject("duplicate_game_id")
            continue
        season = _decimal_or_none(row.get("season"))
        if season is None:
            reject("missing_season")
            continue
        if wanted_seasons is not None and int(season) not in wanted_seasons:
            continue
        if regular_season_only and str(row.get("game_type") or "").strip().upper() != "REG":
            continue

        home_score = _decimal_or_none(row.get("home_score"))
        away_score = _decimal_or_none(row.get("away_score"))
        if home_score is None or away_score is None:
            # Scheduled but not yet played, which is the ordinary state of the
            # current season's remaining rows.
            reject("no_final_score")
            continue
        start_time = _start_time(str(row.get("gameday") or ""), str(row.get("gametime") or ""))
        if start_time is None:
            reject("unusable_start_date")
            continue
        home_team = str(row.get("home_team") or "").strip()
        away_team = str(row.get("away_team") or "").strip()
        if not home_team or not away_team or home_team == away_team:
            reject("unusable_teams")
            continue

        seen.add(event_id)
        games.append(
            GameResult(
                event_id=event_id,
                sport=SPORT,
                league=LEAGUE,
                start_time=start_time,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
            )
        )
        probability = _closing_home_probability(
            _decimal_or_none(row.get("home_moneyline")),
            _decimal_or_none(row.get("away_moneyline")),
        )
        if probability is None:
            reject("no_closing_moneyline_pair")
        else:
            market[event_id] = probability

    games.sort(key=lambda game: (game.start_time, game.event_id))
    return HistoricalDataset(
        games=games,
        market_probabilities=market,
        content_hash=f"sha256:{digest}",
        source_url=source_url,
        rejections=rejections,
        row_count=row_count,
    )


def load_nflverse_games(
    *,
    client: HttpClient | None = None,
    url: str = NFLVERSE_GAMES_URL,
    seasons: Sequence[int] | None = None,
    regular_season_only: bool = False,
    timeout_seconds: int = 60,
    content: str | None = None,
    require_live: bool = False,
) -> HistoricalDataset:
    """Fetch and parse the archive. `content` bypasses the network for tests.

    `require_live` refuses a cached or stale body, which a probe needs and an
    ordinary research load does not: re-reading the same archive from cache is
    exactly the behaviour that keeps a verdict attached to one file hash.
    """
    if content is None:
        http = client or HttpClient()
        response = http.get_text(url, timeout=timeout_seconds)
        status = int(getattr(response, "status", 200))
        if status != 200:
            raise RuntimeError(f"nflverse_games_unavailable:http_{status}")
        if require_live:
            not_live = non_live_response_reason(response)
            if not_live:
                raise RuntimeError(f"nflverse_games_not_live:{not_live}")
        content = str(response.text)
    return normalize_nflverse_games(
        content,
        source_url=url,
        seasons=seasons,
        regular_season_only=regular_season_only,
    )


def summarize_dataset(dataset: HistoricalDataset) -> dict[str, Any]:
    """Coverage by season, so a gap in the archive is visible before it is used."""
    by_season: dict[str, dict[str, int]] = {}
    for game in dataset.games:
        season = game.event_id.split("_", 1)[0]
        entry = by_season.setdefault(season, {"games": 0, "with_closing_market": 0})
        entry["games"] += 1
        if game.event_id in dataset.market_probabilities:
            entry["with_closing_market"] += 1
    return {
        **dataset.evidence(),
        "seasons": dict(sorted(by_season.items())),
    }

