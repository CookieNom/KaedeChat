import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import prepare_codecs as codecs


class CodecPreparationTest(unittest.TestCase):
    def test_artifact_requires_matching_source_target_and_hevc_build(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(codecs, 'fingerprint', return_value='expected'), patch.object(codecs, 'triple', return_value='linux-x64'):
            root = Path(temp)
            manifest = {'fingerprint': 'expected', 'target': 'linux-x64', 'webrtc_revision': codecs.PINS['webrtc_revision']}
            for name in ['lib/libwebrtc.a', 'webrtc.ninja', 'desktop_capture.ninja', 'LICENSE.md', 'include/api/peer_connection_interface.h']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            (root / 'args.gn').write_text('rtc_use_h265 = true\n')
            (root / 'kaede-build.json').write_text(json.dumps(manifest))
            self.assertEqual(codecs.verify_artifact(root), root.resolve())
            for key, value in [('fingerprint', 'old'), ('target', 'win-x64'), ('webrtc_revision', 'other')]:
                with self.subTest(key=key):
                    (root / 'kaede-build.json').write_text(json.dumps({**manifest, key: value}))
                    with self.assertRaises(SystemExit):
                        codecs.verify_artifact(root)
            (root / 'kaede-build.json').write_text(json.dumps(manifest))
            for args in ['rtc_use_h265 = false\n',
                         '# rtc_use_h265 = true\nrtc_use_h265 = false\n',
                         'rtc_use_h265 = true\nrtc_use_h265 = false\n']:
                with self.subTest(args=args):
                    (root / 'args.gn').write_text(args)
                    with self.assertRaises(SystemExit):
                        codecs.verify_artifact(root)
            (root / 'args.gn').write_text('rtc_use_h265 = true\n')
            (root / 'lib/libwebrtc.a').unlink()
            with self.assertRaises(SystemExit):
                codecs.verify_artifact(root)

    def test_sdk_rejects_a_checkout_at_a_different_revision(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(codecs, 'fingerprint', return_value='expected'), patch.object(codecs.subprocess, 'check_output', return_value='wrong-revision\n'):
            root = Path(temp)
            with patch.object(codecs, 'NATIVE', root), patch.object(codecs, 'SDK', root / 'sdk'):
                with self.assertRaisesRegex(SystemExit, 'revision differs'):
                    codecs.prepare_sdk(root / 'source')
                self.assertFalse((root / 'sdk').exists())

    def test_cuda_extracts_verified_headers_only_and_rejects_escape(self):
        for malicious in (False, True):
            with self.subTest(malicious=malicious), tempfile.TemporaryDirectory() as temp:
                wheel = io.BytesIO()
                with zipfile.ZipFile(wheel, 'w') as bundle:
                    bundle.writestr('nvidia/cuda_runtime/include/cuda.h', 'header')
                    bundle.writestr('nvidia/cuda_runtime/lib/libcudart.so', 'never distribute')
                    if malicious:
                        bundle.writestr('nvidia/cuda_runtime/include/../../escaped', 'bad')
                data = wheel.getvalue()
                entry = {'url': 'https://example.invalid/cuda.whl', 'sha256': hashlib.sha256(data).hexdigest()}
                with patch.object(codecs, 'NATIVE', Path(temp)), patch.object(codecs, 'triple', return_value='linux-x64'), patch.dict(codecs.PINS, {'cuda_headers': {'linux-x64': entry}}), patch.object(codecs.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                    if malicious:
                        with self.assertRaisesRegex(SystemExit, 'Unsafe CUDA header path'):
                            codecs.prepare_cuda()
                    else:
                        output = codecs.prepare_cuda()
                        self.assertEqual((output / 'include/cuda.h').read_text(), 'header')
                        self.assertFalse((output / 'lib').exists())
                    self.assertFalse((Path(temp) / 'escaped').exists())
                with patch.object(codecs, 'NATIVE', Path(temp) / 'wrong-hash'), patch.object(codecs, 'triple', return_value='linux-x64'), patch.dict(codecs.PINS, {'cuda_headers': {'linux-x64': {**entry, 'sha256': 'incorrect'}}}), patch.object(codecs.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                    with self.assertRaisesRegex(SystemExit, 'hash mismatch'):
                        codecs.prepare_cuda()


if __name__ == '__main__':
    unittest.main()
