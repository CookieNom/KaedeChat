#!/usr/bin/env python3
"""Check 64-bit native library ELF load alignment in an APK or AAB (requires readelf)."""

from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def check(archive):
    failures = []
    checked = 0
    with zipfile.ZipFile(archive) as bundle, tempfile.TemporaryDirectory() as directory:
        library = Path(directory) / "library.so"
        for name in bundle.namelist():
            if not name.endswith(".so") or not any(
                f"/lib/{abi}/" in f"/{name}" for abi in ("arm64-v8a", "x86_64")
            ):
                continue
            checked += 1
            library.write_bytes(bundle.read(name))
            headers = subprocess.check_output(
                ["readelf", "--program-headers", "--wide", str(library)], text=True
            )
            alignments = [
                int(line.split()[-1], 16)
                for line in headers.splitlines()
                if line.lstrip().startswith("LOAD ")
            ]
            if not alignments or any(alignment < 16384 for alignment in alignments):
                failures.append(name)
                print(f"FAIL: {name}: LOAD alignments {alignments}", file=sys.stderr)
        if not checked:
            raise ValueError(f"No 64-bit native libraries found in {archive}")
    print(f"{archive}: checked {checked} libraries; {len(failures)} failed 16 KB alignment")
    return not failures


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: check_android_page_sizes.py <app.apk|app.aab>")
    sys.exit(0 if check(sys.argv[1]) else 1)
