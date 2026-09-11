"""The dashboard's client-side freshness check, tested by running it.

`pollLiveDataFreshness` decides whether to tell a reader that the data in front
of them has been withheld. It used to decide that from `data_age_seconds`
alone:

    Number(freshness.data_age_seconds || 0) <= LIVE_DATA_STALE_SECONDS

which reads an absent age as an age of zero. Four of the gate's five blocked
outcomes carry no usable age -- a missing timestamp, an unparseable one, one in
the future, and a stale source fallback -- so in every case except ordinary
staleness the reader sat on a page that looked live. That is the outcome the
branch exists to prevent, and it was the only one it did not prevent.

These tests execute the real function out of `app.js` under node rather than
asserting on its source text, because a string check passes whatever the
function actually does. The extraction is checked separately, in pure Python,
so renaming or deleting the function fails the suite even where node is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

from kalshi_research_bot.dashboard_assets import SCRIPT

SOURCE = SCRIPT.body.decode("utf-8")
FUNCTION = "liveDataIsBlocked"


def extract(name: str) -> str:
    """The named function's source, brace-matched out of app.js."""

    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start : index + 1]
    raise AssertionError(f"{name} has unbalanced braces")


def stale_seconds() -> int:
    match = re.search(r"const LIVE_DATA_STALE_SECONDS = (\d+);", SOURCE)
    assert match, "LIVE_DATA_STALE_SECONDS is gone; the fallback below is untestable"
    return int(match.group(1))


class ExtractionTests(unittest.TestCase):
    """Pure Python, so the guarantee never depends on node being installed."""

    def test_the_poller_delegates_to_the_named_decision(self) -> None:
        self.assertIn(f"function {FUNCTION}(", SOURCE)
        self.assertIn(f"if (!{FUNCTION}(freshness)) return;", SOURCE)

    def test_the_decision_reads_the_gates_verdict(self) -> None:
        """Not the age. The age is the fallback, not the rule."""

        body = extract(FUNCTION)
        self.assertIn("freshness.status", body)
        self.assertIn('!== "ready"', body)


@unittest.skipUnless(shutil.which("node"), "node is required to execute the client decision")
class LiveDataIsBlockedTests(unittest.TestCase):
    def decide(self, freshness: object) -> bool:
        script = (
            f"const LIVE_DATA_STALE_SECONDS = {stale_seconds()};\n"
            f"{extract(FUNCTION)}\n"
            f"process.stdout.write(JSON.stringify({FUNCTION}({json.dumps(freshness)})));"
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30, check=True
        )
        return json.loads(result.stdout)

    def test_every_way_the_gate_blocks_is_reported_to_the_reader(self) -> None:
        """The four shapes `slip_payload_gate` actually produces when it blocks,
        with the age each one carries."""

        for code, age in (
            ("blocked_refresh_failed", None),
            ("blocked_stale_source", None),
            ("blocked_missing_generated_at", None),
            ("blocked_invalid_generated_at", -7200),
            ("blocked_stale_payload", 10800),
        ):
            with self.subTest(code=code, age=age):
                self.assertTrue(
                    self.decide({"status": "blocked", "code": code, "data_age_seconds": age}),
                    f"{code} with data_age_seconds={age!r} left the reader unwarned",
                )

    def test_a_blocked_gate_with_no_age_at_all_still_reports(self) -> None:
        self.assertTrue(self.decide({"status": "blocked", "code": "blocked_stale_source"}))

    def test_a_ready_gate_stays_quiet(self) -> None:
        """Including one whose age is past the stale window: the gate is the
        authority, and it said ready."""

        self.assertFalse(self.decide({"status": "ready", "data_age_seconds": 30}))
        self.assertFalse(self.decide({"status": "ready", "data_age_seconds": stale_seconds() + 1}))

    def test_a_response_that_says_nothing_about_freshness_is_blocked(self) -> None:
        """Silence is not evidence of freshness.

        The first version of this returned `false` for a payload with neither
        field -- and `{"data_age_seconds": null}` is the original defect exactly,
        surviving in the fallback: `Number(null || 0)` is 0, and 0 is not stale.
        """

        # Every shape `Number()` quietly turns into a finite number. Four of
        # these were added after the first version of this test covered only
        # `{}` and a non-numeric string and passed while six shapes still read
        # as fresh -- `Number("")` and `Number([])` are 0, `Number(true)` is 1.
        for payload in (
            {},
            None,
            {"data_age_seconds": None},
            {"data_age_seconds": "soon"},
            {"data_age_seconds": ""},
            {"data_age_seconds": "   "},
            {"data_age_seconds": True},
            {"data_age_seconds": []},
        ):
            with self.subTest(payload=payload):
                self.assertTrue(self.decide(payload), f"{payload!r} left the reader unwarned")

    def test_an_age_below_zero_is_a_future_stamp_not_freshness(self) -> None:
        """`blocked_invalid_generated_at` carries a negative age, and `-7200 >
        300` is false, so a payload stamped in the future read as live."""

        for age in (-1, -300, -7200):
            with self.subTest(age=age):
                self.assertTrue(self.decide({"data_age_seconds": age}))

    def test_a_numeric_string_age_is_still_read(self) -> None:
        """The strictness must not cost the ordinary case: JSON numbers arrive
        as numbers, but a string that holds one is still an age."""

        self.assertTrue(self.decide({"data_age_seconds": str(stale_seconds() + 1)}))
        self.assertFalse(self.decide({"data_age_seconds": str(stale_seconds() - 1)}))

    def test_a_status_still_wins_over_any_age(self) -> None:
        """The gate is the authority; the age is only the fallback."""

        self.assertFalse(self.decide({"status": "ready", "data_age_seconds": -1}))
        self.assertTrue(self.decide({"status": "blocked", "data_age_seconds": 0}))

    def test_a_usable_age_is_still_read_when_there_is_no_status(self) -> None:
        self.assertTrue(self.decide({"data_age_seconds": stale_seconds() + 1}))
        self.assertFalse(self.decide({"data_age_seconds": stale_seconds() - 1}))
        self.assertFalse(self.decide({"data_age_seconds": 0}))


