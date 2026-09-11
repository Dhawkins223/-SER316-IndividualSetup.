from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import uuid
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

    # Everything the script legitimately calls, minus Docker. Restricting the
    # PATH to `cat` and `dirname` also hid `grep` and `cut`, which
    # `local_env_value` runs whenever an untracked `.env` exists -- the normal
    # state of a developer checkout, and of CI, which copies one. That emitted
    # `grep: command not found` into every child's stderr, and it would have
    # made any test that reached `db_start` fail on a missing interpreter
    # rather than exercising the flow it meant to.
    REQUIRED_EXECUTABLES = ("cat", "dirname", "grep", "cut", "sleep", "env")

    def _bin_dir(self, tmp: str) -> Path:
        bin_dir = Path(tmp)
        for executable in self.REQUIRED_EXECUTABLES:
            for candidate in (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin")):
                target = candidate / executable
                if target.exists():
                    (bin_dir / executable).symlink_to(target)
                    break
        # The interpreter running this suite, not whichever python3 happens to
        # sit in /usr/bin. `local.sh` falls back to `python3` when there is no
        # .venv, and under actions/setup-python the system python3 is a
        # different installation without psycopg -- so the external-mode
        # connection check would die on an import rather than test anything.
        (bin_dir / "python3").symlink_to(sys.executable)
        return bin_dir

    def _run(self, command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        # GitHub-hosted runners have Docker in /usr/bin, while the minimal
        # execution environment used during development does not. Build the
        # PATH this test needs instead of assuming anything about the host:
        # everything the script legitimately calls, and deliberately not
        # `docker`, which is the condition under test.
        #
        # These are resolved rather than hardcoded to /usr/bin because the
        # interpreter that matters is whichever `python3` the job installed the
        # package into -- on a runner that is under /opt/hostedtoolcache.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin_dir(tmp)
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

    def test_requiring_compose_without_docker_names_both_ways_out(self) -> None:
        result = self._run("db-start", HAWKNETIC_LOCAL_DB="compose")

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

    LOCAL = "postgresql://127.0.0.1:54329/dev"
    LOCAL_TEST = "postgresql://127.0.0.1:54329/dev_test"

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
            HAWKNETIC_TEST_DATABASE_URL="postgresql://monorail.proxy.rlwy.net:1234/railway",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing to use a hosted", result.stderr)

    def test_the_refusal_can_be_overridden_deliberately(self) -> None:
        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.LOCAL,
            HAWKNETIC_TEST_DATABASE_URL="postgresql://monorail.proxy.rlwy.net:1234/railway",
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


class ShellSyntaxTests(unittest.TestCase):
    """`local.sh` must parse.

    A merge resolution closed an `until ... do` loop with `fi`, and the whole
    script stopped parsing -- so every command in it failed, including the
    `./scripts/local.sh test` that CLAUDE.md names as the way to run this suite.
    Eight tests in this file went red, and each reported its own assertion
    rather than the one-line cause, because they all invoke the script and none
    of them checks that it is a script.

    `bash -n` costs nothing and names the defect directly.
    """

    def test_the_workflow_script_parses(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "-n", str(LOCAL_SCRIPT)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_shipped_shell_script_parses(self) -> None:
        """The same check for the rest of them, since one was enough to break."""

        scripts = sorted(LOCAL_SCRIPT.parent.glob("*.sh"))
        self.assertTrue(scripts, "no shell scripts found; this test is not looking where it thinks")
        for script in scripts:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["/bin/bash", "-n", str(script)], capture_output=True, text=True, timeout=30
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class ExternalDatabaseConnectionTests(LocalScriptTestCase):
    """Drives the real no-Docker path, not only its entry-point guards.

    The guard tests above all exit before a connection is opened, so on their
    own they leave the headline claim -- that development works with no Docker
    daemon -- resting on the arguments never reaching a database. These run
    `db-start` in external mode against the live test database, which is the
    command every other external-mode workflow begins with.
    """

    # Unmistakably this test's own. Cleanup is scoped to it, so it can never
    # match a database a developer created.
    SCRATCH_PREFIX = "hawknetic_probe_"

    def setUp(self) -> None:
        self.url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not self.url:
            self.skipTest("no test database configured")

    def test_two_urls_naming_one_database_are_refused(self) -> None:
        """The string comparison cannot see this; only the server can.

        Two URLs differing solely in connection options address the same
        database. Accepting them lets `local.sh test` migrate and truncate the
        developer's development data, so identity is settled by asking each
        server what it is rather than by comparing the strings.
        """

        separator = "&" if "?" in self.url else "?"
        disguised = f"{self.url}{separator}application_name=hawknetic_identity_probe"

        result = self._run(
            "db-start",
            HAWKNETIC_DATABASE_URL=self.url,
            HAWKNETIC_TEST_DATABASE_URL=disguised,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolve to the same database", result.stderr)

    def test_distinct_databases_are_accepted(self) -> None:
        """The success path: two real databases, no Docker, exit 0."""

        try:
            import psycopg
        except ImportError:  # pragma: no cover - psycopg is a runtime dependency
            self.skipTest("psycopg unavailable")

        # Unique per run, and created without a preceding DROP. A fixed name
        # plus `DROP DATABASE IF EXISTS` would destroy a developer's database
        # that happened to share the name -- the same class of accident this
        # whole guard exists to prevent.
        scratch = f"{self.SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
        try:
            with psycopg.connect(self.url, connect_timeout=10, autocommit=True) as connection:
                # Reclaim anything a previous run leaked. A hard kill between
                # CREATE and the DROP below orphans a uniquely-named database
                # that no later run would ever revisit. Scoped to this test's
                # own prefix, so unlike a fixed name it cannot match a
                # developer's database.
                orphans = connection.execute(
                    "SELECT datname FROM pg_database WHERE datname LIKE %s",
                    (self.SCRATCH_PREFIX.replace("_", r"\_") + "%",),
                ).fetchall()
                for (orphan,) in orphans:
                    connection.execute(f'DROP DATABASE IF EXISTS "{orphan}"')
                connection.execute(f'CREATE DATABASE "{scratch}"')
        except Exception as exc:  # noqa: BLE001 - a database this test may not create
            self.skipTest(f"cannot create a scratch database: {exc}")

        try:
            # Keep any query string: `?sslmode=require` sits after the database
            # name, so splitting on the last "/" alone would drop connection
            # options the server requires.
            base, _, query = self.url.partition("?")
            scratch_url = base.rsplit("/", 1)[0] + "/" + scratch + (f"?{query}" if query else "")
            result = self._run(
                "db-start",
                HAWKNETIC_DATABASE_URL=self.url,
                HAWKNETIC_TEST_DATABASE_URL=scratch_url,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("resolve to the same database", result.stderr)
            self.assertNotIn("Docker is required", result.stderr)
            self.assertNotIn("command not found", result.stderr)
        finally:
            with psycopg.connect(self.url, connect_timeout=10, autocommit=True) as connection:
                connection.execute(f'DROP DATABASE IF EXISTS "{scratch}"')

    def test_no_missing_command_noise_reaches_stderr(self) -> None:
        """`local_env_value` runs grep and cut whenever an untracked .env exists."""

        result = self._run(
            "db-status",
            HAWKNETIC_DATABASE_URL=self.url,
            HAWKNETIC_TEST_DATABASE_URL=self.url + "_other",
        )

        self.assertNotIn("command not found", result.stderr)

    def test_without_docker_it_names_the_way_out(self) -> None:
        # No Docker on PATH and nothing listening: the workflow should explain
        # how to point it at a running PostgreSQL rather than demand a
        # container runtime.
        #
        # This asserted `POSTGRES_HOST` and "already running" until the merge
        # that produced Master kept the other branch's script, where the escape
        # hatch is the two URLs rather than four POSTGRES_* variables. The
        # intent is unchanged -- name a way out that is not Docker -- so the
        # assertion follows the script the repository actually ships.
        result = self._run("db-start")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("command not found", result.stderr)
        self.assertIn("Docker is required", result.stderr)
        self.assertIn("HAWKNETIC_DATABASE_URL", result.stderr)
        self.assertIn("HAWKNETIC_TEST_DATABASE_URL", result.stderr)


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
        # Exactly the commands that invoke `unittest discover`. `research-once`
        # is deliberately absent: it runs worker cycles and never starts the
        # suite, so it cannot recurse and must not be refused.
        for command in ("test", "test-integration", "verify"):
            with self.subTest(command=command):
                result = self._run(
                    command,
                    HAWKNETIC_DATABASE_URL=ExternalDatabaseModeTests.LOCAL,
                    HAWKNETIC_TEST_DATABASE_URL=ExternalDatabaseModeTests.LOCAL_TEST,
                    HAWKNETIC_LOCAL_SH_ACTIVE="1",
                )
                self.assertEqual(result.returncode, 3, result.stderr)

    def test_research_once_is_not_refused(self) -> None:
        """It runs worker cycles, never the suite, so it cannot fork-bomb.

        Deliberately run in Compose mode with no external URLs, so it stops at
        the missing-Docker guard immediately. Pointing it at an unreachable
        external database instead would send it into `external_database_ready`,
        whose retry loop costs thirty seconds of connection timeouts on every
        run of the suite to assert one exit code.
        """

        result = self._run("research-once", HAWKNETIC_LOCAL_SH_ACTIVE="1")

        self.assertNotEqual(result.returncode, 3)
        self.assertNotIn("would recurse", result.stderr)
        # Reached the backend selection, which is past the recursion guard.
        self.assertIn("Docker is required", result.stderr)

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
