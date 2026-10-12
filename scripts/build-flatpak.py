#!/usr/bin/env python3
"""Build and inspect an x86_64 release bundle without installing it."""
import configparser
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

APP_ID = 'io.github.stepan.WayVoice'
REF = f'app/{APP_ID}/x86_64/stable'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def validate_tree(output, version):
    metadata = configparser.ConfigParser()
    metadata.read(output / 'metadata')
    app = metadata['Application']
    expected = {'name': APP_ID, 'runtime': 'org.gnome.Platform/x86_64/50',
                'sdk': 'org.gnome.Sdk/x86_64/50', 'command': 'wayvoice-settings'}
    for key, value in expected.items():
        if app.get(key) != value:
            raise ValueError(f'Unexpected Flatpak {key}: {app.get(key)!r}')
    files = output / 'files'
    init = files / 'lib/wayvoice/app/src/wayvoice/__init__.py'
    if f'__version__ = "{version}"' not in init.read_text():
        raise ValueError('Bundled application version mismatch')
    for name in ('wayvoice', 'wayvoice-settings', 'wayvoice-daemon', 'wl-copy', 'wl-paste'):
        if not os.access(files / 'bin' / name, os.X_OK):
            raise ValueError(f'Missing executable: {name}')
    runtime = files / 'lib/wayvoice/runtime'
    if not (runtime / '.engine-v1-ready').is_file():
        raise ValueError('Bundled recognition runtime is not ready')
    forbidden = {'.git', '.env', 'AGENTS.md', 'PRODUCT_TASKS.md', 'ACCEPTANCE.md',
                 'ACCEPTANCE_CURRENT.md', 'MANUAL_ACCEPTANCE_GUIDE_LOCAL.md'}
    for path in files.rglob('*'):
        if path.name in forbidden or path.suffix in ('.pem', '.key'):
            raise ValueError(f'Private/build file in bundle: {path.relative_to(files)}')


def publish_bundle(bundle, destination):
    """Expose only a complete artifact, refusing to replace existing files."""
    checksum = destination.with_name(destination.name + '.sha256')
    owned = False
    checksum_owned = False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.flatpak-', delete=False) as target:
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


def main(root=None):
    root = root or Path(__file__).resolve().parent.parent
    if platform.machine() != 'x86_64':
        raise SystemExit('Only x86_64 release bundles are supported')
    dirty = run('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no',
                capture_output=True).stdout
    if dirty:
        raise SystemExit('A clean tracked Git tree is required')
    version = run('python3', str(root / 'scripts/check-version.py'), capture_output=True).stdout.strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise SystemExit('Invalid release version')
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    name = f'wayvoice-{version}-x86_64.flatpak'
    destination = dist / name
    checksum = dist / (name + '.sha256')
    if destination.exists() or checksum.exists():
        raise SystemExit(f'Refusing to replace existing artifact: {destination}')
    build = root / 'build'
    build.mkdir(exist_ok=True)
    # Retain unique working directories and logs for inspection; never delete a user's build.
    work = Path(tempfile.mkdtemp(prefix='flatpak-release-', dir=build))
    print(f'Build workspace: {work}', flush=True)
    source = work / 'source'
    source.mkdir()
    archive = work / 'source.tar'
    run('git', '-C', str(root), 'archive', '--format=tar', f'--output={archive}', 'HEAD')
    run('tar', '-xf', str(archive), '-C', str(source))
    output, repo = work / 'output', work / 'repo'
    run('flatpak-builder', '--arch=x86_64', '--default-branch=stable',
        f'--state-dir={work / "state"}', str(output), str(source / f'{APP_ID}.json'), cwd=source)
    validate_tree(output, version)
    # Execute only an import probe in the build sandbox: no app, microphone or model download.
    run('flatpak', 'build', '--unshare=network', str(output),
        '/app/lib/wayvoice/runtime/bin/python', '-c',
        'import av, ctranslate2, faster_whisper, onnxruntime; print("Recognition runtime imports OK")')
    run('flatpak', 'build-export', '--arch=x86_64', str(repo), str(output), 'stable')
    bundle = work / name
    run('flatpak', 'build-bundle', '--arch=x86_64',
        '--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo',
        str(repo), str(bundle), APP_ID, 'stable')
    # Re-import the actual bundle into a fresh repository, without installation.
    inspection = work / 'inspection-repo'
    run('ostree', f'--repo={inspection}', 'init', '--mode=archive-z2')
    run('flatpak', 'build-import-bundle', str(inspection), str(bundle))
    exported = run('ostree', f'--repo={repo}', 'rev-parse', REF, capture_output=True).stdout
    imported = run('ostree', f'--repo={inspection}', 'rev-parse', REF, capture_output=True).stdout
    if exported != imported:
        raise ValueError('Bundle commit does not match inspected build')
    digest = publish_bundle(bundle, destination)
    print(f'Inspected bundle: {destination}\nSHA256: {digest}')


if __name__ == '__main__':
    main()
