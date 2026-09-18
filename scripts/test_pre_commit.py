"""Exercise the hook in disposable repositories without installing CI toolchains."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch


HOOK = Path(__file__).resolve().parents[1] / ".githooks/pre-commit"


class PreCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)
        self.write("bin/uv", "#!/bin/sh\nexit 0\n", executable=True)
        self.git("init", "-q")
        self.git("config", "core.hooksPath", ".githooks")
        self.write(".githooks/pre-commit", HOOK.read_text(), executable=True)
        self.git("add", ".githooks")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root)

    def write(self, name, text, executable=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)

    def commit(self):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "test",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def test_docs_need_no_component_tools(self):
        self.write("README.md", "Documentation only.\n")
        self.git("add", "README.md")
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_tool_blocks_commit(self):
        self.write("backend/example.py", "x = 1\n")
        self.git("add", "backend")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv sync --locked", result.stderr)

    def test_dart_format_failure_blocks_commit(self):
        self.write(".github/scripts/check-release-inputs.py", "pass\n")
        self.write("mobile/.fvmrc", '{"flutter": "3.41.4"}')
        self.write("mobile/pubspec.yaml", "staged manifest")
        self.write("mobile/lib/main.dart", "BAD")
        self.git("add", ".github", "mobile")
        self.write("mobile/pubspec.yaml", "unstaged manifest")
        self.write(
            "mobile/.fvm/flutter_sdk/bin/flutter",
            """#!/usr/bin/env python3
from pathlib import Path
import sys
assert sys.argv[1:] == ["pub", "get", "--offline", "--enforce-lockfile"]
assert Path("pubspec.yaml").read_text() == "staged manifest"
Path(".dart_tool").mkdir()
Path(".dart_tool/package_config.json").write_text("resolved staged dependencies")
""",
            executable=True,
        )
        self.write(
            "mobile/.fvm/flutter_sdk/bin/dart",
            """#!/usr/bin/env python3
from pathlib import Path
import sys
assert sys.argv[1:] == ["format", "--output=none", "--set-exit-if-changed", "lib", "test"]
assert Path(".dart_tool/package_config.json").read_text() == "resolved staged dependencies"
sys.exit(1 if Path("lib/main.dart").read_text() == "BAD" else 0)
""",
            executable=True,
        )
        # The working copy is fixed, but the bad staged Dart must still fail.
        self.write("mobile/lib/main.dart", "GOOD")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--set-exit-if-changed", result.stderr)
        self.git("add", "mobile/lib/main.dart")
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_checks_index_and_preserves_partial_staging(self):
        # A fake Ruff makes failures deterministic and verifies both invocations.
        self.write(
            "backend/.venv/bin/ruff",
            """#!/usr/bin/env python3
from pathlib import Path
import sys
assert sys.argv[1:] in (["check", "."], ["format", "--check", "."])
sys.exit(1 if "BAD" in Path("file with spaces.py").read_text() else 0)
""",
            executable=True,
        )
        name = "backend/file with spaces.py"
        for staged, local, succeeds in (("BAD", "GOOD", False), ("GOOD", "BAD", True)):
            with self.subTest(staged=staged):
                self.write(name, staged)
                self.git("add", name)
                self.write(name, local)
                index_before = self.git("show", f":{name}")
                result = self.commit()
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertEqual((self.root / name).read_text(), local)
                self.assertEqual(self.git("show", f":{name}"), index_before)

    def test_audits_staged_dependencies_and_blocks_failures(self):
        self.write(".github/scripts/check-release-inputs.py", "pass\n")
        self.git("add", ".github")
        self.write("backend/.venv/bin/ruff", "#!/bin/sh\nexit 0\n", executable=True)
        (self.root / "frontend/node_modules").mkdir(parents=True)
        for component, tool, lockfile, arguments in (
            ("backend", "uv", "uv.lock", ["run", "--locked", "pip-audit", "--skip-editable"]),
            ("frontend", "pnpm", "pnpm-lock.yaml", ["audit", "--audit-level=moderate"]),
        ):
            with self.subTest(component=component):
                self.write(
                    f"bin/{tool}",
                    f"""#!/usr/bin/env python3
from pathlib import Path
import sys
if sys.argv[1:] == ["lint"]:
    sys.exit(0)
assert sys.argv[1:] == {arguments!r}
status = Path({lockfile!r}).read_text()
print("audit checked staged dependencies: " + status)
sys.exit(int(status))
""",
                    executable=True,
                )
                name = f"{component}/{lockfile}"
                # Both vulnerabilities (1) and service errors (2) must block.
                for status in ("1", "2", "0"):
                    self.write(name, status)
                    self.git("add", name)
                    self.write(name, "unstaged dependencies")
                    result = self.commit()
                    self.assertEqual(result.returncode == 0, status == "0", result.stderr)
                    self.assertIn("audit checked staged dependencies: " + status, result.stderr)
                    self.assertEqual((self.root / name).read_text(), "unstaged dependencies")
                    self.assertEqual(self.git("show", f":{name}"), status.encode())


if __name__ == "__main__":
    unittest.main()
