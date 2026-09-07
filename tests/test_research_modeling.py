from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from kalshi_research_bot.kalshi_ingestion import persist_kalshi_snapshot
from kalshi_research_bot.research_modeling import refresh_market_consensus_baseline
from tests.postgres_support import PostgresTestCase


def _payload() -> dict:
    return {
        "date": "20260726",
        "generated_at": "2026-07-26T05:00:00+00:00",
        "games": [],
        "markets": [
            {
                "ticker": "KXMVE-BASELINE-1",
                "event_ticker": "KXMVE-BASELINE-EVENT",
                "mve_collection_ticker": "KXMVE-BASELINE",
                "title": "Baseline fixture",
                "legs_text": "Fixture outcome",
                "status": "active",
                "api_fetched_at": "2026-07-26T04:59:59+00:00",
                "source_updated_at": "2026-07-26T04:59:58+00:00",
                "source_snapshot_hash": "sha256:baseline",
                "source": "kalshi",
                "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets",
                "close_time": "2026-07-26T18:00:00+00:00",
                "expected_expiration_time": "2026-07-26T20:00:00+00:00",
                "yes_bid_cents": "79.12500000",
                "yes_ask_cents": "80.12500000",
                "no_bid_cents": "19.87500000",
                "no_ask_cents": "20.87500000",
                "volume_24h": "12",
                "liquidity_dollars": "100.5",
            }
        ],
    }


class ResearchModelingTests(PostgresTestCase):
    def _ingest(self, payload=None, key="baseline:one"):
        return persist_kalshi_snapshot(
            payload or _payload(),
            worker_name="kalshi-market-ingestion",
            idempotency_key=key,
            settings=self.settings,
        )

    def test_refresh_populates_normalized_research_lineage_without_claiming_edge(self):
        self._ingest()

        result = refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T05:00:00+00:00",
        )

        self.assertTrue(result["created"])
        self.assertEqual(result["predictions_written"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.model_versions")["count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.feature_snapshots")["count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.prediction_runs")["count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.predictions")["count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.metric_results")["count"], 1)
        prediction = self.query_one(
            """
            SELECT predicted_yes_probability, market_implied_probability,
                   calculated_edge, decision_status, rejection_reason
            FROM research.predictions
            """
        )
        self.assertEqual(str(prediction["predicted_yes_probability"]), "0.79625000")
        self.assertEqual(prediction["predicted_yes_probability"], prediction["market_implied_probability"])
        self.assertEqual(str(prediction["calculated_edge"]), "0E-8")
        self.assertEqual(prediction["decision_status"], "no_edge")
        self.assertEqual(prediction["rejection_reason"], "market_consensus_baseline_only")

    def test_same_dataset_is_idempotent(self):
        self._ingest()
        first = refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T05:00:00+00:00",
        )
        second = refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T05:00:01+00:00",
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["no_material_change"])
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.prediction_runs")["count"], 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.predictions")["count"], 1)

    def test_new_observation_creates_a_new_versioned_run(self):
        self._ingest()
        refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T05:00:00+00:00",
        )
        changed = deepcopy(_payload())
        changed["generated_at"] = "2026-07-26T05:05:00+00:00"
        changed["markets"][0]["api_fetched_at"] = "2026-07-26T05:04:59+00:00"
        changed["markets"][0]["yes_bid_cents"] = "80"
        changed["markets"][0]["yes_ask_cents"] = "82"
        self._ingest(changed, key="baseline:two")

        result = refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T05:05:00+00:00",
        )

        self.assertTrue(result["created"])
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.prediction_runs")["count"], 2)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.predictions")["count"], 2)

    def test_concurrent_refreshes_share_one_completed_dataset_run(self):
        self._ingest()
        # Exercise the common case where the model version already exists,
        # so its unique insert cannot accidentally serialize the first run.
        refresh_market_consensus_baseline(
            run_id="initial", settings=self.settings,
            as_of_time="2026-07-26T05:00:00+00:00",
        )
        changed = deepcopy(_payload())
        changed["generated_at"] = "2026-07-26T05:05:00+00:00"
        changed["markets"][0]["api_fetched_at"] = "2026-07-26T05:04:59+00:00"
        changed["markets"][0]["yes_bid_cents"] = "80"
        changed["markets"][0]["yes_ask_cents"] = "82"
        self._ingest(changed, key="baseline:concurrent")
        start = Barrier(4)

        def refresh(index):
            start.wait(timeout=10)
            return refresh_market_consensus_baseline(
                run_id=f"concurrent-{index}", settings=self.settings,
                as_of_time="2026-07-26T05:05:00+00:00",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(refresh, range(4)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["prediction_run_id"] for result in results}), 1)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.prediction_runs")["count"], 2)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.predictions")["count"], 2)

    def test_stale_observations_are_not_represented_as_current(self):
        self._ingest()

        result = refresh_market_consensus_baseline(
            run_id="research",
            settings=self.settings,
            as_of_time="2026-07-26T06:00:00+00:00",
            maximum_age_seconds=60,
        )

        self.assertFalse(result["created"])
        self.assertEqual(result["records_processed"], 0)
        self.assertIn(result["source_freshness_state"], {"fresh", "stale"})
        self.assertEqual(self.query_one("SELECT COUNT(*) AS count FROM research.predictions")["count"], 0)
