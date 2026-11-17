"""Check desktop build setup without fetching or compiling native code."""
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_webrtc as build


class DesktopWebRTCBuildTest(unittest.TestCase):
    def test_sync_retries_transient_failure_but_preserves_persistent_failure(self):
        for failures in (0, 1, 2, 3):
            with self.subTest(failures=failures):
                error = subprocess.CalledProcessError(2, ['gclient', 'sync'])
                outcomes = [error] * failures + ([None] if failures < 3 else [])
                with patch.object(build, 'run', side_effect=outcomes) as run, patch.object(build.time, 'sleep') as sleep:
                    if failures == 3:
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            build.sync_dependencies('gclient', cwd=Path('work'))
                        self.assertIs(raised.exception, error)
                    else:
                        build.sync_dependencies('gclient', cwd=Path('work'))
                    self.assertEqual(run.call_count, min(failures + 1, 3))
                    for call in run.call_args_list:
                        self.assertEqual(call.args, ('gclient', 'sync', '-D', '--no-history'))
                        self.assertEqual(call.kwargs, {'cwd': Path('work')})
                    self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 20][:failures])

    def test_bootstrap_precedes_sync_with_auto_updates_disabled(self):
        for target in ('linux-x64', 'mac-arm64', 'mac-x64', 'win-x64'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                work = Path(directory).resolve()
                (work / f'{target}-release').mkdir()
                commands = []

                def run(*args, cwd):
                    if 'sync' in args:
                        self.assertEqual(os.environ['DEPOT_TOOLS_UPDATE'], '0')
                    commands.append(args)

                with (
                    patch('sys.argv', ['build_webrtc.py', '--work-dir', str(work), '--output', str(work / 'output')]),
                    patch.dict(os.environ),
                    patch.object(build, 'triple', return_value=target),
                    patch.object(build, 'prepare_sdk', return_value=work / 'sdk'),
                    patch.object(build, 'fingerprint', return_value='fixture'),
                    patch.object(build, 'verify_artifact'),
                    patch.object(build, 'run', side_effect=run),
                    patch.object(build.subprocess, 'check_output', side_effect=[
                        build.PINS['depot_tools_revision'],
                        *(['C:/Visual Studio'] if target.startswith('win-') else []),
                        build.PINS['webrtc_revision'],
                    ]),
                ):
                    build.main()
                bootstrap = 'bootstrap/win_tools.bat' if target.startswith('win-') else 'ensure_bootstrap'
                bootstrap_index = next(i for i, args in enumerate(commands) if str(work / 'depot_tools' / bootstrap) in args)
                sync_index = next(i for i, args in enumerate(commands) if 'sync' in args)
                self.assertLess(bootstrap_index, sync_index)
                timestamp_index = next(i for i, args in enumerate(commands)
                                       if str(work / 'src/build/util/lastchange.py') in args)
                timestamp = commands[timestamp_index]
                self.assertIn('--filter=', timestamp)
                self.assertEqual(timestamp[timestamp.index('--source-dir') + 1], str(work / 'src/build'))
                self.assertEqual(timestamp[timestamp.index('-o') + 1], str(work / 'src/build/util/LASTCHANGE'))
                self.assertLess(sync_index, timestamp_index)
                native_index = next(i for i, args in enumerate(commands)
                                    if any(str(arg).endswith(('build_linux.sh', 'build_macos.sh', 'build_windows.cmd')) for arg in args))
                self.assertLess(timestamp_index, native_index)


if __name__ == '__main__':
    unittest.main()