class FreshnessPayloadContractTests(unittest.TestCase):
    """What `/freshness.json` must carry, now that the client reads it.

    The endpoint serves `gate.get("status")`, and the poller above decides from
    it. A gate outcome that reached the client without a status would put the
    poller back on the age fallback -- which is the defect this pair of changes
    removes -- so the shapes are pinned against the real producer.
    """

    def gates(self):
        import copy
        from datetime import datetime, timedelta, timezone

        from kalshi_research_bot.browser_fixtures import make_verified_fixture_payload
        from kalshi_research_bot.slip_safety import slip_payload_gate

        now = datetime.now(timezone.utc)
        base = make_verified_fixture_payload()

        def mutated(**changes):
            payload = copy.deepcopy(base)
            payload.update(changes)
            return payload

        missing = copy.deepcopy(base)
        missing.pop("generated_at", None)
        return {
            "refresh failed": slip_payload_gate(
                mutated(refresh_error="collector_failed"), now=now
            ),
            "missing timestamp": slip_payload_gate(missing, now=now),
            "unparseable timestamp": slip_payload_gate(mutated(generated_at="not-a-time"), now=now),
            "future timestamp": slip_payload_gate(
                mutated(generated_at=(now + timedelta(hours=2)).isoformat()), now=now
            ),
            "stale source fallback": slip_payload_gate(
                mutated(source_cache_status={"stale_fallback_count": 3}), now=now
            ),
            "ordinary stale": slip_payload_gate(
                mutated(generated_at=(now - timedelta(hours=3)).isoformat()), now=now
            ),
            "fresh": slip_payload_gate(base, now=now),
        }

    def test_every_gate_outcome_carries_a_status(self) -> None:
        for label, gate in self.gates().items():
            with self.subTest(gate=label):
                self.assertIn(gate.get("status"), {"ready", "blocked"}, f"{label}: {gate!r}")

    def test_every_gate_outcome_carries_its_own_message(self) -> None:
        """The poller shows it, so a blank one would put a generic sentence in
        front of a reader for a specific problem."""

        messages = set()
        for label, gate in self.gates().items():
            with self.subTest(gate=label):
                message = gate.get("message")
                self.assertTrue(message, f"{label} has no message")
                messages.add(message)
        self.assertGreater(len(messages), 1, "every gate outcome says the same thing")

    def test_only_ordinary_staleness_carries_an_age_the_old_check_could_use(self) -> None:
        """The measurement behind the fix, kept as a test so it cannot quietly
        stop being true: four of the five blocked outcomes have no age the
        superseded comparison would have caught."""

        usable = {
            label
            for label, gate in self.gates().items()
            if gate.get("status") == "blocked"
            and isinstance(gate.get("data_age_seconds"), (int, float))
            and gate["data_age_seconds"] > 300
        }
        self.assertEqual(usable, {"ordinary stale"})


