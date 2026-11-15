#!/usr/bin/env python3
"""Build the pinned AV1/HEVC cryptor used by Flutter; requires Linux/Android or macOS/iOS."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BUILD_REV = "d6a665312ccdf663e5cbcba2a6837168793d9946"
WEBRTC_REV = "83af826464a3212bf452fcb3a8d0619e7d77a368"
VERSION = "150.7871.06-kaede-video.1"
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
    gn_args = "enable_libaom=true rtc_libvpx_build_vp9=true rtc_use_h265=true rtc_include_dav1d_in_internal_decoder_factory=true rtc_include_tests=false"
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
        # macOS, tvOS or visionOS slices. Use the pinned WebRTC iOS XCFramework builder.
        run(sys.executable, "run.py", "build", "apple", "--webrtc-fetch", "--commit", WEBRTC_REV, cwd=build)
        source = build / "_source/apple/webrtc/src"
        ios_output = source / "out/ios"
        os.environ["PATH"] = str(build / "_source/apple/depot_tools") + os.pathsep + os.environ["PATH"]
        run(sys.executable, str(source / "tools_webrtc/ios/build_ios_libs.py"),
            "--build_config", "release", "--arch", "device:arm64", "simulator:arm64", "simulator:x64",
            # Unlike run.py's Android builder, this direct entry point does
            # not inherit COMMON_GN_ARGS. The SDK's audio module needs RTTI.
            "--extra-gn-args", gn_args + " use_rtti=true ios_deployment_target=\"15.0\"",
            "-o", str(ios_output), cwd=source)
        destination = OUTPUT / "WebRTC.xcframework"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(ios_output / "WebRTC.xcframework", destination, symlinks=True)
    source = build / f"_source/{'apple' if args.platform == 'ios' else 'android'}/webrtc/src"
    # Run the shared Annex-B regression vectors with the host compiler in CI.
    cryptor_test = work / "h26x-frame-test"
    run("c++", "-std=c++20", str(source / "api/crypto/h26x_frame_test.cpp"),
        "-o", str(cryptor_test), cwd=source)
    run(str(cryptor_test), cwd=source)
    (OUTPUT / f"{args.platform}-build.json").write_text(json.dumps({
        "build_revision": BUILD_REV, "webrtc_revision": WEBRTC_REV,
        "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
