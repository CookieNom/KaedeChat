#!/usr/bin/env python3
"""Publish or restore the desktop WebRTC package for this host using ORAS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from prepare_codecs import NATIVE, native_fingerprint, triple, verify_artifact

REPOSITORY = 'CookieNom/KaedeChat'
REGISTRY = 'ghcr.io/cookienom/kaede-webrtc'
LOCK = Path(__file__).with_name('webrtc_packages.json')
ARCHIVE = 'desktop-webrtc.zip'
TAR = 'desktop-webrtc.tar'


def file_digest(path):
    with path.open('rb') as source:
        return 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()


def report(target, entry):
    message = f'Lock entry for {target}\n\n```json\n{json.dumps({target: entry}, indent=2)}\n```\n'
    print(message)
    if summary := os.getenv('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(message)


def require_pin(target, entry, fingerprint):
    if entry.get('fingerprint') != fingerprint or not re.fullmatch(r'sha256:[a-f0-9]{64}', entry.get('digest', '')):
        raise SystemExit(f'No matching registry pin for {target}. Run the Publish desktop WebRTC workflow '
                         f'on the candidate branch and update {LOCK.name} with its job summary.')


def plan(target, tag, entry, fingerprint):
    result = subprocess.run(['oras', 'resolve', tag], capture_output=True, text=True)
    pinned = entry.get('fingerprint') == fingerprint
    if result.returncode == 0:
        digest = result.stdout.strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            raise SystemExit(f'Unexpected registry digest for {tag}: {digest!r}')
        if pinned and digest != entry['digest']:
            raise SystemExit(f'{tag} differs from the committed digest. Restore the original package tag.')
        report(target, entry if pinned else {'fingerprint': fingerprint, 'digest': digest})
        mode = 'ready'
    else:
        missing = re.search(r'not found|manifest_unknown|name_unknown|\b404\b', result.stderr, re.I)
        # GHCR can deny lookups until the first package exists. Both the seed
        # ZIP and its OCI manifest must match the pin before any registry write.
        denied = re.search(r'\b(denied|unauthorized|401|403)\b', result.stderr, re.I)
        if pinned and 'seed' in entry and (missing or denied):
            print(result.stderr, file=sys.stderr)
            mode = 'seed'
        elif missing and not pinned:
            mode = 'build'
        elif missing:
            raise SystemExit(f'Pinned package {tag} is missing. Restore it from its original digest.')
        else:
            raise SystemExit(result.stderr)
    print(f'{target}: {mode}')
    if output := os.getenv('GITHUB_OUTPUT'):
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'mode={mode}\n')


def unpack(archive, directory, entry):
    seed = entry.get('seed')
    if seed and file_digest(archive) != seed['digest']:
        raise SystemExit('Seed ZIP differs from the original GitHub Actions artifact digest')
    directory.mkdir()
    with zipfile.ZipFile(archive) as package:
        if package.namelist() != [TAR]:
            raise SystemExit(f'Expected a single {TAR} in the WebRTC package')
        with package.open(TAR) as data, tarfile.open(fileobj=data, mode='r|') as tar:
            tar.extractall(directory, filter='data')
    if seed:
        verify_artifact(directory, expected_fingerprint=seed['fingerprint'])
        # The first four packages predate per-target fingerprints. Translate
        # the verified marker so local Make and Cargo use the normal checks.
        marker = directory / 'kaede-build.json'
        manifest = json.loads(marker.read_text())
        manifest['fingerprint'] = entry['fingerprint']
        marker.write_text(json.dumps(manifest, indent=2) + '\n')
    verify_artifact(directory)


def pull(target, entry, fingerprint):
    require_pin(target, entry, fingerprint)
    NATIVE.mkdir(parents=True, exist_ok=True)
    destination = NATIVE / 'webrtc'
    if destination.is_symlink() or (destination.exists() and not (destination / 'kaede-build.json').is_file()
                                    and any(destination.iterdir())):
        raise SystemExit(f'Move the unrecognized directory at {destination} before restoring WebRTC')
    with tempfile.TemporaryDirectory(dir=NATIVE, prefix='codec-restore-') as temp:
        work = Path(temp)
        subprocess.run(['oras', 'pull', f"{REGISTRY}@{entry['digest']}", '-o', str(work)], check=True)
        staged = work / 'unpacked'
        unpack(work / ARCHIVE, staged, entry)
        backup = work / 'previous'
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except OSError:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    print(f'Installed {target} WebRTC at {destination}')


def publish(target, tag, entry, fingerprint, seed=False):
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        archive = work / ARCHIVE
        if seed:
            require_pin(target, entry, fingerprint)
            artifact_id = entry['seed']['artifact_id']
            with archive.open('wb') as output:
                subprocess.run(['gh', 'api', f'repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip'],
                               stdout=output, check=True)
            unpack(archive, work / 'verified', entry)
        else:
            directory = verify_artifact(NATIVE / 'webrtc')
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as package:
                with package.open(TAR, 'w', force_zip64=True) as data, tarfile.open(fileobj=data, mode='w|') as tar:
                    tar.add(directory, arcname='.')
        # Build the manifest locally first. A stable creation annotation makes a
        # promoted seed byte-identical on Linux, Windows, and macOS.
        subprocess.run(['oras', 'push', '--oci-layout', 'layout:package', '--artifact-type',
                        'application/vnd.kaede.webrtc.v1', '--annotation', 'org.opencontainers.image.created=1970-01-01T00:00:00Z',
                        '--annotation', f'org.opencontainers.image.source=https://github.com/{REPOSITORY}',
                        '--export-manifest', 'manifest.json', ARCHIVE + ':application/zip'], cwd=work, check=True)
        digest = file_digest(work / 'manifest.json')
        if seed and digest != entry['digest']:
            raise SystemExit(f'Seed manifest digest {digest} differs from pin {entry["digest"]}')
        subprocess.run(['oras', 'cp', '--from-oci-layout', f'layout@{digest}', tag], cwd=work, check=True)
        remote = subprocess.check_output(['oras', 'resolve', tag], text=True).strip()
        if remote != digest:
            raise SystemExit(f'{tag} resolved to {remote} after publishing {digest}')
        report(target, entry if seed else {'fingerprint': fingerprint, 'digest': digest})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['pull', 'plan', 'publish', 'seed'])
    command = parser.parse_args().command
    target = triple()
    fingerprint = native_fingerprint(target)
    entry = json.loads(LOCK.read_text()).get(target, {})
    tag = f'{REGISTRY}:{target}-{fingerprint}'
    if command == 'pull':
        pull(target, entry, fingerprint)
    elif command == 'plan':
        plan(target, tag, entry, fingerprint)
    else:
        publish(target, tag, entry, fingerprint, seed=command == 'seed')


if __name__ == '__main__':
    main()
