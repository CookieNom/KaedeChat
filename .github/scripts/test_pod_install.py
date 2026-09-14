import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt', 'CocoaPods runs on macOS; shell fixtures need POSIX')
class PodInstallTest(unittest.TestCase):
    def run_install(self, failures, message):
        script = Path(__file__).resolve().parents[2] / 'mobile/tool/install_pods.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'count').write_text('0')
            (root / 'pod').write_text('''#!/bin/bash
set -eu
[[ "$*" == "install --deployment --project-directory=ios" ]]
count=$(cat "$TEST_ROOT/count")
count=$((count + 1))
echo "$count" > "$TEST_ROOT/count"
if (( count <= TEST_FAILURES )); then
  echo "$TEST_MESSAGE" >&2
  exit 7
fi
''')
            (root / 'sleep').write_text('#!/bin/bash\nexit 0\n')
            for name in ('pod', 'sleep'):
                (root / name).chmod(0o755)
            result = subprocess.run(
                ['bash', str(script)], capture_output=True, text=True,
                env={**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}',
                     'TEST_ROOT': str(root), 'TEST_FAILURES': str(failures),
                     'TEST_MESSAGE': message},
            )
            return result, int((root / 'count').read_text())

    def test_network_failure_recovers(self):
        result, attempts = self.run_install(1, 'fatal: Could not resolve host: github.com')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attempts, 2)

    def test_network_retries_are_bounded(self):
        result, attempts = self.run_install(10, 'Connection reset by peer')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 4)

    def test_lockfile_errors_fail_immediately(self):
        result, attempts = self.run_install(10, 'There were changes to the lockfile in deployment mode')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 1)

    def test_success_needs_no_retry(self):
        result, attempts = self.run_install(0, '')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(attempts, 1)
