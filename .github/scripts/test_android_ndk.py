import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt', 'Android CI installer runs on Linux')
class AndroidNdkTests(unittest.TestCase):
    def test_installation_is_verified_and_retries_are_bounded(self):
        script = Path(__file__).with_name('install-android-ndk.sh').resolve()
        for failures, failure_status, attempts, status in (
            (0, 0, 1, 0), (1, 1, 2, 0), (1, 0, 2, 0), (10, 1, 3, 1), (10, 0, 3, 1),
        ):
            with self.subTest(failures=failures, failure_status=failure_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                extension = root / 'flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt'
                extension.parent.mkdir(parents=True)
                extension.write_text('val ndkVersion: String = "28.2.13676358"\n')
                manager = root / 'sdk/cmdline-tools/latest/bin/sdkmanager'
                manager.parent.mkdir(parents=True)
                manager.write_text('''#!/bin/bash
set -eu
[[ "$2" == "--install" && "$3" == "ndk;28.2.13676358" ]]
count=$(cat "$TEST_ROOT/count")
count=$((count + 1))
echo "$count" > "$TEST_ROOT/count"
if (( count <= TEST_FAILURES )); then
  echo "Error on ZipFile unknown archive" >&2
  exit "$TEST_STATUS"
fi
ndk="$ANDROID_HOME/ndk/28.2.13676358"
mkdir -p "$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin"
echo 'Pkg.Revision = 28.2.13676358' > "$ndk/source.properties"
printf '#!/bin/sh\\nexit 0\\n' > "$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
chmod +x "$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
''')
                manager.chmod(0o755)
                (root / 'count').write_text('0')
                (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
                (root / 'sleep').chmod(0o755)
                env = os.environ | {'ANDROID_HOME': str(root / 'sdk'), 'TEST_ROOT': str(root),
                                    'TEST_FAILURES': str(failures), 'TEST_STATUS': str(failure_status),
                                    'PATH': f'{root}:{os.environ["PATH"]}'}
                command = ['bash', str(script), str(root / 'flutter')]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(int((root / 'count').read_text()), attempts)
                if status == 0:
                    result = subprocess.run(command, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(int((root / 'count').read_text()), attempts)
