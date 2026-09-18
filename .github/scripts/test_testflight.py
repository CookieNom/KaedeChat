import base64
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile


spec = importlib.util.spec_from_file_location(
    "testflight", Path(__file__).with_name("upload-testflight.py")
)
testflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(testflight)


class TestFlightTest(unittest.TestCase):
    def test_delivery_sets_exemption_without_creating_declaration(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse), tempfile.TemporaryDirectory() as directory:
                ipa = Path(directory) / "app.ipa"
                with zipfile.ZipFile(ipa, "w") as archive:
                    archive.writestr("Payload/Runner.app/Info.plist", plistlib.dumps({
                        "CFBundleIdentifier": "chat.kaede.mobile",
                        "CFBundleVersion": "54.1",
                        "CFBundleShortVersionString": "0.1.59",
                    }, fmt=plistlib.FMT_BINARY))
                    archive.writestr("Payload/Runner.app/PlugIns/Other.appex/Info.plist", b"ignored")
                states = iter(["VALID", "VALID"] if reuse else [None, "PROCESSING", "VALID"])

                def api(key, method, path, data=None):
                    self.assertEqual(key, "key.p8")
                    if path.startswith("apps?"):
                        return {"data": [{"id": "app"}]}
                    if path.startswith("builds?"):
                        self.assertEqual(parse_qs(urlsplit(path).query), {
                            "filter[app]": ["app"], "filter[version]": ["54.1"],
                            "filter[preReleaseVersion.version]": ["0.1.59"],
                            "filter[preReleaseVersion.platform]": ["IOS"],
                        })
                        state = next(states)
                        return {"data": [{"id": "build", "attributes": {
                            "processingState": state,
                        }}] if state else []}
                    if method == "PATCH":
                        self.assertEqual(path, "builds/build")
                        self.assertEqual(data, {
                            "type": "builds", "id": "build",
                            "attributes": {"usesNonExemptEncryption": False},
                        })
                        return {"data": {"id": "build"}}
                    # The previous POST with standard/no-France answers got 409.
                    self.assertEqual((method, path), ("GET", "builds/build"))
                    return {"data": {"id": "build", "attributes": {
                        "usesNonExemptEncryption": False,
                    }}}

                with patch.object(testflight, "request", side_effect=api) as requests, \
                        patch.object(testflight.subprocess, "run") as upload, \
                        patch.object(testflight.time, "sleep") as sleep, \
                        patch.dict(os.environ, APP_STORE_CONNECT_KEY_ID="key", APP_STORE_CONNECT_ISSUER_ID="issuer"):
                    testflight.deliver(str(ipa), "key.p8")
                self.assertEqual(upload.call_count, 0 if reuse else 1)
                self.assertEqual(sleep.call_count, 0 if reuse else 1)
                self.assertEqual(sum(call.args[1] == "PATCH" for call in requests.call_args_list), 1)

    def test_existing_exemption_is_verified_without_upload_or_patch(self):
        for confirmed in (False, True, None):
            with self.subTest(confirmed=confirmed), \
                    patch.object(testflight, "ipa_info", return_value={
                        "CFBundleIdentifier": "chat.kaede.mobile",
                        "CFBundleVersion": "54.1", "CFBundleShortVersionString": "0.1.59",
                    }), \
                    patch.object(testflight, "request", side_effect=[
                        {"data": [{"id": "app"}]},
                        *[{"data": [{"id": "build", "attributes": {
                            "processingState": "VALID", "usesNonExemptEncryption": False,
                        }}]}] * 2,
                        {"data": {"id": "build", "attributes": {
                            "usesNonExemptEncryption": confirmed,
                        }}},
                    ]) as requests, \
                    patch.object(testflight.subprocess, "run") as upload:
                if confirmed is False:
                    testflight.deliver("app.ipa", "key.p8")
                else:
                    with self.assertRaisesRegex(RuntimeError, "did not retain"):
                        testflight.deliver("app.ipa", "key.p8")
                upload.assert_not_called()
                self.assertTrue(all(call.args[1] == "GET" for call in requests.call_args_list))

    def test_changed_owner_answers_require_review_before_upload(self):
        for answer in ("containsProprietaryCryptography", "availableOnFrenchStore"):
            with self.subTest(answer=answer), \
                    patch.dict(testflight.ANSWERS, {answer: True}), \
                    patch.object(testflight, "request") as requests:
                with self.assertRaisesRegex(RuntimeError, "Encryption answers changed"):
                    testflight.deliver("app.ipa", "key.p8")
                requests.assert_not_called()

    def test_failed_or_stalled_processing_never_declares_encryption(self):
        for state in ("FAILED", "INVALID", "PROCESSING"):
            with self.subTest(state=state), \
                    patch.object(testflight, "ipa_info", return_value={
                        "CFBundleIdentifier": "chat.kaede.mobile",
                        "CFBundleVersion": "54.1", "CFBundleShortVersionString": "0.1.59",
                    }), \
                    patch.object(testflight, "request", side_effect=[
                        {"data": [{"id": "app"}]},
                        {"data": [{"id": "build", "attributes": {"processingState": state}}]},
                        {"data": [{"id": "build", "attributes": {"processingState": state}}]},
                    ]) as requests, \
                    patch.object(testflight.time, "monotonic", side_effect=[0, 1801]):
                with self.assertRaises(RuntimeError):
                    testflight.deliver("app.ipa", "key.p8")
                self.assertTrue(all(call.args[1] == "GET" for call in requests.call_args_list))

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required by the macOS release job")
    def test_jwt_signature_verifies_with_openssl(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, APP_STORE_CONNECT_KEY_ID="key", APP_STORE_CONNECT_ISSUER_ID="issuer"):
            key = str(Path(directory) / "key.p8")
            subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                            "ec_paramgen_curve:P-256", "-out", key], check=True, capture_output=True)
            public = str(Path(directory) / "public.pem")
            subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public],
                           check=True, capture_output=True)
            for _ in range(8):  # Exercise DER integers with and without leading sign bytes.
                header, claims, signature = testflight.token(key).split(".")
                decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                self.assertEqual(json.loads(decode(header))["alg"], "ES256")
                payload = json.loads(decode(claims))
                self.assertEqual(payload["aud"], "appstoreconnect-v1")
                self.assertEqual(payload["exp"] - payload["iat"], 600)
                raw = decode(signature)
                self.assertEqual(len(raw), 64)
                integers = []
                for part in (raw[:32], raw[32:]):
                    part = part.lstrip(b"\x00") or b"\x00"
                    if part[0] & 128:
                        part = b"\x00" + part
                    integers.append(b"\x02" + bytes([len(part)]) + part)
                body = b"".join(integers)
                sig_file = Path(directory) / "signature.der"
                sig_file.write_bytes(b"\x30" + bytes([len(body)]) + body)
                subprocess.run(["openssl", "dgst", "-sha256", "-verify", public,
                                "-signature", str(sig_file)], input=f"{header}.{claims}".encode(),
                               check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
