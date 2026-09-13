#!/usr/bin/env python3
"""Build the pinned AV1 cryptor used by Flutter; requires Linux/Android or macOS/iOS."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BUILD_REV = "06e3410d8f67d08202e26f55225705596b60778e"
WEBRTC_REV = "a4bd28d99eb9ed3bc8e41ce7b9ce7254ee7308bd"
VERSION = "137.7151.04-kaede-av1.1"
PATCH = ROOT / "docs/av1-e2ee/webrtc-build.patch"
OUTPUT = ROOT / "mobile/native/webrtc"


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["android", "ios"])
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    required_host = "darwin" if args.platform == "ios" else "linux"
    if sys.platform != required_host:
        parser.error("iOS builds require macOS/Xcode; Android builds require Linux")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    checkout = work / "webrtc-build"
    if not checkout.exists():
        run("git", "init", str(checkout), cwd=work)
        run("git", "remote", "add", "origin", "https://github.com/webrtc-sdk/webrtc-build.git", cwd=checkout)
        run("git", "fetch", "--depth=1", "origin", BUILD_REV, cwd=checkout)
        run("git", "checkout", "--detach", BUILD_REV, cwd=checkout)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if actual != BUILD_REV:
        raise SystemExit("Build checkout has a different revision; use a new --work-dir")
    # The reverse check permits resuming an interrupted build without resetting it.
    applied = subprocess.run(["git", "apply", "--reverse", "--check", str(PATCH)], cwd=checkout,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not applied:
        run("git", "apply", "--check", str(PATCH), cwd=checkout)
        run("git", "apply", str(PATCH), cwd=checkout)
    build = checkout / "build"
    gn_args = "enable_libaom=true rtc_include_dav1d_in_internal_decoder_factory=true rtc_include_tests=false"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.platform == "android":
        run(sys.executable, "run.py", "build", "android", "--webrtc-fetch", "--commit", WEBRTC_REV,
            # In this pinned helper, nobuild skips the additional C++ static
            # libraries, while the Flutter AAR is still compiled and packaged.
            "--webrtc-nobuild", "--webrtc-extra-gn-args", gn_args, cwd=build)
        repo = OUTPUT / "android/chat/kaede/webrtc" / VERSION
        repo.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / "_source/android/webrtc/src/out/aar/libwebrtc.aar", repo / f"webrtc-{VERSION}.aar")
        (repo / f"webrtc-{VERSION}.pom").write_text(
            '<project><modelVersion>4.0.0</modelVersion><groupId>chat.kaede</groupId>'
            f'<artifactId>webrtc</artifactId><version>{VERSION}</version><packaging>aar</packaging></project>\n')
        notice = build / "_source/android/webrtc/src/out/aar/LICENSE.md"
        shutil.copy2(notice, OUTPUT / "android/NOTICE")
    else:
        # The apple target fetches and patches sources without building unrelated
        # macOS, tvOS or visionOS slices. Use M137's own iOS XCFramework builder.
        run(sys.executable, "run.py", "build", "apple", "--webrtc-fetch", "--commit", WEBRTC_REV, cwd=build)
        source = build / "_source/apple/webrtc/src"
        os.environ["PATH"] = str(build / "_source/apple/depot_tools") + os.pathsep + os.environ["PATH"]
        run(sys.executable, str(source / "tools_webrtc/ios/build_ios_libs.py"),
            "--build_config", "release", "--arch", "device:arm64", "simulator:arm64", "simulator:x64",
            "--extra-gn-args", gn_args + " ios_deployment_target=\"15.0\"",
            "-o", str(work / "ios"), cwd=source)
        destination = OUTPUT / "WebRTC.xcframework"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(work / "ios/WebRTC.xcframework", destination, symlinks=True)
    (OUTPUT / f"{args.platform}-build.json").write_text(json.dumps({
        "build_revision": BUILD_REV, "webrtc_revision": WEBRTC_REV,
        "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
