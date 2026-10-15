#!/usr/bin/env python3
"""Build and inspect a portable x86_64 AppImage from a clean Git snapshot."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def publish_bundle(bundle, destination):
    """Expose only a complete artifact, refusing to replace existing files."""
    checksum = destination.with_name(destination.name + '.sha256')
    owned = False
    checksum_owned = False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.appimage-', delete=False) as target:
            temporary = Path(target.name)
            with bundle.open('rb') as origin:
                shutil.copyfileobj(origin, target)
            target.flush()
            os.fsync(target.fileno())
        with temporary.open('rb') as origin:
            digest = hashlib.file_digest(origin, 'sha256').hexdigest()
        os.link(temporary, destination)
        owned = True
        with checksum.open('x') as output:
            checksum_owned = True
            output.write(f'{digest}  {destination.name}\n')
        return digest
    except BaseException:
        if checksum_owned:
            checksum.unlink(missing_ok=True)
        if owned:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_tree(appdir, version, commit=None):
    info = json.loads((appdir / 'BUILD_INFO.json').read_text())
    if info['version'] != version or (commit is not None and info['commit'] != commit):
        raise ValueError('AppImage source identity mismatch')
    forbidden = {'.git', '.env', 'AGENTS.md', 'PRODUCT_TASKS.md', 'ACCEPTANCE.md', 'ACCEPTANCE_CURRENT.md'}
    for path in appdir.rglob('*'):
        if path.name in forbidden or path.suffix in ('.pem', '.key'):
            # CA public certificates use .crt; private key formats never ship.
            raise ValueError(f'Private/build file in AppImage: {path.relative_to(appdir)}')
    if info.get('architecture') != 'x86_64':
        raise ValueError('AppImage architecture mismatch')
    for relative in ('AppRun', 'usr/bin/python3.12', 'usr/bin/wayvoice', 'usr/bin/wayvoice-settings',
                     'usr/bin/wl-copy', 'usr/bin/pw-record', 'usr/lib/wayvoice/parakeet/bin/nemo-speech'):
        if not os.access(appdir / relative, os.X_OK):
            raise ValueError(f'Missing executable: {relative}')
    if not (appdir / 'usr/lib/wayvoice/runtime/.engine-v1-ready').is_file():
        raise ValueError('Bundled recognition runtime is not ready')
    for relative in ('usr/share/common-licenses/LGPL-2.1',
                     'usr/share/doc/wayvoice/third-party/type2-runtime/LICENSE'):
        if not (appdir / relative).is_file():
            raise ValueError(f'Missing license: {relative}')
    for path in appdir.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(appdir.resolve()):
            raise ValueError(f'External symlink in AppImage: {path.relative_to(appdir)}')


def main(root=None):
    root = root or Path(__file__).resolve().parent.parent
    if platform.machine() != 'x86_64':
        raise SystemExit('Only x86_64 AppImages are supported')
    dirty = run('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no', capture_output=True).stdout
    if dirty:
        raise SystemExit('A clean tracked Git tree is required')
    version = run('python3', str(root / 'scripts/check-version.py'), capture_output=True).stdout.strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise SystemExit('Invalid package version')
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    name = f'wayvoice-{version}-x86_64.AppImage'
    destination = dist / name
    if destination.exists() or destination.with_name(name + '.sha256').exists():
        raise SystemExit(f'Refusing to replace existing artifact: {destination}')
    commit = run('git', '-C', str(root), 'rev-parse', 'HEAD', capture_output=True).stdout.strip()
    build = root / 'build'
    build.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='appimage-release-', dir=build))
    source = work / 'source'
    source.mkdir()
    archive = work / 'source.tar'
    run('git', '-C', str(root), 'archive', '--format=tar', f'--output={archive}', 'HEAD')
    run('tar', '-xf', str(archive), '-C', str(source))
    output = work / 'output'
    output.mkdir()
    print(f'AppImage workspace: {work}', flush=True)
    image = 'wayvoice-appimage-builder:0.8.0'
    run('docker', 'build', '-t', image, '-f', str(source / 'packaging/appimage/Dockerfile'), str(source))
    run('docker', 'run', '--rm', '--init', '--user', f'{os.getuid()}:{os.getgid()}',
        '-e', 'HOME=/tmp/wayvoice-builder', '-v', f'{source}:/source:ro', '-v', f'{output}:/work',
        image, 'python3', '/source/packaging/appimage/build-appdir.py', '--source', '/source',
        '--work', '/work', '--version', version, '--commit', commit)
    actual = output / 'extracted with spaces/squashfs-root'
    validate_tree(actual, version, commit)
    smoke_image = 'wayvoice-appimage-smoke:0.8.0'
    run('docker', 'build', '-t', smoke_image, '-f', str(source / 'packaging/appimage/Dockerfile.smoke'), str(source))
    run('docker', 'run', '--rm', '--init', '--network=none', '--user', f'{os.getuid()}:{os.getgid()}',
        '-e', 'HOME=/tmp/wayvoice-smoke', '-e', 'GTK_A11Y=none', '-e', 'GDK_BACKEND=x11',
        '-v', f'{actual}:/portable copy with spaces:ro', smoke_image,
        'xvfb-run', '-a', 'dbus-run-session', '--', '/portable copy with spaces/AppRun', '--check-runtime')
    run('docker', 'run', '--rm', '--init', '--network=none', '--user', f'{os.getuid()}:{os.getgid()}',
        '-e', 'HOME=/tmp/wayvoice-smoke', '-e', 'GTK_A11Y=none', '-e', 'GDK_BACKEND=x11',
        '-e', 'APPIMAGE_EXTRACT_AND_RUN=1', '-v', f'{output / name}:/Way Voice.AppImage:ro', smoke_image,
        'xvfb-run', '-a', 'dbus-run-session', '--', '/Way Voice.AppImage', '--check-runtime')
    run('docker', 'run', '--rm', '--init', '--network=none', '--user', f'{os.getuid()}:{os.getgid()}',
        '-v', f'{output / name}:/Way Voice.AppImage:ro',
        '-v', f'{source / "packaging/appimage/check-lifecycle.sh"}:/check-lifecycle.sh:ro', smoke_image,
        'sh', '/check-lifecycle.sh', '/Way Voice.AppImage', version, timeout=120)
    digest = publish_bundle(output / name, destination)
    destination.chmod(0o755)
    print(f'Inspected AppImage: {destination}\nSHA256: {digest}', flush=True)


if __name__ == '__main__':
    main()
