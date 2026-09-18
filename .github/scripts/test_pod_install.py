import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt', 'CocoaPods runs on macOS; shell fixtures need POSIX')
class PodInstallTest(unittest.TestCase):
    def run_install(self, failures, message, *, rustup=False):
        script = Path(__file__).resolve().parents[2] / 'mobile/tool/install_pods.sh'
        if rustup:
            script = script.parents[2] / '.github/scripts/retry-network.sh'
        command = 'rustup' if rustup else 'pod'
        arguments = ('toolchain', 'install', '1.97.1', '--profile', 'minimal') if rustup else ()
        expected = ' '.join(arguments) if rustup else 'install --deployment --project-directory=ios'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'count').write_text('0')
            (root / command).write_text('''#!/bin/bash
set -eu
[[ "$*" == "$TEST_ARGUMENTS" ]]
count=$(cat "$TEST_ROOT/count")
count=$((count + 1))
echo "$count" > "$TEST_ROOT/count"
if (( count <= TEST_FAILURES )); then
  echo "$TEST_MESSAGE" >&2
  exit 7
fi
''')
            (root / 'sleep').write_text('#!/bin/bash\nexit 0\n')
            for name in (command, 'sleep'):
                (root / name).chmod(0o755)
            result = subprocess.run(
                ['bash', str(script), *([command, *arguments] if rustup else [])],
                capture_output=True, text=True,
                env={**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}',
                     'TEST_ROOT': str(root), 'TEST_FAILURES': str(failures),
                     'TEST_MESSAGE': message, 'TEST_ARGUMENTS': expected},
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

    def test_rust_download_dns_failure_recovers(self):
        result, attempts = self.run_install(
            1, 'dns error: failed to lookup address information: nodename nor servname provided',
            rustup=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attempts, 2)

    def test_rust_download_failure_exhausts_retries(self):
        result, attempts = self.run_install(10, 'dns error: failed to lookup address information', rustup=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 4)

    def test_rust_integrity_errors_are_not_retried(self):
        result, attempts = self.run_install(10, 'checksum failed for downloaded file', rustup=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 1)

    @unittest.skipUnless(shutil.which('make'), 'make is required for Compose checks')
    def test_compose_retries_downloads_but_not_test_failures(self):
        repo = Path(__file__).resolve().parents[2]
        for failures, message, test_status, pulls, runs in (
            (1, 'read: connection reset by peer', 0, 2, 1),
            (10, 'read: connection reset by peer', 0, 4, 0),
            (10, 'manifest unknown', 0, 1, 0),
            (0, '', 1, 1, 1),
        ):
            with self.subTest(failures=failures, message=message, test_status=test_status), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'docker').write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
root = Path(os.environ['TEST_ROOT'])
command = next(part for part in sys.argv if part in ('pull', 'up', 'run', 'down'))
with (root / 'calls').open('a') as log:
    log.write(command + '\\n')
if command == 'pull':
    assert '--ignore-buildable' in sys.argv and '--include-deps' in sys.argv
    if (root / 'calls').read_text().splitlines().count('pull') <= int(os.environ['TEST_FAILURES']):
        print(os.environ['TEST_MESSAGE'], file=sys.stderr)
        sys.exit(7)
if command == 'run':
    sys.exit(int(os.environ['TEST_STATUS']))
''')
                (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
                for name in ('docker', 'sleep'):
                    (root / name).chmod(0o755)
                result = subprocess.run(
                    ['make', 'chat-check'], cwd=repo, capture_output=True, text=True,
                    env={**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}',
                         'TEST_ROOT': str(root), 'TEST_FAILURES': str(failures),
                         'TEST_MESSAGE': message, 'TEST_STATUS': str(test_status)},
                )
                calls = (root / 'calls').read_text().splitlines()
                self.assertEqual(calls.count('pull'), pulls, result.stderr)
                self.assertEqual(calls.count('run'), runs, result.stderr)
                self.assertEqual(calls[-1], 'down')
                self.assertEqual(result.returncode == 0, failures == 1 and test_status == 0)
