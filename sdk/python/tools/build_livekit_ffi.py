#!/usr/bin/env python3
"""Build the optional Linux x86_64 Python LiveKit 1.1.18 library for Kaede calls."""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# The livekit Python rtc-v1.1.18 release pins this FFI and its Cargo.lock.
RUST_SDK = "2d9f01ab1e933a86a8a5c53805ee29ee58b9be1b"
WEBRTC = "89d790b40447c3c5c54c3edd58aa53d285e35fa7"
WEBRTC_ZIP = "https://github.com/livekit/rust-sdks/releases/download/webrtc-89d790b/webrtc-linux-x64-release.zip"
WEBRTC_SHA256 = "b167adad5291cea0e4d66a0454d9d52d2ad714e6b0ed70f4410317d3ebde70c5"
CRYPTOR_URL = f"https://raw.githubusercontent.com/webrtc-sdk/webrtc/{WEBRTC}/api/crypto/frame_crypto_transformer.cc"
CRYPTOR_SHA256 = "a0ad0713525437fd53dd23127e10a093442a4e7f827f22bd0b54907626c582da"
PATCH = Path(__file__).resolve().with_name("livekit_ffi.patch")
TARGET = "x86_64-unknown-linux-gnu"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, expected_sha256: str, path: Path) -> None:
    subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "3",
            "--output",
            path,
            url,
        ],
        check=True,
    )
    if sha256(path) != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch for {path.name}")


def prepare_webrtc(work: Path) -> Path:
    archive = work / "webrtc.zip"
    download(WEBRTC_ZIP, WEBRTC_SHA256, archive)
    # The pinned zip contains symlinks in its build sysroot.
    subprocess.run(["unzip", "-q", archive, "-d", work], check=True)
    webrtc = work / "linux-x64-release"
    library = webrtc / "lib/libwebrtc.a"
    stock_object = "frame_crypto_transformer.o"
    members = subprocess.check_output(["ar", "t", library], text=True).splitlines()
    if members.count(stock_object) != 1:
        raise RuntimeError(
            "Expected one stock WebRTC frame cryptor in the release archive"
        )

    # The stock library and the C++ bridge can both be included in Rust rlibs.
    # Remove its only definition of the KDF so archive/link order cannot select it.
    subprocess.run(["ar", "d", library, stock_object], check=True)
    with tempfile.TemporaryFile(mode="w+") as symbols:
        subprocess.run(
            ["nm", "-g", "--defined-only", library], stdout=symbols, check=True
        )
        symbols.seek(0)
        if any(
            "DoKeyDerivation" in line or "DeriveHkdfSha256FromSecret" in line
            for line in symbols
        ):
            raise RuntimeError(
                "Another stock frame-key implementation is still present"
            )
    return webrtc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir", required=True, type=Path, help="new or empty build directory"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="new artifact directory"
    )
    args = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        parser.error("this build targets Linux x86_64")
    work, output = args.work_dir.resolve(), args.output.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()) or output.exists():
        parser.error("use an empty work directory and a new output directory")

    sdk = work / "rust-sdks"
    subprocess.run(["git", "init", "-q", sdk], check=True)
    subprocess.run(
        [
            "git",
            "fetch",
            "--depth=1",
            "https://github.com/livekit/rust-sdks.git",
            RUST_SDK,
        ],
        cwd=sdk,
        check=True,
    )
    subprocess.run(["git", "checkout", "--detach", RUST_SDK], cwd=sdk, check=True)
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive", "--depth=1"],
        cwd=sdk,
        check=True,
    )
    webrtc = prepare_webrtc(work)
    download(
        CRYPTOR_URL, CRYPTOR_SHA256, sdk / "webrtc-sys/src/frame_crypto_transformer.cc"
    )
    subprocess.run(["git", "apply", PATCH], cwd=sdk, check=True)

    target_dir = work / "target"
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--target",
            TARGET,
            "--target-dir",
            target_dir,
            "-p",
            "livekit-ffi",
        ],
        cwd=sdk,
        env={**os.environ, "LK_CUSTOM_WEBRTC": str(webrtc)},
        check=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as stage:
        bundle = Path(stage) / output.name
        bundle.mkdir()
        shutil.copy2(target_dir / TARGET / "release/liblivekit_ffi.so", bundle)
        shutil.copy2(sdk / "LICENSE", bundle)
        shutil.copy2(sdk / "NOTICE", bundle)
        shutil.copy2(webrtc / "LICENSE.md", bundle / "WEBRTC_LICENSE.md")
        checksums = [
            f"{sha256(path)}  {path.name}\n" for path in sorted(bundle.iterdir())
        ]
        (bundle / "SHA256SUMS").write_text("".join(checksums), encoding="ascii")
        bundle.rename(output)
    print(f"LIVEKIT_LIB_PATH={output / 'liblivekit_ffi.so'}")


if __name__ == "__main__":
    main()
