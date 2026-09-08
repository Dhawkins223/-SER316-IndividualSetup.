from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from urllib.parse import urlencode
from decimal import Decimal

from .business_store import create_store
from .database import DatabaseRow, DatabaseSession, as_decimal, json_default
from .config import repo_path
from .collection_ledger import CollectionLedger, content_hash
from .connectors.firecrawl import is_firecrawl_configured
from .connectors.http import HttpClient
from .connectors.lifecycle import apply_post_report_connectors
from .connectors.source_adapters import (
    FirecrawlJsonSourceAdapter,
    HttpJsonSourceAdapter,
    SourceRequest,
    collect_from_plan,
    configured_retrieval_plan,
)
from .connectors.status import build_connectors_status, connector_status_report_lines
from .sports_clv import capture_sports_closing_lines
from .worker_runtime import current_worker_idempotency_key
from .private_research import (
    accuracy_status,
    deterministic_hash,
    gate_result,
    parse_aware_timestamp,
    read_json,
    sample_status,
    stable_json,
    utc_now_iso,
    write_csv,
    write_json,
    write_text,
)


SPORTS_MODEL_VERSION = "sports_odds_research_v1"
SPORTS_STRATEGY = "pregame_odds_snapshot_v1"
SPORTS_STALE_SECONDS = 60 * 60
DEFAULT_SPORT_KEY = "baseball_mlb"
SPORTS_SOURCE_MODE = "scraper"
SPORTS_PARSER_VERSION = "sports_source_v2"
ESPN_SPORT_MAP = {
    "baseball_mlb": ("baseball", "mlb"),
    "basketball_nba": ("basketball", "nba"),
    "americanfootball_nfl": ("football", "nfl"),
}


def default_sports_daily_report_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_daily_report.txt")


def default_sports_all_report_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_all_report.txt")


def default_sports_payload_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_odds.json")


def default_sports_features_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_features.csv")


def default_sports_labels_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_labels.csv")


def default_sports_validation_ledger_path(run_id: str) -> Path:
    return repo_path("data", "sports_runs", f"{run_id}_validation_ledger.jsonl")


@contextmanager
def _connect():
    with create_store().connect() as connection:
        yield connection


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    parsed = json.loads(str(value), parse_float=Decimal)
    return parsed if isinstance(parsed, dict) else {}


def american_odds_implied_probability(odds: Decimal | int | float | str) -> Decimal:
    odds = Decimal(str(odds))
    if odds < 0:
        return abs(odds) / (abs(odds) + Decimal("100"))
    return Decimal("100") / (odds + Decimal("100"))


def normalize_team_name(name: str | None) -> str:
    value = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    aliases = {
        "laangels": "losangelesangels",
        "losangelesangels": "losangelesangels",
        "ladodgers": "losangelesdodgers",
        "losangelesdodgers": "losangelesdodgers",
        "whitesox": "chicagowhitesox",
        "chisox": "chicagowhitesox",
        "redsox": "bostonredsox",
    }
    return aliases.get(value, value)


def _sports_league_from_key(sport_key: str) -> tuple[str, str]:
    return ESPN_SPORT_MAP.get(sport_key, ESPN_SPORT_MAP[DEFAULT_SPORT_KEY])


def _today_yyyymmdd() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _bounded_config_int(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        configured = int(values.get(name, str(default)))
    except (TypeError, ValueError):
        configured = default
    return max(minimum, min(maximum, configured))


def _prior_yyyymmdd(days_back: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y%m%d")


def _collect_prior_day_finals(
    *,
    sport: str,
    league: str,
    lookback_days: int,
    client: HttpClient,
    timeout_seconds: int,
    source_urls: list[str],
    retrieval_attempts: list[dict[str, Any]],
    raw_evidence: list[dict[str, Any]],
    adapters: dict[str, Any],
    plan: list[str],
) -> list[dict[str, Any]]:
    """Final scores from the days before today.

    A scoreboard request only returns the games of the date asked for, so finals
    are visible on exactly the UTC day a game completes. Anything that finished
    near the day boundary -- or during any cycle the worker missed -- was never
    seen again and stayed unresolved forever, which is why pending settlements
    only ever grew. Re-reading the previous days closes that window.

    Failures here are deliberately quiet: this is settlement catch-up, and a
    missing prior day must never block the current slate's collection.
    """
    finals: list[dict[str, Any]] = []
    for days_back in range(1, max(0, lookback_days) + 1):
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}"
            f"/scoreboard?dates={_prior_yyyymmdd(days_back)}"
        )
        source_urls.append(url)
        try:
            result, attempts, evidence = collect_from_plan(
                SourceRequest(
                    resource=url,
                    parser_version=SPORTS_PARSER_VERSION,
                    freshness_seconds=SPORTS_STALE_SECONDS,
                    timeout_seconds=timeout_seconds,
                ),
                plan=plan,
                adapters=adapters,
            )
            retrieval_attempts.extend(attempts)
            raw_evidence.extend(evidence)
            if result is None:
                continue
            normalized = normalize_espn_scoreboard_payload(
                result.raw_result,
                sport=sport,
                league=league,
                api_fetched_at=result.received_time,
            )
        except Exception:  # noqa: BLE001 - catch-up must never block the current slate
            continue
        finals.extend(normalized["finals"])
        _set_evidence_record_count(raw_evidence, result.raw_evidence_reference, len(normalized["finals"]))
    return finals


