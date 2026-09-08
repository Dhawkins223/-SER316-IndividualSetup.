from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class PublicIntelBot:
    def load_signals(self, path: str | Path | None) -> list[dict[str, Any]]:
        if not path:
            return []
        signal_path = Path(path)
        if not signal_path.exists():
            return []
        payload = json.loads(signal_path.read_text(encoding="utf-8"))
        return payload.get("signals", []) if isinstance(payload, dict) else []

    def build_summary(
        self,
        markets: list[dict[str, Any]],
        signals: list[dict[str, Any]] | None = None,
        primary_slip: dict[str, Any] | None = None,
        leverage_slip: dict[str, Any] | None = None,
        overlap_key_fn: Callable[[dict[str, Any]], str] | None = None,
    ) -> dict[str, Any]:
        signals = signals or []
        scored_signals = [self._score_signal(signal) for signal in signals]
        allowed_signals = [signal for signal in scored_signals if signal["allowed"]]
        blocked_signals = [signal for signal in scored_signals if not signal["allowed"]]
        leg_matches = self._match_signals(markets, allowed_signals, overlap_key_fn)
        intel_by_overlap_key = self._intel_by_overlap_key(leg_matches)
        return {
            "status": "ACTIVE" if signals else "READY_FOR_SOURCES",
            "last_updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "strategy": "Public-only trader/bettor intelligence scored by timestamped track record, consensus, and market match.",
            "signals_loaded": len(signals),
            "trusted_signal_count": len(allowed_signals),
            "blocked_signal_count": len(blocked_signals),
            "connector_plan": self._connector_plan(),
            "signal_weights": self._signal_weights(),
            "top_sources": self._top_sources(allowed_signals),
            "top_matches": leg_matches[:12],
            "intel_by_overlap_key": intel_by_overlap_key,
            "slip_impact": self._slip_impact(primary_slip or {}, leverage_slip or {}),
            "guardrails": self._guardrails(),
            "blocked_reasons": self._blocked_reasons(blocked_signals),
        }

    def _score_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        historical_wins = self._number(signal.get("historical_wins") or signal.get("wins"))
        historical_total = self._number(signal.get("historical_total") or signal.get("sample_size"))
        hit_rate = historical_wins / historical_total if historical_total > 0 else 0.5
        sample_multiplier = min(1.0, math.sqrt(historical_total) / 10.0) if historical_total > 0 else 0.15
        confidence = self._clamp(self._number(signal.get("confidence"), 0.5), 0.0, 1.0)
        roi = self._clamp(self._number(signal.get("roi_percent")) / 100.0, -0.5, 0.5)
        roi_score = (roi + 0.5) / 1.0
        is_public = bool(signal.get("is_public", True))
        has_url = bool(signal.get("url") or signal.get("source_url"))
        allowed = is_public and has_url and not signal.get("contains_private_info", False)
        score = (
            hit_rate * 0.45
            + confidence * 0.25
            + roi_score * 0.15
            + sample_multiplier * 0.15
        ) * 100.0
        if not allowed:
            score = 0.0
        enriched = dict(signal)
        enriched.update(
            {
                "source": signal.get("source", "unknown"),
                "platform": signal.get("platform", "unknown"),
                "score": round(score, 2),
                "hit_rate": round(hit_rate, 4),
                "sample_multiplier": round(sample_multiplier, 4),
                "allowed": allowed,
                "blocked_reason": self._blocked_reason(signal, is_public, has_url),
            }
        )
        return enriched

    def _match_signals(
        self,
        markets: list[dict[str, Any]],
        scored_signals: list[dict[str, Any]],
        overlap_key_fn: Callable[[dict[str, Any]], str] | None,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for market in markets:
            for leg in market.get("leg_details") or []:
                leg_text = self._leg_text(leg)
                overlap_key = overlap_key_fn(leg) if overlap_key_fn else leg.get("event_ticker", "")
                for signal in scored_signals:
                    match_score = self._match_score(signal, leg_text)
                    if match_score <= 0:
                        continue
                    intel_score = round(min(8.0, (signal["score"] / 100.0) * match_score * 8.0), 2)
                    matches.append(
                        {
                            "source": signal.get("source", "unknown"),
                            "platform": signal.get("platform", "unknown"),
                            "url": signal.get("url") or signal.get("source_url", ""),
                            "market_hint": signal.get("market_hint", ""),
                            "selection_hint": signal.get("selection_hint", ""),
                            "overlap_key": overlap_key,
                            "event": leg.get("display_event") or leg.get("event_ticker", ""),
                            "leg": leg.get("subtitle") or leg.get("title") or leg.get("market_ticker", ""),
                            "source_score": signal["score"],
                            "match_score": round(match_score, 2),
                            "intel_score": intel_score,
                        }
                    )
        return sorted(matches, key=lambda item: (item["intel_score"], item["source_score"]), reverse=True)

    def _intel_by_overlap_key(self, matches: list[dict[str, Any]]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for match in matches:
            key = match.get("overlap_key", "")
            if key:
                scores[key] = min(8.0, scores.get(key, 0.0) + float(match.get("intel_score", 0.0)))
        return {key: round(value, 2) for key, value in scores.items()}

    def _match_score(self, signal: dict[str, Any], leg_text: str) -> float:
        market_tokens = self._tokens(signal.get("market_hint", ""))
        selection_tokens = self._tokens(signal.get("selection_hint", ""))
        if not market_tokens and not selection_tokens:
            return 0.0
        market_hits = self._token_hit_ratio(market_tokens, leg_text) if market_tokens else 0.0
        selection_hits = self._token_hit_ratio(selection_tokens, leg_text) if selection_tokens else 0.0
        if market_tokens and market_hits < 0.45:
            return 0.0
        if selection_tokens and selection_hits < 0.45:
            return 0.0
        return self._clamp((market_hits * 0.55) + (selection_hits * 0.45), 0.0, 1.0)

    def _top_sources(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sources: dict[str, dict[str, Any]] = {}
        for signal in signals:
            key = f"{signal.get('platform', 'unknown')}:{signal.get('source', 'unknown')}"
            entry = sources.setdefault(
                key,
                {
                    "source": signal.get("source", "unknown"),
                    "platform": signal.get("platform", "unknown"),
                    "signal_count": 0,
                    "average_score": 0.0,
                },
            )
            entry["signal_count"] += 1
            entry["average_score"] += signal.get("score", 0.0)
        for entry in sources.values():
            entry["average_score"] = round(entry["average_score"] / max(1, entry["signal_count"]), 2)
        return sorted(sources.values(), key=lambda item: item["average_score"], reverse=True)[:10]

    def _slip_impact(self, primary_slip: dict[str, Any], leverage_slip: dict[str, Any]) -> dict[str, Any]:
        return {
            "primary_intel_boosted_legs": sum(1 for leg in primary_slip.get("legs", []) if float(leg.get("public_intel_score") or 0) > 0),
            "leverage_intel_boosted_legs": sum(1 for leg in leverage_slip.get("legs", []) if float(leg.get("public_intel_score") or 0) > 0),
            "primary_overlap_safe": primary_slip.get("overlap_safe"),
            "leverage_overlap_safe": leverage_slip.get("overlap_safe"),
        }

    def _connector_plan(self) -> list[dict[str, str]]:
        return [
            {"name": "Firecrawl-style Web", "purpose": "Public page scraping for news, injuries, weather, public picks.", "status": "ready_to_connect"},
            {"name": "PostgreSQL/Airtable", "purpose": "Timestamp every source, pick, outcome, ROI, and calibration result.", "status": "postgres_ready"},
            {"name": "Social Sources", "purpose": "Public X/Threads/YouTube/newsletter picks only; no DMs or private groups.", "status": "manual_file_now_api_later"},
            {"name": "Market Data", "purpose": "Kalshi public markets plus future stocks/crypto public feeds.", "status": "kalshi_public_live"},
            {"name": "Dashboard", "purpose": "Show consensus, conflict, source quality, and compliance warnings.", "status": "implemented"},
        ]

    def _signal_weights(self) -> dict[str, str]:
        return {
            "market_price": "45%",
            "liquidity_and_spread": "20%",
            "public_bettor_intel": "15%",
            "news_weather_injury_context": "10%",
            "backtest_calibration": "10%",
        }

    def _guardrails(self) -> list[str]:
        return [
            "Use public URLs or public APIs only.",
            "Ignore signals marked private, leaked, hacked, or nonpublic.",
            "Store timestamps so deleted posts cannot rewrite history.",
            "Do not scrape around logins, paywalls, CAPTCHAs, or blocked robots policies.",
            "Do not auto-place real-money trades; keep manual confirmation.",
        ]

    def _blocked_reasons(self, signals: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "source": str(signal.get("source", "unknown")),
                "platform": str(signal.get("platform", "unknown")),
                "reason": str(signal.get("blocked_reason", "blocked")),
            }
            for signal in signals
        ]

    def _blocked_reason(self, signal: dict[str, Any], is_public: bool, has_url: bool) -> str:
        if signal.get("contains_private_info", False):
            return "contains private or nonpublic information"
        if not is_public:
            return "signal is not marked public"
        if not has_url:
            return "missing public URL"
        return ""

    def _leg_text(self, leg: dict[str, Any]) -> str:
        return self._normalize(
            " ".join(
                str(leg.get(field, ""))
                for field in ["display_event", "event_ticker", "market_ticker", "title", "subtitle", "rules", "side"]
            )
        )

    def _tokens(self, value: str) -> list[str]:
        return [token for token in self._normalize(value).split() if len(token) >= 3]

    def _token_hit_ratio(self, tokens: list[str], haystack: str) -> float:
        if not tokens:
            return 0.0
        hits = sum(1 for token in tokens if token in haystack)
        return hits / len(tokens)

    def _normalize(self, value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())

    def _number(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _clamp(self, value: float, low: float, high: float) -> float:
        return max(low, min(high, value))
