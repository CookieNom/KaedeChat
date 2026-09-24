import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt', 'CocoaPods runs on macOS; shell fixtures need POSIX')
class PodInstallTest(unittest.TestCase):
    def run_install(self, failures, message, *, command=None):
        script = Path(__file__).resolve().parents[2] / 'mobile/tool/install_pods.sh'
        if command:
            script = script.parents[2] / '.github/scripts/retry-network.sh'
        direct = command is not None
        command, *arguments = command or ['pod']
        expected = ' '.join(arguments) if direct else 'install --deployment --project-directory=ios'
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
                ['bash', str(script), *([command, *arguments] if direct else [])],
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
            command=['rustup', 'toolchain', 'install', '1.97.1', '--profile', 'minimal'],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attempts, 2)

    def test_rust_download_failure_exhausts_retries(self):
        result, attempts = self.run_install(10, 'dns error: failed to lookup address information', command=['rustup', 'toolchain', 'install', '1.97.1', '--profile', 'minimal'])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 4)

    def test_rust_integrity_errors_are_not_retried(self):
        result, attempts = self.run_install(10, 'checksum failed for downloaded file', command=['rustup', 'toolchain', 'install', '1.97.1', '--profile', 'minimal'])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(attempts, 1)

    def test_native_build_download_retries_without_retrying_compiler_errors(self):
        for failures, message, attempts, status in (
            (1, "Failed to send HTTP request to download WebRTC\n"
             "failed to lookup address information: nodename nor servname provided, or not known", 2, 0),
            (10, "failed to lookup address information", 4, 7),
            (10, "error[E0308]: mismatched types", 1, 7),
        ):
            with self.subTest(message=message):
                result, actual = self.run_install(
                    failures, message,
                    command=['cargo', '+1.97.1', 'build', '--release', '--locked'],
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(actual, attempts)

    def test_docker_registry_failure_recovers_without_retrying_build_errors(self):
        for failures, message, attempts, status in (
            (1, "failed to copy: httpReadSeeker: failed open: unexpected status code "
             "https://registry-1.docker.io/v2/docker/dockerfile/manifests/sha256:abc: "
             "502 Bad Gateway", 2, 0),
            (10, "Dockerfile: unknown instruction: RUNN", 1, 7),
        ):
            with self.subTest(message=message):
                result, actual = self.run_install(
                    failures, message,
                    command=['docker', 'build', '--target', 'development', '.'],
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(actual, attempts)
