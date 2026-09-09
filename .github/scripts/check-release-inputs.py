"""Check release metadata before native builds; uses Python 3.11+ stdlib only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib


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
    args = parser.parse_args()
    try:
        check(Path(__file__).resolve().parents[2], args.tag, os.environ.get("RUST_VERSION"))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Release input check failed: {error}\n")
    print("Release versions, Rust toolchain, and Podfile checksum are consistent.")