def _espn_event_teams(competition: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    home = None
    away = None
    for competitor in competition.get("competitors") or []:
        if competitor.get("homeAway") == "home":
            home = competitor
        elif competitor.get("homeAway") == "away":
            away = competitor
    return home, away


def _team_display_name(competitor: dict[str, Any] | None) -> str | None:
    if not competitor:
        return None
    team = competitor.get("team") or {}
    return team.get("displayName") or team.get("shortDisplayName") or competitor.get("displayName")


def normalize_espn_scoreboard_payload(
    payload: dict[str, Any],
    *,
    sport: str,
    league: str,
    api_fetched_at: str,
) -> dict[str, list[dict[str, Any]]]:
    schedule_rows: list[dict[str, Any]] = []
    final_events: list[dict[str, Any]] = []
    odds_rows: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        event_id = str(event.get("id") or event.get("uid") or "")
        competition = (event.get("competitions") or [{}])[0]
        home, away = _espn_event_teams(competition)
        home_team = _team_display_name(home)
        away_team = _team_display_name(away)
        game_start_time = competition.get("date") or event.get("date")
        status_type = ((competition.get("status") or {}).get("type") or {})
        game_status = str(status_type.get("name") or status_type.get("description") or "").lower()
        completed = bool(status_type.get("completed"))
        if event_id and home_team and away_team and game_start_time:
            schedule_row = {
                "asset_class": "sports",
                "source": "espn_scoreboard",
                "sport": sport,
                "league": league,
                "event_id": event_id,
                "game_id": event_id,
                "home_team": home_team,
                "away_team": away_team,
                "game_start_time": game_start_time,
                "game_status": game_status,
                "api_fetched_at": api_fetched_at,
                "source_payload_ref": f"espn_scoreboard:{league}:{event_id}",
            }
            schedule_row["source_snapshot_hash"] = deterministic_hash(schedule_row)
            schedule_rows.append(schedule_row)
            if completed or game_status in {"final", "status_final", "postponed", "canceled", "cancelled"}:
                final_events.append(
                    {
                        "event_id": event_id,
                        "game_id": event_id,
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_score": None if home is None else home.get("score"),
                        "away_score": None if away is None else away.get("score"),
                        "status": "final" if completed else game_status,
                        "api_fetched_at": api_fetched_at,
                        "source_snapshot_hash": deterministic_hash(
                            {
                                "source": "espn_scoreboard_final",
                                "event_id": event_id,
                                "home_score": None if home is None else home.get("score"),
                                "away_score": None if away is None else away.get("score"),
                                "status": "final" if completed else game_status,
                            }
                        ),
                    }
                )
            odds_rows.extend(
                normalize_espn_odds_from_event(
                    event,
                    schedule_row=schedule_row,
                    api_fetched_at=api_fetched_at,
                )
            )
    return {"schedule": schedule_rows, "finals": final_events, "odds": odds_rows}


def _coerce_american(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        text = str(value).strip().replace("+", "")
        try:
            return Decimal(text)
        except (ArithmeticError, ValueError):
            return None


def normalize_espn_odds_from_event(
    event: dict[str, Any],
    *,
    schedule_row: dict[str, Any],
    api_fetched_at: str,
) -> list[dict[str, Any]]:
    competition = (event.get("competitions") or [{}])[0]
    odds_entries = competition.get("odds") or event.get("odds") or []
    rows: list[dict[str, Any]] = []
    for odds_index, odds in enumerate(odds_entries):
        provider = odds.get("provider") or {}
        bookmaker = provider.get("name") or odds.get("providerName") or "espn_public"
        odds_timestamp = odds.get("lastUpdated") or odds.get("date") or api_fetched_at
        home_odds = odds.get("homeTeamOdds") or {}
        away_odds = odds.get("awayTeamOdds") or {}
        market_rows: list[dict[str, Any]] = []
        home_ml = _coerce_american(home_odds.get("moneyLine"))
        away_ml = _coerce_american(away_odds.get("moneyLine"))
        if home_ml is not None:
            market_rows.append({"market_type": "moneyline", "selection": schedule_row["home_team"], "line": None, "odds": home_ml})
        if away_ml is not None:
            market_rows.append({"market_type": "moneyline", "selection": schedule_row["away_team"], "line": None, "odds": away_ml})
        home_spread = home_odds.get("spread")
        away_spread = away_odds.get("spread")
        home_spread_odds = _coerce_american(home_odds.get("spreadOdds"))
        away_spread_odds = _coerce_american(away_odds.get("spreadOdds"))
        if home_spread is not None and home_spread_odds is not None:
            market_rows.append({"market_type": "spread", "selection": schedule_row["home_team"], "line": Decimal(str(home_spread)), "odds": home_spread_odds})
        if away_spread is not None and away_spread_odds is not None:
            market_rows.append({"market_type": "spread", "selection": schedule_row["away_team"], "line": Decimal(str(away_spread)), "odds": away_spread_odds})
        total_line = odds.get("overUnder")
        over_odds = _coerce_american(odds.get("overOdds"))
        under_odds = _coerce_american(odds.get("underOdds"))
        if total_line is not None and over_odds is not None:
            market_rows.append({"market_type": "total", "selection": "Over", "line": Decimal(str(total_line)), "odds": over_odds})
        if total_line is not None and under_odds is not None:
            market_rows.append({"market_type": "total", "selection": "Under", "line": Decimal(str(total_line)), "odds": under_odds})
        for market_row in market_rows:
            row = {
                **schedule_row,
                "bookmaker": str(bookmaker),
                "market_type": market_row["market_type"],
                "selection": market_row["selection"],
                "line": market_row["line"],
                "odds": market_row["odds"],
                "odds_format": "american",
                "odds_timestamp": odds_timestamp,
                "market_close_time": schedule_row["game_start_time"],
                "match_confidence": 1.0,
                "source": "espn_public_odds",
                "source_payload_ref": f"espn_public_odds:{schedule_row['event_id']}:{odds_index}:{market_row['market_type']}:{market_row['selection']}:{market_row['line']}",
            }
            row["source_snapshot_hash"] = deterministic_hash(row)
            rows.append(row)
    return rows


def _coerce_line(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    text = str(value).strip().lower().replace("o", "").replace("u", "")
    try:
        return Decimal(text)
    except (ArithmeticError, ValueError):
        return None


def normalize_espn_summary_odds(
    payload: dict[str, Any],
    *,
    schedule_row: dict[str, Any],
    api_fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pickcenter_entries = payload.get("pickcenter") or payload.get("odds") or []
    if isinstance(pickcenter_entries, dict):
        pickcenter_entries = [pickcenter_entries]
    for odds_index, odds in enumerate(pickcenter_entries):
        provider = odds.get("provider") or {}
        bookmaker = provider.get("name") or odds.get("providerName") or "espn_public"
        home_odds = odds.get("homeTeamOdds") or {}
        away_odds = odds.get("awayTeamOdds") or {}
        moneyline = odds.get("moneyline") or {}
        point_spread = odds.get("pointSpread") or {}
        total = odds.get("total") or {}
        market_rows: list[dict[str, Any]] = []
        home_ml = _coerce_american(home_odds.get("moneyLine") or ((moneyline.get("home") or {}).get("close") or {}).get("odds"))
        away_ml = _coerce_american(away_odds.get("moneyLine") or ((moneyline.get("away") or {}).get("close") or {}).get("odds"))
        if home_ml is not None:
            market_rows.append({"market_type": "moneyline", "selection": schedule_row["home_team"], "line": None, "odds": home_ml})
        if away_ml is not None:
            market_rows.append({"market_type": "moneyline", "selection": schedule_row["away_team"], "line": None, "odds": away_ml})
        home_spread_close = ((point_spread.get("home") or {}).get("close") or {})
        away_spread_close = ((point_spread.get("away") or {}).get("close") or {})
        home_spread = _coerce_line(home_spread_close.get("line") or odds.get("spread"))
        away_spread = _coerce_line(away_spread_close.get("line"))
        home_spread_odds = _coerce_american(home_spread_close.get("odds"))
        away_spread_odds = _coerce_american(away_spread_close.get("odds"))
        if home_spread is not None and home_spread_odds is not None:
            market_rows.append({"market_type": "spread", "selection": schedule_row["home_team"], "line": home_spread, "odds": home_spread_odds})
        if away_spread is not None and away_spread_odds is not None:
            market_rows.append({"market_type": "spread", "selection": schedule_row["away_team"], "line": away_spread, "odds": away_spread_odds})
        total_line = _coerce_line(odds.get("overUnder"))
        over_close = ((total.get("over") or {}).get("close") or {})
        under_close = ((total.get("under") or {}).get("close") or {})
        over_line = _coerce_line(over_close.get("line")) or total_line
        under_line = _coerce_line(under_close.get("line")) or total_line
        over_odds = _coerce_american(odds.get("overOdds") or over_close.get("odds"))
        under_odds = _coerce_american(odds.get("underOdds") or under_close.get("odds"))
        if over_line is not None and over_odds is not None:
            market_rows.append({"market_type": "total", "selection": "Over", "line": over_line, "odds": over_odds})
        if under_line is not None and under_odds is not None:
            market_rows.append({"market_type": "total", "selection": "Under", "line": under_line, "odds": under_odds})
        for market_row in market_rows:
            row = {
                **schedule_row,
                "bookmaker": str(bookmaker),
                "market_type": market_row["market_type"],
                "selection": market_row["selection"],
                "line": market_row["line"],
                "odds": market_row["odds"],
                "odds_format": "american",
                "odds_timestamp": odds.get("lastUpdated") or odds.get("date") or api_fetched_at,
                "market_close_time": schedule_row["game_start_time"],
                "match_confidence": 1.0,
                "source": "espn_summary_odds",
                "source_payload_ref": f"espn_summary_odds:{schedule_row['event_id']}:{odds_index}:{market_row['market_type']}:{market_row['selection']}:{market_row['line']}",
            }
            row["source_snapshot_hash"] = deterministic_hash(row)
            rows.append(row)
    return rows


def parse_public_odds_fixture_html(
    html: str,
    *,
    sport: str,
    league: str,
    api_fetched_at: str,
    source: str = "public_fixture_odds",
) -> list[dict[str, Any]]:
    lowered = html.lower()
    if "captcha" in lowered or "sign in" in lowered or "log in" in lowered:
        raise PermissionError("captcha_or_login_required")
    match = re.search(r"<script[^>]+id=[\"']sports-odds-data[\"'][^>]*>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("parse_failed")
    try:
        payload = json.loads(unescape(match.group(1)).strip(), parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ValueError("parse_failed") from exc
    return normalize_public_odds_rows(payload, sport=sport, league=league, api_fetched_at=api_fetched_at, source=source)


def normalize_public_odds_rows(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    league: str,
    api_fetched_at: str,
    source: str = "public_odds",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload:
        row = {
            "asset_class": "sports",
            "source": source,
            "sport": sport,
            "league": league,
            "event_id": item.get("event_id"),
            "game_id": item.get("game_id") or item.get("event_id"),
            "home_team": item.get("home_team"),
            "away_team": item.get("away_team"),
            "game_start_time": item.get("game_start_time"),
            "bookmaker": item.get("bookmaker") or "public",
            "market_type": item.get("market_type"),
            "selection": item.get("selection"),
            "line": item.get("line"),
            "odds": item.get("odds"),
            "odds_format": item.get("odds_format") or "american",
            "odds_timestamp": item.get("odds_timestamp") or api_fetched_at,
            "api_fetched_at": api_fetched_at,
            "market_close_time": item.get("market_close_time") or item.get("game_start_time"),
            "source_payload_ref": item.get("source_payload_ref") or f"{source}:{item.get('home_team')}:{item.get('away_team')}:{item.get('market_type')}:{item.get('selection')}:{item.get('line')}",
        }
        if row["line"] is not None:
            row["line"] = Decimal(str(row["line"]))
        if row["odds"] is not None:
            row["odds"] = Decimal(str(row["odds"]))
        row["source_snapshot_hash"] = deterministic_hash(row)
        rows.append(row)
    return rows


def match_odds_to_schedule(
    odds_rows: list[dict[str, Any]],
    schedule_rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.85,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    schedule_by_id = {str(row.get("event_id")): row for row in schedule_rows if row.get("event_id")}
    for odds_row in odds_rows:
        best: tuple[float, dict[str, Any] | None] = (0.0, None)
        if odds_row.get("event_id") and str(odds_row["event_id"]) in schedule_by_id:
            best = (1.0, schedule_by_id[str(odds_row["event_id"])])
        else:
            for schedule_row in schedule_rows:
                score = 0.0
                if normalize_team_name(odds_row.get("home_team")) == normalize_team_name(schedule_row.get("home_team")):
                    score += 0.35
                if normalize_team_name(odds_row.get("away_team")) == normalize_team_name(schedule_row.get("away_team")):
                    score += 0.35
                if str(odds_row.get("league") or "").lower() == str(schedule_row.get("league") or "").lower():
                    score += 0.15
                odds_start = parse_aware_timestamp(odds_row.get("game_start_time"))
                schedule_start = parse_aware_timestamp(schedule_row.get("game_start_time"))
                if odds_start and schedule_start and abs((odds_start - schedule_start).total_seconds()) <= 3600:
                    score += 0.15
                if score > best[0]:
                    best = (score, schedule_row)
        confidence, schedule_row = best
        if confidence < min_confidence or schedule_row is None:
            rejected.append({**odds_row, "rejection_reason": "low_confidence_event_match", "match_confidence": round(confidence, 6)})
            continue
        merged = {**odds_row}
        for key in ("event_id", "game_id", "home_team", "away_team", "game_start_time", "market_close_time"):
            if schedule_row.get(key):
                merged[key] = schedule_row[key]
        merged["match_confidence"] = round(confidence, 6)
        merged["source_snapshot_hash"] = deterministic_hash(merged)
        matched.append(merged)
    return matched, rejected


def normalize_the_odds_api_payload(
    payload: list[dict[str, Any]],
    *,
    sport: str,
    league: str,
    api_fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in payload:
        event_id = str(event.get("id") or "")
        game_start_time = event.get("commence_time")
        home_team = event.get("home_team")
        away_team = event.get("away_team")
        for bookmaker in event.get("bookmakers") or []:
            bookmaker_key = bookmaker.get("key") or bookmaker.get("title") or "unknown"
            odds_timestamp = bookmaker.get("last_update")
            for market in bookmaker.get("markets") or []:
                market_key = market.get("key")
                market_type = {"h2h": "moneyline", "spreads": "spread", "totals": "total"}.get(str(market_key), str(market_key))
                market_updated = market.get("last_update") or odds_timestamp
                for outcome in market.get("outcomes") or []:
                    selection = outcome.get("name")
                    line = outcome.get("point")
                    price = outcome.get("price")
                    if event_id and game_start_time and home_team and away_team and selection is not None and price is not None:
                        row = {
                            "asset_class": "sports",
                            "sport": sport,
                            "league": league,
                            "event_id": event_id,
                            "game_id": event_id,
                            "home_team": home_team,
                            "away_team": away_team,
                            "game_start_time": game_start_time,
                            "bookmaker": str(bookmaker_key),
                            "market_type": market_type,
                            "selection": str(selection),
                            "line": None if line is None else Decimal(str(line)),
                            "odds": Decimal(str(price)),
                            "odds_format": "american",
                            "odds_timestamp": market_updated,
                            "api_fetched_at": api_fetched_at,
                            "market_close_time": game_start_time,
                            "source_payload_ref": f"the_odds_api:{event_id}:{bookmaker_key}:{market_type}:{selection}:{line}",
                        }
                        row["source_snapshot_hash"] = deterministic_hash(row)
                        rows.append(row)
    return rows


def _sports_blocked_payload(
    *,
    sport: str,
    league: str,
    source: str,
    source_urls: list[str],
    errors: list[dict[str, Any]],
    rejected_records: list[dict[str, Any]],
    retrieval_plan: list[str],
    retrieval_attempts: list[dict[str, Any]],
    raw_evidence: list[dict[str, Any]],
    blocker: str = "blocked_public_source_unavailable",
) -> dict[str, Any]:
    return {
        "asset_class": "sports",
        "source_mode": "blocked",
        "source": source,
        "source_urls": source_urls,
        "model_version": SPORTS_MODEL_VERSION,
        "strategy": SPORTS_STRATEGY,
        "generated_at": utc_now_iso(),
        "records": [],
        "schedule": [],
        "finals": [],
        "rejected_records": rejected_records,
        "errors": errors,
        "blocker": blocker,
        "sport": sport,
        "league": league,
        "retrieval_method": None,
        "retrieval_plan": retrieval_plan,
        "retrieval_attempts": retrieval_attempts,
        "raw_evidence": raw_evidence,
        "freshness_state": "blocked",
    }


def _source_rejection(*, sport: str, league: str, source: str, reason: str, evidence_ref: str | None = None) -> dict[str, Any]:
    return {
        "sport": sport,
        "league": league,
        "source": source,
        "source_payload_ref": evidence_ref or f"{source}:unparsed",
        "rejection_reason": reason,
    }


def _set_evidence_record_count(evidence_rows: list[dict[str, Any]], evidence_ref: str | None, count: int) -> None:
    for evidence in reversed(evidence_rows):
        if evidence.get("raw_evidence_reference") == evidence_ref:
            evidence["normalized_record_count"] = count
            return


def _attempt_failure_reason(attempts: list[dict[str, Any]]) -> str:
    for attempt in reversed(attempts):
        if attempt.get("status") == "rejected" and attempt.get("reason"):
            return str(attempt["reason"])
    return "source_unavailable"


def collect_sports_payload(
    *,
    sport_key: str = DEFAULT_SPORT_KEY,
    api_key: str | None = None,
    http: HttpClient | None = None,
    date: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    scraper_enabled = str(values.get("SPORTS_SCRAPER_ENABLED", "true")).lower() in {"1", "true", "yes", "on"}
    resolved_api_key = api_key if api_key is not None else values.get("THE_ODDS_API_KEY") or values.get("ODDS_API_KEY")
    retrieval_plan = configured_retrieval_plan(values.get("SPORTS_RETRIEVAL_PLAN"))
    # Use the project's own identifier rather than a browser string. ESPN rejects
    # the spoofed agent outright -- reproducibly 403, which is why sports collection
    # never produced a row -- and accepts the honest one that names the project and
    # carries a contact URL. Identifying truthfully is also what robots handling
    # here assumes.
    client = http or HttpClient(cache_ttl_seconds=60)
    sport, league = _sports_league_from_key(sport_key)
    yyyymmdd = date or _today_yyyymmdd()
    source_timeout_seconds = _bounded_config_int(values, "SPORTS_SOURCE_TIMEOUT_SECONDS", 8, 1, 30)
    max_summary_requests = _bounded_config_int(values, "SPORTS_MAX_SUMMARY_REQUESTS", 16, 0, 50)
    errors: list[dict[str, Any]] = []
    rejected_records: list[dict[str, Any]] = []
    retrieval_attempts: list[dict[str, Any]] = []
    raw_evidence: list[dict[str, Any]] = []
    source_urls: list[str] = []

    if resolved_api_key and "official_api" in retrieval_plan:
        safe_odds_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        private_odds_url = f"{safe_odds_url}?{urlencode({'apiKey': resolved_api_key, 'regions': 'us', 'markets': 'h2h,spreads,totals', 'oddsFormat': 'american', 'dateFormat': 'iso'})}"
        source_urls.append(safe_odds_url)
        official_result, official_attempts, official_evidence = collect_from_plan(
            SourceRequest(
                resource=safe_odds_url,
                private_resource=private_odds_url,
                parser_version=SPORTS_PARSER_VERSION,
                freshness_seconds=SPORTS_STALE_SECONDS,
                timeout_seconds=source_timeout_seconds,
            ),
            plan=["official_api"],
            adapters={
                "official_api": HttpJsonSourceAdapter(
                    source_name="the_odds_api",
                    http=client,
                    retrieval_method="official_api",
                )
            },
        )
        retrieval_attempts.extend(official_attempts)
        raw_evidence.extend(official_evidence)
        if official_result is not None:
            try:
                official_records = normalize_the_odds_api_payload(
                    official_result.raw_result,
                    sport=sport_key,
                    league=league,
                    api_fetched_at=official_result.received_time,
                )
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({"source": "the_odds_api", "reason": "parse_failed", "message": type(exc).__name__})
                rejected_records.append(
                    _source_rejection(
                        sport=sport_key,
                        league=league,
                        source="the_odds_api",
                        reason="parse_failed",
                        evidence_ref=official_result.raw_evidence_reference,
                    )
                )
            else:
                official_result.normalized_record_count = len(official_records)
                _set_evidence_record_count(raw_evidence, official_result.raw_evidence_reference, len(official_records))
                if official_records:
                    return {
                        "asset_class": "sports",
                        "source_mode": "api",
                        "source": "the_odds_api",
                        "source_urls": source_urls,
                        "model_version": SPORTS_MODEL_VERSION,
                        "strategy": SPORTS_STRATEGY,
                        "generated_at": official_result.received_time,
                        "records": official_records,
                        "schedule": [],
                        "finals": [],
                        "rejected_records": rejected_records,
                        "errors": errors,
                        "blocker": None,
                        "firecrawl_configured": is_firecrawl_configured(values),
                        "retrieval_method": "official_api",
                        "retrieval_plan": retrieval_plan,
                        "retrieval_attempts": retrieval_attempts,
                        "raw_evidence": raw_evidence,
                        "freshness_state": "fresh",
                    }
                errors.append({"source": "the_odds_api", "reason": "empty_content", "message": "official source returned no usable odds rows"})
        else:
            reason = _attempt_failure_reason(official_attempts)
            errors.append({"source": "the_odds_api", "reason": reason or "source_unavailable", "message": "official source unavailable"})

    if not scraper_enabled:
        errors.append({"source": "sports_scraper", "reason": "source_blocked", "message": "SPORTS_SCRAPER_ENABLED is false and no API source produced rows"})
        return _sports_blocked_payload(
            sport=sport_key,
            league=league,
            source="none",
            source_urls=source_urls,
            errors=errors,
            rejected_records=rejected_records,
            retrieval_plan=retrieval_plan,
            retrieval_attempts=retrieval_attempts,
            raw_evidence=raw_evidence,
            blocker="blocked_missing_sports_source",
        )

    scoreboard_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={yyyymmdd}"
    source_urls.append(scoreboard_url)
    public_adapters: dict[str, Any] = {
        "http_json": HttpJsonSourceAdapter(source_name="espn_scoreboard", http=client),
    }
    if is_firecrawl_configured(values):
        public_adapters["firecrawl"] = FirecrawlJsonSourceAdapter(env=values)
    public_plan = [method for method in retrieval_plan if method in {"http_json", "firecrawl"}]
    source_result, source_attempts, source_evidence = collect_from_plan(
        SourceRequest(
            resource=scoreboard_url,
            parser_version=SPORTS_PARSER_VERSION,
            freshness_seconds=SPORTS_STALE_SECONDS,
            timeout_seconds=source_timeout_seconds,
        ),
        plan=public_plan,
        adapters=public_adapters,
    )
    retrieval_attempts.extend(source_attempts)
    raw_evidence.extend(source_evidence)
    if source_result is None:
        reason = _attempt_failure_reason(source_attempts)
        errors.append({"source": "espn_scoreboard", "reason": reason or "source_unavailable", "message": "public source unavailable"})
        rejected_records.append(_source_rejection(sport=sport_key, league=league, source="espn_scoreboard", reason=reason or "source_unavailable"))
        return _sports_blocked_payload(
            sport=sport_key,
            league=league,
            source="espn_scoreboard",
            source_urls=source_urls,
            errors=errors,
            rejected_records=rejected_records,
            retrieval_plan=retrieval_plan,
            retrieval_attempts=retrieval_attempts,
            raw_evidence=raw_evidence,
        )
    try:
        normalized = normalize_espn_scoreboard_payload(
            source_result.raw_result,
            sport=sport,
            league=league,
            api_fetched_at=source_result.received_time,
        )
        _set_evidence_record_count(
            raw_evidence,
            source_result.raw_evidence_reference,
            len(normalized["schedule"]) + len(normalized["finals"]) + len(normalized["odds"]),
        )
        prior_finals = _collect_prior_day_finals(
            sport=sport,
            league=league,
            lookback_days=_bounded_config_int(values, "SPORTS_FINALS_LOOKBACK_DAYS", 2, 0, 7),
            client=client,
            timeout_seconds=source_timeout_seconds,
            source_urls=source_urls,
            retrieval_attempts=retrieval_attempts,
            raw_evidence=raw_evidence,
            adapters=public_adapters,
            plan=public_plan,
        )
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        errors.append({"source": "espn_scoreboard", "reason": "parse_failed", "message": type(exc).__name__})
        rejected_records.append(
            _source_rejection(
                sport=sport_key,
                league=league,
                source="espn_scoreboard",
                reason="parse_failed",
                evidence_ref=source_result.raw_evidence_reference,
            )
        )
        return _sports_blocked_payload(
            sport=sport_key,
            league=league,
            source="espn_scoreboard",
            source_urls=source_urls,
            errors=errors,
            rejected_records=rejected_records,
            retrieval_plan=retrieval_plan,
            retrieval_attempts=retrieval_attempts,
            raw_evidence=raw_evidence,
        )
    records, match_rejections = match_odds_to_schedule(normalized["odds"], normalized["schedule"])
    rejected_records.extend(match_rejections)
    current_source = "espn_scoreboard"
    current_retrieval_method = source_result.retrieval_method
    if not records and normalized["schedule"]:
        summary_odds_rows: list[dict[str, Any]] = []
        summary_retrieval_method: str | None = None
        summary_schedule = normalized["schedule"][:max_summary_requests]
        if len(normalized["schedule"]) > len(summary_schedule):
            errors.append(
                {
                    "source": "espn_summary",
                    "reason": "summary_request_limit_reached",
                    "message": f"limited summary requests to {max_summary_requests}",
                }
            )
        for schedule_row in summary_schedule:
            summary_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={schedule_row['event_id']}"
            source_urls.append(summary_url)
            summary_result, summary_attempts, summary_evidence = collect_from_plan(
                SourceRequest(
                    resource=summary_url,
                    parser_version=SPORTS_PARSER_VERSION,
                    freshness_seconds=SPORTS_STALE_SECONDS,
                    timeout_seconds=source_timeout_seconds,
                ),
                plan=public_plan,
                adapters={
                    "http_json": HttpJsonSourceAdapter(source_name="espn_summary", http=client),
                    **({"firecrawl": FirecrawlJsonSourceAdapter(env=values)} if is_firecrawl_configured(values) else {}),
                },
            )
            retrieval_attempts.extend(summary_attempts)
            raw_evidence.extend(summary_evidence)
            if summary_result is None:
                reason = _attempt_failure_reason(summary_attempts)
                errors.append({"source": "espn_summary", "reason": reason or "source_unavailable", "message": "summary source unavailable"})
                continue
            try:
                parsed_summary_rows = normalize_espn_summary_odds(
                    summary_result.raw_result,
                    schedule_row=schedule_row,
                    api_fetched_at=summary_result.received_time,
                )
                summary_odds_rows.extend(parsed_summary_rows)
                _set_evidence_record_count(
                    raw_evidence,
                    summary_result.raw_evidence_reference,
                    len(parsed_summary_rows),
                )
                if parsed_summary_rows:
                    summary_retrieval_method = summary_result.retrieval_method
            except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                errors.append({"source": "espn_summary", "reason": "parse_failed", "message": type(exc).__name__})
                rejected_records.append(
                    _source_rejection(
                        sport=sport_key,
                        league=league,
                        source="espn_summary",
                        reason="parse_failed",
                        evidence_ref=summary_result.raw_evidence_reference,
                    )
                )
            time.sleep(0.2)
        records, summary_rejections = match_odds_to_schedule(summary_odds_rows, normalized["schedule"])
        rejected_records.extend(summary_rejections)
        if records:
            current_source = "espn_summary"
            current_retrieval_method = summary_retrieval_method or current_retrieval_method
    blocker = None
    if not normalized["schedule"]:
        blocker = "blocked_no_scheduled_events"
        errors.append({"source": "espn_scoreboard", "reason": "empty_content", "message": "fresh scoreboard contained no scheduled events"})
        rejected_records.append(
            _source_rejection(
                sport=sport_key,
                league=league,
                source="espn_scoreboard",
                reason="empty_content",
                evidence_ref=source_result.raw_evidence_reference,
            )
        )
    elif not records:
        blocker = "blocked_no_usable_odds"
        if not any(error.get("source") in {"espn_public_odds", "espn_summary"} for error in errors):
            errors.append({"source": "espn_public_odds", "reason": "empty_content", "message": "public ESPN scoreboard/summary exposed no usable odds rows"})
        if not rejected_records:
            rejected_records.append(
                _source_rejection(
                    sport=sport_key,
                    league=league,
                    source="espn_public_odds",
                    reason="empty_content",
                    evidence_ref=source_result.raw_evidence_reference,
                )
            )
    return {
        "asset_class": "sports",
        "source_mode": SPORTS_SOURCE_MODE,
        "source": current_source,
        "source_urls": source_urls,
        "model_version": SPORTS_MODEL_VERSION,
        "strategy": SPORTS_STRATEGY,
        "generated_at": utc_now_iso(),
        "records": records,
        "schedule": normalized["schedule"],
        "finals": normalized["finals"] + prior_finals,
        "rejected_records": rejected_records,
        "errors": errors,
        "blocker": blocker,
        "firecrawl_configured": is_firecrawl_configured(values),
        "retrieval_method": current_retrieval_method,
        "retrieval_plan": retrieval_plan,
        "retrieval_attempts": retrieval_attempts,
        "raw_evidence": raw_evidence,
        "freshness_state": "fresh" if records else "blocked",
    }


def write_sports_payload(path: str | Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def _start_sports_collection_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    ledger = CollectionLedger()
    evidence_rows = list(payload.get("raw_evidence") or [])
    if not evidence_rows:
        evidence_rows = [
            {
                "source_name": payload.get("source") or "sports_source",
                "requested_resource": (payload.get("source_urls") or ["sports_cycle"])[0],
                "retrieval_method": payload.get("retrieval_method") or "unknown",
                "source_observation_time": payload.get("generated_at"),
                "received_time": payload.get("generated_at") or utc_now_iso(),
                "content_hash": content_hash(payload),
                "parser_version": SPORTS_PARSER_VERSION,
                "freshness_state": payload.get("freshness_state") or "unknown",
                "validation_state": "rejected" if payload.get("blocker") else "valid",
                "failure_reason": payload.get("blocker"),
                "raw_payload": payload,
            }
        ]
    # Scope the batch to its collection cycle. Content alone is not enough: a
    # source that keeps returning the same thing -- unchanged data, or the same
    # failure hour after hour -- hashes identically every cycle, so a purely
    # content-derived key would pin every future cycle onto one batch. The worker
    # supplies a key that is stable across the retries of one cycle and new on the
    # next, which is exactly the scope a batch should have. Standalone runs have no
    # worker cycle, so they fall back to the payload's own collection time.
    cycle_key = current_worker_idempotency_key() or str(payload.get("generated_at") or utc_now_iso())
    idempotency_key = content_hash(
        {
            "worker": "sports-research",
            "cycle": cycle_key,
            "resources": sorted(str(row.get("requested_resource") or "") for row in evidence_rows),
            "hashes": sorted(str(row.get("content_hash") or "") for row in evidence_rows),
        }
    )
    batch = ledger.start_batch(
        idempotency_key=idempotency_key,
        source=str(payload.get("source") or "sports_source"),
        endpoint="sports_cycle",
        worker_name="sports-research",
        worker_version=SPORTS_MODEL_VERSION,
        collector_version=SPORTS_PARSER_VERSION,
        request_parameters={
            "source_urls": payload.get("source_urls") or [],
            "retrieval_plan": payload.get("retrieval_plan") or [],
        },
        started_at=str(payload.get("generated_at") or utc_now_iso()),
    )
    payload_ids: list[str] = []
    duplicate_payloads = 0
    for evidence in evidence_rows:
        stored = ledger.store_payload(
            batch_id=batch.batch_id,
            source=str(evidence.get("source_name") or payload.get("source") or "sports_source"),
            entity_type="sports_source_response",
            source_identifier=str(evidence.get("requested_resource") or "sports_cycle"),
            observed_at=evidence.get("source_observation_time"),
            received_at=evidence.get("received_time"),
            parser_version=str(evidence.get("parser_version") or SPORTS_PARSER_VERSION),
            payload=evidence.get("raw_payload"),
        )
        payload_ids.append(stored["payload_id"])
        duplicate_payloads += int(stored["duplicate"])
    for index, rejected in enumerate(payload.get("rejected_records") or []):
        ledger.reject(
            batch_id=batch.batch_id,
            payload_id=payload_ids[min(index, len(payload_ids) - 1)] if payload_ids else None,
            entity_type="sports_source_record",
            rejection_code=str(rejected.get("rejection_reason") or "parse_failed"),
            rejection_detail=str(rejected.get("source_payload_ref") or ""),
            parser_version=SPORTS_PARSER_VERSION,
        )
    return {
        "ledger": ledger,
        "batch_id": batch.batch_id,
        "batch_created": batch.created,
        "payload_hash": content_hash(evidence_rows),
        "records_received": len(payload.get("records") or []) + len(payload.get("rejected_records") or []),
        "duplicate_payloads": duplicate_payloads,
    }


def _finish_sports_collection_ledger(
    context: dict[str, Any],
    *,
    payload: dict[str, Any],
    log_result: dict[str, Any],
) -> dict[str, Any]:
    ledger: CollectionLedger = context["ledger"]
    source = str(payload.get("source") or "sports_source")
    attempted_at = str(payload.get("generated_at") or utc_now_iso())
    if payload.get("blocker"):
        ledger.fail_batch(
            batch_id=context["batch_id"],
            error_code=str(payload["blocker"]),
            error_message="sports source did not produce validated fresh rows",
            blocked=True,
        )
        ledger.update_source_health(
            source=source,
            last_attempted_at=attempted_at,
            freshness_state="blocked",
            last_error=str(payload["blocker"]),
        )
        return {"status": "blocked", "batch_id": context["batch_id"], "batch_created": context["batch_created"]}
    ledger.complete_batch(
        batch_id=context["batch_id"],
        records_received=context["records_received"],
        records_accepted=int(log_result.get("logged_predictions") or 0),
        records_rejected=int(log_result.get("rejected_predictions") or 0),
        records_duplicated=int(log_result.get("duplicate_rows_ignored") or 0) + int(context["duplicate_payloads"]),
        payload_hash_value=context["payload_hash"],
        checkpoint={
            "source": source,
            "endpoint": "sports_cycle",
            "partition_scope": str(payload.get("sport") or DEFAULT_SPORT_KEY),
            "last_successful_item_time": max(
                (str(row.get("odds_timestamp") or row.get("api_fetched_at") or "") for row in payload.get("records") or []),
                default=attempted_at,
            ),
        },
    )
    ledger.update_source_health(
        source=source,
        last_attempted_at=attempted_at,
        last_successful_at=attempted_at,
        freshness_deadline=(payload.get("raw_evidence") or [{}])[-1].get("freshness_deadline"),
        freshness_state="fresh",
    )
    return {"status": "completed", "batch_id": context["batch_id"], "batch_created": context["batch_created"]}


def _build_candidate(record: dict[str, Any], *, run_id: str, prediction_timestamp: str) -> dict[str, Any]:
    implied = american_odds_implied_probability(record["odds"])
    confidence = min(Decimal("0.69"), max(Decimal("0.5"), implied))
    features = {
        "odds": Decimal(str(record["odds"])),
        "line": record.get("line"),
        "implied_probability": round(implied, 6),
        "probability_source": "bookmaker_implied",
        "model_state": "baseline_only",
        "decision_status": "track_only",
        "decision_reason": "market_price_observation_not_validated_model",
        "bookmaker": record.get("bookmaker"),
        "market_type": record.get("market_type"),
        "source": record.get("source"),
        "match_confidence": record.get("match_confidence"),
    }
    return {
        "asset_class": "sports",
        "run_id": run_id,
        "model_version": SPORTS_MODEL_VERSION,
        "strategy": SPORTS_STRATEGY,
        "sport": record.get("sport"),
        "league": record.get("league"),
        "event_id": record.get("event_id"),
        "game_id": record.get("game_id") or record.get("event_id"),
        "home_team": record.get("home_team"),
        "away_team": record.get("away_team"),
        "bookmaker": record.get("bookmaker"),
        "market_type": record.get("market_type"),
        "selection": record.get("selection"),
        "line": record.get("line"),
        "odds": record.get("odds"),
        "odds_format": record.get("odds_format") or "american",
        "prediction_timestamp": prediction_timestamp,
        "odds_timestamp": record.get("odds_timestamp"),
        "game_start_time": record.get("game_start_time"),
        "market_close_time": record.get("market_close_time") or record.get("game_start_time"),
        "api_fetched_at": record.get("api_fetched_at"),
        "source_snapshot_hash": record.get("source_snapshot_hash"),
        "source_payload_ref": record.get("source_payload_ref"),
        "confidence_score": round(confidence, 6),
        "model_state": "baseline_only",
        "probability_source": "bookmaker_implied",
        "decision_status": "track_only",
        "features": features,
        "pre_rejection_reason": record.get("rejection_reason"),
        "settlement_state": "unresolved",
    }


def build_sports_prediction_candidates(payload: dict[str, Any], *, run_id: str, prediction_timestamp: str | None = None) -> list[dict[str, Any]]:
    timestamp = prediction_timestamp or payload.get("generated_at") or utc_now_iso()
    return [_build_candidate(record, run_id=run_id, prediction_timestamp=timestamp) for record in payload.get("records") or []]


def validate_sports_prediction(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = [
        ("event_id", "missing_event_id"),
        ("game_start_time", "missing_game_start_time"),
        ("prediction_timestamp", "missing_prediction_timestamp"),
        ("odds_timestamp", "missing_odds_timestamp"),
        ("api_fetched_at", "missing_api_fetched_at"),
        ("source_snapshot_hash", "missing_source_snapshot_hash"),
        ("market_type", "missing_market_type"),
        ("odds", "missing_odds"),
    ]
    for field, reason in required:
        if candidate.get(field) in {None, ""}:
            errors.append(reason)
    prediction_time = parse_aware_timestamp(candidate.get("prediction_timestamp"))
    odds_time = parse_aware_timestamp(candidate.get("odds_timestamp"))
    api_time = parse_aware_timestamp(candidate.get("api_fetched_at"))
    start_time = parse_aware_timestamp(candidate.get("game_start_time"))
    for field, timestamp in [
        ("prediction_timestamp", prediction_time),
        ("odds_timestamp", odds_time),
        ("api_fetched_at", api_time),
        ("game_start_time", start_time),
    ]:
        if candidate.get(field) and timestamp is None:
            errors.append("invalid_timezone")
    if prediction_time and start_time and prediction_time >= start_time:
        errors.append("prediction_after_game_start")
    if odds_time and start_time and odds_time >= start_time:
        errors.append("odds_after_game_start")
    if prediction_time and odds_time and (prediction_time - odds_time).total_seconds() > SPORTS_STALE_SECONDS:
        errors.append("stale_odds")
    if prediction_time and odds_time and odds_time > prediction_time:
        errors.append("odds_timestamp_after_prediction")
    if prediction_time and api_time and (prediction_time - api_time).total_seconds() > SPORTS_STALE_SECONDS:
        errors.append("stale_source")
    if prediction_time and api_time and api_time > prediction_time:
        errors.append("api_fetched_after_prediction")
    return sorted(set(errors))


def log_sports_predictions(
    *,
    run_id: str,
    payload: dict[str, Any],
    prediction_timestamp: str | None = None,
) -> dict[str, Any]:
    candidates = build_sports_prediction_candidates(payload, run_id=run_id, prediction_timestamp=prediction_timestamp)
    rejection_timestamp = prediction_timestamp or payload.get("generated_at") or utc_now_iso()
    for record in payload.get("rejected_records") or []:
        try:
            candidate = _build_candidate(record, run_id=run_id, prediction_timestamp=rejection_timestamp)
        except (KeyError, TypeError, ValueError):
            candidate = {
                "asset_class": "sports",
                "run_id": run_id,
                "model_version": SPORTS_MODEL_VERSION,
                "strategy": SPORTS_STRATEGY,
                "sport": record.get("sport"),
                "league": record.get("league"),
                "event_id": record.get("event_id"),
                "market_type": record.get("market_type"),
                "selection": record.get("selection"),
                "line": record.get("line"),
                "bookmaker": record.get("bookmaker"),
                "prediction_timestamp": rejection_timestamp,
                "pre_rejection_reason": record.get("rejection_reason") or "parse_failed",
            }
        candidate["pre_rejection_reason"] = record.get("rejection_reason") or candidate.get("pre_rejection_reason")
        candidates.append(candidate)
    if payload.get("blocker") and not candidates:
        return {
            "asset_class": "sports",
            "run_id": run_id,
            "attempted_predictions": 0,
            "logged_predictions": 0,
            "rejected_predictions": 0,
            "duplicate_rows_ignored": 0,
            "rejection_reasons": {},
            "blocker": payload["blocker"],
        }
    logged = 0
    rejected = 0
    duplicate_rows = 0
    rejection_reasons: Counter[str] = Counter()
    with _connect() as connection:
        for candidate in candidates:
            errors = [candidate["pre_rejection_reason"]] if candidate.get("pre_rejection_reason") else validate_sports_prediction(candidate)
            exact_duplicate = connection.execute(
                """
                SELECT 1
                FROM app.sports_prediction_logs
                WHERE run_id = %s AND strategy = %s AND sport = %s AND league = %s AND event_id = %s
                  AND market_type = %s AND selection = %s AND COALESCE(line, -999999) = COALESCE(%s, -999999)
                  AND bookmaker = %s AND prediction_timestamp = %s
                LIMIT 1
                """,
                (
                    run_id,
                    candidate["strategy"],
                    candidate["sport"],
                    candidate["league"],
                    candidate["event_id"],
                    candidate["market_type"],
                    candidate["selection"],
                    candidate.get("line"),
                    candidate["bookmaker"],
                    candidate["prediction_timestamp"],
                ),
            ).fetchone()
            if exact_duplicate:
                errors.append("exact_duplicate")
            latest = connection.execute(
                """
                SELECT source_snapshot_hash, odds, line, confidence_score, features_json, snapshot_sequence
                FROM app.sports_prediction_logs
                WHERE run_id = %s AND strategy = %s AND sport = %s AND league = %s AND event_id = %s
                  AND market_type = %s AND selection = %s AND bookmaker = %s AND validation_status = 'valid'
                ORDER BY prediction_timestamp DESC, id DESC
                LIMIT 1
                """,
                (
                    run_id,
                    candidate["strategy"],
                    candidate["sport"],
                    candidate["league"],
                    candidate["event_id"],
                    candidate["market_type"],
                    candidate["selection"],
                    candidate["bookmaker"],
                ),
            ).fetchone()
            snapshot_sequence = 1
            if latest:
                snapshot_sequence = int(latest["snapshot_sequence"] or 1) + 1
                if "exact_duplicate" not in errors and (
                    latest["source_snapshot_hash"] == candidate["source_snapshot_hash"]
                    and as_decimal(latest["odds"]) == as_decimal(candidate["odds"])
                    and as_decimal(latest["line"]) == as_decimal(candidate.get("line"))
                    and as_decimal(latest["confidence_score"]) == as_decimal(candidate["confidence_score"])
                    and stable_json(_json_object(latest["features_json"])) == stable_json(candidate["features"])
                ):
                    errors.append("unchanged_repeat_snapshot")
            if errors:
                rejected += 1
                reason = errors[0]
                if reason == "exact_duplicate":
                    duplicate_rows += 1
                rejection_reasons[reason] += 1
                connection.execute(
                    """
                    INSERT INTO app.sports_prediction_rejections
                        (run_id, strategy, sport, league, event_id, market_type, selection, line,
                         bookmaker, prediction_timestamp, rejection_reason, raw_log_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        run_id,
                        candidate.get("strategy"),
                        candidate.get("sport"),
                        candidate.get("league"),
                        candidate.get("event_id"),
                        candidate.get("market_type"),
                        candidate.get("selection"),
                        candidate.get("line"),
                        candidate.get("bookmaker"),
                        candidate.get("prediction_timestamp"),
                        reason,
                        stable_json(candidate),
                    ),
                )
                continue
            cursor = connection.execute(
                """
                INSERT INTO app.sports_prediction_logs
                    (asset_class, run_id, model_version, strategy, sport, league, event_id, game_id,
                     home_team, away_team, bookmaker, market_type, selection, line, odds, odds_format,
                     prediction_timestamp, odds_timestamp, game_start_time, market_close_time,
                     api_fetched_at, source_snapshot_hash, source_payload_ref, confidence_score,
                     features_json, validation_status, rejection_reason, snapshot_sequence, settlement_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (asset_class, run_id, strategy, sport, league, event_id, market_type,
                    selection, line, bookmaker, prediction_timestamp) DO NOTHING
                """,
                (
                    "sports",
                    run_id,
                    candidate["model_version"],
                    candidate["strategy"],
                    candidate["sport"],
                    candidate["league"],
                    candidate["event_id"],
                    candidate["game_id"],
                    candidate["home_team"],
                    candidate["away_team"],
                    candidate["bookmaker"],
                    candidate["market_type"],
                    candidate["selection"],
                    candidate.get("line"),
                    candidate["odds"],
                    candidate["odds_format"],
                    candidate["prediction_timestamp"],
                    candidate["odds_timestamp"],
                    candidate["game_start_time"],
                    candidate.get("market_close_time"),
                    candidate["api_fetched_at"],
                    candidate["source_snapshot_hash"],
                    candidate.get("source_payload_ref"),
                    candidate["confidence_score"],
                    stable_json(candidate["features"]),
                    "valid",
                    None,
                    snapshot_sequence,
                    "unresolved",
                ),
            )
            if cursor.rowcount:
                logged += 1
            else:
                rejected += 1
                duplicate_rows += 1
                rejection_reasons["exact_duplicate"] += 1
        connection.commit()
    return {
        "asset_class": "sports",
        "run_id": run_id,
        "attempted_predictions": len(candidates),
        "logged_predictions": logged,
        "rejected_predictions": rejected,
        "duplicate_rows_ignored": duplicate_rows,
        "rejection_reasons": dict(rejection_reasons),
    }


def _final_score_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(event.get("event_id") or event.get("id") or event.get("game_id")): event for event in payload.get("events") or []}


def _grade_market(row: DatabaseRow, final_event: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    status = str(final_event.get("status") or "final").lower()
    final_score = {
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "home_score": final_event.get("home_score"),
        "away_score": final_event.get("away_score"),
        "status": status,
    }
    if status in {"postponed"}:
        return "void", "postponed", final_score
    if status in {"canceled", "cancelled"}:
        return "void", "canceled", final_score
    if final_event.get("home_score") is None or final_event.get("away_score") is None:
        return "unresolved", None, final_score
    home_score = Decimal(str(final_event["home_score"]))
    away_score = Decimal(str(final_event["away_score"]))
    selection = str(row["selection"])
    market_type = str(row["market_type"])
    if market_type == "moneyline":
        winner = row["home_team"] if home_score > away_score else row["away_team"] if away_score > home_score else None
        if winner is None:
            return "push", "push", final_score
        return "settled", "win" if selection == winner else "loss", final_score
    if market_type == "spread":
        if row["line"] is None:
            return "unresolved", None, final_score
        selected_score = home_score if selection == row["home_team"] else away_score
        other_score = away_score if selection == row["home_team"] else home_score
        margin = selected_score + (as_decimal(row["line"], default=Decimal("0")) or Decimal("0")) - other_score
        if margin == 0:
            return "push", "push", final_score
        return "settled", "win" if margin > 0 else "loss", final_score
    if market_type == "total":
        if row["line"] is None:
            return "unresolved", None, final_score
        total = home_score + away_score
        line = as_decimal(row["line"], default=Decimal("0")) or Decimal("0")
        if total == line:
            return "push", "push", final_score
        is_over = selection.lower() == "over"
        return "settled", "win" if (total > line) == is_over else "loss", final_score
    return "unresolved", None, final_score


def settle_sports_predictions(*, run_id: str, finals_payload: dict[str, Any]) -> dict[str, Any]:
    updated = 0
    unresolved = 0
    issue_counts: Counter[str] = Counter()
    finals = _final_score_map(finals_payload)
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM app.sports_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid' AND settlement_state = 'unresolved'
            FOR UPDATE SKIP LOCKED
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            final_event = finals.get(row["event_id"])
            if not final_event:
                unresolved += 1
                issue_counts["missing_final_score"] += 1
                continue
            state, outcome, final_score = _grade_market(row, final_event)
            if state == "unresolved":
                unresolved += 1
                issue_counts["missing_final_score"] += 1
                continue
            cursor = connection.execute(
                """
                UPDATE app.sports_prediction_logs
                SET settlement_state = %s, actual_outcome = %s, final_score_json = %s::jsonb,
                    settlement_updated_at = %s, settlement_source = %s, settlement_issue = NULL
                WHERE id = %s AND settlement_state = 'unresolved'
                """,
                (state, outcome, stable_json(final_score), utc_now_iso(), "official_final_score", row["id"]),
            )
            updated += int(cursor.rowcount or 0)
        connection.commit()
    return {"asset_class": "sports", "run_id": run_id, "rows_updated": updated, "unresolved_rows": unresolved, "settlement_issue_counts": dict(issue_counts)}


def _deduped_sports_rows(connection: DatabaseSession, run_id: str, *, settled_only: bool = False) -> list[DatabaseRow]:
    state_filter = "AND settlement_state IN ('settled', 'push', 'void')" if settled_only else ""
    return connection.execute(
        f"""
        SELECT * FROM (
          SELECT sports_prediction_logs.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY strategy, sport, league, event_id, market_type, selection, COALESCE(line, -999999)
                   ORDER BY prediction_timestamp ASC, id ASC
                 ) AS dedupe_rank
          FROM app.sports_prediction_logs
          WHERE run_id = %s AND validation_status = 'valid' {state_filter}
          )
        WHERE dedupe_rank = 1
        """,
        (run_id,),
    ).fetchall()


def build_sports_report(*, run_id: str) -> dict[str, Any]:
    with _connect() as connection:
        total_raw = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_logs WHERE run_id = %s AND validation_status = 'valid'",
            (run_id,),
        ).fetchone()[0]
        rejected = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_rejections WHERE run_id = %s",
            (run_id,),
        ).fetchone()[0]
        rejection_reasons = {
            str(row["rejection_reason"]): int(row["count"])
            for row in connection.execute(
                "SELECT rejection_reason, COUNT(*) AS count FROM app.sports_prediction_rejections WHERE run_id = %s GROUP BY rejection_reason",
                (run_id,),
            ).fetchall()
        }
        settled_raw = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_logs WHERE run_id = %s AND settlement_state IN ('settled', 'push', 'void')",
            (run_id,),
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_logs WHERE run_id = %s AND settlement_state = 'unresolved'",
            (run_id,),
        ).fetchone()[0]
        push_count = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_logs WHERE run_id = %s AND settlement_state = 'push'",
            (run_id,),
        ).fetchone()[0]
        void_count = connection.execute(
            "SELECT COUNT(*) FROM app.sports_prediction_logs WHERE run_id = %s AND settlement_state = 'void'",
            (run_id,),
        ).fetchone()[0]
        unique_exposures = len(_deduped_sports_rows(connection, run_id))
        settled_deduped_rows = _deduped_sports_rows(connection, run_id, settled_only=True)
        settled_deduped = len(settled_deduped_rows)
        wins = sum(1 for row in settled_deduped_rows if row["actual_outcome"] == "win")
        losses = sum(1 for row in settled_deduped_rows if row["actual_outcome"] == "loss")
        repeated_snapshot_groups = connection.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT strategy, sport, league, event_id, market_type, selection, line, bookmaker, COUNT(*) AS row_count
              FROM app.sports_prediction_logs
              WHERE run_id = %s AND validation_status = 'valid'
              GROUP BY strategy, sport, league, event_id, market_type, selection, line, bookmaker
              HAVING COUNT(*) > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
        duplicate_exposures = connection.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT strategy, sport, league, event_id, market_type, selection, line, COUNT(*) AS row_count
              FROM app.sports_prediction_logs
              WHERE run_id = %s AND validation_status = 'valid'
              GROUP BY strategy, sport, league, event_id, market_type, selection, line
              HAVING COUNT(*) > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
        avg_odds = connection.execute(
            "SELECT AVG(odds) FROM app.sports_prediction_logs WHERE run_id = %s AND settlement_state IN ('settled', 'push')",
            (run_id,),
        ).fetchone()[0]
        by_market = [dict(row) for row in connection.execute(
            """
            SELECT market_type, COUNT(*) AS rows
            FROM app.sports_prediction_logs
            WHERE run_id = %s AND validation_status = 'valid'
            GROUP BY market_type
            ORDER BY market_type
            """,
            (run_id,),
        ).fetchall()]
    denominator = wins + losses
    accuracy = None if denominator == 0 else round(wins / denominator, 6)
    win_rate_status = sports_win_rate_status(settled_deduped, denominator)
    return {
        "asset_class": "sports",
        "run_id": run_id,
        "model_version": SPORTS_MODEL_VERSION,
        "source_mode": SPORTS_SOURCE_MODE,
        "current_source": "espn_scoreboard",
        "source_name": "espn_scoreboard",
        "source_url_count": 0,
        "total_raw_predictions": total_raw,
        "new_valid_predictions": total_raw,
        "rejected_predictions": rejected,
        "rejection_reasons": rejection_reasons,
        "settled_predictions": settled_raw,
        "newly_settled_predictions": None,
        "unresolved_predictions": unresolved,
        "invalid_rows": 0,
        "unique_deduped_exposures": unique_exposures,
        "repeated_snapshot_groups": repeated_snapshot_groups,
        "exact_duplicates_rejected": rejection_reasons.get("exact_duplicate", 0),
        "duplicate_exposure_warnings": duplicate_exposures,
        "settled_raw_rows": settled_raw,
        "settled_deduped_exposures": settled_deduped,
        "push_count": push_count,
        "void_count": void_count,
        "accuracy": accuracy,
        "win_rate": accuracy,
        "win_rate_status": win_rate_status,
        "win_count": wins,
        "loss_count": losses,
        "win_loss_denominator": denominator,
        "average_odds": as_decimal(avg_odds),
        "metric_status": accuracy_status(settled_deduped),
        "roi_status": "ROI unavailable; no explicit fee/vig/slippage model exists",
        "sample_size_status": sample_status(settled_deduped),
        "gate_result": gate_result(settled_deduped),
        "by_market": by_market,
        "source_blocked_count": rejection_reasons.get("source_blocked", 0),
        "parse_failed_count": rejection_reasons.get("parse_failed", 0),
        "low_confidence_event_match_count": rejection_reasons.get("low_confidence_event_match", 0),
        "connector_status": build_connectors_status(),
        "connector_actions": {
            "google_drive_archive": "not_attempted",
            "airtable_status_sync": "not_attempted",
            "slack_alert": "not_attempted",
        },
        "blockers": [],
        "major_issues": [] if settled_deduped >= 100 else ["sample size below basic audit gate"],
        "minor_issues": ["research-only; no edge or profit claim"],
        "next_automatic_action": "continue sports-cycle with scraper-first public sources",
        "validation_ledger_path": str(default_sports_validation_ledger_path(run_id)),
        "validation_ledger_status": "not_recorded_this_command",
        "latest_validation_record": None,
    }


def sports_win_rate_status(settled_deduped: int, win_loss_denominator: int) -> str:
    if settled_deduped == 0:
        return "unavailable / no settled rows"
    if win_loss_denominator == 0:
        return "unavailable / no settled win-loss rows"
    if settled_deduped < 100:
        return f"recorded / sample too small ({settled_deduped}/100 settled de-duped exposures)"
    return "recorded / research-only settled de-duped exposures"


def build_sports_validation_ledger_entry(
    report: dict[str, Any],
    *,
    log_result: dict[str, Any] | None = None,
    settle_result: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settled_deduped = int(report.get("settled_deduped_exposures") or 0)
    win_loss_denominator = int(report.get("win_loss_denominator") or 0)
    return {
        "record_type": "sports_validation_cycle",
        "asset_class": "sports",
        "run_id": report.get("run_id"),
        "recorded_at": utc_now_iso(),
        "source_mode": report.get("source_mode", SPORTS_SOURCE_MODE),
        "source_name": report.get("current_source") or report.get("source_name"),
        "source_url_count": int(report.get("source_url_count") or 0),
        "valid_sports_predictions_total": int(report.get("total_raw_predictions") or 0),
        "valid_sports_predictions_logged_this_cycle": int((log_result or {}).get("logged_predictions") or 0),
        "settled_sports_predictions": int(report.get("settled_predictions") or report.get("settled_raw_rows") or 0),
        "settled_sports_predictions_this_cycle": int((settle_result or {}).get("rows_updated") or 0),
        "deduped_settled_exposures": settled_deduped,
        "unique_deduped_exposures": int(report.get("unique_deduped_exposures") or 0),
        "unresolved_predictions": int(report.get("unresolved_predictions") or 0),
        "rejected_rows": int(report.get("rejected_predictions") or 0),
        "rejection_reasons": report.get("rejection_reasons") or {},
        "push_count": int(report.get("push_count") or 0),
        "void_count": int(report.get("void_count") or 0),
        "win_count": int(report.get("win_count") or 0),
        "loss_count": int(report.get("loss_count") or 0),
        "win_loss_denominator": win_loss_denominator,
        "win_rate": None if settled_deduped == 0 or win_loss_denominator == 0 else report.get("win_rate"),
        "win_rate_status": sports_win_rate_status(settled_deduped, win_loss_denominator),
        "metric_denominator_policy": "only settled de-duped win/loss rows; unresolved, rejected, blocked, scraper-failed, push, and void rows are excluded from win-rate losses",
        "gate_result": report.get("gate_result"),
        "sample_size_status": report.get("sample_size_status"),
        "blockers": report.get("blockers") or [],
        "source_blocked_count": int(report.get("source_blocked_count") or 0),
        "parse_failed_count": int(report.get("parse_failed_count") or 0),
        "low_confidence_event_match_count": int(report.get("low_confidence_event_match_count") or 0),
        "payload_record_count": len((payload or {}).get("records") or []),
        "payload_rejected_record_count": len((payload or {}).get("rejected_records") or []),
        "research_only": True,
        "no_edge_or_profit_claim": True,
    }


def append_sports_validation_ledger(
    report: dict[str, Any],
    *,
    log_result: dict[str, Any] | None = None,
    settle_result: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    ledger_path = Path(path) if path else default_sports_validation_ledger_path(str(report.get("run_id") or "sports"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = build_sports_validation_ledger_entry(
        report,
        log_result=log_result,
        settle_result=settle_result,
        payload=payload,
    )
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=json_default) + "\n")
    entry["ledger_path"] = str(ledger_path)
    return entry


def read_sports_validation_ledger(path: str | Path, *, limit: int = 20) -> list[dict[str, Any]]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def render_sports_report(report: dict[str, Any]) -> str:
    win_rate_display = report.get("win_rate")
    if report.get("settled_deduped_exposures", 0) == 0:
        win_rate_display = "unavailable / no settled rows"
    lines = [
        "Sports Private Research Report",
        f"Asset class: {report['asset_class']}",
        f"Run ID: {report['run_id']}",
        f"Model version: {report['model_version']}",
        f"Sports source mode: {report.get('source_mode', SPORTS_SOURCE_MODE)}",
        f"Current source used: {report.get('current_source', 'espn_scoreboard')}",
        f"Retrieval method: {report.get('retrieval_method') or 'unavailable'}",
        f"Retrieval plan: {report.get('retrieval_plan') or []}",
        f"Freshness state: {report.get('freshness_state') or 'unknown'}",
        f"Source URL count: {report.get('source_url_count', 0)}",
        "",
        f"Total raw predictions: {report['total_raw_predictions']}",
        f"Rejected predictions: {report['rejected_predictions']}",
        f"Rejection reasons: {report['rejection_reasons']}",
        f"Settled raw rows: {report['settled_raw_rows']}",
        f"Settled de-duped exposures: {report['settled_deduped_exposures']}",
        f"Unresolved predictions: {report['unresolved_predictions']}",
        f"Push count: {report['push_count']}",
        f"Void count: {report['void_count']}",
        f"Unique de-duped exposures: {report['unique_deduped_exposures']}",
        f"Repeated snapshot groups: {report['repeated_snapshot_groups']}",
        f"Duplicate exposure warnings: {report['duplicate_exposure_warnings']}",
        "",
        f"Metric status: {report['metric_status']}",
        f"Win rate: {win_rate_display}",
        f"Win rate status: {report.get('win_rate_status', 'unknown')}",
        f"Win/loss denominator: {report.get('win_loss_denominator', 0)}",
        f"Average odds: {report['average_odds']}",
        f"ROI status: {report.get('roi_status', 'ROI unavailable; no explicit fee/vig/slippage model exists')}",
        f"Sample-size status: {report['sample_size_status']}",
        f"Gate result: {report['gate_result']}",
        f"Validation ledger: {report.get('validation_ledger_path', 'not_configured')}",
        f"Validation ledger status: {report.get('validation_ledger_status', 'unknown')}",
        f"Source blocked count: {report.get('source_blocked_count', 0)}",
        f"Parse failed count: {report.get('parse_failed_count', 0)}",
        f"Low-confidence match count: {report.get('low_confidence_event_match_count', 0)}",
        "",
        *connector_status_report_lines(report.get("connector_status", {})),
        f"Connector actions: {report.get('connector_actions', {})}",
        "",
        "By-market rows:",
    ]
    for row in report["by_market"]:
        lines.append(f"- {row['market_type']}: {row['rows']}")
    lines.extend(
        [
            "",
            f"Blockers: {report['blockers']}",
            f"Major issues: {report['major_issues']}",
            f"Minor issues: {report['minor_issues']}",
            f"Next automatic action: {report['next_automatic_action']}",
            "",
            "No profitability, edge, or model reliability claim is made by this report.",
        ]
    )
    return "\n".join(lines)


def write_sports_report(report: dict[str, Any], path: str | Path) -> None:
    write_text(path, render_sports_report(report))


def export_sports_features(*, run_id: str, output: str | Path, labels_output: str | Path | None = None) -> dict[str, Any]:
    forbidden = {"actual_outcome", "profit_loss", "profit_loss_cents", "final_score", "home_score", "away_score", "closing_line"}
    feature_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    with _connect() as connection:
        for row in _deduped_sports_rows(connection, run_id, settled_only=True):
            features = _json_object(row["features_json"])
            if forbidden.intersection(features):
                raise ValueError("sports feature export contains leakage fields")
            de_dupe_key = stable_json(
                {
                    "asset_class": "sports",
                    "strategy": row["strategy"],
                    "sport": row["sport"],
                    "league": row["league"],
                    "event_id": row["event_id"],
                    "market_type": row["market_type"],
                    "selection": row["selection"],
                    "line": row["line"],
                }
            )
            feature_rows.append(
                {
                    "asset_class": "sports",
                    "run_id": run_id,
                    "model_version": row["model_version"],
                    "strategy": row["strategy"],
                    "sport": row["sport"],
                    "league": row["league"],
                    "event_id": row["event_id"],
                    "market_type": row["market_type"],
                    "selection": row["selection"],
                    "line": row["line"],
                    "bookmaker": row["bookmaker"],
                    "prediction_timestamp": row["prediction_timestamp"],
                    "odds_timestamp": row["odds_timestamp"],
                    "game_start_time": row["game_start_time"],
                    "api_fetched_at": row["api_fetched_at"],
                    "source_snapshot_hash": row["source_snapshot_hash"],
                    "de_dupe_key": de_dupe_key,
                    "feature_odds": features.get("odds"),
                    "feature_line": features.get("line"),
                    "feature_implied_probability": features.get("implied_probability"),
                }
            )
            label_rows.append(
                {
                    "de_dupe_key": de_dupe_key,
                    "actual_outcome": row["actual_outcome"],
                    "final_score_json": row["final_score_json"],
                }
            )
    fields = [
        "asset_class",
        "run_id",
        "model_version",
        "strategy",
        "sport",
        "league",
        "event_id",
        "market_type",
        "selection",
        "line",
        "bookmaker",
        "prediction_timestamp",
        "odds_timestamp",
        "game_start_time",
        "api_fetched_at",
        "source_snapshot_hash",
        "de_dupe_key",
        "feature_odds",
        "feature_line",
        "feature_implied_probability",
    ]
    write_csv(output, feature_rows, fields)
    if labels_output:
        write_csv(labels_output, label_rows, ["de_dupe_key", "actual_outcome", "final_score_json"])
    return {"feature_rows": len(feature_rows), "label_rows": len(label_rows), "output": str(output), "labels_output": str(labels_output) if labels_output else None}


def sports_cycle(*, run_id: str, output: str | Path | None = None, finals: str | Path | None = None) -> dict[str, Any]:
    payload = collect_sports_payload()
    output_path = Path(output) if output else default_sports_payload_path(run_id)
    write_sports_payload(output_path, payload)
    ledger_context = _start_sports_collection_ledger(payload)
    try:
        log_result = log_sports_predictions(run_id=run_id, payload=payload)
    except Exception as exc:
        ledger_context["ledger"].fail_batch(
            batch_id=ledger_context["batch_id"],
            error_code="sports_prediction_logging_failed",
            error_message=type(exc).__name__,
        )
        raise
    ledger_result = _finish_sports_collection_ledger(ledger_context, payload=payload, log_result=log_result)
    settle_result = {"rows_updated": 0, "unresolved_rows": 0, "settlement_issue_counts": {}}
    if finals:
        settle_result = settle_sports_predictions(run_id=run_id, finals_payload=read_json(finals))
    elif payload.get("finals"):
        settle_result = settle_sports_predictions(run_id=run_id, finals_payload={"events": payload["finals"]})
    # Closing lines are recorded once a game starts and need no settled outcome, so
    # this runs every cycle regardless of whether finals were available. Grading is
    # secondary to collection: by this point the rows are already uploaded, so a
    # grading failure is reported rather than allowed to fail the whole cycle.
    try:
        clv_result = capture_sports_closing_lines(run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - reported as a cycle detail, not a collection failure
        clv_result = {
            "asset_class": "sports",
            "run_id": run_id,
            "markets_closed": 0,
            "rows_updated": 0,
            "rows_unchanged": 0,
            "error_code": f"{type(exc).__name__}:{str(exc)[:120]}",
        }
    report = build_sports_report(run_id=run_id)
    report["closing_line_capture"] = clv_result
    report["source_mode"] = payload.get("source_mode") or SPORTS_SOURCE_MODE
    report["current_source"] = payload.get("source") or "espn_scoreboard"
    report["source_name"] = report["current_source"]
    report["source_url_count"] = len(payload.get("source_urls") or [])
    report["retrieval_method"] = payload.get("retrieval_method")
    report["retrieval_plan"] = payload.get("retrieval_plan") or []
    report["freshness_state"] = payload.get("freshness_state")
    # The worker reports source freshness into ops.worker_status from the report,
    # but build_sports_report only knows what is already in the database and never
    # set these. Carry them from the collection that just happened, so sports shows
    # freshness alongside every other collector instead of logging null.
    report["generated_at"] = payload.get("generated_at")
    report["latest_source_fetched_at"] = max(
        (
            str(row.get("api_fetched_at") or row.get("odds_timestamp") or "")
            for row in payload.get("records") or []
        ),
        default="",
    ) or payload.get("generated_at")
    report["collection_ledger"] = ledger_result
    error_reasons = Counter(str(error.get("reason") or "source_blocked") for error in payload.get("errors") or [])
    report["source_blocked_count"] += error_reasons.get("source_blocked", 0)
    report["parse_failed_count"] += error_reasons.get("parse_failed", 0)
    report["low_confidence_event_match_count"] += sum(
        1 for row in payload.get("rejected_records") or [] if row.get("rejection_reason") == "low_confidence_event_match"
    )
    if payload.get("blocker"):
        report["blockers"].append(payload["blocker"])
        report["next_automatic_action"] = "retry sports-cycle later or switch public source if ESPN exposes no odds"
    daily_report_path = default_sports_daily_report_path(run_id)
    all_report_path = default_sports_all_report_path(run_id)
    validation_ledger_path = default_sports_validation_ledger_path(run_id)
    report["validation_ledger_path"] = str(validation_ledger_path)
    report = apply_post_report_connectors(
        report,
        report_paths=[daily_report_path, all_report_path],
        bot_name="sports",
        asset_class="sports",
        run_id=run_id,
        stage="Stage 3A",
        mode="private_research",
    )
    validation_record = append_sports_validation_ledger(
        report,
        log_result=log_result,
        settle_result=settle_result,
        payload=payload,
        path=validation_ledger_path,
    )
    report["validation_ledger_status"] = "recorded"
    report["latest_validation_record"] = validation_record
    write_sports_report(report, daily_report_path)
    write_sports_report(report, all_report_path)
    return {
        "payload_path": str(output_path),
        "log_result": log_result,
        "settle_result": settle_result,
        "clv_result": clv_result,
        "collection_ledger": ledger_result,
        "report": report,
    }
