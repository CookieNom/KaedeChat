"""Select changed clients relative to each client's last published GitHub release."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


CLIENTS = ("desktop", "android", "ios")


def version(tag):
    match = re.fullmatch(r"(?:desktop-)?v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
    return tuple(map(int, match.groups())) if match else None


def required_assets(tag):
    prefix = f"Kaede-Chat-{tag}"
    return {
        "desktop": {
            "latest.json",
            f"{prefix}-linux-x86_64.AppImage",
            f"{prefix}-linux-x86_64.AppImage.sig",
            f"{prefix}-windows-x86_64-setup.exe",
            f"{prefix}-windows-x86_64-setup.exe.sig",
            *{f"{prefix}-macos-{arch}.{suffix}"
              for arch in ("arm64", "x86_64")
              for suffix in ("dmg", "app.tar.gz", "app.tar.gz.sig")},
        },
        "android": {f"{prefix}-android.apk", f"{prefix}-android-play.aab"},
        "ios": {f"{prefix}-ios.ipa"},
    }


def affected_clients(path):
    # Documentation and retired clients don't change shipping binaries.
    if path.endswith(".md") or path.startswith(("desktop/docs/", "desktop/legacy-slint/")):
        return set()
    if path in (".github/workflows/desktop-release.yml", "desktop/Cargo.toml",
                "desktop/Cargo.lock", "desktop/rust-toolchain.toml") or path.startswith((
                    ".github/scripts/", "desktop/.cargo/", ".cargo/",
                    "desktop/crates/kaede-e2ee/", "desktop/crates/kaede-e2ee-ffi/")):
        return set(CLIENTS)
    if path.startswith(("desktop/", "frontend/")):
        return {"desktop"}
    if path.startswith("mobile/android/"):
        return {"android"}
    if path.startswith("mobile/ios/"):
        return {"ios"}
    if path.startswith("mobile/"):
        return {"android", "ios"}
    return set()


def select_releases(root, releases, tag):
    current = version(tag)
    if current is None:
        raise ValueError("invalid client release tag")
    baselines = dict.fromkeys(CLIENTS, "")
    candidates = [release for release in releases
                  if not release["draft"] and not release["prerelease"]
                  and version(release["tag_name"]) is not None
                  and version(release["tag_name"]) < current]
    for release in sorted(candidates, key=lambda item: version(item["tag_name"]), reverse=True):
        previous = release["tag_name"]
        ref = f"refs/tags/{previous}"
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", ref, "HEAD"], cwd=root)
        if ancestor.returncode == 1:
            continue
        ancestor.check_returncode()
        names = {asset["name"] for asset in release["assets"] if asset["size"] > 0}
        for client, required in required_assets(previous).items():
            if not baselines[client] and required <= names:
                baselines[client] = previous
        if all(baselines.values()):
            break

    changed = {}
    diffs = {}
    for client, previous in baselines.items():
        if not previous:
            changed[client] = True
            continue
        if previous not in diffs:
            paths = subprocess.check_output([
                "git", "diff", "--name-only", "--no-renames", "-z",
                f"refs/tags/{previous}", "HEAD", "--",
            ], cwd=root).decode().split("\0")
            diffs[previous] = set().union(*(affected_clients(path) for path in paths))
        changed[client] = client in diffs[previous]
    return changed, baselines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--releases", type=Path, required=True, help="gh api --paginate --slurp output")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    releases = [release for page in json.loads(args.releases.read_text()) for release in page]
    changed, baselines = select_releases(Path.cwd(), releases, args.tag)
    outputs = {client: str(value).lower() for client, value in changed.items()}
    outputs["desktop_base"] = baselines["desktop"]
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, value in outputs.items():
            output.write(f"{name}={value}\n")
    summary = ["### Client release selection", "", "| Client | Compare against | Changed |",
               "| --- | --- | --- |"]
    for client in CLIENTS:
        summary.append(f"| {client} | {baselines[client] or 'No published build; bootstrap'} | {outputs[client]} |")
    summary.extend(["", "iOS still requires IOS_RELEASE_ENABLED=true. CI runs for every tag.", ""])
    print("\n".join(summary))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("\n".join(summary))


if __name__ == "__main__":
    main()
