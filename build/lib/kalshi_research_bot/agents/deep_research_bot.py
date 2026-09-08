from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any


class DeepResearchBot:
    def build_summary(
        self,
        markets: list[dict[str, Any]],
        primary_slip: dict[str, Any],
        leverage_slip: dict[str, Any],
    ) -> dict[str, Any]:
        leg_stats = self._leg_stats(markets)
        return {
            "status": "ACTIVE",
            "last_researched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mission": "Keep improving leg selection accuracy while separating safer 80% legs from higher-leverage 75% legs.",
            "market_scan": leg_stats,
            "slip_tiers": [
                self._slip_tier("Primary 80%", primary_slip),
                self._slip_tier("Leverage 75%", leverage_slip),
            ],
            "research_queue": self._research_queue(leg_stats),
            "accuracy_rules": [
                "Track 80%+ and 75%+ legs separately; do not mix them without labeling the risk tier.",
                "Prefer liquid legs with tighter bid/ask spreads before adding extra payout legs.",
                "Cap repeated games because same-game legs can be correlated even when the math looks independent.",
                "Block overlapping same-matchup legs so a combo never stacks total/spread/half-game variants from one game.",
                "Treat Kalshi prices as market-implied probabilities, not guaranteed true hit rates.",
                "Use manual confirmation for any real-money action until bankroll rules and auth guards exist.",
            ],
        }

    def _leg_stats(self, markets: list[dict[str, Any]]) -> dict[str, Any]:
        total = 0
        priced = 0
        active = 0
        tight_spread = 0
        probability_buckets = Counter()
        sport_counts = Counter()
        for market in markets:
            for leg in market.get("leg_details") or []:
                total += 1
                if leg.get("status") == "active":
                    active += 1
                probability = leg.get("market_implied_probability")
                if probability is not None:
                    priced += 1
                    probability_buckets[self._bucket(probability)] += 1
                bid = leg.get("bid_cents")
                ask = leg.get("ask_cents")
                if bid is not None and ask is not None and float(ask) - float(bid) <= 25:
                    tight_spread += 1
                sport_counts[self._infer_sport(leg)] += 1
        return {
            "combo_markets": len(markets),
            "total_legs_seen": total,
            "active_legs": active,
            "priced_legs": priced,
            "tight_spread_legs": tight_spread,
            "probability_buckets": dict(sorted(probability_buckets.items())),
            "sports_seen": dict(sorted(sport_counts.items())),
        }

    def _slip_tier(self, name: str, slip: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "action": slip.get("action", "UNKNOWN"),
            "min_leg_probability": slip.get("min_leg_probability"),
            "leg_count": slip.get("leg_count", 0),
            "full_slip_probability": slip.get("adjusted_probability"),
            "estimated_payout_if_right": slip.get("estimated_payout_if_right"),
            "overlap_safe": slip.get("overlap_safe"),
            "skipped_overlap_count": slip.get("skipped_overlap_count", 0),
            "sports": slip.get("sports", []),
        }

    def _research_queue(self, leg_stats: dict[str, Any]) -> list[dict[str, str]]:
        sports_seen = set(leg_stats.get("sports_seen", {}))
        queue = [
            {
                "priority": "High",
                "topic": "Line movement and liquidity filter",
                "why": "Avoid stale or wide-spread legs that look safe only because the market is thin.",
                "next_step": "Record bid/ask movement every refresh and penalize legs whose spread widens.",
            },
            {
                "priority": "High",
                "topic": "Correlation control",
                "why": "Huge combo slips can accidentally stack the same hidden risk across one game or sport.",
                "next_step": "Score repeated teams, same event, same sport, and same market type before final slip build.",
            },
            {
                "priority": "High",
                "topic": "Exact-bet de-duplication",
                "why": "Totals, spreads, half-game, and winner props from the same matchup can overlap in one combo.",
                "next_step": "Keep one normalized matchup key per slip and log skipped overlap candidates.",
            },
            {
                "priority": "Medium",
                "topic": "Calibration tracking",
                "why": "An 80% market-implied leg should be checked against actual outcomes over time.",
                "next_step": "Save every suggested leg and later compare it with settled results.",
            },
        ]
        if "Pro Baseball" in sports_seen:
            queue.append(
                {
                    "priority": "Medium",
                    "topic": "MLB total-runs model",
                    "why": "Pitching, bullpen, park, weather, and lineup changes drive run totals.",
                    "next_step": "Add pitcher/bullpen/weather inputs before trusting 75% leverage baseball legs.",
                }
            )
        if "World Soccer Cup" in sports_seen:
            queue.append(
                {
                    "priority": "Medium",
                    "topic": "Soccer goal model",
                    "why": "Low totals depend heavily on lineup strength, knockout incentives, and game state.",
                    "next_step": "Blend market totals with Poisson goal baselines and lineup/news checks.",
                }
            )
        if "Tennis" in sports_seen:
            queue.append(
                {
                    "priority": "Medium",
                    "topic": "Tennis surface/form model",
                    "why": "Match winner prices move fast with fatigue, surface, and serve/return strength.",
                    "next_step": "Track surface-specific Elo and recent match load before adding more tennis legs.",
                }
            )
        return queue

    def _bucket(self, probability: float) -> str:
        if probability >= 0.90:
            return "90%+"
        if probability >= 0.80:
            return "80-89%"
        if probability >= 0.75:
            return "75-79%"
        if probability >= 0.60:
            return "60-74%"
        return "under 60%"

    def _infer_sport(self, leg: dict[str, Any]) -> str:
        ticker = (leg.get("market_ticker") or "").upper()
        text = f"{leg.get('title', '')} {leg.get('subtitle', '')}".lower()
        if "KXMLB" in ticker or "runs scored" in text:
            return "Pro Baseball"
        if "KXWNBA" in ticker or "points scored" in text or "women's" in text:
            return "Pro Basketball (W)"
        if "KXWC" in ticker or "goals scored" in text or "reg time" in text:
            return "World Soccer Cup"
        if "KXATP" in ticker or "KXWTA" in ticker or "MATCH" in ticker:
            return "Tennis"
        return "Sports"
