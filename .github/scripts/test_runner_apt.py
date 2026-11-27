"""Check runner APT isolation without changing the host's package sources."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "Ubuntu setup requires bash")
class RunnerAptTests(unittest.TestCase):
    def test_removes_only_unused_feed_and_preserves_update_failure(self):
        script = Path(__file__).with_name("update-runner-apt.sh")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "commands"
            for command in ("rm", "apt-get"):
                stub = root / command
                stub.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\\n" "$(basename "$0") $*" >> "$COMMAND_LOG"\n'
                    'if [ "$(basename "$0")" = apt-get ]; then exit "$APT_STATUS"; fi\n'
                )
                stub.chmod(0o755)
            for status in (0, 100):
                with self.subTest(status=status):
                    log.write_text("")
                    result = subprocess.run(
                        ["bash", str(script)], capture_output=True, text=True,
                        env=os.environ | {"PATH": f"{root}:{os.environ['PATH']}",
                                          "COMMAND_LOG": str(log), "APT_STATUS": str(status)},
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(log.read_text().splitlines(), [
                        "rm -f /etc/apt/sources.list.d/microsoft-prod.list "
                        "/etc/apt/sources.list.d/microsoft-prod.sources",
                        "apt-get update",
                    ])
