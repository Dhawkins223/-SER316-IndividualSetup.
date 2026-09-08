"""Free official league feeds: schedules and finished games, no key required.

The rating program can only grade leagues it has finished games for, and until
now that meant whatever the sports collector happened to have scraped. MLB and
the NHL each publish a complete schedule-and-results feed with no key, no
account, and no quota worth worrying about:

- MLB: `statsapi.mlb.com/api/v1/schedule` — every game, its status, and its
  linescore totals, back through the modern era.
- NHL: `api-web.nhle.com/v1/score/{date}` and the club schedule endpoints — every
  game with its final score.

Both are the leagues' own systems rather than a scraper's reading of a web page,
so they carry a status field that distinguishes final from postponed from
in-progress. That distinction is the whole reason to prefer them: a scoreboard
scrape that cannot tell a suspended game from a finished one silently converts
an unresolved event into a result.

Neither feed carries odds. They extend which leagues the rating can be *trained
and graded* on; the market baseline still has to come from a price source.

**Written against published documentation, not a live response**, for the same
reason as the Polymarket connector: the environment this was developed in blocks
both hosts. `source-probe mlb` and `source-probe nhl` make one request each, run
the real normalizer, and report which fields were present. Run them once from a
networked machine before trusting the mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ..sports_ratings import GameResult
from .http import HttpClient, live_probe_client, non_live_response_reason


MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
NHL_SCORE_URL = "https://api-web.nhle.com/v1/score"

# Only these mean "this game finished and its score is the result". Anything
# else -- postponed, suspended, in progress, cancelled -- is not a result.
MLB_FINAL_CODES = {"F", "O"}
NHL_FINAL_STATES = {"FINAL", "OFF"}


@dataclass
class LeagueFeedNormalization:
    """Finished games from one feed, plus what was refused and why."""

    league: str
    games: list[GameResult] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    api_fetched_at: str = ""
    source_url: str = ""

    def reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def evidence(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "source_url": self.source_url,
            "api_fetched_at": self.api_fetched_at,
            "finished_games": len(self.games),
            "rejection_reasons": dict(self.rejections),
        }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_mlb_schedule(
    payload: Any,
    *,
    api_fetched_at: str = "",
    source_url: str = MLB_SCHEDULE_URL,
) -> LeagueFeedNormalization:
    """Finished MLB games from a StatsAPI schedule response.

    A game counts only when its `codedGameState` says it finished and both teams
    carry a score. Postponed and suspended games are refused by name rather than
    read as a nil-nil result.
    """
    result = LeagueFeedNormalization(league="mlb", api_fetched_at=api_fetched_at, source_url=source_url)
    dates = payload.get("dates") if isinstance(payload, Mapping) else None
    if not isinstance(dates, list):
        result.reject("no_dates_in_payload")
        return result

    for date_entry in dates:
        games = date_entry.get("games") if isinstance(date_entry, Mapping) else None
        if not isinstance(games, list):
            continue
        for game in games:
            if not isinstance(game, Mapping):
                continue
            event_id = str(game.get("gamePk") or "").strip()
            if not event_id:
                result.reject("missing_game_id")
                continue
            status = game.get("status") if isinstance(game.get("status"), Mapping) else {}
            coded = str(status.get("codedGameState") or status.get("statusCode") or "").strip().upper()
            if coded not in MLB_FINAL_CODES:
                result.reject(f"not_final:{coded or 'unknown'}")
                continue
            teams = game.get("teams") if isinstance(game.get("teams"), Mapping) else {}
            home = teams.get("home") if isinstance(teams.get("home"), Mapping) else {}
            away = teams.get("away") if isinstance(teams.get("away"), Mapping) else {}
            home_name = str((home.get("team") or {}).get("name") or "").strip()
            away_name = str((away.get("team") or {}).get("name") or "").strip()
            home_score = _decimal_or_none(home.get("score"))
            away_score = _decimal_or_none(away.get("score"))
            start_time = _utc(game.get("gameDate"))
            if not home_name or not away_name:
                result.reject("missing_team_name")
                continue
            if home_score is None or away_score is None:
                result.reject("missing_score")
                continue
            if start_time is None:
                result.reject("missing_start_time")
                continue
            result.games.append(
                GameResult(
                    event_id=f"mlb_{event_id}",
                    sport="baseball",
                    league="mlb",
                    start_time=start_time,
                    home_team=home_name,
                    away_team=away_name,
                    home_score=home_score,
                    away_score=away_score,
                )
            )

    result.games.sort(key=lambda game: (game.start_time, game.event_id))
    return result


def normalize_nhl_scores(
    payload: Any,
    *,
    api_fetched_at: str = "",
    source_url: str = NHL_SCORE_URL,
) -> LeagueFeedNormalization:
    """Finished NHL games from an api-web score response."""
    result = LeagueFeedNormalization(league="nhl", api_fetched_at=api_fetched_at, source_url=source_url)
    games = payload.get("games") if isinstance(payload, Mapping) else None
    if not isinstance(games, list):
        result.reject("no_games_in_payload")
        return result

    for game in games:
        if not isinstance(game, Mapping):
            continue
        event_id = str(game.get("id") or "").strip()
        if not event_id:
            result.reject("missing_game_id")
            continue
        state = str(game.get("gameState") or "").strip().upper()
        if state not in NHL_FINAL_STATES:
            result.reject(f"not_final:{state or 'unknown'}")
            continue
        home = game.get("homeTeam") if isinstance(game.get("homeTeam"), Mapping) else {}
        away = game.get("awayTeam") if isinstance(game.get("awayTeam"), Mapping) else {}
        home_name = _nhl_team_name(home)
        away_name = _nhl_team_name(away)
        home_score = _decimal_or_none(home.get("score"))
        away_score = _decimal_or_none(away.get("score"))
        start_time = _utc(game.get("startTimeUTC"))
        if not home_name or not away_name:
            result.reject("missing_team_name")
            continue
        if home_score is None or away_score is None:
            result.reject("missing_score")
            continue
        if start_time is None:
            result.reject("missing_start_time")
            continue
        result.games.append(
            GameResult(
                event_id=f"nhl_{event_id}",
                sport="hockey",
                league="nhl",
                start_time=start_time,
                home_team=home_name,
                away_team=away_name,
                home_score=home_score,
                away_score=away_score,
            )
        )

    result.games.sort(key=lambda game: (game.start_time, game.event_id))
    return result


def _nhl_team_name(team: Mapping[str, Any]) -> str:
    """api-web nests display names under a language map on some endpoints."""
    for key in ("name", "placeName", "commonName"):
        value = team.get(key)
        if isinstance(value, Mapping):
            text = str(value.get("default") or "").strip()
            if text:
                return text
        elif value:
            text = str(value).strip()
            if text:
                return text
    return str(team.get("abbrev") or "").strip()


def probe_league_feed(
    league: str,
    *,
    client: HttpClient | None = None,
    date: str | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Fetch one day from a league feed and report what the normalizer made of it."""
    http = client or live_probe_client()
    if league == "mlb":
        day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = f"{MLB_SCHEDULE_URL}?sportId=1&date={day}"
        normalizer = normalize_mlb_schedule
    elif league == "nhl":
        day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = f"{NHL_SCORE_URL}/{day}"
        normalizer = normalize_nhl_scores
    else:
        return {"league": league, "reachable": False, "error": "unsupported_league"}

    try:
        response = http.get_text(url, timeout=timeout_seconds)
        status = int(getattr(response, "status", 200))
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - the probe reports failures rather than raising
        return {
            "league": league,
            "reachable": False,
            "source_url": url,
            "error": type(exc).__name__,
            "error_detail": str(exc)[:200],
        }

    not_live = non_live_response_reason(response)
    if not_live:
        # A cached or stale body records the last time this worked, not that it
        # works now, and a readiness check must not accept it.
        return {
            "league": league,
            "reachable": False,
            "http_status": status,
            "source_url": url,
            "error": "response_not_live",
            "error_detail": not_live,
        }

    normalization = normalizer(
        payload, api_fetched_at=str(getattr(response, "fetched_at", "")), source_url=url
    )
    sample = normalization.games[0] if normalization.games else None
    return {
        "league": league,
        "reachable": True,
        "http_status": status,
        "source_url": url,
        **normalization.evidence(),
        "sample_game": None
        if sample is None
        else {
            "event_id": sample.event_id,
            "start_time": sample.start_time.isoformat(),
            "home_team": sample.home_team,
            "away_team": sample.away_team,
            "home_score": format(sample.home_score, "f"),
            "away_score": format(sample.away_score, "f"),
        },
    }


def render_league_probe(report: Mapping[str, Any]) -> str:
    league = str(report.get("league") or "?").upper()
    if not report.get("reachable"):
        detail = report.get("error_detail") or report.get("error")
        return f"{league} feed probe: unreachable ({detail}).\n  url: {report.get('source_url')}"
    lines = [
        f"{league} feed probe",
        f"  url: {report.get('source_url')}",
        f"  finished games: {report.get('finished_games')}",
    ]
    rejections = report.get("rejection_reasons") or {}
    if rejections:
        lines.append("  refused: " + ", ".join(f"{reason}={count}" for reason, count in sorted(rejections.items())))
    sample = report.get("sample_game")
    if sample:
        lines.append(
            f"  sample: {sample['away_team']} {sample['away_score']} @ "
            f"{sample['home_team']} {sample['home_score']} ({sample['start_time']})"
        )
    else:
        lines.append("  no finished games in this response; try a date with a completed slate")
    return "\n".join(lines)
