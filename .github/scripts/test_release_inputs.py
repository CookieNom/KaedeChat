import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "release_inputs", Path(__file__).with_name("check-release-inputs.py")
)
release_inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_inputs)


class ReleaseInputsTest(unittest.TestCase):
    def test_release_drift_is_rejected(self):
        files = {
            "desktop/tauri/src-tauri/tauri.conf.json": '{"version": "0.1.42"}',
            "desktop/tauri/src-tauri/Cargo.toml": '[package]\nversion = "0.1.42"\n',
            "desktop/Cargo.lock": '[[package]]\nname = "kaede-tauri"\nversion = "0.1.42"\n',
            "desktop/rust-toolchain.toml": '[toolchain]\nchannel = "1.97.1"\n',
            "mobile/ios/Podfile": "platform :ios, '13.0'\n",
        }
        checksum = hashlib.sha1(files["mobile/ios/Podfile"].encode()).hexdigest()
        files["mobile/ios/Podfile.lock"] = f"PODFILE CHECKSUM: {checksum}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            for tag in (None, "v0.1.42", "desktop-v0.1.42"):
                release_inputs.check(root, tag, "1.97.1")
            for tag in ("v0.1.41", "main", "v0.1.42-rc.1"):
                with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "tag"):
                    release_inputs.check(root, tag)
            with self.assertRaisesRegex(ValueError, "RUST_VERSION"):
                release_inputs.check(root, rust_version="1.88.0")
            for name in (
                "desktop/tauri/src-tauri/tauri.conf.json",
                "desktop/tauri/src-tauri/Cargo.toml",
                "desktop/Cargo.lock",
                "mobile/ios/Podfile",
            ):
                with self.subTest(file=name):
                    original = files[name]
                    (root / name).write_text(original.replace("0.1.42", "0.1.43") + "\n")
                    with self.assertRaises(ValueError):
                        release_inputs.check(root)
                    (root / name).write_text(original)


if __name__ == "__main__":
    unittest.main()
