"""Exercise native build orchestration without downloading/compiling WebRTC."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "webrtc_build", Path(__file__).resolve().parents[2] / "mobile/tool/build_webrtc.py"
)
webrtc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webrtc)


class WebRTCBuildTest(unittest.TestCase):
    def test_ios_direct_builder_enables_rtti_and_preserves_framework_links(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "webrtc-build").mkdir()
            framework = work / "ios/WebRTC.xcframework"
            framework.mkdir(parents=True)
            (framework / "WebRTC").write_bytes(b"test framework")
            # Windows runners may not permit symlink creation.
            if os.name != "nt":
                (framework / "Current").symlink_to("WebRTC")
            output = work / "output"
            with (
                patch.object(webrtc.sys, "argv", ["build_webrtc.py", "ios", "--work-dir", str(work)]),
                patch.object(webrtc.sys, "platform", "darwin"),
                patch.object(webrtc, "OUTPUT", output),
                patch.dict(os.environ),
                patch.object(webrtc.subprocess, "check_output", return_value=webrtc.BUILD_REV),
                patch.object(webrtc.subprocess, "run") as command,
            ):
                command.return_value.returncode = 0
                webrtc.main()
            calls = [call.args[0] for call in command.call_args_list]
            ios = next(args for args in calls if any(str(arg).endswith("build_ios_libs.py") for arg in args))
            gn_args = ios[ios.index("--extra-gn-args") + 1].split()
            self.assertIn("use_rtti=true", gn_args)
            self.assertIn("enable_libaom=true", gn_args)
            self.assertIn("rtc_include_dav1d_in_internal_decoder_factory=true", gn_args)
            self.assertIn("device:arm64", ios)
            self.assertIn("simulator:arm64", ios)
            self.assertIn("simulator:x64", ios)
            self.assertEqual((output / "WebRTC.xcframework/WebRTC").read_bytes(), b"test framework")
            if os.name != "nt":
                self.assertTrue((output / "WebRTC.xcframework/Current").is_symlink())
            self.assertEqual(json.loads((output / "ios-build.json").read_text())["webrtc_revision"], webrtc.WEBRTC_REV)


if __name__ == "__main__":
    unittest.main()
