from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SCRIPT = ROOT / "scripts" / "local.sh"


class LocalWorkflowEntrypointTests(unittest.TestCase):
    def _run(self, command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        # GitHub-hosted runners have Docker in /usr/bin, while the minimal
        # execution environment used during development does not. Build the
        # PATH this test needs instead of assuming anything about the host.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            for executable in ("cat", "dirname"):
                target = Path("/usr/bin") / executable
                if not target.exists():
                    target = Path("/bin") / executable
                (bin_dir / executable).symlink_to(target)
            # Pin the database settings rather than inheriting them. A developer
            # who happens to have PostgreSQL listening on the project's port
            # would otherwise have these preflight tests connect to it and
            # actually run the workflow -- and `local.sh test` runs this suite,
            # so that recurses. Port 1 refuses connections everywhere.
            env = {
                **os.environ,
                "PATH": str(bin_dir),
                "HAWKNETIC_LOCAL_DB": "auto",
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": "1",
                "POSTGRES_USER": "hawknetic",
                "POSTGRES_PASSWORD": "unused-by-a-refused-connection",
                "POSTGRES_DB": "hawknetic",
                "POSTGRES_TEST_DB": "hawknetic_test",
                **overrides,
            }
            return subprocess.run(
                ["/bin/bash", str(LOCAL_SCRIPT), command],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_help_does_not_require_local_configuration_or_docker(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: scripts/local.sh <command>", result.stdout)
        self.assertIn("test", result.stdout)

    def test_unknown_command_is_rejected_before_runtime_preflight(self) -> None:
        result = self._run("not-a-command")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown local workflow command: not-a-command", result.stderr)
        self.assertNotIn("POSTGRES_PASSWORD", result.stderr)
        self.assertNotIn("Docker is required", result.stderr)

    def test_requiring_compose_without_docker_names_both_ways_out(self) -> None:
        result = self._run("db-start", HAWKNETIC_LOCAL_DB="compose")

        self.assertEqual(result.returncode, 127)
        self.assertIn("needs Docker", result.stderr)
        self.assertIn("Codespace", result.stderr)
        # The point of naming the escape hatch is that a laptop without Docker
        # is a supported way to develop this project, not a dead end.
        self.assertIn("HAWKNETIC_LOCAL_DB=external", result.stderr)
        self.assertNotIn("command not found", result.stderr)

    def test_without_docker_it_falls_back_to_an_external_server(self) -> None:
        # No Docker on PATH and nothing listening: the workflow should explain
        # how to point it at a running PostgreSQL rather than demand a
        # container runtime.
        result = self._run("db-start")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("command not found", result.stderr)
        self.assertIn("POSTGRES_HOST", result.stderr)
        self.assertIn("already running", result.stderr)

    def test_an_unknown_database_mode_is_refused(self) -> None:
        result = self._run("db-start", HAWKNETIC_LOCAL_DB="sometimes")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown HAWKNETIC_LOCAL_DB mode: sometimes", result.stderr)


if __name__ == "__main__":
    unittest.main()
