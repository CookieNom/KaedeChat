"""Exercise release selection with real Git history and publication conditions."""

import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


SPEC = importlib.util.spec_from_file_location(
    "client_releases", Path(__file__).with_name("select-client-releases.py")
)
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)
WORKFLOW = Path(__file__).parents[1] / "workflows/desktop-release.yml"


def release(tag, clients=selector.CLIENTS, **kwargs):
    names = set().union(*(selector.required_assets(tag)[client] for client in clients))
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": name, "size": 1} for name in names], **kwargs}


class ClientReleaseTests(unittest.TestCase):
    def test_desktop_manifest_requires_all_platforms(self):
        block = WORKFLOW.read_text().split("      - name: Create signed desktop update manifest\n")[1]
        script = textwrap.dedent(block.split("        run: |\n")[1].split("      - name:")[0])
        script = script.split("python3 - <<'PY'\n")[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory) / "release-assets"
            assets.mkdir()
            for name in selector.required_assets("v1.2.3")["desktop"] - {"latest.json"}:
                (assets / name).write_text("fixture")
            env = os.environ | {"GITHUB_REF_NAME": "v1.2.3", "version": "1.2.3", "REPOSITORY": "owner/repo"}
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((assets / "latest.json").read_text())
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(set(manifest["platforms"]), {
                "windows-x86_64", "linux-x86_64", "darwin-aarch64", "darwin-x86_64"})
            (assets / "Kaede-Chat-v1.2.3-macos-arm64.app.tar.gz.sig").unlink()
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_paths(self):
        cases = {
            "frontend/src/routes/+page.svelte": {"desktop"},
            "desktop/tauri/src-tauri/src/main.rs": {"desktop"},
            "desktop/crates/kaede-voice/src/lib.rs": {"desktop"},
            "mobile/lib/main.dart": {"android", "ios"},
            "mobile/pubspec.lock": {"android", "ios"},
            "mobile/tool/build_e2ee_native.sh": {"android", "ios"},
            "mobile/android/app/build.gradle.kts": {"android"},
            "mobile/ios/Podfile.lock": {"ios"},
            "desktop/crates/kaede-e2ee/src/lib.rs": set(selector.CLIENTS),
            "desktop/crates/kaede-e2ee-ffi/src/lib.rs": set(selector.CLIENTS),
            "desktop/Cargo.lock": set(selector.CLIENTS),
            "desktop/Cargo.toml": set(selector.CLIENTS),
            "desktop/rust-toolchain.toml": set(selector.CLIENTS),
            ".github/workflows/desktop-release.yml": set(selector.CLIENTS),
            ".github/scripts/select-client-releases.py": set(selector.CLIENTS),
            "desktop/docs/releasing.md": set(),
            "mobile/README.md": set(),
            "desktop/legacy-slint/crates/kaede-desktop/src/main.rs": set(),
            "backend/app/main.py": set(),
            "deploy/compose.yml": set(),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(selector.affected_clients(path), expected)

    def test_published_baselines_and_git_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)

            def commit(path, text, tag):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
                git("add", ".")
                git("commit", "-qm", tag)
                git("tag", tag)

            git("init", "-q")
            git("config", "user.email", "release-test@example.invalid")
            git("config", "user.name", "Release Test")
            commit("mobile/lib/main.dart", "initial", "v1.0.0")
            changed, _ = selector.select_releases(root, [], "v1.0.0")
            self.assertTrue(all(changed.values()))

            commit("frontend/app.js", "desktop change", "desktop-v1.1.0")
            history = [release("v1.0.0"), release("desktop-v1.1.0", ["desktop"])]
            commit("mobile/lib/main.dart", "mobile change", "v1.2.0")
            # Drafts, prereleases, and the current release never advance baselines.
            history += [release("v1.2.0"), release("v1.2.0", draft=True)]
            changed, bases = selector.select_releases(root, history, "v1.2.0")
            self.assertEqual(changed, {"desktop": False, "android": True, "ios": True})
            self.assertEqual(bases, {"desktop": "desktop-v1.1.0", "android": "v1.0.0", "ios": "v1.0.0"})

            # A skipped iOS build keeps its older baseline even after Android ships.
            history = history[:2] + [release("v1.2.0", ["android"])]
            commit("docs/readme.md", "docs", "v1.3.0")
            changed, _ = selector.select_releases(root, history, "v1.3.0")
            self.assertEqual(changed, {"desktop": False, "android": False, "ios": True})
            history[-1] = release("v1.2.0", ["android", "ios"])
            changed, _ = selector.select_releases(root, history, "v1.3.0")
            self.assertFalse(any(changed.values()))

            # Renaming shipping code into an ignored directory still rebuilds it.
            git("mv", "mobile/lib/main.dart", "docs/old.dart")
            git("commit", "-qm", "remove mobile source")
            changed, _ = selector.select_releases(root, history, "v1.3.0")
            self.assertEqual(changed, {"desktop": False, "android": True, "ios": True})

            # A published release on a different branch is not a valid baseline.
            git("checkout", "-qb", "other", "v1.0.0")
            commit("other.txt", "branch", "v1.2.5")
            git("checkout", "-q", "-")
            history += [release("v1.2.5")]
            _, bases = selector.select_releases(root, history, "v1.3.0")
            self.assertNotIn("v1.2.5", bases.values())

    def test_unpublished_and_incomplete_builds_do_not_advance_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "initial"], cwd=root, check=True)
            subprocess.run(["git", "tag", "v1.0.0"], cwd=root, check=True)
            for item in (release("v1.0.0", draft=True), release("v1.0.0", prerelease=True),
                         release("sdk-v1.0.0"), release("v1.0.0", clients=[])):
                with self.subTest(release=item):
                    changed, _ = selector.select_releases(root, [item], "v1.1.0")
                    self.assertTrue(all(changed.values()))
            item = release("v1.0.0")
            next(asset for asset in item["assets"] if asset["name"].endswith("arm64.dmg"))["size"] = 0
            changed, _ = selector.select_releases(root, [item], "v1.1.0")
            self.assertEqual(changed, {"desktop": True, "android": False, "ios": False})

    def test_publish_requires_exactly_the_selected_builds(self):
        expression = WORKFLOW.read_text().split("  publish:\n")[1].split("    if: >-\n")[1].split("    runs-on:")[0]
        jobs = {"desktop-linux": "desktop", "desktop-windows": "desktop", "desktop-macos": "desktop",
                "android": "android", "ios": "ios"}
        for flags in itertools.product((False, True), repeat=4):
            desktop, android, ios, ios_enabled = flags
            selected = {"desktop": desktop, "android": android, "ios": ios and ios_enabled}
            values = {"needs.preflight.result": "success", "needs.ci.result": "success",
                      "needs.preflight.outputs.desktop": str(desktop).lower(),
                      "needs.preflight.outputs.android": str(android).lower(),
                      "needs.preflight.outputs.ios": str(ios).lower(),
                      "vars.IOS_RELEASE_ENABLED": str(ios_enabled).lower()}
            values.update({f"needs.{job}.result": "success" if selected[client] else "skipped"
                           for job, client in jobs.items()})

            def evaluate(overrides=None, cancelled=False):
                context = values | (overrides or {})
                source = re.sub(r"(?:needs|vars)\.[\w.-]+", lambda m: repr(context[m[0]]), expression)
                source = source.replace("!cancelled()", repr(not cancelled)).replace("&&", "and").replace("||", "or")
                return eval(" ".join(source.split()), {"__builtins__": {}}, {})

            with self.subTest(flags=flags):
                self.assertEqual(evaluate(), any(selected.values()))
                self.assertFalse(evaluate(cancelled=True))
                for job in ("preflight", "ci", *jobs):
                    for status in ("failure", "cancelled"):
                        self.assertFalse(evaluate({f"needs.{job}.result": status}))
                for job, client in jobs.items():
                    wrong = "skipped" if selected[client] else "success"
                    self.assertFalse(evaluate({f"needs.{job}.result": wrong}))


if __name__ == "__main__":
    unittest.main()
