"""Check release metadata before native builds; uses Python 3.11+ stdlib only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib


def stamp(root: Path, tag: str) -> None:
    """Stamp only shipping client metadata, preserving locked dependencies."""
    match = re.fullmatch(r"(?:desktop-)?v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))", tag)
    if match is None:
        raise ValueError("release tag must be vMAJOR.MINOR.PATCH or desktop-vMAJOR.MINOR.PATCH")
    version = match[1]
    changes = {}
    for name in ("desktop/tauri/src-tauri/tauri.conf.json", "frontend/package.json"):
        path = root / name
        data = json.loads(path.read_text())
        data["version"] = version
        changes[path] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    for name, pattern in (
        ("desktop/tauri/src-tauri/Cargo.toml", r'(\[package\]\n(?:(?!\[)[^\n]*\n)*?version = ")[^"]+(")'),
        ("desktop/Cargo.lock", r'(\[\[package\]\]\nname = "kaede-tauri"\nversion = ")[^"]+(")'),
    ):
        path = root / name
        # Match the exact package field, never dependency versions.
        text, count = re.subn(pattern, lambda m: m[1] + version + m[2], path.read_text())
        if count != 1:
            raise ValueError(f"expected exactly one app version in {name}")
        changes[path] = text
    for path, text in changes.items():
        path.write_text(text)


def check(root: Path, tag: str | None = None, rust_version: str | None = None) -> None:
    desktop = root / "desktop"
    config = json.loads((desktop / "tauri/src-tauri/tauri.conf.json").read_text())
    cargo = tomllib.loads((desktop / "tauri/src-tauri/Cargo.toml").read_text())
    lock = tomllib.loads((desktop / "Cargo.lock").read_text())
    versions = [config["version"], cargo["package"]["version"]]
    versions.extend(p["version"] for p in lock["package"] if p["name"] == "kaede-tauri")
    if len(versions) != 3 or len(set(versions)) != 1:
        raise ValueError("Tauri config, Cargo manifest, and Cargo.lock versions must match")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", versions[0]):
        raise ValueError("release version must be numeric MAJOR.MINOR.PATCH")
    if tag is not None and tag not in (f"v{versions[0]}", f"desktop-v{versions[0]}"):
        raise ValueError(f"tag {tag!r} does not match Tauri version {versions[0]}")

    toolchain = tomllib.loads((desktop / "rust-toolchain.toml").read_text())
    if rust_version is not None and toolchain["toolchain"]["channel"] != rust_version:
        raise ValueError("workflow RUST_VERSION must match desktop/rust-toolchain.toml")

    ios = root / "mobile/ios"
    checksum = hashlib.sha1((ios / "Podfile").read_bytes()).hexdigest()
    if f"PODFILE CHECKSUM: {checksum}" not in (ios / "Podfile.lock").read_text().splitlines():
        raise ValueError("Podfile.lock is stale; run pod install on macOS and commit the lockfile")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="also require this release tag to match the app version")
    parser.add_argument("--stamp", action="store_true", help="set client versions from --tag before checking")
    args = parser.parse_args()
    try:
        if args.stamp:
            if args.tag is None:
                raise ValueError("--stamp requires --tag")
            stamp(Path(__file__).resolve().parents[2], args.tag)
        check(Path(__file__).resolve().parents[2], args.tag, os.environ.get("RUST_VERSION"))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Release input check failed: {error}\n")
    print("Release versions, Rust toolchain, and Podfile checksum are consistent.")
