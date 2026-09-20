#!/usr/bin/env python3
"""Install checksum-verified kubectl, k3d and Tilt into this checkout; no sudo."""

import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = {"kubectl": "v1.36.4", "k3d": "v5.8.3", "tilt": "0.37.7"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def main() -> None:
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
    if system not in {"linux", "darwin"}:
        raise SystemExit("Use Linux or macOS for the Kubernetes development tooling")
    directory = ROOT / ".kaede-tools/bin"
    directory.mkdir(parents=True, exist_ok=True)
    k3d_file = f"k3d-{system}-{arch}"
    tilt_arch = "x86_64" if arch == "amd64" else "arm64"
    tilt_system = "mac" if system == "darwin" else system
    tilt_file = f"tilt.{VERSIONS['tilt']}.{tilt_system}.{tilt_arch}.tar.gz"
    sources = {
        "kubectl": (
            f"https://dl.k8s.io/release/{VERSIONS['kubectl']}/bin/{system}/{arch}",
            "kubectl",
            "kubectl.sha256",
        ),
        "k3d": (
            f"https://github.com/k3d-io/k3d/releases/download/{VERSIONS['k3d']}",
            k3d_file,
            "checksums.txt",
        ),
        "tilt": (
            f"https://github.com/tilt-dev/tilt/releases/download/v{VERSIONS['tilt']}",
            tilt_file,
            "checksums.txt",
        ),
    }
    for name, (base, filename, checksum_file) in sources.items():
        marker = directory / f".{name}-version"
        if (
            marker.exists()
            and marker.read_text() == VERSIONS[name]
            and (directory / name).is_file()
        ):
            continue
        print(f"Downloading {name} {VERSIONS[name]}", flush=True)
        checksums = fetch(f"{base}/{checksum_file}").decode().splitlines()
        expected = next(
            line.split()[0]
            for line in checksums
            if len(line.split()) == 1
            or Path(line.split()[-1].lstrip("*")).name == filename
        )
        data = fetch(f"{base}/{filename}")
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit(f"Checksum mismatch for {name}")
        if name == "tilt":
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                data = archive.extractfile("tilt").read()
        temporary = directory / f".{name}.download"
        temporary.write_bytes(data)
        temporary.chmod(0o755)
        os.replace(temporary, directory / name)
        marker.write_text(VERSIONS[name])
    print(f"Tools installed in {directory}")


if __name__ == "__main__":
    main()
