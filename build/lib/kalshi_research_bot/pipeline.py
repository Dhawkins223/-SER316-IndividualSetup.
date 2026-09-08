from __future__ import annotations

from .contracts import Game, MarketQuote
from .agents import EdgeBot, ModelBot


class ResearchPipeline:
    def __init__(self) -> None:
        self.model_bot = ModelBot()
        self.edge_bot = EdgeBot()

    def run(self, games: list[Game], quotes: list[MarketQuote], min_edge_cents: float = 0.0):
        predictions = {game.game_id: self.model_bot.predict_home_win(game) for game in games}
        edges = []
        for quote in quotes:
            prediction = predictions.get(quote.game_id)
            if prediction:
                edges.extend(self.edge_bot.evaluate(quote, prediction, min_edge_cents=min_edge_cents))
        return sorted(edges, key=lambda edge: edge.expected_value_cents, reverse=True)
