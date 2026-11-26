import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import prepare_codecs as codecs
import webrtc_registry as registry


def write_seed(path, target='mac-x64'):
    manifest = {'target': target, 'webrtc_revision': codecs.PINS['webrtc_revision'], 'fingerprint': 'legacy'}
    files = {name: b'fixture' for name in (
        'lib/libwebrtc.a', 'webrtc.ninja', 'desktop_capture.ninja', 'LICENSE.md',
        'include/api/peer_connection_interface.h',
    )}
    files.update({'kaede-build.json': json.dumps(manifest).encode(), 'args.gn': b'rtc_use_h265=true\n'})
    with zipfile.ZipFile(path, 'w') as package, package.open(registry.TAR, 'w') as data:
        with tarfile.open(fileobj=data, mode='w|') as tar:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            if os.name != 'nt':
                link = tarfile.TarInfo('include/peer_connection.h')
                link.type = tarfile.SYMTYPE
                link.linkname = 'api/peer_connection_interface.h'
                tar.addfile(link)
    return {'fingerprint': 'current', 'digest': 'sha256:' + 'a' * 64,
            'seed': {'fingerprint': 'legacy', 'digest': registry.file_digest(path)}}


class WebRTCRegistryTest(unittest.TestCase):
    def test_seed_pull_migrates_verified_manifest_and_preserves_previous_install_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'seed.zip'
            entry = write_seed(archive)
            native = root / 'native'
            installed = native / 'webrtc'

            def download(args, *, check):
                self.assertEqual(args[:4], ['oras', 'pull', f"{registry.REGISTRY}@{entry['digest']}", '-o'])
                self.assertTrue(check)
                shutil.copyfile(archive, Path(args[4]) / registry.ARCHIVE)

            with (patch.object(registry, 'NATIVE', native), patch.object(codecs, 'triple', return_value='mac-x64'),
                  patch.object(codecs, 'native_fingerprint', return_value='current'),
                  patch.object(registry.subprocess, 'run', side_effect=download) as fetch):
                for bad_pin in ({**entry, 'fingerprint': 'stale'}, {**entry, 'digest': 'unversioned'}):
                    with self.assertRaisesRegex(SystemExit, 'No matching registry pin'):
                        registry.pull('mac-x64', bad_pin, 'current')
                fetch.assert_not_called()
                registry.pull('mac-x64', entry, 'current')
                self.assertEqual(codecs.verify_artifact(installed), installed.resolve())
                if os.name != 'nt':
                    link = installed / 'include/peer_connection.h'
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(link.samefile(installed / 'include/api/peer_connection_interface.h'))
                (installed / 'previous-install').touch()

                for change, error in [({'digest': 'bad'}, 'artifact digest'), ({'fingerprint': 'wrong'}, 'source revision')]:
                    bad_seed = {**entry, 'seed': {**entry['seed'], **change}}
                    with self.subTest(change=change), self.assertRaisesRegex(SystemExit, error):
                        registry.pull('mac-x64', bad_seed, 'current')
                wrong_target = write_seed(archive, target='mac-arm64')
                with self.assertRaisesRegex(SystemExit, 'source revision and target'):
                    registry.pull('mac-x64', wrong_target, 'current')
                with zipfile.ZipFile(archive, 'a') as package:
                    package.writestr('unrecognized.txt', 'unexpected package member')
                wrong_target['seed']['digest'] = registry.file_digest(archive)
                with self.assertRaisesRegex(SystemExit, 'Expected a single'):
                    registry.pull('mac-x64', wrong_target, 'current')
                self.assertTrue((installed / 'previous-install').exists())
                self.assertEqual(codecs.verify_artifact(installed), installed.resolve())

    def test_seed_manifest_digest_is_checked_before_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'source.zip'
            entry = write_seed(archive)
            entry['seed']['artifact_id'] = 42

            def run(args, **kwargs):
                if args[:2] == ['gh', 'api']:
                    kwargs['stdout'].write(archive.read_bytes())
                elif args[:3] == ['oras', 'push', '--oci-layout']:
                    (kwargs['cwd'] / 'manifest.json').write_text('different OCI manifest')
                else:
                    self.fail(f'Unexpected command before seed digest verification: {args}')

            with (patch.object(registry.subprocess, 'run', side_effect=run),
                  patch.object(codecs, 'triple', return_value='mac-x64'),
                  patch.object(codecs, 'native_fingerprint', return_value='current'),
                  self.assertRaisesRegex(SystemExit, 'Seed manifest digest')):
                registry.publish('mac-x64', 'package:tag', entry, 'current', seed=True)

    def test_publisher_plans_from_registry_state_without_replacing_a_pin(self):
        digest = 'sha256:' + 'a' * 64
        pinned = {'fingerprint': 'current', 'digest': digest}
        cases = [
            (pinned, 0, digest, '', 'ready'),
            ({'fingerprint': 'previous'}, 0, digest, '', 'ready'),
            ({}, 1, '', 'manifest_unknown', 'build'),
            ({**pinned, 'seed': {}}, 1, '', 'manifest_unknown', 'seed'),
            ({**pinned, 'seed': {}}, 1, '', 'denied', 'seed'),
            (pinned, 1, '', 'manifest_unknown', 'Pinned package'),
            (pinned, 0, 'sha256:' + 'b' * 64, '', 'differs from the committed digest'),
            ({}, 0, 'empty manifest', '', 'Unexpected registry digest'),
            ({}, 1, '', 'connection closed', 'connection closed'),
            ({**pinned, 'seed': {}}, 1, '', 'connection closed', 'connection closed'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            output, summary = Path(temp) / 'output', Path(temp) / 'summary'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}):
                for entry, code, stdout, stderr, expected in cases:
                    result = subprocess.CompletedProcess([], code, stdout, stderr)
                    with self.subTest(expected=expected, entry=entry), patch.object(registry.subprocess, 'run', return_value=result):
                        output.write_text('')
                        if expected in {'ready', 'build', 'seed'}:
                            registry.plan('mac-x64', 'package:tag', entry, 'current')
                            self.assertEqual(output.read_text(), f'mode={expected}\n')
                        else:
                            with self.assertRaisesRegex(SystemExit, expected):
                                registry.plan('mac-x64', 'package:tag', entry, 'current')
                            self.assertEqual(output.read_text(), '')


if __name__ == '__main__':
    unittest.main()
