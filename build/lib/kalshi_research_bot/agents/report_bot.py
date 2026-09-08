from __future__ import annotations

from ..contracts import ComboResult, EdgeResult


class ReportBot:
    def render_edges(self, edges: list[EdgeResult]) -> str:
        if not edges:
            return "No positive edges found with the current inputs."
        lines = ["Kalshi Research Results", ""]
        for rank, edge in enumerate(edges, start=1):
            lines.extend(
                [
                    f"{rank}. {edge.side} {edge.ticker}",
                    f"   Game: {edge.game_id}",
                    f"   Title: {edge.title}",
                    f"   Model probability: {edge.model_probability:.2%}",
                    f"   Entry: {edge.entry_price_cents:.2f}c | Fair: {edge.fair_price_cents:.2f}c",
                    f"   EV: {edge.expected_value_cents:.2f}c per contract",
                    f"   Notes: {', '.join(edge.notes)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def render_combos(self, combos: list[ComboResult]) -> str:
        if not combos:
            return "No combos met the target probability with the current inputs."
        lines = ["Kalshi Combo Results", ""]
        for rank, combo in enumerate(combos, start=1):
            lines.extend(
                [
                    f"{rank}. {combo.combo_id}",
                    f"   Adjusted probability: {combo.adjusted_probability:.2%}",
                    f"   Independent probability: {combo.raw_probability:.2%}",
                    f"   Correlation adjustment: {combo.correlation_adjustment:+.2%}",
                    f"   Cost as a unit: {combo.synthetic_cost_cents:.2f}c | Fair: {combo.fair_price_cents:.2f}c",
                    f"   EV: {combo.expected_value_cents:.2f}c per contract",
                ]
            )
            for leg in combo.legs:
                lines.append(
                    f"   - {leg.selection.upper()} {leg.line:g}: {leg.event_name} "
                    f"({leg.model_probability:.2%}, entry {leg.entry_price_cents:.2f}c)"
                )
            lines.append("")
        return "\n".join(lines).rstrip()