class FreshnessEndpointWireTests(unittest.TestCase):
    """What `/freshness.json` actually puts on the wire.

    `FreshnessPayloadContractTests` above checks the gate, which is the producer
    -- and that is not the same thing. Verified by mutation: deleting `message`
    from the endpoint's response dict left those tests green, because none of
    them ever read the response. So this makes the request.
    """

    def serve(self, payload: dict) -> dict:
        import json as jsonlib
        import tempfile
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from unittest.mock import patch

        from kalshi_research_bot.paper_server import PaperHandler

        class Handler(PaperHandler):
            def log_message(self, format, *args):
                return

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "payload.json"
            source.write_text(jsonlib.dumps(payload), encoding="utf-8")
            # Both of these, or the request does not read this payload.
            # `data_path` is a class attribute the handler passes to
            # `load_current_payload`; there is no DASHBOARD_PAYLOAD_PATH. And
            # `DASHBOARD_PAYLOAD_SOURCE` defaults to "postgres", so without it
            # the file is never opened at all and every case came back as the
            # same `blocked_refresh_failed` fallback -- which these tests then
            # read as proof of their own assertions.
            original_data_path = Handler.data_path
            Handler.data_path = source
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            environment = {
                "DASHBOARD_PAYLOAD_SOURCE": "file",
                "DASHBOARD_AUTH_ENABLED": "false",
            }
            try:
                with patch.dict("os.environ", environment, clear=False):
                    url = f"http://127.0.0.1:{server.server_address[1]}/freshness.json"
                    with urllib.request.urlopen(url, timeout=10) as response:
                        return jsonlib.loads(response.read().decode())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                Handler.data_path = original_data_path

    def payload_with(self, **changes) -> dict:
        import copy

        from kalshi_research_bot.browser_fixtures import make_verified_fixture_payload

        payload = copy.deepcopy(make_verified_fixture_payload())
        payload.update(changes)
        return payload

    def test_the_harness_serves_the_payload_it_was_given(self) -> None:
        """The control, and it is not decoration.

        Without it these tests passed against a `blocked_refresh_failed`
        fallback served because the handler never opened the file -- so every
        assertion below was reading a payload no test wrote. A fresh fixture
        must come back `ready`, which only happens if the request read it.
        """

        body = self.serve(self.payload_with())
        self.assertEqual(body.get("status"), "ready", f"the payload was not served: {body!r}")
        self.assertEqual(body.get("code"), "fresh_data_ready")

    def test_the_response_carries_the_status_the_client_reads(self) -> None:
        body = self.serve(self.payload_with())
        self.assertIn("status", body, f"the poller has nothing to decide from: {body!r}")
        self.assertIn(body["status"], {"ready", "blocked"})

    def test_the_response_carries_the_message_the_client_shows(self) -> None:
        body = self.serve(self.payload_with())
        self.assertTrue(
            body.get("message"),
            f"the reader would get the generic sentence for every problem: {body!r}",
        )

    def test_a_payload_with_no_timestamp_is_reported_as_blocked_on_the_wire(self) -> None:
        """The case the old client missed entirely: no age to read, and the
        status is the only thing that says anything is wrong."""

        payload = self.payload_with()
        payload.pop("generated_at", None)
        body = self.serve(payload)
        self.assertEqual(body.get("status"), "blocked")
        self.assertIsNone(body.get("data_age_seconds"))
        self.assertTrue(body.get("message"))


if __name__ == "__main__":
    unittest.main()
