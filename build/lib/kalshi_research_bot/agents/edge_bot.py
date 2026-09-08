from __future__ import annotations

from ..contracts import EdgeResult, MarketQuote, ModelPrediction
from ..math import buy_no_ev_cents, buy_yes_ev_cents


class EdgeBot:
    def evaluate(
        self,
        quote: MarketQuote,
        prediction: ModelPrediction,
        fee_cents: float = 0.0,
        min_edge_cents: float = 0.0,
    ) -> list[EdgeResult]:
        results: list[EdgeResult] = []
        if quote.yes_ask is not None:
            yes_ev = buy_yes_ev_cents(prediction.probability, quote.yes_ask, fee_cents)
            if yes_ev >= min_edge_cents:
                results.append(
                    EdgeResult(
                        ticker=quote.ticker,
                        game_id=quote.game_id,
                        side="YES",
                        model_probability=prediction.probability,
                        entry_price_cents=quote.yes_ask,
                        fair_price_cents=prediction.fair_price_cents,
                        expected_value_cents=yes_ev,
                        title=quote.title,
                        notes=prediction.notes,
                    )
                )
        if quote.no_ask is not None:
            no_ev = buy_no_ev_cents(prediction.probability, quote.no_ask, fee_cents)
            if no_ev >= min_edge_cents:
                results.append(
                    EdgeResult(
                        ticker=quote.ticker,
                        game_id=quote.game_id,
                        side="NO",
                        model_probability=1.0 - prediction.probability,
                        entry_price_cents=quote.no_ask,
                        fair_price_cents=round(100.0 - prediction.fair_price_cents, 2),
                        expected_value_cents=no_ev,
                        title=quote.title,
                        notes=prediction.notes,
                    )
                )
        return sorted(results, key=lambda result: result.expected_value_cents, reverse=True)
