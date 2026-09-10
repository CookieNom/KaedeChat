import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


spec = importlib.util.spec_from_file_location(
    "release_inputs", Path(__file__).with_name("check-release-inputs.py")
)
release_inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_inputs)


class ReleaseInputsTest(unittest.TestCase):
    def test_ci_lockfiles_are_present_and_not_ignored(self):
        root = Path(__file__).resolve().parents[2]
        for name in (
            "backend/uv.lock",
            "sdk/python/uv.lock",
            "frontend/pnpm-lock.yaml",
            "desktop/Cargo.lock",
            "mobile/pubspec.lock",
            "mobile/ios/Podfile.lock",
        ):
            with self.subTest(lockfile=name):
                self.assertTrue((root / name).is_file(), f"Missing CI lockfile: {name}")
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", name],
                    cwd=root, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1, f"CI lockfile is ignored: {name}\n{result.stderr}")

    def test_stamp_release_checkout(self):
        source = Path(__file__).resolve().parents[2]
        names = (
            "desktop/tauri/src-tauri/tauri.conf.json",
            "desktop/tauri/src-tauri/Cargo.toml",
            "desktop/Cargo.lock",
            "desktop/rust-toolchain.toml",
            "frontend/package.json",
            "mobile/ios/Podfile",
            "mobile/ios/Podfile.lock",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, root / name)
            lock_path = root / "desktop/Cargo.lock"
            original_lock = tomllib.loads(lock_path.read_text())
            for tag in ("v1.2.3", "desktop-v2.0.0"):
                release_inputs.stamp(root, tag)
                release_inputs.check(root, tag)
                version = tag.removeprefix("desktop-").removeprefix("v")
                self.assertEqual(json.loads((root / "frontend/package.json").read_text())["version"], version)
                expected_lock = original_lock.copy()
                expected_lock["package"] = [
                    dict(p, version=version) if p["name"] == "kaede-tauri" else p
                    for p in original_lock["package"]
                ]
                self.assertEqual(tomllib.loads(lock_path.read_text()), expected_lock)
                stamped = {name: (root / name).read_bytes() for name in names}
                release_inputs.stamp(root, tag)
                self.assertEqual(stamped, {name: (root / name).read_bytes() for name in names})
            for tag in ("main", "sdk-v1.2.3", "v01.2.3", "v1.2.3-rc.1", "v1.2.3\n"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release_inputs.stamp(root, tag)
                self.assertEqual(stamped, {name: (root / name).read_bytes() for name in names})

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
