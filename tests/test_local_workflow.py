from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SCRIPT = ROOT / "scripts" / "local.sh"


class LocalWorkflowEntrypointTests(unittest.TestCase):
    def _run(self, command: str) -> subprocess.CompletedProcess[str]:
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
            return subprocess.run(
                ["/bin/bash", str(LOCAL_SCRIPT), command],
                cwd=ROOT,
                env={**os.environ, "PATH": str(bin_dir)},
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

    def test_database_workflow_explains_the_missing_canonical_runtime(self) -> None:
        result = self._run("test")

        self.assertEqual(result.returncode, 127)
        self.assertIn("Docker is required", result.stderr)
        self.assertIn("repository Codespace", result.stderr)
        self.assertNotIn("command not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
