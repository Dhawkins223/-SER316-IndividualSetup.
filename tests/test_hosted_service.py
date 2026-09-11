from __future__ import annotations

import argparse
import os
import unittest
from unittest import mock

from kalshi_research_bot import cli


class HostedWebRefreshTests(unittest.TestCase):
    """The repository's start command must mean what the deployed one means.

    Production runs the dashboard with a 300-second self-refresh. The hosted
    entry point used to hardcode `refresh_seconds=0`, so adopting the
    repository's `service-start` command would have quietly stopped the
    refreshes that `/readyz` reports `fresh_data_ready` from.
    """

    def _refresh(self, value: str | None) -> int:
        env = {k: v for k, v in os.environ.items() if k != "DASHBOARD_REFRESH_SECONDS"}
        if value is not None:
            env["DASHBOARD_REFRESH_SECONDS"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return cli.hosted_web_refresh_seconds()

    def test_unset_keeps_refreshing(self) -> None:
        self.assertEqual(self._refresh(None), cli.HOSTED_WEB_REFRESH_SECONDS)
        self.assertGreater(cli.HOSTED_WEB_REFRESH_SECONDS, 0)

    def test_blank_is_treated_as_unset(self) -> None:
        self.assertEqual(self._refresh("   "), cli.HOSTED_WEB_REFRESH_SECONDS)

    def test_explicit_cadence_is_honoured(self) -> None:
        self.assertEqual(self._refresh("600"), 600)

    def test_zero_disables_refreshing(self) -> None:
        """A deployment whose data comes only from collector workers may opt out."""

        self.assertEqual(self._refresh("0"), 0)

    def test_negative_is_clamped_not_rejected(self) -> None:
        self.assertEqual(self._refresh("-5"), 0)

    def test_unparseable_falls_back_rather_than_crashing(self) -> None:
        self.assertEqual(self._refresh("every-5-minutes"), cli.HOSTED_WEB_REFRESH_SECONDS)

    def test_web_role_starts_the_server_with_that_cadence(self) -> None:
        env = {
            "HAWKNETIC_SERVICE": "web",
            "PORT": "8080",
            "DASHBOARD_REFRESH_SECONDS": "300",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "run_server") as started:
                exit_code = cli.run_hosted_service(argparse.Namespace())

        self.assertEqual(exit_code, 0)
        started.assert_called_once_with(host="0.0.0.0", port=8080, refresh_seconds=300)


class HostedServiceModeTests(unittest.TestCase):
    """A slow worker must be deployable as a scheduled job, not only a loop.

    A worker on a 6-hour cadence spends 21,600 seconds asleep to do a few
    seconds of work, and Railway meters the memory it holds for every one of
    those seconds. `HAWKNETIC_SERVICE_MODE=once` runs a single cycle and exits
    so the same worker can be a scale-to-zero cron service. The always-on
    behaviour stays the default so an existing service is unaffected by the
    variable simply existing.
    """

    def _mode(self, value: str | None) -> str:
        env = {k: v for k, v in os.environ.items() if k != "HAWKNETIC_SERVICE_MODE"}
        if value is not None:
            env["HAWKNETIC_SERVICE_MODE"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return cli.hosted_service_mode()

    def test_unset_stays_always_on(self) -> None:
        self.assertEqual(self._mode(None), "loop")

    def test_blank_stays_always_on(self) -> None:
        self.assertEqual(self._mode("  "), "loop")

    def test_once_is_recognised(self) -> None:
        self.assertEqual(self._mode("once"), "once")

    def test_mode_is_case_insensitive_and_trimmed(self) -> None:
        self.assertEqual(self._mode(" ONCE "), "once")

    def test_unknown_mode_falls_back_to_the_loop(self) -> None:
        """A typo in a Railway variable must not stop a collector."""

        self.assertEqual(self._mode("cron"), "loop")

    def _run_worker_role(self, mode: str | None) -> argparse.Namespace:
        env = {"HAWKNETIC_SERVICE": "sports-research", "PORT": "8080"}
        if mode is not None:
            env["HAWKNETIC_SERVICE_MODE"] = mode
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "start_worker_health_server") as health:
                with mock.patch.object(cli, "run_worker_command", return_value=0) as command:
                    exit_code = cli.run_hosted_service(argparse.Namespace())
        self.assertEqual(exit_code, 0)
        self.health_server = health
        return command.call_args.args[0]

    def test_default_worker_role_loops_and_serves_health(self) -> None:
        worker_args = self._run_worker_role(None)
        self.assertFalse(worker_args.once)
        self.assertEqual(worker_args.service, "sports-research")
        self.health_server.assert_called_once_with("sports-research", 8080)

    def test_once_mode_runs_a_single_cycle(self) -> None:
        worker_args = self._run_worker_role("once")
        self.assertTrue(worker_args.once)
        self.assertEqual(worker_args.service, "sports-research")

    def test_once_mode_binds_no_health_port(self) -> None:
        """Two scheduled runs must not collide on a port neither of them needs."""

        self._run_worker_role("once")
        self.health_server.assert_not_called()

    def test_unknown_service_is_still_rejected_in_once_mode(self) -> None:
        env = {"HAWKNETIC_SERVICE": "not-a-worker", "HAWKNETIC_SERVICE_MODE": "once"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cli.run_hosted_service(argparse.Namespace()), 2)


if __name__ == "__main__":
    unittest.main()
