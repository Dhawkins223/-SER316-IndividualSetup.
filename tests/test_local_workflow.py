from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SCRIPT = ROOT / "scripts" / "local.sh"


class LocalScriptTestCase(unittest.TestCase):
    """Runs `scripts/local.sh` as a subprocess with a controlled environment.

    Holds no tests of its own: subclassing a class that has them would re-run
    every parent test once per subclass.
    """

    # Set by the script itself. A test that inherited it would be refused as a
    # recursive invocation rather than exercising what it means to.
    RECURSION_MARKER = "HAWKNETIC_LOCAL_SH_ACTIVE"

    # Choosing the database backend from the ambient environment would make
    # these tests pass or fail depending on whether the developer happens to
    # have an external database configured.
    BACKEND_KEYS = (
        "HAWKNETIC_DATABASE_URL",
        "HAWKNETIC_TEST_DATABASE_URL",
        "HAWKNETIC_ALLOW_HOSTED_DATABASE",
    )

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
            env = {
                key: value
                for key, value in os.environ.items()
                if key != self.RECURSION_MARKER and key not in self.BACKEND_KEYS
            }
            env["PATH"] = str(bin_dir)
            env.update(overrides)
            return subprocess.run(
                ["/bin/bash", str(LOCAL_SCRIPT), command],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )


class LocalWorkflowEntrypointTests(LocalScriptTestCase):
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

    def test_database_workflow_explains_the_missing_canonical_runtime(self) -> None:
        result = self._run("test")

        self.assertEqual(result.returncode, 127)
        self.assertIn("Docker is required", result.stderr)
        # The message must name the way out, not only the missing dependency.
        self.assertIn("HAWKNETIC_DATABASE_URL", result.stderr)
        self.assertNotIn("command not found", result.stderr)


class ExternalDatabaseModeTests(LocalScriptTestCase):
    """Development must be possible with no Docker daemon at all.

    The Compose service is a backend, not a prerequisite of the workflow. These
    tests run with Docker absent from PATH, so anything that reaches Compose
    would fail loudly rather than silently passing on a host that has it.
    """

    LOCAL = "postgresql://u:p@127.0.0.1:54329/dev"
    LOCAL_TEST = "postgresql://u:p@127.0.0.1:54329/dev_test"

    def test_both_urls_are_required(self) -> None:
        result = self._run("migrate", HAWKNETIC_DATABASE_URL=self.LOCAL)

        self.assertEqual(result.returncode, 2)
        self.assertIn("needs both", result.stderr)
        self.assertNotIn("Docker is required", result.stderr)

    def test_the_test_database_must_differ_from_the_development_one(self) -> None:
        """The suite writes to the test database; sharing it destroys dev data."""

        result = self._run(
            "migrate",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=self.LOCAL,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must name different databases", result.stderr)

    def test_a_hosted_database_is_refused(self) -> None:
        """CLAUDE.md: never point a Codespace or a test process at production."""

        result = self._run(
            "migrate",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL="postgresql://u:p@monorail.proxy.rlwy.net:1234/railway",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing to use a hosted", result.stderr)

    def test_the_refusal_can_be_overridden_deliberately(self) -> None:
        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL="postgresql://u:p@monorail.proxy.rlwy.net:1234/railway",
            HAWKNETIC_ALLOW_HOSTED_DATABASE="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Refusing to use a hosted", result.stderr)

    def test_compose_only_commands_report_rather_than_fail(self) -> None:
        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=self.LOCAL_TEST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("external database", result.stdout)

    def test_external_mode_does_not_require_docker(self) -> None:
        """A missing daemon must not be reported when Compose is never used."""

        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=self.LOCAL_TEST,
        )

        self.assertNotIn("Docker is required", result.stderr)

    def test_external_mode_does_not_demand_a_compose_password(self) -> None:
        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=self.LOCAL_TEST,
            POSTGRES_PASSWORD="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("POSTGRES_PASSWORD", result.stderr)


class RecursionGuardTests(LocalScriptTestCase):
    """`local.sh test` must not be able to run itself.

    This test suite invokes the script as a subprocess. Once an external
    database is configured, a `test` invocation that inherited it would run the
    suite again, and so would its child: a fork bomb rather than a failure.
    """

    def test_reentry_into_the_suite_is_refused(self) -> None:
        result = self._run(
            "test",
            HAWKNETIC_DATABASE_URL=ExternalDatabaseModeTests.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=ExternalDatabaseModeTests.LOCAL_TEST,
            HAWKNETIC_LOCAL_SH_ACTIVE="1",
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn("would recurse", result.stderr)

    def test_the_guard_covers_every_suite_running_command(self) -> None:
        for command in ("test", "test-integration", "verify", "research-once"):
            with self.subTest(command=command):
                result = self._run(
                    command,
                    HAWKNETIC_DATABASE_URL=ExternalDatabaseModeTests.LOCAL,
                    HAWKNETIC_TEST_DATABASE_URL=ExternalDatabaseModeTests.LOCAL_TEST,
                    HAWKNETIC_LOCAL_SH_ACTIVE="1",
                )
                self.assertEqual(result.returncode, 3, result.stderr)

    def test_the_guard_does_not_block_ordinary_commands(self) -> None:
        """Only the suite recurses. `migrate` from inside a session is fine."""

        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=ExternalDatabaseModeTests.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL=ExternalDatabaseModeTests.LOCAL_TEST,
            HAWKNETIC_LOCAL_SH_ACTIVE="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
