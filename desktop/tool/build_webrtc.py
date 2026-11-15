#!/usr/bin/env python3
"""Build the pinned HEVC-enabled desktop WebRTC archive; run on the target OS in CI."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from prepare_codecs import NATIVE, PINS, ROOT, fingerprint, prepare_sdk, run, triple, verify_artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=NATIVE / 'webrtc')
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    target = triple()
    system, arch = target.split('-')
    sdk = prepare_sdk(args.source)
    build_fingerprint = fingerprint()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if (work / 'src').exists():
        raise SystemExit('Use a clean --work-dir for a native build; restore completed artifacts through CI cache')
    depot = work / 'depot_tools'
    if not depot.exists():
        run('git', 'init', str(depot), cwd=work)
        run('git', 'fetch', '--depth=1', 'https://chromium.googlesource.com/chromium/tools/depot_tools.git', PINS['depot_tools_revision'], cwd=depot)
        run('git', 'checkout', '--detach', 'FETCH_HEAD', cwd=depot)
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=depot, text=True).strip()
    if actual != PINS['depot_tools_revision']:
        raise SystemExit('depot_tools revision mismatch; use a clean --work-dir')
    os.environ['PATH'] = str(depot) + os.pathsep + os.environ['PATH']
    os.environ['DEPOT_TOOLS_UPDATE'] = '0'
    os.environ['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'
    solution = {'name': 'src', 'url': PINS['webrtc_repository'] + '@' + PINS['webrtc_revision'],
                'custom_deps': {}, 'deps_file': 'DEPS', 'managed': False}
    (work / '.gclient').write_text('solutions = ' + repr([solution]) + '\n')
    if system == 'win':
        vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        installation = subprocess.check_output([str(vswhere), '-latest', '-property', 'installationPath'], text=True).strip()
        if not installation:
            raise SystemExit('Visual Studio C++ build tools are required')
        os.environ['vs2022_install'] = installation
        run('cmd', '/c', str(depot / 'gclient.bat'), 'sync', '-D', '--no-history', cwd=work)
    else:
        run(str(depot / 'gclient'), 'sync', '-D', '--no-history', cwd=work)
    source = work / 'src'
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if actual != PINS['webrtc_revision']:
        raise SystemExit('WebRTC revision mismatch')
    patch = ROOT / 'desktop/patches/webrtc-hevc.patch'
    run('git', 'apply', '--check', str(patch), cwd=source)
    run('git', 'apply', str(patch), cwd=source)
    scripts = sdk / 'webrtc-sys/libwebrtc'
    script = {'linux': 'build_linux.sh', 'mac': 'build_macos.sh', 'win': 'build_windows.cmd'}[system]
    if system == 'win':
        run('cmd', '/c', str(scripts / script), '--arch', arch, '--profile', 'release', cwd=work)
    else:
        run('bash', str(scripts / script), '--arch', arch, '--profile', 'release', cwd=work)
    built = work / f'{target}-release'
    if fingerprint() != build_fingerprint:
        raise SystemExit('Codec sources changed during the build; rebuild from a stable checkout')
    (built / 'kaede-build.json').write_text(json.dumps({
        'fingerprint': build_fingerprint, 'target': target, 'webrtc_revision': PINS['webrtc_revision'],
    }, indent=2) + '\n')
    verify_artifact(built)
    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, args.output, symlinks=True)
    print(f'LK_CUSTOM_WEBRTC={args.output.resolve()}')


if __name__ == '__main__':
    main()
