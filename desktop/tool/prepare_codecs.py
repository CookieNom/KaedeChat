#!/usr/bin/env python3
"""Prepare the pinned, patched desktop SDK and CUDA headers without compiling code."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'desktop/native'
SOURCES = Path(__file__).with_name('codec_sources.json')
PINS = json.loads(SOURCES.read_text())
SDK = NATIVE / 'rust-sdks'
PATCHES = [
    ROOT / 'desktop/patches/livekit-codec-integration.patch',
    ROOT / 'desktop/patches/livekit-hardware-codecs.patch',
    ROOT / 'desktop/patches/livekit-media-foundation.patch',
    ROOT / 'desktop/patches/livekit-hevc-decode-probe.patch',
    ROOT / 'docs/av1-e2ee/native-interop.patch',
    ROOT / 'desktop/patches/livekit-frame-keys.patch',
    ROOT / 'desktop/patches/livekit-windows-voice-startup.patch',
    ROOT / 'desktop/patches/livekit-remove-track-error.patch',
    ROOT / 'desktop/patches/livekit-windows-exceptions.patch',
]
WEBRTC_PATCH = ROOT / 'desktop/patches/webrtc-hevc.patch'
BUILD_SCRIPTS = {'linux': 'build_linux.sh', 'mac': 'build_macos.sh', 'win': 'build_windows.cmd'}
XCODE_VERSION = '26.3'


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def fingerprint():
    digest = hashlib.sha256(SOURCES.read_bytes())
    for path in [Path(__file__), Path(__file__).with_name('build_webrtc.py'), *PATCHES, WEBRTC_PATCH]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def native_fingerprint(target):
    """Identify the archive on any host, before fetching the SDK or WebRTC."""
    system, _ = target.split('-')
    toolchain = {'linux': 'ubuntu-24.04', 'mac': f'macos-15/Xcode-{XCODE_VERSION}',
                 'win': 'windows-2025/Visual Studio (latest)'}[system]
    inputs = {name: PINS[name] for name in (
        'sdk_repository', 'sdk_revision', 'webrtc_repository', 'webrtc_revision', 'depot_tools_revision',
    )}
    inputs.update(target=target, toolchain=toolchain, script=BUILD_SCRIPTS[system])
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode())
    for path in (Path(__file__).with_name('build_webrtc.py'), WEBRTC_PATCH):
        # Git for Windows can use CRLF in the checkout.
        digest.update(path.read_text(encoding='utf-8').encode())

    # The pinned SDK supplies the upstream recipes. Our mixed SDK patches also
    # modify Rust/C++ bindings that Cargo compiles separately from the archive.
    directory = 'webrtc-sys/libwebrtc/'
    other_scripts = {directory + name for os_name, name in BUILD_SCRIPTS.items() if os_name != system}
    for patch in PATCHES:
        for section in re.split(r'(?m)(?=^diff --git )', patch.read_text(encoding='utf-8')):
            header = section.partition('\n')[0]
            if directory not in header:
                continue
            if any(header == f'diff --git a/{name} b/{name}' for name in other_scripts):
                continue
            digest.update(section.encode())
    return digest.hexdigest()


def triple():
    system = {'Windows': 'win', 'Darwin': 'mac', 'Linux': 'linux'}[platform.system()]
    arch = {'AMD64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}[platform.machine()]
    return f'{system}-{arch}'


def prepare_sdk(source=None):
    expected = {'revision': PINS['sdk_revision'], 'fingerprint': fingerprint()}
    marker = SDK / 'kaede-source.json'
    if marker.is_file() and json.loads(marker.read_text()) == expected:
        return SDK
    if SDK.exists() and not marker.exists():
        raise SystemExit(f'{SDK} is not a generated Kaede SDK; move it before preparing codecs')
    NATIVE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='codec-source-', dir=NATIVE) as temp:
        temp = Path(temp)
        prepared = temp / 'sdk'
        if source is None:
            run('git', 'init', str(prepared), cwd=temp)
            run('git', 'fetch', '--depth=1', PINS['sdk_repository'], PINS['sdk_revision'], cwd=prepared)
            actual = subprocess.check_output(['git', 'rev-parse', 'FETCH_HEAD'], cwd=prepared, text=True).strip()
        else:
            source = source.resolve()
            actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        if actual != PINS['sdk_revision']:
            raise SystemExit('SDK source revision differs from codec_sources.json')
        if source is not None:
            run('git', 'clone', '--no-checkout', '--no-hardlinks', str(source), str(prepared), cwd=temp)
        run('git', 'config', 'core.autocrlf', 'false', cwd=prepared)
        run('git', 'checkout', '--detach', PINS['sdk_revision'], cwd=prepared)
        # The native interop fixture also builds libyuv and protocol code from pinned submodules.
        if source is not None:
            for name, path in [('livekit/protocol', 'livekit-protocol/protocol'), ('yuv-sys/libyuv', 'yuv-sys/libyuv')]:
                if (source / path / '.git').exists():
                    run('git', 'config', f'submodule.{name}.url', str(source / path), cwd=prepared)
        run('git', '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive', '--depth=1', cwd=prepared)
        for patch in PATCHES:
            run('git', 'apply', '--check', str(patch), cwd=prepared)
            run('git', 'apply', str(patch), cwd=prepared)
        # Preserve the runtime's original source identity in the app and native interop fixture.
        manifest = prepared / 'webrtc-sys/Cargo.toml'
        manifest.write_text(manifest.read_text().replace(
            'webrtc-sys-build = { workspace = true }', 'webrtc-sys-build = "=0.3.20"',
        ))
        manifest = prepared / 'Cargo.toml'
        manifest.write_text(manifest.read_text().replace(
            'webrtc-sys-build = { version = "0.3.19", path = "webrtc-sys/build" }',
            'webrtc-sys-build = "=0.3.20"',
        ).replace(
            'livekit-runtime = { version = "0.4.0", path = "livekit-runtime", default-features = false }',
            'livekit-runtime = { git = "' + PINS['sdk_repository'].removesuffix('.git') + '", rev = "' + PINS['sdk_revision'] + '", default-features = false }',
        ))
        if fingerprint() != expected['fingerprint']:
            raise SystemExit('Codec patches changed during source preparation; retry from a stable checkout')
        (prepared / 'kaede-source.json').write_text(json.dumps(expected, indent=2) + '\n')
        if SDK.exists():
            shutil.rmtree(SDK)
        prepared.rename(SDK)
    return SDK


def prepare_cuda():
    target = triple()
    if target.startswith('mac-'):
        return None
    entry = PINS['cuda_headers'][target]
    destination = NATIVE / 'cuda'
    marker = destination / 'kaede-headers.json'
    if marker.exists() and json.loads(marker.read_text()) == entry and (destination / 'include/cuda.h').is_file():
        return destination
    with urllib.request.urlopen(entry['url'], timeout=60) as response:
        wheel = response.read(32 * 1024 * 1024)
    if hashlib.sha256(wheel).hexdigest() != entry['sha256']:
        raise SystemExit('CUDA header wheel hash mismatch')
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(wheel)) as bundle:
        for member in bundle.namelist():
            prefix = 'nvidia/cuda_runtime/include/'
            if not member.startswith(prefix) or member.endswith('/'):
                continue
            relative = Path(member.removeprefix(prefix))
            if relative.is_absolute() or '..' in relative.parts:
                raise SystemExit('Unsafe CUDA header path')
            path = destination / 'include' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bundle.read(member))
        for member in bundle.namelist():
            if member.endswith('/License.txt') or member.endswith('/LICENSE'):
                (destination / 'LICENSE').write_bytes(bundle.read(member))
    if not (destination / 'include/cuda.h').is_file():
        raise SystemExit('NVIDIA wheel did not contain cuda.h')
    marker.write_text(json.dumps(entry, indent=2) + '\n')
    return destination


def verify_artifact(path, *, expected_fingerprint=None):
    if not (path / 'kaede-build.json').is_file():
        raise SystemExit('Missing custom WebRTC artifact. Restore the matching Desktop WebRTC package into desktop/native/webrtc or build it with desktop/tool/build_webrtc.py, then set LK_CUSTOM_WEBRTC to that directory.')
    manifest = json.loads((path / 'kaede-build.json').read_text())
    target = triple()
    if expected_fingerprint is None:
        expected_fingerprint = native_fingerprint(target)
    if manifest != {'fingerprint': expected_fingerprint, 'target': target, 'webrtc_revision': PINS['webrtc_revision']}:
        raise SystemExit('Desktop WebRTC artifact does not match this source revision and target')
    args = [line.split('#', 1)[0].strip().split('=', 1)
            for line in (path / 'args.gn').read_text().splitlines()]
    hevc = [value.strip() for parts in args if len(parts) == 2
            for key, value in [parts] if key.strip() == 'rtc_use_h265']
    if hevc != ['true']:
        raise SystemExit('Desktop WebRTC artifact was built without HEVC')
    library = 'webrtc.lib' if target.startswith('win-') else 'libwebrtc.a'
    for required in [path / 'lib' / library, path / 'webrtc.ninja', path / 'desktop_capture.ninja', path / 'LICENSE.md']:
        if not required.is_file() or required.stat().st_size == 0:
            raise SystemExit(f'Missing WebRTC artifact file: {required}')
    if not (path / 'include/api/peer_connection_interface.h').is_file():
        raise SystemExit('Missing WebRTC artifact headers')
    return path.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Reuse a checkout at the exact pinned SDK revision')
    parser.add_argument('--cuda', action='store_true', help='Fetch verified NVIDIA headers without installing CUDA')
    parser.add_argument('--artifact', type=Path, help='Verify the matching custom WebRTC artifact')
    parser.add_argument('--github-env', action='store_true')
    args = parser.parse_args()
    artifact = verify_artifact(args.artifact) if args.artifact else None
    sdk = prepare_sdk(args.source)
    values = {'KAEDE_CODEC_SDK': str(sdk)}
    if args.cuda:
        cuda = prepare_cuda()
        if cuda:
            values['CUDA_HOME'] = str(cuda)
    if artifact:
        values['LK_CUSTOM_WEBRTC'] = str(artifact)
    if args.github_env:
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as output:
            for name, value in values.items():
                output.write(f'{name}={value}\n')
    for name, value in values.items():
        print(f'{name}={value}')


if __name__ == '__main__':
    main()
