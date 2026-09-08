from __future__ import annotations


class ResearchBot:
    def model_plan_for_sport(self, sport: str) -> dict[str, object]:
        sport_key = sport.lower()
        if sport_key in {"soccer", "football-soccer"}:
            return {
                "primary": "poisson_goal_model",
                "secondary": ["market_prior_blend"],
                "features": ["expected_goals", "injuries", "rest", "travel", "lineups"],
            }
        if sport_key in {"mlb", "baseball"}:
            return {
                "primary": "elo_rating_model",
                "secondary": ["pitcher_adjustments", "market_prior_blend"],
                "features": ["starting_pitcher", "bullpen", "park", "weather", "lineups"],
            }
        if sport_key in {"nba", "wnba", "nfl", "football", "tennis", "golf"}:
            return {
                "primary": "elo_rating_model",
                "secondary": ["market_prior_blend"],
                "features": ["injuries", "rest", "travel", "form", "matchup"],
            }
        return {
            "primary": "market_prior_model",
            "secondary": [],
            "features": ["market_price", "news", "availability"],
        }
